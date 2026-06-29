"""
timeline_store.py
================
EPOCH — Canonical Athlete Timeline storage (P0).

Defines the storage **port** (`TimelineRepository`) plus two implementations:
  * `InMemoryTimelineRepository` — the tested reference; no DB required.
  * `PostgresTimelineRepository` — additive `timeline_events` + `timeline_import_log`
    tables (JSONB). It does **NOT** touch the existing `clean_sessions`/`sessions`.

clean-architecture: the pipeline depends on the *port*, never on a concrete DB. Swap
the implementation (memory ↔ Postgres ↔ future) without changing ingestion logic.

The Postgres repo imports psycopg2 lazily, so importing this module never requires a DB.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from timeline_model import EventStatus, EventType, TimelineEvent


@runtime_checkable
class TimelineRepository(Protocol):
    """Port: what the ingestion pipeline needs from storage."""

    def save_event(self, event: TimelineEvent) -> None: ...
    def get_event(self, event_id: str) -> Optional[TimelineEvent]: ...
    def list_events(self, athlete_id: Optional[str] = None) -> List[TimelineEvent]: ...
    def find_by_file_hash(self, athlete_id: str, file_hash: str) -> Optional[TimelineEvent]: ...
    def find_dedup_candidates(self, athlete_id: str, start_time: Any, window_seconds: int) -> List[TimelineEvent]: ...
    def save_import_log(self, log: Any) -> None: ...
    def list_import_logs(self, athlete_id: Optional[str] = None) -> List[Dict[str, Any]]: ...


# ─────────────────────────────────────────────────────────────────────────────
# In-memory reference implementation (used by tests; great for a dry-run import)
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryTimelineRepository:
    def __init__(self) -> None:
        self._events: Dict[str, TimelineEvent] = {}
        self._import_logs: List[Dict[str, Any]] = []

    def save_event(self, event: TimelineEvent) -> None:
        self._events[event.event_id] = event

    def get_event(self, event_id: str) -> Optional[TimelineEvent]:
        return self._events.get(event_id)

    def list_events(self, athlete_id: Optional[str] = None) -> List[TimelineEvent]:
        evs = list(self._events.values())
        if athlete_id is not None:
            evs = [e for e in evs if e.athlete_id == athlete_id]
        return sorted(evs, key=lambda e: (e.start_time is None, e.start_time or 0) if e.start_time else (True, 0))

    def find_by_file_hash(self, athlete_id: str, file_hash: str) -> Optional[TimelineEvent]:
        if not file_hash:
            return None
        for e in self._events.values():
            if (e.athlete_id == athlete_id and e.source.file_hash == file_hash
                    and e.status != EventStatus.FAILED):
                return e
        return None

    def find_dedup_candidates(self, athlete_id: str, start_time: Any, window_seconds: int) -> List[TimelineEvent]:
        out: List[TimelineEvent] = []
        for e in self._events.values():
            if (
                e.athlete_id != athlete_id
                or e.status == EventStatus.FAILED
                or e.event_type != EventType.ENDURANCE_WORKOUT
            ):
                continue
            if not e.start_time or not start_time:
                continue
            if abs((e.start_time - start_time).total_seconds()) <= window_seconds:
                out.append(e)
        return out

    def save_import_log(self, log: Any) -> None:
        self._import_logs.append(log.to_dict() if hasattr(log, "to_dict") else dict(log))

    def list_import_logs(self, athlete_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if athlete_id is None:
            return list(self._import_logs)
        return [l for l in self._import_logs if l.get("athlete_id") == athlete_id]


# ─────────────────────────────────────────────────────────────────────────────
# Postgres schema (additive — never alters clean_sessions/sessions)
# ─────────────────────────────────────────────────────────────────────────────

TIMELINE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS timeline_events (
    event_id            TEXT PRIMARY KEY,
    athlete_id          TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    start_time          TIMESTAMPTZ,
    end_time            TIMESTAMPTZ,
    duration_sec        INT,
    timezone            TEXT,
    sport_category      TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    file_hash           TEXT,
    source              TEXT,
    source_lineage      JSONB,
    availability_state  TEXT,
    confidence          JSONB,
    confidence_score    NUMERIC,
    confidence_level    TEXT,
    normalized_summary  JSONB,
    payload             JSONB,
    linked_event_ids    JSONB,
    raw_import_reference TEXT,
    notes               TEXT,
    model_version       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_timeline_events_athlete_start ON timeline_events(athlete_id, start_time);
CREATE INDEX IF NOT EXISTS idx_timeline_events_type ON timeline_events(event_type);
CREATE INDEX IF NOT EXISTS idx_timeline_events_status ON timeline_events(status);
CREATE INDEX IF NOT EXISTS idx_timeline_events_file_hash ON timeline_events(file_hash);
ALTER TABLE timeline_events ADD COLUMN IF NOT EXISTS availability_state TEXT;

CREATE TABLE IF NOT EXISTS timeline_import_log (
    import_id          TEXT PRIMARY KEY,
    athlete_id         TEXT NOT NULL,
    status             TEXT NOT NULL,
    received_at        TIMESTAMPTZ DEFAULT NOW(),
    original_filename  TEXT,
    file_type          TEXT,
    file_hash          TEXT,
    source             TEXT,
    parser             TEXT,
    parser_version     TEXT,
    event_id           TEXT,
    duplicate_of       TEXT,
    error_message      TEXT,
    warnings           JSONB
);
CREATE INDEX IF NOT EXISTS idx_timeline_import_log_athlete ON timeline_import_log(athlete_id, received_at);
CREATE INDEX IF NOT EXISTS idx_timeline_import_log_status ON timeline_import_log(status);
"""


