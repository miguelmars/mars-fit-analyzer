"""
mars_load.py — Training load model (Fitness / Fatigue / Form)
=============================================================

Pure, dependency-free functions that turn an athlete's workouts into the
classic training-load curve:

    CTL = Fitness  (slow, ~42-day average)
    ATL = Fatigue  (fast, ~7-day average)
    TSB = Form     (CTL - ATL, "fresh or cooked")

Design notes live in docs/TRAINING_LOAD_MODEL.md. Key ideas:

  * No DB access here. Feed it data, get numbers back. Easy to unit-test and
    reuse from any endpoint (Today, Progress, Plan Vivo, Calendar, AI Plan Draft).
  * Never assumes a power meter. `session_tss` is TIERED: it uses the best
    signal each athlete actually has (power -> heart rate -> pace -> duration)
    and reports which method and confidence it used.
  * Math adapted from the TrainingPeaks model. We compute it ourselves from
    data we own, so there is no third-party login to break.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

# --- Constants ---------------------------------------------------------------

CTL_DAYS = 42  # Fitness time constant (chronic load).
ATL_DAYS = 7   # Fatigue time constant (acute load).

# When only duration is known, assume a moderate steady effort.
# IF 0.65 means ~42 TSS per hour — conservative on purpose.
DURATION_ONLY_IF = 0.65


# --- Step 1: per-session TSS (tiered) ---------------------------------------

def session_tss(session: Dict[str, Any], thresholds: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate Training Stress Score for one session using the best data available.

    Args:
        session: a workout with any of:
            duration_s (int, required for every tier),
            normalized_power_w (float), avg_hr_bpm (float),
            avg_speed_kmh (float), distance_km (float).
        thresholds: athlete reference values, any of:
            ftp_watts (float), threshold_hr (float), threshold_speed_kmh (float).

    Returns:
        { "tss": float, "method": str, "confidence": str }
        method   in {"power", "heart_rate", "pace", "duration", "none"}
        confidence in {"high", "medium", "medium_low", "low", "none"}
    """
    duration_s = session.get("duration_s") or 0
    hours = duration_s / 3600.0
    if hours <= 0:
        return {"tss": 0.0, "method": "none", "confidence": "none"}

    ftp = thresholds.get("ftp_watts")
    threshold_hr = thresholds.get("threshold_hr")
    threshold_speed = thresholds.get("threshold_speed_kmh")

    np_w = session.get("normalized_power_w")
    avg_hr = session.get("avg_hr_bpm")
    avg_speed = session.get("avg_speed_kmh")

    # Tier 1 — Power. Most accurate. 1 h at FTP == 100 TSS.
    if np_w and ftp and ftp > 0:
        intensity = np_w / ftp
        return {
            "tss": round(hours * intensity ** 2 * 100, 1),
            "method": "power",
            "confidence": "high",
        }

    # Tier 2 — Heart rate (hrTSS). 1 h at threshold HR == 100 TSS.
    if avg_hr and threshold_hr and threshold_hr > 0:
        intensity = avg_hr / threshold_hr
        return {
            "tss": round(hours * intensity ** 2 * 100, 1),
            "method": "heart_rate",
            "confidence": "medium",
        }

    # Tier 3 — Pace (running without HR). 1 h at threshold pace == 100 TSS.
    if avg_speed and threshold_speed and threshold_speed > 0:
        intensity = avg_speed / threshold_speed
        return {
            "tss": round(hours * intensity ** 2 * 100, 1),
            "method": "pace",
            "confidence": "medium_low",
        }

    # Tier 4 — Duration only. Last resort, assume a moderate effort.
    return {
        "tss": round(hours * DURATION_ONLY_IF ** 2 * 100, 1),
        "method": "duration",
        "confidence": "low",
    }


# --- Step 2: continuous daily TSS series ------------------------------------

