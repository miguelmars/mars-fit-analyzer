"""
shared/sql_helpers.py — Helpers SQL, telemetría y geo compartidos
==================================================================
TD-010A/B: extraídos de main.py para compartir entre routers.

Importar así:
    from shared.sql_helpers import (
        SESSION_READ_TABLE, TELEMETRY_INDEX_CACHE,
        _telemetry_exists_sql, _sport_filter_sql,
        _enrich_session_dict, find_or_create_route,
    )
"""
import json
import math
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("mars_fit")

# ── Constantes ────────────────────────────────────────────────────────────────

SESSION_READ_TABLE = "sessions_clean_compat"

# Cache de telemetría (session_records) — TTL 5 min
TELEMETRY_INDEX_CACHE: dict = {"loaded_at": 0, "by_id": None, "by_day": None}


# ── Helpers de SQL de telemetría ─────────────────────────────────────────────

def _telemetry_match_sql(alias: str = "sessions_clean_compat") -> str:
    return f"""
        (
            sr.session_id IN ({alias}.session_id, REPLACE({alias}.session_id, 'session:', ''))
        )
    """


def _telemetry_exists_sql(alias: str = "sessions_clean_compat") -> str:
    return f"EXISTS(SELECT 1 FROM session_records sr WHERE {_telemetry_match_sql(alias)})"


def _telemetry_points_sql(alias: str = "sessions_clean_compat") -> str:
    return f"(SELECT COUNT(*) FROM session_records sr WHERE {_telemetry_match_sql(alias)})"


def _telemetry_map_sql(alias: str = "sessions_clean_compat") -> str:
    return (
        f"EXISTS(SELECT 1 FROM session_records sr "
        f"WHERE {_telemetry_match_sql(alias)} "
        f"AND sr.lat IS NOT NULL AND sr.lon IS NOT NULL)"
    )


# ── Filtro por deporte ────────────────────────────────────────────────────────

def _sport_filter_sql(sport, table_alias=None):
    """Retorna (sql_fragment, params) para WHERE según el deporte pedido."""
    col = f"{table_alias}.sport" if table_alias else "sport"
    if not sport or sport in ("all", "todo", "todos"):
        return "", []
    if sport in ("run", "running_all", "running_group"):
        return f" AND {col} IN %s", [("running", "trail_running", "treadmill_running")]
    if sport in ("bike", "cycling_all", "cycling_group"):
        return f" AND {col} IN %s", [("cycling", "indoor_cycling")]
    if sport in ("swim", "swimming", "swimming_all", "swimming_group"):
        return f" AND {col} IN %s", [("lap_swimming", "open_water_swimming")]
    if sport in ("strength", "fuerza", "strength_all", "strength_group"):
        return f" AND {col} IN %s", [("strength_training",)]
    if sport in ("walk", "walking", "walking_all", "walking_group"):
        return f" AND {col} IN %s", [("walking",)]
    if sport in ("cardio", "indoor_cardio", "indoor_cardio_all"):
        return f" AND {col} IN %s", [("indoor_cardio",)]
    if sport in ("yoga", "mobility", "mobility_all"):
        return f" AND {col} IN %s", [("yoga",)]
    if sport in ("multi", "multisport", "multi_sport", "transition", "triathlon"):
        return f" AND {col} IN %s", [("multi_sport", "bikeToRunTransition_v2")]
    return f" AND {col} = %s", [sport]


# ── Parse de fechas loose ─────────────────────────────────────────────────────

def _parse_dt_loose(value) -> Optional[datetime]:
    """Parsea una fecha ISO flexible (con Z, con offset, sin tz)."""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        try:
            return datetime.fromisoformat(str(value).split("+")[0])
        except Exception:
            return None


# ── Índice de telemetría (cache) ──────────────────────────────────────────────

