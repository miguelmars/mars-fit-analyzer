"""
tests/test_p0_pipeline_integration.py
=====================================
Integration: proves the P0 spine composes end-to-end —
  #1 Ingestion  →  #2 Data Quality + Zone Audit  →  #3 Post-Workout Debrief
  →  #4 Goal Readiness / Capability Gap
through real objects (not mocks): a file is ingested into the timeline, audited against
an athlete profile, and the audit's 🔴 gating flows into the debrief's conclusion.

Run:  ./.venv/bin/python -m pytest tests/test_p0_pipeline_integration.py -q
"""

from datetime import datetime, timezone

from ingest_pipeline import ingest_file
from timeline_store import InMemoryTimelineRepository
from timeline_model import EventType
from data_quality_audit import AthleteProfile, Severity, audit_athlete
from post_workout_debrief import AthleteContext, PlannedIntent, debrief
from goal_readiness import Goal, ReadinessState, assess


def _gpx_with_max_hr(peak_hr: int, start_iso: str = "2026-06-23T13:00:00Z",
                     end_iso: str = "2026-06-23T14:00:00Z") -> bytes:
    return (
        f'<?xml version="1.0"?>'
        f'<gpx version="1.1" creator="Garmin Connect" xmlns="http://www.topografix.com/GPX/1/1" '
        f'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f'<trk><name>Z2 Aerobic</name><type>cycling</type><trkseg>'
        f'<trkpt lat="19.50" lon="-99.20"><ele>2300</ele><time>{start_iso}</time>'
        f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr>'
        f'</gpxtpx:TrackPointExtension></extensions></trkpt>'
        f'<trkpt lat="19.55" lon="-99.25"><ele>2360</ele><time>{end_iso}</time>'
        f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{peak_hr}</gpxtpx:hr>'
        f'</gpxtpx:TrackPointExtension></extensions></trkpt>'
        f'</trkseg></trk></gpx>'
    ).encode()


def test_ingestion_audit_debrief_chain_with_gating():
    # #1 Ingestion: a real file becomes a timeline event.
    repo = InMemoryTimelineRepository()
    res = ingest_file(_gpx_with_max_hr(195), "ride.gpx", "athlete1", repo)
    assert res.ok and res.event.event_type == EventType.ENDURANCE_WORKOUT
    event = res.event
    for i, (start, end) in enumerate((
        ("2026-06-16T13:00:00Z", "2026-06-16T15:00:00Z"),
        ("2026-06-09T13:00:00Z", "2026-06-09T15:00:00Z"),
        ("2026-06-02T13:00:00Z", "2026-06-02T15:00:00Z"),
    )):
        extra = ingest_file(_gpx_with_max_hr(170, start, end), f"support-{i}.gpx", "athlete1", repo)
        assert extra.ok

    # #2 Audit: the activity's max HR (195) exceeds the declared max (185) → 🔴 flag.
    profile = AthleteProfile(hr_max=185, lthr=168)
    health = audit_athlete(repo.list_events("athlete1"), profile)
    assert any(f.code == "suspicious_hr_max" and f.severity == Severity.HIGH for f in health.flags)
    assert health.zones_reliable is False

    # #3 Debrief: the 🔴 audit gating must flow into the conclusion.
    ctx = AthleteContext(lthr=profile.lthr, hr_max=profile.hr_max)
    d = debrief(event, PlannedIntent(intent_type="endurance base", phase="base"), ctx,
                audit_flags=health.flags)
    assert d.gating_note is not None                      # gating crossed all three layers
    assert "imprecise" in d.gating_note.lower()
    assert d.event_id == event.event_id

    # #4 Goal Readiness: the same red audit must lower readiness confidence.
    goal = Goal(name="Gran Fondo", event_date=event.start_time.replace(year=2026, month=9, day=1),
                sport="cycling", target_distance_m=120000, target_duration_s=5 * 3600,
                target_elevation_m=1800)
    readiness = assess(repo.list_events("athlete1"), goal, data_health=health, as_of=event.start_time)
    assert readiness.state == ReadinessState.READY_RANGE
    assert readiness.confidence_level == "low"
    assert "data_quality" in readiness.blockers


def test_clean_chain_has_no_gating():
    repo = InMemoryTimelineRepository()
    res = ingest_file(_gpx_with_max_hr(170), "ride.gpx", "athlete1", repo)
    event = res.event
    profile = AthleteProfile(hr_max=190, lthr=168)
    health = audit_athlete(repo.list_events("athlete1"), profile)
    assert not any(f.severity == Severity.HIGH for f in health.flags)
    d = debrief(event, PlannedIntent(intent_type="endurance"), AthleteContext(lthr=168, hr_max=190),
                audit_flags=health.flags)
    assert d.gating_note is None                          # nothing to warn about


def test_full_spine_readiness_after_imported_history():
    repo = InMemoryTimelineRepository()
    as_of = datetime(2026, 6, 28, tzinfo=timezone.utc)
    for i, (start, end) in enumerate((
        ("2026-06-26T13:00:00Z", "2026-06-26T15:00:00Z"),
        ("2026-06-19T13:00:00Z", "2026-06-19T15:00:00Z"),
        ("2026-06-12T13:00:00Z", "2026-06-12T15:00:00Z"),
        ("2026-06-05T13:00:00Z", "2026-06-05T15:00:00Z"),
        ("2026-05-29T13:00:00Z", "2026-05-29T15:00:00Z"),
        ("2026-05-22T13:00:00Z", "2026-05-22T15:00:00Z"),
    )):
        res = ingest_file(_gpx_with_max_hr(170, start, end), f"ride-{i}.gpx", "athlete1", repo)
        assert res.ok

    profile = AthleteProfile(hr_max=190, lthr=168)
    health = audit_athlete(repo.list_events("athlete1"), profile)
    goal = Goal(name="September Fondo", event_date=datetime(2026, 9, 15, tzinfo=timezone.utc),
                sport="cycling", target_distance_m=120000, target_duration_s=5 * 3600,
                target_elevation_m=1800)

    readiness = assess(repo.list_events("athlete1"), goal, data_health=health, as_of=as_of)
    assert readiness.state == ReadinessState.READY_RANGE
    assert readiness.readiness_low_pct is not None
    assert readiness.readiness_low_pct < readiness.readiness_high_pct
    assert readiness.weakest_capability
    assert readiness.next_proof_point
