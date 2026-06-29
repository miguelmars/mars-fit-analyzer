from datetime import datetime

from routers import activities


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.lower().split())
        assert "has_telemetry" in normalized
        assert "telemetry_points" in normalized
        assert "has_map" in normalized
        self.description = [
            ("session_id",),
            ("start_time",),
            ("sport",),
            ("distance_km",),
            ("duration_s",),
            ("avg_hr_bpm",),
            ("avg_speed_kmh",),
            ("ascent_m",),
            ("avg_cadence",),
            ("workout_name",),
            ("route_id",),
            ("has_telemetry",),
            ("telemetry_points",),
            ("has_map",),
        ]

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return FakeCursor(self.row)


def test_gpt_latest_session_returns_session_id_for_workout_analysis(monkeypatch):
    row = (
        "strava_123",
        datetime(2026, 6, 27, 8, 15),
        "cycling",
        42.1,
        5400,
        142,
        28.0,
        430,
        87,
        "Tempo Ride",
        "route_1",
        True,
        720,
        True,
    )
    monkeypatch.setattr(activities, "get_db", lambda: FakeConn(row))
    monkeypatch.setattr(activities, "_enrich_session_dict", lambda session: session)

    result = activities.gpt_latest_session()

    assert result["session_id"] == "strava_123"
    assert result["has_telemetry"] is True
    assert result["telemetry_points"] == 720
    assert result["has_map"] is True
    assert result["duration_hms"] == "01h 30m"
