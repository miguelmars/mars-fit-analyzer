"""
routers/timeline.py — Canonical Athlete Timeline ingestion API (Phase 3)
======================================================================
Thin HTTP layer over the P0 ingestion pipeline:

  POST /timeline/import        upload a FIT/GPX/TCX/CSV (or .zip with a FIT) → timeline event
  GET  /timeline               read the athlete's timeline (newest first)
  GET  /timeline/import-logs   recent import logs (traceability / debugging)

Design notes:
  * This router is **additive**. It does not modify `main.py`, the `/app` PWA, or the
    existing `clean_sessions`/`sessions` schema. Registration in `main.py` is a single
    `app.include_router(...)` line (see docs/EPOCH_P0_TIMELINE_INGESTION_IMPLEMENTATION.md).
  * Write protection is handled by the app's global `X-Epoch-Key` middleware (main.py),
    so no per-route auth is needed here.
  * The Postgres timeline tables are created on demand (`ensure=True`) and never touch
    existing tables.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from db import get_db
from ingest_pipeline import ingest_file
from timeline_store import PostgresTimelineRepository
from timeline_model import EventType, TimelineEvent, parse_dt
from data_quality_audit import AthleteProfile, audit_event, audit_athlete, gating_note
from post_workout_debrief import AthleteContext, PlannedIntent, debrief as run_debrief
from goal_readiness import Goal, assess as assess_readiness
from data_capability_matrix import capability_matrix
from recovery_context import PlannedWorkoutContext, WellnessSignal, assess_recovery
from plan_intent_router import PlannedWorkout, planned_workout_from_dict, resolve_plan_intent

logger = logging.getLogger("mars_fit")

router = APIRouter(tags=["timeline"])

# Single-user app today; athlete_id is explicit so multi-user is a drop-in later.
DEFAULT_ATHLETE_ID = "default"
_UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # 30 MB (matches /analyze-fit)


def get_repo() -> PostgresTimelineRepository:
    """Build a Postgres-backed timeline repository. Overridden in tests with an
    in-memory repo. Raises 503 if the DB is unavailable."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    return PostgresTimelineRepository(conn, ensure=True)


@router.post("/timeline/import")
async def timeline_import(
    file: UploadFile = File(...),
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
):
    """Import one activity file into the Canonical Athlete Timeline.

    Returns HTTP 200 with `ok=false` on a safe parse failure (the timeline is never
    corrupted; a failed import is logged). Returns 413 if the file is too large.
    """
    data = await file.read(_UPLOAD_MAX_BYTES + 1)
    if len(data) > _UPLOAD_MAX_BYTES:
        raise HTTPException(413, "Upload too large (max 30 MB).")

    repo = get_repo()
    res = ingest_file(data, file.filename, athlete_id, repo)
    ev = res.event
    logger.info("TIMELINE import filename=%s status=%s event=%s",
                file.filename, res.status.value, ev.event_id if ev else None)

    body = {
        "ok": res.ok,
        "status": res.status.value,
        "import_id": res.import_log.import_id,
        "event_id": ev.event_id if ev else None,
        "event_type": ev.event_type.value if ev else None,
        "sport_category": ev.sport_category if ev else None,
        "source": ev.source.source.value if ev else None,
        "detected_source": ev.source.detected_source.value if ev else None,
        "duplicate_of": res.duplicate_of,
        "availability_state": ev.availability_state.value if ev else None,
        "confidence_level": ev.confidence.level.value if ev else None,
        "confidence_score": ev.confidence.score if ev else None,
        "raw_import_reference": ev.raw_import_reference if ev else None,
        "normalized_summary": ev.normalized_summary if ev else None,
        "warnings": res.import_log.warnings,
        "error": res.error,
    }
    return body


