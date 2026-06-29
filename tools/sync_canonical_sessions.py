#!/usr/bin/env python3
"""Synchronize the active Garmin snapshot into clean_sessions without deletes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import (
    _ensure_canonical_sessions_view,
    _ensure_clean_sessions_table,
    _ensure_garmin_staging_tables,
    get_db,
)


def _one(cur, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def sync_active_garmin(execute: bool = False) -> dict[str, Any]:
    conn = get_db()
    if not conn:
        raise RuntimeError("No DATABASE_URL available or DB connection failed")

    _ensure_garmin_staging_tables(conn)
    _ensure_clean_sessions_table(conn)

    with conn.cursor() as cur:
        active_count = _one(
            cur,
            "SELECT COUNT(*) FROM garmin_export_activities WHERE is_active_snapshot IS TRUE",
        )
        cutoff = _one(
            cur,
            "SELECT MAX(start_time_utc) FROM garmin_export_activities "
            "WHERE is_active_snapshot IS TRUE",
        )
        missing = _one(
            cur,
            """
            SELECT COUNT(*)
            FROM garmin_export_activities ga
            LEFT JOIN clean_sessions cs
              ON cs.clean_session_id = 'garmin:' || ga.source_activity_id
            WHERE ga.is_active_snapshot IS TRUE
              AND cs.clean_session_id IS NULL
            """,
        )
        stale = _one(
            cur,
            """
            SELECT COUNT(*)
            FROM clean_sessions cs
            LEFT JOIN garmin_export_activities ga
              ON ga.source_activity_id = cs.source_activity_id
             AND ga.is_active_snapshot IS TRUE
            WHERE cs.source = 'garmin_export'
              AND ga.source_activity_id IS NULL
            """,
        )

        preview = {
            "active_garmin_activities": active_count,
            "garmin_cutoff_utc": cutoff,
            "new_clean_sessions_to_insert": missing,
            "inactive_garmin_rows_retained_but_hidden": stale,
        }
        if not execute:
            return {"dry_run": True, "preview": preview, "synced": None}

        cur.execute("""
            INSERT INTO clean_sessions (
                clean_session_id, source, source_activity_id, name, sport, sport_type,
                start_time, start_date, duration_s, moving_duration_s, elapsed_duration_s,
                distance_km, ascent_m, descent_m, avg_speed_kmh, max_speed_kmh,
                avg_hr_bpm, max_hr_bpm, avg_cadence, max_cadence, calories,
                start_lat, start_lon, end_lat, end_lon, route_id, power_available,
                avg_power_w, max_power_w, normalized_power_w,
                efficiency_speed_hr, quality, quality_notes, raw_json
            )
            SELECT
                'garmin:' || source_activity_id,
                'garmin_export',
                source_activity_id,
                name,
                sport,
                sport_type,
                COALESCE(start_time_utc, start_time_local),
                start_time_local::date,
                duration_s,
                moving_duration_s,
                elapsed_duration_s,
                distance_km,
                ascent_m,
                descent_m,
                avg_speed_kmh,
                max_speed_kmh,
                avg_hr_bpm,
                max_hr_bpm,
                avg_cadence,
                max_cadence,
                calories,
                start_lat,
                start_lon,
                end_lat,
                end_lon,
                CASE
                    WHEN start_lat IS NOT NULL AND start_lon IS NOT NULL
                         AND end_lat IS NOT NULL AND end_lon IS NOT NULL
                         AND distance_km IS NOT NULL
                    THEN md5(concat_ws('|',
                        COALESCE(sport, ''),
                        ROUND(distance_km::numeric, 1)::text,
                        COALESCE((ROUND(COALESCE(ascent_m, 0)::numeric / 25) * 25)::text, '0'),
                        ROUND(start_lat::numeric, 3)::text,
                        ROUND(start_lon::numeric, 3)::text,
                        ROUND(end_lat::numeric, 3)::text,
                        ROUND(end_lon::numeric, 3)::text
                    ))
                    ELSE NULL
                END,
                power_available,
                avg_power_w,
                max_power_w,
                normalized_power_w,
                efficiency_speed_hr,
                CASE WHEN is_probable_real_activity THEN 'clean' ELSE 'garmin_summary' END,
                'Active Garmin reference snapshot: ' || COALESCE(source_batch, 'unversioned'),
                raw_json
            FROM garmin_export_activities
            WHERE is_active_snapshot IS TRUE
            ON CONFLICT (clean_session_id) DO UPDATE SET
                source = EXCLUDED.source,
                source_activity_id = EXCLUDED.source_activity_id,
                name = EXCLUDED.name,
                sport = EXCLUDED.sport,
                sport_type = EXCLUDED.sport_type,
                start_time = EXCLUDED.start_time,
                start_date = EXCLUDED.start_date,
                duration_s = EXCLUDED.duration_s,
                moving_duration_s = EXCLUDED.moving_duration_s,
                elapsed_duration_s = EXCLUDED.elapsed_duration_s,
                distance_km = EXCLUDED.distance_km,
                ascent_m = EXCLUDED.ascent_m,
                descent_m = EXCLUDED.descent_m,
                avg_speed_kmh = EXCLUDED.avg_speed_kmh,
                max_speed_kmh = EXCLUDED.max_speed_kmh,
                avg_hr_bpm = EXCLUDED.avg_hr_bpm,
                max_hr_bpm = EXCLUDED.max_hr_bpm,
                avg_cadence = EXCLUDED.avg_cadence,
                max_cadence = EXCLUDED.max_cadence,
                calories = EXCLUDED.calories,
                start_lat = EXCLUDED.start_lat,
                start_lon = EXCLUDED.start_lon,
                end_lat = EXCLUDED.end_lat,
                end_lon = EXCLUDED.end_lon,
                route_id = EXCLUDED.route_id,
                power_available = EXCLUDED.power_available,
                avg_power_w = EXCLUDED.avg_power_w,
                max_power_w = EXCLUDED.max_power_w,
                normalized_power_w = EXCLUDED.normalized_power_w,
                efficiency_speed_hr = EXCLUDED.efficiency_speed_hr,
                quality = EXCLUDED.quality,
                quality_notes = EXCLUDED.quality_notes,
                raw_json = EXCLUDED.raw_json
        """)
        upserted = cur.rowcount

    conn.commit()
    _ensure_canonical_sessions_view(conn)

    with conn.cursor() as cur:
        canonical_count = _one(cur, "SELECT COUNT(*) FROM canonical_sessions")
        by_source = {}
        cur.execute(
            "SELECT source, COUNT(*) FROM canonical_sessions GROUP BY source ORDER BY source"
        )
        for source, count in cur.fetchall():
            by_source[source] = count

    return {
        "dry_run": False,
        "preview": preview,
        "synced": {
            "garmin_rows_upserted": upserted,
            "canonical_sessions": canonical_count,
            "canonical_by_source": by_source,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the active Garmin snapshot into the canonical read layer."
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    result = sync_active_garmin(execute=args.execute)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if not args.execute:
        print("Dry run only. Re-run with --execute after reviewing the preview.")


if __name__ == "__main__":
    main()
