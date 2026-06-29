"""
tests/test_data_capability_matrix.py
===================================
Validation for the Data Capability Matrix (foundation layer over the timeline):
what Epoch can compute now vs needs-history vs needs-signal, from real signal coverage.

Run:  ./.venv/bin/python -m pytest tests/test_data_capability_matrix.py -q
"""

from datetime import datetime, timedelta, timezone

from timeline_model import (
    AvailabilityState, Confidence, EndurancePayload, EventType, TimelineEvent,
    SIGNAL_CADENCE, SIGNAL_DISTANCE, SIGNAL_DURATION, SIGNAL_ELEVATION, SIGNAL_GPS,
    SIGNAL_HR, SIGNAL_POWER,
)
from data_capability_matrix import capability_matrix, MetricStatus

NOW = datetime(2026, 6, 28, tzinfo=timezone.utc)


def _ev(days_ago, signals):
    sg = {s: AvailabilityState.AVAILABLE for s in signals}
    return TimelineEvent.create(
        "a", EventType.ENDURANCE_WORKOUT, start_time=NOW - timedelta(days=days_ago),
        payload=EndurancePayload(duration_s=3600, elevation_gain_m=300,
                                 distance_m=30000, avg_hr=150).to_dict(),
        confidence=Confidence(signals=sg), duration_sec=3600)


def _status(matrix, key):
    return next(r.status for r in matrix.rows if r.key == key)


def _hr_only_history():
    sigs = [SIGNAL_DURATION, SIGNAL_HR, SIGNAL_DISTANCE, SIGNAL_GPS, SIGNAL_ELEVATION, SIGNAL_CADENCE]
    return [_ev(d, sigs) for d in range(1, 57, 4)]  # ~8 weeks, HR-only (no power)


def test_hr_only_athlete_capability_tiers():
    m = capability_matrix(_hr_only_history(), as_of=NOW)
    # Computable now from HR + history:
    assert _status(m, "session_load") == MetricStatus.AVAILABLE_NOW
    assert _status(m, "fitness_fatigue_form") == MetricStatus.AVAILABLE_NOW
    assert _status(m, "climbing") == MetricStatus.AVAILABLE_NOW
    assert _status(m, "route_comparison") == MetricStatus.AVAILABLE_NOW
    # Recovery Reserve only as an estimate (no sleep/HRV):
    assert _status(m, "recovery_reserve") == MetricStatus.ESTIMATE_ONLY
    # Device-only signals missing:
    assert _status(m, "hrv_status") == MetricStatus.NEEDS_SIGNAL
    assert _status(m, "running_power") == MetricStatus.NEEDS_SIGNAL
    assert "Session Load (TSS-equivalent)" in m.available_now


def test_no_hr_no_power_blocks_load():
    sigs = [SIGNAL_DURATION, SIGNAL_GPS, SIGNAL_DISTANCE, SIGNAL_ELEVATION]
    m = capability_matrix([_ev(d, sigs) for d in range(1, 30, 3)], as_of=NOW)
    # Without HR or power, Session Load cannot be computed.
    assert _status(m, "session_load") == MetricStatus.NEEDS_SIGNAL
    # But climbing/volume still work.
    assert _status(m, "climbing") == MetricStatus.AVAILABLE_NOW
    assert _status(m, "volume") == MetricStatus.AVAILABLE_NOW


def test_short_history_needs_history():
    sigs = [SIGNAL_DURATION, SIGNAL_HR, SIGNAL_DISTANCE]
    m = capability_matrix([_ev(d, sigs) for d in (1, 4, 8)], as_of=NOW)  # ~1 week span
    assert _status(m, "session_load") == MetricStatus.AVAILABLE_NOW       # history_days=0
    assert _status(m, "fitness_fatigue_form") == MetricStatus.NEEDS_HISTORY  # needs 42d
    assert any("more days of history" in (u or "") for u in m.unlock_suggestions)


def test_climbing_needs_elevation():
    sigs = [SIGNAL_DURATION, SIGNAL_HR, SIGNAL_DISTANCE]   # no elevation
    m = capability_matrix([_ev(d, sigs) for d in range(1, 30, 3)], as_of=NOW)
    assert _status(m, "climbing") == MetricStatus.NEEDS_SIGNAL


def test_empty_timeline():
    m = capability_matrix([], as_of=NOW)
    assert m.n_events == 0
    assert "import" in m.summary.lower()
    assert m.rows == []


def test_serializes():
    m = capability_matrix(_hr_only_history(), as_of=NOW)
    d = m.to_dict()
    assert d["n_events"] == 14 and "rows" in d and d["available_now"]