def _telemetry_match_for_session(session, old_records_by_id, old_records_by_day):
    """Busca si una sesión clean tiene telemetría en session_records (por ID o por fecha+distancia)."""
    sid = session.get("session_id") or ""
    candidates = [sid, sid.replace("session:", "")]
    for cid in candidates:
        if cid and cid in old_records_by_id:
            return old_records_by_id[cid]

    clean_dt = _parse_dt_loose(session.get("start_time"))
    if not clean_dt:
        return None
    try:
        clean_distance = float(session.get("distance_km") or 0)
        clean_duration = int(float(session.get("duration_s") or 0))
    except Exception:
        return None

    for old in old_records_by_day.get(clean_dt.date().isoformat(), []):
        old_dt = old.get("_dt")
        if not old_dt:
            continue
        try:
            seconds = abs((clean_dt.replace(tzinfo=None) - old_dt.replace(tzinfo=None)).total_seconds())
            distance_delta = abs(clean_distance - float(old.get("distance_km") or 0))
            duration_delta = abs(clean_duration - int(float(old.get("duration_s") or 0)))
        except Exception:
            continue
        time_matches = seconds <= 5 or abs(seconds - 21600) <= 5
        if time_matches and distance_delta <= 0.02 and duration_delta <= 180:
            return old
    return None


