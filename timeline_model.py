"""
timeline_model.py
=================
EPOCH — **Canonical Athlete Timeline** core model (P0 foundation).

Why this exists (see docs: EPOCH_CANONICAL_ATHLETE_TIMELINE_SPEC.md):
    Epoch is NOT "upload your ride". Its foundation is a *timeline of typed athlete
    events* (endurance, strength, mobility, recovery therapy, nutrition, subjective
    note, sleep signal, biomarker, planned workout, ...), not an "activity table".
    The base is one common schema + a per-type `payload`. Adding a new event type
    must NOT require migrating existing events.

Design (clean-architecture / Dependency Rule):
    This module is the CORE and is **framework-free** — no FastAPI, no psycopg2, no
    vendor SDKs. Parsers, storage and connectors depend on this; never the reverse.

P0 scope: the model is generic; ingestion (ingest_pipeline.py) writes the first event
type, `endurance_workout`. All other event types are declared but not implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

MODEL_VERSION = "0.1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Controlled vocabularies (str-Enums → serialize to plain strings; new members are
# additive and never break stored events, which keep the string value).
# ─────────────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    """Typed events the Canonical Athlete Timeline can hold.

    P0 implements only ENDURANCE_WORKOUT. The rest are declared now so future layers
    write into the SAME timeline without a schema migration.
    """

    ENDURANCE_WORKOUT = "endurance_workout"   # P0 — implemented
    STRENGTH_SESSION = "strength_session"     # future
    MOBILITY_SESSION = "mobility_session"     # future
    RECOVERY_THERAPY = "recovery_therapy"     # massage / Compex / foam / physio
    NUTRITION_FUELING = "nutrition_fueling"   # future
    SUBJECTIVE_NOTE = "subjective_note"       # soreness / fatigue / illness / RPE
    SLEEP_RECOVERY_SIGNAL = "sleep_recovery_signal"
    BIOMARKER_UPLOAD = "biomarker_upload"     # future, high privacy boundary
    PLANNED_WORKOUT = "planned_workout"       # intent
    UNKNOWN = "unknown"


class Source(str, Enum):
    """Where an event came from (lineage). UNKNOWN when it cannot be identified —
    we never invent a source."""

    FILE_UPLOAD = "file_upload"
    GARMIN_EXPORT = "garmin_export"
    STRAVA_EXPORT = "strava_export"
    ZWIFT_EXPORT = "zwift_export"
    MYWHOOSH_EXPORT = "mywhoosh_export"
    WAHOO_EXPORT = "wahoo_export"
    MANUAL_UPLOAD = "manual_upload"
    UNKNOWN = "unknown"


class AvailabilityState(str, Enum):
    """Per-signal availability/quality. Lets Epoch declare confidence honestly."""

    AVAILABLE = "available"        # imported directly from the source
    DERIVED = "derived"            # computed from other available data
    ESTIMATED = "estimated"        # modeled when device data is missing
    USER_REPORTED = "user_reported"
    MISSING = "missing"            # not present but the source could have it
    UNAVAILABLE = "unavailable"    # the source/device does not expose this signal
    CONFLICT = "conflict"          # sources disagree


def summarize_availability(signals: Optional[Dict[str, "AvailabilityState"]]) -> AvailabilityState:
    """Collapse per-signal availability into one event-level state.

    The detailed truth still lives in `Confidence.signals`; this summary is for quick
    routing/UI decisions ("usable", "partial", "estimated", etc.) without forcing every
    downstream reader to re-interpret the signal map.
    """
    vals = list((signals or {}).values())
    if not vals:
        return AvailabilityState.MISSING
    if any(v == AvailabilityState.CONFLICT for v in vals):
        return AvailabilityState.CONFLICT
    if any(v == AvailabilityState.ESTIMATED for v in vals):
        return AvailabilityState.ESTIMATED
    usable = {AvailabilityState.AVAILABLE, AvailabilityState.DERIVED, AvailabilityState.USER_REPORTED}
    if vals and all(v in usable for v in vals):
        return AvailabilityState.AVAILABLE
    if any(v in usable for v in vals):
        return AvailabilityState.DERIVED
    if all(v == AvailabilityState.UNAVAILABLE for v in vals):
        return AvailabilityState.UNAVAILABLE
    return AvailabilityState.MISSING


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EventStatus(str, Enum):
    ACTIVE = "active"                      # a normal, trusted event
    DUPLICATE = "duplicate"                # confirmed duplicate, kept for lineage
    DUPLICATE_UNCERTAIN = "duplicate_uncertain"  # likely-but-not-sure dup; kept, never deleted
    FAILED = "failed"                      # explicitly marked failed import (never silent corruption)


class ImportStatus(str, Enum):
    RECEIVED = "received"
    PARSED = "parsed"
    NORMALIZED = "normalized"
    DUPLICATE = "duplicate"
    IMPORTED = "imported"
    FAILED = "failed"


class FileType(str, Enum):
    FIT = "fit"
    GPX = "gpx"
    TCX = "tcx"
    CSV = "csv"
    ZIP = "zip"
    UNKNOWN = "unknown"


class SportCategory(str, Enum):
    """Normalized sport vocabulary for endurance events. Small for P0; stored as a
    string so unmapped values survive."""

    CYCLING = "cycling"
    INDOOR_RIDE = "indoor_ride"
    RUNNING = "running"
    TRAIL_RUNNING = "trail_running"
    WALK = "walk"
    HIKING = "hiking"
    SWIM = "swim"
    STRENGTH = "strength"
    OTHER = "other"
    UNKNOWN = "unknown"


# Canonical signal names used in Confidence.signals (kept stable for downstream layers).
SIGNAL_HR = "heart_rate"
SIGNAL_POWER = "power"
SIGNAL_GPS = "gps"
SIGNAL_ELEVATION = "elevation"
SIGNAL_CADENCE = "cadence"
SIGNAL_DISTANCE = "distance"
SIGNAL_DURATION = "duration"


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def parse_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse to an aware datetime. Returns None if not parseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _enum_val(x: Any) -> Any:
    return x.value if isinstance(x, Enum) else x


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Source lineage
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceLineage:
    """Traceability: where an event came from and how it was produced.

    On dedup/merge, `merged_from` keeps the superseded event ids and `field_origins`
    records which source won each field — so a merge is auditable and reversible.
    """

    source: Source = Source.UNKNOWN
    source_event_id: Optional[str] = None      # the source's own activity id, if any
    upload_method: str = "file_upload"          # file_upload / manual / api ...
    original_filename: Optional[str] = None
    file_type: Optional[FileType] = None
    file_hash: Optional[str] = None             # sha256 of raw bytes (fingerprint)
    parser: Optional[str] = None
    parser_version: Optional[str] = None
    raw_payload_ref: Optional[str] = None       # pointer/ref to raw file or metadata
    detected_source: Source = Source.UNKNOWN    # what we detected vs the resolved source
    merged_from: List[str] = field(default_factory=list)
    field_origins: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": _enum_val(self.source),
            "source_event_id": self.source_event_id,
            "upload_method": self.upload_method,
            "original_filename": self.original_filename,
            "file_type": _enum_val(self.file_type),
            "file_hash": self.file_hash,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "raw_payload_ref": self.raw_payload_ref,
            "detected_source": _enum_val(self.detected_source),
            "merged_from": list(self.merged_from),
            "field_origins": dict(self.field_origins),
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "SourceLineage":
        d = d or {}
        return cls(
            source=Source(d.get("source", Source.UNKNOWN.value)),
            source_event_id=d.get("source_event_id"),
            upload_method=d.get("upload_method", "file_upload"),
            original_filename=d.get("original_filename"),
            file_type=FileType(d["file_type"]) if d.get("file_type") else None,
            file_hash=d.get("file_hash"),
            parser=d.get("parser"),
            parser_version=d.get("parser_version"),
            raw_payload_ref=d.get("raw_payload_ref"),
            detected_source=Source(d.get("detected_source", Source.UNKNOWN.value)),
            merged_from=list(d.get("merged_from") or []),
            field_origins=dict(d.get("field_origins") or {}),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Confidence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Confidence:
    """Reliability of a normalized event.

    `signals` maps a canonical signal name (heart_rate/power/gps/...) to its
    AvailabilityState, so a later layer can say *exactly* why an analysis is high or
    low confidence. `data_flags` carries audit warnings (e.g. duplicate, partial_data).
    """

    score: float = 0.0                          # 0..1 overall
    level: ConfidenceLevel = ConfidenceLevel.LOW
    source_confidence: float = 0.0              # trust in the source/lineage
    parsing_confidence: float = 0.0             # trust in the parse
    signals: Dict[str, AvailabilityState] = field(default_factory=dict)
    imported_fields: List[str] = field(default_factory=list)
    derived_fields: List[str] = field(default_factory=list)
    estimated_fields: List[str] = field(default_factory=list)
    unavailable_fields: List[str] = field(default_factory=list)  # format/source structurally can't provide
    missing_key_fields: List[str] = field(default_factory=list)
    data_flags: List[str] = field(default_factory=list)

    def has(self, signal: str) -> bool:
        return self.signals.get(signal) in (AvailabilityState.AVAILABLE, AvailabilityState.DERIVED)

    @staticmethod
    def level_from_score(score: float) -> ConfidenceLevel:
        if score >= 0.75:
            return ConfidenceLevel.HIGH
        if score >= 0.45:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(float(self.score), 4),
            "level": _enum_val(self.level),
            "source_confidence": round(float(self.source_confidence), 4),
            "parsing_confidence": round(float(self.parsing_confidence), 4),
            "signals": {k: _enum_val(v) for k, v in self.signals.items()},
            "imported_fields": list(self.imported_fields),
            "derived_fields": list(self.derived_fields),
            "estimated_fields": list(self.estimated_fields),
            "unavailable_fields": list(self.unavailable_fields),
            "missing_key_fields": list(self.missing_key_fields),
            "data_flags": list(self.data_flags),
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Confidence":
        d = d or {}
        return cls(
            score=float(d.get("score", 0.0)),
            level=ConfidenceLevel(d.get("level", ConfidenceLevel.LOW.value)),
            source_confidence=float(d.get("source_confidence", 0.0)),
            parsing_confidence=float(d.get("parsing_confidence", 0.0)),
            signals={k: AvailabilityState(v) for k, v in (d.get("signals") or {}).items()},
            imported_fields=list(d.get("imported_fields") or []),
            derived_fields=list(d.get("derived_fields") or []),
            estimated_fields=list(d.get("estimated_fields") or []),
            unavailable_fields=list(d.get("unavailable_fields") or []),
            missing_key_fields=list(d.get("missing_key_fields") or []),
            data_flags=list(d.get("data_flags") or []),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Typed payload — endurance (the only one implemented in P0)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EndurancePayload:
    """Per-type payload for EventType.ENDURANCE_WORKOUT. Every field is optional:
    store what is available, leave the rest None and mark availability in Confidence."""

    sport_type: Optional[str] = None
    distance_m: Optional[float] = None
    duration_s: Optional[int] = None
    moving_time_s: Optional[int] = None
    elapsed_time_s: Optional[int] = None
    elevation_gain_m: Optional[float] = None
    elevation_loss_m: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    avg_cadence: Optional[float] = None
    max_cadence: Optional[float] = None
    avg_power: Optional[float] = None
    max_power: Optional[float] = None
    normalized_power: Optional[float] = None
    avg_speed_mps: Optional[float] = None
    max_speed_mps: Optional[float] = None
    avg_pace_s_per_km: Optional[float] = None
    calories: Optional[float] = None
    device: Optional[str] = None
    original_name: Optional[str] = None
    hr_series_available: bool = False
    power_series_available: bool = False
    gps_available: bool = False
    route_available: bool = False
    laps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "sport_type": self.sport_type,
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "moving_time_s": self.moving_time_s,
            "elapsed_time_s": self.elapsed_time_s,
            "elevation_gain_m": self.elevation_gain_m,
            "elevation_loss_m": self.elevation_loss_m,
            "avg_hr": self.avg_hr,
            "max_hr": self.max_hr,
            "avg_cadence": self.avg_cadence,
            "max_cadence": self.max_cadence,
            "avg_power": self.avg_power,
            "max_power": self.max_power,
            "normalized_power": self.normalized_power,
            "avg_speed_mps": self.avg_speed_mps,
            "max_speed_mps": self.max_speed_mps,
            "avg_pace_s_per_km": self.avg_pace_s_per_km,
            "calories": self.calories,
            "device": self.device,
            "original_name": self.original_name,
            "hr_series_available": self.hr_series_available,
            "power_series_available": self.power_series_available,
            "gps_available": self.gps_available,
            "route_available": self.route_available,
            "laps": list(self.laps),
        }
        return d

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "EndurancePayload":
        d = d or {}
        return cls(
            sport_type=d.get("sport_type"),
            distance_m=d.get("distance_m"),
            duration_s=d.get("duration_s"),
            moving_time_s=d.get("moving_time_s"),
            elapsed_time_s=d.get("elapsed_time_s"),
            elevation_gain_m=d.get("elevation_gain_m"),
            elevation_loss_m=d.get("elevation_loss_m"),
            avg_hr=d.get("avg_hr"),
            max_hr=d.get("max_hr"),
            avg_cadence=d.get("avg_cadence"),
            max_cadence=d.get("max_cadence"),
            avg_power=d.get("avg_power"),
            max_power=d.get("max_power"),
            normalized_power=d.get("normalized_power"),
            avg_speed_mps=d.get("avg_speed_mps"),
            max_speed_mps=d.get("max_speed_mps"),
            avg_pace_s_per_km=d.get("avg_pace_s_per_km"),
            calories=d.get("calories"),
            device=d.get("device"),
            original_name=d.get("original_name"),
            hr_series_available=bool(d.get("hr_series_available", False)),
            power_series_available=bool(d.get("power_series_available", False)),
            gps_available=bool(d.get("gps_available", False)),
            route_available=bool(d.get("route_available", False)),
            laps=list(d.get("laps") or []),
        )


# ─────────────────────────────────────────────────────────────────────────────
# The canonical event
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TimelineEvent:
    """One typed event in the Canonical Athlete Timeline.

    Common schema for ALL event types + a per-type `payload` dict. For endurance the
    payload is `EndurancePayload.to_dict()`. Future types fill `payload` with their own
    shape — no change to this class required.
    """

    event_id: str
    athlete_id: str
    event_type: EventType
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_sec: Optional[int] = None
    timezone: Optional[str] = None
    sport_category: Optional[str] = None
    source: SourceLineage = field(default_factory=SourceLineage)
    raw_import_reference: Optional[str] = None
    availability_state: AvailabilityState = AvailabilityState.MISSING
    normalized_summary: Dict[str, Any] = field(default_factory=dict)
    confidence: Confidence = field(default_factory=Confidence)
    payload: Dict[str, Any] = field(default_factory=dict)
    linked_event_ids: List[str] = field(default_factory=list)
    notes: Optional[str] = None
    status: EventStatus = EventStatus.ACTIVE
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)

    @classmethod
    def create(
        cls,
        athlete_id: str,
        event_type: EventType,
        *,
        start_time: Optional[datetime] = None,
        source: Optional[SourceLineage] = None,
        confidence: Optional[Confidence] = None,
        payload: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> "TimelineEvent":
        return cls(
            event_id=kwargs.pop("event_id", None) or new_event_id(),
            athlete_id=athlete_id,
            event_type=event_type,
            start_time=start_time,
            source=source or SourceLineage(),
            confidence=confidence or Confidence(),
            payload=payload or {},
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        availability_state = self.availability_state or summarize_availability(self.confidence.signals)
        return {
            "event_id": self.event_id,
            "athlete_id": self.athlete_id,
            "event_type": _enum_val(self.event_type),
            "start_time": _iso(self.start_time),
            "end_time": _iso(self.end_time),
            "duration_sec": self.duration_sec,
            "timezone": self.timezone,
            "sport_category": self.sport_category,
            "source": self.source.to_dict(),
            "raw_import_reference": self.raw_import_reference,
            "availability_state": _enum_val(availability_state),
            "normalized_summary": dict(self.normalized_summary),
            "confidence": self.confidence.to_dict(),
            "payload": dict(self.payload),
            "linked_event_ids": list(self.linked_event_ids),
            "notes": self.notes,
            "status": _enum_val(self.status),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "model_version": MODEL_VERSION,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TimelineEvent":
        return cls(
            event_id=d["event_id"],
            athlete_id=d["athlete_id"],
            event_type=EventType(d.get("event_type", EventType.UNKNOWN.value)),
            start_time=parse_dt(d.get("start_time")),
            end_time=parse_dt(d.get("end_time")),
            duration_sec=d.get("duration_sec"),
            timezone=d.get("timezone"),
            sport_category=d.get("sport_category"),
            source=SourceLineage.from_dict(d.get("source")),
            raw_import_reference=d.get("raw_import_reference"),
            availability_state=AvailabilityState(d.get("availability_state") or AvailabilityState.MISSING.value),
            normalized_summary=dict(d.get("normalized_summary") or {}),
            confidence=Confidence.from_dict(d.get("confidence")),
            payload=dict(d.get("payload") or {}),
            linked_event_ids=list(d.get("linked_event_ids") or []),
            notes=d.get("notes"),
            status=EventStatus(d.get("status", EventStatus.ACTIVE.value)),
            created_at=parse_dt(d.get("created_at")) or now_utc(),
            updated_at=parse_dt(d.get("updated_at")) or now_utc(),
        )


__all__ = [
    "MODEL_VERSION",
    "EventType", "Source", "AvailabilityState", "ConfidenceLevel", "EventStatus",
    "ImportStatus", "FileType", "SportCategory",
    "SIGNAL_HR", "SIGNAL_POWER", "SIGNAL_GPS", "SIGNAL_ELEVATION", "SIGNAL_CADENCE",
    "SIGNAL_DISTANCE", "SIGNAL_DURATION",
    "now_utc", "new_event_id", "parse_dt",
    "summarize_availability",
    "SourceLineage", "Confidence", "EndurancePayload", "TimelineEvent",
]
