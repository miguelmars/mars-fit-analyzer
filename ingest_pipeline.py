"""
ingest_pipeline.py
=================
EPOCH — Activity Ingestion pipeline (P0).

Orchestrates: file -> parse -> source detection -> sport detection -> normalize into a
canonical `TimelineEvent` (event_type = endurance_workout) -> confidence -> dedup with
source precedence -> persist via a `TimelineRepository` -> import log, with **safe
failure** (a bad file never corrupts the timeline; it produces a FAILED import log).

This module depends on `timeline_model` (core) and `ingest_parsers`. It treats the
repository as a duck-typed port (see `timeline_store.TimelineRepository`); it does not
import any storage/DB code directly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from timeline_model import (
    AvailabilityState, Confidence, ConfidenceLevel, EndurancePayload, EventStatus,
    EventType, FileType, ImportStatus, Source, SourceLineage, SportCategory,
    TimelineEvent, new_event_id, now_utc,
    summarize_availability,
    SIGNAL_CADENCE, SIGNAL_DISTANCE, SIGNAL_DURATION, SIGNAL_ELEVATION, SIGNAL_GPS,
    SIGNAL_HR, SIGNAL_POWER,
)
from ingest_parsers import INGEST_PARSER_VERSION, ParseError, ParsedActivity, parse

if TYPE_CHECKING:  # avoid runtime coupling to storage
    from timeline_store import TimelineRepository

INGEST_PIPELINE_VERSION = "0.1.0"

# ── Dedup thresholds (aligned with strava/dedup.py) ──────────────────────────
_EXACT_DATE_MIN, _EXACT_DIST_PCT, _EXACT_DUR_PCT = 5, 0.02, 0.03
_PROB_DATE_MIN, _PROB_DIST_PCT, _PROB_DUR_PCT = 30, 0.05, 0.10
_DEDUP_WINDOW_S = 3600  # only compare against events within ±1h

# ── Source precedence for the activity record (higher wins) ──────────────────
# Garmin/FIT carry the richest device+physiological data; Strava is transport/display;
# manual/unknown lowest. (See connector supplement §3: precedence by data type.)
_SOURCE_PRECEDENCE = {
    Source.GARMIN_EXPORT: 5,
    Source.FILE_UPLOAD: 4,
    Source.WAHOO_EXPORT: 4,
    Source.ZWIFT_EXPORT: 3,
    Source.MYWHOOSH_EXPORT: 3,
    Source.STRAVA_EXPORT: 2,
    Source.MANUAL_UPLOAD: 1,
    Source.UNKNOWN: 0,
}

# Trust per source for confidence (known device exports are trusted more).
_SOURCE_TRUST = {
    Source.GARMIN_EXPORT: 0.9,
    Source.WAHOO_EXPORT: 0.85,
    Source.ZWIFT_EXPORT: 0.8,
    Source.MYWHOOSH_EXPORT: 0.8,
    Source.STRAVA_EXPORT: 0.8,
    Source.FILE_UPLOAD: 0.6,
    Source.MANUAL_UPLOAD: 0.5,
    Source.UNKNOWN: 0.5,
}

_SPORT_MAP = {
    "cycling": SportCategory.CYCLING, "biking": SportCategory.CYCLING,
    "ride": SportCategory.CYCLING, "road_biking": SportCategory.CYCLING,
    "gravel_cycling": SportCategory.CYCLING, "mountain_biking": SportCategory.CYCLING,
    "virtual_ride": SportCategory.INDOOR_RIDE, "indoor_cycling": SportCategory.INDOOR_RIDE,
    "virtualride": SportCategory.INDOOR_RIDE,
    "running": SportCategory.RUNNING, "run": SportCategory.RUNNING,
    "treadmill_running": SportCategory.RUNNING,
    "trail_running": SportCategory.TRAIL_RUNNING, "trail": SportCategory.TRAIL_RUNNING,
    "walking": SportCategory.WALK, "walk": SportCategory.WALK,
    "hiking": SportCategory.HIKING, "hike": SportCategory.HIKING,
    "swimming": SportCategory.SWIM, "swim": SportCategory.SWIM,
    "strength_training": SportCategory.STRENGTH, "strength": SportCategory.STRENGTH,
}

# Relative weight of each signal in the confidence score (sums to 1.0).
_SIGNAL_WEIGHTS = {
    SIGNAL_DURATION: 0.25, SIGNAL_DISTANCE: 0.20, SIGNAL_HR: 0.20,
    SIGNAL_POWER: 0.15, SIGNAL_GPS: 0.08, SIGNAL_CADENCE: 0.07, SIGNAL_ELEVATION: 0.05,
}


# ─────────────────────────────────────────────────────────────────────────────
# Sport + source resolution
# ─────────────────────────────────────────────────────────────────────────────

def detect_sport(sport_hint: Optional[str]) -> str:
    if not sport_hint:
        return SportCategory.UNKNOWN.value
    key = str(sport_hint).strip().lower().replace(" ", "_")
    if key in _SPORT_MAP:
        return _SPORT_MAP[key].value
    return SportCategory.OTHER.value


def resolve_source(pa: ParsedActivity, declared: Optional[Source]) -> Source:
    """Declared source wins; else the detected source; else a generic file upload."""
    if declared and declared != Source.UNKNOWN:
        return declared
    if pa.source_hint and pa.source_hint != Source.UNKNOWN:
        return pa.source_hint
    return Source.FILE_UPLOAD


def source_precedence(source: Source) -> int:
    return _SOURCE_PRECEDENCE.get(source, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Confidence
# ─────────────────────────────────────────────────────────────────────────────

def build_confidence(pa: ParsedActivity, source: Source) -> Confidence:
    derived = AvailabilityState.DERIVED
    avail = AvailabilityState.AVAILABLE
    missing = AvailabilityState.MISSING
    unavail = AvailabilityState.UNAVAILABLE

    # GPX / per-record CSV compute distance & elevation from points → DERIVED.
    derive_geo = pa.file_type == FileType.GPX or (pa.file_type == FileType.CSV and pa.n_records > 0)
    # A summary CSV row structurally cannot carry a GPS track → UNAVAILABLE, not MISSING.
    summary_csv = pa.file_type == FileType.CSV and pa.n_records == 0

    def geo_state(present: bool) -> AvailabilityState:
        if present:
            return derived if derive_geo else avail
        return unavail if summary_csv else missing

    signals: Dict[str, AvailabilityState] = {
        SIGNAL_DURATION: avail if pa.duration_s else missing,
        SIGNAL_DISTANCE: geo_state(pa.has_distance) if derive_geo else (avail if pa.has_distance else missing),
        SIGNAL_HR: avail if pa.has_hr else missing,
        SIGNAL_POWER: avail if pa.has_power else missing,
        SIGNAL_GPS: (avail if pa.has_gps else (unavail if summary_csv else missing)),
        SIGNAL_CADENCE: avail if pa.has_cadence else missing,
        SIGNAL_ELEVATION: geo_state(pa.has_elevation) if derive_geo else (avail if pa.has_elevation else missing),
    }

    imported = [k for k, v in signals.items() if v == avail]
    derived_fields = [k for k, v in signals.items() if v == derived]
    missing_fields = [k for k, v in signals.items() if v == missing]
    unavailable_fields = [k for k, v in signals.items() if v == unavail]

    base = 0.0
    for sig, w in _SIGNAL_WEIGHTS.items():
        st = signals.get(sig)
        if st == avail:
            base += w
        elif st == derived:
            base += w * 0.6

    source_conf = _SOURCE_TRUST.get(source, 0.5)
    parsing_conf = 0.8 if any("assumed" in w.lower() for w in pa.warnings) else 0.95
    score = base * (0.7 + 0.3 * source_conf) * parsing_conf

    flags: List[str] = []
    if not pa.has_hr:
        flags.append("missing_sensor_data")
    if not pa.duration_s or not pa.has_distance:
        flags.append("partial_data")
    if derived_fields:
        flags.append("derived_metrics")

    key = {SIGNAL_DURATION, SIGNAL_DISTANCE, SIGNAL_HR}
    missing_key = [k for k in missing_fields if k in key]

    return Confidence(
        score=round(min(score, 1.0), 4),
        level=Confidence.level_from_score(min(score, 1.0)),
        source_confidence=round(source_conf, 4),
        parsing_confidence=round(parsing_conf, 4),
        signals=signals,
        imported_fields=imported,
        derived_fields=derived_fields,
        estimated_fields=[],
        unavailable_fields=unavailable_fields,
        missing_key_fields=missing_key,
        data_flags=flags,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Normalizer  (ParsedActivity -> TimelineEvent)
# ─────────────────────────────────────────────────────────────────────────────

def normalize(
    pa: ParsedActivity,
    athlete_id: str,
    *,
    declared_source: Optional[Source] = None,
    upload_method: str = "file_upload",
    file_hash: Optional[str] = None,
    raw_ref: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> TimelineEvent:
    source = resolve_source(pa, declared_source)
    sport = detect_sport(pa.sport_hint)

    lineage = SourceLineage(
        source=source,
        upload_method=upload_method,
        original_filename=original_filename,
        file_type=pa.file_type,
        file_hash=file_hash,
        parser=pa.parser,
        parser_version=pa.parser_version or INGEST_PARSER_VERSION,
        raw_payload_ref=raw_ref,
        detected_source=pa.source_hint,
    )

    payload = EndurancePayload(
        sport_type=pa.sport_hint,
        distance_m=pa.distance_m,
        duration_s=pa.duration_s,
        moving_time_s=pa.moving_time_s,
        elapsed_time_s=pa.elapsed_time_s,
        elevation_gain_m=pa.elevation_gain_m,
        elevation_loss_m=pa.elevation_loss_m,
        avg_hr=pa.avg_hr,
        max_hr=pa.max_hr,
        avg_cadence=pa.avg_cadence,
        max_cadence=pa.max_cadence,
        avg_power=pa.avg_power,
        max_power=pa.max_power,
        normalized_power=pa.normalized_power,
        avg_speed_mps=pa.avg_speed_mps,
        max_speed_mps=pa.max_speed_mps,
        calories=pa.calories,
        device=pa.device,
        original_name=pa.original_name,
        hr_series_available=pa.has_hr,
        power_series_available=pa.has_power,
        gps_available=pa.has_gps,
        route_available=pa.has_gps,
        laps=pa.laps,
    ).to_dict()

    confidence = build_confidence(pa, source)

    end_time = None
    if pa.start_time and pa.duration_s:
        end_time = pa.start_time + timedelta(seconds=pa.duration_s)

    summary = {
        "sport": sport,
        "distance_km": round(pa.distance_m / 1000.0, 2) if pa.distance_m else None,
        "duration_min": round(pa.duration_s / 60.0, 1) if pa.duration_s else None,
        "avg_hr": pa.avg_hr,
        "avg_power": pa.avg_power,
        "elevation_gain_m": pa.elevation_gain_m,
        "source": source.value,
        "confidence_level": confidence.level.value,
        "name": pa.original_name,
    }

    return TimelineEvent.create(
        athlete_id=athlete_id,
        event_type=EventType.ENDURANCE_WORKOUT,
        start_time=pa.start_time,
        source=lineage,
        confidence=confidence,
        payload=payload,
        duration_sec=pa.duration_s,
        end_time=end_time,
        sport_category=sport,
        raw_import_reference=raw_ref,
        availability_state=summarize_availability(confidence.signals),
        normalized_summary=summary,
        notes=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────

def _pct_diff(a: Optional[float], b: Optional[float]) -> float:
    if not a or not b:
        return 0.0
    return abs(a - b) / max(a, b)


def _classify(
    date_min: float,
    dist_pct: float,
    dur_pct: float,
    *,
    has_distance_pair: bool,
    has_duration_pair: bool,
) -> str:
    # Without distance+duration on both sides, a close timestamp is not enough to
    # call something an exact duplicate. Keep it reviewable instead.
    if not (has_distance_pair and has_duration_pair):
        if date_min <= _PROB_DATE_MIN:
            return "probable"
        return "none"
    if date_min <= _EXACT_DATE_MIN and dist_pct <= _EXACT_DIST_PCT and dur_pct <= _EXACT_DUR_PCT:
        return "exact"
    if date_min <= _PROB_DATE_MIN and dist_pct <= _PROB_DIST_PCT and dur_pct <= _PROB_DUR_PCT:
        return "probable"
    return "none"


def detect_duplicate(repo: "TimelineRepository", event: TimelineEvent) -> Tuple[str, Optional[TimelineEvent], str]:
    """Return (decision, other_event, reason).

    decision ∈ {"duplicate", "possible_duplicate", "new"}:
      * same file_hash, or same activity (exact time/distance/duration) -> "duplicate"
      * near match (probable) -> "possible_duplicate" (kept active, flagged for review)
      * otherwise -> "new"
    """
    athlete_id = event.athlete_id
    fh = event.source.file_hash
    if fh:
        existing = repo.find_by_file_hash(athlete_id, fh)
        if existing and existing.event_id != event.event_id:
            return "duplicate", existing, "same file_hash"

    if not event.start_time:
        return "new", None, "no start_time to compare"

    cands = repo.find_dedup_candidates(athlete_id, event.start_time, _DEDUP_WINDOW_S)
    dist = event.payload.get("distance_m")
    dur = event.duration_sec
    best_prob: Optional[TimelineEvent] = None
    for c in cands:
        if c.event_id == event.event_id or c.status == EventStatus.FAILED:
            continue
        if not c.start_time:
            continue
        date_min = abs((event.start_time - c.start_time).total_seconds()) / 60.0
        other_dist = c.payload.get("distance_m")
        other_dur = c.duration_sec
        has_distance_pair = bool(dist and other_dist)
        has_duration_pair = bool(dur and other_dur)
        dist_pct = _pct_diff(dist, other_dist)
        dur_pct = _pct_diff(dur, other_dur)
        verdict = _classify(
            date_min,
            dist_pct,
            dur_pct,
            has_distance_pair=has_distance_pair,
            has_duration_pair=has_duration_pair,
        )
        if verdict == "exact":
            return "duplicate", c, f"exact match (Δ{date_min:.1f}min)"
        if verdict == "probable" and best_prob is None:
            best_prob = c
    if best_prob is not None:
        return "possible_duplicate", best_prob, "probable match (needs review)"
    return "new", None, "no match"


# ─────────────────────────────────────────────────────────────────────────────
# Import log + result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ImportLog:
    import_id: str
    athlete_id: str
    status: ImportStatus
    received_at: Any = field(default_factory=now_utc)
    original_filename: Optional[str] = None
    file_type: Optional[FileType] = None
    file_hash: Optional[str] = None
    source: Source = Source.UNKNOWN
    parser: Optional[str] = None
    parser_version: Optional[str] = None
    event_id: Optional[str] = None
    duplicate_of: Optional[str] = None
    error_message: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "import_id": self.import_id,
            "athlete_id": self.athlete_id,
            "status": self.status.value if isinstance(self.status, ImportStatus) else self.status,
            "received_at": self.received_at.isoformat() if hasattr(self.received_at, "isoformat") else self.received_at,
            "original_filename": self.original_filename,
            "file_type": self.file_type.value if isinstance(self.file_type, FileType) else self.file_type,
            "file_hash": self.file_hash,
            "source": self.source.value if isinstance(self.source, Source) else self.source,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "event_id": self.event_id,
            "duplicate_of": self.duplicate_of,
            "error_message": self.error_message,
            "warnings": list(self.warnings),
        }


@dataclass
class IngestResult:
    ok: bool
    status: ImportStatus
    import_log: ImportLog
    event: Optional[TimelineEvent] = None
    duplicate_of: Optional[str] = None
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def ingest_file(
    data: bytes,
    filename: Optional[str],
    athlete_id: str,
    repo: "TimelineRepository",
    *,
    declared_source: Optional[Source] = None,
    upload_method: str = "file_upload",
    raw_ref: Optional[str] = None,
) -> IngestResult:
    """Ingest one uploaded file into the Canonical Athlete Timeline.

    Safe failure: any parse/normalize error is captured as a FAILED import log; no
    partial/corrupt event is written to the timeline.
    """
    file_hash = hashlib.sha256(data).hexdigest() if data else None
    raw_ref = raw_ref or (f"sha256:{file_hash}" if file_hash else None)
    log = ImportLog(
        import_id=new_event_id("imp"),
        athlete_id=athlete_id,
        status=ImportStatus.RECEIVED,
        original_filename=filename,
        file_hash=file_hash,
    )

    try:
        pa = parse(data, filename)
        log.status = ImportStatus.PARSED
        log.file_type = pa.file_type
        log.parser = pa.parser
        log.parser_version = pa.parser_version
        log.warnings = list(pa.warnings)

        event = normalize(
            pa, athlete_id,
            declared_source=declared_source,
            upload_method=upload_method,
            file_hash=file_hash,
            raw_ref=raw_ref,
            original_filename=filename,
        )
        log.status = ImportStatus.NORMALIZED
        log.source = event.source.source
        log.event_id = event.event_id

        decision, other, reason = detect_duplicate(repo, event)

        if decision == "duplicate":
            result = _handle_duplicate(repo, event, other, reason, log)
        elif decision == "possible_duplicate":
            # Uncertain: mark status (not deleted), keep in the timeline, flag for review.
            event.status = EventStatus.DUPLICATE_UNCERTAIN
            event.confidence.data_flags.append("possible_duplicate")
            other_id = other.event_id if other else None
            if other_id:
                event.linked_event_ids.append(other_id)
            repo.save_event(event)
            log.status = ImportStatus.IMPORTED
            log.warnings.append(f"possible duplicate of {other_id or '?'} ({reason}) — marked uncertain, not deleted")
            result = IngestResult(True, ImportStatus.IMPORTED, log, event=event, duplicate_of=other_id)
        else:
            repo.save_event(event)
            log.status = ImportStatus.IMPORTED
            result = IngestResult(True, ImportStatus.IMPORTED, log, event=event)

    except ParseError as e:
        log.status = ImportStatus.FAILED
        log.error_message = str(e)
        result = IngestResult(False, ImportStatus.FAILED, log, error=str(e))
    except Exception as e:  # never let an unexpected error corrupt the timeline
        log.status = ImportStatus.FAILED
        log.error_message = f"unexpected error: {e}"
        result = IngestResult(False, ImportStatus.FAILED, log, error=str(e))

    repo.save_import_log(log)
    return result


def _handle_duplicate(
    repo: "TimelineRepository",
    event: TimelineEvent,
    other: Optional[TimelineEvent],
    reason: str,
    log: ImportLog,
) -> IngestResult:
    """Resolve an exact duplicate using source precedence (by data type).

    If the new event's source outranks the existing one, the new event becomes the
    primary and the existing is demoted to DUPLICATE (lineage preserved, reversible).
    Otherwise the new event is stored as DUPLICATE. Nothing is ever deleted.
    """
    if other is None:
        repo.save_event(event)
        log.status = ImportStatus.IMPORTED
        return IngestResult(True, ImportStatus.IMPORTED, log, event=event)

    new_rank = source_precedence(event.source.source)
    old_rank = source_precedence(other.source.source)

    if new_rank > old_rank:
        # New wins: demote the existing one, keep lineage link both ways.
        other.status = EventStatus.DUPLICATE
        other.linked_event_ids.append(event.event_id)
        other.confidence.data_flags.append("superseded")
        other.updated_at = now_utc()
        repo.save_event(other)

        event.source.merged_from.append(other.event_id)
        event.linked_event_ids.append(other.event_id)
        repo.save_event(event)
        log.status = ImportStatus.IMPORTED
        log.duplicate_of = None
        log.warnings.append(f"replaced lower-precedence duplicate {other.event_id} ({reason})")
        return IngestResult(True, ImportStatus.IMPORTED, log, event=event, duplicate_of=other.event_id)

    # Existing wins: store new as duplicate.
    event.status = EventStatus.DUPLICATE
    event.linked_event_ids.append(other.event_id)
    event.confidence.data_flags.append("duplicate")
    repo.save_event(event)
    log.status = ImportStatus.DUPLICATE
    log.duplicate_of = other.event_id
    log.warnings.append(f"duplicate of {other.event_id} ({reason})")
    return IngestResult(True, ImportStatus.DUPLICATE, log, event=event, duplicate_of=other.event_id)


__all__ = [
    "INGEST_PIPELINE_VERSION", "ImportLog", "IngestResult",
    "ingest_file", "normalize", "build_confidence", "detect_duplicate",
    "detect_sport", "resolve_source", "source_precedence",
]
