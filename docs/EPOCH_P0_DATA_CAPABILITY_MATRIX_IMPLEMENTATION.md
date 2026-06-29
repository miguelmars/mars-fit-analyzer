# EPOCH P0 — Data Capability Matrix (implementation report)

Date: 2026-06-28 · Status: **engine + endpoint built, tested locally, NOT deployed by Codex.**
Foundation layer over the Canonical Athlete Timeline. Listed in the original task's
foundation set and the index P0 build sequence (#3). Metric tiers from the Metric Math /
Data Ecosystem supplement ("derivable now vs needs device").

> Answers: "Given the data you actually have, what can Epoch tell you **now**, what is
> **derivable**, what **needs more history**, and what **needs a sensor**?" Pure aggregation
> over per-signal confidence — no domain logic, no new data, always declares confidence.

## Files created (none modified by Codex this step)
| File | Role |
|---|---|
| `data_capability_matrix.py` | Pure engine: metric catalog + `capability_matrix(events)`. |
| `tests/test_data_capability_matrix.py` | 6 validation cases (all pass). |
| `routers/timeline.py` | + `GET /capability-matrix` (read-only). |
| `tests/test_intelligence_endpoints.py` | + endpoint test. |

## How it works
`capability_matrix(events) -> CapabilityMatrix`. It computes **per-signal coverage** across
the timeline's endurance events (fraction with each signal `available`/`derived`), then for a
catalog of metrics decides a status:
- `available_now` — computable from current imported data.
- `estimate_only` — only as a modeled estimate (e.g. Recovery Reserve without sleep/HRV).
- `needs_history` — signals present, not enough history yet (says how much more).
- `needs_signal` — a required signal/sensor is missing (says what to record/connect).
Each row carries coverage, confidence, missing signals, and an `unlock` hint; the matrix adds
`available_now`, de-duplicated `unlock_suggestions`, and a plain summary.

## Catalog (P0)
session_load · fitness/fatigue/form · overreach (ACWR) · efficiency · durability · climbing ·
aerobic engine · consistency · volume · this-route-over-time → derivable from imported FIT/GPX/
TCX/CSV. recovery_reserve → estimate-only (better with sleep/HRV). hrv_status, running_power →
needs a device/sensor not carried by the timeline yet.

## Why it matters for the real athlete
For an HR-only athlete with GPS + elevation + ~8 weeks history (exactly Miguel's case), the
matrix reports ~10 metrics available now, Recovery Reserve as estimate-only, and HRV/running-power
as needs-signal — matching the product principle "works with old devices, improves with new ones,
always declares confidence."

## Tests / validation
`tests/test_data_capability_matrix.py` — **6 cases**: HR-only tiers; no-HR/no-power blocks Session
Load; short history → needs_history; climbing needs elevation; empty timeline; serialization.
Plus an endpoint test in `tests/test_intelligence_endpoints.py`.

## What is still missing / next
- Auto-fill is not needed here (pure timeline aggregation); the endpoint takes only `athlete_id`.
- Source-level capability (which connector could provide a missing signal) is a later refinement
  once the Source Manager/Router exists.
- Metric formulas themselves live in their own engines (mars_load etc.); this layer reports
  *availability*, not the metric values.
