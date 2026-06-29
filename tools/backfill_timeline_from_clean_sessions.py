"""
tools/backfill_timeline_from_clean_sessions.py
=============================================
One-time / idempotent backfill: map existing endurance rows in `clean_sessions` into
the Canonical Athlete Timeline as `endurance_workout` events.

Why: so the athlete's existing history lives in the timeline too (not just new uploads),
WITHOUT re-parsing raw files and WITHOUT touching `clean_sessions` (read-only here).

Idempotent: each event id is derived deterministically from `clean_session_id`
(`evt_cs_<id>`), so re-running upserts the same rows — no duplicates.

Usage (on the deploy machine, with DATABASE_URL set):
    python tools/backfill_timeline_from_clean_sessions.py            # dry-run (default)
    python tools/backfill_timeline_from_clean_sessions.py --execute  # write events
    python tools/backfill_timeline_from_clean_sessions.py --execute --limit 50
    python tools/backfill_timeline_from_clean_sessions.py --athlete-id mars --execute
"""

from __future__ import annotations

import argparse
import sys
from datetime import timezone

from db import get_db
from ingest_parsers import ParsedActivity
from ingest_pipeline import normalize
from timeline_model import Source
from timeline_store import PostgresTimelineRepository

_SOURCE_MAP = {
    "garmin_api": Source.GARMIN_EXPORT,
    "garmin": Source.GARMIN_EXPORT,
    "garmin_export": Source.GARMIN_EXPORT,
    "strava": Source.STRAVA_EXPORT,
    "strava_api": Source.STRAVA_EXPORT,
    "file": Source.FILE_UPLOAD,
    "manual": Source.MANUAL_UPLOAD,
}

_COLS = [
    "clean_session_id", "source", "source_activity_id", "name", "sport", "start_time",
    "duration_s", "moving_duration_s", "elapsed_duration_s", "distance_km",
    "ascent_m", "descent_m", "avg_speed_kmh", "max_speed_kmh", "avg_hr_bpm",
    "max_hr_bpm", "avg_cadence", "max_cadence", "calories", "power_available",
    "avg_power_w", "max_power_w", "normalized_power_w", "start_lat", "start_lon", "route_id",
]


def _f(v):
    return float(v) if v is not None else None


def _row_to_event(row: dict, athlete_id: str):
    src = _SOURCE_MAP.get((row.get("source") or "").lower(), Source.UNKNOWN)
    st = row.get("start_time")
    if st is not None and getattr(st, "tzinfo", None) is None:
        st = st.replace(tzinfo=timezone.utc)

    pa = ParsedActivity(
        file_type=None,
        parser="backfill_clean_sessions",
        start_time=st,
        sport_hint=row.get("sport"),
        original_name=row.get("name"),
        source_hint=src,
        distance_m=(_f(row.get("distance_km")) * 1000.0) if row.get("distance_km") is not None else None,
        duration_s=row.get("duration_s"),
        moving_time_s=row.get("moving_duration_s"),
        elapsed_time_s=row.get("elapsed_duration_s"),
        elevation_gain_m=_f(row.get("ascent_m")),
        elevation_loss_m=_f(row.get("descent_m")),
        avg_hr=_f(row.get("avg_hr_bpm")),
        max_hr=_f(row.get("max_hr_bpm")),
        avg_cadence=_f(row.get("avg_cadence")),
        max_cadence=_f(row.get("max_cadence")),
        avg_power=_f(row.get("avg_power_w")),
        max_power=_f(row.get("max_power_w")),
        normalized_power=_f(row.get("normalized_power_w")),
        avg_speed_mps=(_f(row.get("avg_speed_kmh")) / 3.6) if row.get("avg_speed_kmh") is not None else None,
        max_speed_mps=(_f(row.get("max_speed_kmh")) / 3.6) if row.get("max_speed_kmh") is not None else None,
        calories=_f(row.get("calories")),
    )
    pa.has_hr = row.get("avg_hr_bpm") is not None
    pa.has_power = bool(row.get("power_available")) or row.get("avg_power_w") is not None
    pa.has_gps = row.get("start_lat") is not None
    pa.has_elevation = row.get("ascent_m") is not None
    pa.has_cadence = row.get("avg_cadence") is not None
    pa.has_distance = row.get("distance_km") is not None

    ev = normalize(
        pa, athlete_id,
        declared_source=src,
        upload_method="backfill",
        file_hash=None,
        raw_ref=f"clean_sessions:{row.get('clean_session_id')}",
        original_filename=None,
    )
    # Deterministic id → idempotent upsert.
    ev.event_id = f"evt_cs_{row.get('clean_session_id')}"
    ev.source.source_event_id = row.get("source_activity_id")
    return ev


def run(athlete_id: str, execute: bool, limit: int) -> int:
    conn = get_db()
    if not conn:
        print("ERROR: DATABASE_URL not set / DB unavailable.", file=sys.stderr)
        return 2

    from psycopg2.extras import RealDictCursor
    sql = f"SELECT {', '.join(_COLS)} FROM clean_sessions ORDER BY start_time"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]

    print(f"clean_sessions rows read: {len(rows)}")
    if not execute:
        sample = rows[0] if rows else None
        if sample:
            ev = _row_to_event(sample, athlete_id)
            print("DRY RUN — example mapped event:")
            print(f"  event_id={ev.event_id} sport={ev.sport_category} "
                  f"source={ev.source.source.value} conf={ev.confidence.level.value} "
                  f"dist_km={ev.normalized_summary.get('distance_km')}")
        print(f"DRY RUN — would write {len(rows)} endurance_workout events. "
              f"Re-run with --execute to apply.")
        return 0

    repo = PostgresTimelineRepository(conn, ensure=True)
    written = 0
    for row in rows:
        try:
            repo.save_event(_row_to_event(row, athlete_id))
            written += 1
        except Exception as e:  # never abort the whole backfill on one bad row
            print(f"  skip clean_session {row.get('clean_session_id')}: {e}", file=sys.stderr)
    print(f"DONE — wrote/updated {written} timeline events for athlete '{athlete_id}'.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill clean_sessions into the Canonical Athlete Timeline.")
    ap.add_argument("--athlete-id", default="default")
    ap.add_argument("--execute", action="store_true", help="Write events (default is dry-run).")
    ap.add_argument("--limit", type=int, default=0, help="Limit rows (0 = all).")
    args = ap.parse_args()
    return run(args.athlete_id, args.execute, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
