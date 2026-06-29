#!/usr/bin/env python3
"""Build per-session altitude and location context without changing source data."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import _ensure_session_environment_table, get_db
from tools.recalculate_mars_zones import _load_telemetry, _match_telemetry


ENVIRONMENT_VERSION = "session_environment_v1"


def altitude_band(altitude_m: float | None) -> str:
    if altitude_m is None:
        return "unknown"
    if altitude_m < 800:
        return "low"
    if altitude_m < 1800:
        return "moderate"
    if altitude_m < 2500:
        return "high"
    return "very_high"


def relative_altitude_band(delta_m: float | None) -> str:
    if delta_m is None:
        return "unknown"
    if delta_m <= -600:
        return "well_below_habitual"
    if delta_m < -250:
        return "below_habitual"
    if delta_m <= 250:
        return "habitual"
    if delta_m < 600:
        return "above_habitual"
    return "well_above_habitual"


def infer_country(lat: float | None, lon: float | None) -> tuple[str | None, str | None, float]:
    if lat is None or lon is None:
        return None, None, 0.0
    # Conservative bounding boxes. They provide broad context, not reverse geocoding.
    specific_boxes = [
        ("IE", "Ireland", 51.3, 55.5, -10.8, -5.3),
    ]
    for code, label, min_lat, max_lat, min_lon, max_lon in specific_boxes:
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return code, label, 0.6
    boxes = [
        ("MX", "Mexico", 14.3, 32.8, -118.5, -86.4),
        ("GB", "United Kingdom", 49.8, 60.9, -8.7, 1.8),
        ("US", "United States", 24.3, 49.5, -125.0, -66.5),
        ("ES", "Spain", 35.7, 43.9, -9.5, 3.4),
        ("FR", "France", 41.2, 51.2, -5.5, 9.7),
        ("IT", "Italy", 35.4, 47.2, 6.6, 18.6),
    ]
    matches = [
        (code, label)
        for code, label, min_lat, max_lat, min_lon, max_lon in boxes
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
    ]
    if len(matches) == 1:
        return matches[0][0], matches[0][1], 0.6
    return None, None, 0.0


def location_cluster(lat: float | None, lon: float | None) -> str | None:
    if lat is None or lon is None:
        return None
    return f"{round(lat, 2):.2f},{round(lon, 2):.2f}"


def telemetry_altitude(samples: list[dict[str, float]]) -> dict[str, float] | None:
    valid = [
        sample for sample in samples
        if sample.get("altitude") is not None
        and -500 <= float(sample["altitude"]) <= 9000
    ]
    if not valid:
        return None
    weighted = []
    for index, sample in enumerate(valid):
        seconds = int(sample["t"])
        if index + 1 < len(valid):
            step = int(valid[index + 1]["t"]) - seconds
            step = step if 0 < step <= 10 else 1
        else:
            step = 1
        weighted.append((float(sample["altitude"]), step))
    total_seconds = sum(step for _, step in weighted)
    return {
        "start": float(valid[0]["altitude"]),
        "avg": sum(value * step for value, step in weighted) / total_seconds,
        "min": min(float(sample["altitude"]) for sample in valid),
        "max": max(float(sample["altitude"]) for sample in valid),
    }


def _load_altitude_telemetry(conn) -> dict[str, list[dict[str, float]]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT session_id, t, altitude
            FROM session_records
            WHERE altitude IS NOT NULL
            ORDER BY session_id, t
        """)
        result: dict[str, list[dict[str, float]]] = defaultdict(list)
        for session_id, seconds, altitude in cur.fetchall():
            result[str(session_id)].append({
                "t": int(seconds),
                "altitude": float(altitude),
            })
    return result


