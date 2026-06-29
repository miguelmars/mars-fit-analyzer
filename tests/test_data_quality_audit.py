"""
tests/test_data_quality_audit.py
================================
Validation for the Data Quality + Zone Audit layer (build step #2).
Maps to the spec's acceptance criteria: suspicious HR max, stale FTP (suggest, not change),
mislabeled activity, unreliable power, red-flag gating, and "nothing auto-changed".

Run:  ./.venv/bin/python -m pytest tests/test_data_quality_audit.py -q
"""

from datetime import datetime, timedelta, timezone

from timeline_model import (
    AvailabilityState, Confidence, EndurancePayload, EventStatus, EventType,
    SourceLineage, Source, TimelineEvent, SIGNAL_HR,
)
from data_quality_audit import (
    AthleteProfile, Severity, audit_event, audit_athlete, gating_note,
)

_NOW = datetime(2026, 6, 27, tzinfo=timezone.utc)


def _ev(**kw):
    payload = EndurancePayload(
        original_name=kw.get("name"),
        max_hr=kw.get("max_hr"),
        avg_hr=kw.get("avg_hr"),
        max_power=kw.get("max_power"),
        avg_power=kw.get("avg_power"),
        normalized_power=kw.get("normalized_power"),
        distance_m=kw.get("distance_m"),
    ).to_dict()
    conf = Confidence(signals=kw.get("signals") or {}, data_flags=kw.get("data_flags") or [])
    return TimelineEvent.create(
        "ath", EventType.ENDURANCE_WORKOUT,
        start_time=kw.get("start", _NOW),
        payload=payload, confidence=conf,
        source=kw.get("source") or SourceLineage(source=Source.FILE_UPLOAD),
        status=kw.get("status", EventStatus.ACTIVE),
    )


def _codes(flags):
    return {f.code for f in flags}


# 1. suspicious_hr_max → HIGH + gating
def test_suspicious_hr_max_high_and_gating():
    profile = AthleteProfile(hr_max=185, lthr=168)
    flags = audit_event(_ev(max_hr=188, avg_hr=150, signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)
    assert "suspicious_hr_max" in _codes(flags)
    hi = [f for f in flags if f.code == "suspicious_hr_max"][0]
    assert hi.severity == Severity.HIGH
    assert gating_note(flags) is not None


# clean event → no flags
def test_clean_event_has_no_flags():
    profile = AthleteProfile(hr_max=190, lthr=168)
    flags = audit_event(_ev(max_hr=170, avg_hr=140, signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)
    assert flags == []


# 2. stale FTP by date → MEDIUM, suggests test, does NOT change FTP
def test_stale_ftp_by_date_suggests_only():
    profile = AthleteProfile(ftp_w=250, ftp_set_date=_NOW - timedelta(days=200))
    health = audit_athlete([_ev(avg_hr=150, signals={SIGNAL_HR: AvailabilityState.AVAILABLE})],
                           profile, as_of=_NOW)
    assert "stale_ftp" in _codes(health.flags)
    assert health.ftp_current is False
    assert profile.ftp_w == 250          # nothing auto-changed


def test_stale_ftp_by_recent_power():
    profile = AthleteProfile(ftp_w=250, ftp_set_date=_NOW - timedelta(days=10))
    e = _ev(normalized_power=270, avg_hr=150, signals={SIGNAL_HR: AvailabilityState.AVAILABLE},
            start=_NOW - timedelta(days=5))
    health = audit_athlete([e], profile, as_of=_NOW)
    assert "stale_ftp" in _codes(health.flags)


# 3. mislabeled activity (easy/recovery name but high HR)
def test_mislabeled_recovery_high_intensity():
    profile = AthleteProfile(lthr=168)
    flags = audit_event(_ev(name="Recovery Ride", avg_hr=160,
                            signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)
    assert "mislabeled_activity" in _codes(flags)


# 4. unreliable power
def test_unreliable_power_flag():
    profile = AthleteProfile()
    flags = audit_event(_ev(max_power=3000, avg_power=200,
                            signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)
    assert "unreliable_power" in _codes(flags)


# missing sensor data surfaced
def test_missing_sensor_data_flag():
    profile = AthleteProfile(hr_max=190)
    flags = audit_event(_ev(signals={SIGNAL_HR: AvailabilityState.MISSING}), profile)
    assert "missing_sensor_data" in _codes(flags)


# duplicate-uncertain surfaced as informational
def test_duplicate_uncertain_flag():
    profile = AthleteProfile()
    flags = audit_event(_ev(status=EventStatus.DUPLICATE_UNCERTAIN, data_flags=["possible_duplicate"],
                            signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)
    dup = [f for f in flags if f.code == "duplicate"]
    assert dup and "uncertain" in dup[0].message.lower()


# incorrect zones (top zone above max HR)
def test_incorrect_zones_high():
    profile = AthleteProfile(hr_max=185, lthr=168, hr_zones=[(0, 120), (120, 150), (150, 200)])
    health = audit_athlete([], profile, as_of=_NOW)
    assert "incorrect_zones" in _codes(health.flags)
    assert health.zones_reliable is False


# inconsistent source between merged events
def test_inconsistent_source_between_merged():
    e1 = _ev(distance_m=40000, source=SourceLineage(source=Source.STRAVA_EXPORT))
    e2 = _ev(distance_m=50000, source=SourceLineage(source=Source.GARMIN_EXPORT,
                                                    merged_from=[e1.event_id]))
    health = audit_athlete([e1, e2], AthleteProfile(), as_of=_NOW)
    assert "inconsistent_source" in _codes(health.flags)


# 5/6. gating + no-auto-change discipline
def test_gating_none_without_high_flags():
    profile = AthleteProfile(lthr=168)
    flags = audit_event(_ev(name="Recovery", avg_hr=160,
                            signals={SIGNAL_HR: AvailabilityState.AVAILABLE}), profile)  # only MEDIUM
    assert gating_note(flags) is None


def test_audit_does_not_mutate_inputs():
    profile = AthleteProfile(hr_max=185, lthr=168, ftp_w=250, ftp_set_date=_NOW - timedelta(days=300))
    e = _ev(max_hr=190, avg_hr=160, name="Recovery",
            signals={SIGNAL_HR: AvailabilityState.AVAILABLE})
    payload_before = dict(e.payload)
    audit_athlete([e], profile, as_of=_NOW)
    assert profile.hr_max == 185 and profile.ftp_w == 250     # profile untouched
    assert e.payload == payload_before                         # event untouched
    assert e.status == EventStatus.ACTIVE
