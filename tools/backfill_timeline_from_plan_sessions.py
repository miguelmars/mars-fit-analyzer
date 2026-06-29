"""
tools/backfill_timeline_from_plan_sessions.py
=============================================
One-time / idempotent backfill: map existing rows in `plan_sessions` into the
Canonical Athlete Timeline as `planned_workout` events.

Why: the Plan / Intent Source Router reads planned workouts from the timeline. This
tool bridges the existing Garmin Coach plan tables into that new source of truth
without deleting or changing `plan_sessions`.

Usage (on the deploy machine, with DATABASE_URL set):
    python tools/backfill_timeline_from_plan_sessions.py            # dry-run
    python tools/backfill_timeline_from_plan_sessions.py --execute  # write events
    python tools/backfill_timeline_from_plan_sessions.py --plan-id garmin_tt_2026 --execute
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_db
from plan_session_timeline_bridge import row_to_planned_workout_event
from timeline_store import PostgresTimelineRepository


_COLS = [
    "ps.id", "ps.plan_id", "ps.week_number", "ps.planned_date", "ps.session_type",
    "ps.description", "ps.target", "ps.matched_clean_session_id", "ps.status",
    "ps.moved_from", "ps.move_reason", "tp.name AS plan_name", "tp.source AS plan_source",
]


def _load_rows(conn, plan_id: str, limit: int):
    from psycopg2.extras import RealDictCursor
    sql = (
        f"SELECT {', '.join(_COLS)} "
        "FROM plan_sessions ps "
        "JOIN training_plans tp ON tp.plan_id = ps.plan_id "
        "WHERE (%s = '' OR ps.plan_id = %s) "
        "ORDER BY ps.planned_date, ps.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (plan_id or "", plan_id or ""))
        return [dict(r) for r in cur.fetchall()]


def run(athlete_id: str, plan_id: str, execute: bool, limit: int) -> int:
    conn = get_db()
    if not conn:
        print("ERROR: DATABASE_URL not set / DB unavailable.", file=sys.stderr)
        return 2

    rows = _load_rows(conn, plan_id, limit)
    print(f"plan_sessions rows read: {len(rows)}")
    if not rows:
        print("No plan_sessions rows found. Seed/import the plan first.")
        return 0

    sample = row_to_planned_workout_event(rows[0], athlete_id)
    print("Example mapped planned_workout:")
    print(
        f"  event_id={sample.event_id} title={sample.normalized_summary.get('title')} "
        f"date={sample.payload.get('scheduled_date_local')} "
        f"confidence={sample.confidence.level.value}"
    )

    if not execute:
        print(f"DRY RUN — would write {len(rows)} planned_workout events. Re-run with --execute to apply.")
        return 0

    repo = PostgresTimelineRepository(conn, ensure=True)
    written = 0
    for row in rows:
        try:
            repo.save_event(row_to_planned_workout_event(row, athlete_id))
            written += 1
        except Exception as e:
            print(f"  skip plan_session {row.get('id')}: {e}", file=sys.stderr)
    print(f"DONE — wrote/updated {written} planned_workout timeline events for athlete '{athlete_id}'.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill plan_sessions into the Canonical Athlete Timeline.")
    ap.add_argument("--athlete-id", default="default")
    ap.add_argument("--plan-id", default="", help="Optional plan_id filter. Empty = all plans.")
    ap.add_argument("--execute", action="store_true", help="Write events (default is dry-run).")
    ap.add_argument("--limit", type=int, default=0, help="Limit rows (0 = all).")
    args = ap.parse_args()
    return run(args.athlete_id, args.plan_id, args.execute, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
