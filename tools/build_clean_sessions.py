#!/usr/bin/env python3
"""Build a non-destructive clean session layer.

The current `sessions` table is intentionally left untouched. This script
rebuilds `clean_sessions` from:

1. Garmin export staging activities as the master historical index.
2. Valid current sessions that happened after the Garmin export max date.

That keeps recent uploads while removing monitoring/stress/internal FIT noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import _ensure_clean_sessions_table, _ensure_garmin_staging_tables, get_db


def _fetch_one(cur, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def build_clean_sessions(execute: bool = False, include_unmatched_legacy: bool = False) -> dict[str, Any]:
    conn = get_db()
    if not conn:
        raise RuntimeError("No DATABASE_URL available or DB connection failed")

    with conn.cursor() as cur:
        _ensure_garmin_staging_tables(conn)
        _ensure_clean_sessions_table(conn)

        current_sessions = _fetch_one(cur, "SELECT COUNT(*) FROM sessions")
        staging_activities = _fetch_one(cur, "SELECT COUNT(*) FROM garmin_export_activities")
        staging_real = _fetch_one(
            cur,
            "SELECT COUNT(*) FROM garmin_export_activities WHERE is_probable_real_activity IS TRUE",
        )
        junk_current = _fetch_one(
            cur,
            """
            SELECT COUNT(*)
            FROM sessions
            WHERE COALESCE(start_time, '') = ''
              AND COALESCE(sport, '') = ''
              AND COALESCE(distance_km, 0) = 0
              AND COALESCE(duration_s, 0) = 0
            """,
        )
        valid_current = _fetch_one(
            cur,
            """
            SELECT COUNT(*)
            FROM sessions
            WHERE COALESCE(start_time, '') <> ''
              AND COALESCE(sport, '') <> ''
              AND COALESCE(duration_s, 0) >= 60
            """,
        )
        garmin_max_date = _fetch_one(
            cur,
            "SELECT MAX(start_time_local)::date FROM garmin_export_activities",
        )
        recent_valid_current = _fetch_one(
            cur,
            """
            SELECT COUNT(*)
            FROM sessions
            WHERE COALESCE(start_time, '') <> ''
              AND COALESCE(sport, '') <> ''
              AND COALESCE(duration_s, 0) >= 60
              AND start_time::timestamp::date > %s
            """,
            (garmin_max_date,),
        ) if garmin_max_date else 0

        preview = {
            "current_sessions": current_sessions,
            "current_junk_zero_empty": junk_current,
            "current_valid_sessions": valid_current,
            "garmin_staging_activities": staging_activities,
            "garmin_staging_real": staging_real,
            "garmin_export_max_date": str(garmin_max_date) if garmin_max_date else None,
            "recent_valid_current_after_export": recent_valid_current,
            "include_unmatched_legacy": include_unmatched_legacy,
        }

        if not execute:
            return {"dry_run": True, "preview": preview, "built": None}

        cur.execute("TRUNCATE TABLE clean_sessions")

        cur.execute("""
            INSERT INTO clean_sessions (
                clean_session_id, source, source_activity_id, name, sport, sport_type,
                start_time, start_date, duration_s, moving_duration_s, elapsed_duration_s,
                distance_km, ascent_m, descent_m, avg_speed_kmh, max_speed_kmh,
                avg_hr_bpm, max_hr_bpm, avg_cadence, max_cadence, calories,
                start_lat, start_lon, end_lat, end_lon, power_available,
                efficiency_speed_hr, quality, quality_notes, raw_json
            )
            SELECT
                'garmin:' || source_activity_id,
                'garmin_export',
                source_activity_id,
                name,
                sport,
                sport_type,
                start_time_local,
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
                power_available,
                efficiency_speed_hr,
                CASE WHEN is_probable_real_activity THEN 'clean' ELSE 'low_confidence' END,
                CASE WHEN is_probable_real_activity THEN 'Garmin summarized activity'
                     ELSE 'Garmin summarized row below activity threshold' END,
                raw_json
            FROM garmin_export_activities
        """)
        garmin_inserted = cur.rowcount

        cur.execute("""
            WITH ranked AS (
                SELECT
                    s.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            LEFT(COALESCE(s.start_time, ''), 10),
                            COALESCE(s.sport, ''),
                            ROUND(COALESCE(s.distance_km, 0)::numeric, 2),
                            COALESCE(s.duration_s, 0)
                        ORDER BY COALESCE(s.uploaded_at, NOW()) DESC, s.session_id DESC
                    ) AS rn
                FROM sessions s
                WHERE COALESCE(s.start_time, '') <> ''
                  AND COALESCE(s.sport, '') <> ''
                  AND COALESCE(s.duration_s, 0) >= 60
                  AND s.start_time::timestamp::date > %s
            )
            INSERT INTO clean_sessions (
                clean_session_id, source, original_session_id, name, sport,
                start_time, start_date, duration_s, distance_km, ascent_m,
                avg_speed_kmh, avg_hr_bpm, avg_cadence, start_lat, start_lon,
                end_lat, end_lon, route_id, power_available, efficiency_speed_hr,
                quality, quality_notes, raw_json
            )
            SELECT
                'session:' || session_id,
                'current_sessions_recent',
                session_id,
                workout_name,
                sport,
                start_time::timestamp,
                start_time::timestamp::date,
                duration_s,
                distance_km,
                ascent_m,
                avg_speed_kmh,
                avg_hr_bpm,
                avg_cadence,
                start_lat,
                start_lon,
                end_lat,
                end_lon,
                route_id,
                FALSE,
                CASE WHEN avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0
                     THEN ROUND((avg_speed_kmh / avg_hr_bpm)::numeric, 5)
                     ELSE NULL END,
                'clean_recent',
                'Valid current session after Garmin export date',
                result_json::jsonb
            FROM ranked
            WHERE rn = 1
            ON CONFLICT (clean_session_id) DO NOTHING
        """, (garmin_max_date,))
        recent_inserted = cur.rowcount

        legacy_inserted = 0
        if include_unmatched_legacy:
            cur.execute("""
                WITH ranked AS (
                    SELECT
                        s.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                LEFT(COALESCE(s.start_time, ''), 10),
                                COALESCE(s.sport, ''),
                                ROUND(COALESCE(s.distance_km, 0)::numeric, 2),
                                COALESCE(s.duration_s, 0)
                            ORDER BY COALESCE(s.uploaded_at, NOW()) DESC, s.session_id DESC
                        ) AS rn
                    FROM sessions s
                    WHERE COALESCE(s.start_time, '') <> ''
                      AND COALESCE(s.sport, '') <> ''
                      AND COALESCE(s.duration_s, 0) >= 60
                      AND s.start_time::timestamp::date <= %s
                )
                INSERT INTO clean_sessions (
                    clean_session_id, source, original_session_id, name, sport,
                    start_time, start_date, duration_s, distance_km, ascent_m,
                    avg_speed_kmh, avg_hr_bpm, avg_cadence, start_lat, start_lon,
                    end_lat, end_lon, route_id, power_available, efficiency_speed_hr,
                    quality, quality_notes, raw_json
                )
                SELECT
                    'legacy:' || session_id,
                    'current_sessions_legacy_unmatched',
                    session_id,
                    workout_name,
                    sport,
                    start_time::timestamp,
                    start_time::timestamp::date,
                    duration_s,
                    distance_km,
                    ascent_m,
                    avg_speed_kmh,
                    avg_hr_bpm,
                    avg_cadence,
                    start_lat,
                    start_lon,
                    end_lat,
                    end_lon,
                    route_id,
                    FALSE,
                    CASE WHEN avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0
                         THEN ROUND((avg_speed_kmh / avg_hr_bpm)::numeric, 5)
                         ELSE NULL END,
                    'legacy_unmatched',
                    'Valid legacy session not fuzzy-matched to Garmin export',
                    result_json::jsonb
                FROM ranked s
                WHERE rn = 1
                  AND NOT EXISTS (
                    SELECT 1
                    FROM garmin_export_activities g
                    WHERE g.sport = s.sport
                      AND ABS(COALESCE(s.distance_km, 0) - COALESCE(g.distance_km::float, 0)) < 0.05
                      AND ABS(COALESCE(s.duration_s, 0) - COALESCE(g.duration_s, 0)) <= 5
                      AND LEFT(COALESCE(s.start_time, ''), 10) = LEFT(g.start_time_local::text, 10)
                  )
                ON CONFLICT (clean_session_id) DO NOTHING
            """, (garmin_max_date,))
            legacy_inserted = cur.rowcount

        cur.execute("""
            SELECT source, quality, COUNT(*)
            FROM clean_sessions
            GROUP BY source, quality
            ORDER BY source, quality
        """)
        by_source_quality = [
            {"source": row[0], "quality": row[1], "count": row[2]}
            for row in cur.fetchall()
        ]
        cur.execute("SELECT COUNT(*) FROM clean_sessions")
        total_clean = cur.fetchone()[0]

    conn.commit()
    return {
        "dry_run": False,
        "preview": preview,
        "built": {
            "garmin_inserted": garmin_inserted,
            "recent_current_inserted": recent_inserted,
            "legacy_unmatched_inserted": legacy_inserted,
            "total_clean_sessions": total_clean,
            "by_source_quality": by_source_quality,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build clean_sessions from Garmin staging and valid recent sessions.")
    parser.add_argument("--execute", action="store_true", help="Write clean_sessions table")
    parser.add_argument(
        "--include-unmatched-legacy",
        action="store_true",
        help="Also include valid old sessions that do not fuzzy-match Garmin export",
    )
    parser.add_argument("--out", default="", help="Optional JSON output path")
    args = parser.parse_args()

    result = build_clean_sessions(
        execute=args.execute,
        include_unmatched_legacy=args.include_unmatched_legacy,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if not args.execute:
        print("Dry run only. Re-run with --execute to rebuild clean_sessions.")


if __name__ == "__main__":
    main()
