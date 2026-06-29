import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from strava.transform import _is_newer_than_garmin_cutoff
from tools.auto_activate_garmin import (
    BATCH_ID,
    EXPECTED_ACTIVITIES,
    EXPECTED_SHA256,
    STAGING_DIR,
    validate_bundled_snapshot,
)
from tools.garmin_export_import_staging import _default_batch_id, _load


ROOT = Path(__file__).resolve().parents[1]


def test_strava_before_garmin_cutoff_is_not_canonical():
    assert not _is_newer_than_garmin_cutoff(
        "2026-06-04T15:38:54Z",
        datetime(2026, 6, 4, 15, 38, 55, tzinfo=timezone.utc),
    )


def test_strava_at_garmin_cutoff_is_not_canonical():
    assert not _is_newer_than_garmin_cutoff(
        "2026-06-04T15:38:55+00:00",
        "2026-06-04T15:38:55Z",
    )


def test_strava_after_garmin_cutoff_is_canonical():
    assert _is_newer_than_garmin_cutoff(
        "2026-06-04T15:38:56Z",
        "2026-06-04T15:38:55+00:00",
    )


def test_timezone_offsets_are_normalized_before_cutoff_comparison():
    assert not _is_newer_than_garmin_cutoff(
        "2026-06-04T09:38:55-06:00",
        "2026-06-04T15:38:55Z",
    )
    assert _is_newer_than_garmin_cutoff(
        "2026-06-04T09:38:56-06:00",
        "2026-06-04T15:38:55Z",
    )


def test_strava_is_allowed_when_no_garmin_snapshot_exists():
    assert _is_newer_than_garmin_cutoff("2026-06-12T12:00:00Z", None)


def test_missing_strava_timestamp_is_rejected_when_cutoff_exists():
    assert not _is_newer_than_garmin_cutoff(None, "2026-06-04T15:38:55Z")


def test_garmin_batch_id_is_stable_for_same_snapshot(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    activities = staging / "garmin_activities_clean.json"
    activities.write_text('[{"source_activity_id":"1"}]', encoding="utf-8")

    first = _default_batch_id(staging)
    second = _default_batch_id(staging)

    assert first == second
    assert first.startswith("garmin_")


def test_compressed_staging_files_are_supported(tmp_path):
    path = tmp_path / "garmin_activities_clean.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump([{"source_activity_id": "1"}], handle)

    rows = _load(tmp_path / "garmin_activities_clean.json")

    assert rows == [{"source_activity_id": "1"}]


def test_bundled_latest_garmin_snapshot_is_exact_reference():
    validation = validate_bundled_snapshot()
    rows = _load(STAGING_DIR / "garmin_activities_clean.json")

    assert validation["batch_id"] == BATCH_ID == "garmin_2026_06_08"
    assert len(rows) == EXPECTED_ACTIVITIES == 2915
    assert max(row["start_time_utc"] for row in rows) == "2026-06-04T15:38:55+00:00"
    assert validation["checksums"] == EXPECTED_SHA256


def test_canonical_migration_is_non_destructive():
    sql = (ROOT / "migrations" / "006_garmin_canonical_cutoff.sql").read_text(
        encoding="utf-8"
    ).upper()

    assert "CREATE OR REPLACE VIEW CANONICAL_SESSIONS" in sql
    assert "IS_ACTIVE_SNAPSHOT IS TRUE" in sql
    assert "CS.START_TIME > C.GARMIN_CUTOFF_UTC" in sql
    assert "DELETE FROM" not in sql
    assert "TRUNCATE" not in sql
    assert "DROP TABLE" not in sql


def test_sync_tool_never_deletes_source_rows():
    source = (ROOT / "tools" / "sync_canonical_sessions.py").read_text(
        encoding="utf-8"
    ).upper()

    assert "ON CONFLICT (CLEAN_SESSION_ID) DO UPDATE" in source
    assert "WHERE IS_ACTIVE_SNAPSHOT IS TRUE" in source
    assert "DELETE FROM" not in source
    assert "TRUNCATE" not in source


def test_visible_product_queries_use_canonical_sessions():
    visible_modules = [
        "routers/gpt_analytics.py",
        "routers/plan_vivo.py",
        "routers/plan_builder.py",
        "routers/gpt_environment.py",
        "routers/activities.py",
    ]
    for relative_path in visible_modules:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "FROM clean_sessions" not in source
        assert "JOIN clean_sessions" not in source
        assert "canonical_sessions" in source


def test_railway_startup_schedules_automatic_garmin_activation():
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "async def _auto_activate_latest_garmin" in source
    assert "_asyncio.create_task(_auto_activate_latest_garmin())" in source
