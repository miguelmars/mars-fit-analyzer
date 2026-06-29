"""
tests/test_plan_session_timeline_bridge.py
==========================================
Validation for bridging legacy plan_sessions rows into Canonical Athlete Timeline
planned_workout events.
"""

from datetime import date

from plan_intent_router import MatchState, resolve_plan_intent
from plan_session_timeline_bridge import (
    plan_session_event_id,
    planned_workout_id,
    row_to_planned_workout_event,
)
from timeline_model import EndurancePayload, EventType, TimelineEvent


def _row(
    *,
    plan_session_id=42,
    planned_date=date(2026, 6, 30),
    description="Threshold intervals (Garmin TT Build scaffold)",
    session_type="threshold_intervals",
    status="planned",
    target=None,
    matched_clean_session_id=None,
):
    return {
        "id": plan_session_id,
        "plan_id": "garmin_tt_2026",
        "plan_name": "Garmin Coach — Time Trial Plan",
        "plan_source": "garmin_coach",
        "week_number": 9,
        "planned_date": planned_date,
        "session_type": session_type,
        "description": description,
        "target": target or {
            "phase": "build",
            "duration_min": 80,
            "intensity": "threshold",
            "primary_zone": "threshold",
            "source": "garmin_coach_reconstructed",
            "source_confidence": "medium",
        },
        "matched_clean_session_id": matched_clean_session_id,
        "status": status,
        "moved_from": None,
        "move_reason": None,
    }


def _completed_event(title="Morning Ride", days=0, duration_s=4800):
    payload = EndurancePayload(
        sport_type="cycling",
        duration_s=duration_s,
        distance_m=42000,
        avg_hr=154,
        original_name=title,
    ).to_dict()
    return TimelineEvent.create(
        "default",
        EventType.ENDURANCE_WORKOUT,
        event_id="evt_done",
        start_time=date(2026, 6, 30).fromordinal(date(2026, 6, 30).toordinal()),
        duration_sec=duration_s,
        sport_category="cycling",
        payload=payload,
        normalized_summary={"title": title},
    )


def test_plan_session_row_maps_to_planned_workout_event():
    ev = row_to_planned_workout_event(_row())
    assert ev.event_id == "evt_plan_42"
    assert ev.event_type == EventType.PLANNED_WORKOUT
    assert ev.payload["planned_workout_id"] == "plan_session_42"
    assert ev.payload["plan_id"] == "garmin_tt_2026"
    assert ev.payload["canonical_title"] == "Threshold intervals (Garmin TT Build scaffold)"
    assert ev.payload["intent_type"] == "threshold"
    assert ev.payload["duration_target_s"] == 4800
    assert ev.payload["scheduled_date_local"] == "2026-06-30"
    assert ev.normalized_summary["phase"] == "build"
    assert ev.confidence.level.value == "medium"


def test_plan_session_ids_are_deterministic():
    assert plan_session_event_id(99) == "evt_plan_99"
    assert planned_workout_id(99) == "plan_session_99"


def test_low_confidence_plan_source_is_flagged():
    ev = row_to_planned_workout_event(_row(target={
        "phase": "base",
        "duration_min": 75,
        "intensity": "unknown",
        "source": "garmin_coach_confirmed_date",
        "source_confidence": "low",
    }))
    assert ev.confidence.level.value == "medium"
    assert "plan_source_confidence:low" in ev.confidence.data_flags


def test_matched_clean_session_creates_linked_event_id():
    ev = row_to_planned_workout_event(_row(matched_clean_session_id="abc123"))
    assert ev.linked_event_ids == ["evt_cs_abc123"]
    assert ev.payload["matched_clean_session_id"] == "abc123"


def test_plan_session_event_feeds_plan_intent_router():
    planned_event = row_to_planned_workout_event(_row())
    completed = TimelineEvent.create(
        "default",
        EventType.ENDURANCE_WORKOUT,
        event_id="evt_done",
        start_time=planned_event.start_time,
        duration_sec=4800,
        sport_category="cycling",
        payload=EndurancePayload(
            sport_type="cycling",
            duration_s=4800,
            distance_m=42000,
            avg_hr=154,
            original_name="Morning Ride",
        ).to_dict(),
        normalized_summary={"title": "Morning Ride"},
    )

    from routers.timeline import _planned_workouts_from_timeline

    plans = _planned_workouts_from_timeline([planned_event, completed], "default")
    r = resolve_plan_intent(plans, [completed], event=completed, athlete_id="default")
    assert r.match_state == MatchState.MATCHED
    assert r.display_title == "Threshold intervals (Garmin TT Build scaffold)"
    assert r.activity_display_title == "Morning Ride"
