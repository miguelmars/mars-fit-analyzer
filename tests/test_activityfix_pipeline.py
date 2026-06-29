from datetime import date

from strava import rename


class PlanMatchCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        pass

    def fetchall(self):
        return self.rows


class PlanMatchConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return PlanMatchCursor(self.rows)


class MarkPlanCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        query = " ".join(sql.lower().split())
        params = params or ()
        if "from clean_sessions" in query:
            self.result = (self.conn.clean_session_id,)
        elif "update plan_sessions" in query:
            self.conn.plan_update_params = params
            self.rowcount = 1

    def fetchone(self):
        return self.result


class MarkPlanConn:
    def __init__(self):
        self.clean_session_id = "strava_123"
        self.plan_update_params = None

    def cursor(self):
        return MarkPlanCursor(self)


def test_activityfix_plan_match_uses_plan_intent_for_generic_strava_name():
    conn = PlanMatchConn([
        (101, "Sweet Spot Progression", "sweet_spot", "Garmin TT", date(2026, 6, 18)),
    ])
    row = {
        "strava_id": "123",
        "name": "Morning Ride",
        "started_at_local": "2026-06-18T07:00:00",
        "moving_time_s": 3600,
        "distance_m": 42000,
        "avg_hr": 152,
    }

    candidate = rename._find_plan_match(conn, row)

    assert rename.is_generic_strava_name(row["name"])
    assert candidate["source"] == "epoch_plan"
    assert candidate["name"] == "Sweet Spot Progression"
    assert candidate["plan_session_id"] == 101
    assert candidate["confidence"] >= 82


def test_activityfix_marks_plan_session_idempotently_when_match_is_applied():
    conn = MarkPlanConn()
    row = {"strava_id": "123"}
    candidate = {
        "plan_session_id": 101,
        "signed_day_delta": 1,
    }

    result = rename._mark_plan_from_activityfix(conn, row, candidate)

    assert result == {
        "plan_session_id": 101,
        "clean_session_id": "strava_123",
        "status": "moved",
        "changed": True,
    }
    assert conn.plan_update_params[0] == "strava_123"
    assert conn.plan_update_params[2] == "moved"
