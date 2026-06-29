"""
tests/test_timeline_endpoint.py
==============================
Phase 3: HTTP layer for the Canonical Athlete Timeline.

Tests mount ONLY `routers.timeline` on a throwaway FastAPI app and inject an in-memory
repository (no DB, no `main.py` import). This keeps the endpoint test isolated from the
in-progress backend refactor.

Run:  ./.venv/bin/python -m pytest tests/test_timeline_endpoint.py -q
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.timeline as tl
from timeline_store import InMemoryTimelineRepository


_GPX = (
    '<?xml version="1.0"?>'
    '<gpx version="1.1" creator="Garmin Connect" xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
    '<trk><name>Morning Ride</name><type>cycling</type><trkseg>'
    '<trkpt lat="19.50" lon="-99.20"><ele>2300</ele><time>2026-06-23T13:00:00Z</time>'
    '<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr>'
    '</gpxtpx:TrackPointExtension></extensions></trkpt>'
    '<trkpt lat="19.55" lon="-99.25"><ele>2360</ele><time>2026-06-23T14:00:00Z</time>'
    '<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>160</gpxtpx:hr>'
    '</gpxtpx:TrackPointExtension></extensions></trkpt>'
    '</trkseg></trk></gpx>'
).encode()


@pytest.fixture()
def client(monkeypatch):
    repo = InMemoryTimelineRepository()
    monkeypatch.setattr(tl, "get_repo", lambda: repo)
    app = FastAPI()
    app.include_router(tl.router)
    return TestClient(app)


def test_import_endpoint_creates_event(client):
    r = client.post("/timeline/import",
                    files={"file": ("ride.gpx", _GPX, "application/gpx+xml")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "imported"
    assert body["event_type"] == "endurance_workout"
    assert body["sport_category"] == "cycling"
    assert body["source"] == "garmin_export"
    assert body["availability_state"] in ("available", "derived", "estimated", "missing", "unavailable", "conflict")
    assert body["raw_import_reference"].startswith("sha256:")
    assert body["confidence_level"] in ("low", "medium", "high")
    assert body["event_id"]


def test_list_endpoint_returns_event(client):
    client.post("/timeline/import", files={"file": ("ride.gpx", _GPX, "application/gpx+xml")})
    r = client.get("/timeline")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["events"][0]["event_type"] == "endurance_workout"
    assert data["events"][0]["availability_state"]


def test_import_logs_endpoint(client):
    client.post("/timeline/import", files={"file": ("ride.gpx", _GPX, "application/gpx+xml")})
    r = client.get("/timeline/import-logs")
    assert r.status_code == 200
    logs = r.json()["logs"]
    assert logs and logs[0]["status"] == "imported"
    assert logs[0]["file_hash"]


def test_import_endpoint_bad_file_fails_safely(client):
    r = client.post("/timeline/import",
                    files={"file": ("junk.txt", b"not an activity", "text/plain")})
    assert r.status_code == 200            # safe failure, not a crash
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "failed"
    assert body["error"]
    # No event created; timeline stays clean.
    assert client.get("/timeline").json()["count"] == 0


def test_duplicate_upload_via_endpoint(client):
    client.post("/timeline/import", files={"file": ("ride.gpx", _GPX, "application/gpx+xml")})
    r2 = client.post("/timeline/import", files={"file": ("ride.gpx", _GPX, "application/gpx+xml")})
    assert r2.json()["status"] == "duplicate"
    # Default list hides confirmed duplicates.
    assert client.get("/timeline").json()["count"] == 1
    assert client.get("/timeline", params={"include_duplicates": True}).json()["count"] == 2
