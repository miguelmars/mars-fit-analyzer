# EPOCH P0 — Data Quality + Zone Audit (implementation report)

Date: 2026-06-27 · Status: **engine built + tested locally, NOT wired/deployed.**
Build step #2, runs ON the Canonical Athlete Timeline (after ingestion, before any
coaching conclusion). Spec: `EPOCH_DESIGN_SYSTEM/EPOCH_DATA_QUALITY_ZONE_AUDIT_SPEC.md`.

> "Before judging the athlete, Epoch checks the data." This layer **flags and suggests**;
> it **never** changes zones/FTP automatically and makes no medical claims (per spec).
> Additive + isolated: a pure module, no `main.py`/router/schema changes, no endpoint yet.

## Files created (none modified)
| File | Role |
|---|---|
| `data_quality_audit.py` | Pure audit engine: checks + per-athlete data-health + confidence gating. |
| `tests/test_data_quality_audit.py` | 12 validation cases (all pass). |

## How it works
Inputs: normalized `TimelineEvent`s (from ingestion, carrying `confidence.signals` +
`data_flags`) and an `AthleteProfile` (hr_max, lthr, ftp_w + set date, hr_zones).
It only runs the checks it has data for and **says what it could not check** (`notes`).
- `audit_event(event, profile)` → per-activity flags.
- `audit_athlete(events, profile)` → a "health of your data" panel (`AthleteDataHealth`:
  zones_reliable, ftp_current, high/medium/low counts, flags, notes).
- `gating_note(flags)` → a sentence the Debrief / Goal Readiness layers MUST show before
  concluding if any 🔴 HIGH flag exists (confidence gating).

## Checks (spec-aligned), severity, suggest-only
- 🔴 `suspicious_hr_max` — activity max HR above declared max HR (affects all zones).
- 🔴 `incorrect_zones` — HR zones not ascending / above max HR / threshold outside range.
- 🟠 `stale_ftp` — FTP older than ~180 days OR recent power efforts > 105% of FTP (suggest test).
- 🟠 `mislabeled_activity` — "easy/recovery" label but avg HR ≥ 90% of threshold.
- 🟠 `unreliable_power` — implausible max watts / spiky max-vs-avg (lower power confidence).
- 🟡 `missing_sensor_data` — HR missing (surfaced from ingestion confidence).
- 🟡 `duplicate` — confirmed or uncertain duplicate (from ingestion lineage).
- 🟡 `inconsistent_source` — merged sources disagree on distance > 10% (keeps higher precedence).

Every flag carries `severity + message + suggested_action`. Nothing is auto-applied
(test `test_audit_does_not_mutate_inputs` proves profile + events are untouched).

## Tests / validation
`tests/test_data_quality_audit.py` — **12 cases, all pass**: suspicious HR max + gating;
clean event → no flags; stale FTP by date and by recent power (suggest only, FTP unchanged);
mislabeled recovery; unreliable power; missing sensor; uncertain duplicate; incorrect zones;
inconsistent source; gating None without HIGH flags; inputs never mutated.

## What is still missing / next
- Not wired to HTTP yet (engine only). Optional next: `GET /timeline/{event_id}/audit`
  and `GET /data-health` in `routers/timeline.py` (build `AthleteProfile` from the existing
  Mars zones/HR-max/FTP), + show `gating_note` in the Debrief layer.
- `unreliable_power` is summary-level (no power-stream dropout analysis yet).
- Next P0 layer after this: Post-Workout Debrief / Intent vs Reality, which should consume
  `gating_note` so it never concludes confidently on red-flagged data.
