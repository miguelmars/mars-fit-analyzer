-- =============================================================
-- Migración 002: Source lineage en clean_sessions (B1)
-- Railway PostgreSQL
-- v5.0 — 2026-06-08
--
-- Permite saber de dónde vino cada sesión, con qué confianza,
-- y si el archivo original sigue disponible.
-- Las sesiones existentes reciben valores por default — no se toca nada.
-- =============================================================

ALTER TABLE clean_sessions
    ADD COLUMN IF NOT EXISTS source               TEXT    DEFAULT 'garmin_fit',
    ADD COLUMN IF NOT EXISTS source_activity_id   TEXT,
    ADD COLUMN IF NOT EXISTS source_confidence    FLOAT   DEFAULT 1.00,
    ADD COLUMN IF NOT EXISTS source_batch         TEXT,
    ADD COLUMN IF NOT EXISTS original_file_available BOOLEAN DEFAULT true;

-- Todas las sesiones existentes = Garmin FIT con confianza 1.0
UPDATE clean_sessions
SET source            = 'garmin_fit',
    source_confidence = 1.00
WHERE source IS NULL;

-- Índice para filtrar por fuente (útil para dedup y análisis)
CREATE INDEX IF NOT EXISTS idx_clean_sessions_source
    ON clean_sessions (source);

CREATE INDEX IF NOT EXISTS idx_clean_sessions_source_activity_id
    ON clean_sessions (source_activity_id)
    WHERE source_activity_id IS NOT NULL;

COMMENT ON COLUMN clean_sessions.source IS 'garmin_fit | strava_stream | garmin_export | manual';
COMMENT ON COLUMN clean_sessions.source_activity_id IS 'ID externo: strava_id, garmin_activity_id, etc.';
COMMENT ON COLUMN clean_sessions.source_confidence IS '1.00=Garmin FIT | 0.85=Strava stream | 0.82=Strava listing | 0.70=manual';
COMMENT ON COLUMN clean_sessions.source_batch IS 'Identificador del batch de importación, e.g. garmin_export_2026_06_08';
COMMENT ON COLUMN clean_sessions.original_file_available IS 'true si el archivo .fit original está en el repo';
