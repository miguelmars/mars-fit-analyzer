"""
plan_intent_router.py
=====================
EPOCH — Plan / Intent Source Router.

Answers: "What was this athlete supposed to do, and which completed workout
matched it?" The core rule is:

Garmin/imported/structured plan tells EPOCH what the athlete was supposed to do.
Strava tells EPOCH what got uploaded. EPOCH reconciles both and declares confidence.

This module is intentionally pure: no FastAPI, DB clients, Garmin clients, Strava
clients, TrainingPeaks clients, or UI code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from timeline_model import EventStatus, EventType, TimelineEvent, parse_dt

PLAN_INTENT_ROUTER_VERSION = "0.1.0"


class MatchState(str, Enum):
    MATCHED = "matched"
    MATCHED_MOVED_DAY = "matched_moved_day"
    PARTIAL_MATCH = "partial_match"
    MISSED = "missed"
    SKIPPED = "skipped"
    RESCHEDULED = "rescheduled"
    REPLACED = "replaced"
    EXTRA_UNPLANNED = "extra_unplanned"
    NEEDS_REVIEW = "needs_review"
    NO_PLAN_SOURCE = "no_plan_source"


class IntentConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_SOURCE_RANK = {
    "coach_import": 1,
    "coach": 1,
    "imported_plan": 1,
    "garmin_calendar": 2,
    "garmin_structured_workout": 2,
    "trainingpeaks": 3,
    "intervals": 3,
    "plan_file": 3,
    "manual_epoch": 4,
    "manual": 4,
    "epoch_library": 5,
    "epoch_program": 5,
    "epoch_inference": 6,
    "strava_title": 7,
    "strava": 7,
    "unknown": 99,
}

_GENERIC_ACTIVITY_TITLES = {
    "morning ride", "afternoon ride", "evening ride", "lunch ride", "night ride",
    "ride", "cycling", "bike ride", "indoor cycling", "workout", "activity",
}


@dataclass
class PlannedWorkout:
    planned_workout_id: str
    athlete_id: str = "default"
    source: str = "manual_epoch"
    source_workout_id: Optional[str] = None
    plan_id: Optional[str] = None
    plan_name: Optional[str] = None
    phase: Optional[str] = None
    week_number: Optional[int] = None
    scheduled_start: Optional[datetime] = None
    scheduled_date_local: Optional[str] = None
    sport: Optional[str] = None
    canonical_title: Optional[str] = None
    intent_type: Optional[str] = None
    duration_target_s: Optional[int] = None
    distance_target_m: Optional[float] = None
    tss_target: Optional[float] = None
    target_hr_zone: Optional[str] = None
    target_power_zone: Optional[str] = None
    workout_steps: List[Dict[str, Any]] = field(default_factory=list)
    coach_notes: Optional[str] = None
    revision: int = 1
    status: str = "planned"

    @property
    def source_rank(self) -> int:
        return _SOURCE_RANK.get(str(self.source or "unknown").lower(), 99)

    @property
    def scheduled_date(self) -> Optional[date]:
        if self.scheduled_date_local:
            try:
                return date.fromisoformat(str(self.scheduled_date_local)[:10])
            except ValueError:
                pass
        if self.scheduled_start:
            return self.scheduled_start.date()
        return None


@dataclass
class ManualIntentCorrection:
    event_id: str
    planned_workout_id: Optional[str] = None
    match_state: MatchState = MatchState.MATCHED
    note: Optional[str] = None


@dataclass
class PlanIntentResolution:
    athlete_id: str
    match_state: MatchState
    confidence_level: IntentConfidence
    planned_workout_id: Optional[str] = None
    matched_event_id: Optional[str] = None
    source: Optional[str] = None
    source_rank: Optional[int] = None
    canonical_title: Optional[str] = None
    display_title: Optional[str] = None
    activity_display_title: Optional[str] = None
    intent_type: Optional[str] = None
    phase: Optional[str] = None
    scheduled_start: Optional[str] = None
    actual_start: Optional[str] = None
    targets: Dict[str, Any] = field(default_factory=dict)
    evidence: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolution_version": PLAN_INTENT_ROUTER_VERSION,
            "athlete_id": self.athlete_id,
            "match_state": self.match_state.value,
            "confidence_level": self.confidence_level.value,
            "planned_workout_id": self.planned_workout_id,
            "matched_event_id": self.matched_event_id,
            "source": self.source,
            "source_rank": self.source_rank,
            "canonical_title": self.canonical_title,
            "display_title": self.display_title,
            "activity_display_title": self.activity_display_title,
            "intent_type": self.intent_type,
            "phase": self.phase,
            "scheduled_start": self.scheduled_start,
            "actual_start": self.actual_start,
            "targets": dict(self.targets),
            "evidence": list(self.evidence),
            "flags": list(self.flags),
            "missing": list(self.missing),
            "next_action": self.next_action,
        }


def resolve_plan_intent(
    planned_workouts: List[PlannedWorkout],
    completed_events: List[TimelineEvent],
    *,
    event: Optional[TimelineEvent] = None,
    planned_workout: Optional[PlannedWorkout] = None,
    manual_corrections: Optional[List[ManualIntentCorrection]] = None,
    athlete_id: str = "default",
    move_window_days: int = 2,
) -> PlanIntentResolution:
    """Resolve planned intent against completed work.

    Use `event` to answer "what was this completed activity supposed to be?"
    Use `planned_workout` to answer "what happened to this planned workout?"
    """
    manual_corrections = manual_corrections or []
    planned = [p for p in planned_workouts if p.athlete_id == athlete_id]
    completed = [
        e for e in completed_events
        if e.athlete_id == athlete_id
        and e.event_type == EventType.ENDURANCE_WORKOUT
        and e.status == EventStatus.ACTIVE
    ]

    if event:
        correction = _correction_for_event(event, manual_corrections)
        if correction:
            target = _find_plan(planned, correction.planned_workout_id)
            return _resolution_from_manual(event, target, correction, athlete_id)
        return _resolve_for_event(event, planned, athlete_id, move_window_days)

    if planned_workout:
        correction = _correction_for_plan(planned_workout, manual_corrections)
        if correction:
            matched = _find_event(completed, correction.event_id)
            return _resolution_from_manual(matched, planned_workout, correction, athlete_id)
        return _resolve_for_plan(planned_workout, completed, athlete_id, move_window_days)

    if not planned and completed:
        return _extra_unplanned(completed[0], athlete_id, ["no plan source"])

    return PlanIntentResolution(
        athlete_id=athlete_id,
        match_state=MatchState.NO_PLAN_SOURCE,
        confidence_level=IntentConfidence.LOW,
        missing=["planned workouts", "completed event"],
        next_action="Import or enter a plan before judging plan compliance.",
    )


def _resolve_for_event(
    event: TimelineEvent,
    planned: List[PlannedWorkout],
    athlete_id: str,
    move_window_days: int,
) -> PlanIntentResolution:
    if not planned:
        return _extra_unplanned(event, athlete_id, ["no plan source"])

    scored = sorted(
        ((*_score_candidate(p, event, move_window_days), p) for p in planned),
        key=lambda x: (x[0], -x[2].source_rank),
        reverse=True,
    )
    top = [x for x in scored if x[0] >= 40]
    if not top:
        return _extra_unplanned(event, athlete_id, ["no planned workout matched this activity"])

    if _ambiguous(top):
        plans = ", ".join(p.planned_workout_id for _, _, p in top[:3])
        return PlanIntentResolution(
            athlete_id=athlete_id,
            match_state=MatchState.NEEDS_REVIEW,
            confidence_level=IntentConfidence.LOW,
            matched_event_id=event.event_id,
            activity_display_title=_activity_title(event),
            evidence=top[0][1],
            flags=[f"multiple possible planned workouts: {plans}"],
            next_action="Ask the athlete to confirm which planned workout this activity fulfilled.",
        )

    score, evidence, plan = top[0]
    return _matched_resolution(plan, event, score, evidence, move_window_days)


def _resolve_for_plan(
    plan: PlannedWorkout,
    completed: List[TimelineEvent],
    athlete_id: str,
    move_window_days: int,
) -> PlanIntentResolution:
    if str(plan.status).lower() == "skipped":
        return _planned_only(plan, athlete_id, MatchState.SKIPPED, "The planned workout was marked skipped.")
    if str(plan.status).lower() == "rescheduled":
        return _planned_only(plan, athlete_id, MatchState.RESCHEDULED, "The planned workout was marked rescheduled.")

    scored = sorted(
        ((*_score_candidate(plan, e, move_window_days), e) for e in completed),
        key=lambda x: x[0],
        reverse=True,
    )
    top = [x for x in scored if x[0] >= 40]
    if not top:
        return _planned_only(plan, athlete_id, MatchState.MISSED, "No completed activity matched this planned workout.")
    if len(top) > 1 and top[0][0] - top[1][0] <= 8:
        return PlanIntentResolution(
            athlete_id=athlete_id,
            match_state=MatchState.NEEDS_REVIEW,
            confidence_level=IntentConfidence.LOW,
            planned_workout_id=plan.planned_workout_id,
            source=plan.source,
            source_rank=plan.source_rank,
            canonical_title=plan.canonical_title,
            display_title=plan.canonical_title,
            intent_type=plan.intent_type,
            phase=plan.phase,
            scheduled_start=_iso(plan.scheduled_start),
            targets=_targets(plan),
            evidence=top[0][1],
            flags=["multiple completed activities could match this plan"],
            next_action="Ask the athlete to confirm which activity completed this planned workout.",
        )
    score, evidence, event = top[0]
    return _matched_resolution(plan, event, score, evidence, move_window_days)


def _score_candidate(
    plan: PlannedWorkout,
    event: TimelineEvent,
    move_window_days: int,
) -> Tuple[int, List[str]]:
    score = 0
    evidence: List[str] = []

    payload = event.payload or {}
    refs = {
        payload.get("planned_workout_id"),
        payload.get("source_workout_id"),
        payload.get("workout_id"),
        event.normalized_summary.get("planned_workout_id") if event.normalized_summary else None,
    }
    if plan.planned_workout_id in refs or (plan.source_workout_id and plan.source_workout_id in refs):
        evidence.append("direct planned workout reference")
        score += 100

    date_gap = _date_gap_days(plan, event)
    if date_gap == 0:
        evidence.append("same local date")
        score += 25
    elif date_gap is not None and abs(date_gap) <= move_window_days:
        evidence.append(f"moved within {abs(date_gap)} day(s)")
        score += 15
    elif date_gap is not None:
        evidence.append(f"outside move window by {abs(date_gap)} day(s)")
        score -= 20
    else:
        evidence.append("missing date evidence")

    if _sport_matches(plan, event):
        evidence.append(f"sport matches {plan.sport or event.sport_category}")
        score += 25
    elif plan.sport and event.sport_category:
        evidence.append("sport differs")
        score -= 20

    duration_score, duration_evidence = _duration_score(plan, event)
    score += duration_score
    if duration_evidence:
        evidence.append(duration_evidence)

    if _intent_compatible(plan, event):
        evidence.append("intent pattern is compatible")
        score += 10

    source_bonus = max(0, 8 - min(plan.source_rank, 8))
    if source_bonus:
        evidence.append(f"{plan.source} plan source outranks activity display title")
        score += source_bonus

    return score, evidence


def _matched_resolution(
    plan: PlannedWorkout,
    event: TimelineEvent,
    score: int,
    evidence: List[str],
    move_window_days: int,
) -> PlanIntentResolution:
    gap = _date_gap_days(plan, event)
    state = MatchState.MATCHED
    if gap is not None and gap != 0 and abs(gap) <= move_window_days:
        state = MatchState.MATCHED_MOVED_DAY
    elif score < 55:
        state = MatchState.PARTIAL_MATCH

    flags = []
    title = _activity_title(event)
    if _is_generic_title(title):
        flags.append("activity title is generic display noise")
        evidence = list(evidence) + ["planned intent beats generic activity title"]

    return PlanIntentResolution(
        athlete_id=plan.athlete_id,
        match_state=state,
        confidence_level=_confidence(score),
        planned_workout_id=plan.planned_workout_id,
        matched_event_id=event.event_id,
        source=plan.source,
        source_rank=plan.source_rank,
        canonical_title=plan.canonical_title,
        display_title=plan.canonical_title or title,
        activity_display_title=title,
        intent_type=plan.intent_type,
        phase=plan.phase,
        scheduled_start=_iso(plan.scheduled_start),
        actual_start=_iso(event.start_time),
        targets=_targets(plan),
        evidence=evidence,
        flags=flags,
        next_action="Use this planned intent for debrief and weekly progress.",
    )


def _extra_unplanned(event: TimelineEvent, athlete_id: str, missing: Optional[List[str]] = None) -> PlanIntentResolution:
    title = _activity_title(event)
    flags = []
    evidence = ["completed activity exists without a matched planned workout"]
    if _is_generic_title(title):
        flags.append("Strava/generic title cannot be treated as plan intent")
        evidence.append("activity title alone is weak evidence")
    return PlanIntentResolution(
        athlete_id=athlete_id,
        match_state=MatchState.EXTRA_UNPLANNED,
        confidence_level=IntentConfidence.LOW,
        matched_event_id=event.event_id,
        display_title=title,
        activity_display_title=title,
        actual_start=_iso(event.start_time),
        evidence=evidence,
        flags=flags,
        missing=missing or [],
        next_action="Classify the workout from data, but do not claim plan compliance.",
    )


def _planned_only(
    plan: PlannedWorkout,
    athlete_id: str,
    state: MatchState,
    next_action: str,
) -> PlanIntentResolution:
    return PlanIntentResolution(
        athlete_id=athlete_id,
        match_state=state,
        confidence_level=IntentConfidence.MEDIUM,
        planned_workout_id=plan.planned_workout_id,
        source=plan.source,
        source_rank=plan.source_rank,
        canonical_title=plan.canonical_title,
        display_title=plan.canonical_title,
        intent_type=plan.intent_type,
        phase=plan.phase,
        scheduled_start=_iso(plan.scheduled_start),
        targets=_targets(plan),
        evidence=["planned workout exists"],
        missing=["matched completed activity"] if state == MatchState.MISSED else [],
        next_action=next_action,
    )


def _resolution_from_manual(
    event: Optional[TimelineEvent],
    plan: Optional[PlannedWorkout],
    correction: ManualIntentCorrection,
    athlete_id: str,
) -> PlanIntentResolution:
    evidence = ["manual athlete correction overrides inference"]
    if correction.note:
        evidence.append(correction.note)
    if plan and event:
        return PlanIntentResolution(
            athlete_id=athlete_id,
            match_state=correction.match_state,
            confidence_level=IntentConfidence.HIGH,
            planned_workout_id=plan.planned_workout_id,
            matched_event_id=event.event_id,
            source=plan.source,
            source_rank=plan.source_rank,
            canonical_title=plan.canonical_title,
            display_title=plan.canonical_title or _activity_title(event),
            activity_display_title=_activity_title(event),
            intent_type=plan.intent_type,
            phase=plan.phase,
            scheduled_start=_iso(plan.scheduled_start),
            actual_start=_iso(event.start_time),
            targets=_targets(plan),
            evidence=evidence,
            next_action="Use the confirmed manual match for debrief and weekly progress.",
        )
    if plan:
        return _planned_only(plan, athlete_id, correction.match_state, "Use the manual correction.")
    if event:
        r = _extra_unplanned(event, athlete_id)
        r.evidence = evidence
        r.confidence_level = IntentConfidence.HIGH
        r.match_state = correction.match_state
        return r
    return PlanIntentResolution(
        athlete_id=athlete_id,
        match_state=MatchState.NEEDS_REVIEW,
        confidence_level=IntentConfidence.LOW,
        evidence=evidence,
        missing=["manual correction target not found"],
        next_action="Review the manual correction target ids.",
    )


def _confidence(score: int) -> IntentConfidence:
    if score >= 95:
        return IntentConfidence.HIGH
    if score >= 55:
        return IntentConfidence.MEDIUM
    return IntentConfidence.LOW


def _ambiguous(scored: List[Tuple[int, List[str], PlannedWorkout]]) -> bool:
    if len(scored) < 2:
        return False
    if scored[0][0] >= 95 and scored[0][0] - scored[1][0] > 8:
        return False
    return scored[0][0] - scored[1][0] <= 8


def _date_gap_days(plan: PlannedWorkout, event: TimelineEvent) -> Optional[int]:
    pdate = plan.scheduled_date
    edate = event.start_time.date() if event.start_time else None
    if not pdate or not edate:
        return None
    return (edate - pdate).days


def _sport_matches(plan: PlannedWorkout, event: TimelineEvent) -> bool:
    if not plan.sport or not event.sport_category:
        return False
    ps = str(plan.sport).lower()
    es = str(event.sport_category).lower()
    if ps == es:
        return True
    if ps == "cycling" and es in ("cycling", "indoor_ride"):
        return True
    if ps == "running" and es in ("running", "trail_running"):
        return True
    return False


def _duration_score(plan: PlannedWorkout, event: TimelineEvent) -> Tuple[int, Optional[str]]:
    if not plan.duration_target_s:
        return 0, "missing planned duration"
    actual = event.duration_sec or event.payload.get("duration_s")
    if not actual:
        return 0, "missing completed duration"
    try:
        diff = abs(float(actual) - float(plan.duration_target_s)) / max(float(plan.duration_target_s), 1.0)
    except (TypeError, ValueError):
        return 0, "duration not comparable"
    if diff <= 0.15:
        return 20, "duration within tolerance"
    if diff <= 0.30:
        return 10, "duration roughly compatible"
    return -8, "duration differs from target"


def _intent_compatible(plan: PlannedWorkout, event: TimelineEvent) -> bool:
    text = " ".join(filter(None, [
        plan.intent_type, plan.canonical_title, plan.coach_notes,
        _activity_title(event), str(event.payload.get("sport_type") or ""),
    ])).lower()
    if not text:
        return False
    groups = [
        ("endurance", ("endurance", "aerobic", "base", "zone 2", "z2", "long")),
        ("tempo", ("tempo", "sweet spot", "sweetspot", "zone 3", "z3")),
        ("threshold", ("threshold", "ftp", "umbral", "zone 4", "z4")),
        ("vo2", ("vo2", "interval", "zone 5", "z5", "anaerobic", "sprint")),
        ("recovery", ("recovery", "easy", "zone 1", "z1")),
    ]
    plan_words = " ".join(filter(None, [plan.intent_type, plan.canonical_title, plan.coach_notes])).lower()
    event_words = " ".join(filter(None, [_activity_title(event), str(event.payload.get("sport_type") or "")])).lower()
    for _, words in groups:
        if any(w in plan_words for w in words) and any(w in event_words for w in words):
            return True
    return any(w in text for _, words in groups for w in words) and bool(plan.intent_type)


def _activity_title(event: TimelineEvent) -> Optional[str]:
    payload = event.payload or {}
    summary = event.normalized_summary or {}
    for key in ("original_name", "name", "title"):
        if payload.get(key):
            return str(payload[key])
    for key in ("title", "name", "activity_name"):
        if summary.get(key):
            return str(summary[key])
    return None


def _is_generic_title(title: Optional[str]) -> bool:
    if not title:
        return False
    return str(title).strip().lower() in _GENERIC_ACTIVITY_TITLES


def _targets(plan: PlannedWorkout) -> Dict[str, Any]:
    return {
        "duration_s": plan.duration_target_s,
        "distance_m": plan.distance_target_m,
        "hr_zone": plan.target_hr_zone,
        "power_zone": plan.target_power_zone,
        "tss": plan.tss_target,
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _find_plan(plans: List[PlannedWorkout], planned_workout_id: Optional[str]) -> Optional[PlannedWorkout]:
    if not planned_workout_id:
        return None
    for p in plans:
        if p.planned_workout_id == planned_workout_id:
            return p
    return None


def _find_event(events: List[TimelineEvent], event_id: Optional[str]) -> Optional[TimelineEvent]:
    if not event_id:
        return None
    for e in events:
        if e.event_id == event_id:
            return e
    return None


def _correction_for_event(
    event: TimelineEvent,
    corrections: List[ManualIntentCorrection],
) -> Optional[ManualIntentCorrection]:
    for c in corrections:
        if c.event_id == event.event_id:
            return c
    return None


def _correction_for_plan(
    plan: PlannedWorkout,
    corrections: List[ManualIntentCorrection],
) -> Optional[ManualIntentCorrection]:
    for c in corrections:
        if c.planned_workout_id == plan.planned_workout_id:
            return c
    return None


def planned_workout_from_dict(data: Dict[str, Any]) -> PlannedWorkout:
    return PlannedWorkout(
        planned_workout_id=data["planned_workout_id"],
        athlete_id=data.get("athlete_id", "default"),
        source=data.get("source", "manual_epoch"),
        source_workout_id=data.get("source_workout_id"),
        plan_id=data.get("plan_id"),
        plan_name=data.get("plan_name"),
        phase=data.get("phase"),
        week_number=data.get("week_number"),
        scheduled_start=parse_dt(data.get("scheduled_start")),
        scheduled_date_local=data.get("scheduled_date_local"),
        sport=data.get("sport"),
        canonical_title=data.get("canonical_title"),
        intent_type=data.get("intent_type"),
        duration_target_s=data.get("duration_target_s"),
        distance_target_m=data.get("distance_target_m"),
        tss_target=data.get("tss_target"),
        target_hr_zone=data.get("target_hr_zone"),
        target_power_zone=data.get("target_power_zone"),
        workout_steps=list(data.get("workout_steps") or []),
        coach_notes=data.get("coach_notes"),
        revision=int(data.get("revision", 1)),
        status=data.get("status", "planned"),
    )


__all__ = [
    "PLAN_INTENT_ROUTER_VERSION",
    "MatchState",
    "IntentConfidence",
    "PlannedWorkout",
    "ManualIntentCorrection",
    "PlanIntentResolution",
    "planned_workout_from_dict",
    "resolve_plan_intent",
]
