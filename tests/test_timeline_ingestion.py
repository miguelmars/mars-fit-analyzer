"""
tests/test_timeline_ingestion.py
================================
Validation cases for the P0 Canonical Athlete Timeline + Activity Ingestion.

Covers: FIT/GPX/TCX/CSV import -> endurance_workout events; confidence flags;
source lineage; dedup (same file + cross-source precedence); safe failure on bad files;
extensibility (a strength_session event with no schema change); serialization roundtrip.

Run (from repo root):  ./.venv/bin/python -m pytest tests/test_timeline_ingestion.py -q
"""

from datetime import datetime, timezone

import pytest

from timeline_model import (
    AvailabilityState, EventStatus, EventType, FileType, Source, SourceLineage,
    TimelineEvent, SIGNAL_HR, SIGNAL_POWER,
)
from ingest_parsers import parse, parse_gpx, parse_tcx, parse_csv, ParseError
from ingest_pipeline import ingest_file, detect_sport
from timeline_store import InMemoryTimelineRepository


# ── Synthetic fixtures (inline; no raw files committed — .gitignore blocks them) ──

def _gpx(creator="Garmin Connect", t0="2026-06-23T13:00:00Z", t1="2026-06-23T14:00:00Z",
         lat0=19.50, lon0=-99.20, lat1=19.55, lon1=-99.25, hr=True):
    hr0 = ("<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr>"
           "</gpxtpx:TrackPointExtension></extensions>") if hr else ""
    hr1 = ("<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>160</gpxtpx:hr>"
           "</gpxtpx:TrackPointExtension></extensions>") if hr else ""
    return (
        f'<?xml version="1.0"?>'
        f'<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1" '
        f'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">'
        f'<trk><name>Morning Ride</name><type>cycling</type><trkseg>'
        f'<trkpt lat="{lat0}" lon="{lon0}"><ele>2300</ele><time>{t0}</time>{hr0}</trkpt>'
        f'<trkpt lat="{lat1}" lon="{lon1}"><ele>2360</ele><time>{t1}</time>{hr1}</trkpt>'
        f'</trkseg></trk></gpx>'
    ).encode()


_TCX = (
    '<?xml version="1.0"?>'
    '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
    'xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">'
    '<Activities><Activity Sport="Biking">'
    '<Lap StartTime="2026-06-23T13:00:00Z"><TotalTimeSeconds>600</TotalTimeSeconds>'
    '<DistanceMeters>5000</DistanceMeters><Calories>120</Calories>'
    '<AverageHeartRateBpm><Value>150</Value></AverageHeartRateBpm>'
    '<MaximumHeartRateBpm><Value>165</Value></MaximumHeartRateBpm>'
    '<Track>'
    '<Trackpoint><Time>2026-06-23T13:00:00Z</Time>'
    '<Position><LatitudeDegrees>19.5</LatitudeDegrees><LongitudeDegrees>-99.2</LongitudeDegrees></Position>'
    '<AltitudeMeters>2300</AltitudeMeters><HeartRateBpm><Value>150</Value></HeartRateBpm>'
    '<Extensions><ns3:TPX><ns3:Watts>180</ns3:Watts></ns3:TPX></Extensions></Trackpoint>'
    '<Trackpoint><Time>2026-06-23T13:10:00Z</Time><HeartRateBpm><Value>160</Value></HeartRateBpm>'
    '<Extensions><ns3:TPX><ns3:Watts>200</ns3:Watts></ns3:TPX></Extensions></Trackpoint>'
    '</Track></Lap>'
    '<Creator><Name>Garmin Forerunner 935</Name></Creator>'
    '</Activity></Activities></TrainingCenterDatabase>'
).encode()

_CSV_SUMMARY = (
    b"name,sport,start_time,distance_km,duration,avg_hr,max_hr\n"
    b"Morning Ride,cycling,2026-06-23T13:00:00Z,41.094,02:22:00,159,178\n"
)

_CSV_RECORDS = (
    b"timestamp,heart_rate_bpm,speed_kmh,cadence_rpm,altitude_m,distance_m,lat,lon\n"
    b"2026-06-23T13:00:00Z,150,28.0,85,2300,0,19.5,-99.2\n"
    b"2026-06-23T13:00:01Z,151,29.0,86,2301,8,19.5001,-99.2001\n"
)


# ── Parser-level ──────────────────────────────────────────────────────────────

def test_gpx_parses_with_derived_distance_and_source():
    pa = parse_gpx(_gpx(creator="Garmin Connect"))
    assert pa.file_type == FileType.GPX
    assert pa.source_hint == Source.GARMIN_EXPORT
    assert pa.has_gps and pa.has_hr and pa.has_elevation
    assert pa.distance_m and pa.distance_m > 0          # derived via haversine
    assert pa.duration_s == 3600


