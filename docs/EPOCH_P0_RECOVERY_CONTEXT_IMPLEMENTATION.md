# EPOCH - Recovery Context Implementation

Date: 2026-06-28  
Status: implemented locally, pure engine, endpoint wired, not deployed.

## What This Adds

`recovery_context.py` implements Recovery Context / Recovery Reserve:

> What can I absorb today, and why?

It is a pure engine. It does not import FastAPI, DB clients, Garmin, Strava, templates, or UI code.

The HTTP adapter is separate:

```text
GET /recovery-context
```

It lives in `routers/timeline.py`, which was already mounted. No `main.py` change was required.

## Inputs

- Canonical timeline events from #1.
- Data health from #2.
- Capability matrix from the foundation layer.
- Optional wellness/check-in records:
  - sleep
  - HRV
  - resting HR
  - fatigue
  - soreness
  - stress
  - illness / injury
- Optional recent debriefs.
- Optional planned workout context.

## Output

The engine returns:

- recovery range
- confidence
- state
- recommendation
- drivers with evidence
- blockers
- risks
- missing signals
- data sources
- next action
- gating note when data quality is red

## States

- `ready`
- `estimated`
- `needs_signal`
- `needs_history`
- `conflict`
- `red_flag`

## Recommendations

- `rest_or_recover`
- `easy_only`
- `aerobic_ok`
- `quality_possible`
- `race_ready`
- `unknown`

## Rules Preserved

- No false precision.
- No medical diagnosis.
- No vendor-name copying.
- No automatic plan, FTP, zone, or recovery changes.
- Missing sleep/HRV stays missing.
- Subjective fatigue can downgrade the recommendation.
- Illness or injury blocks hard recommendations.
- Red data quality lowers confidence.

## Files

Core:

```text
recovery_context.py
```

Tests:

```text
tests/test_recovery_context.py
```

Docs:

```text
docs/EPOCH_P0_RECOVERY_CONTEXT_IMPLEMENTATION.md
```

HTTP adapter:

```text
routers/timeline.py
```

## Verification

Run:

```text
PYTHONPYCACHEPREFIX=/tmp/mars-fit-pycache .venv/bin/python -m pytest tests/test_recovery_context.py -q
```

Expected engine test:

```text
9 passed
```

Endpoint + engine tests:

```text
PYTHONPYCACHEPREFIX=/tmp/mars-fit-pycache .venv/bin/python -m pytest tests/test_intelligence_endpoints.py tests/test_recovery_context.py -q
```

Expected:

```text
16 passed
```

Full suite:

```text
286 passed, 0 failed
```

## Next Step

Only after deploy:

- wire the Today/Home hero after P0 is deployed