def aggregate_daily_tss(
    dated_tss: Sequence[Dict[str, Any]],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Collapse per-session TSS into one value per calendar day, gaps filled with 0.

    Args:
        dated_tss: items shaped like { "date": date|"YYYY-MM-DD", "tss": float }.
                   Several sessions on the same day are summed.
        start, end: optional bounds. Default to the min/max date present.

    Returns:
        Continuous daily list [{ "date": "YYYY-MM-DD", "tss": float }, ...]
        with NO missing days (rest days appear as 0) — required for a correct curve.
    """
    totals: Dict[date, float] = {}
    for item in dated_tss:
        d = item["date"]
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        totals[d] = totals.get(d, 0.0) + float(item.get("tss") or 0.0)

    if not totals and not (start and end):
        return []

    lo = start or min(totals)
    hi = end or max(totals)

    series: List[Dict[str, Any]] = []
    day = lo
    while day <= hi:
        series.append({"date": day.isoformat(), "tss": round(totals.get(day, 0.0), 1)})
        day += timedelta(days=1)
    return series


# --- Step 3: the fitness curve ----------------------------------------------

def fitness_curve(
    daily_tss: Sequence[Dict[str, Any]],
    initial_ctl: float = 0.0,
    initial_atl: float = 0.0,
) -> List[Dict[str, Any]]:
    """Compute CTL / ATL / TSB for each day from a continuous daily TSS series.

        CTL_today = CTL_prev + (TSS_today - CTL_prev) / 42
        ATL_today = ATL_prev + (TSS_today - ATL_prev) / 7
        TSB_today = CTL_prev - ATL_prev      (yesterday's fitness minus fatigue)

    Note: CTL needs a ramp-up of roughly 6 weeks of continuous data to be
    meaningful. If you start from 0 with little history, early values read low —
    that is expected, not a bug. Seed initial_ctl/initial_atl if you have a prior.

    Returns:
        [{ "date", "ctl", "atl", "tsb" }, ...] aligned with daily_tss.
    """
    ctl_prev = initial_ctl
    atl_prev = initial_atl
    out: List[Dict[str, Any]] = []

    for day in daily_tss:
        tss = float(day.get("tss") or 0.0)
        tsb = ctl_prev - atl_prev  # form reflects yesterday's balance
        ctl = ctl_prev + (tss - ctl_prev) / CTL_DAYS
        atl = atl_prev + (tss - atl_prev) / ATL_DAYS
        out.append({
            "date": day["date"],
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
        })
        ctl_prev, atl_prev = ctl, atl

    return out


# --- Step 4: interpret Form (TSB) -------------------------------------------

def interpret_tsb(tsb: float) -> Dict[str, str]:
    """Turn a TSB number into a friendly status. English, user-facing."""
    if tsb > 25:
        status, note = "Very fresh", "Detraining risk — you may be losing fitness."
    elif tsb > 10:
        status, note = "Fresh", "Race-ready, well rested."
    elif tsb > 0:
        status, note = "Neutral", "Normal training balance."
    elif tsb > -10:
        status, note = "Tired", "Absorbing load — this is normal in a build."
    elif tsb > -25:
        status, note = "Very tired", "High fatigue — watch recovery."
    else:
        status, note = "Exhausted", "Overtraining risk — back off and recover."
    return {"status": status, "note": note}


# --- Convenience: full pipeline + current state -----------------------------

def lowest_confidence(methods: Sequence[str]) -> str:
    """Overall confidence of a curve = its weakest input (honest by design)."""
    order = ["high", "medium", "medium_low", "low", "none"]
    present = [m for m in methods if m in order]
    if not present:
        return "none"
    return max(present, key=order.index)


def build_training_load(
    sessions: Sequence[Dict[str, Any]],
    thresholds: Dict[str, Any],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> Dict[str, Any]:
    """End-to-end helper: sessions -> { current, daily_data, confidence }.

    Each session must include a "date" plus whatever metrics it has
    (see session_tss). This is the shape the /gpt/training-load endpoint returns.
    """
    dated_tss = []
    confidences = []
    method_summary: Dict[str, int] = {}
    for s in sessions:
        r = session_tss(s, thresholds)
        confidences.append(r["confidence"])
        method_summary[r["method"]] = method_summary.get(r["method"], 0) + 1
        dated_tss.append({"date": s["date"], "tss": r["tss"]})

    daily = aggregate_daily_tss(dated_tss, start=start, end=end)
    curve = fitness_curve(daily)

    current = None
    if curve:
        last = curve[-1]
        current = {**last, **interpret_tsb(last["tsb"])}

    return {
        "current": current,
        "daily_data": curve,
        "confidence": lowest_confidence(confidences),
        "method_summary": method_summary,
    }


# --- Tiny self-test (illustrative, safe to run) -----------------------------

if __name__ == "__main__":
    # HR-only athlete (Miguel's case): no power, just duration + avg HR.
    thresholds = {"threshold_hr": 165}
    sample = [
        {"date": "2026-06-15", "duration_s": 3600, "avg_hr_bpm": 140},
        {"date": "2026-06-16", "duration_s": 5400, "avg_hr_bpm": 150},
        {"date": "2026-06-18", "duration_s": 3000, "avg_hr_bpm": 160},
        {"date": "2026-06-20", "duration_s": 9000, "avg_hr_bpm": 137},
        {"date": "2026-06-23", "duration_s": 3774, "avg_hr_bpm": 141},
    ]
    result = build_training_load(sample, thresholds)
    print("method of first session:", session_tss(sample[0], thresholds))
    print("current:", result["current"])
    print("overall confidence:", result["confidence"])