@router.get("/timeline")
def timeline_list(
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    limit: int = Query(100, ge=1, le=1000),
    include_duplicates: bool = Query(False),
):
    """Read the athlete's timeline, newest first."""
    repo = get_repo()
    _min_dt = datetime.min.replace(tzinfo=timezone.utc)
    events = sorted(repo.list_events(athlete_id), key=lambda e: e.start_time or _min_dt, reverse=True)
    out = []
    for e in events:
        if not include_duplicates and e.status.value in ("duplicate",):
            continue
        out.append({
            "event_id": e.event_id,
            "event_type": e.event_type.value,
            "status": e.status.value,
            "start_time": e.start_time.isoformat() if e.start_time else None,
            "sport_category": e.sport_category,
            "source": e.source.source.value,
            "availability_state": e.availability_state.value,
            "confidence_level": e.confidence.level.value,
            "summary": e.normalized_summary,
        })
        if len(out) >= limit:
            break
    return {"athlete_id": athlete_id, "count": len(out), "events": out}


@router.get("/timeline/import-logs")
def timeline_import_logs(
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    limit: int = Query(50, ge=1, le=500),
):
    """Recent import logs for traceability / debugging."""
    repo = get_repo()
    logs = repo.list_import_logs(athlete_id)
    return {"athlete_id": athlete_id, "count": len(logs[:limit]), "logs": logs[:limit]}


# ─────────────────────────────────────────────────────────────────────────────
# Intelligence read endpoints (P0 #2 Audit · #3 Debrief · #4 Goal Readiness)
# All GET / read-only. Athlete settings (hr_max/lthr/ftp) and the goal are passed as
# query params so this router stays platform-agnostic; the engines degrade gracefully
# when a value is missing and say what they could not check.
# ─────────────────────────────────────────────────────────────────────────────

def _profile(hr_max: Optional[int], lthr: Optional[int], ftp_w: Optional[int]) -> AthleteProfile:
    return AthleteProfile(hr_max=hr_max, lthr=lthr, ftp_w=ftp_w)


def _require_event(repo, event_id: str):
    ev = repo.get_event(event_id)
    if ev is None:
        raise HTTPException(404, f"timeline event '{event_id}' not found")
    return ev


def _planned_workouts_from_timeline(events: List[TimelineEvent], athlete_id: str) -> List[PlannedWorkout]:
    """Adapt planned_workout timeline events into the pure Plan/Intent engine DTO.

    The timeline remains the storage truth; this router is only an adapter. Missing
    fields degrade honestly instead of inventing plan details.
    """
    out: List[PlannedWorkout] = []
    for ev in events:
        if ev.athlete_id != athlete_id or ev.event_type != EventType.PLANNED_WORKOUT:
            continue
        payload = ev.payload or {}
        summary = ev.normalized_summary or {}
        scheduled_start = payload.get("scheduled_start") or (ev.start_time.isoformat() if ev.start_time else None)
        scheduled_date = payload.get("scheduled_date_local") or (ev.start_time.date().isoformat() if ev.start_time else None)
        out.append(planned_workout_from_dict({
            "planned_workout_id": payload.get("planned_workout_id") or ev.event_id,
            "athlete_id": athlete_id,
            "source": payload.get("source") or "manual_epoch",
            "source_workout_id": payload.get("source_workout_id") or ev.source.source_event_id,
            "plan_id": payload.get("plan_id"),
            "plan_name": payload.get("plan_name"),
            "phase": payload.get("phase"),
            "week_number": payload.get("week_number"),
            "scheduled_start": scheduled_start,
            "scheduled_date_local": scheduled_date,
            "sport": payload.get("sport") or ev.sport_category,
            "canonical_title": (
                payload.get("canonical_title")
                or payload.get("title")
                or summary.get("title")
                or summary.get("name")
            ),
            "intent_type": payload.get("intent_type"),
            "duration_target_s": payload.get("duration_target_s") or ev.duration_sec,
            "distance_target_m": payload.get("distance_target_m"),
            "tss_target": payload.get("tss_target"),
            "target_hr_zone": payload.get("target_hr_zone"),
            "target_power_zone": payload.get("target_power_zone"),
            "workout_steps": payload.get("workout_steps") or [],
            "coach_notes": payload.get("coach_notes"),
            "revision": payload.get("revision", 1),
            "status": payload.get("status", "planned"),
        }))
    return out


