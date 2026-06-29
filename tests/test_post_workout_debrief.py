"""
tests/test_post_workout_debrief.py
=================================
Validation for the Post-Workout Debrief / Intent vs Reality layer (build step #3).
Maps to the spec's acceptance criteria.

Run:  ./.venv/bin/python -m pytest tests/test_post_workout_debrief.py -q
"""

from timeline_model import (
    Confidence, ConfidenceLevel, EndurancePayload, EventType, TimelineEvent,
)
from data_quality_audit import AuditFlag, Severity
from post_workout_debrief import (
    AthleteContext, PlannedIntent, Verdict, debrief,
)

CTX = AthleteContext(lthr=168, hr_max=187)


def _ev(avg_hr=None, max_hr=None, duration_s=None, name=None, laps=None,
        data_flags=None, conf=ConfidenceLevel.MEDIUM, distance_km=None):
    payload = EndurancePayload(
        avg_hr=avg_hr, max_hr=max_hr, duration_s=duration_s,
        original_name=name, laps=laps or [],
    ).to_dict()
    c = Confidence(level=conf, data_flags=data_flags or [])
    return TimelineEvent.create(
        "ath", EventType.ENDURANCE_WORKOUT,
        payload=payload, confidence=c, duration_sec=duration_s,
        normalized_summary={"distance_km": distance_km, "name": name},
    )


# 1. Hit the effort but the plan wanted easy aerobic → Over-reached.
def test_over_reached_when_easy_planned_but_hard_done():
    intent = PlannedIntent(intent_type="endurance base", phase="base")
    d = debrief(_ev(avg_hr=160, max_hr=175, duration_s=3600, name="Z2 Aerobic"), intent, CTX)
    assert d.verdict == Verdict.OVER_REACHED
    assert "harder" in d.verdict_reason.lower()
    assert d.plan_needs_attention is True


# 2. Recovery done easy → Fulfilled (not punished for low load).
def test_recovery_done_easy_is_fulfilled():
    intent = PlannedIntent(intent_type="recovery")
    d = debrief(_ev(avg_hr=120, max_hr=135, duration_s=1800, name="Recovery"), intent, CTX)
    assert d.verdict == Verdict.FULFILLED


# 3. No plan → Unplanned, still gives what it built.
def test_unplanned_still_debriefs():
    d = debrief(_ev(avg_hr=140, duration_s=5400, name="Morning Ride"), None, CTX)
    assert d.verdict == Verdict.UNPLANNED
    assert d.likely_built
    assert d.outcome_summary


# 4. Red audit flag → debrief shows the gating warning before concluding.
def test_red_flag_gating_is_declared():
    flags = [AuditFlag(code="suspicious_hr_max", severity=Severity.HIGH,
                       message="Saw 190 but max HR is 187 → zones may be off.",
                       suggested_action="Re-test threshold.")]
    d = debrief(_ev(avg_hr=160, max_hr=190, duration_s=3600), PlannedIntent(intent_type="threshold"),
                CTX, audit_flags=flags)
    assert d.gating_note is not None
    assert "imprecise" in d.gating_note.lower()


# 5. Coach notes are read (an "easy day" note overrides the type) → Fulfilled.
def test_coach_note_easy_overrides_and_is_reflected():
    intent = PlannedIntent(intent_type="endurance", coach_notes="Take it easy today, recovery legs")
    d = debrief(_ev(avg_hr=120, max_hr=140, duration_s=2400, name="Spin"), intent, CTX)
    assert d.verdict == Verdict.FULFILLED


def test_under_when_planned_hard_done_easy():
    intent = PlannedIntent(intent_type="threshold")
    d = debrief(_ev(avg_hr=140, max_hr=160, duration_s=3600, name="Threshold"), intent, CTX)
    assert d.verdict == Verdict.UNDER
    assert d.plan_needs_attention is True


def test_different_stimulus_when_structured_done_steady():
    intent = PlannedIntent(intent_type="VO2 intervals", intervals=[{"reps": 5}])
    # steady ride: no laps, threshold-ish effort (< VO2)
    d = debrief(_ev(avg_hr=160, max_hr=172, duration_s=3600, name="Ride", laps=[]), intent, CTX)
    assert d.verdict == Verdict.DIFFERENT_STIMULUS


def test_no_intensity_data_is_low_confidence():
    intent = PlannedIntent(intent_type="endurance")
    d = debrief(_ev(duration_s=3600, name="Ride", conf=ConfidenceLevel.LOW), intent,
                AthleteContext())  # no lthr/ftp, no avg_hr
    assert d.verdict == Verdict.FULFILLED
    assert "could not be measured" in d.verdict_reason.lower()
    assert d.confidence_level == "low"


def test_debrief_serializes():
    d = debrief(_ev(avg_hr=150, max_hr=170, duration_s=3600), PlannedIntent(intent_type="endurance"), CTX)
    out = d.to_dict()
    assert out["verdict"] in {v.value for v in Verdict}
    assert "outcome_summary" in out and "evidence" in out
