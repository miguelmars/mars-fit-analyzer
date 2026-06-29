-- 007_timeline_events.sql
-- EPOCH P0 — Canonical Athlete Timeline storage (ADDITIVE).
-- Creates timeline_events + timeline_import_log. Does NOT alter clean_sessions/sessions.
-- Safe to run repeatedly (IF NOT EXISTS). Also created at runtime by
-- timeline_store.ensure_schema(); this file is for explicit/manual migration.

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
