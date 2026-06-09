#!/usr/bin/env python3
"""Normalize Garmin account export JSON into staging files for Mars.

This does not write to the application database. It creates clean JSON files
that can be inspected before any import or cleanup is performed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import zipfile
from pathlib import Path
from typing import Any


def _read_json(zf: zipfile.ZipFile, name: str) -> Any:
    return json.loads(zf.read(name).decode("utf-8-sig"))


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(round(number)) if number is not None else None


def _ms_to_iso(value: Any) -> str | None:
    number = _as_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number = number / 1000
    return dt.datetime.fromtimestamp(number, tz=dt.timezone.utc).isoformat()


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _duration_s(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(round(number / 1000))


def _distance_km(value: Any) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    return round(number / 100_000, 3)


def _elevation_m(value: Any) -> int | None:
    number = _as_float(value)
    if number is None:
        return None
    return int(round(number / 100))


def _speed_kmh(value: Any) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    return round(number * 36, 2)


def _calories(value: Any) -> int | None:
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    # Garmin exports are not fully consistent across account export files:
    # some rows are kcal already, older/summarized rows can be tenths of kcal.
    # Only scale down values that are clearly too large for kcal.
    if number > 5000:
        number = number / 10
    return int(round(number))


def _activity_rows(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in zf.namelist():
        if "summarizedActivities" not in name or not name.endswith(".json"):
            continue
        data = _read_json(zf, name)
        if not isinstance(data, list):
            continue
        for wrapper in data:
            if not isinstance(wrapper, dict):
                continue
            items = wrapper.get("summarizedActivitiesExport")
            if isinstance(items, list):
                rows.extend(item for item in items if isinstance(item, dict))
    return rows


def _normalize_activity(row: dict[str, Any]) -> dict[str, Any]:
    avg_speed_kmh = _speed_kmh(row.get("avgSpeed"))
    avg_hr = _as_float(row.get("avgHr"))
    distance_km = _distance_km(row.get("distance"))
    duration_s = _duration_s(row.get("duration"))
    moving_duration_s = _duration_s(row.get("movingDuration"))

    activity = {
        "source": "garmin_export",
        "source_activity_id": str(row.get("activityId")),
        "name": row.get("name"),
        "sport": row.get("activityType"),
        "sport_type": row.get("sportType"),
        "start_time_utc": _ms_to_iso(row.get("startTimeGmt")),
        "start_time_local": _ms_to_iso(row.get("startTimeLocal")),
        "duration_s": duration_s,
        "moving_duration_s": moving_duration_s,
        "elapsed_duration_s": _duration_s(row.get("elapsedDuration")),
        "distance_km": distance_km,
        "ascent_m": _elevation_m(row.get("elevationGain")),
        "descent_m": _elevation_m(row.get("elevationLoss")),
        "min_elevation_m": _elevation_m(row.get("minElevation")),
        "max_elevation_m": _elevation_m(row.get("maxElevation")),
        "avg_speed_kmh": avg_speed_kmh,
        "max_speed_kmh": _speed_kmh(row.get("maxSpeed")),
        "avg_hr_bpm": _round(avg_hr, 1) if avg_hr is not None else None,
        "max_hr_bpm": _as_int(row.get("maxHr")),
        "avg_cadence": _round(_as_float(row.get("avgBikeCadence") or row.get("avgRunCadence") or row.get("avgCadence")), 1),
        "max_cadence": _as_int(row.get("maxBikeCadence") or row.get("maxRunCadence") or row.get("maxCadence")),
        "calories": _calories(row.get("calories")),
        "temperature_min_c": _round(_as_float(row.get("minTemperature")), 1),
        "temperature_max_c": _round(_as_float(row.get("maxTemperature")), 1),
        "aerobic_training_effect": _round(_as_float(row.get("aerobicTrainingEffect")), 1),
        "anaerobic_training_effect": _round(_as_float(row.get("anaerobicTrainingEffect")), 1),
        "start_lat": _as_float(row.get("startLatitude")),
        "start_lon": _as_float(row.get("startLongitude")),
        "end_lat": _as_float(row.get("endLatitude")),
        "end_lon": _as_float(row.get("endLongitude")),
        "device_id": str(row.get("deviceId")) if row.get("deviceId") is not None else None,
        "lap_count": _as_int(row.get("lapCount")),
        "favorite": bool(row.get("favorite")) if row.get("favorite") is not None else None,
        "pr": bool(row.get("pr")) if row.get("pr") is not None else None,
        "power_available": any(row.get(k) not in (None, "", 0) for k in ("avgPower", "maxPower", "normalizedPower")),
        "avg_power_w": _as_int(row.get("avgPower")),
        "max_power_w": _as_int(row.get("maxPower")),
        "normalized_power_w": _as_int(row.get("normalizedPower")),
        "raw": row,
    }
    if avg_speed_kmh and avg_hr:
        activity["efficiency_speed_hr"] = round(avg_speed_kmh / avg_hr, 5)
    else:
        activity["efficiency_speed_hr"] = None
    if distance_km and duration_s:
        activity["computed_avg_speed_kmh"] = round(distance_km / (duration_s / 3600), 2)
    else:
        activity["computed_avg_speed_kmh"] = None
    activity["is_probable_real_activity"] = bool(
        activity["source_activity_id"]
        and activity["sport"]
        and duration_s
        and duration_s >= 60
    )
    return activity


def _normalize_gear(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    name = next((n for n in zf.namelist() if n.endswith("_gear.json")), None)
    if not name:
        return []
    data = _read_json(zf, name)
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("gearDTOS"), list):
                rows.extend(row for row in item["gearDTOS"] if isinstance(row, dict))

    gear = []
    for row in rows:
        max_meters = _as_float(row.get("maximumMeters"))
        gear.append({
            "source": "garmin_export",
            "source_gear_id": str(row.get("gearPk")),
            "uuid": row.get("uuid"),
            "name": row.get("displayName"),
            "type": row.get("gearTypeName"),
            "status": row.get("gearStatusName"),
            "model": row.get("customMakeModel"),
            "date_begin": row.get("dateBegin"),
            "max_distance_km": round(max_meters / 1000, 1) if max_meters else None,
            "raw": row,
        })
    return gear


def _normalize_sleep(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in zf.namelist():
        if not name.endswith("_sleepData.json"):
            continue
        data = _read_json(zf, name)
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = []
            for value in data.values():
                if isinstance(value, list):
                    candidates = value
                    break
        else:
            candidates = []
        for row in candidates:
            if not isinstance(row, dict):
                continue
            rows.append({
                "source": "garmin_export",
                "calendar_date": row.get("calendarDate"),
                "sleep_start_gmt": row.get("sleepStartTimestampGMT"),
                "sleep_end_gmt": row.get("sleepEndTimestampGMT"),
                "sleep_start_local": row.get("sleepStartTimestampLocal"),
                "sleep_end_local": row.get("sleepEndTimestampLocal"),
                "duration_s": _as_int(row.get("durationInSeconds")),
                "deep_sleep_s": _as_int(row.get("deepSleepSeconds")),
                "light_sleep_s": _as_int(row.get("lightSleepSeconds")),
                "rem_sleep_s": _as_int(row.get("remSleepSeconds")),
                "awake_s": _as_int(row.get("awakeSleepSeconds") or row.get("awakeDurationInSeconds")),
                "sleep_score": _as_int(row.get("sleepScores", {}).get("overall", {}).get("value")) if isinstance(row.get("sleepScores"), dict) else None,
                "confidence": "low",
                "notes": "Garmin sleep is low confidence for Mars because the watch is not worn consistently overnight.",
                "raw": row,
            })
    rows.sort(key=lambda x: str(x.get("calendar_date") or ""))
    return rows


def normalize_export(zip_path: str) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        activities = [_normalize_activity(row) for row in _activity_rows(zf)]
        activities.sort(key=lambda x: str(x.get("start_time_local") or ""))
        gear = _normalize_gear(zf)
        sleep = _normalize_sleep(zf)
    return {"activities": activities, "gear": gear, "sleep": sleep}


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize Garmin export into staging JSON files.")
    parser.add_argument("zip_path", help="Path to Garmin export ZIP")
    parser.add_argument("--out-dir", default="reports/staging", help="Output directory")
    args = parser.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = normalize_export(args.zip_path)

    (out / "garmin_activities_clean.json").write_text(
        json.dumps(data["activities"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "garmin_gear_clean.json").write_text(
        json.dumps(data["gear"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "garmin_sleep_clean.json").write_text(
        json.dumps(data["sleep"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Activities staged: {len(data['activities'])}")
    print(f"Gear staged: {len(data['gear'])}")
    print(f"Sleep staged: {len(data['sleep'])}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