def test_tcx_parses_power_and_hr():
    pa = parse_tcx(_TCX)
    assert pa.file_type == FileType.TCX
    assert pa.has_power and pa.avg_power == 190.0
    assert pa.has_hr and pa.distance_m == 5000.0
    assert pa.source_hint == Source.GARMIN_EXPORT


def test_csv_summary_and_records():
    s = parse_csv(_CSV_SUMMARY)
    assert s.distance_m == pytest.approx(41094.0)
    assert s.duration_s == 8520 and s.avg_hr == 159.0
    r = parse(_CSV_RECORDS, "rec.csv")
    assert r.has_hr and r.has_gps and r.n_records == 2


def test_unsupported_file_raises_parse_error():
    with pytest.raises(ParseError):
        parse(b"not an activity at all", "junk.txt")
    with pytest.raises(ParseError):
        parse(b"", "empty.gpx")


def test_fit_parser_unit(monkeypatch):
    """Validate the FIT path logic without a binary FIT, by faking fitparse + decode_fit."""
    import fitparse
    import decode_fit
    import ingest_parsers

    class _FakeFit:
        def get_messages(self, name):
            return []  # no file_id messages -> device/source stay unknown

    monkeypatch.setattr(fitparse, "FitFile", lambda *a, **k: _FakeFit())
    monkeypatch.setattr(decode_fit, "extract_session", lambda f: {
        "start_time": datetime(2026, 6, 23, 13, 0, tzinfo=timezone.utc),
        "total_distance": 41000.0, "total_timer_time": 3600, "total_elapsed_time": 3700,
        "avg_heart_rate": 150, "max_heart_rate": 172, "sport": "cycling",
        "total_ascent": 300, "avg_speed": 7.5, "total_calories": 600, "avg_power": 180,
    })
    monkeypatch.setattr(decode_fit, "extract_records", lambda f: [
        {"heart_rate_bpm": 150, "lat": 19.5, "lon": -99.2, "altitude_m": 2300, "cadence_rpm": 85, "distance_m": 10},
    ])
    monkeypatch.setattr(decode_fit, "extract_laps", lambda f: [])

    pa = ingest_parsers.parse_fit(b"FAKEFITBYTES")
    assert pa.file_type == FileType.FIT
    assert pa.distance_m == 41000.0 and pa.avg_hr == 150
    assert pa.has_hr and pa.has_gps and pa.has_power and pa.has_cadence


# ── Pipeline-level ────────────────────────────────────────────────────────────

def test_ingest_gpx_creates_endurance_timeline_event():
    repo = InMemoryTimelineRepository()
    res = ingest_file(_gpx(), "ride.gpx", "athlete1", repo)
    assert res.ok and res.event is not None
    ev = res.event
    assert ev.event_type == EventType.ENDURANCE_WORKOUT
    assert ev.sport_category == "cycling"
    assert ev.athlete_id == "athlete1"
    assert ev.availability_state == AvailabilityState.DERIVED
    assert ev.normalized_summary["distance_km"] is not None
    assert ev.raw_import_reference.startswith("sha256:")
    assert len(repo.list_events("athlete1")) == 1


def test_source_lineage_recorded():
    repo = InMemoryTimelineRepository()
    res = ingest_file(_gpx(creator="StravaGPX"), "ride.gpx", "athlete1", repo)
    lin = res.event.source
    assert isinstance(lin, SourceLineage)
    assert lin.source == Source.STRAVA_EXPORT          # detected from creator
    assert lin.file_type == FileType.GPX
    assert lin.file_hash and lin.parser == "gpx_parser"
    assert lin.original_filename == "ride.gpx"


def test_confidence_flags_when_hr_missing():
    repo = InMemoryTimelineRepository()
    res = ingest_file(_gpx(hr=False), "ride.gpx", "athlete1", repo)
    conf = res.event.confidence
    assert conf.signals[SIGNAL_HR] == AvailabilityState.MISSING
    assert conf.signals[SIGNAL_POWER] == AvailabilityState.MISSING
    assert "missing_sensor_data" in conf.data_flags
    # GPX distance is derived, not imported.
    assert "derived_metrics" in conf.data_flags


def test_confidence_higher_with_power_and_hr():
    repo = InMemoryTimelineRepository()
    with_pwr = ingest_file(_TCX, "a.tcx", "athlete1", repo).event
    no_hr = ingest_file(_gpx(hr=False), "b.gpx", "athlete1", repo).event
    assert with_pwr.confidence.score > no_hr.confidence.score


