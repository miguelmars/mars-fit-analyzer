#!/usr/bin/env python3
"""Build one athlete snapshot per calendar week from clean_sessions.

Dry-run is the default. Use --execute only after reviewing the summary.
The script is idempotent: week_start is unique and rows are upserted.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import _ensure_athlete_snapshots_table, get_db


SNAPSHOT_VERSION = "phase2_weekly_v1"
CYCLING_SPORTS = {"cycling", "indoor_cycling"}
RUNNING_SPORTS = {"running", "trail_running", "treadmill_running"}
AEROBIC_SPORTS = CYCLING_SPORTS | RUNNING_SPORTS | {"walking"}


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _week_start(value: dt.date) -> dt.date:
    return value - dt.timedelta(days=value.weekday())


def _weighted_average(pairs: list[tuple[float, float]]) -> float | None:
    valid = [(value, weight) for value, weight in pairs if weight > 0]
    if not valid:
        return None
    total_weight = sum(weight for _, weight in valid)
    return sum(value * weight for value, weight in valid) / total_weight


def _efficiency(rows: list[dict[str, Any]]) -> float | None:
    pairs = []
    for row in rows:
        speed = _as_float(row.get("avg_speed_kmh"))
        hr = _as_float(row.get("avg_hr_bpm"))
        duration = _as_float(row.get("duration_s")) or 0
        if speed and hr and hr > 0 and duration > 0:
            pairs.append((speed / hr, duration))
    return _weighted_average(pairs)


def _temperature(row: dict[str, Any]) -> float | None:
    raw = row.get("raw_json") or {}
    if not isinstance(raw, dict):
        return None
    values = [
        _as_float(raw.get("minTemperature")),
        _as_float(raw.get("maxTemperature")),
        _as_float(raw.get("avgTemperature")),
    ]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _load_garmin_weights(zip_path: Path | None) -> dict[dt.date, float]:
    if not zip_path:
        return {}
    target = "DI_CONNECT/DI-Connect-Wellness/67097603_userBioMetrics.json"
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        name = target if target in names else next(
            (item for item in names if item.endswith("_userBioMetrics.json")),
            None,
        )
        if not name:
            return {}
        rows = json.loads(archive.read(name).decode("utf-8-sig"))

    latest: dict[dt.date, tuple[int, float]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metaData") or {}
        weight = row.get("weight") or {}
        date_text = str(metadata.get("calendarDate") or "")[:10]
        grams = _as_float(weight.get("weight"))
        if not date_text or grams is None:
            continue
        try:
            date_value = dt.date.fromisoformat(date_text)
        except ValueError:
            continue
        kg = grams / 1000
        if not 40 <= kg <= 200:
            continue
        sequence = int(metadata.get("sequence") or row.get("version") or 0)
        previous = latest.get(date_value)
        if previous is None or sequence >= previous[0]:
            latest[date_value] = (sequence, kg)
    return {date_value: value for date_value, (_, value) in latest.items()}


def _load_manual_weights(conn) -> dict[dt.date, float]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT date, weight_kg
            FROM weight_log
            WHERE weight_kg IS NOT NULL
            ORDER BY date, created_at
        """)
        return {row[0]: float(row[1]) for row in cur.fetchall()}


def _weekly_weight(
    week: dt.date,
    manual: dict[dt.date, float],
    garmin: dict[dt.date, float],
) -> tuple[float | None, str | None]:
    week_end = week + dt.timedelta(days=6)
    manual_rows = [(date, kg) for date, kg in manual.items() if week <= date <= week_end]
    if manual_rows:
        return sorted(manual_rows)[-1][1], "weight_log"
    garmin_rows = [(date, kg) for date, kg in garmin.items() if week <= date <= week_end]
    if garmin_rows:
        return sorted(garmin_rows)[-1][1], "garmin_biometrics"
    return None, None


