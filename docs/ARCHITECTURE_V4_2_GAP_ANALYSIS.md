# Architecture v4.2 Gap Analysis

Review date: 6 junio 2026

Source reviewed: `BitacoraMars_Architecture_v4_2.docx`

## Executive Verdict

The document has the right long-term architecture. The repository is ahead of
the document in Phase 1 and Phase 2, but it has not yet implemented the
versioned zone and capability contracts required for Phase 3.

Stage 24 is implemented and its historical recalculation completed:

1. preserve every original zone value
2. create an append-only `zone_models` catalog
3. calculate `z2_pct_mars` with explicit confidence and model version
4. roll the result into `athlete_snapshots`
5. validate the JSON before building Capability UI

## Status Matrix

| Architecture area | Status | Repository evidence | Next action |
| --- | --- | --- | --- |
| Original Garmin source | Complete | Private original ZIP audited; staging and clean build tools exist | Add repeatable source-manifest checksum |
| Clean activity master | Complete | `clean_sessions` is used by activities, sessions, dashboard and history | Add integrity constraints and audit runs |
| Complete athletic history | Complete | 2,901 sessions and nine living sport groups | Keep automatic freshness checks |
| Weekly snapshots | Complete | 436 weeks populated in `athlete_snapshots` | Add zone-version fields |
| Correlations and trends | Complete with safeguards | Heat confidence, within-year weight control and load context are active | Monitor sample quality |
| Multisport status | Complete locally | General, Cycling and Running modes; continuity and discipline transitions | Verify after final deployment |
| Running zones LT 173 | Complete | `mars_context.py` contains LT 173 and correct ranges | Remove stale TD-003 from architecture status |
| Charts endpoint | Working locally | `/charts/{id}` and telemetry paths load | Revalidate in Railway |
| Wellness summary | Working locally | `/gpt/wellness-summary` responds locally | Revalidate in Railway |
| Versioned zone models | Complete locally | Append-only `zone_models` catalog and two active models | Validate after final deployment |
| Historical Mars Z2 | Ready to execute | Non-destructive recalculation and dry-run are complete | Execute once, then validate coverage |
| Capability Engine | Complete locally | Six capabilities, separate confidence, limiters and personal anchors | Validate UI and production deployment |
| Goal Engine | Missing | No event-weight tables or readiness endpoint | Stage 26 |
| Historical Pattern Engine | Missing | Return context exists, but no similarity engine or pattern records | Stage 28 |
| Recommendation Engine | Partial | Human recommendations exist in Progress and Coach | Move to backend contract after capabilities |
| Automated backups | Partial | JSON backup endpoint exists | Add scheduled, encrypted, restorable backups |
| Operational security | Needs reinforcement | Admin endpoints have a fallback token | Require environment token; remove fallback |
| Schema migrations | Needs reinforcement | Schema is created from application helpers | Add migration ledger and idempotent migrations |
| Automated tests | Partial | Zone assignment, elapsed-time weighting, matching and weekly rollup are covered | Add API regression suite |

## Stage 24 Dry-run

Executed and validated on 6 junio 2026:

- 2,901 sessions scanned
- 1,503 cycling/running sessions eligible
- 620 unique legacy telemetry sessions linked one-to-one
- 557 sessions with usable heart-rate telemetry
- 581 high-confidence heuristic links, 35 moderate and 4 exact-ID links
- 831 session-average estimates, explicitly marked with confidence 0.40
- 115 sessions honestly retained as no-heart-rate
- 252 weekly snapshot rows ready to update
- original Z2 values remain immutable
- 1,503 eligible sessions updated
- 252 weekly snapshots updated

## Capability Engine v1

- `/gpt/capacidades` returns the six capability contracts.
- `/gpt/capacidad/{nombre}` returns one stable capability key.
- Motor Aerobico uses efficiency, calendar consistency, Mars Z2 and long
  endurance against personal historical references.
- Missing evidence produces `score: null`, not a false zero-performance score.
- Score, confidence and maturity are independent.
- Historical and similar-era anchor status is explicit for every capability.
- `/capacidades` renders the engine for non-technical use.

## Session Environment

- `session_environment` stores one non-destructive context row per clean session.
- 2,901 sessions were processed; 2,592 have altitude context (89.3%).
- Habitual altitude is learned from the athlete's dominant GPS cluster.
- Absolute altitude and accumulated ascent remain separate concepts.
- Motor Aerobico uses comparable-altitude history when at least 3 recent and
  10 historical sessions exist within +/-300 m.
- Acclimatization is explicitly an estimate from comparable-altitude training
  days in the prior 21 days, not assumed residence.
- Country labels are coarse bounding-box inferences; GPS remains source truth.

## Document Corrections

- TD-003 is already resolved locally: running uses LT 173.
- TD-001 and TD-002 did not reproduce locally and should be marked
  "verify in production", not "broken".
- The source-of-truth chain should explicitly distinguish:
  - immutable Garmin export
  - legacy raw `sessions`
  - verified analytical master `clean_sessions`
  - optional telemetry `session_records`
- Zone enrichment belongs on `clean_sessions` as the analytical master. Legacy
  `sessions` may receive compatibility columns, but capabilities should not read
  it directly.
- "Best historical version" should use a robust benchmark such as a sustainable
  percentile or comparable era, not a single raw maximum.

## Resistance Priorities

### P0: Data correctness

- versioned zones
- immutable original values
- confidence recorded separately from score
- repeatable recalculation with dry run
- row counts and coverage before/after every migration

### P1: Recoverability

- scheduled database backup
- restore drill
- Garmin ZIP checksum and source manifest
- migration ledger

### P2: Reliability

- unit tests for zone assignment and capability math
- endpoint regression tests
- startup health without schema races
- freshness and coverage alerts

### P3: Product intelligence

- six capabilities
- comparable-era anchors
- event readiness
- historical patterns
- recommendation engine

## Stage 24 Acceptance Criteria

- `zone_models` is append-only and contains active cycling and running models.
- Every recalculated session records `zone_model_used`.
- `z2_pct_original` is never modified after first preservation.
- `z2_pct_mars` and `z2_confidence_score` are separate fields.
- Telemetry-based rows and summary-estimated rows are distinguishable.
- Snapshot zone fields can be rebuilt from enriched clean sessions.
- Re-running the process is idempotent.
- Dry-run and execute summaries report the same eligible population.