def test_duplicate_same_file_detected():
    repo = InMemoryTimelineRepository()
    f = _gpx()
    first = ingest_file(f, "ride.gpx", "athlete1", repo)
    second = ingest_file(f, "ride.gpx", "athlete1", repo)
    assert first.status.value == "imported"
    assert second.status.value == "duplicate"
    assert second.duplicate_of == first.event.event_id
    assert second.event.status == EventStatus.DUPLICATE


def test_cross_source_dedup_precedence_garmin_supersedes_strava():
    """Same activity from Strava then Garmin: Garmin (higher precedence) becomes primary,
    Strava is demoted to duplicate (lineage preserved, nothing deleted)."""
    repo = InMemoryTimelineRepository()
    strava = ingest_file(_gpx(creator="StravaGPX"), "s.gpx", "athlete1", repo)
    garmin = ingest_file(_gpx(creator="Garmin Connect"), "g.gpx", "athlete1", repo)

    assert garmin.event.status == EventStatus.ACTIVE
    assert garmin.event.source.source == Source.GARMIN_EXPORT
    assert strava.event.event_id in garmin.event.source.merged_from
    # The originally-stored Strava event is now demoted.
    demoted = repo.get_event(strava.event.event_id)
    assert demoted.status == EventStatus.DUPLICATE
    assert "superseded" in demoted.confidence.data_flags


def test_bad_file_fails_safely_without_corrupting_timeline():
    repo = InMemoryTimelineRepository()
    res = ingest_file(b"\x00\x01 broken", "broken.fit", "athlete1", repo)
    assert res.ok is False and res.status.value == "failed"
    assert res.event is None
    assert repo.list_events("athlete1") == []          # no partial/corrupt event
    logs = repo.list_import_logs("athlete1")
    assert logs and logs[-1]["status"] == "failed" and logs[-1]["error_message"]


def test_import_log_is_always_written():
    repo = InMemoryTimelineRepository()
    ingest_file(_gpx(), "ok.gpx", "athlete1", repo)
    ingest_file(b"junk", "junk.txt", "athlete1", repo)
    logs = repo.list_import_logs("athlete1")
    statuses = {l["status"] for l in logs}
    assert "imported" in statuses and "failed" in statuses
    assert all(l["file_hash"] for l in logs)           # fingerprint always recorded


# ── Timeline is multi-event / extensible ──────────────────────────────────────

def test_timeline_accepts_other_event_types_without_schema_change():
    """A strength_session event lives in the SAME timeline as endurance, using the
    common base schema + a free-form payload — no migration, no new table."""
    repo = InMemoryTimelineRepository()
    ingest_file(_gpx(), "ride.gpx", "athlete1", repo)   # endurance

    strength = TimelineEvent.create(
        athlete_id="athlete1",
        event_type=EventType.STRENGTH_SESSION,
        start_time=datetime(2026, 6, 24, 18, 0, tzinfo=timezone.utc),
        source=SourceLineage(source=Source.MANUAL_UPLOAD, upload_method="manual"),
        payload={"exercises": [{"name": "squat", "sets": 5, "reps": 5, "weight_kg": 100}]},
        sport_category="strength",
    )
    repo.save_event(strength)

    events = repo.list_events("athlete1")
    types = {e.event_type for e in events}
    assert EventType.ENDURANCE_WORKOUT in types
    assert EventType.STRENGTH_SESSION in types
    got = repo.get_event(strength.event_id)
    assert got.payload["exercises"][0]["name"] == "squat"


def test_event_serialization_roundtrip():
    repo = InMemoryTimelineRepository()
    ev = ingest_file(_TCX, "a.tcx", "athlete1", repo).event
    d = ev.to_dict()
    back = TimelineEvent.from_dict(d)
    assert back.to_dict() == d
    assert back.event_type == EventType.ENDURANCE_WORKOUT
    assert back.availability_state == ev.availability_state


def test_detect_sport_maps_known_and_unknown():
    assert detect_sport("Biking") == "cycling"
    assert detect_sport("running") == "running"
    assert detect_sport("kitesurfing") == "other"
    assert detect_sport(None) == "unknown"


# ── Deltas from the corrected task spec ───────────────────────────────────────

def test_unknown_source_falls_back_to_file_upload():
    """An unrecognized creator → detected_source UNKNOWN, resolved source = file_upload
    (we never invent a source)."""
    repo = InMemoryTimelineRepository()
    ev = ingest_file(_gpx(creator="SomeRandomApp 2.0"), "ride.gpx", "athlete1", repo).event
    assert ev.source.detected_source == Source.UNKNOWN
    assert ev.source.source == Source.FILE_UPLOAD


