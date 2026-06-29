"""
plan_session_timeline_bridge.py
===============================
EPOCH — bridge legacy `plan_sessions` rows into Canonical Athlete Timeline
`planned_workout` events.

This is intentionally pure: no FastAPI, no DB connection, no Garmin/Strava client.
It converts a row-shaped dict into a TimelineEvent so the new P0 timeline can consume
the existing structured plan without creating a second plan engine.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Dict, Optional

from timeline_model import (
    AvailabilityState,
    Confidence,
    ConfidenceLevel,
    EventType,
    Source,
    SourceLineage,
    TimelineEvent,
)

BRIDGE_VERSION = "0.1.0"


def plan_session_event_id(plan_session_id: Any) -> str:
    return f"evt_plan_{plan_session_id}"


def planned_workout_id(plan_session_id: Any) -> str:
    return f"plan_session_{plan_session_id}"


def row_to_planned_workout_event(row: Dict[str, Any], athlete_id: str = "default") -> TimelineEvent:
    """Convert one `plan_sessions` row into a deterministic planned_workout event."""
    target = dict(row.get("target") or {})
    plan_source = str(row.get("plan_source") or target.get("source") or "manual").lower()
    source = Source.GARMIN_EXPORT if "garmin" in plan_source else Source.MANUAL_UPLOAD
    source_confidence = str(target.get("source_confidence") or row.get("source_confidence") or "medium").lower()
    confidence_score = _confidence_score(source_confidence)
    start = _planned_start(row.get("planned_date"))
    duration_s = _duration_s(row, target)
    status = str(row.get("status") or "planned")
    plan_session_id = row["id"]
    title = _title(row, target)
    phase = target.get("phase") or row.get("phase")
    sport = target.get("sport") or row.get("sport") or "cycling"

    payload = {
        "planned_workout_id": planned_workout_id(plan_session_id),
        "source": target.get("source") or row.get("plan_source") or "manual_epoch",
        "source_workout_id": target.get("source_workout_id") or f"plan_sessions:{plan_session_id}",
        "plan_session_id": plan_session_id,
        "plan_id": row.get("plan_id"),
        "plan_name": row.get("plan_name"),
        "phase": phase,
        "phase_focus": target.get("phase_focus"),
        "week_number": row.get("week_number"),
        "scheduled_start": start.isoformat() if start else None,
        "scheduled_date_local": _date_str(row.get("planned_date")),
        "sport": sport,
        "canonical_title": title,
        "title": title,
        "intent_type": _intent_type(row, target),
        "duration_target_s": duration_s,
        "distance_target_m": target.get("distance_m") or _km_to_m(target.get("km") or target.get("distance_km")),
        "tss_target": target.get("tss") or target.get("tss_target"),
        "target_hr_zone": target.get("hr_zone") or target.get("primary_zone"),
        "target_power_zone": target.get("power_zone"),
        "workout_steps": target.get("workout_steps") or target.get("intervals") or [],
        "coach_notes": row.get("description"),
        "status": status,
        "matched_clean_session_id": row.get("matched_clean_session_id"),
        "moved_from": _date_str(row.get("moved_from")),
        "move_reason": row.get("move_reason"),
        "source_confidence": source_confidence,
        "bridge_version": BRIDGE_VERSION,
    }

    ev = TimelineEvent.create(
        athlete_id,
        EventType.PLANNED_WORKOUT,
        event_id=plan_session_event_id(plan_session_id),
        start_time=start,
        duration_sec=duration_s,
        sport_category=sport,
        source=SourceLineage(
            source=source,
            source_event_id=str(plan_session_id),
            upload_method="plan_sessions_backfill",
            parser="plan_session_timeline_bridge",
            parser_version=BRIDGE_VERSION,
            raw_payload_ref=f"plan_sessions:{plan_session_id}",
            detected_source=source,
            field_origins={
                "title": "plan_sessions.description",
                "targets": "plan_sessions.target",
                "scheduled_date": "plan_sessions.planned_date",
            },
        ),
        raw_import_reference=f"plan_sessions:{plan_session_id}",
        availability_state=AvailabilityState.AVAILABLE,
        confidence=Confidence(
            score=confidence_score,
            level=Confidence.level_from_score(confidence_score),
            source_confidence=confidence_score,
            parsing_confidence=0.95,
            signals={"plan_intent": AvailabilityState.AVAILABLE},
            imported_fields=["planned_date", "description", "target", "status"],
            data_flags=[] if source_confidence == "high" else [f"plan_source_confidence:{source_confidence}"],
        ),
        normalized_summary={
            "title": title,
            "plan_id": row.get("plan_id"),
            "plan_name": row.get("plan_name"),
            "phase": phase,
            "week_number": row.get("week_number"),
            "status": status,
            "source_confidence": source_confidence,
        },
        payload=payload,
        linked_event_ids=[f"evt_cs_{row['matched_clean_session_id']}"] if row.get("matched_clean_session_id") else [],
        notes=row.get("description"),
    )
    return ev


def _title(row: Dict[str, Any], target: Dict[str, Any]) -> str:
    return (
        target.get("canonical_title")
        or target.get("title")
        or row.get("description")
        or row.get("session_type")
        or "Planned workout"
    )


def _intent_type(row: Dict[str, Any], target: Dict[str, Any]) -> Optional[str]:
    raw = str(target.get("intensity") or target.get("primary_zone") or row.get("session_type") or "").lower()
    if any(x in raw for x in ("recovery", "z1")):
        return "recovery"
    if any(x in raw for x in ("sweet", "tempo", "z3")):
        return "tempo"
    if any(x in raw for x in ("threshold", "ftp", "z4", "race")):
        return "threshold"
    if any(x in raw for x in ("vo2", "z5", "high", "sprint")):
        return "vo2"
    if any(x in raw for x in ("z2", "endurance", "long", "base")):
        return "endurance"
    return raw or None


def _duration_s(row: Dict[str, Any], target: Dict[str, Any]) -> Optional[int]:
    for key in ("duration_target_s", "duration_s"):
        if target.get(key) is not None:
            return int(target[key])
    if target.get("duration_min") is not None:
        return int(float(target["duration_min"]) * 60)
    if row.get("duration_s") is not None:
        return int(row["duration_s"])
    return None


def _planned_start(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(12, 0), tzinfo=timezone.utc)
    if value:
        try:
            return datetime.combine(date.fromisoformat(str(value)[:10]), time(12, 0), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _date_str(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else None


def _km_to_m(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value) * 1000.0
    except (TypeError, ValueError):
        return None


def _confidence_score(label: str) -> float:
    if label == "high":
        return 0.9
    if label == "low":
        return 0.45
    return 0.65


__all__ = [
    "BRIDGE_VERSION",
    "plan_session_event_id",
    "planned_workout_id",
    "row_to_planned_workout_event",
]
