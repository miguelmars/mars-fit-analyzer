"""
recovery_context.py
===================
EPOCH #5 — Recovery Context / Recovery Reserve.

Answers: "What can I absorb today, and why?" It consumes the P0 timeline, data
health, and capability matrix without importing FastAPI, DB clients, Garmin, Strava,
or UI code. It is useful with imperfect data and never pretends missing sleep/HRV
signals exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from timeline_model import EventStatus, EventType, TimelineEvent, now_utc
from data_quality_audit import AthleteDataHealth
from data_capability_matrix import CapabilityMatrix, MetricStatus

RECOVERY_CONTEXT_VERSION = "0.1.0"


class RecoveryState(str, Enum):
    READY = "ready"
    ESTIMATED = "estimated"
    NEEDS_SIGNAL = "needs_signal"
    NEEDS_HISTORY = "needs_history"
    CONFLICT = "conflict"
    RED_FLAG = "red_flag"


class TrainingRecommendation(str, Enum):
    REST_OR_RECOVER = "rest_or_recover"
    EASY_ONLY = "easy_only"
    AEROBIC_OK = "aerobic_ok"
    QUALITY_POSSIBLE = "quality_possible"
    RACE_READY = "race_ready"
    UNKNOWN = "unknown"


@dataclass
class WellnessSignal:
    """Optional recovery context from a wearable or manual check-in."""

    date: datetime
    source: str = "manual_check_in"
    sleep_hours: Optional[float] = None
    sleep_quality: Optional[str] = None
    sleep_score: Optional[float] = None
    resting_hr: Optional[float] = None
    hrv_ms: Optional[float] = None
    fatigue_1_10: Optional[float] = None
    soreness_1_10: Optional[float] = None
    stress_1_10: Optional[float] = None
    illness: bool = False
    injury: bool = False
    note: Optional[str] = None


@dataclass
class PlannedWorkoutContext:
    title: Optional[str] = None
    intensity: Optional[str] = None
    duration_s: Optional[int] = None


@dataclass
class RecoveryDriver:
    key: str
    direction: str
    label: str
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "direction": self.direction,
            "label": self.label,
            "evidence": self.evidence,
        }


@dataclass
class RecoveryContext:
    state: RecoveryState
    summary: str
    recovery_low_pct: Optional[int] = None
    recovery_high_pct: Optional[int] = None
    confidence_level: str = "low"
    training_recommendation: TrainingRecommendation = TrainingRecommendation.UNKNOWN
    drivers: List[RecoveryDriver] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    data_sources: List[Dict[str, str]] = field(default_factory=list)
    next_action: str = ""
    gating_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recovery_version": RECOVERY_CONTEXT_VERSION,
            "state": self.state.value,
            "summary": self.summary,
            "recovery_low_pct": self.recovery_low_pct,
            "recovery_high_pct": self.recovery_high_pct,
            "confidence_level": self.confidence_level,
            "training_recommendation": self.training_recommendation.value,
            "drivers": [d.to_dict() for d in self.drivers],
            "blockers": list(self.blockers),
            "risks": list(self.risks),
            "missing": list(self.missing),
            "data_sources": list(self.data_sources),
            "next_action": self.next_action,
            "gating_note": self.gating_note,
        }


def _endur(events: List[TimelineEvent]) -> List[TimelineEvent]:
    return [
        e for e in events
        if e.event_type == EventType.ENDURANCE_WORKOUT
        and e.status == EventStatus.ACTIVE
        and e.start_time
    ]


def _hours(events: List[TimelineEvent]) -> float:
    total = 0.0
    for e in events:
        dur = e.duration_sec or e.payload.get("duration_s")
        try:
            total += float(dur or 0) / 3600.0
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def _latest_wellness(wellness: List[WellnessSignal]) -> Optional[WellnessSignal]:
    if not wellness:
        return None
    return sorted(wellness, key=lambda w: w.date, reverse=True)[0]


def _baseline(values: List[float]) -> Optional[float]:
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 5:
        return None
    return sum(clean) / len(clean)


def _matrix_status(matrix: Optional[CapabilityMatrix], key: str) -> Optional[MetricStatus]:
    if matrix is None:
        return None
    for row in matrix.rows:
        if row.key == key:
            return row.status
    return None


def _has_sleep(wellness: List[WellnessSignal]) -> bool:
    return any(w.sleep_hours is not None or w.sleep_score is not None for w in wellness)


def _has_hrv(wellness: List[WellnessSignal]) -> bool:
    return any(w.hrv_ms is not None for w in wellness)


def _source_list(wellness: List[WellnessSignal]) -> List[Dict[str, str]]:
    sources = [{"source": "timeline", "confidence": "medium"}]
    if wellness:
        unique = sorted({w.source for w in wellness})
        sources.extend({"source": s, "confidence": "medium"} for s in unique)
    return sources


def assess_recovery(
    events: List[TimelineEvent],
    *,
    data_health: Optional[AthleteDataHealth] = None,
    capability: Optional[CapabilityMatrix] = None,
    wellness: Optional[List[WellnessSignal]] = None,
    recent_debriefs: Optional[List[Dict[str, Any]]] = None,
    planned_workout: Optional[PlannedWorkoutContext] = None,
    as_of: Optional[datetime] = None,
) -> RecoveryContext:
    as_of = as_of or now_utc()
    wellness = wellness or []
    recent_debriefs = recent_debriefs or []
    endur = _endur(events)

    if len(endur) < 3:
        return RecoveryContext(
            state=RecoveryState.NEEDS_HISTORY,
            summary="Not enough recent training history to estimate recovery honestly.",
            confidence_level="low",
            training_recommendation=TrainingRecommendation.UNKNOWN,
            missing=["more training history"],
            data_sources=_source_list(wellness),
            next_action="Import more recent activities before trusting a recovery recommendation.",
        )

    recent = [e for e in endur if e.start_time and e.start_time >= as_of - timedelta(days=7)]
    baseline = [
        e for e in endur
        if e.start_time and as_of - timedelta(days=35) <= e.start_time < as_of - timedelta(days=7)
    ]
    recent_h = _hours(recent)
    baseline_week_h = round(_hours(baseline) / 4.0, 2) if baseline else 0.0
    load_ratio = recent_h / baseline_week_h if baseline_week_h > 0 else None

    drivers: List[RecoveryDriver] = []
    blockers: List[str] = []
    risks: List[str] = []
    missing: List[str] = []
    score = 68.0

    if load_ratio is None:
        missing.append("baseline load history")
        score -= 8
    elif load_ratio >= 1.35:
        drivers.append(RecoveryDriver(
            "recent_load", "negative", "High recent load",
            f"Last 7 days are {recent_h:.1f}h vs a {baseline_week_h:.1f}h weekly baseline."
        ))
        risks.append("recent load is above baseline")
        score -= 16
    elif load_ratio <= 0.75:
        drivers.append(RecoveryDriver(
            "recent_load", "positive", "Load is lighter than baseline",
            f"Last 7 days are {recent_h:.1f}h vs a {baseline_week_h:.1f}h weekly baseline."
        ))
        score += 6
    else:
        drivers.append(RecoveryDriver(
            "recent_load", "neutral", "Load near baseline",
            f"Last 7 days are {recent_h:.1f}h vs a {baseline_week_h:.1f}h weekly baseline."
        ))

    latest = _latest_wellness(wellness)
    sleep_present = _has_sleep(wellness)
    hrv_present = _has_hrv(wellness)

    if not sleep_present:
        missing.append("sleep")
    if not hrv_present:
        missing.append("HRV")

    if latest:
        if latest.illness or latest.injury:
            blockers.append("illness_or_injury")
            drivers.append(RecoveryDriver(
                "illness_injury", "negative", "Illness or injury reported",
                "Manual check-in reported illness or injury."
            ))
            score -= 28
        if latest.fatigue_1_10 is not None:
            if latest.fatigue_1_10 >= 8:
                drivers.append(RecoveryDriver(
                    "fatigue", "negative", "High fatigue",
                    f"Latest check-in fatigue is {latest.fatigue_1_10}/10."
                ))
                score -= 18
            elif latest.fatigue_1_10 <= 3:
                drivers.append(RecoveryDriver(
                    "fatigue", "positive", "Low fatigue",
                    f"Latest check-in fatigue is {latest.fatigue_1_10}/10."
                ))
                score += 5
        if latest.soreness_1_10 is not None and latest.soreness_1_10 >= 8:
            drivers.append(RecoveryDriver(
                "soreness", "negative", "High soreness",
                f"Latest check-in soreness is {latest.soreness_1_10}/10."
            ))
            score -= 10
        if latest.sleep_hours is not None:
            if latest.sleep_hours < 6:
                drivers.append(RecoveryDriver(
                    "sleep_duration", "negative", "Short sleep",
                    f"Last sleep was {latest.sleep_hours:.1f}h."
                ))
                score -= 12
            elif latest.sleep_hours >= 7:
                drivers.append(RecoveryDriver(
                    "sleep_duration", "positive", "Sleep looks sufficient",
                    f"Last sleep was {latest.sleep_hours:.1f}h."
                ))
                score += 6

    rhr_values = [w.resting_hr for w in wellness if w.resting_hr is not None]
    rhr_base = _baseline(rhr_values[:-1])
    if latest and latest.resting_hr is not None and rhr_base is not None:
        delta = latest.resting_hr - rhr_base
        if delta >= 6:
            drivers.append(RecoveryDriver(
                "resting_hr", "negative", "Resting HR elevated",
                f"Latest RHR is {latest.resting_hr:.0f} bpm, about {delta:.0f} bpm above baseline."
            ))
            score -= 12
        elif delta <= 2:
            drivers.append(RecoveryDriver(
                "resting_hr", "positive", "Resting HR stable",
                f"Latest RHR is {latest.resting_hr:.0f} bpm, close to baseline."
            ))
            score += 4
    elif not any(w.resting_hr is not None for w in wellness):
        missing.append("resting HR")

    hrv_values = [w.hrv_ms for w in wellness if w.hrv_ms is not None]
    hrv_base = _baseline(hrv_values[:-1])
    if latest and latest.hrv_ms is not None and hrv_base is not None:
        drop = (hrv_base - latest.hrv_ms) / hrv_base if hrv_base else 0
        if drop >= 0.15:
            drivers.append(RecoveryDriver(
                "hrv_trend", "negative", "HRV below baseline",
                f"Latest HRV is {latest.hrv_ms:.0f} ms vs ~{hrv_base:.0f} ms baseline."
            ))
            score -= 12
        else:
            drivers.append(RecoveryDriver(
                "hrv_trend", "positive", "HRV near baseline",
                f"Latest HRV is {latest.hrv_ms:.0f} ms vs ~{hrv_base:.0f} ms baseline."
            ))
            score += 4

    for d in recent_debriefs:
        if str(d.get("verdict", "")).lower() == "over_reached":
            drivers.append(RecoveryDriver(
                "debrief_overreach", "negative", "Recent overreach",
                "A recent debrief marked the session as harder than intended."
            ))
            risks.append("recent overreach")
            score -= 8
            break

    gating = None
    if data_health and data_health.high_count > 0:
        gating = "Recovery confidence is lowered because data quality has unresolved red flags."
        risks.append("data quality red flag")
        score -= 10

    if capability:
        hrv_status = _matrix_status(capability, "hrv_status")
        if hrv_status == MetricStatus.NEEDS_SIGNAL and "HRV" not in missing:
            missing.append("HRV")
        if _matrix_status(capability, "recovery_reserve") == MetricStatus.ESTIMATE_ONLY:
            risks.append("recovery reserve is estimate-only without richer recovery signals")

    score = max(5, min(95, score))
    low = int(max(0, round(score - 8)))
    high = int(min(100, round(score + 8)))

    confidence = "low"
    if len(endur) >= 5 and (sleep_present or any(w.resting_hr is not None for w in wellness)):
        confidence = "medium"
    if len(endur) >= 10 and sleep_present and hrv_present and any(w.resting_hr is not None for w in wellness):
        confidence = "high"
    if data_health and data_health.high_count > 0:
        confidence = "low"

    state = RecoveryState.ESTIMATED
    if blockers:
        state = RecoveryState.RED_FLAG
    elif _conflicting(latest):
        state = RecoveryState.CONFLICT
        risks.append("subjective and device signals disagree")
    elif not sleep_present and not hrv_present and not any(w.resting_hr is not None for w in wellness):
        state = RecoveryState.NEEDS_SIGNAL
    elif confidence == "high":
        state = RecoveryState.READY

    recommendation = _recommendation(score, confidence, blockers, latest, planned_workout)
    summary = _summary(recommendation, state, confidence)
    next_action = _next_action(recommendation, missing, blockers)

    return RecoveryContext(
        state=state,
        summary=summary,
        recovery_low_pct=low,
        recovery_high_pct=high,
        confidence_level=confidence,
        training_recommendation=recommendation,
        drivers=drivers,
        blockers=blockers,
        risks=sorted(set(risks)),
        missing=sorted(set(missing)),
        data_sources=_source_list(wellness),
        next_action=next_action,
        gating_note=gating,
    )


def _conflicting(latest: Optional[WellnessSignal]) -> bool:
    if latest is None:
        return False
    subjective_bad = (latest.fatigue_1_10 is not None and latest.fatigue_1_10 >= 8)
    device_good = False
    if latest.sleep_hours is not None and latest.sleep_hours >= 7:
        device_good = True
    if latest.sleep_score is not None and latest.sleep_score >= 80:
        device_good = True
    return subjective_bad and device_good


def _recommendation(
    score: float,
    confidence: str,
    blockers: List[str],
    latest: Optional[WellnessSignal],
    planned: Optional[PlannedWorkoutContext],
) -> TrainingRecommendation:
    if blockers:
        return TrainingRecommendation.REST_OR_RECOVER
    if latest and latest.fatigue_1_10 is not None and latest.fatigue_1_10 >= 8:
        return TrainingRecommendation.EASY_ONLY
    if score < 45:
        return TrainingRecommendation.REST_OR_RECOVER
    if score < 58:
        return TrainingRecommendation.EASY_ONLY
    if confidence == "low":
        return TrainingRecommendation.AEROBIC_OK
    if score >= 78 and confidence in ("medium", "high"):
        if planned and str(planned.intensity or "").lower() in ("race", "test"):
            return TrainingRecommendation.RACE_READY
        return TrainingRecommendation.QUALITY_POSSIBLE
    return TrainingRecommendation.AEROBIC_OK


def _summary(rec: TrainingRecommendation, state: RecoveryState, confidence: str) -> str:
    if state == RecoveryState.RED_FLAG:
        return "Recovery context is conservative today because a red flag is present."
    if rec == TrainingRecommendation.REST_OR_RECOVER:
        return "Recovery looks limited today; keep the day restorative."
    if rec == TrainingRecommendation.EASY_ONLY:
        return "You can likely absorb easy work, but hard intensity is not well supported today."
    if rec == TrainingRecommendation.QUALITY_POSSIBLE:
        return "Quality work looks possible today, with declared recovery confidence."
    if rec == TrainingRecommendation.RACE_READY:
        return "Race/test readiness looks supported today, with declared recovery confidence."
    if rec == TrainingRecommendation.AEROBIC_OK:
        return "Aerobic work looks reasonable today, but keep an eye on recovery drivers."
    return f"Recovery context is uncertain ({confidence} confidence)."


def _next_action(rec: TrainingRecommendation, missing: List[str], blockers: List[str]) -> str:
    if blockers:
        return "Choose recovery or very easy movement; do not force intensity until the red flag clears."
    if rec == TrainingRecommendation.REST_OR_RECOVER:
        return "Prioritize recovery today and reassess after the next check-in."
    if rec == TrainingRecommendation.EASY_ONLY:
        return "Keep today's work easy; skip maximal or threshold efforts."
    if missing:
        return "Proceed conservatively and add missing recovery signals to raise confidence."
    return "Proceed with the planned workout, then review the debrief after completion."


__all__ = [
    "RECOVERY_CONTEXT_VERSION",
    "RecoveryState", "TrainingRecommendation", "WellnessSignal",
    "PlannedWorkoutContext", "RecoveryDriver", "RecoveryContext",
    "assess_recovery",
]