@router.get("/timeline/{event_id}/audit")
def timeline_event_audit(
    event_id: str,
    hr_max: Optional[int] = Query(None),
    lthr: Optional[int] = Query(None),
    ftp_w: Optional[int] = Query(None),
):
    """Data Quality + Zone Audit (#2) for a single event."""
    repo = get_repo()
    ev = _require_event(repo, event_id)
    flags = audit_event(ev, _profile(hr_max, lthr, ftp_w))
    return {
        "event_id": event_id,
        "flags": [f.to_dict() for f in flags],
        "gating_note": gating_note(flags),
    }


@router.get("/data-health")
def data_health(
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    hr_max: Optional[int] = Query(None),
    lthr: Optional[int] = Query(None),
    ftp_w: Optional[int] = Query(None),
):
    """Per-athlete 'health of your data' panel (#2) across the whole timeline."""
    repo = get_repo()
    health = audit_athlete(repo.list_events(athlete_id), _profile(hr_max, lthr, ftp_w))
    return {"athlete_id": athlete_id, **health.to_dict()}


@router.get("/timeline/{event_id}/debrief")
def timeline_event_debrief(
    event_id: str,
    hr_max: Optional[int] = Query(None),
    lthr: Optional[int] = Query(None),
    ftp_w: Optional[int] = Query(None),
    intent_type: Optional[str] = Query(None),
    coach_notes: Optional[str] = Query(None),
    phase: Optional[str] = Query(None),
    target_avg_hr: Optional[float] = Query(None),
    target_if: Optional[float] = Query(None),
):
    """Post-Workout Debrief / Intent vs Reality (#3) for a single event.
    The audit (#2) for this event is run internally so 🔴 issues gate the conclusion."""
    repo = get_repo()
    ev = _require_event(repo, event_id)
    profile = _profile(hr_max, lthr, ftp_w)
    audit_flags = audit_event(ev, profile)
    ctx = AthleteContext(lthr=lthr, ftp_w=ftp_w, hr_max=hr_max)
    intent = None
    if any([intent_type, coach_notes, phase, target_avg_hr, target_if]):
        intent = PlannedIntent(intent_type=intent_type, coach_notes=coach_notes, phase=phase,
                               target_avg_hr=target_avg_hr, target_if=target_if)
    return run_debrief(ev, intent, ctx, audit_flags=audit_flags).to_dict()


@router.get("/goal-readiness")
def goal_readiness_endpoint(
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    event_name: Optional[str] = Query(None),
    event_date: Optional[str] = Query(None),
    sport: Optional[str] = Query(None),
    target_distance_m: Optional[float] = Query(None),
    target_elevation_m: Optional[float] = Query(None),
    target_duration_s: Optional[int] = Query(None),
    target_weekly_hours: Optional[float] = Query(None),
    hr_max: Optional[int] = Query(None),
    lthr: Optional[int] = Query(None),
    ftp_w: Optional[int] = Query(None),
):
    """Goal Readiness / Capability Gap (#4): a range-with-confidence reality check.
    Data confidence is gated by the audit (#2) over the athlete's timeline."""
    repo = get_repo()
    events = repo.list_events(athlete_id)
    health = audit_athlete(events, _profile(hr_max, lthr, ftp_w))
    goal = None
    if event_name or event_date:
        goal = Goal(name=event_name, event_date=parse_dt(event_date), sport=sport,
                    target_distance_m=target_distance_m, target_elevation_m=target_elevation_m,
                    target_duration_s=target_duration_s, target_weekly_hours=target_weekly_hours)
    return {"athlete_id": athlete_id, **assess_readiness(events, goal, data_health=health).to_dict()}


@router.get("/capability-matrix")
def capability_matrix_endpoint(athlete_id: str = Query(DEFAULT_ATHLETE_ID)):
    """Data Capability Matrix: what Epoch can compute now vs needs-history vs needs-signal,
    from the athlete's actual timeline coverage."""
    repo = get_repo()
    return {"athlete_id": athlete_id, **capability_matrix(repo.list_events(athlete_id)).to_dict()}


