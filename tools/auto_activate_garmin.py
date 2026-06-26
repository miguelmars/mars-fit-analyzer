"""Automatically activate the bundled June 2026 Garmin reference snapshot."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from db import _ensure_canonical_sessions_view, get_db
from tools.garmin_export_import_staging import _read_data_bytes, import_staging
from tools.sync_canonical_sessions import sync_active_garmin


logger = logging.getLogger("epoch.garmin_bootstrap")

ROOT = Path(__file__).resolve().parents[1]
STAGING_DIR = ROOT / "data" / "garmin_reference_2026_06_08"
BATCH_ID = "garmin_2026_06_08"
EXPECTED_ACTIVITIES = 2915
EXPECTED_CUTOFF_UTC = "2026-06-04 15:38:55+00"
EXPECTED_SHA256 = {
    "garmin_activities_clean.json": "3501ab5cff18eddfcd12bb676e1600a198d07534755121f7ba5a089c2b149266",
    "garmin_gear_clean.json": "2e828a1f54e5b8572cfc3fb624b627a68f3697a3f8bb7b8579044a3d554319d2",
    "garmin_sleep_clean.json": "b2307c9097ef78666dc14c0e1bc643ff274ce560365eb08a854453ff4e7ab12a",
}


def validate_bundled_snapshot() -> dict[str, Any]:
    checksums = {}
    for filename, expected in EXPECTED_SHA256.items():
        path = STAGING_DIR / filename
        actual = hashlib.sha256(_read_data_bytes(path)).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"Bundled Garmin snapshot checksum mismatch for {filename}"
            )
        checksums[filename] = actual
    return {
        "staging_dir": str(STAGING_DIR),
        "batch_id": BATCH_ID,
        "checksums": checksums,
    }


def canonical_status(conn=None) -> dict[str, Any]:
    connection = conn or get_db()
    if not connection:
        raise RuntimeError("Database unavailable")
    _ensure_canonical_sessions_view(connection)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), MAX(start_time_utc)::text
            FROM garmin_export_activities
            WHERE is_active_snapshot IS TRUE
              AND source_batch = %s
            """,
            (BATCH_ID,),
        )
        active_count, cutoff = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM clean_sessions")
        raw_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM canonical_sessions")
        canonical_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT source, COUNT(*)
            FROM canonical_sessions
            GROUP BY source
            ORDER BY source
            """
        )
        by_source = {source: count for source, count in cur.fetchall()}
    return {
        "batch_id": BATCH_ID,
        "active_garmin_activities": active_count,
        "garmin_cutoff_utc": cutoff,
        "raw_clean_sessions": raw_count,
        "canonical_sessions": canonical_count,
        "canonical_by_source": by_source,
        "ready": (
            active_count == EXPECTED_ACTIVITIES
            and cutoff == EXPECTED_CUTOFF_UTC
        ),
    }


def activate_bundled_snapshot() -> dict[str, Any]:
    validation = validate_bundled_snapshot()
    conn = get_db()
    if not conn:
        raise RuntimeError("Database unavailable")

    before = canonical_status(conn)
    if before["ready"]:
        logger.info(
            "Garmin reference already active: %s activities, cutoff %s",
            before["active_garmin_activities"],
            before["garmin_cutoff_utc"],
        )
        return {
            "status": "already_active",
            "validation": validation,
            "canonical": before,
        }

    imported = import_staging(
        STAGING_DIR,
        dry_run=False,
        activate_snapshot=True,
        batch_id=BATCH_ID,
    )
    synced = sync_active_garmin(execute=True)
    after = canonical_status(conn)
    if not after["ready"]:
        raise RuntimeError(
            "Garmin activation finished but the canonical status is incomplete"
        )

    logger.info(
        "Garmin reference activated: %s activities, %s canonical sessions",
        after["active_garmin_activities"],
        after["canonical_sessions"],
    )
    return {
        "status": "activated",
        "validation": validation,
        "imported": imported,
        "synced": synced,
        "canonical": after,
    }
