# EPOCH P0 - Plan / Intent Source Router Implementation

Date: 2026-06-28  
Status: built locally as a pure engine; read endpoints wired.

## Purpose

Resolve the question:

> What was this athlete supposed to do, and which completed workout matched it?

The product rule is:

> Garmin/imported/structured plan tells EPOCH what the athlete was supposed to do. Strava tells EPOCH what got uploaded. EPOCH reconciles both and declares confidence.

## Canonical File

- `plan_intent_router.py`

## Tests

- `tests/test_plan_intent_router.py`

Current verification:

- `tests/test_plan_intent_router.py`: 12 passed.
- Plan/intent endpoint coverage in `tests/test_intelligence_endpoints.py`.
- Route registration coverage in `tests/test_routes_registered.py`.
- Full backend suite: 304 passed, 0 failed.

## What It Does

- Matches a completed timeline event to a planned workout.
- Matches a planned workout to a completed event.
- Supports same-day, moved-day, partial, missed, skipped, rescheduled, extra-unplanned, and needs-review states.
- Uses source hierarchy: coach/imported/Garmin structured plan beats Strava display names.
- Treats generic Strava titles like `Morning Ride` as weak display noise, not training truth.
- Allows manual correction to override inference.
- Returns evidence, flags, source rank, targets, canonical title, activity display title, confidence, and next action.

## Endpoints

- `GET /timeline/{event_id}/plan-intent`
- `GET /planned-workouts/{planned_workout_id}/match`

The endpoints read planned workouts from `planned_workout` timeline events and adapt
them into the pure engine DTO. No second plan table or second plan-matching engine is
introduced.

## What It Does Not Do Yet

- No database table yet.
- No Garmin/Strava/TrainingPeaks client.
- No automatic activity rename in Strava.
- No automatic plan rewrite.

## Contract Summary

The engine returns a `PlanIntentResolution`:

- `match_state`
- `confidence_level`
- `planned_workout_id`
- `matched_event_id`
- `source`
- `source_rank`
- `canonical_title`
- `display_title`
- `activity_display_title`
- `intent_type`
- `phase`
- `scheduled_start`
- `actual_start`
- `targets`
- `evidence`
- `flags`
- `missing`
- `next_action`

## Acceptance Cases Covered

- Garmin plan title beats generic Strava title.
- Same-day planned workout matches completed event.
- Workout moved within two days is matched as moved.
- Two possible plan matches requires review.
- Completed activity with no plan is extra unplanned.
- Planned workout with no completion is missed.
- Manual correction overrides inference.
- Strava title alone is low confidence.
- Direct source workout id is high confidence.
- Plan query can return matched event.
- Serialization and dictionary helper work.
- Skipped plan state is preserved.

## Dedup Fix Caught While Wiring

When planned workouts live in the same timeline, activity ingestion must not compare a
new endurance workout against planned-workout events as duplicate candidates. Fixed in
`timeline_store.py` for both in-memory and Postgres repositories: dedup candidates are
now limited to `event_type = endurance_workout`.

## Next Step

Use the endpoints from Calendar, Activity Detail, Goal Readiness, and Recovery Context.
Do not add another router mount to `main.py`; `routers.timeline` is already mounted.