@router.get("/recovery-context")
def recovery_context_endpoint(
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    hr_max: Optional[int] = Query(None),
    lthr: Optional[int] = Query(None),
    ftp_w: Optional[int] = Query(None),
    check_in_date: Optional[str] = Query(None),
    sleep_hours: Optional[float] = Query(None),
    sleep_score: Optional[float] = Query(None),
    resting_hr: Optional[float] = Query(None),
    hrv_ms: Optional[float] = Query(None),
    fatigue_1_10: Optional[float] = Query(None),
    soreness_1_10: Optional[float] = Query(None),
    stress_1_10: Optional[float] = Query(None),
    illness: bool = Query(False),
    injury: bool = Query(False),
    planned_title: Optional[str] = Query(None),
    planned_intensity: Optional[str] = Query(None),
    planned_duration_s: Optional[int] = Query(None),
):
    """Recovery Context / Recovery Reserve (#5): what the athlete can likely absorb today.

    Wellness inputs are optional and explicit. When sleep/HRV/RHR are absent, the engine
    returns lower confidence instead of inventing recovery certainty.
    """
    repo = get_repo()
    events = repo.list_events(athlete_id)
    profile = _profile(hr_max, lthr, ftp_w)
    health = audit_athlete(events, profile)
    matrix = capability_matrix(events)

    wellness = []
    if any([
        check_in_date, sleep_hours is not None, sleep_score is not None, resting_hr is not None,
        hrv_ms is not None, fatigue_1_10 is not None, soreness_1_10 is not None,
        stress_1_10 is not None, illness, injury,
    ]):
        wellness.append(WellnessSignal(
            date=parse_dt(check_in_date) or datetime.now(timezone.utc),
            source="query_check_in",
            sleep_hours=sleep_hours,
            sleep_score=sleep_score,
            resting_hr=resting_hr,
            hrv_ms=hrv_ms,
            fatigue_1_10=fatigue_1_10,
            soreness_1_10=soreness_1_10,
            stress_1_10=stress_1_10,
            illness=illness,
            injury=injury,
        ))

    planned = None
    if any([planned_title, planned_intensity, planned_duration_s]):
        planned = PlannedWorkoutContext(
            title=planned_title,
            intensity=planned_intensity,
            duration_s=planned_duration_s,
        )

    return {
        "athlete_id": athlete_id,
        **assess_recovery(
            events,
            data_health=health,
            capability=matrix,
            wellness=wellness,
            planned_workout=planned,
        ).to_dict(),
    }


@router.get("/timeline/{event_id}/plan-intent")
def timeline_event_plan_intent(
    event_id: str,
    move_window_days: int = Query(2, ge=0, le=7),
):
    """Plan / Intent Source Router for one completed activity.

    Answers: what was this completed activity supposed to be? Garmin/imported/
    structured plan intent beats generic activity titles like "Morning Ride".
    """
    repo = get_repo()
    ev = _require_event(repo, event_id)
    events = repo.list_events(ev.athlete_id)
    planned = _planned_workouts_from_timeline(events, ev.athlete_id)
    completed = [e for e in events if e.event_type == EventType.ENDURANCE_WORKOUT]
    return resolve_plan_intent(
        planned,
        completed,
        event=ev,
        athlete_id=ev.athlete_id,
        move_window_days=move_window_days,
    ).to_dict()


@router.get("/planned-workouts/{planned_workout_id}/match")
def planned_workout_match(
    planned_workout_id: str,
    athlete_id: str = Query(DEFAULT_ATHLETE_ID),
    move_window_days: int = Query(2, ge=0, le=7),
):
    """Plan / Intent Source Router from the planned-workout side.

    Answers: did this planned workout get completed, moved, missed, skipped, or
    does it need review?
    """
    repo = get_repo()
    events = repo.list_events(athlete_id)
    planned = _planned_workouts_from_timeline(events, athlete_id)
    target = next((p for p in planned if p.planned_workout_id == planned_workout_id), None)
    if target is None:
        raise HTTPException(404, f"planned workout '{planned_workout_id}' not found")
    completed = [e for e in events if e.event_type == EventType.ENDURANCE_WORKOUT]
    return resolve_plan_intent(
        planned,
        completed,
        planned_workout=target,
        athlete_id=athlete_id,
        move_window_days=move_window_days,
    ).to_dict()
