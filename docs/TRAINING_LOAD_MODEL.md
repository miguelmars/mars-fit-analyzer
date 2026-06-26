# Training Load Model — Fitness / Fatigue / Form

> Design doc (plan, not implementation). Adds an objective training-load signal
> to EPOCH: CTL (Fitness), ATL (Fatigue), TSB (Form), computed from the athlete's
> own workouts. Math adapted from the TrainingPeaks model — **without** its
> fragile login. We own the data; we own the calculation.

## 1. Why this exists

EPOCH today has two health signals, and **neither is an objective training-load curve**:

| Existing signal | What it measures | Source |
|---|---|---|
| **Readiness** (`capability_engine.readiness`) | Are your *capabilities* ready for the event? | Capabilities vs event demand |
| **Today-adaptation** (`routers/plan_vivo`) | How did you wake up? | Sleep, resting HR, **self-reported** fatigue |

`plan_vivo` literally notes: *"the body may be carrying fatigue the numbers do not show yet."*
**CTL/ATL/TSB is exactly those numbers.** It fills a known gap.

It is also the right signal for athletes who do **not** track sleep or enter manual
data: it is computed **only from workouts**, which already sync automatically (Strava).
No manual input. No sleep wearable required.

## 2. Principles

1. **Nothing is removed.** This signal is *added* next to Readiness and
   Today-adaptation. The three coexist and complement each other.
2. **Adaptive per athlete.** EPOCH detects what each athlete has and lights up the
   signals that apply. A signal an athlete lacks stays dormant for them, active for others.
3. **No instrument required.** Never assume a power meter. The load model degrades
   gracefully across whatever the athlete has.
4. **Confidence is declared.** Every metric states the method used and its confidence,
   consistent with EPOCH's honesty contract ("say what it used and what it cannot know").
5. **Not the headline.** Unlike TrainerRoad/TrainingPeaks, CTL/ATL/TSB is *one input*,
   shown alongside capabilities — not the king metric.
6. **English everywhere** (names, user-facing strings, comments).

## 3. The metrics (athlete-friendly)

| Metric | Name | Plain meaning | Speed |
|---|---|---|---|
| **CTL** | Fitness | How trained you are. Your accumulated base. | Slow (≈42 days) |
| **ATL** | Fatigue | How tired you are right now. | Fast (≈7 days) |
| **TSB** | Form | Fresh or cooked. `CTL − ATL`. | Daily |

## 4. The model

### Step 1 — `session_tss` (tiered, picks best available data)

```
session_tss(session, athlete_thresholds) -> { tss, method, confidence }

  1. Power  (normalized_power_w + ftp_watts)
        IF  = NP / FTP
        TSS = (duration_s / 3600) * IF^2 * 100        confidence: HIGH
  2. Heart rate  (avg_hr_bpm + threshold_hr)          <-- Miguel today
        hrTSS via TRIMP / %threshold                   confidence: MEDIUM
  3. Pace / distance  (running, no HR)
        rTSS / pace-based                              confidence: MEDIUM-LOW
  4. Duration only
        duration * estimated intensity                 confidence: LOW
```

### Step 2 — daily TSS series
Sum TSS per calendar day; days with no workout = 0. Build a continuous series.

### Step 3 — fitness curve (the TrainingPeaks math)

```
CTL_today = CTL_yesterday + (TSS_today - CTL_yesterday) / 42
ATL_today = ATL_yesterday + (TSS_today - ATL_yesterday) / 7
TSB_today = CTL_yesterday - ATL_yesterday
```

### Step 4 — interpret TSB (English table)

| TSB | Status |
|---|---|
| > +25 | Very fresh (detraining risk) |
| +10 … +25 | Fresh (race-ready) |
| 0 … +10 | Neutral (normal training) |
| −10 … 0 | Tired (absorbing load) |
| −25 … −10 | Very tired (high fatigue) |
| < −25 | Exhausted (overtraining risk) |

## 5. Inputs — and where they come from (all automatic)

| Input | Source | Manual? |
|---|---|---|
| Session duration | Each workout (Strava sync) | No |
| Session avg HR | Each workout (Strava sync) | No |
| Normalized power | Each workout, **if** sensor present | No |
| **Threshold HR** (once) | Athlete's Mars HR zones (Z4 ≈161-168) or estimated from history | No (set once, not daily) |
| FTP (once, power path) | Athlete profile `ftp_watts` | No (set once) |

## 6. Where it lives in the backend

- **New pure module `mars_load.py`** (repo root, beside `capability_engine.py`):
  `session_tss()`, `fitness_curve()`, `interpret_tsb()`. No DB access → unit-testable,
  reusable everywhere.
- **New endpoint `GET /gpt/training-load`** in `routers/gpt_analytics.py` (sibling of
  `/gpt/fitness-timeline`, which stays as-is — it computes monthly aerobic efficiency,
  a different model). Reads daily TSS from `sessions_clean_compat` + athlete thresholds,
  calls the pure functions, returns:
  `{ ctl, atl, tsb, status, confidence, daily_data[] }`.
- **Does not touch** `readiness` or `today-adaptation`.

## 7. Consumers (where it gets shown)

- **Today** — objective "how am I", not only self-reported.
- **Progress** — fitness/fatigue/form trend.
- **Plan Vivo** — detect fatigue *before* it's felt (closes the gap above).
- **Calendar** — real load budget.
- **AI Plan Draft** — load-ramp rule, recovery guardrail.
- **EPOCH mobile (React)** — replaces the hardcoded 72.4 / 81.6 / −9.2 snapshot.

## 8. Coexistence (nothing removed)

| Signal | Lights up for | Status |
|---|---|---|
| CTL/ATL/TSB (workouts) | All athletes | NEW (this doc) |
| Today-adaptation (sleep / resting HR / manual) | Athletes who have that data | Kept |
| Readiness (capabilities vs event) | All athletes | Kept |

## 9. Build order

1. `session_tss` (the foundation; reusable beyond this curve).
2. Daily TSS series.
3. `fitness_curve` + `interpret_tsb`.
4. `GET /gpt/training-load`.
5. Wire consumers, starting with Plan Vivo / Today.

## 10. Open question

- **Threshold HR source:** read from existing Mars zones config, or estimate from
  session history (e.g. max sustained HR)? Decide before wiring the HR path so
  hrTSS is trustworthy for Miguel (HR-only athlete).
