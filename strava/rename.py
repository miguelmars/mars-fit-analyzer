"""Rename Strava activities using Garmin/EPOCH workout intent.

Strava often replaces structured Garmin workout titles with generic names such
as "Morning Ride". For EPOCH, Garmin/plan intent is the source of truth; Strava
is the public activity surface. This module reconciles the two without guessing
when confidence is low.
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import get_db

from .auth import get_supabase
from .client import update_activity_name

logger = logging.getLogger("bitacora.strava.rename")

GENERIC_NAME_FRAGMENTS = (
    "morning ride",
    "afternoon ride",
    "evening ride",
    "lunch ride",
    "night ride",
    "morning run",
    "afternoon run",
    "evening run",
    "paseo matutino",
    "paseo en la manana",
    "paseo por la manana",
    "paseo de manana",
    "paseo vespertino",
    "paseo por la tarde",
    "paseo en la tarde",
    "paseo nocturno",
    "carrera matutina",
    "carrera por la manana",
    "carrera vespertina",
    "carrera nocturna",
)


def _plain(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def is_generic_strava_name(name: str) -> bool:
    value = _plain(name)
    return any(fragment in value for fragment in GENERIC_NAME_FRAGMENTS)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sport_family(value: str) -> str:
    sport = _plain(value)
    if "ride" in sport or "cycling" in sport or "bike" in sport:
        return "cycling"
    if "run" in sport or "running" in sport:
        return "running"
    if "swim" in sport:
        return "swimming"
    if "walk" in sport:
        return "walking"
    if "yoga" in sport:
        return "yoga"
    if "weight" in sport or "strength" in sport:
        return "strength"
    return sport or "unknown"


def _usable_intent_name(name: str) -> bool:
    value = _plain(name)
    if not value:
        return False
    if is_generic_strava_name(value):
        return False
    if "type to confirm" in value:
        return False
    if value in {"ride", "run", "workout", "activity"}:
        return False
    return True


def _find_garmin_match(conn, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    started = _parse_dt(row.get("started_at"))
    if not started:
        return None

    distance_km = (row.get("distance_m") or 0) / 1000.0
    duration_s = row.get("moving_time_s") or row.get("elapsed_time_s") or 0
    family = _sport_family(row.get("sport_type") or "")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_activity_id, name, sport, sport_type, start_time_utc,
                   distance_km, duration_s
            FROM garmin_export_activities
            WHERE start_time_utc BETWEEN %s AND %s
              AND name IS NOT NULL
            ORDER BY ABS(EXTRACT(EPOCH FROM (start_time_utc - %s))) ASC
            LIMIT 12
            """,
            (started - timedelta(minutes=12), started + timedelta(minutes=12), started),
        )
        candidates = cur.fetchall()

    best = None
    best_score = -1
    for source_id, name, sport, sport_type, start_utc, g_distance, g_duration in candidates:
        if not _usable_intent_name(name):
            continue
        if _sport_family(sport or sport_type or "") != family:
            continue
        time_diff = abs((start_utc.astimezone(timezone.utc) - started).total_seconds())
        dist_diff = abs(float(g_distance or 0) - float(distance_km or 0))
        dur_diff = abs(float(g_duration or 0) - float(duration_s or 0))

        score = 0
        if time_diff <= 120:
            score += 45
        elif time_diff <= 600:
            score += 25
        if distance_km <= 0.1 or dist_diff <= max(0.3, 0.04 * max(distance_km, float(g_distance or 0), 1)):
            score += 35
        if duration_s <= 0 or dur_diff <= max(180, 0.06 * max(duration_s, float(g_duration or 0), 1)):
            score += 20

        if score > best_score:
            best_score = score
            best = {
                "source": "garmin_export",
                "name": name,
                "garmin_source_activity_id": source_id,
                "confidence": score,
                "time_diff_s": round(time_diff),
                "distance_diff_km": round(dist_diff, 3),
                "duration_diff_s": round(dur_diff),
            }

    if best and best["confidence"] >= 70:
        return best
    return None


