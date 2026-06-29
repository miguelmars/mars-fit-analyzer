"""
tests/test_recovery_context.py
==============================
Validation for Recovery Context / Recovery Reserve (#5).

Run: ./.venv/bin/python -m pytest tests/test_recovery_context.py -q
"""

from datetime import datetime, timedelta, timezone

from timeline_model import (
    AvailabilityState, Confidence, EndurancePayload, EventType, TimelineEvent,
    SIGNAL_DISTANCE, SIGNAL_DURATION, SIGNAL_HR,
)
from data_capability_matrix import capability_matrix
from data_quality_audit import AthleteDataHealth
from recovery_context import (
    PlannedWorkoutContext, RecoveryState, TrainingRecommendation, WellnessSignal,
    assess_recovery,
)

NOW = datetime(2026, 6, 28, tzinfo=timezone.utc)


def _ev(days_ago, dur_h=1.5):
    return TimelineEvent.create(
        "a", EventType.ENDURANCE_WORKOUT,
        start_time=NOW - timedelta(days=days_ago),
        duration_sec=int(dur_h * 3600),
        payload=EndurancePayload(duration_s=int(dur_h * 3600), avg_hr=145,
                                 distance_m=dur_h * 25000).to_dict(),
        confidence=Confidence(signals={
            SIGNAL_DURATION: AvailabilityState.AVAILABLE,
            SIGNAL_HR: AvailabilityState.AVAILABLE,
            SIGNAL_DISTANCE: AvailabilityState.AVAILABLE,
        }),
    )


def _history():
    return [_ev(d, 1.5) for d in range(1, 36, 3)]


def _wellness(days_ago, **kwargs):
    return WellnessSignal(date=NOW - timedelta(days=days_ago), **kwargs)


def test_load_only_returns_needs_signal_low_confidence():
    r = assess_recovery(_history(), as_of=NOW)
    assert r.state == RecoveryState.NEEDS_SIGNAL
    assert r.confidence_level == "low"
    assert "sleep" in r.missing and "HRV" in r.missing
    assert r.recovery_low_pct is not None and r.recovery_high_pct is not None


def test_sleep_and_rhr_raise_confidence_to_medium():
    wellness = [_wellness(i, sleep_hours=7.4, resting_hr=48 + (i % 2)) for i in range(0, 8)]
    r = assess_recovery(_history(), wellness=wellness, as_of=NOW)
    assert r.confidence_level == "medium"
    assert r.training_recommendation in (
        TrainingRecommendation.AEROBIC_OK,
        TrainingRecommendation.QUALITY_POSSIBLE,
    )


def test_high_subjective_fatigue_downgrades_recommendation():
    wellness = [_wellness(0, sleep_hours=7.8, fatigue_1_10=9)]
    r = assess_recovery(_history(), wellness=wellness, as_of=NOW)
    assert r.training_recommendation == TrainingRecommendation.EASY_ONLY
    assert any(d.key == "fatigue" for d in r.drivers)


def test_audit_red_adds_gating_note_and_low_confidence():
    health = AthleteDataHealth(high_count=1)
    r = assess_recovery(_history(), data_health=health, as_of=NOW)
    assert r.confidence_level == "low"
    assert r.gating_note
    assert "data quality" in " ".join(r.risks).lower()


def test_illness_note_blocks_quality_work():
    wellness = [_wellness(0, illness=True, sleep_hours=8.0)]
    r = assess_recovery(_history(), wellness=wellness, as_of=NOW)
    assert r.state == RecoveryState.RED_FLAG
    assert r.training_recommendation == TrainingRecommendation.REST_OR_RECOVER
    assert "illness_or_injury" in r.blockers


def test_needs_history_when_recent_data_is_sparse():
    r = assess_recovery([_ev(1), _ev(3)], as_of=NOW)
    assert r.state == RecoveryState.NEEDS_HISTORY
    assert r.recovery_low_pct is None


def test_conflicting_sources_are_declared():
    wellness = [_wellness(0, sleep_hours=8.0, sleep_score=90, fatigue_1_10=9)]
    r = assess_recovery(_history(), wellness=wellness, as_of=NOW)
    assert r.state == RecoveryState.CONFLICT
    assert any("disagree" in risk for risk in r.risks)


def test_capability_matrix_missing_hrv_is_reflected():
    matrix = capability_matrix(_history(), as_of=NOW)
    r = assess_recovery(_history(), capability=matrix, as_of=NOW)
    assert "HRV" in r.missing
    assert any("estimate-only" in risk for risk in r.risks)


def test_serializes():
    r = assess_recovery(
        _history(),
        wellness=[_wellness(0, sleep_hours=7.2, resting_hr=48)],
        planned_workout=PlannedWorkoutContext(title="Endurance", intensity="endurance"),
        as_of=NOW,
    )
    d = r.to_dict()
    assert d["recovery_version"] == "0.1.0"
    assert "drivers" in d and "training_recommendation" in d
