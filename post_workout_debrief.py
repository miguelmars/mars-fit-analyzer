"""
post_workout_debrief.py
======================
EPOCH P0 — Post-Workout Debrief + Intent vs Reality (build step #3).

Runs over a normalized (#1) and audited (#2) endurance event PLUS the planned intent
(planned workout / coach notes / phase) and athlete context. It does not just say what
happened — it says **whether the session did what it was supposed to do**.

"The Read" (6 modules):
  1. Outcome summary        — one plain sentence.
  2. Intent vs Reality      — Fulfilled / Over-reached / Under / Different stimulus / Unplanned + why.
  3. What it likely built   — the capacity/stimulus.
  4. Evidence + confidence  — with gating: if the audit raised a 🔴 flag, declare it first.
  5. What looked unusual    — drift / HR high for the effort / data gaps.
  6. Next action / watch     — what to do next, and whether the plan needs attention.

Design: framework-free, pure, testable. Depends on `timeline_model` and (for gating)
`data_quality_audit`. Tone: you-vs-you, explain the why, never scold, never auto-change.
Heuristics v1 — honest about confidence; reads coach notes / phase / fatigue before judging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from timeline_model import EventType, TimelineEvent
from data_quality_audit import AuditFlag, gating_note

DEBRIEF_VERSION = "0.1.0"


class Verdict(str, Enum):
    FULFILLED = "fulfilled"                  # met the intent (not just the numbers)
    OVER_REACHED = "over_reached"            # harder than planned (maybe not better)
    UNDER = "under"                          # easier / didn't reach the intended stimulus
    DIFFERENT_STIMULUS = "different_stimulus"  # trained a different system than asked
    UNPLANNED = "unplanned"                  # no plan to compare


# Intensity ladder (shared vocabulary for planned vs actual).
_LEVEL_RECOVERY = 1
_LEVEL_ENDURANCE = 2
_LEVEL_TEMPO = 3
_LEVEL_THRESHOLD = 4
_LEVEL_VO2 = 5

_LEVEL_NAME = {
    _LEVEL_RECOVERY: "recovery",
    _LEVEL_ENDURANCE: "endurance",
    _LEVEL_TEMPO: "tempo",
    _LEVEL_THRESHOLD: "threshold",
    _LEVEL_VO2: "VO2/high-end",
}

_INTENT_WORDS = [
    (_LEVEL_RECOVERY, ("recovery", "recuperacion", "recuperación", "easy", "rest", "regen",
                       "regenerativo", "suave", "descanso", "z1", "zone 1")),
    (_LEVEL_ENDURANCE, ("endurance", "base", "aerobic", "aeróbico", "aerobico", "long",
                        "larga", "steady", "foundation", "z2", "zone 2")),
    (_LEVEL_TEMPO, ("tempo", "sweet spot", "sweetspot", "z3", "zone 3")),
    (_LEVEL_THRESHOLD, ("threshold", "umbral", "ftp", "lt", "sustained", "z4", "zone 4")),
    (_LEVEL_VO2, ("vo2", "vo2max", "interval", "intervals", "intervalos", "anaerobic",
                  "anaeróbico", "sprint", "z5", "zone 5")),
]

_STRUCTURED_WORDS = ("interval", "intervals", "intervalos", "vo2", "sprint")


@dataclass
class PlannedIntent:
    """What the session was supposed to be. All optional."""
    intent_type: Optional[str] = None        # free text; mapped to an intensity level
    description: Optional[str] = None
    coach_notes: Optional[str] = None
    phase: Optional[str] = None              # base / build / peak
    target_duration_s: Optional[int] = None
    target_avg_hr: Optional[float] = None
    target_avg_power: Optional[float] = None
    target_if: Optional[float] = None
    target_tss: Optional[float] = None
    intervals: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AthleteContext:
    lthr: Optional[int] = None
    ftp_w: Optional[int] = None
    hr_max: Optional[int] = None
    fatigue_state: Optional[str] = None      # e.g. "fresh" / "tired" (optional)


@dataclass
class Debrief:
    event_id: str
    verdict: Verdict
    outcome_summary: str
    verdict_reason: str
    likely_built: str
    evidence: Dict[str, Any]
    confidence_level: str
    gating_note: Optional[str]
    unusual: List[str]
    next_actions: List[str]
    plan_needs_attention: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "debrief_version": DEBRIEF_VERSION,
            "event_id": self.event_id,
            "verdict": self.verdict.value,
            "outcome_summary": self.outcome_summary,
            "verdict_reason": self.verdict_reason,
            "likely_built": self.likely_built,
            "evidence": self.evidence,
            "confidence_level": self.confidence_level,
            "gating_note": self.gating_note,
            "unusual": list(self.unusual),
            "next_actions": list(self.next_actions),
            "plan_needs_attention": self.plan_needs_attention,
        }


# ── intensity helpers ────────────────────────────────────────────────────────

def _num(event: TimelineEvent, key: str) -> Optional[float]:
    v = event.payload.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _if_from_power(event: TimelineEvent, ctx: AthleteContext) -> Optional[float]:
    if not ctx.ftp_w:
        return None
    np = _num(event, "normalized_power") or _num(event, "avg_power")
    return (np / ctx.ftp_w) if np else None


def _level_from_if(intensity_factor: float) -> int:
    if intensity_factor < 0.55:
        return _LEVEL_RECOVERY
    if intensity_factor < 0.75:
        return _LEVEL_ENDURANCE
    if intensity_factor < 0.85:
        return _LEVEL_TEMPO
    if intensity_factor < 0.95:
        return _LEVEL_THRESHOLD
    return _LEVEL_VO2


def _level_from_hr_ratio(ratio: float) -> int:
    if ratio < 0.80:
        return _LEVEL_RECOVERY
    if ratio < 0.89:
        return _LEVEL_ENDURANCE
    if ratio < 0.94:
        return _LEVEL_TEMPO
    if ratio < 1.0:
        return _LEVEL_THRESHOLD
    return _LEVEL_VO2


def actual_intensity_level(event: TimelineEvent, ctx: AthleteContext):
    """Return (level, intensity_factor_or_None, basis_str) or (None, None, 'no_intensity_data')."""
    inten = _if_from_power(event, ctx)
    if inten is not None:
        return _level_from_if(inten), round(inten, 3), "power_if"
    avg_hr = _num(event, "avg_hr")
    if avg_hr and ctx.lthr:
        ratio = avg_hr / ctx.lthr
        return _level_from_hr_ratio(ratio), round(ratio, 3), "hr_ratio"
    return None, None, "no_intensity_data"


def planned_intensity_level(intent: PlannedIntent, ctx: AthleteContext) -> Optional[int]:
    # Coach note that says "easy/recovery" overrides everything.
    note = (intent.coach_notes or "").lower()
    if any(w in note for w in ("easy", "suave", "recovery", "recuperaci", "rest", "descanso")):
        return _LEVEL_RECOVERY
    # Explicit numeric targets win next.
    if intent.target_if is not None and ctx.ftp_w:
        return _level_from_if(intent.target_if)
    if intent.target_avg_hr is not None and ctx.lthr:
        return _level_from_hr_ratio(intent.target_avg_hr / ctx.lthr)
    # Otherwise map the words in type/description.
    text = " ".join(filter(None, [intent.intent_type, intent.description, intent.phase])).lower()
    best = None
    for level, words in _INTENT_WORDS:
        if any(w in text for w in words):
            best = level if best is None else max(best, level)
    return best


def _intent_is_structured(intent: PlannedIntent) -> bool:
    if intent.intervals:
        return True
    text = " ".join(filter(None, [intent.intent_type, intent.description])).lower()
    return any(w in text for w in _STRUCTURED_WORDS)


def _looks_structured(event: TimelineEvent) -> bool:
    return len(event.payload.get("laps") or []) >= 4


def _likely_built(level: Optional[int], duration_s: Optional[float]) -> str:
    if level is None:
        return "a training stimulus (intensity unknown — no HR or power on this activity)"
    long_ride = bool(duration_s and duration_s >= 90 * 60)
    return {
        _LEVEL_RECOVERY: "active recovery / blood flow (little new fitness, aids absorption)",
        _LEVEL_ENDURANCE: ("aerobic base & durability" if long_ride else "aerobic base"),
        _LEVEL_TEMPO: "tempo / muscular endurance",
        _LEVEL_THRESHOLD: "threshold / sustainable power",
        _LEVEL_VO2: "VO2 / high-end capacity",
    }[level]


# ── main ─────────────────────────────────────────────────────────────────────

def debrief(
    event: TimelineEvent,
    intent: Optional[PlannedIntent] = None,
    context: Optional[AthleteContext] = None,
    audit_flags: Optional[List[AuditFlag]] = None,
) -> Debrief:
    ctx = context or AthleteContext()
    a_level, intensity, basis = actual_intensity_level(event, ctx)
    duration_s = event.duration_sec or _num(event, "duration_s")
    avg_hr = _num(event, "avg_hr")
    max_hr = _num(event, "max_hr")

    # 4. Evidence + confidence (with gating from the audit layer).
    gate = gating_note(audit_flags) if audit_flags else None
    confidence_level = event.confidence.level.value
    evidence = {
        "intensity_basis": basis,
        "intensity_factor": intensity,
        "intensity_label": _LEVEL_NAME.get(a_level) if a_level else None,
        "avg_hr": avg_hr,
        "lthr": ctx.lthr,
        "avg_power": _num(event, "avg_power"),
        "normalized_power": _num(event, "normalized_power"),
        "ftp_w": ctx.ftp_w,
        "duration_min": round(duration_s / 60.0, 1) if duration_s else None,
        "distance_km": (event.normalized_summary or {}).get("distance_km"),
    }

    # 2. Intent vs Reality.
    verdict, reason, plan_attention = _decide_verdict(event, intent, ctx, a_level)

    # 3. What it likely built.
    built = _likely_built(a_level, duration_s)

    # 1. Outcome summary (plain).
    outcome = _outcome_summary(event, a_level, verdict, built)

    # 5. What looked unusual.
    unusual = _unusual(event, ctx, a_level, avg_hr, max_hr, audit_flags)

    # 6. Next action / what to watch.
    next_actions = _next_actions(verdict, a_level)

    return Debrief(
        event_id=event.event_id,
        verdict=verdict,
        outcome_summary=outcome,
        verdict_reason=reason,
        likely_built=built,
        evidence=evidence,
        confidence_level=confidence_level,
        gating_note=gate,
        unusual=unusual,
        next_actions=next_actions,
        plan_needs_attention=plan_attention,
    )


def _decide_verdict(event, intent, ctx, a_level):
    if intent is None:
        return Verdict.UNPLANNED, "No planned workout or coach note to compare against.", False

    p_level = planned_intensity_level(intent, ctx)

    # Structured plan (intervals/VO2) executed as a steady ride → different stimulus.
    if _intent_is_structured(intent) and not _looks_structured(event) and a_level is not None:
        if a_level < _LEVEL_VO2:
            return (Verdict.DIFFERENT_STIMULUS,
                    "The plan asked for a structured/interval session but the execution looks steady — "
                    "a different system than intended.", True)

    if p_level is None or a_level is None:
        # Fall back to duration if we have a target and no intensity read.
        if intent.target_duration_s and event.duration_sec:
            ratio = event.duration_sec / intent.target_duration_s
            if ratio < 0.8:
                return Verdict.UNDER, "Shorter than the planned duration.", True
            if ratio > 1.2:
                return Verdict.OVER_REACHED, "Longer than the planned duration.", False
            return Verdict.FULFILLED, "Completed close to the planned duration.", False
        return (Verdict.FULFILLED,
                "Logged against the plan, but intensity could not be measured (no HR/power).", False)

    if a_level == p_level:
        return Verdict.FULFILLED, f"Execution matched the intended {_LEVEL_NAME[p_level]} effort.", False

    if a_level > p_level:
        if p_level <= _LEVEL_ENDURANCE and a_level >= _LEVEL_TEMPO:
            return (Verdict.OVER_REACHED,
                    f"Planned an easy {_LEVEL_NAME[p_level]} effort but executed at "
                    f"{_LEVEL_NAME[a_level]} — harder, not necessarily better; it can cost your key sessions.",
                    True)
        return (Verdict.OVER_REACHED,
                f"Executed harder ({_LEVEL_NAME[a_level]}) than the planned {_LEVEL_NAME[p_level]}.", False)

    # a_level < p_level
    return (Verdict.UNDER,
            f"Executed easier ({_LEVEL_NAME[a_level]}) than the planned {_LEVEL_NAME[p_level]} — "
            f"the intended stimulus may not have been reached.", True)


def _outcome_summary(event, a_level, verdict, built):
    name = event.payload.get("original_name") or (event.normalized_summary or {}).get("name") or "Session"
    if a_level is None:
        return f"{name}: completed; intensity unknown (no HR/power), so this is a low-confidence read."
    label = _LEVEL_NAME[a_level]
    tag = {
        Verdict.FULFILLED: "on-target",
        Verdict.OVER_REACHED: "harder than planned",
        Verdict.UNDER: "easier than planned",
        Verdict.DIFFERENT_STIMULUS: "a different stimulus than planned",
        Verdict.UNPLANNED: "unplanned",
    }[verdict]
    return f"{name}: a {label} effort, {tag} — likely built {built.split(' (')[0]}."


def _unusual(event, ctx, a_level, avg_hr, max_hr, audit_flags):
    out: List[str] = []
    if ctx.lthr and avg_hr and avg_hr > ctx.lthr:
        out.append(f"Average HR ({int(avg_hr)}) was above your threshold ({ctx.lthr}) — a hard session.")
    if avg_hr and max_hr and max_hr - avg_hr <= 8:
        out.append("HR stayed very close to its max for most of the session.")
    if "derived_metrics" in event.confidence.data_flags:
        out.append("Distance/elevation were derived from the track, not measured by a device.")
    if audit_flags:
        for f in audit_flags:
            if f.severity.value == "high":
                out.append(f"Data audit: {f.message}")
    if event.confidence.level.value == "low" and not out:
        out.append("Limited data on this activity — treat the read as low confidence.")
    return out


def _next_actions(verdict, a_level):
    if verdict == Verdict.OVER_REACHED:
        return ["Ease the next session so this extra load doesn't cost your key workouts.",
                "If this keeps happening, the plan's easy days may be drifting too hard."]
    if verdict == Verdict.UNDER:
        return ["You had more in the tank — if the plan wanted more, push the next one.",
                "Check whether fatigue or time cut it short."]
    if verdict == Verdict.DIFFERENT_STIMULUS:
        return ["Realign with the planned session type if the goal needs that specific stimulus."]
    if verdict == Verdict.UNPLANNED:
        return ["No plan to compare; logged what it built. Add an intent to get an Intent-vs-Reality read."]
    return ["On target — continue as planned."]


__all__ = [
    "DEBRIEF_VERSION", "Verdict", "PlannedIntent", "AthleteContext", "Debrief",
    "debrief", "actual_intensity_level", "planned_intensity_level",
]
