#!/usr/bin/env python3
"""Versioned, non-destructive Mars zone recalculation for clean sessions."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import Json, execute_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import _ensure_zone_model_system, get_db
from mars_context import MARS_ZONES


MODEL_IDS = {
    "cycling": "zone_model_v2_mars_lt168",
    "running": "zone_model_v2_mars_run_lt173",
}

SPORT_GROUPS = {
    "cycling": {"cycling", "indoor_cycling"},
    "running": {"running", "trail_running", "treadmill_running"},
}


def sport_group(sport: str | None) -> str | None:
    for group, sports in SPORT_GROUPS.items():
        if sport in sports:
            return group
    return None


def extract_original_z2(raw_json: Any) -> float | None:
    if not isinstance(raw_json, dict):
        return None
    candidates = [
        raw_json.get("z2_pct_original"),
        raw_json.get("z2_pct"),
        raw_json.get("pct_z2"),
        raw_json.get("zone2_pct"),
    ]
    for candidate in candidates:
        try:
            if candidate is None or candidate == "":
                continue
            value = float(candidate)
            if 0 <= value <= 100:
                return round(value, 2)
        except (TypeError, ValueError):
            continue
    return None


def zone_result(
    sport: str,
    avg_hr: float | None,
    telemetry_samples: list[tuple[int, float]] | None = None,
    telemetry_match: str | None = None,
    duration_s: int | None = None,
) -> dict[str, Any]:
    group = sport_group(sport)
    if not group:
        return {
            "eligible": False,
            "z2_pct_mars": None,
            "zone_model_used": None,
            "zone_confidence": "not_applicable",
            "z2_confidence_score": 0.0,
        }
    z2_low, z2_high = MARS_ZONES[group]["z2"]
    valid_samples = [
        (int(seconds), float(hr))
        for seconds, hr in (telemetry_samples or [])
        if hr is not None and 35 <= float(hr) <= 230
    ]
    if valid_samples:
        weighted_seconds = 0
        z2_seconds = 0
        fallback_step = 1
        positive_steps = [
            next_seconds - seconds
            for (seconds, _), (next_seconds, _) in zip(valid_samples, valid_samples[1:])
            if 0 < next_seconds - seconds <= 10
        ]
        if positive_steps:
            fallback_step = max(1, round(sum(positive_steps) / len(positive_steps)))
        for index, (seconds, hr) in enumerate(valid_samples):
            if index + 1 < len(valid_samples):
                step = valid_samples[index + 1][0] - seconds
                step = step if 0 < step <= 10 else fallback_step
            else:
                remaining = int(duration_s or 0) - seconds
                step = remaining if 0 < remaining <= 10 else fallback_step
            weighted_seconds += step
            if z2_low <= hr <= z2_high:
                z2_seconds += step
        match = telemetry_match or "exact"
        confidence_score = {
            "exact": 1.0,
            "linked_high": 0.9,
            "linked_moderate": 0.75,
        }.get(match, 0.75)
        return {
            "eligible": True,
            "z2_pct_mars": round(z2_seconds / weighted_seconds * 100, 2),
            "zone_model_used": MODEL_IDS[group],
            "zone_confidence": f"telemetry_{match}",
            "z2_confidence_score": confidence_score,
        }
    if avg_hr is not None and 35 <= float(avg_hr) <= 230:
        return {
            "eligible": True,
            "z2_pct_mars": 100.0 if z2_low <= float(avg_hr) <= z2_high else 0.0,
            "zone_model_used": MODEL_IDS[group],
            "zone_confidence": "session_average_estimate",
            "z2_confidence_score": 0.4,
        }
    return {
        "eligible": True,
        "z2_pct_mars": None,
        "zone_model_used": MODEL_IDS[group],
        "zone_confidence": "no_heart_rate",
        "z2_confidence_score": 0.0,
    }


def ensure_zone_models(conn) -> None:
    _ensure_zone_model_system(conn)
    with conn.cursor() as cur:
        for group, model_id in MODEL_IDS.items():
            zones = MARS_ZONES[group]
            cur.execute(
                """
                INSERT INTO zone_models (
                    model_id, sport, lt_bpm, max_hr_bpm, method, zones_json,
                    effective_from, status, source, notes
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                ON CONFLICT (model_id) DO NOTHING
                """,
                (
                    model_id,
                    group,
                    zones["lt_bpm"],
                    zones["max_hr"],
                    "lt_based",
                    Json(zones),
                    date(2018, 1, 1),
                    "mars_context.py",
                    "Mars LT zones; append-only model used for historical recalculation.",
                ),
            )
    conn.commit()


def _load_telemetry(
    conn,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                s.session_id,
                s.start_time,
                s.distance_km,
                s.duration_s,
                ARRAY_AGG(sr.t ORDER BY sr.t) FILTER (WHERE sr.hr IS NOT NULL) AS sample_times,
                ARRAY_AGG(sr.hr ORDER BY sr.t) FILTER (WHERE sr.hr IS NOT NULL) AS hrs
            FROM sessions s
            JOIN session_records sr ON sr.session_id = s.session_id
            WHERE NULLIF(s.start_time, '') IS NOT NULL
            GROUP BY s.session_id, s.start_time, s.distance_km, s.duration_s
        """)
        for session_id, start_time, distance_km, duration_s, sample_times, hrs in cur.fetchall():
            samples = [
                (int(seconds), float(hr))
                for seconds, hr in zip(sample_times or [], hrs or [])
            ]
            try:
                dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            item = {
                "session_id": str(session_id),
                "dt": dt.replace(tzinfo=None),
                "distance_km": float(distance_km or 0),
                "duration_s": int(duration_s or 0),
                "samples": samples,
            }
            by_id[str(session_id)] = item
            by_day[dt.date().isoformat()].append(item)
    return by_id, by_day


