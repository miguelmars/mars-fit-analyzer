# Athlete Snapshots

`athlete_snapshots` is the weekly historical spine for Phase 2.

## Source

- Athletic load comes from `clean_sessions`.
- Manual weight from `weight_log` has priority.
- Historical Garmin weight can be read from the private original export ZIP.
- Weeks with no activity are included with zero load.

## Metric Definitions

- `km_week`, `hours_week`, `sessions`, `active_days`, `calories_week`: all sports.
- `avg_hr`: duration-weighted average across sessions with heart rate.
- `cycling_efficiency`: duration-weighted `speed_kmh / avg_hr_bpm` for cycling.
- `running_efficiency`: duration-weighted `speed_kmh / avg_hr_bpm` for running.
- `efficiency`: compatibility value from the dominant cycling/running sport that week.
- `efficiency_sport`: identifies which sport produced `efficiency`.
- `pct_z2`: estimated cycling duration whose session-average HR was 134-150 bpm.
- `z2_estimated`: always true until exact time-in-zone is reconstructed from telemetry.
- `sport_breakdown`: sessions, distance and hours per raw Garmin sport.
- `fitness_score`: temporary workload proxy, not the future Mars Index.

Weight is only assigned when there is an actual measurement inside that week.
Missing weeks remain null; weight is not interpolated or carried forward.

## Offline Preview

```bash
.venv/bin/python tools/backfill_athlete_snapshots.py \
  --activities-json private_data/garmin_audit/staging/garmin_activities_clean.json \
  --garmin-zip private_data/garmin_source/garmin_export_original_27_abril_2026.zip \
  --out private_data/garmin_audit/athlete_snapshots_preview.json
```

## Database Dry Run

```bash
.venv/bin/python tools/backfill_athlete_snapshots.py \
  --garmin-zip private_data/garmin_source/garmin_export_original_27_abril_2026.zip \
  --out private_data/garmin_audit/athlete_snapshots_preview_db.json
```

## Execute

```bash
.venv/bin/python tools/backfill_athlete_snapshots.py \
  --garmin-zip private_data/garmin_source/garmin_export_original_27_abril_2026.zip \
  --execute
```

The execute mode is idempotent. It upserts by `week_start` and does not modify
`clean_sessions`.
