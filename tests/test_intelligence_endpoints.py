"""
tests/test_intelligence_endpoints.py
===================================
Phase-3 HTTP layer for the P0 intelligence engines (#2 Audit, #3 Debrief, #4 Goal
Readiness, #5 Recovery Context), added to `routers.timeline`. Isolated app +
in-memory repo (no DB, no `main.py` import).

Run:  ./.venv/bin/python -m pytest tests/test_intelligence_endpoints.py -q
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.timeline as tl
from timeline_model import EventType, TimelineEvent
from timeline_store import InMemoryTimelineRepository


def _gpx(peak_hr=195, start="2026-06-23T13:00:00Z", name="Z2 Aerobic"):
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end = (start_dt + timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return (
        f'<?xml version="1.0"?>'
        f'<gpx version="1.1" creator="Garmin Connect" xmlns="http://www.topografix.com/GPX/1/1" '
        f'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f'<trk><name>{name}</name><type>cycling</type><trkseg>'
        f'<trkpt lat="19.50" lon="-99.20"><ele>2300</ele><time>{start}</time>'
        f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr>'
        f'</gpxtpx:TrackPointExtension></extensions></trkpt>'
        f'<trkpt lat="19.55" lon="-99.25"><ele>2360</ele><time>{end}</time>'
        f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{peak_hr}</gpxtpx:hr>'
        f'</gpxtpx:TrackPointExtension></extensions></trkpt>'
        f'</trkseg></trk></gpx>'
    ).encode()


@pytest.fixture()
def client(monkeypatch):
    repo = InMemoryTimelineRepository()
    monkeypatch.setattr(tl, "get_repo", lambda: repo)
    app = FastAPI()
    app.include_router(tl.router)
    c = TestClient(app)
    c._repo = repo
    return c


def _import(client, peak_hr=195, start="2026-06-23T13:00:00Z", name="Z2 Aerobic"):
    r = client.post("/timeline/import", files={
        "file": ("ride.gpx", _gpx(peak_hr, start=start, name=name), "application/gpx+xml"),
    })
    assert r.status_code == 200
    return r.json()["event_id"]


def _planned_event(
    client,
    planned_workout_id="plan-1",
    start="2026-06-23T13:00:00Z",
    title="Zone 2 Aerobic",
    duration_s=3600,
    status="planned",
):
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    ev = TimelineEvent.create(
        "default",
        EventType.PLANNED_WORKOUT,
        event_id=f"evt_{planned_workout_id}",
        start_time=start_dt,
        duration_sec=duration_s,
        sport_category="cycling",
        payload={
            "planned_workout_id": planned_workout_id,
            "source": "garmin_calendar",
            "source_workout_id": f"garmin-{planned_workout_id}",
            "plan_id": "garmin_time_trial_22w",
            "plan_name": "Time Trial Plan",
            "phase": "build",
            "week_number": 9,
            "scheduled_start": start_dt.isoformat(),
            "scheduled_date_local": start_dt.date().isoformat(),
            "sport": "cycling",
            "canonical_title": title,
            "intent_type": "endurance",
            "duration_target_s": duration_s,
            "target_hr_zone": "Z2",
            "status": status,
        },
        normalized_summary={"title": title},
    )
    client._repo.save_event(ev)
    return ev.event_id


def test_audit_endpoint_flags_suspicious_hr(client):
    eid = _import(client, peak_hr=195)
    r = client.get(f"/timeline/{eid}/audit", params={"hr_max": 185, "lthr": 168})
    assert r.status_code == 200
    body = r.json()
    codes = {f["code"] for f in body["flags"]}
    assert "suspicious_hr_max" in codes
    assert body["gating_note"] is not None


def test_audit_endpoint_404_for_unknown_event(client):
    r = client.get("/timeline/nope/audit")
    assert r.status_code == 404


def test_data_health_endpoint(client):
    _import(client, peak_hr=195)
    r = client.get("/data-health", params={"hr_max": 185, "lthr": 168})
    assert r.status_code == 200
    body = r.json()
    assert body["high_count"] >= 1
    assert body["zones_reliable"] is False


def test_debrief_endpoint_gating(client):
    eid = _import(client, peak_hr=195)
    r = client.get(f"/timeline/{eid}/debrief",
                   params={"hr_max": 185, "lthr": 168, "intent_type": "endurance base", "phase": "base"})
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == eid
    assert body["verdict"] in {"fulfilled", "over_reached", "under", "different_stimulus", "unplanned"}
    assert body["gating_note"] is not None      # red audit gated the debrief


def test_goal_readiness_endpoint(client):
    _import(client, peak_hr=170)
    r = client.get("/goal-readiness", params={
        "event_name": "Gran Fondo", "event_date": "2026-09-01",
        "sport": "cycling", "target_distance_m": 120000, "target_duration_s": 18000,
        "target_elevation_m": 2500, "hr_max": 190, "lthr": 168,
    })
    assert r.status_code == 200
    body = r.json()
    # Only one activity → not enough history for a verdict (honest state, no invented number).
    assert body["state"] == "needs_history"
    assert body["readiness_low_pct"] is None


def test_capability_matrix_endpoint(client):
    _import(client, peak_hr=170)
    r = client.get("/capability-matrix")
    assert r.status_code == 200
    body = r.json()
    assert body["n_events"] == 1
    assert "rows" in body and body["rows"]
    keys = {row["key"] for row in body["rows"]}
    assert {"session_load", "hrv_status", "running_power"} <= keys


def test_recovery_context_endpoint(client):
    _import(client, peak_hr=165, start="2026-06-27T13:00:00Z", name="Endurance 1")
    _import(client, peak_hr=166, start="2026-06-24T13:00:00Z", name="Endurance 2")
    _import(client, peak_hr=167, start="2026-06-14T13:00:00Z", name="Endurance 3")
    _import(client, peak_hr=168, start="2026-06-07T13:00:00Z", name="Endurance 4")

    r = client.get("/recovery-context", params={
        "hr_max": 190,
        "lthr": 168,
        "sleep_hours": 7.4,
        "resting_hr": 48,
        "fatigue_1_10": 3,
        "planned_title": "Endurance Ride",
        "planned_intensity": "endurance",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["athlete_id"] == "default"
    assert body["state"] in {"estimated", "needs_signal", "ready"}
    assert body["recovery_low_pct"] is not None
    assert body["training_recommendation"] in {
        "aerobic_ok", "quality_possible", "easy_only", "rest_or_recover",
    }
    assert "drivers" in body and "missing" in body


def test_plan_intent_endpoint_uses_garmin_plan_over_generic_strava_title(client):
    _planned_event(client, planned_workout_id="plan-z2", title="Zone 2 Aerobic")
    eid = _import(client, peak_hr=165, name="Morning Ride")

    r = client.get(f"/timeline/{eid}/plan-intent")
    assert r.status_code == 200
    body = r.json()
    assert body["match_state"] == "matched"
    assert body["planned_workout_id"] == "plan-z2"
    assert body["display_title"] == "Zone 2 Aerobic"
    assert body["activity_display_title"] == "Morning Ride"
    assert "activity title is generic display noise" in body["flags"]


def test_plan_intent_endpoint_reports_extra_unplanned_without_plan(client):
    eid = _import(client, peak_hr=165, name="Morning Ride")

    r = client.get(f"/timeline/{eid}/plan-intent")
    assert r.status_code == 200
    body = r.json()
    assert body["match_state"] == "extra_unplanned"
    assert body["confidence_level"] == "low"


def test_planned_workout_match_endpoint(client):
    _planned_event(client, planned_workout_id="plan-z2", title="Zone 2 Aerobic")
    _import(client, peak_hr=165, name="Morning Ride")

    r = client.get("/planned-workouts/plan-z2/match")
    assert r.status_code == 200
    body = r.json()
    assert body["match_state"] == "matched"
    assert body["matched_event_id"] is not None


def test_planned_workout_match_endpoint_reports_missed(client):
    _planned_event(client, planned_workout_id="plan-missed", title="Endurance Ride")

    r = client.get("/planned-workouts/plan-missed/match")
    assert r.status_code == 200
    body = r.json()
    assert body["match_state"] == "missed"
    assert "matched completed activity" in body["missing"]