@dataclass
class WeekBucket:
    week_start: dt.date
    rows: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(
        self,
        manual_weights: dict[dt.date, float],
        garmin_weights: dict[dt.date, float],
    ) -> dict[str, Any]:
        rows = self.rows
        durations = [(_as_float(row.get("duration_s")) or 0) for row in rows]
        total_duration = sum(durations)
        distance = sum((_as_float(row.get("distance_km")) or 0) for row in rows)
        calories = sum(int(_as_float(row.get("calories")) or 0) for row in rows)
        ascent = sum(int(_as_float(row.get("ascent_m")) or 0) for row in rows)
        active_days = len({row["start_date"] for row in rows})

        hr_pairs = []
        speed_pairs = []
        cadence_pairs = []
        temperature_pairs = []
        for row, duration in zip(rows, durations):
            hr = _as_float(row.get("avg_hr_bpm"))
            speed = _as_float(row.get("avg_speed_kmh"))
            cadence = _as_float(row.get("avg_cadence"))
            temperature = _temperature(row)
            if hr and hr > 0 and duration > 0:
                hr_pairs.append((hr, duration))
            if speed and speed > 0 and duration > 0 and row.get("sport") in AEROBIC_SPORTS:
                speed_pairs.append((speed, duration))
            if cadence and cadence > 0 and duration > 0:
                cadence_pairs.append((cadence, duration))
            if temperature is not None and duration > 0:
                temperature_pairs.append((temperature, duration))

        cycling_rows = [row for row in rows if row.get("sport") in CYCLING_SPORTS]
        running_rows = [row for row in rows if row.get("sport") in RUNNING_SPORTS]
        cycling_efficiency = _efficiency(cycling_rows)
        running_efficiency = _efficiency(running_rows)
        cycling_hours = sum(
            (_as_float(row.get("duration_s")) or 0) for row in cycling_rows
        )
        running_hours = sum(
            (_as_float(row.get("duration_s")) or 0) for row in running_rows
        )
        if cycling_hours >= running_hours and cycling_efficiency is not None:
            efficiency = cycling_efficiency
            efficiency_sport = "cycling"
        elif running_efficiency is not None:
            efficiency = running_efficiency
            efficiency_sport = "running"
        else:
            efficiency = None
            efficiency_sport = None

        cycling_hr_duration = sum(
            (_as_float(row.get("duration_s")) or 0)
            for row in cycling_rows
            if (_as_float(row.get("avg_hr_bpm")) or 0) > 0
        )
        cycling_z2_duration = sum(
            (_as_float(row.get("duration_s")) or 0)
            for row in cycling_rows
            if 134 <= (_as_float(row.get("avg_hr_bpm")) or 0) <= 150
        )
        pct_z2 = (
            cycling_z2_duration / cycling_hr_duration * 100
            if cycling_hr_duration > 0
            else None
        )

        breakdown: dict[str, dict[str, float | int]] = {}
        grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in rows:
            grouped[str(row.get("sport") or "unknown")].append(row)
        for sport, sport_rows in sorted(grouped.items()):
            breakdown[sport] = {
                "sessions": len(sport_rows),
                "km": round(sum((_as_float(r.get("distance_km")) or 0) for r in sport_rows), 2),
                "hours": round(sum((_as_float(r.get("duration_s")) or 0) for r in sport_rows) / 3600, 2),
            }

        aerobic_rows = [row for row in rows if row.get("sport") in AEROBIC_SPORTS]
        aerobic_duration = sum((_as_float(row.get("duration_s")) or 0) for row in aerobic_rows)
        weight, weight_source = _weekly_weight(
            self.week_start,
            manual_weights,
            garmin_weights,
        )
        # Compatibility workload proxy. It is not the future Mars Index.
        fitness_score = total_duration / 3600 * 10 + active_days * 2

        return {
            "week_start": self.week_start,
            "week_end": self.week_start + dt.timedelta(days=6),
            "weight_kg": round(weight, 2) if weight is not None else None,
            "weight_source": weight_source,
            "km_week": round(distance, 2),
            "hours_week": round(total_duration / 3600, 2),
            "sessions": len(rows),
            "active_days": active_days,
            "calories_week": calories,
            "ascent_m_week": ascent,
            "avg_hr": round(_weighted_average(hr_pairs), 1) if hr_pairs else None,
            "avg_speed": round(_weighted_average(speed_pairs), 2) if speed_pairs else None,
            "avg_cadence": round(_weighted_average(cadence_pairs), 1) if cadence_pairs else None,
            "pct_z2": round(pct_z2, 2) if pct_z2 is not None else None,
            "z2_estimated": True,
            "efficiency": round(efficiency, 5) if efficiency is not None else None,
            "efficiency_sport": efficiency_sport,
            "cycling_efficiency": round(cycling_efficiency, 5) if cycling_efficiency is not None else None,
            "running_efficiency": round(running_efficiency, 5) if running_efficiency is not None else None,
            "aerobic_sessions": len(aerobic_rows),
            "aerobic_hours": round(aerobic_duration / 3600, 2),
            "avg_temperature_c": (
                round(_weighted_average(temperature_pairs), 2) if temperature_pairs else None
            ),
            "fitness_score": round(fitness_score, 2),
            "sport_breakdown": breakdown,
            "snapshot_version": SNAPSHOT_VERSION,
        }


