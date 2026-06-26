-- Garmin active snapshot + canonical read layer.
-- Non-destructive: clean_sessions keeps every source row.

ALTER TABLE garmin_export_activities
    ADD COLUMN IF NOT EXISTS source_batch TEXT;

ALTER TABLE garmin_export_activities
    ADD COLUMN IF NOT EXISTS is_active_snapshot BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_garmin_export_activities_active_start
    ON garmin_export_activities(is_active_snapshot, start_time_utc);

CREATE OR REPLACE VIEW canonical_sessions AS
WITH cutoff AS (
    SELECT MAX(start_time_utc) AS garmin_cutoff_utc
    FROM garmin_export_activities
    WHERE is_active_snapshot IS TRUE
),
active_garmin AS (
    SELECT cs.*
    FROM clean_sessions cs
    JOIN garmin_export_activities ga
      ON ga.source_activity_id = cs.source_activity_id
     AND ga.is_active_snapshot IS TRUE
    WHERE cs.source = 'garmin_export'
),
new_strava AS (
    SELECT cs.*
    FROM clean_sessions cs
    CROSS JOIN cutoff c
    WHERE cs.source = 'strava'
      AND (c.garmin_cutoff_utc IS NULL OR cs.start_time > c.garmin_cutoff_utc)
),
unmatched_recent AS (
    SELECT cs.*
    FROM clean_sessions cs
    CROSS JOIN cutoff c
    WHERE cs.source = 'current_sessions_recent'
      AND (c.garmin_cutoff_utc IS NULL OR cs.start_time > c.garmin_cutoff_utc)
      AND NOT EXISTS (
          SELECT 1
          FROM clean_sessions s
          WHERE s.source = 'strava'
            AND ABS(EXTRACT(EPOCH FROM (s.start_time - cs.start_time))) <= 120
            AND CASE
                  WHEN s.sport IN ('cycling', 'indoor_cycling') THEN 'cycling'
                  WHEN s.sport IN ('running', 'trail_running', 'treadmill_running') THEN 'running'
                  WHEN s.sport IN ('lap_swimming', 'open_water_swimming', 'swimming') THEN 'swimming'
                  WHEN s.sport IN ('strength', 'strength_training') THEN 'strength'
                  WHEN s.sport IN ('workout', 'indoor_cardio', 'cardio') THEN 'workout'
                  ELSE s.sport
                END
                =
                CASE
                  WHEN cs.sport IN ('cycling', 'indoor_cycling') THEN 'cycling'
                  WHEN cs.sport IN ('running', 'trail_running', 'treadmill_running') THEN 'running'
                  WHEN cs.sport IN ('lap_swimming', 'open_water_swimming', 'swimming') THEN 'swimming'
                  WHEN cs.sport IN ('strength', 'strength_training') THEN 'strength'
                  WHEN cs.sport IN ('workout', 'indoor_cardio', 'cardio') THEN 'workout'
                  ELSE cs.sport
                END
            AND (
                (COALESCE(s.distance_km, 0) <= 0.1 AND COALESCE(cs.distance_km, 0) <= 0.1)
                OR ABS(COALESCE(s.distance_km, 0) - COALESCE(cs.distance_km, 0))
                   <= GREATEST(
                       0.2,
                       0.03 * GREATEST(
                           COALESCE(s.distance_km, 0),
                           COALESCE(cs.distance_km, 0)
                       )
                   )
            )
      )
),
unmatched_other_new AS (
    SELECT cs.*
    FROM clean_sessions cs
    CROSS JOIN cutoff c
    WHERE cs.source NOT IN ('garmin_export', 'strava', 'current_sessions_recent')
      AND (c.garmin_cutoff_utc IS NULL OR cs.start_time > c.garmin_cutoff_utc)
)
SELECT * FROM active_garmin
UNION ALL
SELECT * FROM new_strava
UNION ALL
SELECT * FROM unmatched_recent
UNION ALL
SELECT * FROM unmatched_other_new;

COMMENT ON VIEW canonical_sessions IS
    'Garmin active snapshot owns history; Strava contributes only after Garmin UTC cutoff.';
