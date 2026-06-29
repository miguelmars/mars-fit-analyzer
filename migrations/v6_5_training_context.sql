-- v6.5 Training Context — migración ADITIVA (Railway PostgreSQL)
-- Seguro: solo CREATE IF NOT EXISTS. No toca datos existentes.
-- Las tablas también se crean lazy desde routers/gpt_training_context.py.

-- Laps/intervalos por sesión. Fuente: strava_laps_raw (Supabase) o FIT upload.
CREATE TABLE IF NOT EXISTS session_laps (
    lap_id           BIGSERIAL PRIMARY KEY,
    clean_session_id TEXT NOT NULL REFERENCES clean_sessions(clean_session_id) ON DELETE CASCADE,
    lap_index        INT NOT NULL,
    name             TEXT,
    duration_s       INT,
    moving_s         INT,
    distance_km      NUMERIC(9,3),
    avg_speed_kmh    NUMERIC(6,2),
    max_speed_kmh    NUMERIC(6,2),
    avg_hr_bpm       NUMERIC(5,1),
    max_hr_bpm       INT,
    avg_cadence      NUMERIC(5,1),
    avg_watts        NUMERIC(7,1),
    zone_label       TEXT,            -- z1/trans/z2/z3/z4/z5 según FC promedio
    lap_type         TEXT,            -- work / recovery / steady (heurística)
    source           TEXT DEFAULT 'strava',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (clean_session_id, lap_index)
);
CREATE INDEX IF NOT EXISTS idx_session_laps_session ON session_laps(clean_session_id);

-- Plan de entrenamiento activo (Garmin Coach hoy es texto; esto lo vuelve dato).
CREATE TABLE IF NOT EXISTS training_plans (
    plan_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source      TEXT DEFAULT 'manual',    -- manual | garmin_coach
    goal_id     INT,                      -- mars_goals.id (sin FK: tablas en mismo DB pero ciclo de vida distinto)
    start_date  DATE,
    end_date    DATE,
    total_weeks INT,
    status      TEXT DEFAULT 'active',    -- active | completed | abandoned
    meta        JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Sesiones planificadas por semana; se enlazan a clean_sessions al cumplirse.
CREATE TABLE IF NOT EXISTS plan_sessions (
    id              BIGSERIAL PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES training_plans(plan_id) ON DELETE CASCADE,
    week_number     INT NOT NULL,
    planned_date    DATE,
    session_type    TEXT,                 -- z2_ride | tempo | intervals | long_ride | rest | strength
    description     TEXT,
    target          JSONB,                -- {duration_min, km, hr_zone, intervals:[{reps,work_s,rest_s,zone}]}
    matched_clean_session_id TEXT,        -- clean_sessions.clean_session_id al completar
    status          TEXT DEFAULT 'planned', -- planned | completed | skipped
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plan_sessions_plan_week ON plan_sessions(plan_id, week_number);