def _load_sessions(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                start_date,
                sport,
                duration_s,
                distance_km,
                ascent_m,
                avg_hr_bpm,
                avg_speed_kmh,
                avg_cadence,
                calories,
                raw_json
            FROM clean_sessions
            WHERE start_date IS NOT NULL
            ORDER BY start_date, start_time
        """)
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _load_staging_sessions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sessions = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        date_text = str(row.get("start_time_local") or row.get("start_time_utc") or "")[:10]
        if not date_text:
            continue
        try:
            start_date = dt.date.fromisoformat(date_text)
        except ValueError:
            continue
        sessions.append({
            "start_date": start_date,
            "sport": row.get("sport"),
            "duration_s": row.get("duration_s"),
            "distance_km": row.get("distance_km"),
            "ascent_m": row.get("ascent_m"),
            "avg_hr_bpm": row.get("avg_hr_bpm"),
            "avg_speed_kmh": row.get("avg_speed_kmh"),
            "avg_cadence": row.get("avg_cadence"),
            "calories": row.get("calories"),
            "raw_json": row.get("raw") or {},
        })
    sessions.sort(key=lambda row: row["start_date"])
    return sessions


def build_snapshots_from_sessions(
    sessions: list[dict[str, Any]],
    manual_weights: dict[dt.date, float] | None = None,
    garmin_weights: dict[dt.date, float] | None = None,
) -> list[dict[str, Any]]:
    if not sessions:
        return []
    manual_weights = manual_weights or {}
    garmin_weights = garmin_weights or {}

    first_week = _week_start(sessions[0]["start_date"])
    last_week = _week_start(sessions[-1]["start_date"])
    buckets: dict[dt.date, WeekBucket] = {}
    cursor = first_week
    while cursor <= last_week:
        buckets[cursor] = WeekBucket(cursor)
        cursor += dt.timedelta(days=7)
    for row in sessions:
        buckets[_week_start(row["start_date"])].rows.append(row)
    return [
        buckets[week].snapshot(manual_weights, garmin_weights)
        for week in sorted(buckets)
    ]


def build_snapshots(conn, garmin_zip: Path | None = None) -> list[dict[str, Any]]:
    return build_snapshots_from_sessions(
        _load_sessions(conn),
        _load_manual_weights(conn),
        _load_garmin_weights(garmin_zip),
    )


def write_snapshots(conn, snapshots: list[dict[str, Any]]) -> int:
    _ensure_athlete_snapshots_table(conn)
    columns = [
        "week_start", "week_end", "weight_kg", "weight_source", "km_week",
        "hours_week", "sessions", "active_days", "calories_week", "ascent_m_week",
        "avg_hr", "avg_speed", "avg_cadence", "pct_z2", "z2_estimated",
        "efficiency", "efficiency_sport", "cycling_efficiency", "running_efficiency",
        "aerobic_sessions", "aerobic_hours", "avg_temperature_c", "fitness_score",
        "sport_breakdown", "snapshot_version",
    ]
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(
        f"{column}=EXCLUDED.{column}" for column in columns if column != "week_start"
    )
    sql = f"""
        INSERT INTO athlete_snapshots ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT (week_start) DO UPDATE SET
            {updates},
            updated_at=NOW()
    """
    with conn.cursor() as cur:
        for snapshot in snapshots:
            values = [
                Json(snapshot[column]) if column == "sport_breakdown" else snapshot[column]
                for column in columns
            ]
            cur.execute(sql, values)
    conn.commit()
    return len(snapshots)


def _summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        return {"weeks": 0}
    active = [row for row in snapshots if row["sessions"] > 0]
    return {
        "weeks": len(snapshots),
        "active_weeks": len(active),
        "empty_weeks": len(snapshots) - len(active),
        "week_start": snapshots[0]["week_start"].isoformat(),
        "week_end": snapshots[-1]["week_end"].isoformat(),
        "sessions": sum(row["sessions"] for row in snapshots),
        "km": round(sum(row["km_week"] for row in snapshots), 1),
        "hours": round(sum(row["hours_week"] for row in snapshots), 1),
        "calories": sum(row["calories_week"] for row in snapshots),
        "weeks_with_weight": sum(1 for row in snapshots if row["weight_kg"] is not None),
        "weeks_with_cycling_efficiency": sum(
            1 for row in snapshots if row["cycling_efficiency"] is not None
        ),
        "weeks_with_running_efficiency": sum(
            1 for row in snapshots if row["running_efficiency"] is not None
        ),
        "z2_note": (
            "Estimated from duration of cycling sessions whose session-average HR "
            "was 134-150 bpm; not exact time-in-zone."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill weekly athlete snapshots.")
    parser.add_argument(
        "--garmin-zip",
        type=Path,
        help="Optional original Garmin ZIP used only for historical weight measurements.",
    )
    parser.add_argument(
        "--activities-json",
        type=Path,
        help="Optional normalized Garmin activities JSON for an offline dry run.",
    )
    parser.add_argument("--execute", action="store_true", help="Write/upsert snapshots.")
    parser.add_argument("--out", type=Path, help="Optional private JSON preview path.")
    args = parser.parse_args()

    conn = None
    if args.activities_json:
        if args.execute:
            raise RuntimeError("--activities-json is preview-only; remove it when using --execute")
        snapshots = build_snapshots_from_sessions(
            _load_staging_sessions(args.activities_json),
            garmin_weights=_load_garmin_weights(args.garmin_zip),
        )
    else:
        conn = get_db()
        if not conn:
            raise RuntimeError("No DATABASE_URL available or DB connection failed")
        snapshots = build_snapshots(conn, args.garmin_zip)
    summary = _summary(snapshots)
    result = {"dry_run": not args.execute, "summary": summary}

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        serializable = [
            {
                key: value.isoformat() if isinstance(value, dt.date) else value
                for key, value in row.items()
            }
            for row in snapshots
        ]
        args.out.write_text(
            json.dumps({"summary": summary, "snapshots": serializable}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["preview"] = str(args.out)

    if args.execute:
        if conn is None:
            raise RuntimeError("A database connection is required for --execute")
        result["upserted"] = write_snapshots(conn, snapshots)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
