# EPOCH — Master Project Document & Roadmap
### Single source of truth · June 2026

> **Most athletes know what they did. Few understand what they built.**
> EPOCH turns sports data into *understanding*. The past is context; the present decides.

---

## 1. What EPOCH Is

A training-intelligence platform that explains your training instead of just showing
more metrics. Cycling-first, athlete-mobile + coach-web later. It answers the 4
questions the product was born to answer:

1. **Will I reach my goal?**
2. **How am I right now?**
3. **How am I training?**
4. **Which workouts work for me?**

### Founding insight (the "why")
The founder trained for years "with the handbrake on" because his watch had the
wrong HR zones (thought 130 bpm was the top of Z2). Fixing the zones unlocked a
big jump in speed/efficiency. EPOCH exists to catch this for every athlete:
*you may be training braked and not know it.*

### Core philosophy (non-negotiable)
- **Present over past.** Years of history = context, never a verdict on today.
- **Personal over universal.** You vs you, in your current block — not a generic table.
- **Honesty as a feature.** Every reading declares its method and confidence. If data
  is missing, say so — never invent.
- **Ask-first, not form-first.** People want to *ask* and be told, not type data in.
- **Multi-instrument.** Works with whatever the athlete has (power → HR → pace →
  duration). Never require a power meter. Serve everyone.
- **Capabilities, not sports.** Organize by human capabilities (aerobic engine,
  strength-endurance, recovery, consistency…). The sport changes; the body adapting is the same.
- **English everywhere in the product** (UI, labels, user-facing strings).

---

## 2. Strategic Positioning (vs competition)

From competitor research (TrainerRoad / r/Velo) + live observation of TrainingPeaks,
Strava, Garmin. People **hate**: forced AI everywhere, inflated/mislabeled FTP,
gamified "high-score" metrics for retention, lost minimalism. People **want**:
transparent power/FTP well-named with confidence, a good library + fast builder,
flawless Garmin/Edge sync, simplicity, and honesty.

**EPOCH wins by being what they ask for:** honest, confidence-first, individual,
impeccable sync — and adds what nobody else has (capabilities model, fair
workout-identity comparison, longitudinal "épocas", declared data confidence).

