-- =============================================================
-- Migración 005: dedup_approval_log + canonical backup (C3 + C4)
-- Railway PostgreSQL
-- v5.0 — 2026-06-08
--
-- Tabla de auditoría: quién aprobó qué muestra de dedup y cuándo.
-- El botón de transformar Strava → Mars NO aparece sin una fila aquí.
-- =============================================================

-- ── Tabla de aprobación de muestras de dedup ─────────────
CREATE TABLE IF NOT EXISTS dedup_approval_log (
    id              SERIAL PRIMARY KEY,
    diagnosis_id    TEXT NOT NULL,
    sample_set_id   TEXT,
    approved_by     TEXT NOT NULL DEFAULT 'mars',
    approved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT,
    -- Resumen de lo que se aprobó
    total_strava    INT,
    exact_matches   INT,
    probable_matches INT,
    new_sessions    INT,
    conflicts       INT,
    -- Estado de la transformación
    transform_executed  BOOLEAN DEFAULT false,
    transform_at        TIMESTAMPTZ,
    transform_results   JSONB
);

CREATE INDEX IF NOT EXISTS idx_dedup_approval_diagnosis_id
    ON dedup_approval_log (diagnosis_id);

CREATE INDEX IF NOT EXISTS idx_dedup_approval_approved_at
    ON dedup_approval_log (approved_at DESC);

COMMENT ON TABLE dedup_approval_log IS 'Auditoría de aprobaciones de dedup. Una fila = una sesión de revisión aprobada por el atleta.';

-- ── Backup canónico antes de transformar Strava (C4) ─────
-- EJECUTAR JUSTO ANTES de POST /api/strava/transform (no antes)
-- Descomentar cuando sea el momento:

-- CREATE TABLE IF NOT EXISTS canonical_backup_pre_strava_2026_06 AS
--     SELECT * FROM clean_sessions;

-- COMMENT ON TABLE canonical_backup_pre_strava_2026_06 IS
--     'Backup de clean_sessions antes de transformar Strava. Creado 2026-06-08. Eliminar después de 30 días si todo ok.';
