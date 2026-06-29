"""
ingest_parsers.py
================
EPOCH — Activity Ingestion parsers (P0).

Turns a raw uploaded file (FIT / GPX / TCX / CSV, or a ZIP containing a FIT) into a
neutral `ParsedActivity` intermediate. Parsers do NOT know about the timeline storage
or the canonical event — that mapping happens in `ingest_pipeline.py` (normalizer).

Rules:
    * Store what is available; never invent missing data. Missing values stay None and
      the `has_*` flags say what was actually present.
    * Bad input raises `ParseError` — the pipeline turns that into a safe failure
      (import log = failed) and never corrupts the timeline.
    * FIT parsing reuses the existing `decode_fit.py` helpers.
    * XML parsing uses the stdlib only (namespace-agnostic by local tag name).
"""

from __future__ import annotations

import csv as _csv
import io
import math
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from timeline_model import FileType, Source, parse_dt

INGEST_PARSER_VERSION = "0.1.0"


class ParseError(Exception):
    """Raised when a file cannot be parsed into a ParsedActivity."""


@dataclass
class ParsedActivity:
    """Neutral, source-agnostic result of parsing one activity file."""

    file_type: FileType = FileType.UNKNOWN
    parser: str = ""
    parser_version: str = INGEST_PARSER_VERSION
    start_time: Optional[datetime] = None
    sport_hint: Optional[str] = None        # raw sport string from the file
    device: Optional[str] = None
    original_name: Optional[str] = None
    source_hint: Source = Source.UNKNOWN    # detected from file content (creator/author)

    # Metrics (SI units: meters, seconds, m/s; bpm; rpm; watts; kcal)
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
    calories: Optional[float] = None

    # Availability flags (what was actually present in the file)
    has_hr: bool = False
    has_power: bool = False
    has_gps: bool = False
    has_elevation: bool = False
    has_cadence: bool = False
    has_distance: bool = False

    laps: List[Dict[str, Any]] = field(default_factory=list)
    n_records: int = 0
    warnings: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _avg(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in meters between two (lat, lon) points."""
    r = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _parse_duration_s(v: Any) -> Optional[int]:
    """Accept seconds (int/float/str) or hh:mm:ss / mm:ss."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            parts = [float(p) for p in parts]
        except ValueError:
            return None
        while len(parts) < 3:
            parts.insert(0, 0.0)
        h, m, sec = parts[-3], parts[-2], parts[-1]
        return int(h * 3600 + m * 60 + sec)
    f = _to_float(s)
    return int(f) if f is not None else None


def _ln(tag: str) -> str:
    """Local name of an XML tag (drops the namespace)."""
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str):
    for el in root.iter():
        if _ln(el.tag) == name:
            yield el