def _match_telemetry(
    row: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_day: dict[str, list[dict[str, Any]]],
    used_session_ids: set[str],
) -> tuple[list[tuple[int, float]] | None, str | None, dict[str, Any]]:
    session_id = str(row["clean_session_id"])
    original_id = str(row.get("original_session_id") or "")
    candidates = [
        session_id,
        session_id.replace("session:", ""),
        original_id,
        original_id.replace("session:", ""),
    ]
    for candidate in candidates:
        if candidate and candidate in by_id and candidate not in used_session_ids:
            old = by_id[candidate]
            used_session_ids.add(old["session_id"])
            return old["samples"], "exact", {
                "old_session_id": old["session_id"],
                "reason": "exact_id",
            }

    start_time = row.get("start_time")
    if not start_time:
        return None, None, {"reason": "missing_start_time"}
    clean_dt = start_time.replace(tzinfo=None)
    possible_matches = []
    day_keys = [
        (clean_dt.date() + timedelta(days=offset)).isoformat()
        for offset in (-1, 0, 1)
    ]
    for day_key in day_keys:
        for old in by_day.get(day_key, []):
            if old["session_id"] in used_session_ids:
                continue
            seconds = abs((clean_dt - old["dt"]).total_seconds())
            offsets = (0, 18000, 21600, 25200)
            best_offset = min(offsets, key=lambda offset: abs(seconds - offset))
            time_delta = abs(seconds - best_offset)
            clean_distance = float(row.get("distance_km") or 0)
            distance_delta = abs(clean_distance - old["distance_km"])
            duration_delta = abs(int(row.get("duration_s") or 0) - old["duration_s"])
            distance_matches = (
                distance_delta <= 0.05
                if clean_distance > 0 and old["distance_km"] > 0
                else True
            )
            if time_delta > 120 or not distance_matches or duration_delta > 300:
                continue
            score = (
                time_delta / 120
                + (distance_delta / 0.05 if clean_distance > 0 and old["distance_km"] > 0 else 0.5)
                + duration_delta / 300
            )
            possible_matches.append((score, old, {
                "old_session_id": old["session_id"],
                "reason": "heuristic",
                "offset_hours": round(best_offset / 3600),
                "time_delta_s": round(time_delta, 2),
                "distance_delta_km": round(distance_delta, 4),
                "duration_delta_s": duration_delta,
                "score": round(score, 4),
            }))

    if not possible_matches:
        return None, None, {"reason": "no_match"}
    possible_matches.sort(key=lambda item: item[0])
    if len(possible_matches) > 1:
        best_score, second_score = possible_matches[0][0], possible_matches[1][0]
        if second_score - best_score < 0.35:
            return None, None, {"reason": "ambiguous"}
    _, old, metadata = possible_matches[0]
    used_session_ids.add(old["session_id"])
    linked_quality = (
        "linked_high"
        if metadata["time_delta_s"] <= 5
        and metadata["distance_delta_km"] <= 0.02
        and metadata["duration_delta_s"] <= 180
        else "linked_moderate"
    )
    return old["samples"], linked_quality, metadata