**Validation (2026-06-26):** TrainingPeaks independently detected the athlete's HR
threshold at **168** — identical to EPOCH's value. Competitors confirm our math.
Turning that cross-check into a visible trust feature ("your 168 matches
TrainingPeaks & Garmin — confidence: high") is uniquely on-brand.

---

## 3. Current State

### Backend — LIVE in production
- URL: `https://mars-fit-analyzer-production.up.railway.app` (Railway, FastAPI + Postgres + Supabase)
- ~185 endpoints, full intelligence engine (v6.x). Used daily by the founder.
- Real data: 9 years, ~2,900 activities (2018-02-02 → 2026-06-04), sleep to 2026-06-05.

### Frontends — TWO exist
1. **Production PWA (vanilla JS)** served by the backend at `/app` — the live app
   the founder uses. ⚠️ Still has Spanish leaking from backend data (cleanup pending).
2. **React redesign** (`epoch-mobile-prototype`, Vite) — newer UI, English. Runs
   locally; reads `/gpt/training-load` with a `LOCAL GARMIN EXPORT` fallback when the
   backend is unreachable.

### Status honest
- The production backend works and serves real data.
- New code (training-load, living zones, garmin_sync, artifact filter) is built and
  in the upload package, **deployed progressively**. Some still pending a deploy.

---

## 4. Architecture & Data Rules

### Data flow
```
Garmin (history base, to cutoff)  ─┐
                                   ├─►  canonical_sessions (dedup)  ─►  intelligence engine  ─►  API  ─►  app
Strava API (live, after cutoff)  ─┘
```

### Hard data rules
- **Garmin owns history** up to its last export timestamp: **`2026-06-04T15:38:55Z`**.
- **Strava only adds** activities *after* that cutoff. Never duplicate Garmin with Strava.
- **Garmin direct sync** (new, `garmin_sync.py` via the free `garminconnect` lib) brings
  real workout names + structured workouts + wellness (sleep/HRV/RHR) that Strava strips.
- **Wellness** declares its source + confidence (manual vs Garmin). Opportunistic capture,
  never forced (athlete doesn't wear watch to sleep).

---

## 5. Key Facts (athlete + plan)

| Item | Value |
|---|---|
| Athlete | Miguel Ángel Mars · 47 · CDMX **2,300 m altitude (adapted — his normal)** |
| Device | Garmin Forerunner 935 · **no power meter** → HR-based |
| Threshold HR (LTHR) | **168** (confirmed by TrainingPeaks 2026-06-26) |
| Max HR | **187** (from "Test FC Máxima", 2026-05-04) |
| Weight | **88.9 kg** (down from 89.1) |
| "Current era" cutoff | **2026-04-28** — zones fixed, handbrake off; compute threshold from here, not 9 years |
| Goal event | **Time Trial · 2026-10-03** |

### Plan calendar (Garmin Coach Time Trial · 22 weeks)
- **Base:** 2026-05-04 → 2026-06-27 (aerobic engine + volume)
- **Build:** 2026-06-28 → 2026-08-22 (intensity + capacity)
- **Peak:** 2026-08-23 → 2026-10-03 (sharpen)
- **Threshold test window:** ~2026-06-29 (fresh, before Build's Z5 work) → re-anchor zones

---

## 6. What's Built (recent work)

| Module / endpoint | What it does | Status |
|---|---|---|
| `mars_load.py` + `/gpt/training-load` | CTL/ATL/TSB from tiered TSS (power→HR→pace→duration), confidence-declared, era-gated to 2026-04-28 | ✅ Live (CTL 50.7/ATL 71/TSB -20.5) |
| `mars_zones.py` + `/gpt/zone-anchor` + `/gpt/zone-history` | Living HR zones anchored to threshold tests; reproduce current zones; move with each test, kept as history | ✅ Live |
| `garmin_sync.py` + `/admin/garmin-sync` | Direct Garmin pull: real names + wellness | 🟡 Built, needs deploy + Garmin creds |
| Speed-sensor artifact filter + `/admin/clean-device-artifacts` | Block/clean fake "rides" from washing/moving the bike | ✅ Live |

---

## 7. Complete Feature Inventory (~396 features · 22 domains · P0–P4)

EPOCH is a full platform, not just the recent engineering. The inventory:
- **372 unique base features** (`module_registry.json` / `epoch_feature_screen_allocation.json`)
- **+14 prior additions** (N001–N014) — founder-requested extensions
- **+~11 additions from the current build sprint** (N015–N025) — what we've built/designed now
= **~397 designed features** across **22 domains (D01–D22)**, sequenced P0→P4.

### Priority sequencing (base registry, 371)
| Priority | Meaning | Count |
|---|---|---|
| **P0** | Cycling launch (MVP) | 37 |
| **P1** | Competitive product | 211 |
| **P2** | Premium differentiation | 70 |
| **P3** | Expansion | 36 |
| **P4** | Later / experimental | 17 |

Tiers: 281 Free · 88 Premium · 2 Future.

### The 22 domains (D01–D22)
| Domain | Area | | Domain | Area |
|---|---|---|---|---|
| D01 | Home & Daily Decision | | D12 | Body, Health, Injury & Longevity |
| D02 | Athlete Identity & Status | | D13 | Athlete History & Evolution |
| D03 | Goals, Events & Season | | D14 | Sports, Strength & Cross-Training* |
| D04 | Plans, Calendar & Workout Library | | D15 | Routes, Navigation & Safety |
| D05 | Training Execution & Recording | | D16 | Gear & Maintenance |
| D06 | Activity Analysis | | D17 | Connections, Imports & Data Quality |
| D07 | Heart Rate & Cardiovascular* | | D18 | Reports, Sharing, Privacy & Export |
| D08 | Power & Cycling Performance | | D19 | Training Intelligence, Prediction & AI |
| D09 | Training Load & Capabilities | | D20 | Human Coaching & Specialists |
| D10 | Recovery, Sleep & Wellness | | D21 | Community, Motivation & Competition |
| D11 | Nutrition & Hydration | | D22 | Platform, Account & Operations |

\* D07 and D14 are newer domains introduced by the additions.

### P0 — launch set (37 features, cycling MVP), by screen
- **Train:** main sports (cycling/run/walk/hike), GPS recording
- **Data Sources:** initial Garmin/Strava/Apple Health integration, auto import, manual entry, export (GPX/FIT/CSV), export to Garmin/Wahoo
- **Activity Detail:** activity map, basic analysis (power/HR/cadence/duration/distance), personal records & best efforts
- **Performance:** power-duration curve + PRs (5s/1m/5m/20m), power & HR zones, FTP record/update
- **Insights:** training load + time in zones, activity history, weekly/monthly stats
- **Plan / Calendar:** weekly/monthly calendar, sport calendar, manual structured-workout creation
- **Settings:** account login/recovery, athlete profile, responsive web app, notifications & reminders
- **Reports / Privacy:** per-activity privacy, privacy zones (hide home)

### Additions N001–N014 (prior, founder-requested)
| ID | Pri | Domain | Feature |
|---|---|---|---|
| N001 | P1 | D07 | All-day heart-rate timeline (resting baseline vs daily range vs exercise) |
| N002 | P2 | D07 | Cardiovascular alerts for unusual values / baseline shifts (context, not diagnosis) |
| N003 | P1 | D11 | Per-hour fueling prescription (carbs/sodium/fluids), planned vs consumed |
| N004 | P2 | D11 | Detailed food logging (search/barcode/photo/custom/meals) + macro targets |
| N005 | P2 | D11 | Explain fueling↔sleep↔recovery↔outcome relationships, with confidence |
| N006 | P1 | D14 | Structured strength log (sets/reps/load/rest/RPE, planned vs done) |
| N007 | P2 | D14 | Muscle-group map + strength progression tied to cycling demands |
| N008 | P2 | D10 | Lifestyle log (caffeine/alcohol/late meals/travel) vs sleep/stress/HRV |
| N009 | P2 | D16 | Gear collections, auto-assignment, photos, component hierarchy |
| N010 | P1 | D18 | Configurable home/work privacy zones with map preview |
| N011 | P1 | D18 | Full account export bundle (files, index, profile, corrections) |
| N012 | P2 | D19 | Read-only revocable AI data access with scopes + audit log |
| N013 | P1 | D19 | Personalized next-workout intents (Maintain/Build/Explore/Recover) + route gen |
| N014 | P2 | D12 | Physical therapy / rehab as recordable activity type |

### Additions N015–N025 (this build sprint — what we built / designed now)
| ID | Pri | Domain | Feature | Status |
|---|---|---|---|---|
| N015 | P0 | D09 | Tiered training-load (CTL/ATL/TSB) from best available signal (power→HR→pace→duration), confidence-declared, never requires power | ✅ built |
| N016 | P0 | D09 | Current-era gating: compute load/zones only from the correct era (post 2026-04-28); old "handbrake" data = context, not truth | ✅ built |
| N017 | P0 | D08 | Living zones anchored to threshold tests: zones move with each test, full anchor history ("no more handbrake") | ✅ built |
| N018 | P1 | D08/D19 | Cross-source threshold validation: confirm threshold vs own data + competitors (TrainingPeaks/Garmin) as a confidence statement | designed |
| N019 | P1 | D17 | Direct Garmin sync (real names + structured workouts + wellness) bypassing Strava renaming | 🟡 built, pending deploy |
| N020 | P0 | D17 | Device-artifact filter: ignore fake "rides" from washing/moving the bike | ✅ built |
| N021 | P1 | D04/D06 | Session-type classification from data (Z2/intervals/sweet spot) + executed↔planned matching tolerant to moved days | designed |
| N022 | P1 | D09 | Personal-range interpretation: fatigue/form vs the athlete's own normal band for the current phase | designed |
| N023 | P1 | D19/D04 | Proactive coach prompts from the structured plan ("do your test before Build") | designed |
| N024 | P1 | D10 | Opportunistic recovery capture (morning RHR, nap sleep) — never forced | designed |
| N025 | P2 | D06/D07 | Context/altitude adaptation: home altitude/terrain = normal baseline; adjust only when location changes | designed |

> Per-feature detail lives in `module_registry.json` (371) + `epoch_feature_screen_allocation.json` (372 unique + N001–N014) + this section (N015+). **Keep this list growing** as new capabilities are designed — every idea we discuss becomes a tracked feature here.

---

## 8. Competitive Checklist

### 🔴 MISSING (competitors have it — table stakes)
- [ ] Garmin direct sync — real names, structured workouts, wellness *(in progress)*
- [ ] Push notifications — post-activity, "threshold changed to 168", "test due"
- [ ] Automatic threshold/FTP detection per workout + alert *(have `lt-detect`, not auto/notified)*
- [ ] Recovery in-app — sleep, HRV, resting HR, body battery *(in progress via Garmin)*
- [ ] Push structured workouts **to** Garmin/Edge device (intervals.icu/TR killer feature)
- [ ] Workout library + fast builder (text / AI-assisted)
- [ ] Power-based targets with FTP derived & well-named (when a power meter exists)

### 🟢 HAVE (several are *better* than competitors)
- [x] Load curve CTL/ATL/TSB
- [x] **Capabilities model** — unique
- [x] **Workout Identity / fair comparison** — unique
- [x] Plan Vivo adaptive + event readiness/projection
- [x] **Honesty / declared confidence** — the #1 thing the research says athletes want
- [x] Épocas / 9-year longitudinal identity

### ⛔ DON'T COPY (people hate these)
- Gamified "high-score" metrics for retention
- Inflated / mislabeled FTP
- Forced AI everywhere → ours must be optional and skippable
- 4-week FTP prediction gimmick

---

## 9. Roadmap (prioritized)

**Now — close the data foundation**
1. Deploy + connect **Garmin sync** → unlocks real names, recovery, structured workouts at once.
2. **Notifications** → activity ready, threshold change, test reminder.
3. **Cross-check trust feature** → "your 168 matches TrainingPeaks & Garmin · confidence high".

**Next — make it feel like the product**
4. **English cleanup** of all user-facing strings (kill `escalada`, `carga`, phase Spanglish).
5. Link executed ↔ planned by session **type/intent** (tolerant to moved days), not Strava name.
6. Recovery interpreted by **personal range per block** (you vs you), not a generic TSB table.

**Then — competitive parity + edge**
7. Workout library + fast builder; push workouts to device.
8. Power path (when a meter arrives) — FTP derived, well-named, confidence-first.

**Later — scale**
9. Auth + multi-user + onboarding (today single-user).
10. Garmin official API (as a company, when it reopens) or aggregator; coach-web.

---

## 10. Open Items / Blockers
- **Spanish leaking** into the app from backend data (capability keys, phase text). Real cleanup pass needed.
- **Two computers, sync risk** — deploy happens on the other machine; keep the upload package and repo aligned.
- **Garmin MFA** — if 2FA on, `garmin_sync` needs a one-time local login to mint a token.
- **Phase logic inconsistency** — read phases from the structured plan everywhere (some paths still compute by weeks-to-event).
- **Data gap 2026-06-09 → 26** lives only in Strava (generic names) until Garmin sync runs.

## 11. Security Rules
- Never commit: `.env`, `.env.save`, `private_data/`, `reports/`, raw Garmin ZIP, raw `FIT/GPX/TCX`.
- Secrets (Garmin/Strava/Supabase/ADMIN_TOKEN) live in Railway env vars, never in code.

---
*Maintained as the EPOCH single source of truth. Update when state changes.*
