"""
tests/test_goal_readiness.py
===========================
Validation for Goal Readiness / Capability Gap (build step #4).
Maps to the spec's acceptance criteria: range-with-confidence (not exact), weakest
capability, volume gap, low confidence when audit is red, needs_history, next proof point.

Run:  ./.venv/bin/python -m pytest tests/test_goal_readiness.py -q
"""

from datetime import datetime, timedelta, timezone

from timeline_model import EndurancePayload, EventType, TimelineEvent
from data_quality_audit import AthleteDataHealth
from goal_readiness import Goal, ReadinessState, assess

NOW = datetime(2026, 6, 28, tzinfo=timezone.utc)


def _ev(days_ago, dur_h, elev=200.0):
    p = EndurancePayload(duration_s=int(dur_h * 3600), elevation_gain_m=elev,
                         distance_m=dur_h * 28000).to_dict()
    return TimelineEvent.create("a", EventType.ENDURANCE_WORKOUT,
                                start_time=NOW - timedelta(days=days_ago),
                                payload=p, duration_sec=int(dur_h * 3600))


def _six_weeks_flat():
    hist = []
    for w in range(6):
        hist.append(_ev(w * 7 + 1, 2.0))
        hist.append(_ev(w * 7 + 4, 2.5))
    return hist


def _hilly_goal():
    return Goal(name="Gran Fondo", event_date=NOW + timedelta(weeks=10), sport="cycling",
                target_distance_m=120000, target_duration_s=int(4.5 * 3600),
                target_elevation_m=2500)


# 1. range with confidence, not an exact number
def test_returns_range_not_exact_number():
    r = assess(_six_weeks_flat(), _hilly_goal(), as_of=NOW)
    assert r.state == ReadinessState.READY_RANGE
    assert r.readiness_low_pct is not None and r.readiness_high_pct is not None
    assert r.readiness_low_pct < r.readiness_high_pct
    assert r.confidence_level in ("low", "medium", "high")


# 2. weakest capability for the demand (flat history vs hilly event → climbing)
def test_identifies_weakest_capability_climbing():
    r = assess(_six_weeks_flat(), _hilly_goal(), as_of=NOW)
    assert r.weakest_capability == "climbing"
    assert "climbing" in r.blockers


# 3. concrete volume gap
def test_shows_volume_gap():
    r = assess(_six_weeks_flat(), _hilly_goal(), as_of=NOW)
    assert r.volume_recent_h_per_week == 4.5
    assert r.volume_target_h_per_week and r.volume_target_h_per_week > r.volume_recent_h_per_week
    assert r.volume_gap_h_per_week == round(r.volume_target_h_per_week - r.volume_recent_h_per_week, 1)


# 4. data flagged red by #2 → low confidence + warning
def test_low_confidence_when_audit_red():
    health = AthleteDataHealth(high_count=1, zones_reliable=False)
    r = assess(_six_weeks_flat(), _hilly_goal(), data_health=health, as_of=NOW)
    assert r.confidence_level == "low"
    assert any("data quality" in risk.lower() for risk in r.risks)


# 5. not enough history → needs_history (no invented verdict)
def test_needs_history_when_sparse():
    hist = [_ev(2, 2.0), _ev(5, 2.0)]  # only ~1-2 active weeks
    r = assess(hist, _hilly_goal(), as_of=NOW)
    assert r.state == ReadinessState.NEEDS_HISTORY
    assert r.readiness_low_pct is None


# 6. actionable next proof point
def test_next_proof_point_present():
    r = assess(_six_weeks_flat(), _hilly_goal(), as_of=NOW)
    assert r.next_proof_point


# states: no target / event passed
def test_no_target():
    assert assess(_six_weeks_flat(), None, as_of=NOW).state == ReadinessState.NO_TARGET
    g = Goal(name="x")  # no date
    assert assess(_six_weeks_flat(), g, as_of=NOW).state == ReadinessState.NO_TARGET


def test_event_passed():
    g = Goal(name="Past", event_date=NOW - timedelta(days=3))
    assert assess(_six_weeks_flat(), g, as_of=NOW).state == ReadinessState.EVENT_PASSED


def test_serializes():
    r = assess(_six_weeks_flat(), _hilly_goal(), as_of=NOW)
    d = r.to_dict()
    assert d["state"] == "ready_range" and "capability_scores" in d
