# Architecture v4.5 Gap Analysis

Review date: 6 junio 2026

Source reviewed: `BitacoraMars_Architecture_v4_5.docx`

## Executive Verdict

Architecture v4.5 is aligned with the product direction and correctly places
Capability Validation before Capability History. The repository is slightly
ahead of the document in environmental context and local endpoint availability.

The correct sequence is:

1. record an audited Capability Engine baseline
2. detect changes greater than 15 points in later executions
3. build annual capability history from period-specific calculations
4. add the visual timeline only after historical results are validated

## Corrections To v4.5

- Motor Aerobico is no longer the documented 87.2 result. Altitude-aware
  comparison changed the current local result to approximately 89.4. The exact
  value must be captured from the database as the first official validation
  run rather than copied from this document.
- `/charts/{id}` exists and works locally. TD-001 should say `verify Railway`,
  not `broken`.
- `/gpt/wellness-summary` exists and works locally. TD-002 should say
  `verify Railway`, not `broken`.
- `/gpt/session-environment/{id}` is already implemented. It is not an E25
  deliverable.
- `session_environment` uses `clean_session_id` as its foreign key, not an
  integer `session_id`.
- Environment confidence is split into altitude and location confidence. This
  is more precise than the single field proposed in the document.
- Altitude bands in the implementation use `low`, `moderate`, `high` and
  `very_high`; the document uses a different five-band vocabulary. One naming
  contract must be selected before exposing band labels as public API.
- The current altitude comparator uses a continuous +/-300 m window. This is
  more precise than comparing only broad altitude bands and should remain the
  primary method.

## Stage 24.5 Implemented Locally

- `validate_capability()` recomputes the weighted score from its indicators.
- Indicator weights must total 1.0.
- Historical and similar-era anchors are checked separately.
- Every indicator reports its weighted contribution.
- A change greater than 15 points produces `review_required`.
- Missing indicators, invalid arithmetic or a missing double anchor produce
  `invalid`.
- `capability_runs` stores official audited executions without modifying source
  sessions or snapshots.
- Read-only validation:
  `GET /gpt/capacidad/{nombre}/validation`
- Controlled recording:
  `POST /admin/capability-validation/{nombre}?execute=true`

The first recorded run becomes the official baseline. Later runs compare score
and indicator deltas against that record.

## Official Motor Aerobico Baseline

Recorded on 6 junio 2026 as `capability_runs.id = 1`.

- score: 89.4
- confidence: 0.93
- maturity: 92.6
- validation: accepted
- arithmetic delta: 0.0
- alerts: none
- blockers: none
- historical anchor: week of 20 septiembre 2021
- similar-era anchor: week of 24 julio 2023, similarity 0.961
- altitude comparison: 877 historical sessions within +/-300 m

Indicator contributions:

| Indicator | Score | Weight | Contribution |
| --- | ---: | ---: | ---: |
| Cycling efficiency | 94.4 | 0.40 | 37.76 |
| 12-week consistency | 66.7 | 0.25 | 16.68 |
| Mars Z2 | 100.0 | 0.20 | 20.00 |
| Long endurance | 100.0 | 0.15 | 15.00 |

The current limiter is 12-week consistency: 8 active weeks out of 12.

The older 74.9 and 87.2 values were not stored as audited executions, so their
exact indicator deltas cannot be reconstructed honestly. The accepted 89.4
baseline differs because it uses recalculated Mars Z2 and an efficiency
reference selected from similar-altitude history. Future changes are fully
auditable from `capability_runs`.

## Remaining For E25

Motor Aerobico history is implemented:

- 424 rolling 12-week blocks evaluated.
- Every reference uses only data before the evaluated block.
- Efficiency first uses sessions within +/-300 m of block altitude.
- A block needs 6 active weeks and 3 efficiency sessions.
- Years below 60% maturity or 70% confidence remain visible but cannot be
  named best historical period.
- `GET /gpt/capacidad/motor_aerobico/history` returns annual history.
- `/capacidades` renders the annual timeline, current official baseline and
  best mature period.

Current best mature period:

- 13 noviembre 2023 to 4 febrero 2024
- historical score 93.4
- cycling context
- confidence 0.70
- maturity 85.3

Remaining E25 work:

1. Extend the same historical contract to the other five capabilities.
2. Resolve LT history before claiming period-perfect historical zone accuracy.
3. Add API regression tests with a disposable database fixture.

## Recommended E25 Contract

Each annual history item should contain:

- year
- score, confidence and maturity
- indicator breakdown
- historical and similar-era anchors
- dominant sport and altitude context
- active weeks and sample coverage
- best 12-week block
- best-period tag
- validation status

Years without sufficient evidence must return `score: null` and an explicit
reason. They must never be represented as zero performance.

## Parallel Work

LT history is valuable but should not silently rewrite historical capability
scores. Auto-detected tests need their own confidence and must create versioned
zone models before any historical recalculation.

## Operational Improvements

- Remove the default admin token before production; require `ADMIN_TOKEN`.
- Add a migration ledger instead of relying only on startup table helpers.
- Add API regression tests around capabilities, validation and history.
- Add scheduled database backups and a restore drill.
- Add a checksum manifest for the original Garmin ZIP.