def test_fit_pipeline_import_endurance_event(monkeypatch):
    """Full pipeline FIT import (faking fitparse/decode_fit, no binary FIT needed)."""
    import fitparse
    import decode_fit

    class _Fld:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class _FakeFit:
        def get_messages(self, name):
            if name == "file_id":
                return [[_Fld("manufacturer", "garmin"), _Fld("garmin_product", "fr935")]]
            return []

    monkeypatch.setattr(fitparse, "FitFile", lambda *a, **k: _FakeFit())
    monkeypatch.setattr(decode_fit, "extract_session", lambda f: {
        "start_time": datetime(2026, 6, 23, 13, 0, tzinfo=timezone.utc),
        "total_distance": 41000.0, "total_timer_time": 3600, "total_elapsed_time": 3700,
        "avg_heart_rate": 150, "max_heart_rate": 172, "sport": "cycling",
        "total_ascent": 300, "avg_speed": 7.5, "total_calories": 600,
    })
    monkeypatch.setattr(decode_fit, "extract_records", lambda f: [
        {"heart_rate_bpm": 150, "lat": 19.5, "lon": -99.2, "altitude_m": 2300, "cadence_rpm": 85, "distance_m": 10},
    ])
    monkeypatch.setattr(decode_fit, "extract_laps", lambda f: [])

    repo = InMemoryTimelineRepository()
    res = ingest_file(b"FAKE-FIT-BYTES", "ride.fit", "athlete1", repo)
    assert res.ok and res.event.event_type == EventType.ENDURANCE_WORKOUT
    assert res.event.source.file_type == FileType.FIT
    assert res.event.source.source == Source.GARMIN_EXPORT      # from manufacturer
    assert res.event.payload["distance_m"] == 41000.0
    assert res.event.confidence.signals[SIGNAL_HR] == AvailabilityState.AVAILABLE


def test_unavailable_vs_missing_distinction():
    """A summary CSV cannot carry a GPS track → GPS is UNAVAILABLE (not MISSING);
    power is simply absent → MISSING. The two are distinguishable."""
    repo = InMemoryTimelineRepository()
    ev = ingest_file(_CSV_SUMMARY, "summary.csv", "athlete1", repo).event
    conf = ev.confidence
    assert conf.signals["gps"] == AvailabilityState.UNAVAILABLE
    assert "gps" in conf.unavailable_fields
    assert conf.signals[SIGNAL_POWER] == AvailabilityState.MISSING
    assert "power" not in conf.unavailable_fields
    # HR/distance/duration came from the file → imported.
    assert SIGNAL_HR in conf.imported_fields


def test_uncertain_duplicate_is_marked_not_deleted():
    """A probable (not exact) match is kept in the timeline and marked uncertain,
    never deleted aggressively."""
    repo = InMemoryTimelineRepository()
    csv1 = (b"name,sport,start_time,distance_km,duration,avg_hr\n"
            b"Ride,cycling,2026-06-23T13:00:00Z,40.0,02:00:00,150\n")
    csv2 = (b"name,sport,start_time,distance_km,duration,avg_hr\n"
            b"Ride,cycling,2026-06-23T13:02:00Z,41.0,02:05:00,151\n")
    first = ingest_file(csv1, "a.csv", "athlete1", repo)
    second = ingest_file(csv2, "b.csv", "athlete1", repo)
    assert first.status.value == "imported"
    assert second.status.value == "imported"                       # imported, but flagged
    assert second.event.status == EventStatus.DUPLICATE_UNCERTAIN
    assert "possible_duplicate" in second.event.confidence.data_flags
    assert second.duplicate_of == first.event.event_id
    assert len(repo.list_events("athlete1")) == 2                  # nothing deleted


def test_partial_data_never_becomes_exact_duplicate():
    """If duration/distance are missing, a close start time is reviewable, not exact."""
    repo = InMemoryTimelineRepository()
    csv1 = (b"name,sport,start_time,distance_km,duration,avg_hr\n"
            b"Ride,cycling,2026-06-23T13:00:00Z,,02:00:00,150\n")
    csv2 = (b"name,sport,start_time,distance_km,duration,avg_hr\n"
            b"Ride,cycling,2026-06-23T13:02:00Z,,02:00:00,151\n")

    first = ingest_file(csv1, "partial-a.csv", "athlete1", repo)
    second = ingest_file(csv2, "partial-b.csv", "athlete1", repo)

    assert first.status.value == "imported"
    assert second.status.value == "imported"
    assert second.event.status == EventStatus.DUPLICATE_UNCERTAIN
    assert second.duplicate_of == first.event.event_id
