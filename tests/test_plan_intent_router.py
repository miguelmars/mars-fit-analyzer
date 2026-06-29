"""
tests/test_plan_intent_router.py
================================
Validation for Plan / Intent Source Router.

Run: ./.venv/bin/python -m pytest tests/test_plan_intent_router.py -q
"""

from datetime import datetime, timedelta, timezone

from timeline_model import EndurancePayload, EventType, TimelineEvent
from plan_intent_router import (
    IntentConfidence,
    ManualIntentCorrection,
    MatchState,
    PlannedWorkout,
    planned_workout_from_dict,
    resolve_plan_intent,
)

NOW = datetime(2026, 6, 29, 13, 0, tzinfo=timezone.utc)


def _plan(
    pid="p1",
    *,
    days=0,
    title="Zone 2 Aerobic",
    source="garmin_calendar",
    sport="cycling",
    intent="endurance",
    duration_s=3600,
    status="planned",
    source_workout_id=None,
):
    start = NOW + timedelta(days=days)
    return PlannedWorkout(
        planned_workout_id=pid,
        athlete_id="a",
        source=source,
        source_workout_id=source_workout_id,
        plan_id="garmin_time_trial_22w",
        plan_name="Time Trial Plan",
        phase="build",
        week_number=9,
        scheduled_start=start,
        scheduled_date_local=start.date().isoformat(),
        sport=sport,
        canonical_title=title,
        intent_type=intent,
        duration_target_s=duration_s,
        target_hr_zone="Z2" if intent == "endurance" else None,
        status=status,
    )


def _event(
    eid="e1",
    *,
    days=0,
    title="Morning Ride",
    sport="cycling",
    duration_s=3600,
    planned_workout_id=None,
    source_workout_id=None,
):
    payload = EndurancePayload(
        duration_s=duration_s,
        distance_m=28000,
        avg_hr=145,
        original_name=title,
        sport_type=sport,
    ).to_dict()
    if planned_workout_id:
        payload["planned_workout_id"] = planned_workout_id
    if source_workout_id:
        payload["source_workout_id"] = source_workout_id
    return TimelineEvent.create(
        "a",
        EventType.ENDURANCE_WORKOUT,
        event_id=eid,
        start_time=NOW + timedelta(days=days),
        duration_sec=duration_s,
        sport_category=sport,
        payload=payload,
        normalized_summary={"title": title},
    )


def test_garmin_plan_beats_strava_generic_title():
    plan = _plan(title="Zone 2 Aerobic", source="garmin_calendar")
    event = _event(title="Morning Ride")
    r = resolve_plan_intent([plan], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.MATCHED
    assert r.display_title == "Zone 2 Aerobic"
    assert r.activity_display_title == "Morning Ride"
    assert "activity title is generic display noise" in r.flags
    assert any("planned intent beats" in e for e in r.evidence)


def test_same_day_planned_workout_matches_completed_event():
    plan = _plan()
    event = _event(title="Zone 2 Aerobic")
    r = resolve_plan_intent([plan], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.MATCHED
    assert r.planned_workout_id == plan.planned_workout_id
    assert r.matched_event_id == event.event_id
    assert r.confidence_level in (IntentConfidence.MEDIUM, IntentConfidence.HIGH)


def test_moved_workout_matches_within_two_days():
    plan = _plan(days=0)
    event = _event(days=2, title="Zone 2 Aerobic")
    r = resolve_plan_intent([plan], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.MATCHED_MOVED_DAY
    assert any("moved within 2 day" in e for e in r.evidence)


def test_two_possible_planned_workouts_need_review():
    plan_a = _plan("p-a", title="Zone 2 Aerobic")
    plan_b = _plan("p-b", title="Endurance Ride")
    event = _event(title="Morning Ride")
    r = resolve_plan_intent([plan_a, plan_b], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.NEEDS_REVIEW
    assert r.confidence_level == IntentConfidence.LOW
    assert any("multiple possible" in f for f in r.flags)


def test_completed_activity_with_no_plan_is_extra_unplanned():
    event = _event(title="Morning Ride")
    r = resolve_plan_intent([], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.EXTRA_UNPLANNED
    assert r.confidence_level == IntentConfidence.LOW
    assert "no plan source" in r.missing


def test_planned_workout_with_no_completed_event_is_missed():
    plan = _plan()
    r = resolve_plan_intent([plan], [], planned_workout=plan, athlete_id="a")
    assert r.match_state == MatchState.MISSED
    assert "matched completed activity" in r.missing


def test_manual_correction_overrides_inference():
    plan_a = _plan("p-a", title="Zone 2 Aerobic")
    plan_b = _plan("p-b", title="Tempo Intervals", intent="tempo")
    event = _event(title="Morning Ride")
    correction = ManualIntentCorrection(event_id=event.event_id, planned_workout_id=plan_b.planned_workout_id)
    r = resolve_plan_intent([plan_a, plan_b], [event], event=event,
                            manual_corrections=[correction], athlete_id="a")
    assert r.match_state == MatchState.MATCHED
    assert r.confidence_level == IntentConfidence.HIGH
    assert r.planned_workout_id == "p-b"
    assert any("manual athlete correction" in e for e in r.evidence)


def test_strava_title_alone_is_low_confidence():
    event = _event(title="Morning Ride")
    r = resolve_plan_intent([], [event], event=event, athlete_id="a")
    assert r.confidence_level == IntentConfidence.LOW
    assert any("Strava/generic title" in f for f in r.flags)


def test_direct_source_workout_id_is_high_confidence():
    plan = _plan(source_workout_id="garmin-workout-42")
    event = _event(title="Morning Ride", source_workout_id="garmin-workout-42")
    r = resolve_plan_intent([plan], [event], event=event, athlete_id="a")
    assert r.match_state == MatchState.MATCHED
    assert r.confidence_level == IntentConfidence.HIGH
    assert any("direct planned workout reference" in e for e in r.evidence)


def test_plan_query_returns_matched_event():
    plan = _plan()
    event = _event(title="Zone 2 Aerobic")
    r = resolve_plan_intent([plan], [event], planned_workout=plan, athlete_id="a")
    assert r.match_state == MatchState.MATCHED
    assert r.matched_event_id == event.event_id


def test_serializes_and_from_dict_helper():
    plan = planned_workout_from_dict({
        "planned_workout_id": "p-json",
        "athlete_id": "a",
        "source": "garmin_calendar",
        "scheduled_start": NOW.isoformat(),
        "sport": "cycling",
        "canonical_title": "Zone 2 Aerobic",
        "intent_type": "endurance",
        "duration_target_s": 3600,
    })
    event = _event(title="Morning Ride")
    d = resolve_plan_intent([plan], [event], event=event, athlete_id="a").to_dict()
    assert d["resolution_version"] == "0.1.0"
    assert d["display_title"] == "Zone 2 Aerobic"
    assert d["targets"]["duration_s"] == 3600


def test_skipped_plan_state_is_preserved():
    plan = _plan(status="skipped")
    r = resolve_plan_intent([plan], [], planned_workout=plan, athlete_id="a")
    assert r.match_state == MatchState.SKIPPED

