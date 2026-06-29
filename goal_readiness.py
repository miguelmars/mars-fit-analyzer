"""
goal_readiness.py
================
EPOCH P0 — Goal Readiness / Capability Gap (build step #4, the reality check).

Closes the MVP intelligence chain: #1 Ingestion (clean data) → #2 Audit (confidence) →
#3 Debrief (what each session built) → **#4 Goal Readiness** (am I going to make it?).

Answers honestly: "Are you ready for your goal, what's missing, and with what confidence?"
Output is a **range with confidence**, never a false-precision single number. It never
promises and never scares — if you're short, it says *what's missing and what to do*.

Design: framework-free, pure, testable. Reads the timeline (endurance events) for volume /
consistency / durability / climbing, the goal demand, and the audit's data health (#2) to
gate confidence. Heuristics v1 — honest about what it cannot assess (e.g. fueling).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from timeline_model import EventType, TimelineEvent, now_utc

try:  # optional dependency on the audit layer for confidence gating
    from data_quality_audit import AthleteDataHealth
except Exception:  # pragma: no cover
    AthleteDataHealth = Any  # type: ignore

READINESS_VERSION = "0.1.0"

RECENT_WINDOW_DAYS = 42          # ~6 weeks defines "recent" volume/consistency
RECENT_WEEKS = RECENT_WINDOW_DAYS / 7.0
MIN_ACTIVE_WEEKS = 3             # below this → needs_history
GOOD_CAP = 0.7                   # capability score below this is a blocker


class ReadinessState(str, Enum):
    READY_RANGE = "ready_range"          # a verdict (range) was produced
    NEEDS_HISTORY = "needs_history"      # not enough history for a solid verdict
    NO_TARGET = "no_target"              # no goal set
    EVENT_PASSED = "event_passed"        # the goal date is in the past
    LOW_CONFIDENCE = "low_confidence"    # (reserved) — normally expressed via confidence_level
    INJURED_RETURNING = "injured_returning"  # (reserved) special context


class Capability(str, Enum):
    ENDURANCE = "endurance"
    DURABILITY = "durability"
    CLIMBING = "climbing"
    CONSISTENCY = "consistency"
    SUSTAINED_POWER = "sustained_power"
    FUELING = "fueling"


@dataclass
class Goal:
    name: Optional[str] = None
    event_date: Optional[datetime] = None
    sport: Optional[str] = None
    target_distance_m: Optional[float] = None
    target_elevation_m: Optional[float] = None
    target_duration_s: Optional[int] = None
    target_weekly_hours: Optional[float] = None
    description: Optional[str] = None


@dataclass
class Readiness:
    state: ReadinessState
    summary: str
    readiness_low_pct: Optional[int] = None
    readiness_high_pct: Optional[int] = None
    confidence_level: str = "low"
    weeks_remaining: Optional[float] = None
    weakest_capability: Optional[str] = None
    capability_scores: Dict[str, Optional[float]] = field(default_factory=dict)
    volume_recent_h_per_week: Optional[float] = None
    volume_target_h_per_week: Optional[float] = None
    volume_gap_h_per_week: Optional[float] = None
    blockers: List[str] = field(default_factory=list)
    next_proof_point: Optional[str] = None
    risks: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "readiness_version": READINESS_VERSION,
            "state": self.state.value,
            "summary": self.summary,
            "readiness_low_pct": self.readiness_low_pct,
            "readiness_high_pct": self.readiness_high_pct,
            "confidence_level": self.confidence_level,
            "weeks_remaining": self.weeks_remaining,
            "weakest_capability": self.weakest_capability,
            "capability_scores": self.capability_scores,
            "volume_recent_h_per_week": self.volume_recent_h_per_week,
            "volume_target_h_per_week": self.volume_target_h_per_week,
            "volume_gap_h_per_week": self.volume_gap_h_per_week,
            "blockers": list(self.blockers),
            "next_proof_point": self.next_proof_point,
            "risks": list(self.risks),
            "missing": list(self.missing),
        }


# ── helpers ──────────────────────────────────────────────────────────────────

def _num(event: TimelineEvent, key: str) -> Optional[float]:
    v = event.payload.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _endurance(events: List[TimelineEvent]) -> List[TimelineEvent]:
    return [e for e in events if e.event_type == EventType.ENDURANCE_WORKOUT
            and e.status.value in ("active",) and e.start_time]


def _target_duration_h(goal: Goal) -> Optional[float]:
    if goal.target_duration_s:
        return goal.target_duration_s / 3600.0
    if goal.target_distance_m:
        # crude fallback speed if only distance is known (cycling ~25 km/h, else ~10 km/h)
        speed_kmh = 25.0 if (goal.sport or "").lower() in ("cycling", "ride") else 10.0
        return (goal.target_distance_m / 1000.0) / speed_kmh
    return None


def _target_weekly_hours(goal: Goal, tgt_dur_h: Optional[float]) -> Optional[float]:
    if goal.target_weekly_hours:
        return goal.target_weekly_hours
    if tgt_dur_h:
        return max(tgt_dur_h * 1.5, tgt_dur_h + 2.0)
    return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ── main ─────────────────────────────────────────────────────────────────────

def assess(
    events: List[TimelineEvent],
    goal: Optional[Goal],
    data_health: Optional["AthleteDataHealth"] = None,
    as_of: Optional[datetime] = None,
) -> Readiness:
    as_of = as_of or now_utc()

    if goal is None or goal.event_date is None:
        return Readiness(ReadinessState.NO_TARGET,
                         "No goal set — add an event and date to get a readiness reality-check.")

    weeks_remaining = round((goal.event_date - as_of).days / 7.0, 1)
    if goal.event_date < as_of:
        return Readiness(ReadinessState.EVENT_PASSED,
                         f"'{goal.name or 'event'}' is in the past — set a new goal to assess readiness.",
                         weeks_remaining=weeks_remaining)

    endur = _endurance(events)
    recent = [e for e in endur if e.start_time >= as_of - timedelta(days=RECENT_WINDOW_DAYS)]
    active_weeks = len({e.start_time.isocalendar()[:2] for e in recent})

    if active_weeks < MIN_ACTIVE_WEEKS:
        need = MIN_ACTIVE_WEEKS - active_weeks
        return Readiness(
            ReadinessState.NEEDS_HISTORY,
            f"Not enough recent history yet ({active_weeks} active week(s)). "
            f"With ~{need} more week(s) of data I can give a solid readiness verdict.",
            weeks_remaining=weeks_remaining,
            missing=["recent training history"],
        )

    # ── volume / consistency / durability / climbing from the timeline ──
    recent_hours = sum((e.duration_sec or 0) for e in recent) / 3600.0
    recent_weekly_hours = round(recent_hours / RECENT_WEEKS, 1)
    consistency = _clamp01(active_weeks / RECENT_WEEKS)
    longest_recent_h = max(((e.duration_sec or 0) for e in recent), default=0) / 3600.0
    recent_max_elev = max((_num(e, "elevation_gain_m") or 0.0 for e in recent), default=0.0)

    tgt_dur_h = _target_duration_h(goal)
    tgt_weekly_h = _target_weekly_hours(goal, tgt_dur_h)

    scores: Dict[str, Optional[float]] = {}
    missing: List[str] = []

    scores[Capability.ENDURANCE.value] = (
        _clamp01(recent_weekly_hours / tgt_weekly_h) if tgt_weekly_h else None)
    scores[Capability.DURABILITY.value] = (
        _clamp01(longest_recent_h / tgt_dur_h) if tgt_dur_h else None)
    scores[Capability.CLIMBING.value] = (
        _clamp01(recent_max_elev / goal.target_elevation_m) if goal.target_elevation_m else None)
    scores[Capability.CONSISTENCY.value] = round(consistency, 2)
    # Not assessable from imported activity data in P0 → honest data limitations.
    scores[Capability.SUSTAINED_POWER.value] = None
    scores[Capability.FUELING.value] = None
    missing.append("Fueling not tracked → fueling readiness not assessed")
    missing.append("No power/intensity target → sustained-power readiness not assessed")
    if not goal.target_elevation_m:
        missing.append("No target elevation → climbing demand not assessed")

    # ── overall readiness (weighted over available capabilities) ──
    weights = {Capability.ENDURANCE.value: 0.30, Capability.DURABILITY.value: 0.30,
               Capability.CLIMBING.value: 0.20, Capability.CONSISTENCY.value: 0.20}
    avail = {k: v for k, v in scores.items() if v is not None and k in weights}
    if avail:
        wsum = sum(weights[k] for k in avail)
        mid = sum(scores[k] * weights[k] for k in avail) / wsum
    else:
        mid = consistency
    mid_pct = int(round(mid * 100))

    # ── confidence + gating from the audit (#2) ──
    confidence_level = "high" if active_weeks >= 5 else "medium"
    risks: List[str] = []
    red = False
    if data_health is not None:
        if getattr(data_health, "high_count", 0) and data_health.high_count > 0:
            red = True
        if getattr(data_health, "zones_reliable", None) is False:
            red = True
    if red:
        confidence_level = "low"
        risks.append("Data quality issues (zones/HR/FTP) lower the confidence of this verdict — validate first.")
    # data-limited capabilities cap confidence at medium
    if confidence_level == "high" and (scores[Capability.SUSTAINED_POWER.value] is None):
        confidence_level = "medium"

    band = {"high": 7, "medium": 12, "low": 18}[confidence_level]
    low_pct = int(_clamp01((mid_pct - band) / 100.0) * 100)
    high_pct = int(_clamp01((mid_pct + band) / 100.0) * 100)

    # ── weakest capability + blockers ──
    scorable = {k: v for k, v in scores.items() if v is not None}
    weakest = min(scorable, key=scorable.get) if scorable else None
    blockers: List[str] = [k for k, v in scorable.items() if v < GOOD_CAP]
    if red:
        blockers.append("data_quality")

    # ── volume gap ──
    volume_gap = round(tgt_weekly_h - recent_weekly_hours, 1) if tgt_weekly_h else None

    # ── next proof point (actionable) ──
    next_pp = _next_proof_point(weakest, goal, tgt_dur_h, tgt_weekly_h, red)

    name = goal.name or "your goal"
    summary = (f"Readiness {low_pct}–{high_pct}% for {name} in {weeks_remaining:.0f} weeks; "
               f"weakest: {weakest or 'n/a'}. Confidence: {confidence_level}.")

    return Readiness(
        state=ReadinessState.READY_RANGE,
        summary=summary,
        readiness_low_pct=low_pct,
        readiness_high_pct=high_pct,
        confidence_level=confidence_level,
        weeks_remaining=weeks_remaining,
        weakest_capability=weakest,
        capability_scores=scores,
        volume_recent_h_per_week=recent_weekly_hours,
        volume_target_h_per_week=tgt_weekly_h,
        volume_gap_h_per_week=volume_gap,
        blockers=blockers,
        next_proof_point=next_pp,
        risks=risks,
        missing=missing,
    )


def _next_proof_point(weakest, goal: Goal, tgt_dur_h, tgt_weekly_h, red: bool) -> str:
    if red:
        return "Re-test your threshold/FTP so zones are reliable — then the readiness verdict can be trusted."
    if weakest == Capability.DURABILITY.value and tgt_dur_h:
        return f"Do one long {goal.sport or 'session'} of ~{tgt_dur_h * 0.8:.0f}h in the next 2–3 weeks."
    if weakest == Capability.CLIMBING.value and goal.target_elevation_m:
        return f"Train a route with ~{goal.target_elevation_m:.0f} m of climbing to match the demand."
    if weakest == Capability.ENDURANCE.value and tgt_weekly_h:
        return f"Build weekly volume toward ~{tgt_weekly_h:.0f} h/week."
    if weakest == Capability.CONSISTENCY.value:
        return "String together 3–4 consistent weeks before the next check."
    return "Keep the current block; re-check readiness in 2–3 weeks."


__all__ = [
    "READINESS_VERSION", "ReadinessState", "Capability", "Goal", "Readiness", "assess",
]
