# EPOCH P0 - Plan Sessions to Timeline Bridge

Date: 2026-06-28  
Status: built locally, tested, dry-run tool ready.

## Purpose

Bridge the existing `training_plans` / `plan_sessions` tables into the Canonical Athlete Timeline as `planned_workout` events.

This lets the Plan / Intent Source Router compare:

```text
planned_workout timeline events
        vs
completed endurance_workout timeline events
```

without creating a second plan engine or abandoning the existing Garmin Coach plan tables.

## Canonical Files

- `plan_session_timeline_bridge.py`
- `tools/backfill_timeline_from_plan_sessions.py`
- `tests/test_plan_session_timeline_bridge.py`

## What It Does

- Converts one `plan_sessions` row into one deterministic timeline event.
- Uses event id: `evt_plan_<plan_session_id>`.
- Uses planned workout id: `plan_session_<plan_session_id>`.
- Preserves plan metadata:
  - plan id
  - plan name
  - week number
  - scheduled date
  - session type
  - phase
  - duration target
  - HR/power zone targets when present
  - source confidence
  - matched clean session id when present
- Keeps `plan_sessions` unchanged.
- Writes to `timeline_events` only when the tool runs with `--execute`.

## Tool Usage

Dry run:

```bash
python tools/backfill_timeline_from_plan_sessions.py
```

Write all plan sessions:

```bash
python tools/backfill_timeline_from_plan_sessions.py --execute
```

Write one plan only:

```bash
python tools/backfill_timeline_from_plan_sessions.py --plan-id garmin_tt_2026 --execute
```

## Verification

- `tests/test_plan_session_timeline_bridge.py`: 5 passed.
- Plan bridge + Plan/Intent + endpoint tests: 28 passed.
- Full backend suite: 309 passed, 0 failed.

## Important Boundaries

- No new plan table.
- No Strava rename work.
- No Garmin API work.
- No automatic plan rewrite.
- No deploy from this machine.

## Next Step

On the deploy machine:

1. Make sure `training_plans` and `plan_sessions` exist.
2. Seed/import the active Garmin Time Trial plan if needed.
3. Run:

```bash
python tools/backfill_timeline_from_plan_sessions.py --plan-id garmin_tt_2026 --execute
```

Then the existing Plan / Intent endpoints can resolve planned vs completed sessions from the timeline.