def _load_rows(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                cs.clean_session_id, cs.original_session_id, cs.source_activity_id,
                cs.start_time, cs.start_date, cs.distance_km, cs.duration_s,
                cs.ascent_m, cs.start_lat, cs.start_lon, cs.end_lat, cs.end_lon,
                ga.min_elevation_m, ga.max_elevation_m,
                ga.start_lat AS garmin_start_lat, ga.start_lon AS garmin_start_lon,
                ga.end_lat AS garmin_end_lat, ga.end_lon AS garmin_end_lon
            FROM clean_sessions cs
            LEFT JOIN garmin_export_activities ga
              ON ga.source_activity_id = cs.source_activity_id
            ORDER BY cs.start_time, cs.clean_session_id
        """)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _matched_altitude(
    row: dict[str, Any],
    telemetry_index: dict[str, list[dict[str, float]]],
    matched_source_id: str | None,
) -> dict[str, float] | None:
    candidate_ids = [
        str(row.get("clean_session_id") or ""),
        str(row.get("clean_session_id") or "").replace("session:", ""),
        str(row.get("original_session_id") or ""),
        str(row.get("original_session_id") or "").replace("session:", ""),
        str(matched_source_id or ""),
    ]
    for candidate in candidate_ids:
        if candidate and candidate in telemetry_index:
            return telemetry_altitude(telemetry_index[candidate])
    return None


def _habitual_altitude(environments: list[dict[str, Any]]) -> float | None:
    # Location mode prevents travel sessions from redefining the athlete's home altitude.
    clusters = defaultdict(list)
    for row in environments:
        if row.get("location_cluster") and row.get("start_altitude_m") is not None:
            clusters[row["location_cluster"]].append(row)
    if not clusters:
        values = [
            row["start_altitude_m"] for row in environments
            if row.get("start_altitude_m") is not None
        ]
        return median(values) if values else None
    dominant = max(clusters.values(), key=len)
    return median(row["start_altitude_m"] for row in dominant)


def _add_exposure_context(rows: list[dict[str, Any]], habitual: float | None) -> None:
    for index, row in enumerate(rows):
        avg_altitude = row.get("avg_altitude_m")
        delta = avg_altitude - habitual if avg_altitude is not None and habitual is not None else None
        row["habitual_altitude_m"] = habitual
        row["altitude_delta_m"] = delta
        row["altitude_band"] = altitude_band(avg_altitude)
        row["relative_altitude_band"] = relative_altitude_band(delta)
        if avg_altitude is None or not row.get("start_time"):
            row["prior_21d_exposure_days"] = 0
            row["acclimatization_status"] = "unknown"
            continue
        start = row["start_time"] - timedelta(days=21)
        comparable_days = {
            previous["start_date"]
            for previous in rows[:index]
            if previous.get("start_time")
            and previous["start_time"] >= start
            and previous.get("avg_altitude_m") is not None
            and abs(previous["avg_altitude_m"] - avg_altitude) <= 300
        }
        exposure_days = len(comparable_days)
        row["prior_21d_exposure_days"] = exposure_days
        row["acclimatization_status"] = (
            "established" if exposure_days >= 8
            else "developing" if exposure_days >= 3
            else "limited"
        )


def build_environment_rows(conn) -> list[dict[str, Any]]:
    rows = _load_rows(conn)
    hr_by_id, hr_by_day = _load_telemetry(conn)
    altitude_index = _load_altitude_telemetry(conn)
    used_ids: set[str] = set()
    result = []
    for row in rows:
        _, match, match_info = _match_telemetry(row, hr_by_id, hr_by_day, used_ids)
        source_id = match_info.get("old_session_id") if match else None
        alt = _matched_altitude(row, altitude_index, source_id)
        min_summary = float(row["min_elevation_m"]) if row.get("min_elevation_m") is not None else None
        max_summary = float(row["max_elevation_m"]) if row.get("max_elevation_m") is not None else None
        if alt:
            altitude_source = "session_records"
            altitude_confidence = 1.0 if match == "exact" else 0.9 if match == "linked_high" else 0.75
        elif min_summary is not None or max_summary is not None:
            alt = {
                "start": min_summary,
                "avg": (
                    (min_summary + max_summary) / 2
                    if min_summary is not None and max_summary is not None
                    else min_summary if min_summary is not None else max_summary
                ),
                "min": min_summary,
                "max": max_summary,
            }
            altitude_source = "garmin_activity_summary"
            altitude_confidence = 0.55
        else:
            alt = None
            altitude_source = "unavailable"
            altitude_confidence = 0.0
        start_lat = float(row.get("start_lat") or row.get("garmin_start_lat") or 0) or None
        start_lon = float(row.get("start_lon") or row.get("garmin_start_lon") or 0) or None
        end_lat = float(row.get("end_lat") or row.get("garmin_end_lat") or 0) or None
        end_lon = float(row.get("end_lon") or row.get("garmin_end_lon") or 0) or None
        country_code, region_label, location_confidence = infer_country(start_lat, start_lon)
        result.append({
            "clean_session_id": row["clean_session_id"],
            "start_time": row.get("start_time"),
            "start_date": row.get("start_date"),
            "start_altitude_m": alt.get("start") if alt else None,
            "avg_altitude_m": alt.get("avg") if alt else None,
            "min_altitude_m": alt.get("min") if alt else None,
            "max_altitude_m": alt.get("max") if alt else None,
            "altitude_range_m": (
                alt["max"] - alt["min"]
                if alt and alt.get("max") is not None and alt.get("min") is not None
                else None
            ),
            "ascent_m": int(row.get("ascent_m") or 0),
            "start_lat": start_lat,
            "start_lon": start_lon,
            "end_lat": end_lat,
            "end_lon": end_lon,
            "country_code": country_code,
            "region_label": region_label,
            "location_cluster": location_cluster(start_lat, start_lon),
            "altitude_source": altitude_source,
            "location_source": "gps_bounding_box" if country_code else "gps_only" if start_lat else "unavailable",
            "altitude_confidence": altitude_confidence,
            "location_confidence": location_confidence if country_code else 0.4 if start_lat else 0.0,
        })
    habitual = _habitual_altitude(result)
    _add_exposure_context(result, habitual)
    return result


def write_environment_rows(conn, rows: list[dict[str, Any]]) -> None:
    query = """
        INSERT INTO session_environment (
            clean_session_id, start_altitude_m, avg_altitude_m,
            min_altitude_m, max_altitude_m, altitude_range_m, ascent_m,
            start_lat, start_lon, end_lat, end_lon, country_code, region_label,
            location_cluster, habitual_altitude_m, altitude_delta_m,
            altitude_band, relative_altitude_band, prior_21d_exposure_days,
            acclimatization_status, altitude_source, location_source,
            altitude_confidence, location_confidence, environment_version,
            calculated_at
        ) VALUES %s
        ON CONFLICT (clean_session_id) DO UPDATE SET
            start_altitude_m = EXCLUDED.start_altitude_m,
            avg_altitude_m = EXCLUDED.avg_altitude_m,
            min_altitude_m = EXCLUDED.min_altitude_m,
            max_altitude_m = EXCLUDED.max_altitude_m,
            altitude_range_m = EXCLUDED.altitude_range_m,
            ascent_m = EXCLUDED.ascent_m,
            start_lat = EXCLUDED.start_lat,
            start_lon = EXCLUDED.start_lon,
            end_lat = EXCLUDED.end_lat,
            end_lon = EXCLUDED.end_lon,
            country_code = EXCLUDED.country_code,
            region_label = EXCLUDED.region_label,
            location_cluster = EXCLUDED.location_cluster,
            habitual_altitude_m = EXCLUDED.habitual_altitude_m,
            altitude_delta_m = EXCLUDED.altitude_delta_m,
            altitude_band = EXCLUDED.altitude_band,
            relative_altitude_band = EXCLUDED.relative_altitude_band,
            prior_21d_exposure_days = EXCLUDED.prior_21d_exposure_days,
            acclimatization_status = EXCLUDED.acclimatization_status,
            altitude_source = EXCLUDED.altitude_source,
            location_source = EXCLUDED.location_source,
            altitude_confidence = EXCLUDED.altitude_confidence,
            location_confidence = EXCLUDED.location_confidence,
            environment_version = EXCLUDED.environment_version,
            calculated_at = EXCLUDED.calculated_at
    """
    now = datetime.now(timezone.utc)
    values = [
        (
            row["clean_session_id"], row["start_altitude_m"], row["avg_altitude_m"],
            row["min_altitude_m"], row["max_altitude_m"], row["altitude_range_m"],
            row["ascent_m"], row["start_lat"], row["start_lon"], row["end_lat"],
            row["end_lon"], row["country_code"], row["region_label"],
            row["location_cluster"], row["habitual_altitude_m"],
            row["altitude_delta_m"], row["altitude_band"],
            row["relative_altitude_band"], row["prior_21d_exposure_days"],
            row["acclimatization_status"], row["altitude_source"],
            row["location_source"], row["altitude_confidence"],
            row["location_confidence"], ENVIRONMENT_VERSION, now,
        )
        for row in rows
    ]
    with conn.cursor() as cur:
        execute_values(cur, query, values, page_size=100)


def summarize(rows: list[dict[str, Any]], execute: bool) -> dict[str, Any]:
    coverage = defaultdict(int)
    countries = defaultdict(int)
    bands = defaultdict(int)
    for row in rows:
        coverage[row["altitude_source"]] += 1
        bands[row["altitude_band"]] += 1
        if row["country_code"]:
            countries[row["country_code"]] += 1
    with_altitude = sum(count for source, count in coverage.items() if source != "unavailable")
    habitual = next((row["habitual_altitude_m"] for row in rows if row["habitual_altitude_m"] is not None), None)
    return {
        "ok": True,
        "dry_run": not execute,
        "sessions_scanned": len(rows),
        "sessions_with_altitude": with_altitude,
        "altitude_coverage_pct": round(with_altitude / len(rows) * 100, 1) if rows else 0,
        "habitual_altitude_m": round(habitual, 1) if habitual is not None else None,
        "altitude_sources": dict(coverage),
        "altitude_bands": dict(bands),
        "countries_inferred": dict(countries),
        "location_rule": "Conservative bounding boxes; coordinates remain the source of truth.",
        "acclimatization_rule": "Training days at comparable altitude in the prior 21 days; not residence days.",
        "updated_sessions": len(rows) if execute else 0,
        "version": ENVIRONMENT_VERSION,
    }


def backfill_session_environment(conn, execute: bool = False) -> dict[str, Any]:
    _ensure_session_environment_table(conn)
    rows = build_environment_rows(conn)
    if execute:
        write_environment_rows(conn, rows)
    return summarize(rows, execute)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build session environment context.")
    parser.add_argument("--execute", action="store_true", help="Write environment rows.")
    args = parser.parse_args()
    conn = get_db()
    if not conn:
        raise RuntimeError("Database unavailable")
    print(json.dumps(
        backfill_session_environment(conn, execute=args.execute),
        indent=2,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
