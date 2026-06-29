#!/usr/bin/env python3
"""Import normalized Garmin export staging JSON into non-destructive DB tables."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from psycopg2.extras import Json, execute_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import _ensure_garmin_staging_tables, get_db


ACTIVITY_COLUMNS = [
    "source_activity_id",
    "source",
    "name",
    "sport",
    "sport_type",
    "start_time_utc",
    "start_time_local",
    "duration_s",
    "moving_duration_s",
    "elapsed_duration_s",
    "distance_km",
    "ascent_m",
    "descent_m",
    "min_elevation_m",
    "max_elevation_m",
    "avg_speed_kmh",
    "max_speed_kmh",
    "computed_avg_speed_kmh",
    "avg_hr_bpm",
    "max_hr_bpm",
    "avg_cadence",
    "max_cadence",
    "calories",
    "temperature_min_c",
    "temperature_max_c",
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "device_id",
    "lap_count",
    "favorite",
    "pr",
    "power_available",
    "avg_power_w",
    "max_power_w",
    "normalized_power_w",
    "efficiency_speed_hr",
    "is_probable_real_activity",
    "source_batch",
    "is_active_snapshot",
    "raw_json",
]

GEAR_COLUMNS = [
    "source_gear_id",
    "source",
    "uuid",
    "name",
    "type",
    "status",
    "model",
    "date_begin",
    "max_distance_km",
    "raw_json",
]

SLEEP_COLUMNS = [
    "source",
    "calendar_date",
    "sleep_start_gmt",
    "sleep_end_gmt",
    "sleep_start_local",
    "sleep_end_local",
    "duration_s",
    "deep_sleep_s",
    "light_sleep_s",
    "rem_sleep_s",
    "awake_s",
    "sleep_score",
    "confidence",
    "notes",
    "raw_json",
]


def _resolve_data_path(path: Path) -> Path:
    if path.exists():
        return path
    compressed = Path(f"{path}.gz")
    if compressed.exists():
        return compressed
    raise FileNotFoundError(path)


def _read_data_bytes(path: Path) -> bytes:
    resolved = _resolve_data_path(path)
    if resolved.suffix == ".gz":
        with gzip.open(resolved, "rb") as handle:
            return handle.read()
    return resolved.read_bytes()


def _load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(_read_data_bytes(path).decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [row for row in data if isinstance(row, dict)]


def _row_values(row: dict[str, Any], columns: list[str]) -> list[Any]:
    values = []
    for col in columns:
        if col == "raw_json":
            values.append(Json(row.get("raw") or {}))
        else:
            values.append(row.get(col))
    return values


def _upsert_many(conn, table: str, columns: list[str], rows: list[dict[str, Any]], conflict: str) -> int:
    if not rows:
        return 0
    col_sql = ", ".join(columns)
    update_cols = [c for c in columns if c not in conflict.split(", ") and c != "raw_json"]
    update_sql = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
    if "raw_json" in columns:
        update_sql += (", " if update_sql else "") + "raw_json=EXCLUDED.raw_json"
    sql = f"""
        INSERT INTO {table} ({col_sql})
        VALUES %s
        ON CONFLICT ({conflict}) DO UPDATE SET {update_sql}, imported_at=NOW()
    """
    with conn.cursor() as cur:
        execute_values(
            cur,
            sql,
            [_row_values(row, columns) for row in rows],
            page_size=500,
        )
    conn.commit()
    return len(rows)


def _default_batch_id(staging_dir: Path) -> str:
    activities_path = staging_dir / "garmin_activities_clean.json"
    digest = hashlib.sha256(_read_data_bytes(activities_path)).hexdigest()[:12]
    return f"garmin_{digest}"


def import_staging(
    staging_dir: Path,
    dry_run: bool = True,
    activate_snapshot: bool = False,
    batch_id: str | None = None,
) -> dict[str, Any]:
    activities = _load(staging_dir / "garmin_activities_clean.json")
    gear = _load(staging_dir / "garmin_gear_clean.json")
    sleep = _load(staging_dir / "garmin_sleep_clean.json")
    resolved_batch_id = batch_id or _default_batch_id(staging_dir)

    for activity in activities:
        activity["source_batch"] = resolved_batch_id
        activity["is_active_snapshot"] = bool(activate_snapshot)

    counts = {
        "activities": len(activities),
        "gear": len(gear),
        "sleep": len(sleep),
        "real_activity_candidates": sum(1 for row in activities if row.get("is_probable_real_activity")),
        "source_batch": resolved_batch_id,
        "activate_snapshot": activate_snapshot,
    }
    if dry_run:
        return {"dry_run": True, "counts": counts, "imported": None}

    conn = get_db()
    if not conn:
        raise RuntimeError("No DATABASE_URL available or DB connection failed")
    _ensure_garmin_staging_tables(conn)

    imported = {
        "activities": _upsert_many(
            conn,
            "garmin_export_activities",
            ACTIVITY_COLUMNS,
            activities,
            "source_activity_id",
        ),
        "gear": _upsert_many(
            conn,
            "garmin_export_gear",
            GEAR_COLUMNS,
            gear,
            "source_gear_id",
        ),
        "sleep": _upsert_many(
            conn,
            "garmin_export_sleep",
            SLEEP_COLUMNS,
            sleep,
            "calendar_date, sleep_start_gmt, sleep_end_gmt",
        ),
    }
    activated = None
    if activate_snapshot:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE garmin_export_activities
                SET is_active_snapshot = (source_batch = %s)
                """,
                (resolved_batch_id,),
            )
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE is_active_snapshot IS TRUE),
                    COUNT(*) FILTER (WHERE is_active_snapshot IS FALSE),
                    MAX(start_time_utc)
                FROM garmin_export_activities
                """
            )
            active, inactive, cutoff = cur.fetchone()
        conn.commit()
        activated = {
            "source_batch": resolved_batch_id,
            "active_activities": active,
            "inactive_activities": inactive,
            "garmin_cutoff_utc": cutoff,
        }
    return {
        "dry_run": False,
        "counts": counts,
        "imported": imported,
        "activated_snapshot": activated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Garmin normalized staging data into DB staging tables.")
    parser.add_argument("--staging-dir", default="reports/staging", help="Directory with normalized staging JSON")
    parser.add_argument("--execute", action="store_true", help="Actually write to the database")
    parser.add_argument(
        "--activate-snapshot",
        action="store_true",
        help="Mark this activity set as the only active Garmin reference snapshot",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="Optional stable name for this Garmin snapshot",
    )
    args = parser.parse_args()

    result = import_staging(
        Path(args.staging_dir),
        dry_run=not args.execute,
        activate_snapshot=args.activate_snapshot,
        batch_id=args.batch_id or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.execute:
        print("Dry run only. Re-run with --execute to write staging tables.")


if __name__ == "__main__":
    main()