def _find_plan_match(conn, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    planned_date = (row.get("started_at_local") or row.get("started_at") or "")[:10]
    if not planned_date:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ps.description, ps.session_type, tp.name
            FROM plan_sessions ps
            JOIN training_plans tp ON tp.plan_id = ps.plan_id
            WHERE tp.status = 'active'
              AND ps.planned_date = %s
            ORDER BY ps.id
            LIMIT 1
            """,
            (planned_date,),
        )
        match = cur.fetchone()
    if not match:
        return None
    description, session_type, plan_name = match
    name = description or session_type
    if not _usable_intent_name(name):
        return None
    clean_name = str(name).replace(" (plan Garmin)", "").strip()
    return {
        "source": "epoch_plan",
        "name": clean_name,
        "plan_name": plan_name,
        "confidence": 80,
    }


def _candidate_name(conn, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    return _find_garmin_match(conn, row) or _find_plan_match(conn, row)


async def rename_strava_activity(activity_id: int, name: str, execute: bool = False) -> dict[str, Any]:
    """Manual rename for a known activity id."""
    result = {
        "strava_id": activity_id,
        "target_name": name,
        "execute": execute,
        "changed": False,
    }
    if not execute:
        result["status"] = "preview"
        return result

    updated = await update_activity_name(activity_id, name)
    result.update({"status": "renamed", "changed": True, "strava_response_name": updated.get("name")})

    sb = get_supabase()
    sb.table("strava_activities_raw").update({"name": name}).eq("strava_id", activity_id).execute()
    conn = get_db()
    if conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE clean_sessions
                SET name = %s,
                    quality_notes = COALESCE(quality_notes, '') || ' | Strava title reconciled by EPOCH'
                WHERE source = 'strava'
                  AND source_activity_id = %s
                """,
                (name, str(activity_id)),
            )
        conn.commit()

    return result


async def reconcile_strava_names(
    execute: bool = False,
    limit: int = 50,
    only_generic: bool = True,
) -> dict[str, Any]:
    """Preview or apply Garmin/EPOCH names to Strava activities."""
    sb = get_supabase()
    conn = get_db()
    if not conn:
        return {"status": "error", "message": "Database unavailable"}

    response = (
        sb.table("strava_activities_raw")
        .select(
            "strava_id,name,sport_type,started_at,started_at_local,"
            "distance_m,moving_time_s,elapsed_time_s"
        )
        .order("started_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = response.data or []
    actions = []
    renamed = 0
    skipped = 0

    for row in rows:
        current_name = row.get("name") or ""
        if only_generic and not is_generic_strava_name(current_name):
            skipped += 1
            continue

        candidate = _candidate_name(conn, row)
        if not candidate:
            skipped += 1
            actions.append({
                "strava_id": row.get("strava_id"),
                "current_name": current_name,
                "status": "no_safe_match",
            })
            continue

        target = candidate["name"]
        if _plain(target) == _plain(current_name):
            skipped += 1
            continue

        action = {
            "strava_id": row.get("strava_id"),
            "current_name": current_name,
            "target_name": target,
            "match": candidate,
            "execute": execute,
            "status": "preview",
        }

        if execute:
            try:
                await update_activity_name(int(row["strava_id"]), target)
                sb.table("strava_activities_raw").update({"name": target}).eq(
                    "strava_id", row["strava_id"]
                ).execute()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE clean_sessions
                        SET name = %s,
                            quality_notes = COALESCE(quality_notes, '') || ' | Strava title reconciled by EPOCH'
                        WHERE source = 'strava'
                          AND source_activity_id = %s
                        """,
                        (target, str(row["strava_id"])),
                    )
                conn.commit()
                renamed += 1
                action["status"] = "renamed"
            except Exception as exc:
                conn.rollback()
                action["status"] = "error"
                action["error"] = str(exc)

        actions.append(action)

    return {
        "status": "ok",
        "execute": execute,
        "only_generic": only_generic,
        "checked": len(rows),
        "renamed": renamed,
        "skipped": skipped,
        "actions": actions,
        "note": "If Strava returns 401/403, re-authorize with /api/strava/authorize to grant activity:write.",
    }
