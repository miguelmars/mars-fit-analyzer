# EPOCH P0 — Post-Workout Debrief + Intent vs Reality (implementation report)

Date: 2026-06-27 · Status: **engine built + tested locally, NOT wired/deployed.**
Build step #3, runs over a normalized (#1) + audited (#2) endurance event PLUS the planned
intent. Spec: `EPOCH_DESIGN_SYSTEM/EPOCH_DEBRIEF_INTENT_VS_REALITY_SPEC.md`.

> Says **whether the session did what it was supposed to do** — not just that it was
> completed. You-vs-you tone, explains the why, never scolds, never auto-changes anything.

## Files created (none modified)
| File | Role |
|---|---|
| `post_workout_debrief.py` | Pure engine: Intent-vs-Reality verdict + "The Read" (6 modules). |
| `tests/test_post_workout_debrief.py` | 9 validation cases (all pass). |

## How it works
Inputs: a `TimelineEvent` (endurance), an optional `PlannedIntent` (intent type / coach
notes / phase / targets), an `AthleteContext` (lthr, ftp, hr_max), and optional audit
flags. Output = a `Debrief` with the 6 spec modules:
1. **Outcome summary** — one plain sentence.
2. **Intent vs Reality** verdict + reason: `fulfilled` / `over_reached` / `under` /
   `different_stimulus` / `unplanned`.
3. **What it likely built** — recovery / aerobic base / tempo / threshold / VO2.
4. **Evidence + confidence** — numbers used + `confidence_level`, with **gating**:
   if the audit raised a 🔴 flag, `gating_note` is declared before any conclusion.
5. **What looked unusual** — HR above threshold, HR pinned near max, derived metrics, audit reds.
6. **Next action / what to watch** + `plan_needs_attention`.

## Intent vs Reality logic (heuristics v1)
- Maps both planned and actual effort onto one intensity ladder
  (recovery→endurance→tempo→threshold→VO2), from power IF (`NP/FTP`) when available,
  else HR ratio (`avg_hr/lthr`).
- **Context first**: a coach note saying "easy/recovery" overrides the labeled type;
  numeric targets (`target_if`, `target_avg_hr`) win over words; phase is considered.
- Verdict: equal level → fulfilled; harder → over-reached (easy-planned-but-hard ⇒
  "harder, not better"); easier → under; structured plan done steady → different stimulus;
  no plan → unplanned (still debriefs what it built); no HR/power → low-confidence read.

## Tests / validation
`tests/test_post_workout_debrief.py` — **9 cases, all pass**, mapping to the spec's
acceptance criteria: easy-planned-but-hard → over-reached; recovery done easy → fulfilled;
no plan → unplanned (still debriefs); 🔴 audit flag → gating declared; coach note read &
reflected; planned-hard-done-easy → under; structured-done-steady → different stimulus;
no-intensity-data → low confidence; serialization.

## What is still missing / next
- Not wired to HTTP yet (engine only). Optional: `GET /timeline/{event_id}/debrief` that
  builds `AthleteContext` from the athlete's settings and pulls `PlannedIntent` from the
  plan/coach-notes source, passing audit flags for gating.
- `different_stimulus` / structured detection is coarse (laps-count heuristic); refine with
  interval-vs-target comparison later.
- Next P0 layer: **Goal Readiness / Capability Gap** (`EPOCH_GOAL_READINESS_SPEC.md`),
  which consumes this debrief history + the timeline.