def _load_old_telemetry_index(conn):
    """Carga (o usa cache) el índice de sesiones con telemetría (session_records)."""
    now_ts = datetime.now(timezone.utc).timestamp()
    if (
        TELEMETRY_INDEX_CACHE.get("by_id") is not None
        and now_ts - TELEMETRY_INDEX_CACHE.get("loaded_at", 0) < 300
    ):
        return TELEMETRY_INDEX_CACHE["by_id"], TELEMETRY_INDEX_CACHE["by_day"]

    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.session_id, s.start_time, s.distance_km, s.duration_s,
                   COUNT(sr.*) AS telemetry_points,
                   BOOL_OR(sr.lat IS NOT NULL AND sr.lon IS NOT NULL) AS has_map
            FROM sessions s
            JOIN session_records sr ON sr.session_id = s.session_id
            WHERE NULLIF(s.start_time, '') IS NOT NULL
            GROUP BY s.session_id, s.start_time, s.distance_km, s.duration_s
        """)
        rows = cur.fetchall()

    by_id: dict = {}
    by_day: dict = {}
    for sid, start_time, distance_km, duration_s, points, has_map in rows:
        dt = _parse_dt_loose(start_time)
        item = {
            "session_id": sid,
            "start_time": start_time,
            "distance_km": distance_km,
            "duration_s": duration_s,
            "telemetry_points": int(points or 0),
            "has_map": bool(has_map),
            "_dt": dt,
        }
        by_id[sid] = item
        if dt:
            by_day.setdefault(dt.date().isoformat(), []).append(item)

    TELEMETRY_INDEX_CACHE.update({"loaded_at": now_ts, "by_id": by_id, "by_day": by_day})
    return by_id, by_day


# ── Enriquecimiento de sesión ─────────────────────────────────────────────────

def _extract_calories_from_payload(payload) -> Optional[int]:
    """Recupera calorías del JSON crudo cuando clean_sessions.calories está vacío."""
    if not payload:
        return None
    try:
        data = payload if isinstance(payload, dict) else json.loads(payload)
    except Exception:
        return None

    candidates = [
        data.get("calories"),
        data.get("calories_kcal"),
        data.get("total_calories"),
    ]
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    candidates.extend([
        session.get("calories"),
        session.get("calories_kcal"),
        session.get("total_calories"),
    ])
    for value in candidates:
        try:
            if value is None or value == "":
                continue
            cal = int(round(float(value)))
            if cal <= 0:
                continue
            return int(round(cal / 10)) if cal > 5000 else cal
        except Exception:
            continue
    return None


def _enrich_session_dict(session: dict) -> dict:
    """Normaliza una sesión clean antes de devolverla a UI/GPT endpoints."""
    if not session:
        return session
    payload = session.get("result_json")
    if not session.get("calories"):
        recovered = _extract_calories_from_payload(payload)
        if recovered:
            session["calories"] = recovered
            session["calories_source"] = "result_json"
    else:
        session["calories_source"] = "clean_sessions"
    if session.get("avg_speed_kmh") and session.get("avg_hr_bpm") and not session.get("efficiency"):
        try:
            session["efficiency"] = round(
                float(session["avg_speed_kmh"]) / float(session["avg_hr_bpm"]), 4
            )
        except Exception:
            session["efficiency"] = None
    session["telemetry_status"] = (
        "available" if session.get("has_telemetry") else "summary_only"
    )
    session["charts_status"] = (
        "available" if session.get("has_telemetry") else "needs_original_fit_tcx"
    )
    return session


# ── Geo helpers ───────────────────────────────────────────────────────────────

def coords_within_meters(lat1, lon1, lat2, lon2, meters: float) -> bool:
    """True si dos coordenadas están dentro de N metros."""
    if None in (lat1, lon1, lat2, lon2):
        return False
    dlat = abs(lat1 - lat2) * 111000
    dlon = abs(lon1 - lon2) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat**2 + dlon**2) <= meters


def route_signature(start_lat, start_lon, end_lat, end_lon, distance_km, ascent_m) -> Optional[str]:
    """Genera una firma fuzzy de ruta para matching (usado en derived_insights)."""
    if not start_lat or not start_lon:
        return None
    slat = round(start_lat * 200) / 200
    slon = round(start_lon * 200) / 200
    elat = round(end_lat * 200) / 200 if end_lat else slat
    elon = round(end_lon * 200) / 200 if end_lon else slon
    dist_bucket = round(distance_km / 3) * 3
    asc_bucket = round(ascent_m / 100) * 100
    return f"{slat:.3f},{slon:.3f}|{elat:.3f},{elon:.3f}|{dist_bucket}|{asc_bucket}"


def find_or_create_route(
    conn,
    start_lat, start_lon, end_lat, end_lon,
    distance_km, ascent_m, workout_name, sport=""
) -> Optional[dict]:
    """
    Busca una ruta existente que haga match o crea una nueva.

    Criterios de match (todos deben pasar):
      1. Mismo sport
      2. Start coords dentro de 500m
      3. End coords dentro de 500m (si disponibles)
      4. Distancia dentro de 15%
      5. Ascenso dentro de 20% (solo si > 50m)
    """
    if not conn or not start_lat:
        return None

    sport_norm = str(sport).lower().strip() if sport else ""

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT route_id, name, distance_km, ascent_m,
                       sample_lat, sample_lon, end_lat, end_lon, sport
                FROM routes
                WHERE ABS(sample_lat - %s) < 0.05
                  AND ABS(sample_lon - %s) < 0.05
                  AND (sport = %s OR sport IS NULL OR sport = '')
            """, (start_lat, start_lon, sport_norm))
            candidates = cur.fetchall()

        for row in candidates:
            r_id, r_name, r_dist, r_asc, r_slat, r_slon, r_elat, r_elon, r_sport = row

            if sport_norm and r_sport and sport_norm != r_sport.lower().strip():
                continue
            if not coords_within_meters(start_lat, start_lon, r_slat, r_slon, 500):
                continue
            if end_lat and end_lon and r_elat and r_elon:
                if not coords_within_meters(end_lat, end_lon, r_elat, r_elon, 500):
                    continue
            if r_dist and r_dist > 0:
                if abs(distance_km - r_dist) / r_dist > 0.15:
                    continue
            if r_asc and r_asc > 50 and ascent_m and ascent_m > 50:
                if abs(ascent_m - r_asc) / r_asc > 0.20:
                    continue
            return {"route_id": r_id, "name": r_name}

        # No match → crear ruta nueva
        route_id = str(uuid.uuid4())[:8]
        if workout_name and workout_name not in ("", "None"):
            base = (
                workout_name
                .replace("Atizapán de Zaragoza - ", "")
                .replace("Atizapán de Zaragoza", "")
                .strip()
            )
            name = base if base else f"Ruta {round(distance_km)}km"
        else:
            name = f"Ruta {round(distance_km)}km +{ascent_m}m"

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO routes
                    (route_id, name, distance_km, ascent_m, created_at,
                     sample_lat, sample_lon, end_lat, end_lon, sport)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                route_id, name, round(distance_km, 1), ascent_m,
                datetime.now(timezone.utc),
                start_lat, start_lon, end_lat, end_lon, sport_norm,
            ))
        return {"route_id": route_id, "name": name}

    except Exception as e:
        logger.error(f"find_or_create_route error: {e}")
        return None
