"""
data_quality_audit.py
=====================
EPOCH P0 — Data Quality + Zone Audit (build step #2, runs ON the timeline).

"Before judging the athlete, Epoch checks the data." This layer runs over normalized
timeline events (from ingestion) + the athlete profile (HR max, threshold HR, FTP, zones)
and flags problems **before** any coaching conclusion. It explains what's wrong and why
it matters, and **suggests** validation — it NEVER changes zones/FTP automatically
(spec: suggest, ask approval; no medical claims).

Design: framework-free and pure (no FastAPI / DB / vendor SDKs), so it is fully testable.
It consumes the `confidence.data_flags` / `signals` already produced by ingestion and adds
the profile-based checks that need the athlete's settings.

Checks (per the spec):
  suspicious_hr_max · incorrect_zones · stale_ftp · mislabeled_activity ·
  unreliable_power · missing_sensor_data · duplicate · inconsistent_source
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from timeline_model import (
    AvailabilityState, EventStatus, EventType, TimelineEvent,
    SIGNAL_HR, now_utc,
)

AUDIT_VERSION = "0.1.0"

STALE_FTP_DAYS = 180                 # ~6 months without an FTP test → stale
FTP_CONTRADICTION_RATIO = 1.05       # recent power efforts > 105% of FTP → FTP likely low
RECENT_WINDOW_DAYS = 90             # window for "recent efforts"
MISLABEL_EASY_HR_RATIO = 0.90       # avg HR ≥ 90% of threshold on an "easy/recovery" label
IMPLAUSIBLE_MAX_POWER_W = 2500       # summary-level plausibility ceiling
SOURCE_DISAGREE_PCT = 0.10          # >10% disagreement between merged sources

_EASY_LABEL_WORDS = (
    "recovery", "recuperacion", "recuperación", "easy", "rest", "regenerative",
    "regenerativo", "z1", "zone 1", "zona 1", "descanso", "suave",
)


class Severity(str, Enum):
    HIGH = "high"      # 🔴 affects conclusions broadly (e.g. all HR zones)
    MEDIUM = "medium"  # 🟠 worth validating
    LOW = "low"        # 🟡 informational


@dataclass
class AuditFlag:
    code: str
    severity: Severity
    message: str
    suggested_action: str
    event_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "suggested_action": self.suggested_action,
            "event_id": self.event_id,
        }


@dataclass
class AthleteProfile:
    """The athlete's current settings the audit checks against. All optional — the audit
    only runs the checks it has data for, and says what it could not check."""

    hr_max: Optional[int] = None
    lthr: Optional[int] = None                     # threshold HR (bpm)
    ftp_w: Optional[int] = None
    ftp_set_date: Optional[datetime] = None
    hr_zones: Optional[List[Tuple[int, int]]] = None  # ascending [(lo, hi), ...]
    zones_set_date: Optional[datetime] = None


@dataclass
class AthleteDataHealth:
    """Per-athlete 'health of your data' panel."""

    flags: List[AuditFlag] = field(default_factory=list)
    zones_reliable: Optional[bool] = None     # None = could not determine
    ftp_current: Optional[bool] = None
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    checked_events: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_version": AUDIT_VERSION,
            "zones_reliable": self.zones_reliable,
            "ftp_current": self.ftp_current,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "low_count": self.low_count,
            "checked_events": self.checked_events,
            "flags": [f.to_dict() for f in self.flags],
            "notes": list(self.notes),
        }


# ── helpers ──────────────────────────────────────────────────────────────────

def _num(event: TimelineEvent, key: str) -> Optional[float]:
    v = event.payload.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _name(event: TimelineEvent) -> str:
    return (event.payload.get("original_name")
            or (event.normalized_summary or {}).get("name") or "")


def _is_endurance(event: TimelineEvent) -> bool:
    return event.event_type == EventType.ENDURANCE_WORKOUT


# ── per-event checks ─────────────────────────────────────────────────────────

def check_suspicious_hr_max(event: TimelineEvent, profile: AthleteProfile) -> Optional[AuditFlag]:
    max_hr = _num(event, "max_hr")
    if max_hr is None or profile.hr_max is None:
        return None
    if max_hr > profile.hr_max:
        return AuditFlag(
            code="suspicious_hr_max",
            severity=Severity.HIGH,
            message=(f"Saw {int(max_hr)} bpm but your declared max HR is {profile.hr_max} → "
                     f"your HR zones may be off (this affects every zone)."),
            suggested_action="Review your max HR / threshold; consider a fresh test before trusting zones.",
            event_id=event.event_id,
        )
    return None


def check_mislabeled_activity(event: TimelineEvent, profile: AthleteProfile) -> Optional[AuditFlag]:
    if profile.lthr is None:
        return None
    avg_hr = _num(event, "avg_hr")
    if avg_hr is None:
        return None
    name = _name(event).lower()
    looks_easy = any(w in name for w in _EASY_LABEL_WORDS)
    if looks_easy and avg_hr >= MISLABEL_EASY_HR_RATIO * profile.lthr:
        return AuditFlag(
            code="mislabeled_activity",
            severity=Severity.MEDIUM,
            message=(f"This session is labeled easy/recovery but average HR was {int(avg_hr)} bpm "
                     f"(near your threshold {profile.lthr})."),
            suggested_action="Check the activity label/type before using it as a recovery day.",
            event_id=event.event_id,
        )
    return None


def check_unreliable_power(event: TimelineEvent, profile: AthleteProfile) -> Optional[AuditFlag]:
    max_power = _num(event, "max_power")
    avg_power = _num(event, "avg_power")
    if max_power is None:
        return None
    implausible = max_power > IMPLAUSIBLE_MAX_POWER_W
    spiky = avg_power is not None and avg_power > 0 and max_power > avg_power * 8
    if implausible or spiky:
        return AuditFlag(
            code="unreliable_power",
            severity=Severity.MEDIUM,
            message=(f"Power looks off (max {int(max_power)} W). Sensor spikes/dropouts can make "
                     f"the numbers unreliable."),
            suggested_action="Treat power-derived metrics with lower confidence; check sensor/calibration.",
            event_id=event.event_id,
        )
    return None


def check_missing_sensor_data(event: TimelineEvent, profile: AthleteProfile) -> Optional[AuditFlag]:
    sig = event.confidence.signals
    if sig.get(SIGNAL_HR) == AvailabilityState.MISSING or "missing_sensor_data" in event.confidence.data_flags:
        return AuditFlag(
            code="missing_sensor_data",
            severity=Severity.LOW,
            message="No heart-rate data on this activity; some readings rely on estimates.",
            suggested_action="Wear/enable the HR sensor next time for higher-confidence analysis.",
            event_id=event.event_id,
        )
    return None


def check_duplicate(event: TimelineEvent, profile: AthleteProfile) -> Optional[AuditFlag]:
    flags = event.confidence.data_flags
    if event.status == EventStatus.DUPLICATE or "duplicate" in flags:
        return AuditFlag(
            code="duplicate",
            severity=Severity.LOW,
            message="This activity arrived from more than one source; kept linked, not double-counted.",
            suggested_action="No action needed — lineage is preserved.",
            event_id=event.event_id,
        )
    if event.status == EventStatus.DUPLICATE_UNCERTAIN or "possible_duplicate" in flags:
        return AuditFlag(
            code="duplicate",
            severity=Severity.LOW,
            message="This looks like a possible duplicate (uncertain). Kept, not deleted.",
            suggested_action="Review and confirm if it is the same activity from another source.",
            event_id=event.event_id,
        )
    return None


_PER_EVENT_CHECKS = (
    check_suspicious_hr_max,
    check_mislabeled_activity,
    check_unreliable_power,
    check_missing_sensor_data,
    check_duplicate,
)


def audit_event(event: TimelineEvent, profile: AthleteProfile) -> List[AuditFlag]:
    """Run all per-activity checks. Returns [] for a clean endurance event."""
    if not _is_endurance(event):
        return []
    out: List[AuditFlag] = []
    for check in _PER_EVENT_CHECKS:
        flag = check(event, profile)
        if flag is not None:
            out.append(flag)
    return out


# ── profile / athlete-level checks ───────────────────────────────────────────

def check_incorrect_zones(profile: AthleteProfile) -> Optional[AuditFlag]:
    zones = profile.hr_zones
    if not zones:
        return None
    flat = [b for z in zones for b in z]
    ascending = all(flat[i] <= flat[i + 1] for i in range(len(flat) - 1))
    top = zones[-1][1]
    problems = []
    if not ascending:
        problems.append("zone bounds are not in ascending order")
    if profile.hr_max is not None and top > profile.hr_max:
        problems.append(f"top zone ({top}) is above your max HR ({profile.hr_max})")
    if profile.lthr is not None and not (zones[0][0] <= profile.lthr <= top):
        problems.append(f"your threshold ({profile.lthr}) falls outside the zone range")
    if problems:
        return AuditFlag(
            code="incorrect_zones",
            severity=Severity.HIGH,
            message="Your HR zones don't line up with your settings: " + "; ".join(problems) + ".",
            suggested_action="Re-anchor zones to a recent threshold test before trusting zone-based readings.",
        )
    return None


def check_stale_ftp(profile: AthleteProfile, events: List[TimelineEvent],
                    as_of: datetime) -> Optional[AuditFlag]:
    if profile.ftp_w is None:
        return None
    reasons = []
    if profile.ftp_set_date is not None:
        age_days = (as_of - profile.ftp_set_date).days
        if age_days >= STALE_FTP_DAYS:
            reasons.append(f"it was set {age_days} days ago")
    # Recent power efforts contradicting the FTP.
    cutoff = as_of - timedelta(days=RECENT_WINDOW_DAYS)
    recent_peak = None
    for e in events:
        if not _is_endurance(e) or not e.start_time or e.start_time < cutoff:
            continue
        for key in ("normalized_power", "avg_power"):
            p = _num(e, key)
            if p is not None:
                recent_peak = p if recent_peak is None else max(recent_peak, p)
    if recent_peak is not None and recent_peak > profile.ftp_w * FTP_CONTRADICTION_RATIO:
        reasons.append(f"recent efforts (~{int(recent_peak)} W) exceed it")
    if reasons:
        return AuditFlag(
            code="stale_ftp",
            severity=Severity.MEDIUM,
            message=(f"Your FTP ({profile.ftp_w} W) may be out of date: " + " and ".join(reasons) + "."),
            suggested_action="Consider an FTP test; Epoch will not change it automatically.",
        )
    return None


def check_inconsistent_source(events: List[TimelineEvent]) -> List[AuditFlag]:
    """Compare merged/linked events: if the same activity from two sources disagrees a lot."""
    by_id = {e.event_id: e for e in events}
    out: List[AuditFlag] = []
    seen: set = set()
    for e in events:
        for other_id in e.source.merged_from:
            other = by_id.get(other_id)
            if other is None:
                continue
            pair = tuple(sorted((e.event_id, other_id)))
            if pair in seen:
                continue
            seen.add(pair)
            d1, d2 = _num(e, "distance_m"), _num(other, "distance_m")
            if d1 and d2 and abs(d1 - d2) / max(d1, d2) > SOURCE_DISAGREE_PCT:
                out.append(AuditFlag(
                    code="inconsistent_source",
                    severity=Severity.LOW,
                    message=(f"Sources disagree on distance ({d1/1000:.1f} vs {d2/1000:.1f} km); "
                             f"kept the higher-precedence source ({e.source.source.value})."),
                    suggested_action="No action needed; review if the gap looks wrong.",
                    event_id=e.event_id,
                ))
    return out


def audit_athlete(events: List[TimelineEvent], profile: AthleteProfile,
                  as_of: Optional[datetime] = None) -> AthleteDataHealth:
    """Per-athlete data-health panel: profile checks + every event's checks + source consistency."""
    as_of = as_of or now_utc()
    endurance = [e for e in events if _is_endurance(e)]
    flags: List[AuditFlag] = []

    zones_flag = check_incorrect_zones(profile)
    if zones_flag:
        flags.append(zones_flag)
    ftp_flag = check_stale_ftp(profile, endurance, as_of)
    if ftp_flag:
        flags.append(ftp_flag)
    flags.extend(check_inconsistent_source(endurance))
    for e in endurance:
        flags.extend(audit_event(e, profile))

    high = sum(1 for f in flags if f.severity == Severity.HIGH)
    medium = sum(1 for f in flags if f.severity == Severity.MEDIUM)
    low = sum(1 for f in flags if f.severity == Severity.LOW)

    has_hr_max_issue = any(f.code == "suspicious_hr_max" for f in flags)
    zones_reliable = None
    if profile.hr_zones is not None or profile.hr_max is not None:
        zones_reliable = not (zones_flag is not None or has_hr_max_issue)
    ftp_current = None
    if profile.ftp_w is not None:
        ftp_current = ftp_flag is None

    notes = []
    if profile.hr_max is None:
        notes.append("Max HR not set — could not check for suspicious HR.")
    if profile.ftp_w is None:
        notes.append("FTP not set — skipped FTP staleness check.")
    if not profile.hr_zones:
        notes.append("HR zones not provided — skipped zone-consistency check.")

    return AthleteDataHealth(
        flags=flags, zones_reliable=zones_reliable, ftp_current=ftp_current,
        high_count=high, medium_count=medium, low_count=low,
        checked_events=len(endurance), notes=notes,
    )


def gating_note(flags: List[AuditFlag]) -> Optional[str]:
    """Confidence gating for downstream layers (Debrief / Goal Readiness): if any HIGH
    flag exists, return a sentence they MUST show before concluding. Else None."""
    highs = [f for f in flags if f.severity == Severity.HIGH]
    if not highs:
        return None
    reasons = "; ".join(sorted({f.code.replace("_", " ") for f in highs}))
    return (f"This reading may be imprecise because your data has unresolved issues ({reasons}). "
            f"Validate before treating it as a confident conclusion.")


__all__ = [
    "AUDIT_VERSION", "Severity", "AuditFlag", "AthleteProfile", "AthleteDataHealth",
    "audit_event", "audit_athlete", "gating_note",
    "check_suspicious_hr_max", "check_mislabeled_activity", "check_unreliable_power",
    "check_missing_sensor_data", "check_duplicate", "check_incorrect_zones",
    "check_stale_ftp", "check_inconsistent_source",
]