def recalculate_zones(conn, execute: bool = False) -> dict[str, Any]:
    ensure_zone_models(conn)
    telemetry_by_id, telemetry_by_day = _load_telemetry(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT clean_session_id, original_session_id, sport, start_time,
                   distance_km, duration_s, avg_hr_bpm, raw_json,
                   z2_pct_original, z2_pct_mars, zone_model_used,
                   zone_confidence, z2_confidence_score
            FROM clean_sessions
            ORDER BY start_time, clean_session_id
        """)
        columns = [description[0] for description in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    updates = []
    coverage = defaultdict(int)
    by_sport = defaultdict(int)
    used_telemetry_ids: set[str] = set()
    match_diagnostics = []
    source_match_counts = defaultdict(int)
    match_rejections = defaultdict(int)
    for row in rows:
        group = sport_group(row.get("sport"))
        if not group:
            continue
        samples, match, match_info = _match_telemetry(
            row,
            telemetry_by_id,
            telemetry_by_day,
            used_telemetry_ids,
        )
        if match:
            source_match_counts[match] += 1
        if match and match.startswith("linked"):
            match_diagnostics.append(match_info)
        elif not match:
            match_rejections[match_info["reason"]] += 1
        result = zone_result(
            row["sport"],
            float(row["avg_hr_bpm"]) if row.get("avg_hr_bpm") is not None else None,
            samples,
            match,
            int(row.get("duration_s") or 0),
        )
        original = (
            float(row["z2_pct_original"])
            if row.get("z2_pct_original") is not None
            else extract_original_z2(row.get("raw_json"))
        )
        updates.append({
            "clean_session_id": row["clean_session_id"],
            "duration_s": int(row.get("duration_s") or 0),
            "start_time": row.get("start_time"),
            "sport_group": group,
            "z2_pct_original": original,
            "needs_write": (
                _numeric_changed(row.get("z2_pct_mars"), result["z2_pct_mars"])
                or row.get("zone_model_used") != result["zone_model_used"]
                or row.get("zone_confidence") != result["zone_confidence"]
                or _numeric_changed(
                    row.get("z2_confidence_score"),
                    result["z2_confidence_score"],
                )
                or (
                    row.get("z2_pct_original") is None
                    and original is not None
                )
            ),
            **result,
        })
        by_sport[group] += 1
        coverage[result["zone_confidence"]] += 1

    snapshot_rows = _build_snapshot_rollup(updates)
    pending_updates = [update for update in updates if update["needs_write"]]
    if execute:
        now = datetime.now(timezone.utc)
        _write_session_batches(conn, pending_updates, now)
        _write_snapshot_batches(conn, snapshot_rows)

    telemetry_coverage = sum(
        count for confidence, count in coverage.items()
        if confidence.startswith("telemetry_")
    )
    source_sessions_matched = sum(source_match_counts.values())
    linked_offsets = defaultdict(int)
    for item in match_diagnostics:
        linked_offsets[str(item["offset_hours"])] += 1

    def _diagnostic_stat(field: str, mode: str = "avg") -> float:
        values = [float(item[field]) for item in match_diagnostics]
        if not values:
            return 0.0
        value = max(values) if mode == "max" else sum(values) / len(values)
        return round(value, 3)

    return {
        "ok": True,
        "dry_run": not execute,
        "sessions_scanned": len(rows),
        "sessions_eligible": len(updates),
        "sessions_by_sport": dict(by_sport),
        "coverage": dict(coverage),
        "telemetry_coverage": {
            "sessions": telemetry_coverage,
            "pct_eligible": round(telemetry_coverage / len(updates) * 100, 2)
            if updates else 0.0,
            "source_sessions_matched": source_sessions_matched,
            "source_sessions_without_hr": source_sessions_matched - telemetry_coverage,
            "unique_source_sessions": len(used_telemetry_ids),
            "one_to_one": source_sessions_matched == len(used_telemetry_ids),
            "match_confidence": dict(source_match_counts),
        },
        "linked_match_quality": {
            "offset_hours": dict(linked_offsets),
            "avg_time_delta_s": _diagnostic_stat("time_delta_s"),
            "max_time_delta_s": _diagnostic_stat("time_delta_s", "max"),
            "avg_distance_delta_km": _diagnostic_stat("distance_delta_km"),
            "max_distance_delta_km": _diagnostic_stat("distance_delta_km", "max"),
            "avg_duration_delta_s": _diagnostic_stat("duration_delta_s"),
            "max_duration_delta_s": _diagnostic_stat("duration_delta_s", "max"),
            "rejected": dict(match_rejections),
        },
        "weeks_calculated": len(snapshot_rows),
        "models": MODEL_IDS,
        "original_values_preserved": True,
        "sessions_already_current": len(updates) - len(pending_updates),
        "sessions_pending": len(pending_updates),
        "updated_sessions": len(pending_updates) if execute else 0,
        "updated_weeks": len(snapshot_rows) if execute else 0,
    }


def _numeric_changed(current: Any, calculated: Any, tolerance: float = 0.005) -> bool:
    if current is None or calculated is None:
        return current is not None or calculated is not None
    return abs(float(current) - float(calculated)) > tolerance


def _write_session_batches(
    conn,
    updates: list[dict[str, Any]],
    recalculated_at: datetime,
    batch_size: int = 100,
) -> None:
    query = """
        UPDATE clean_sessions AS target
        SET z2_pct_original = COALESCE(target.z2_pct_original, batch.z2_original::numeric),
            z2_pct_mars = batch.z2_mars::numeric,
            zone_model_used = batch.model_id,
            zone_confidence = batch.confidence,
            z2_confidence_score = batch.confidence_score::numeric,
            zone_recalculated_at = batch.recalculated_at::timestamptz
        FROM (VALUES %s) AS batch(
            clean_session_id, z2_original, z2_mars, model_id,
            confidence, confidence_score, recalculated_at
        )
        WHERE target.clean_session_id = batch.clean_session_id
    """
    with conn.cursor() as cur:
        for start in range(0, len(updates), batch_size):
            batch = updates[start:start + batch_size]
            values = [
                (
                    update["clean_session_id"],
                    update["z2_pct_original"],
                    update["z2_pct_mars"],
                    update["zone_model_used"],
                    update["zone_confidence"],
                    update["z2_confidence_score"],
                    recalculated_at,
                )
                for update in batch
            ]
            execute_values(cur, query, values, page_size=batch_size)


def _write_snapshot_batches(
    conn,
    snapshots: list[dict[str, Any]],
    batch_size: int = 100,
) -> None:
    query = """
        UPDATE athlete_snapshots AS target
        SET z2_pct_original = COALESCE(target.z2_pct_original, batch.z2_original::numeric),
            z2_pct_mars = batch.z2_mars::numeric,
            zone_model_used = batch.model_id,
            zone_confidence = batch.confidence,
            z2_confidence_score = batch.confidence_score::numeric,
            updated_at = NOW()
        FROM (VALUES %s) AS batch(
            week_start, z2_original, z2_mars, model_id,
            confidence, confidence_score
        )
        WHERE target.week_start = batch.week_start::date
    """
    with conn.cursor() as cur:
        for start in range(0, len(snapshots), batch_size):
            batch = snapshots[start:start + batch_size]
            values = [
                (
                    snapshot["week_start"],
                    snapshot["z2_pct_original"],
                    snapshot["z2_pct_mars"],
                    snapshot["zone_model_used"],
                    snapshot["zone_confidence"],
                    snapshot["z2_confidence_score"],
                )
                for snapshot in batch
            ]
            execute_values(cur, query, values, page_size=batch_size)


def _build_snapshot_rollup(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weeks: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for update in updates:
        start_time = update.get("start_time")
        if not start_time:
            continue
        week_start = start_time.date()
        week_start = week_start.fromordinal(week_start.toordinal() - week_start.weekday())
        weeks[week_start].append(update)

    result = []
    for week_start, rows in sorted(weeks.items()):
        valid = [
            row for row in rows
            if row.get("z2_pct_mars") is not None and row.get("duration_s", 0) > 0
        ]
        total_duration = sum(row["duration_s"] for row in valid)
        z2_pct_mars = (
            sum(row["z2_pct_mars"] * row["duration_s"] for row in valid) / total_duration
            if total_duration else None
        )
        confidence = (
            sum(row["z2_confidence_score"] * row["duration_s"] for row in valid) / total_duration
            if total_duration else 0.0
        )
        original_valid = [
            row for row in rows
            if row.get("z2_pct_original") is not None and row.get("duration_s", 0) > 0
        ]
        original_duration = sum(row["duration_s"] for row in original_valid)
        z2_pct_original = (
            sum(row["z2_pct_original"] * row["duration_s"] for row in original_valid)
            / original_duration
            if original_duration else None
        )
        models = sorted({
            row["zone_model_used"] for row in rows if row.get("zone_model_used")
        })
        result.append({
            "week_start": week_start,
            "z2_pct_original": round(z2_pct_original, 2) if z2_pct_original is not None else None,
            "z2_pct_mars": round(z2_pct_mars, 2) if z2_pct_mars is not None else None,
            "zone_model_used": ",".join(models) if models else None,
            "zone_confidence": (
                "telemetry_dominant" if confidence >= 0.75
                else "mixed" if confidence >= 0.45
                else "summary_estimate" if confidence > 0
                else "no_heart_rate"
            ),
            "z2_confidence_score": round(confidence, 2),
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Recalculate Mars Z2 without overwriting original data.")
    parser.add_argument("--execute", action="store_true", help="Write calculated fields.")
    args = parser.parse_args()
    conn = get_db()
    if not conn:
        raise RuntimeError("Database unavailable")
    print(json.dumps(recalculate_zones(conn, execute=args.execute), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