def _first_local(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el.iter():
        if _ln(c.tag) == name:
            return c
    return None


def _text_local(el: ET.Element, name: str) -> Optional[str]:
    c = _first_local(el, name)
    return c.text if (c is not None and c.text is not None) else None


_SOURCE_KEYWORDS = [
    ("garmin", Source.GARMIN_EXPORT),
    ("strava", Source.STRAVA_EXPORT),
    ("zwift", Source.ZWIFT_EXPORT),
    ("mywhoosh", Source.MYWHOOSH_EXPORT),
    ("wahoo", Source.WAHOO_EXPORT),
]


def _source_from_text(s: Optional[str]) -> Source:
    """Map a creator/author/manufacturer string to a known Source, else UNKNOWN."""
    if not s:
        return Source.UNKNOWN
    low = str(s).lower()
    for kw, src in _SOURCE_KEYWORDS:
        if kw in low:
            return src
    return Source.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# File-type detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_file_type(filename: Optional[str], data: bytes) -> FileType:
    """Detect type by content first (more reliable), then by extension."""
    head = data[:512] if data else b""
    if head[:4] == b"PK\x03\x04":
        return FileType.ZIP
    # FIT files carry ".FIT" at bytes 8..12 of the header.
    if len(data) >= 12 and data[8:12] == b".FIT":
        return FileType.FIT
    sniff = head.lstrip()[:256].lower()
    if b"<gpx" in sniff:
        return FileType.GPX
    if b"<trainingcenterdatabase" in sniff:
        return FileType.TCX
    name = (filename or "").lower()
    for ext, ft in ((".fit", FileType.FIT), (".gpx", FileType.GPX),
                    (".tcx", FileType.TCX), (".csv", FileType.CSV), (".zip", FileType.ZIP)):
        if name.endswith(ext):
            return ft
    # Last resort: looks like XML vs CSV.
    if sniff.startswith(b"<?xml") or sniff.startswith(b"<"):
        if b"gpx" in sniff:
            return FileType.GPX
        if b"trainingcenter" in sniff:
            return FileType.TCX
    if b"," in head:
        return FileType.CSV
    return FileType.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# FIT  (reuses decode_fit.py helpers)
# ─────────────────────────────────────────────────────────────────────────────

def parse_fit(data: bytes) -> ParsedActivity:
    try:
        import fitparse  # noqa: WPS433 (optional dependency, present in this repo)
    except ImportError as e:  # pragma: no cover
        raise ParseError("fitparse not installed") from e
    from decode_fit import extract_session, extract_records, extract_laps

    try:
        fit = fitparse.FitFile(io.BytesIO(data))
        session = extract_session(fit) or {}
        records = extract_records(fit) or []
        laps = extract_laps(fit) or []
    except Exception as e:
        raise ParseError(f"FIT parse failed: {e}") from e

    pa = ParsedActivity(file_type=FileType.FIT, parser="fit_parser")
    pa.start_time = _aware(session.get("start_time"))
    pa.sport_hint = session.get("sport")
    pa.original_name = session.get("workout_name") or session.get("event")
    pa.n_records = len(records)

    pa.distance_m = _to_float(session.get("total_distance"))
    pa.elapsed_time_s = int(session["total_elapsed_time"]) if session.get("total_elapsed_time") else None
    pa.moving_time_s = int(session["total_timer_time"]) if session.get("total_timer_time") else None
    pa.duration_s = pa.moving_time_s or pa.elapsed_time_s
    pa.elevation_gain_m = _to_float(session.get("total_ascent"))
    pa.elevation_loss_m = _to_float(session.get("total_descent"))
    pa.avg_hr = _to_float(session.get("avg_heart_rate"))
    pa.max_hr = _to_float(session.get("max_heart_rate"))
    pa.avg_cadence = _to_float(session.get("avg_cadence"))
    pa.max_cadence = _to_float(session.get("max_cadence"))
    pa.avg_power = _to_float(session.get("avg_power"))
    pa.max_power = _to_float(session.get("max_power"))
    pa.normalized_power = _to_float(session.get("normalized_power"))
    pa.avg_speed_mps = _to_float(session.get("avg_speed") or session.get("enhanced_avg_speed"))
    pa.max_speed_mps = _to_float(session.get("max_speed") or session.get("enhanced_max_speed"))
    pa.calories = _to_float(session.get("total_calories"))

    # Availability from the actual second-by-second records (decode_fit row shape).
    pa.has_hr = pa.avg_hr is not None or any(r.get("heart_rate_bpm") not in (None, "") for r in records)
    pa.has_cadence = pa.avg_cadence is not None or any(r.get("cadence_rpm") not in (None, "") for r in records)
    pa.has_elevation = pa.elevation_gain_m is not None or any(r.get("altitude_m") not in (None, "") for r in records)
    pa.has_gps = any(r.get("lat") not in (None, "") and r.get("lon") not in (None, "") for r in records)
    pa.has_distance = pa.distance_m is not None or any(r.get("distance_m") not in (None, "") for r in records)
    pa.has_power = pa.avg_power is not None or pa.normalized_power is not None

    # Manufacturer → device + source hint (best-effort).
    try:
        for fid in fit.get_messages("file_id"):
            vals = {d.name: d.value for d in fid if d.value is not None}
            man = vals.get("manufacturer")
            prod = vals.get("garmin_product") or vals.get("product")
            if man:
                pa.device = str(prod or man)
                pa.source_hint = _source_from_text(str(man))
            break
    except Exception:
        pass

    pa.laps = [_normalize_fit_lap(l) for l in laps]
    return pa


def _normalize_fit_lap(lap: Dict[str, Any]) -> Dict[str, Any]:
    return _drop_none_dict({
        "duration_s": int(lap["total_timer_time"]) if lap.get("total_timer_time") else (
            int(lap["total_elapsed_time"]) if lap.get("total_elapsed_time") else None),
        "distance_m": _to_float(lap.get("total_distance")),
        "avg_hr": _to_float(lap.get("avg_heart_rate")),
        "max_hr": _to_float(lap.get("max_heart_rate")),
        "avg_speed_mps": _to_float(lap.get("avg_speed")),
        "calories": _to_float(lap.get("total_calories")),
    })


def _drop_none_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# GPX
# ─────────────────────────────────────────────────────────────────────────────

def parse_gpx(data: bytes) -> ParsedActivity:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ParseError(f"GPX XML parse failed: {e}") from e

    pa = ParsedActivity(file_type=FileType.GPX, parser="gpx_parser")
    creator = root.attrib.get("creator")
    pa.device = creator
    pa.source_hint = _source_from_text(creator)
    pa.original_name = _text_local(root, "name")
    pa.sport_hint = _text_local(root, "type")

    pts = list(_iter_local(root, "trkpt")) or list(_iter_local(root, "rtept"))
    if not pts:
        raise ParseError("GPX has no track points")

    times: List[datetime] = []
    hrs: List[float] = []
    cads: List[float] = []
    eles: List[float] = []
    coords: List[Tuple[float, float]] = []

    for pt in pts:
        lat = _to_float(pt.attrib.get("lat"))
        lon = _to_float(pt.attrib.get("lon"))
        if lat is not None and lon is not None:
            coords.append((lat, lon))
        t = parse_dt(_text_local(pt, "time"))
        if t:
            times.append(t)
        ele = _to_float(_text_local(pt, "ele"))
        if ele is not None:
            eles.append(ele)
        hr = _to_float(_text_local(pt, "hr"))
        if hr is not None:
            hrs.append(hr)
        cad = _to_float(_text_local(pt, "cad"))
        if cad is not None:
            cads.append(cad)

    pa.n_records = len(pts)
    pa.has_gps = len(coords) > 0
    pa.has_elevation = len(eles) > 0
    pa.has_hr = len(hrs) > 0
    pa.has_cadence = len(cads) > 0

    if times:
        pa.start_time = times[0]
        span = int((times[-1] - times[0]).total_seconds())
        pa.elapsed_time_s = span if span > 0 else None
        pa.duration_s = pa.elapsed_time_s

    # Distance derived by haversine over GPS track.
    if len(coords) >= 2:
        dist = sum(_haversine_m(coords[i - 1], coords[i]) for i in range(1, len(coords)))
        pa.distance_m = round(dist, 1)
        pa.has_distance = True
        if pa.duration_s:
            pa.avg_speed_mps = round(dist / pa.duration_s, 3)

    # Elevation gain derived from positive deltas.
    if len(eles) >= 2:
        gain = sum(max(0.0, eles[i] - eles[i - 1]) for i in range(1, len(eles)))
        loss = sum(max(0.0, eles[i - 1] - eles[i]) for i in range(1, len(eles)))
        pa.elevation_gain_m = round(gain, 1)
        pa.elevation_loss_m = round(loss, 1)

    pa.avg_hr = _avg(hrs)
    pa.max_hr = max(hrs) if hrs else None
    pa.avg_cadence = _avg(cads)
    pa.warnings.append("GPX: distance/speed/elevation are derived from track points")
    return pa


# ─────────────────────────────────────────────────────────────────────────────
# TCX
# ─────────────────────────────────────────────────────────────────────────────

def parse_tcx(data: bytes) -> ParsedActivity:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ParseError(f"TCX XML parse failed: {e}") from e

    activity = _first_local(root, "Activity")
    if activity is None:
        raise ParseError("TCX has no Activity")

    pa = ParsedActivity(file_type=FileType.TCX, parser="tcx_parser")
    pa.sport_hint = activity.attrib.get("Sport")

    creator_el = _first_local(activity, "Creator")
    author_el = _first_local(root, "Author")
    creator_name = _text_local(creator_el, "Name") if creator_el is not None else None
    author_name = _text_local(author_el, "Name") if author_el is not None else None
    pa.device = creator_name or author_name
    pa.source_hint = _source_from_text(creator_name or author_name)

    laps = list(_iter_local(activity, "Lap"))
    total_time = 0.0
    total_dist = 0.0
    total_cal = 0.0
    max_speed = None
    lap_dicts: List[Dict[str, Any]] = []
    for lap in laps:
        t = _to_float(_text_local(lap, "TotalTimeSeconds")) or 0.0
        d = _to_float(_text_local(lap, "DistanceMeters")) or 0.0
        cal = _to_float(_text_local(lap, "Calories")) or 0.0
        ms = _to_float(_text_local(lap, "MaximumSpeed"))
        avg_hr_el = _first_local(lap, "AverageHeartRateBpm")
        max_hr_el = _first_local(lap, "MaximumHeartRateBpm")
        lap_avg_hr = _to_float(_text_local(avg_hr_el, "Value")) if avg_hr_el is not None else None
        lap_max_hr = _to_float(_text_local(max_hr_el, "Value")) if max_hr_el is not None else None
        total_time += t
        total_dist += d
        total_cal += cal
        if ms is not None:
            max_speed = ms if max_speed is None else max(max_speed, ms)
        lap_dicts.append(_drop_none_dict({
            "duration_s": int(t) if t else None,
            "distance_m": d or None,
            "avg_hr": lap_avg_hr,
            "max_hr": lap_max_hr,
            "calories": cal or None,
        }))

    # Trackpoints (truth for series availability + start time).
    tps = list(_iter_local(activity, "Trackpoint"))
    times: List[datetime] = []
    hrs: List[float] = []
    cads: List[float] = []
    watts: List[float] = []
    has_gps = False
    has_ele = False
    for tp in tps:
        t = parse_dt(_text_local(tp, "Time"))
        if t:
            times.append(t)
        if _first_local(tp, "Position") is not None:
            has_gps = True
        if _text_local(tp, "AltitudeMeters") is not None:
            has_ele = True
        hr_el = _first_local(tp, "HeartRateBpm")
        hr = _to_float(_text_local(hr_el, "Value")) if hr_el is not None else None
        if hr is not None:
            hrs.append(hr)
        cad = _to_float(_text_local(tp, "Cadence"))
        if cad is not None:
            cads.append(cad)
        w = _to_float(_text_local(tp, "Watts"))
        if w is not None:
            watts.append(w)

    pa.n_records = len(tps)
    pa.laps = lap_dicts
    pa.duration_s = int(total_time) if total_time else None
    pa.moving_time_s = pa.duration_s
    pa.distance_m = round(total_dist, 1) if total_dist else None
    pa.calories = round(total_cal, 1) if total_cal else None
    pa.max_speed_mps = max_speed
    if pa.distance_m and pa.duration_s:
        pa.avg_speed_mps = round(pa.distance_m / pa.duration_s, 3)

    if times:
        pa.start_time = times[0]
        if not pa.duration_s:
            span = int((times[-1] - times[0]).total_seconds())
            pa.duration_s = span if span > 0 else None

    pa.has_distance = pa.distance_m is not None
    pa.has_gps = has_gps
    pa.has_elevation = has_ele
    pa.has_hr = len(hrs) > 0
    pa.has_cadence = len(cads) > 0
    pa.has_power = len(watts) > 0
    pa.avg_hr = _avg(hrs)
    pa.max_hr = max(hrs) if hrs else None
    pa.avg_cadence = _avg(cads)
    pa.avg_power = _avg(watts)
    pa.max_power = max(watts) if watts else None
    return pa


# ─────────────────────────────────────────────────────────────────────────────
# CSV  (supports a summary CSV or a per-record CSV like decode_fit's output)
# ─────────────────────────────────────────────────────────────────────────────

# Column aliases (lowercased). Distance/duration units are resolved from the header.
_CSV_ALIASES = {
    "start_time": ["start_time", "starttime", "timestamp", "date", "start"],
    "sport": ["sport", "activity_type", "type", "activity"],
    "name": ["name", "title", "activity_name"],
    "distance_m": ["distance_m", "distance_meters", "distance(m)"],
    "distance_km": ["distance_km", "distance(km)", "distance"],
    "duration": ["duration", "elapsed_time", "total_time", "moving_time", "time", "duration_s"],
    "avg_hr": ["avg_hr", "average_hr", "avg_heart_rate", "heart_rate_avg"],
    "max_hr": ["max_hr", "max_heart_rate"],
    "avg_power": ["avg_power", "average_power", "avg_watts"],
    "max_power": ["max_power", "max_watts"],
    "avg_cadence": ["avg_cadence", "average_cadence"],
    "elevation_gain_m": ["elevation_gain_m", "ascent", "total_ascent", "elevation_gain"],
    "calories": ["calories", "kcal"],
}

# Per-record column names (decode_fit.py output shape).
_CSV_RECORD_COLS = {"heart_rate_bpm", "speed_kmh", "cadence_rpm", "altitude_m", "distance_m", "lat", "lon"}


def parse_csv(data: bytes) -> ParsedActivity:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    reader = _csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ParseError("CSV is empty")
    header = [h.strip().lower() for h in rows[0]]
    data_rows = rows[1:]
    if not data_rows:
        raise ParseError("CSV has a header but no data rows")

    if _CSV_RECORD_COLS.intersection(header):
        return _parse_csv_records(header, data_rows)
    return _parse_csv_summary(header, data_rows)


def _col(header: List[str], key: str) -> Optional[int]:
    for alias in _CSV_ALIASES.get(key, [key]):
        if alias in header:
            return header.index(alias)
    return None


def _parse_csv_summary(header: List[str], rows: List[List[str]]) -> ParsedActivity:
    row = rows[0]  # P0: one activity per summary CSV (first data row)

    def g(key: str) -> Optional[str]:
        i = _col(header, key)
        return row[i].strip() if (i is not None and i < len(row) and row[i].strip() != "") else None

    pa = ParsedActivity(file_type=FileType.CSV, parser="csv_parser")
    pa.start_time = parse_dt(g("start_time"))
    pa.sport_hint = g("sport")
    pa.original_name = g("name")
    pa.source_hint = Source.UNKNOWN  # generic CSV: source not identifiable

    dist_m = _to_float(g("distance_m"))
    dist_km = _to_float(g("distance_km"))
    if dist_m is not None:
        pa.distance_m = dist_m
    elif dist_km is not None:
        pa.distance_m = dist_km * 1000.0
    pa.has_distance = pa.distance_m is not None

    pa.duration_s = _parse_duration_s(g("duration"))
    pa.moving_time_s = pa.duration_s
    pa.avg_hr = _to_float(g("avg_hr"))
    pa.max_hr = _to_float(g("max_hr"))
    pa.avg_power = _to_float(g("avg_power"))
    pa.max_power = _to_float(g("max_power"))
    pa.avg_cadence = _to_float(g("avg_cadence"))
    pa.elevation_gain_m = _to_float(g("elevation_gain_m"))
    pa.calories = _to_float(g("calories"))

    pa.has_hr = pa.avg_hr is not None
    pa.has_power = pa.avg_power is not None or pa.max_power is not None
    pa.has_cadence = pa.avg_cadence is not None
    pa.has_elevation = pa.elevation_gain_m is not None
    if pa.distance_m and pa.duration_s:
        pa.avg_speed_mps = round(pa.distance_m / pa.duration_s, 3)
    pa.warnings.append("CSV summary: stored as provided; units inferred from column names")
    return pa


def _parse_csv_records(header: List[str], rows: List[List[str]]) -> ParsedActivity:
    idx = {name: header.index(name) for name in _CSV_RECORD_COLS if name in header}
    ts_i = header.index("timestamp") if "timestamp" in header else None

    def cell(r: List[str], name: str) -> Optional[str]:
        i = idx.get(name)
        return r[i].strip() if (i is not None and i < len(r) and r[i].strip() != "") else None

    hrs: List[float] = []
    cads: List[float] = []
    speeds_kmh: List[float] = []
    eles: List[float] = []
    dists: List[float] = []
    times: List[datetime] = []
    has_gps = False
    for r in rows:
        hr = _to_float(cell(r, "heart_rate_bpm"))
        if hr is not None:
            hrs.append(hr)
        cad = _to_float(cell(r, "cadence_rpm"))
        if cad is not None:
            cads.append(cad)
        spd = _to_float(cell(r, "speed_kmh"))
        if spd is not None:
            speeds_kmh.append(spd)
        ele = _to_float(cell(r, "altitude_m"))
        if ele is not None:
            eles.append(ele)
        d = _to_float(cell(r, "distance_m"))
        if d is not None:
            dists.append(d)
        if cell(r, "lat") and cell(r, "lon"):
            has_gps = True
        if ts_i is not None and ts_i < len(r):
            t = parse_dt(r[ts_i])
            if t:
                times.append(t)

    pa = ParsedActivity(file_type=FileType.CSV, parser="csv_parser")
    pa.n_records = len(rows)
    pa.has_hr = len(hrs) > 0
    pa.has_cadence = len(cads) > 0
    pa.has_elevation = len(eles) > 0
    pa.has_gps = has_gps
    pa.avg_hr = _avg(hrs)
    pa.max_hr = max(hrs) if hrs else None
    pa.avg_cadence = _avg(cads)
    if speeds_kmh:
        pa.avg_speed_mps = round((sum(speeds_kmh) / len(speeds_kmh)) / 3.6, 3)
        pa.max_speed_mps = round(max(speeds_kmh) / 3.6, 3)
    if dists:
        pa.distance_m = round(max(dists), 1)
        pa.has_distance = True
    if len(eles) >= 2:
        pa.elevation_gain_m = round(sum(max(0.0, eles[i] - eles[i - 1]) for i in range(1, len(eles))), 1)
    if times:
        pa.start_time = times[0]
        span = int((times[-1] - times[0]).total_seconds())
        pa.duration_s = span if span > 0 else None
        pa.moving_time_s = pa.duration_s
    elif pa.n_records:
        pa.duration_s = pa.n_records  # 1 Hz assumption
        pa.moving_time_s = pa.duration_s
        pa.warnings.append("CSV records: no timestamps; duration assumed 1 Hz")
    pa.warnings.append("CSV records: summary aggregated from per-second rows")
    return pa


# ─────────────────────────────────────────────────────────────────────────────
# ZIP + dispatch
# ─────────────────────────────────────────────────────────────────────────────

def _fit_from_zip(data: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise ParseError("ZIP contains no .fit file")
            return zf.read(fits[0])
    except zipfile.BadZipFile as e:
        raise ParseError(f"Bad ZIP file: {e}") from e


def parse(data: bytes, filename: Optional[str] = None) -> ParsedActivity:
    """Detect the file type and dispatch to the right parser.

    Raises ParseError on unsupported or unparseable input (the pipeline turns that
    into a safe failure).
    """
    if not data:
        raise ParseError("Empty file")
    ftype = detect_file_type(filename, data)
    if ftype == FileType.ZIP:
        inner = _fit_from_zip(data)
        pa = parse_fit(inner)
        pa.warnings.append("Extracted FIT from ZIP upload")
        return pa
    if ftype == FileType.FIT:
        return parse_fit(data)
    if ftype == FileType.GPX:
        return parse_gpx(data)
    if ftype == FileType.TCX:
        return parse_tcx(data)
    if ftype == FileType.CSV:
        return parse_csv(data)
    raise ParseError(f"Unsupported or unrecognized file type (filename={filename!r})")


__all__ = [
    "INGEST_PARSER_VERSION", "ParseError", "ParsedActivity",
    "detect_file_type", "parse", "parse_fit", "parse_gpx", "parse_tcx", "parse_csv",
]
