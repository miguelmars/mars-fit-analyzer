-- =============================================================
-- Migración 001: Agregar columnas watts, temp, moving a strava_streams_raw
-- Supabase (PostgreSQL)
-- v5.0 — 2026-06-08
--
-- Contexto: STRAVA_STREAM_KEYS se expandió de 8 a 11 streams.
-- Las tres columnas nuevas son opcionales (NULL si el sensor no existe).
--
--   stream_watts  : Potencia en vatios [integer[]] — requiere potenciómetro
--   stream_temp   : Temperatura ambiente en °C [smallint[]] — requiere sensor
--   stream_moving : Booleano de movimiento [boolean[]] — siempre disponible
--
-- Ejecutar en: Supabase Dashboard → SQL Editor
-- =============================================================

-- Agregar columnas solo si no existen (idempotente)
ALTER TABLE strava_streams_raw
    ADD COLUMN IF NOT EXISTS stream_watts   integer[],
    ADD COLUMN IF NOT EXISTS stream_temp    smallint[],
    ADD COLUMN IF NOT EXISTS stream_moving  boolean[];

-- Índice parcial para actividades con potencia (útil para análisis de ciclismo)
CREATE INDEX IF NOT EXISTS idx_streams_has_watts
    ON strava_streams_raw (strava_activity_id)
    WHERE stream_watts IS NOT NULL;

-- Índice parcial para actividades con temperatura
CREATE INDEX IF NOT EXISTS idx_streams_has_temp
    ON strava_streams_raw (strava_activity_id)
    WHERE stream_temp IS NOT NULL;

-- Comentarios descriptivos
COMMENT ON COLUMN strava_streams_raw.stream_watts  IS 'Potencia en vatios por segundo. NULL si no hay potenciómetro.';
COMMENT ON COLUMN strava_streams_raw.stream_temp   IS 'Temperatura ambiente en °C por segundo. NULL si no hay sensor.';
COMMENT ON COLUMN strava_streams_raw.stream_moving IS 'True si el atleta se movió en ese segundo. Disponible en casi todas las actividades GPS.';
