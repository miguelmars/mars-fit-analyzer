from datetime import date

from routers import gpt_training_context as gtc


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        query = " ".join(sql.lower().split())
        params = params or ()
        self.result = None
        self.rows = []
        self.rowcount = 0

        if "from canonical_sessions where clean_session_id" in query:
            self.result = self.conn.session
        elif "from session_laps where clean_session_id" in query:
            self.rows = list(self.conn.laps)
        elif "from plan_sessions where matched_clean_session_id" in query:
            self.result = self.conn.planned
        elif "select count(*) from plan_sessions" in query:
            self.result = (self.conn.active_plan_sessions,)
        elif "from plan_sessions ps join training_plans" in query:
            self.result = self.conn.nearby_plan
        elif "from epoch_notifications" in query and "select id" in query:
            category, clean_session_id = params
            self.result = self.conn.notification_index.get((category, clean_session_id))
            if self.result is not None:
                self.result = (self.result,)
        elif "insert into epoch_notifications" in query:
            category = params[0]
            payload = params[6]
            clean_session_id = self.conn.extract_clean_session_id(payload)
            key = (category, clean_session_id)
            if key not in self.conn.notification_index:
                self.conn.next_notification_id += 1
                self.conn.notification_index[key] = self.conn.next_notification_id
                self.conn.notifications.append({
                    "id": self.conn.next_notification_id,
                    "category": category,
                    "clean_session_id": clean_session_id,
                })
            self.result = (self.conn.notification_index[key],)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, laps):
        self.session = (
            "Sweet Spot Progression",
            "Ride",
            date(2026, 6, 18),
            42.0,
            2400,
            154,
            174,
            420,
            88,
        )
        self.laps = laps
        self.planned = (
            "sweet_spot",
            "Sweet Spot Progression",
            {"power": "238-252 W"},
            "completed",
            date(2026, 6, 18),
        )
        self.active_plan_sessions = 12
        self.nearby_plan = None
        self.notifications = []
        self.notification_index = {}
        self.next_notification_id = 0
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def extract_clean_session_id(self, payload_json):
        import json

        payload = json.loads(payload_json)
        return payload.get("clean_session_id")


def _patch_common(monkeypatch, conn):
    monkeypatch.setattr(gtc, "get_db", lambda: conn)
    monkeypatch.setattr(gtc, "_ensure_training_tables", lambda _conn: None)
    monkeypatch.setattr(
        gtc,
        "_threshold_evidence_summary",
        lambda _conn, _sport: {"active_anchor": {"lt_bpm": 168}},
    )
    monkeypatch.setattr(
        gtc,
        "_active_plan_context",
        lambda _conn: {"current_phase": "base"},
    )
    monkeypatch.setattr(
        gtc,
        "_testing_calibration_recommendation",
        lambda _conn, _phase, _sport: {"recommended": False},
    )


def test_workout_analysis_creates_idempotent_session_notifications(monkeypatch):
    laps = [
        (0, 600, 10, 25, 140, 150, 88, 180, "z2", "steady"),
        (1, 720, 12, 30, 166, 172, 90, 250, "z4", "work"),
        (2, 360, 5, 20, 140, 150, 86, 120, "z2", "recovery"),
        (3, 720, 12, 30, 167, 174, 89, 252, "z4", "work"),
    ]
    conn = FakeConn(laps)
    _patch_common(monkeypatch, conn)

    first = gtc.workout_analysis("strava_test_1")
    second = gtc.workout_analysis("strava_test_1")

    assert first["workout_type"] == "intervals"
    assert first["plan_match"]["status"] == "matched"
    assert first["threshold_signal"]["status"] == "confirms"
    assert first["notifications_created"] == second["notifications_created"]
    assert sorted(n["category"] for n in conn.notifications) == [
        "activity_ready",
        "threshold_evidence_found",
        "workout_matched",
    ]


def test_workout_analysis_without_laps_does_not_invent_notifications(monkeypatch):
    conn = FakeConn([])
    _patch_common(monkeypatch, conn)

    result = gtc.workout_analysis("strava_test_empty")

    assert result["workout_type"] == "tempo"
    assert result["confidence_score"] == 0.25
    assert "Provisional read" in result["training_effect_summary"]
    assert result["threshold_signal"]["status"] == "insufficient_data"
    assert result["next_action"]["type"] == "transform_laps"
    assert result["notifications_created"] == []
    assert conn.notifications == []
