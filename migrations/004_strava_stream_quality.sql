-- =============================================================
-- Migración 004: Stream quality + completeness en strava_activities_raw (B3)
-- Supabase (PostgreSQL)
-- v5.0 — 2026-06-08
--
-- has_hr = true es distinto a hr_coverage = 0.93.
-- La cobertura determina si la actividad sirve para análisis fino.
--
-- Reglas:
--   hr_coverage  < 0.80 → no sirve para análisis de zonas
--   gps_coverage < 0.90 → no sirve para análisis de ruta
--   stream_quality = promedio de coverages disponibles
-- =============================================================

ALTER TABLE strava_activities_raw
    ADD COLUMN IF NOT EXISTS has_gps          BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_cadence      BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_power        BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_temp         BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS has_moving       BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS gps_coverage     FLOAT   DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS hr_coverage      FLOAT   DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS cadence_coverage FLOAT   DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS power_coverage   FLOAT   DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS stream_quality   FLOAT   DEFAULT 0.0;

-- Nota: has_hr ya existe en la tabla (creada en v4.8)
-- Solo se agregan las columnas nuevas con IF NOT EXISTS

-- Índices para análisis de calidad
CREATE INDEX IF NOT EXISTS idx_strava_raw_stream_quality
    ON strava_activities_raw (stream_quality DESC)
    WHERE stream_quality > 0;

CREATE INDEX IF NOT EXISTS idx_strava_raw_has_power
    ON strava_activities_raw (strava_id)
    WHERE has_power = true;

CREATE INDEX IF NOT EXISTS idx_strava_raw_has_gps
    ON strava_activities_raw (strava_id)
    WHERE has_gps = true;

COMMENT ON COLUMN strava_activities_raw.gps_coverage     IS 'Fracción de la actividad con coordenadas GPS válidas (0.0–1.0)';
COMMENT ON COLUMN strava_activities_raw.hr_coverage      IS 'Fracción de la actividad con HR válido (0.0–1.0). <0.80 = no apto para zonas';
COMMENT ON COLUMN strava_activities_raw.cadence_coverage IS 'Fracción de la actividad con cadencia válida (0.0–1.0)';
COMMENT ON COLUMN strava_activities_raw.power_coverage   IS 'Fracción de la actividad con potencia válida (0.0–1.0)';
COMMENT ON COLUMN strava_activities_raw.stream_quality   IS 'Promedio de todas las coverages disponibles. Métrica global de calidad del stream.';
