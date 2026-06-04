#!/usr/bin/env python3
"""Compare Garmin staging tables against current production sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_db


def _fetch_one(cur, sql: str, params: tuple[Any, ...] = ()) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def compare() -> dict[str, Any]:
    conn = get_db()
    if not conn:
        raise RuntimeError("No DATABASE_URL available or DB connection failed")

    with conn.cursor() as cur:
        counts = {
            "sessions_current": _fetch_one(cur, "SELECT COUNT(*) FROM sessions"),
            "garmin_staging_activities": _fetch_one(cur, "SELECT COUNT(*) FROM garmin_export_activities"),
            "garmin_staging_real_candidates": _fetch_one(
                cur,
                "SELECT COUNT(*) FROM garmin_export_activities WHERE is_probable_real_activity IS TRUE",
            ),
            "garmin_staging_gear": _fetch_one(cur, "SELECT COUNT(*) FROM garmin_export_gear"),
            "garmin_staging_sleep": _fetch_one(cur, "SELECT COUNT(*) FROM garmin_export_sleep"),
        }

        cur.execute("""
            SELECT sport, COUNT(*)
            FROM sessions
            GROUP BY sport
            ORDER BY COUNT(*) DESC
        """)
        current_by_sport = dict(cur.fetchall())

        cur.execute("""
            SELECT sport, COUNT(*)
            FROM garmin_export_activities
            GROUP BY sport
            ORDER BY COUNT(*) DESC
        """)
        staging_by_sport = dict(cur.fetchall())

        cur.execute("""
            SELECT COUNT(*)
            FROM garmin_export_activities g
            JOIN sessions s
              ON s.sport = g.sport
             AND ABS(COALESCE(s.distance_km, 0) - COALESCE(g.distance_km::float, 0)) < 0.05
             AND ABS(COALESCE(s.duration_s, 0) - COALESCE(g.duration_s, 0)) <= 5
             AND LEFT(COALESCE(s.start_time, ''), 10) = LEFT(g.start_time_local::text, 10)
        """)
        fuzzy_matches = cur.fetchone()[0]

        cur.execute("""
            SELECT LEFT(COALESCE(start_time, ''), 10) AS date,
                   sport,
                   ROUND(COALESCE(distance_km, 0)::numeric, 2) AS distance_km,
                   COALESCE(duration_s, 0) AS duration_s,
                   COUNT(*) AS duplicates
            FROM sessions
            GROUP BY 1, 2, 3, 4
            HAVING COUNT(*) > 1
            ORDER BY duplicates DESC
            LIMIT 20
        """)
        duplicate_groups = [
            {
                "date": row[0],
                "sport": row[1],
                "distance_km": float(row[2]) if row[2] is not None else None,
                "duration_s": row[3],
                "duplicates": row[4],
            }
            for row in cur.fetchall()
        ]

    return {
        "counts": counts,
        "current_by_sport": current_by_sport,
        "staging_by_sport": staging_by_sport,
        "fuzzy_matches_staging_to_current": fuzzy_matches,
        "top_duplicate_groups_current_sessions": duplicate_groups,
        "verdict": (
            "Use Garmin staging as clean activity index, then migrate/merge after duplicate review."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Garmin staging tables to current sessions.")
    parser.add_argument("--out", default="", help="Optional JSON output path")
    args = parser.parse_args()

    result = compare()
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
