"""
data_capability_matrix.py
========================
EPOCH P0 — Data Capability Matrix (foundation layer over the timeline).

Answers: "Given the data you actually have, what can Epoch tell you **now**, what is
**derivable**, what **needs more history**, and what **needs a sensor/device**?"
It is a pure aggregation over the Canonical Athlete Timeline's per-signal confidence —
no domain logic (recovery/nutrition/strength) and no new data; it reports *coverage* and
*what would unlock more*, always with confidence. (Metric tiers from the Metric Math /
Data Ecosystem supplement: derivable-now vs needs-device.)

Framework-free and pure → fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from timeline_model import (
    AvailabilityState, EventType, TimelineEvent, now_utc,
    SIGNAL_HR, SIGNAL_POWER, SIGNAL_GPS, SIGNAL_ELEVATION, SIGNAL_CADENCE,
    SIGNAL_DISTANCE, SIGNAL_DURATION,
)

CAPABILITY_MATRIX_VERSION = "0.1.0"

COVER_THRESHOLD = 0.5   # a signal counts as "covered" if ≥50% of events have it
MIN_EVENTS = 3          # minimum events before history-based metrics are trusted

# Device-only signals the timeline does not carry yet (need a sensor/connector).
_DEVICE_SIGNALS = {"hrv", "sleep", "running_power", "spo2", "body_battery"}

# Friendly names for unlock messages.
_SIGNAL_LABEL = {
    SIGNAL_HR: "heart rate", SIGNAL_POWER: "power", SIGNAL_GPS: "GPS",
    SIGNAL_ELEVATION: "elevation", SIGNAL_CADENCE: "cadence",
    SIGNAL_DISTANCE: "distance", SIGNAL_DURATION: "duration",
    "hrv": "HRV", "sleep": "sleep", "running_power": "running power",
    "spo2": "SpO₂", "body_battery": "Body Battery",
}


class MetricStatus(str, Enum):
    AVAILABLE_NOW = "available_now"    # computable from current imported data
    ESTIMATE_ONLY = "estimate_only"    # only as a modeled estimate (lower confidence)
    NEEDS_HISTORY = "needs_history"    # signals present but not enough history yet
    NEEDS_SIGNAL = "needs_signal"      # a required signal/sensor is missing


@dataclass(frozen=True)
class _MetricSpec:
    key: str
    label: str
    requires_all: Tuple[str, ...] = ()           # all must be covered
    requires_any: Tuple[Tuple[str, ...], ...] = ()  # at least one inner group fully covered
    history_days: int = 0
    estimate: bool = False
    better_with: Tuple[str, ...] = ()


# Catalog (derivable-now vs needs-device per the metric supplement).
_CATALOG: Tuple[_MetricSpec, ...] = (
    _MetricSpec("session_load", "Session Load (TSS-equivalent)",
                requires_all=(SIGNAL_DURATION,), requires_any=((SIGNAL_POWER,), (SIGNAL_HR,))),
    _MetricSpec("fitness_fatigue_form", "Fitness / Fatigue / Form (CTL·ATL·TSB)",
                requires_all=(SIGNAL_DURATION,), requires_any=((SIGNAL_POWER,), (SIGNAL_HR,)),
                history_days=42),
    _MetricSpec("overreach_acwr", "Overreach risk (ACWR)",
                requires_all=(SIGNAL_DURATION,), requires_any=((SIGNAL_POWER,), (SIGNAL_HR,)),
                history_days=28),
    _MetricSpec("efficiency", "Aerobic efficiency / decoupling",
                requires_all=(SIGNAL_HR,), requires_any=((SIGNAL_POWER,), (SIGNAL_DISTANCE,))),
    _MetricSpec("durability", "Durability",
                requires_all=(SIGNAL_DURATION,), requires_any=((SIGNAL_POWER,), (SIGNAL_HR,)),
                history_days=42),
    _MetricSpec("climbing", "Climbing", requires_all=(SIGNAL_ELEVATION,)),
    _MetricSpec("aerobic_engine", "Aerobic engine trend",
                requires_all=(SIGNAL_HR,), requires_any=((SIGNAL_POWER,), (SIGNAL_DISTANCE,)),
                history_days=42),
    _MetricSpec("consistency", "Consistency", requires_all=(SIGNAL_DURATION,), history_days=21),
    _MetricSpec("volume", "Volume", requires_all=(SIGNAL_DURATION,), history_days=7),
    _MetricSpec("route_comparison", "This-route-over-time", requires_all=(SIGNAL_GPS,)),
    _MetricSpec("recovery_reserve", "Recovery Reserve (estimate)",
                requires_all=(SIGNAL_DURATION,), requires_any=((SIGNAL_POWER,), (SIGNAL_HR,)),
                history_days=7, estimate=True, better_with=("sleep", "hrv")),
    _MetricSpec("hrv_status", "HRV status", requires_all=("hrv",), history_days=30),
    _MetricSpec("running_power", "Running power / ground-contact", requires_all=("running_power",)),
)


@dataclass
class CapabilityRow:
    key: str
    label: str
    status: MetricStatus
    confidence: str                 # high / medium / low
    coverage: Dict[str, float] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    unlock: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "status": self.status.value,
            "confidence": self.confidence, "coverage": self.coverage,
            "missing": list(self.missing), "unlock": self.unlock,
        }


@dataclass
class CapabilityMatrix:
    rows: List[CapabilityRow] = field(default_factory=list)
    available_now: List[str] = field(default_factory=list)
    unlock_suggestions: List[str] = field(default_factory=list)
    n_events: int = 0
    span_days: int = 0
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability_matrix_version": CAPABILITY_MATRIX_VERSION,
            "n_events": self.n_events, "span_days": self.span_days,
            "summary": self.summary,
            "available_now": list(self.available_now),
            "unlock_suggestions": list(self.unlock_suggestions),
            "rows": [r.to_dict() for r in self.rows],
        }


def _signal_coverage(events: List[TimelineEvent]) -> Dict[str, float]:
    n = len(events)
    if n == 0:
        return {}
    usable = {AvailabilityState.AVAILABLE, AvailabilityState.DERIVED}
    cov: Dict[str, float] = {}
    for sig in (SIGNAL_HR, SIGNAL_POWER, SIGNAL_GPS, SIGNAL_ELEVATION,
                SIGNAL_CADENCE, SIGNAL_DISTANCE, SIGNAL_DURATION):
        hits = sum(1 for e in events if e.confidence.signals.get(sig) in usable)
        cov[sig] = round(hits / n, 2)
    return cov


def _covered(cov: Dict[str, float], sig: str) -> bool:
    return cov.get(sig, 0.0) >= COVER_THRESHOLD


def _evaluate(spec: _MetricSpec, cov: Dict[str, float], n: int, span_days: int) -> CapabilityRow:
    # 1) Missing required signals?
    missing_all = [s for s in spec.requires_all if not _covered(cov, s)]
    any_ok = (not spec.requires_any) or any(
        all(_covered(cov, s) for s in group) for group in spec.requires_any)
    any_missing: List[str] = []
    if spec.requires_any and not any_ok:
        # report the first option group as the missing requirement
        any_missing = list(spec.requires_any[0])

    needed = list(spec.requires_all) + [s for g in spec.requires_any for s in g]
    device_missing = [s for s in needed if s in _DEVICE_SIGNALS and cov.get(s, 0.0) < COVER_THRESHOLD]

    if device_missing:
        miss = sorted(set(device_missing))
        return CapabilityRow(
            spec.key, spec.label, MetricStatus.NEEDS_SIGNAL, "low",
            coverage={s: cov.get(s, 0.0) for s in needed if s not in _DEVICE_SIGNALS},
            missing=[_SIGNAL_LABEL.get(s, s) for s in miss],
            unlock=f"Connect a device that records {', '.join(_SIGNAL_LABEL.get(s, s) for s in miss)}.",
        )

    if missing_all or any_missing:
        miss = missing_all + any_missing
        labels = [_SIGNAL_LABEL.get(s, s) for s in miss]
        return CapabilityRow(
            spec.key, spec.label, MetricStatus.NEEDS_SIGNAL, "low",
            coverage={s: cov.get(s, 0.0) for s in needed},
            missing=labels,
            unlock=f"Record {', '.join(labels)} on your activities.",
        )

    # 2) Signals OK — enough history?
    if spec.history_days > 0 and (span_days < spec.history_days or n < MIN_EVENTS):
        more = max(spec.history_days - span_days, 0)
        return CapabilityRow(
            spec.key, spec.label, MetricStatus.NEEDS_HISTORY, "low",
            coverage={s: cov.get(s, 0.0) for s in needed},
            unlock=f"~{more} more days of history (have {span_days} of {spec.history_days}).",
        )

    # 3) Available. Confidence from coverage of the required signals.
    cov_vals = [cov.get(s, 0.0) for s in spec.requires_all] + \
               [max((cov.get(s, 0.0) for s in g), default=0.0) for g in spec.requires_any]
    avg_cov = sum(cov_vals) / len(cov_vals) if cov_vals else 1.0
    status = MetricStatus.ESTIMATE_ONLY if spec.estimate else MetricStatus.AVAILABLE_NOW
    if spec.estimate:
        confidence = "low"
        unlock = (f"Add {', '.join(_SIGNAL_LABEL.get(s, s) for s in spec.better_with)} "
                  f"to raise confidence.") if spec.better_with else None
    else:
        confidence = "high" if avg_cov >= 0.8 else "medium"
        unlock = None
    return CapabilityRow(
        spec.key, spec.label, status, confidence,
        coverage={s: cov.get(s, 0.0) for s in needed}, unlock=unlock,
    )


def capability_matrix(events: List[TimelineEvent],
                      as_of: Optional[datetime] = None) -> CapabilityMatrix:
    as_of = as_of or now_utc()
    endur = [e for e in events
             if e.event_type == EventType.ENDURANCE_WORKOUT
             and e.status.value == "active" and e.start_time]
    n = len(endur)
    if n == 0:
        return CapabilityMatrix(
            rows=[], n_events=0, span_days=0,
            summary="No activities yet — import FIT/GPX/TCX/CSV to see what Epoch can tell you.",
            unlock_suggestions=["Import your first activities."],
        )

    starts = [e.start_time for e in endur]
    span_days = (max(starts) - min(starts)).days
    cov = _signal_coverage(endur)

    rows = [_evaluate(spec, cov, n, span_days) for spec in _CATALOG]
    available_now = [r.label for r in rows if r.status == MetricStatus.AVAILABLE_NOW]
    unlocks = [r.unlock for r in rows if r.unlock and r.status in
               (MetricStatus.NEEDS_SIGNAL, MetricStatus.NEEDS_HISTORY)]
    # de-dup unlock suggestions while preserving order
    seen: set = set()
    unlock_suggestions = [u for u in unlocks if not (u in seen or seen.add(u))]

    summary = (f"From {n} activities over {span_days} days, Epoch can compute "
               f"{len(available_now)} metrics now"
               + (f"; {len(unlock_suggestions)} more would unlock with extra data."
                  if unlock_suggestions else "."))

    return CapabilityMatrix(
        rows=rows, available_now=available_now, unlock_suggestions=unlock_suggestions,
        n_events=n, span_days=span_days, summary=summary,
    )


__all__ = [
    "CAPABILITY_MATRIX_VERSION", "MetricStatus", "CapabilityRow", "CapabilityMatrix",
    "capability_matrix",
]