def ensure_schema(conn: Any) -> None:
    """Create the additive timeline tables if they do not exist. `conn` = psycopg2 conn."""
    with conn.cursor() as cur:
        cur.execute(TIMELINE_SCHEMA_SQL)
    conn.commit()


class PostgresTimelineRepository:
    """psycopg2-backed repository. Not exercised by the P0 tests (no live DB here);
    the in-memory repo is the tested reference. Wire this in Phase 3 to persist for real.
    """

    def __init__(self, conn: Any, ensure: bool = False) -> None:
        self._conn = conn
        if ensure:
            ensure_schema(conn)

    @staticmethod
    def _json(obj: Any):
        from psycopg2.extras import Json  # lazy import
        return Json(obj)

    def save_event(self, event: TimelineEvent) -> None:
        d = event.to_dict()
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO timeline_events (
                    event_id, athlete_id, event_type, start_time, end_time, duration_sec,
                    timezone, sport_category, status, file_hash, source, source_lineage,
                    availability_state, confidence, confidence_score, confidence_level, normalized_summary,
                    payload, linked_event_ids, raw_import_reference, notes, model_version,
                    created_at, updated_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT (event_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    end_time = EXCLUDED.end_time,
                    duration_sec = EXCLUDED.duration_sec,
                    sport_category = EXCLUDED.sport_category,
                    source = EXCLUDED.source,
                    source_lineage = EXCLUDED.source_lineage,
                    availability_state = EXCLUDED.availability_state,
                    confidence = EXCLUDED.confidence,
                    confidence_score = EXCLUDED.confidence_score,
                    confidence_level = EXCLUDED.confidence_level,
                    normalized_summary = EXCLUDED.normalized_summary,
                    payload = EXCLUDED.payload,
                    linked_event_ids = EXCLUDED.linked_event_ids,
                    notes = EXCLUDED.notes,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    d["event_id"], d["athlete_id"], d["event_type"], d["start_time"],
                    d["end_time"], d["duration_sec"], d["timezone"], d["sport_category"],
                    d["status"], d["source"].get("file_hash"), d["source"].get("source"),
                    self._json(d["source"]), d.get("availability_state"), self._json(d["confidence"]),
                    d["confidence"].get("score"), d["confidence"].get("level"),
                    self._json(d["normalized_summary"]), self._json(d["payload"]),
                    self._json(d["linked_event_ids"]), d["raw_import_reference"],
                    d["notes"], d.get("model_version"), d["created_at"], d["updated_at"],
                ),
            )
        self._conn.commit()

    def _row_to_event(self, row: Dict[str, Any]) -> TimelineEvent:
        return TimelineEvent.from_dict({
            "event_id": row["event_id"], "athlete_id": row["athlete_id"],
            "event_type": row["event_type"], "start_time": row["start_time"],
            "end_time": row["end_time"], "duration_sec": row["duration_sec"],
            "timezone": row["timezone"], "sport_category": row["sport_category"],
            "status": row["status"], "source": row["source_lineage"],
            "availability_state": row.get("availability_state"),
            "confidence": row["confidence"], "normalized_summary": row["normalized_summary"],
            "payload": row["payload"], "linked_event_ids": row["linked_event_ids"],
            "raw_import_reference": row["raw_import_reference"], "notes": row["notes"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })

    def _query(self, sql: str, params: tuple) -> List[Dict[str, Any]]:
        from psycopg2.extras import RealDictCursor  # lazy import
        with self._conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_event(self, event_id: str) -> Optional[TimelineEvent]:
        rows = self._query("SELECT * FROM timeline_events WHERE event_id = %s", (event_id,))
        return self._row_to_event(rows[0]) if rows else None

    def list_events(self, athlete_id: Optional[str] = None) -> List[TimelineEvent]:
        if athlete_id is None:
            rows = self._query("SELECT * FROM timeline_events ORDER BY start_time", ())
        else:
            rows = self._query(
                "SELECT * FROM timeline_events WHERE athlete_id = %s ORDER BY start_time",
                (athlete_id,))
        return [self._row_to_event(r) for r in rows]

    def find_by_file_hash(self, athlete_id: str, file_hash: str) -> Optional[TimelineEvent]:
        if not file_hash:
            return None
        rows = self._query(
            "SELECT * FROM timeline_events WHERE athlete_id = %s AND file_hash = %s "
            "AND status <> 'failed' LIMIT 1",
            (athlete_id, file_hash))
        return self._row_to_event(rows[0]) if rows else None

    def find_dedup_candidates(self, athlete_id: str, start_time: Any, window_seconds: int) -> List[TimelineEvent]:
        rows = self._query(
            "SELECT * FROM timeline_events WHERE athlete_id = %s AND status <> 'failed' "
            "AND event_type = 'endurance_workout' "
            "AND start_time BETWEEN %s AND %s",
            (athlete_id,
             start_time - _td(window_seconds),
             start_time + _td(window_seconds)))
        return [self._row_to_event(r) for r in rows]

    def save_import_log(self, log: Any) -> None:
        d = log.to_dict() if hasattr(log, "to_dict") else dict(log)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO timeline_import_log (
                    import_id, athlete_id, status, received_at, original_filename,
                    file_type, file_hash, source, parser, parser_version, event_id,
                    duplicate_of, error_message, warnings
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (import_id) DO UPDATE SET
                    status = EXCLUDED.status, event_id = EXCLUDED.event_id,
                    duplicate_of = EXCLUDED.duplicate_of, error_message = EXCLUDED.error_message,
                    warnings = EXCLUDED.warnings
                """,
                (
                    d["import_id"], d["athlete_id"], d["status"], d["received_at"],
                    d["original_filename"], d["file_type"], d["file_hash"], d["source"],
                    d["parser"], d["parser_version"], d["event_id"], d["duplicate_of"],
                    d["error_message"], self._json(d["warnings"]),
                ),
            )
        self._conn.commit()

    def list_import_logs(self, athlete_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if athlete_id is None:
            return self._query("SELECT * FROM timeline_import_log ORDER BY received_at DESC", ())
        return self._query(
            "SELECT * FROM timeline_import_log WHERE athlete_id = %s ORDER BY received_at DESC",
            (athlete_id,))


def _td(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)


__all__ = [
    "TimelineRepository", "InMemoryTimelineRepository", "PostgresTimelineRepository",
    "TIMELINE_SCHEMA_SQL", "ensure_schema",
]
