# EPOCH P0 — Goal Readiness / Capability Gap (implementation report)

Date: 2026-06-28 · Status: **engine built + tested locally, NOT wired/deployed.**
Build step #4 (the reality check). Closes the MVP chain: #1 Ingestion → #2 Audit →
#3 Debrief → **#4 Goal Readiness**. Spec: `EPOCH_DESIGN_SYSTEM/EPOCH_GOAL_READINESS_SPEC.md`.

> Answers "am I going to make it, what's missing, with what confidence?" as a **range with
> confidence**, never a false-precision number. Honest reality-check: if you're short, it
> says what's missing and what to do. Never auto-changes anything.

## Files created / updated
| File | Role |
|---|---|
| `goal_readiness.py` | Pure engine: `Goal`, `Readiness`, `assess()`. |
| `tests/test_goal_readiness.py` | 9 validation cases (all pass). |
| `tests/test_p0_pipeline_integration.py` | Updated to prove #1→#2→#3→#4 composition with real imported events. |

## How it works
`assess(events, goal, data_health=None, as_of=None) -> Readiness`. It reads the timeline's
endurance events for **volume / consistency / durability / climbing**, compares them to the
**goal demand** (distance / elevation / duration / weekly hours / time remaining), and uses
the audit's `AthleteDataHealth` (#2) to **gate confidence**.

Output (`Readiness`):
1. **Readiness range** — `readiness_low_pct..readiness_high_pct` (band widens as confidence drops).
2. **Capability gap** — per-capability scores (endurance, durability, climbing, consistency;
   sustained_power & fueling honestly marked *not assessable* in P0) + the **weakest**.
3. **Volume gap** — `recent h/week` vs `target h/week` (derived from the goal) + the gap.
4. **Blockers** — capabilities below 0.7, plus `data_quality` when the audit is red.
5. **Next proof point** — one actionable step keyed to the weakest capability (or "re-test
   threshold/FTP" when data is red).
6. **Confidence + risks + missing** — always declared.

## States (no invented verdicts)
`ready_range` (a range was produced) · `needs_history` (<3 active recent weeks) ·
`no_target` (no goal/date) · `event_passed` · plus reserved `low_confidence`,
`injured_returning`. Low data confidence is normally expressed via `confidence_level` +
`risks` while still giving a range (per spec criterion 4).

## Tests / validation
`tests/test_goal_readiness.py` — **9 cases, all pass**, mapping to the spec: returns a range
(not exact); identifies weakest capability (flat history vs hilly event → climbing); shows the
concrete volume gap; low confidence + warning when the audit is red; `needs_history` when sparse;
actionable next proof point; `no_target` / `event_passed`; serialization.

Cross-layer validation: `tests/test_p0_pipeline_integration.py` now covers #1→#2→#3→#4 so
an imported activity can flow through audit, debrief, and readiness without mocks.

## What is still missing / next
- Not wired to HTTP yet (engine only). Optional: `GET /goal-readiness` that builds the `Goal`
  from `mars_goals`, the history from the timeline, and `data_health` from the audit layer.
- `sustained_power` and `fueling` are intentionally unassessed in P0 (no power-target / no
  nutrition ingestion yet) and reported as data limitations.
- Capability/volume heuristics are v1 (e.g. target weekly hours derived from event duration);
  refine with real sport-demand profiles later.
- This completes the P0 intelligence spine (#1–#4). Beyond P0 (do not build yet per the index):
  Recovery Context, Plan/Intent layer, Source Manager/Router, then the additive addenda layers.
