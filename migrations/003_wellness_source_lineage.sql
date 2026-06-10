-- =============================================================
-- Migración 003: Source lineage en wellness (B2)
-- Railway PostgreSQL
-- v5.0 — 2026-06-08
--
-- El sueño de Garmin (sensor) y el check matutino manual
-- no son el mismo tipo de dato — hay que distinguirlos.
--
-- Confidence por tipo de fuente:
--   garmin_sleep_export    → 0.90  is_subjective = false
--   manual_morning_check   → 0.75  is_subjective = true  (FC reposo)
--   manual_fatigue         → 0.65  is_subjective = true  (fatiga subjetiva)
--   manual_sleep_hours     → 0.70  is_subjective = true  (horas estimadas)
-- =============================================================

ALTER TABLE wellness
    ADD COLUMN IF NOT EXISTS source            TEXT    DEFAULT 'manual_morning_check',
    ADD COLUMN IF NOT EXISTS source_record_id  TEXT,
    ADD COLUMN IF NOT EXISTS source_confidence FLOAT   DEFAULT 0.75,
    ADD COLUMN IF NOT EXISTS source_batch      TEXT,
    ADD COLUMN IF NOT EXISTS is_subjective     BOOLEAN DEFAULT true;

-- Los registros existentes son check matutino manual
UPDATE wellness
SET source            = 'manual_morning_check',
    source_confidence = 0.75,
    is_subjective     = true
WHERE source IS NULL;

CREATE INDEX IF NOT EXISTS idx_wellness_source
    ON wellness (source);

CREATE INDEX IF NOT EXISTS idx_wellness_is_subjective
    ON wellness (is_subjective);

COMMENT ON COLUMN wellness.source IS 'garmin_sleep_export | manual_morning_check | manual_fatigue | manual_sleep_hours';
COMMENT ON COLUMN wellness.source_confidence IS '0.90=Garmin sensor | 0.75=FC reposo manual | 0.70=horas sueño estimadas | 0.65=fatiga subjetiva';
COMMENT ON COLUMN wellness.is_subjective IS 'false=dato de sensor, true=percepción del atleta';
