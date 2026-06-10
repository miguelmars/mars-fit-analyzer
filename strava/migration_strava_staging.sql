-- ============================================================
-- Bitácora Mars — Strava Staging Tables
-- v4.8 — Corrección crítica: sin colisión con tablas Mars
-- Ejecutar en Supabase SQL Editor
-- ============================================================
--
-- IMPORTANTE: Este archivo reemplaza supabase_migration.sql original.
-- El original creaba una tabla 'sessions' que colisionaba con la
-- tabla sessions canónica de Mars. Este usa nombres strava_*_raw.
--
-- Flujo:
--   strava_activities_raw → transform_to_mars_canonical() → clean_sessions (existente)
--   strava_streams_raw    → session_records (existente)
--   strava_laps_raw       → referencia analítica (sin tabla Mars equivalente)
-- ============================================================

-- ── Tokens OAuth de Strava ──────────────────────────────
CREATE TABLE IF NOT EXISTS strava_tokens (
    athlete_id    BIGINT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    BIGINT NOT NULL,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- ── Actividades raw de Strava (staging — no es clean_sessions) ──
-- Guarda el JSON de Strava tal como llega, antes de transformar al
-- modelo canónico Mars. Nunca se borra — es el historial de ingestión.
CREATE TABLE IF NOT EXISTS strava_activities_raw (
    id               BIGSERIAL PRIMARY KEY,
    strava_id        BIGINT UNIQUE NOT NULL,
    athlete_id       BIGINT NOT NULL,         -- owner_id del webhook
    name             TEXT,
    sport_type       TEXT NOT NULL,           -- Ride, Run, VirtualRide, etc.
    started_at       TIMESTAMPTZ NOT NULL,
    started_at_local TIMESTAMPTZ,
    timezone         TEXT,
    distance_m       FLOAT DEFAULT 0,
    moving_time_s    INT DEFAULT 0,
    elapsed_time_s   INT DEFAULT 0,
    elevation_gain_m FLOAT DEFAULT 0,
    elev_high_m      FLOAT,
    elev_low_m       FLOAT,
    avg_speed_ms     FLOAT DEFAULT 0,
    max_speed_ms     FLOAT DEFAULT 0,
    avg_hr           FLOAT,
    max_hr           FLOAT,
    has_hr           BOOLEAN DEFAULT FALSE,
    avg_cadence      FLOAT,
    avg_watts        FLOAT,
    max_watts        INT,
    calories         FLOAT,
    device           TEXT,
    gear_id          TEXT,
    start_lat        FLOAT,
    start_lng        FLOAT,
    suffer_score     FLOAT,
    raw_json         JSONB,                   -- JSON completo de Strava, sin procesar
    source_confidence FLOAT DEFAULT 0.85,     -- Strava summary = 0.85 (ADR-012)
    transformed_at   TIMESTAMPTZ,            -- cuándo se pasó al modelo canónico
    transform_status TEXT DEFAULT 'pending', -- pending | done | error | skipped
    transform_error  TEXT,
    synced_at        TIMESTAMPTZ DEFAULT now(),
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strava_raw_athlete   ON strava_activities_raw(athlete_id);
CREATE INDEX IF NOT EXISTS idx_strava_raw_started   ON strava_activities_raw(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_strava_raw_status    ON strava_activities_raw(transform_status);
CREATE INDEX IF NOT EXISTS idx_strava_raw_sport     ON strava_activities_raw(sport_type);

-- ── Streams raw de Strava (staging) ─────────────────────
-- Arrays de telemetría segundo a segundo — pesados, opcionales.
-- Solo se jalan con with_streams=true en el backfill.
CREATE TABLE IF NOT EXISTS strava_streams_raw (
    id               BIGSERIAL PRIMARY KEY,
    strava_activity_id BIGINT REFERENCES strava_activities_raw(strava_id) ON DELETE CASCADE,
    stream_time      INT[],
    stream_distance  FLOAT[],
    stream_heartrate INT[],
    stream_cadence   INT[],
    stream_altitude  FLOAT[],
    stream_velocity  FLOAT[],
    stream_grade     FLOAT[],
    stream_latlng    JSONB,              -- [[lat, lng], ...]
    source_confidence FLOAT DEFAULT 0.85,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_strava_streams_activity ON strava_streams_raw(strava_activity_id);

-- ── Laps raw de Strava (staging) ─────────────────────────
CREATE TABLE IF NOT EXISTS strava_laps_raw (
    id               BIGSERIAL PRIMARY KEY,
    strava_activity_id BIGINT REFERENCES strava_activities_raw(strava_id) ON DELETE CASCADE,
    lap_index        INT NOT NULL,
    name             TEXT,
    distance_m       FLOAT,
    moving_time_s    INT,
    elapsed_time_s   INT,
    avg_speed_ms     FLOAT,
    max_speed_ms     FLOAT,
    avg_hr           FLOAT,
    max_hr           FLOAT,
    avg_cadence      FLOAT,
    avg_watts        FLOAT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strava_laps_activity ON strava_laps_raw(strava_activity_id);

-- ── Cursor de sincronización (backfill reanudable) ───────
-- Nunca correr backfill desde cero si se interrumpe.
-- El cursor permite reanudar desde last_activity_id.
CREATE TABLE IF NOT EXISTS strava_sync_state (
    id               INT PRIMARY KEY DEFAULT 1,   -- fila única (single athlete)
    last_activity_id BIGINT,                       -- Strava ID del último procesado
    last_synced_at   TIMESTAMPTZ,
    total_synced     INT DEFAULT 0,
    with_streams     BOOLEAN DEFAULT FALSE,
    status           TEXT DEFAULT 'idle',          -- idle | running | paused | completed | error
    error_message    TEXT,
    updated_at       TIMESTAMPTZ DEFAULT now()
);

-- Fila inicial del cursor
INSERT INTO strava_sync_state (id, status) VALUES (1, 'idle')
ON CONFLICT (id) DO NOTHING;

-- ── RLS ──────────────────────────────────────────────────
ALTER TABLE strava_tokens          ENABLE ROW LEVEL SECURITY;
ALTER TABLE strava_activities_raw  ENABLE ROW LEVEL SECURITY;
ALTER TABLE strava_streams_raw     ENABLE ROW LEVEL SECURITY;
ALTER TABLE strava_laps_raw        ENABLE ROW LEVEL SECURITY;
ALTER TABLE strava_sync_state      ENABLE ROW LEVEL SECURITY;

-- Service role key tiene acceso total (FastAPI usa service_role, nunca anon)
DO $$ BEGIN
  CREATE POLICY "service_full_access" ON strava_tokens
      FOR ALL USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE POLICY "service_full_access" ON strava_activities_raw
      FOR ALL USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE POLICY "service_full_access" ON strava_streams_raw
      FOR ALL USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE POLICY "service_full_access" ON strava_laps_raw
      FOR ALL USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE POLICY "service_full_access" ON strava_sync_state
      FOR ALL USING (true) WITH CHECK (true);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
