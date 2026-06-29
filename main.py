"""
Epoch API v6.4
=====================================
Endpoints:
  GET  /                        → página web para subir desde el celular
  POST /analyze-fit             → procesa ZIP/FIT, guarda en DB, devuelve session_id
  GET  /result/{session_id}     → GPT consulta resultado por ID
  GET  /charts/{session_id}     → gráficas interactivas de la sesión
  GET  /routes                  → lista de rutas identificadas con historial
  GET  /route/{route_id}        → detalle y progreso de una ruta específica
  GET  /sessions                → lista sesiones (debug)
"""

from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, List
from pydantic import BaseModel, Field
import tempfile, os, zipfile, math, statistics, uuid, json
from datetime import datetime, timezone, timedelta
import logging
from logging.handlers import RotatingFileHandler

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("mars_fit")
logger.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(f"{LOG_DIR}/mars_fit.log", maxBytes=2_000_000, backupCount=5)
_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(_log_handler)

try:
    import fitparse
except ImportError:
    raise RuntimeError("pip install fitparse")

# ── Database setup ────────────────────────────────────────────────────────────
# Uses PostgreSQL (Supabase) when DATABASE_URL is set, otherwise in-memory dict




def store_session(sid, data):
    """Guarda sesión en memoria limitando a RESULTS_STORE_MAX entradas."""
    RESULTS_STORE[sid] = data
    if len(RESULTS_STORE) > RESULTS_STORE_MAX:
        # Eliminar la entrada más antigua
        oldest = next(iter(RESULTS_STORE))
        del RESULTS_STORE[oldest]

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Constants ─────────────────────────────────────────────────────────────────
SEMICIRCLES_TO_DEG = 180 / 2**31

MARS_ZONES = [
    {"zone": 1, "name": "Z1 Recovery",  "bpm_low": 0,   "bpm_high": 108},
    {"zone": 2, "name": "Z2 Aerobic",   "bpm_low": 134, "bpm_high": 150},
    {"zone": 3, "name": "Z3 Tempo",        "bpm_low": 151, "bpm_high": 160},
    {"zone": 4, "name": "Z4 Threshold", "bpm_low": 161, "bpm_high": 168},
    {"zone": 5, "name": "Z5 Maximum",   "bpm_low": 169, "bpm_high": 999},
]

# coords_within_meters, route_signature, find_or_create_route → shared/sql_helpers.py (TD-010A)


def check_duplicate_session(conn, start_time, duration_s, distance_km, file_hash=None):
    """Detecta duplicados por hash SHA256 primero, luego por fecha+duración+distancia."""
    try:
        with conn.cursor() as cur:
            if file_hash:
                cur.execute("SELECT session_id FROM sessions WHERE file_hash=%s LIMIT 1", (file_hash,))
                row = cur.fetchone()
                if row:
                    return row[0]
            if start_time:
                cur.execute("""
                    SELECT session_id FROM sessions
                    WHERE ABS(EXTRACT(EPOCH FROM (start_time::timestamp - %s::timestamp))) < 60
                      AND ABS(COALESCE(duration_s,0) - %s) < 30
                      AND ABS(COALESCE(distance_km,0) - %s) < 0.5
                    LIMIT 1
                """, (start_time, duration_s or 0, distance_km or 0))
                row = cur.fetchone()
                if row:
                    return row[0]
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
    return None


def save_session_db(conn, session_id, filename, result, file_hash=None):
    """Persist session to PostgreSQL."""
    if not conn:
        return
    s = result["session"]
    records = result.get("records", [])
    start_lat = end_lat = start_lon = end_lon = None
    if records:
        for r in records:
            if r.get("lat"):
                start_lat, start_lon = r["lat"], r["lon"]
                break
        for r in reversed(records):
            if r.get("lat"):
                end_lat, end_lon = r["lat"], r["lon"]
                break

    route = find_or_create_route(
        conn, start_lat, start_lon, end_lat, end_lon,
        s.get("distance_km", 0), s.get("ascent_m", 0) or 0,
        s.get("workout_name", ""), s.get("sport", "")
    )

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO sessions
            (session_id, filename, uploaded_at, start_time, sport, distance_km,
             duration_s, ascent_m, avg_hr_bpm, avg_speed_kmh, avg_cadence,
             workout_name, start_lat, start_lon, end_lat, end_lon, route_id, result_json, file_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (session_id) DO NOTHING
        """, (
            session_id, filename, datetime.now(timezone.utc),
            s.get("start_time"), s.get("sport"),
            s.get("distance_km"), s.get("duration_s"),
            s.get("ascent_m"), s.get("avg_hr_bpm"),
            s.get("avg_speed_kmh"), s.get("avg_cadence_rpm"),
            s.get("workout_name", ""),
            start_lat, start_lon, end_lat, end_lon,
            route["route_id"] if route else None,
            json.dumps({k: v for k, v in result.items() if k != "records"}),
            file_hash
        ))


def save_records_db(conn, session_id, records):
    """Guarda records de telemetría en session_records."""
    if not conn or not records:
        return
    # Calcular offset de tiempo desde el primer record
    from datetime import datetime as dt
    start_ts = None
    rows = []
    for r in records:
        try:
            t = dt.fromisoformat(r.get("timestamp", ""))
            if start_ts is None:
                start_ts = t
            elapsed = int((t - start_ts).total_seconds())
        except:
            elapsed = len(rows)

        lat = r.get("lat")
        lon = r.get("lon")
        rows.append((
            session_id, elapsed,
            r.get("heart_rate_bpm"),
            r.get("speed_kmh"),
            r.get("cadence_rpm"),
            r.get("altitude_m"),
            round(lat, 6) if lat else None,
            round(lon, 6) if lon else None,
            r.get("power_watts")
        ))

    # Insertar en lotes de 500
    try:
        with conn.cursor() as cur:
            # Borrar registros anteriores si los hay
            cur.execute("DELETE FROM session_records WHERE session_id=%s", (session_id,))
            batch = 500
            for i in range(0, len(rows), batch):
                chunk = rows[i:i+batch]
                args = ','.join(cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s,%s)", row).decode() for row in chunk)
                cur.execute(f"INSERT INTO session_records (session_id,t,hr,speed,cadence,altitude,lat,lon,power) VALUES {args} ON CONFLICT DO NOTHING")
        logger.info(f"Records saved: {len(rows)} rows for {session_id}")
    except Exception as e:
        logger.error(f"save_records_db error: {e}")


# ── FIT parsing helpers ───────────────────────────────────────────────────────

def extract_fit_from_zip(zip_bytes):
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes); zpath = f.name
    try:
        with zipfile.ZipFile(zpath) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise HTTPException(400, "The ZIP contains no .fit file")
            return zf.read(fits[0])
    finally:
        os.unlink(zpath)

def percentile(values, p):
    values = sorted([v for v in values if v is not None])
    if not values: return None
    k = (len(values)-1)*(p/100); f = math.floor(k); c = math.ceil(k)
    if f == c: return values[int(k)]
    return values[f]*(c-k)+values[c]*(k-f)

def zone_for_hr(hr):
    if hr is None: return None
    if 109 <= hr <= 133: return 0
    for z in MARS_ZONES:
        if z["bpm_low"] <= hr <= z["bpm_high"]: return z["zone"]
    return None

def summarize_records(records):
    hrs  = [r["heart_rate_bpm"] for r in records if r.get("heart_rate_bpm") is not None]
    cads = [r["cadence_rpm"]    for r in records if r.get("cadence_rpm")    is not None]
    spds = [r["speed_kmh"]      for r in records if r.get("speed_kmh")      is not None]
    alts = [r["altitude_m"]     for r in records if r.get("altitude_m")     is not None]
    return {
        "records_count": len(records),
        "hr":      {"min":min(hrs) if hrs else None,"max":max(hrs) if hrs else None,
                    "avg":round(statistics.mean(hrs),1) if hrs else None,
                    "p90":round(percentile(hrs,90),1) if hrs else None},
        "cadence": {"min":min(cads) if cads else None,"max":max(cads) if cads else None,
                    "avg":round(statistics.mean(cads),1) if cads else None,
                    "p90":round(percentile(cads,90),1) if cads else None},
        "speed":   {"min_kmh":round(min(spds),1) if spds else None,
                    "max_kmh":round(max(spds),1) if spds else None,
                    "avg_kmh":round(statistics.mean(spds),1) if spds else None},
        "altitude":{"min_m":round(min(alts),1) if alts else None,
                    "max_m":round(max(alts),1) if alts else None},
    }

def compute_zones(records):
    counts = {z["zone"]: 0 for z in MARS_ZONES}; gap_count = 0
    for rec in records:
        hr = rec.get("heart_rate_bpm")
        if hr is None: continue
        z = zone_for_hr(hr)
        if z == 0: gap_count += 1
        elif z is not None: counts[z] += 1
    total = sum(counts.values()) + gap_count or 1
    zones = []
    for z in MARS_ZONES:
        secs = counts[z["zone"]]
        zones.append({"zone":z["zone"],"name":z["name"],"bpm_low":z["bpm_low"],
                      "bpm_high":None if z["bpm_high"]==999 else z["bpm_high"],
                      "seconds":secs,"minutes":round(secs/60,1),
                      "percent":round(secs/total*100,1)})
    zones.append({"zone":0,"name":"Entre Z1 y Z2 oficial","bpm_low":109,"bpm_high":133,
                  "seconds":gap_count,"minutes":round(gap_count/60,1),
                  "percent":round(gap_count/total*100,1)})
    return zones

def derive_insights(records, laps, session):
    """Generate automatic insights from second-by-second data."""
    if not records:
        return {}

    insights = {}

    # HR drift between first and last third
    n = len(records)
    if n > 30:
        first_third = [r["heart_rate_bpm"] for r in records[:n//3] if r.get("heart_rate_bpm")]
        last_third  = [r["heart_rate_bpm"] for r in records[2*n//3:] if r.get("heart_rate_bpm")]
        if first_third and last_third:
            drift = round(statistics.mean(last_third) - statistics.mean(first_third), 1)
            insights["hr_drift_bpm"] = drift
            if drift > 8:
                insights["hr_drift_note"] = f"Cardiac drift of +{drift} bpm from start to finish — possible fatigue or heat buildup"
            elif drift < -5:
                insights["hr_drift_note"] = f"HR dropped {abs(drift)} bpm toward the end — good recovery or lower intensity"
            else:
                insights["hr_drift_note"] = f"HR steady throughout the session (drift of {drift} bpm)"

    # Best aerobic window (5-min rolling where HR is in Z2 and speed is highest)
    window = 300  # 5 min
    best_window_spd = 0
    best_window_start = None
    for i in range(len(records) - window):
        chunk = records[i:i+window]
        hrs = [r["heart_rate_bpm"] for r in chunk if r.get("heart_rate_bpm")]
        spds = [r["speed_kmh"] for r in chunk if r.get("speed_kmh")]
        if not hrs or not spds: continue
        avg_hr = statistics.mean(hrs)
        avg_spd = statistics.mean(spds)
        if 134 <= avg_hr <= 150 and avg_spd > best_window_spd:
            best_window_spd = avg_spd
            best_window_start = i
    if best_window_start is not None:
        start_rec = records[best_window_start]
        insights["best_aerobic_window"] = {
            "speed_kmh": round(best_window_spd, 1),
            "note": f"Best Z2 aerobic window: {round(best_window_spd,1)} km/h at minute ~{best_window_start//60}"
        }

    # Cadence drops (likely traffic/stops)
    cad_vals = [r.get("cadence_rpm") or 0 for r in records]
    drops = sum(1 for i in range(1, len(cad_vals))
                if cad_vals[i-1] > 40 and cad_vals[i] < 5)
    if drops > 0:
        insights["traffic_stops_approx"] = drops
        insights["traffic_note"] = f"~{drops} stops or interruptions detected (cadence drops to 0)"

    # Altitude impact
    alts = [r.get("altitude_m") for r in records if r.get("altitude_m")]
    if alts:
        alt_range = max(alts) - min(alts)
        insights["altitude_range_m"] = round(alt_range, 0)
        if alt_range > 80:
            insights["altitude_note"] = f"Rolling terrain of {round(alt_range)}m — climbs and descents affect speed and HR"

    # Route signature for matching
    start_lat = end_lat = start_lon = end_lon = None
    for r in records:
        if r.get("lat"):
            start_lat, start_lon = r["lat"], r["lon"]
            break
    for r in reversed(records):
        if r.get("lat"):
            end_lat, end_lon = r["lat"], r["lon"]
            break
    if start_lat:
        insights["route_signature"] = route_signature(
            start_lat, start_lon, end_lat, end_lon,
            session.get("distance_km", 0), session.get("ascent_m", 0) or 0
        )
        insights["start_coords"] = {"lat": start_lat, "lon": start_lon}
        insights["end_coords"]   = {"lat": end_lat,   "lon": end_lon}

    return insights

def parse_fit(fit_bytes, include_records=True):
    with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as f:
        f.write(fit_bytes); fpath = f.name
    try:
        fit = fitparse.FitFile(fpath)
        sr = {}
        for msg in fit.get_messages("session"):
            for d in msg:
                if d.value is not None: sr[d.name] = d.value

        # Get workout name
        workout_name = ""
        for msg in fit.get_messages("workout"):
            for d in msg:
                if d.name == "wkt_name" and d.value: workout_name = str(d.value)

        et = sr.get("total_elapsed_time", 0) or 0
        session = {
            "start_time":               str(sr.get("start_time", "")),
            "duration_seconds":         round(et),
            "duration_s":               round(et),
            "duration_hms":             f"{int(et//3600):02d}h {int((et%3600)//60):02d}m {int(et%60):02d}s",
            "distance_km":              round((sr.get("total_distance", 0) or 0)/1000, 2),
            "calories_kcal":            sr.get("total_calories"),
            "ascent_m":                 sr.get("total_ascent"),
            "descent_m":                sr.get("total_descent"),
            "avg_hr_bpm":               sr.get("avg_heart_rate"),
            "max_hr_bpm":               sr.get("max_heart_rate"),
            "avg_speed_kmh":            round((sr.get("avg_speed", 0) or 0)*3.6, 1),
            "max_speed_kmh":            round((sr.get("max_speed", 0) or 0)*3.6, 1),
            "avg_cadence_rpm":          sr.get("avg_cadence"),
            "max_cadence_rpm":          sr.get("max_cadence"),
            "avg_temperature_c":        sr.get("avg_temperature"),
            "max_temperature_c":        sr.get("max_temperature"),
            "training_effect_aerobic":  sr.get("total_training_effect"),
            "training_effect_anaerobic":sr.get("total_anaerobic_training_effect"),
            "sport":                    str(sr.get("sport", "")),
            "sub_sport":                str(sr.get("sub_sport", "")),
            "workout_name":             workout_name,
        }

        laps = []
        for i, msg in enumerate(fit.get_messages("lap"), 1):
            r = {d.name: d.value for d in msg if d.value is not None}
            t = r.get("total_elapsed_time", 0) or 0
            laps.append({
                "lap": i, "duration_s": round(t),
                "duration_mmss": f"{int(t//60)}m{int(t%60):02d}s",
                "distance_km": round((r.get("total_distance", 0) or 0)/1000, 2),
                "avg_hr_bpm": r.get("avg_heart_rate"), "max_hr_bpm": r.get("max_heart_rate"),
                "avg_speed_kmh": round((r.get("avg_speed", 0) or 0)*3.6, 1),
                "avg_cadence_rpm": r.get("avg_cadence"), "calories_kcal": r.get("total_calories"),
            })

        records = []
        for msg in fit.get_messages("record"):
            rec = {d.name: d.value for d in msg if d.value is not None}
            lat = rec.get("position_lat"); lon = rec.get("position_long")
            spd = rec.get("speed", rec.get("enhanced_speed", 0)) or 0
            records.append({
                "timestamp":      str(rec.get("timestamp", "")),
                "heart_rate_bpm": rec.get("heart_rate"),
                "speed_kmh":      round(spd*3.6, 2),
                "cadence_rpm":    rec.get("cadence"),
                "altitude_m":     rec.get("enhanced_altitude", rec.get("altitude")),
                "distance_m":     round(rec.get("distance", 0), 1),
                "temperature_c":  rec.get("temperature"),
                "lat":            round(lat*SEMICIRCLES_TO_DEG, 6) if lat else None,
                "lon":            round(lon*SEMICIRCLES_TO_DEG, 6) if lon else None,
            })

        insights = derive_insights(records, laps, session)

        result = {
            "athlete":        "Mars / Miguel Ángel Ramírez Sousa",
            "zone_model":     "Official Mars zones by bpm",
            "zones_definition": MARS_ZONES,
            "session":        session,
            "laps":           laps,
            "zones":          compute_zones(records),
            "record_summary": summarize_records(records),
            "derived_insights": insights,
            "analysis_guidance": {
                "use_as_primary_data": True,
                "do_not_invent": True,
                "notes": [
                    "Use derived_insights for context so Mars does not have to explain anything.",
                    "traffic_stops_approx means traffic stops — do not read it as fatigue.",
                    "hr_drift_note already carries the cardiac-drift interpretation.",
                    "best_aerobic_window is the session's best real Z2 window.",
                ]
            }
        }
        if include_records:
            result["records"] = records
        return result
    finally:
        os.unlink(fpath)


# ── FastAPI app ───────────────────────────────────────────────────────────────

from db import DATABASE_URL, db_conn, get_db, _init_db, _ensure_gear_service_table, _ensure_gear_activity_links_table, _ensure_nutrition_table, _ensure_weight_table, _ensure_wellness_table, _ensure_fuerza_table, _ensure_accidents_table, _ensure_garmin_staging_tables, _ensure_clean_sessions_table, _ensure_clean_sessions_compat_view, _ensure_zone_model_system, _ensure_session_environment_table, _ensure_capability_runs_table, _ensure_goals_table, RESULTS_STORE_MAX
from mars_context import MARS_PROFILE_DEFAULT, MARS_ZONES as MARS_ZONES_PROFILE, _ensure_profile_table, _get_profile, get_zone_label, analyze_session_quick

# TD-010A — Shared models, helpers y sql_helpers extraídos del monolito
from shared.models import (
    GearServiceIn, AccidentIn, NutritionIn,
    WeightIn, WellnessIn, FuerzaIn,
    PostSessionIn, GearIn, GearUpdate, MaintenanceIn, RecoveryIn,
    GoalCreate, GoalUpdate,
    AthleteProfileIn, AthleteTestIn,
)
from shared.helpers import (
    detect_and_save_achievements,
    generate_weekly_snapshot,
    _normalize_capability_name,
    _find_capability,
    _previous_capability_run,
)
from shared.sql_helpers import (
    SESSION_READ_TABLE, TELEMETRY_INDEX_CACHE,
    _telemetry_match_sql, _telemetry_exists_sql,
    _telemetry_points_sql, _telemetry_map_sql,
    _sport_filter_sql, _parse_dt_loose,
    _telemetry_match_for_session, _load_old_telemetry_index,
    _extract_calories_from_payload, _enrich_session_dict,
    coords_within_meters, route_signature, find_or_create_route,
)

# _load_old_telemetry_index, _extract_calories_from_payload, _enrich_session_dict
# → shared/sql_helpers.py (TD-010A)

# GearServiceIn, AccidentIn, NutritionIn → shared/models.py (TD-010A)


from shared.results_store import RESULTS_STORE  # TD-010A: moved to shared

app = FastAPI(title="Epoch API", version="6.4")


# ── Auto stream backfill — corre en Railway sin necesitar la compu ────────────
import asyncio as _asyncio

async def _auto_activate_latest_garmin():
    """Activate the bundled Garmin reference inside Railway, once per database."""
    _logger = logging.getLogger("epoch.garmin_bootstrap")
    await _asyncio.sleep(5)
    for attempt in range(1, 7):
        try:
            from tools.auto_activate_garmin import activate_bundled_snapshot

            result = await _asyncio.to_thread(activate_bundled_snapshot)
            canonical = result.get("canonical") or {}
            _logger.info(
                "Garmin bootstrap %s: %s active, %s canonical",
                result.get("status"),
                canonical.get("active_garmin_activities"),
                canonical.get("canonical_sessions"),
            )
            return
        except Exception as exc:
            _logger.warning(
                "Garmin bootstrap attempt %s/6 failed: %s",
                attempt,
                exc,
            )
            if attempt < 6:
                await _asyncio.sleep(30)


async def _auto_stream_backfill():
    """
    Background task que descarga streams de Strava automáticamente.
    Corre 2 lotes de 50 cada 20 minutos hasta que no queden actividades pendientes.
    Respeta el rate limit de Strava (200 req/15min).
    Se detiene solo cuando remaining_without_streams = 0.
    """
    import httpx as _httpx
    _url = "http://localhost:8080/api/strava/backfill-streams?batch=50"
    _hdrs = {"X-Epoch-Key": os.environ.get("EPOCH_API_KEY", "")}
    _logger = logging.getLogger("mars_fit.auto_streams")
    await _asyncio.sleep(30)  # esperar que el servidor arranque completamente

    while True:
        try:
            remaining = 1  # asumir que hay trabajo al inicio del ciclo
            for _ in range(2):  # 2 lotes por ciclo
                async with _httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(_url, headers=_hdrs)
                    data = resp.json()
                    remaining = data.get("remaining_without_streams", 0)
                    total = data.get("total_with_streams", 0)
                    _logger.info(
                        f"Auto stream backfill: {total} con streams, "
                        f"{remaining} pendientes"
                    )
                if remaining == 0:
                    break
                await _asyncio.sleep(15)  # pausa entre lotes del mismo ciclo

            if remaining == 0:
                # Loguear reporte final de cobertura
                try:
                    async with _httpx.AsyncClient(timeout=30) as client:
                        rep = await client.get("http://localhost:8080/api/strava/stream-completeness")
                        d = rep.json()
                        _logger.info(
                            f"✅ Auto stream backfill COMPLETO — "
                            f"{d.get('with_streams',0)}/{d.get('total_activities',0)} actividades "
                            f"({d.get('pct_complete',0)}%) · "
                            f"HR:{d.get('avg_hr_coverage',0):.0%} "
                            f"GPS:{d.get('avg_gps_coverage',0):.0%} "
                            f"CAD:{d.get('avg_cadence_coverage',0):.0%} "
                            f"POW:{d.get('avg_power_coverage',0):.0%}"
                        )
                except Exception as e_rep:
                    _logger.info(f"✅ Auto stream backfill completado — reporte: {e_rep}")
                break

        except Exception as e:
            _logger.warning(f"Auto stream backfill error (reintentando en 5min): {e}")

        await _asyncio.sleep(1200)  # 20 minutos entre ciclos


async def _auto_activity_sync():
    """
    v6.5.3: red de seguridad del webhook — el webhook de Strava no siempre
    entrega eventos (se detectó actividad del 9-jun ausente). Sync ligero de
    1 página cada hora (~24 req Strava/día) + transform de lo nuevo, para que
    la actividad aparezca poco después de terminar de entrenar.
    """
    import httpx as _httpx
    _hdrs = {"X-Epoch-Key": os.environ.get("EPOCH_API_KEY", "")}
    _logger = logging.getLogger("epoch.auto_sync")
    await _asyncio.sleep(120)  # no competir con el arranque
    while True:
        try:
            async with _httpx.AsyncClient(timeout=120) as client:
                r = await client.post("http://localhost:8080/api/strava/sync-now?pages=1&force=true", headers=_hdrs)
                d = r.json()
                ingested = d.get("ingested", 0)
                if ingested:
                    await client.post("http://localhost:8080/api/strava/transform?batches=1", headers=_hdrs)
                    _logger.info(f"Auto-sync: +{ingested} actividades nuevas transformadas")
        except Exception as e:
            _logger.warning(f"Auto-sync error (siguiente intento en 1h): {e}")
        await _asyncio.sleep(3600)  # 1 hora


@app.on_event("startup")
async def _start_auto_stream_backfill():
    """Launch Garmin activation, stream backfill and recent Strava sync."""
    _asyncio.create_task(_auto_activate_latest_garmin())
    _asyncio.create_task(_auto_stream_backfill())
    _asyncio.create_task(_auto_activity_sync())

try:
    from strava.webhook import router as strava_router
    app.include_router(strava_router)
    print("✅ Strava router cargado OK")
except Exception as _strava_err:
    import traceback
    print(f"❌ ERROR Strava router: {_strava_err}")
    traceback.print_exc()

try:
    from routers.admin import router as admin_router
    app.include_router(admin_router)
    print("✅ Admin router cargado OK")
except Exception as _admin_err:
    import traceback
    print(f"❌ ERROR Admin router: {_admin_err}")
    traceback.print_exc()

try:
    from routers.capabilities import router as capabilities_router
    app.include_router(capabilities_router)
    print("✅ Capabilities router cargado OK")
except Exception as _cap_err:
    import traceback
    print(f"❌ ERROR Capabilities router: {_cap_err}")
    traceback.print_exc()

try:
    from routers.data_entry import router as data_entry_router
    app.include_router(data_entry_router)
    print("✅ Data entry router cargado OK")
except Exception as _de_err:
    import traceback
    print(f"❌ ERROR Data entry router: {_de_err}")
    traceback.print_exc()

try:
    from routers.activities import router as activities_router
    app.include_router(activities_router)
    print("✅ Activities router cargado OK")
except Exception as _act_err:
    import traceback
    print(f"❌ ERROR Activities router: {_act_err}")
    traceback.print_exc()

try:
    from routers.timeline import router as timeline_router
    app.include_router(timeline_router)
    print("✅ Timeline router cargado OK")
except Exception as _tl_err:
    import traceback
    print(f"❌ ERROR Timeline router: {_tl_err}")
    traceback.print_exc()

try:
    from routers.gpt_analytics import router as gpt_analytics_router
    app.include_router(gpt_analytics_router)
    print("✅ GPT Analytics router cargado OK")
except Exception as _ga_err:
    import traceback
    print(f"❌ ERROR GPT Analytics router: {_ga_err}")
    traceback.print_exc()

try:
    from routers.gpt_environment import router as gpt_environment_router
    app.include_router(gpt_environment_router)
    print("✅ GPT Environment router cargado OK")
except Exception as _ge_err:
    import traceback
    print(f"❌ ERROR GPT Environment router: {_ge_err}")
    traceback.print_exc()

# Bloque A: routers/gpt_history.py ELIMINADO — duplicaba al 100% endpoints de
# gpt_analytics (registrado antes) y jamás sirvió tráfico. Recuperable en Git.

try:
    from routers.gpt_training_context import router as gpt_training_context_router
    app.include_router(gpt_training_context_router)
    print("✅ GPT Training Context router cargado OK")
except Exception as _gtc_err:
    import traceback
    print(f"❌ ERROR GPT Training Context router: {_gtc_err}")
    traceback.print_exc()

try:
    from routers.workout_identity import router as workout_identity_router
    app.include_router(workout_identity_router)
    print("✅ Workout Identity router cargado OK")
except Exception as _wi_err:
    import traceback
    print(f"❌ ERROR Workout Identity router: {_wi_err}")
    traceback.print_exc()

try:
    from routers.plan_vivo import router as plan_vivo_router
    app.include_router(plan_vivo_router)
    print("✅ Plan Vivo router cargado OK")
except Exception as _pv_err:
    import traceback
    print(f"❌ ERROR Plan Vivo router: {_pv_err}")
    traceback.print_exc()

try:
    from routers.plan_builder import router as plan_builder_router
    app.include_router(plan_builder_router)
    print("✅ Plan Builder router cargado OK")
except Exception as _pb_err:
    import traceback
    print(f"❌ ERROR Plan Builder router: {_pb_err}")
    traceback.print_exc()

# Bloque A: routers/gpt_coaching.py ELIMINADO — duplicaba al 100% endpoints de
# gpt_analytics (registrado antes) y jamás sirvió tráfico. Recuperable en Git.

# Bloque A: routers/gpt_patterns.py ELIMINADO — duplicaba al 100% endpoints de
# gpt_analytics (registrado antes) y jamás sirvió tráfico. Recuperable en Git.

# Bloque A: routers/gpt_dashboard.py ELIMINADO — duplicaba al 100% endpoints de
# gpt_analytics (registrado antes) y jamás sirvió tráfico. Recuperable en Git.

# TD-010A: DARK_CSS y DARK_NAV eliminados — eran código muerto.
# El CSS del SPA vive en templates/app.html.

# ── Bloque A (seguridad) ─────────────────────────────────────────────────────
# CORS restringido: la PWA es same-origin; solo se permiten los orígenes propios.
_ALLOWED_ORIGINS = [
    "https://mars-fit-analyzer-production.up.railway.app",
    "https://app.useepoch.app",
    "http://localhost:8000",
]
app.add_middleware(CORSMiddleware, allow_origins=_ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])

# Auth por token en TODA escritura (POST/PUT/DELETE/PATCH).
#  · La llave vive en la env var EPOCH_API_KEY (configurar en Railway).
#  · El frontend la manda como X-Epoch-Key (se guarda una vez en Perfil).
#  · Exento: el webhook de Strava (Strava no puede mandar nuestra llave;
#    tiene su propia verificación de subscripción).
#  · Si la env var NO está configurada, se permite con warning — para que el
#    deploy no se bloquee antes de configurarla. Configúrala y queda armado.
import time as _sec_time
from collections import deque as _sec_deque
from fastapi.responses import JSONResponse as _SecJSON

_EPOCH_API_KEY = os.environ.get("EPOCH_API_KEY", "")
_AUTH_EXEMPT = ("/api/strava/webhook",)
# GETs que MODIFICAN estado (externo o interno) — también exigen llave.
# /api/strava/authorize y /callback quedan libres: son el flujo OAuth de
# Strava (el navegador llega por redirect y no puede mandar nuestra header).
_PROTECTED_GETS = ("/api/strava/register-webhook",
                   "/api/strava/dedup-diagnosis")
_RATE_BUCKETS: dict = {}
_RATE_MAX_PER_MIN = 240
_UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # 30 MB — un FIT grande pesa <5 MB

_warned_no_key = False


@app.middleware("http")
async def _epoch_security(request, call_next):
    global _warned_no_key
    # Rate limit por IP (ventana deslizante de 60 s, en memoria)
    ip = (request.headers.get("x-forwarded-for", "") or
          (request.client.host if request.client else "?")).split(",")[0].strip()
    now = _sec_time.monotonic()
    dq = _RATE_BUCKETS.get(ip)
    if dq is None:
        if len(_RATE_BUCKETS) > 10000:
            _RATE_BUCKETS.clear()
        dq = _RATE_BUCKETS.setdefault(ip, _sec_deque())
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= _RATE_MAX_PER_MIN:
        return _SecJSON({"detail": "Too many requests — slow down."}, status_code=429)
    dq.append(now)

    _is_protected_get = (request.method == "GET" and
                         any(request.url.path.startswith(p) for p in _PROTECTED_GETS))
    if request.method in ("POST", "PUT", "DELETE", "PATCH") or _is_protected_get:
        path = request.url.path
        # Límite de subida
        try:
            cl = int(request.headers.get("content-length") or 0)
        except ValueError:
            cl = 0
        if cl > _UPLOAD_MAX_BYTES:
            return _SecJSON({"detail": "Upload too large (max 30 MB)."}, status_code=413)
        # Auth en escrituras
        if not any(path.startswith(p) for p in _AUTH_EXEMPT):
            if _EPOCH_API_KEY:
                if request.headers.get("x-epoch-key") != _EPOCH_API_KEY:
                    return _SecJSON({"detail": "Missing or invalid X-Epoch-Key. "
                                               "Set your access key in Profile."},
                                    status_code=401)
            elif not _warned_no_key:
                _warned_no_key = True
                logger.warning("EPOCH_API_KEY not set — write endpoints are "
                               "UNPROTECTED. Set it in Railway variables.")
    return await call_next(request)

import pathlib as _pathlib_static
_static_dir = _pathlib_static.Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── PWA ───────────────────────────────────────────────────────────────────────

# WeightIn, WellnessIn, FuerzaIn → shared/models.py (TD-010A)


@app.get("/manifest.json")
def pwa_manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "Epoch",
        "short_name": "Epoch",
        "description": "Tu plataforma de fitness personal — conoce tu cuerpo, entrena mejor",
        "start_url": "/home",
        "display": "standalone",
        "background_color": "#0F1115",
        "theme_color": "#4A1C6B",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ],
        "shortcuts": [
            {"name": "Home", "url": "/home", "description": "Main dashboard"},
            {"name": "Activities", "url": "/activities", "description": "View all sessions"},
            {"name": "Wellness", "url": "/wellness", "description": "Log recovery"},
            {"name": "Strength", "url": "/fuerza", "description": "Log strength"}
        ]
    })


@app.get("/sw.js")
def service_worker():
    from fastapi.responses import Response
    sw_code = """const BUILD='20260612.3';
const CACHE='epoch-shell-'+BUILD;
const HOME='/home?build='+BUILD;
const SHELL=[
  HOME,
  '/static/app.css?v='+BUILD,
  '/static/config.js?v='+BUILD,
  '/static/utils.js?v='+BUILD,
  '/static/api.js?v='+BUILD,
  '/static/app.js?v='+BUILD
];

async function cacheValid(cache,request,response){
  if(!response || !response.ok)return response;
  const path=new URL(request.url).pathname;
  const type=(response.headers.get('content-type')||'').toLowerCase();
  if(path.endsWith('.css') && !type.includes('text/css'))return response;
  if(path.endsWith('.js') && !type.includes('javascript'))return response;
  await cache.put(request,response.clone());
  return response;
}

self.addEventListener('install',event=>{
  event.waitUntil(
    caches.open(CACHE).then(cache=>
      Promise.all(SHELL.map(url=>
        fetch(url,{cache:'reload'})
          .then(response=>cacheValid(cache,new Request(url),response))
          .catch(()=>null)
      ))
    ).then(()=>self.skipWaiting())
  );
});

self.addEventListener('activate',event=>{
  event.waitUntil(
    caches.keys()
      .then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key))))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener('fetch',event=>{
  const request=event.request;
  if(request.method!=='GET')return;
  const url=new URL(request.url);
  if(url.origin!==self.location.origin)return;
  if(url.pathname.startsWith('/api/') ||
     url.pathname.startsWith('/gpt/') ||
     url.pathname.startsWith('/admin/') ||
     url.pathname.includes('/analyze-fit'))return;

  if(request.mode==='navigate'){
    event.respondWith(
      fetch(request,{cache:'no-store'})
        .then(async response=>{
          if(response.ok){
            const cache=await caches.open(CACHE);
            await cache.put(HOME,response.clone());
          }
          return response;
        })
        .catch(()=>caches.match(HOME))
    );
    return;
  }

  if(url.pathname.startsWith('/static/')){
    event.respondWith(
      caches.open(CACHE).then(cache=>
        fetch(request,{cache:'no-store'})
          .then(response=>cacheValid(cache,request,response))
          .catch(()=>cache.match(request))
      )
    );
  }
});"""
    return Response(
        content=sw_code,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/icon-192.png")
def icon_192():
    from fastapi.responses import Response
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (192,192), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    # Epoch: fondo morado oscuro #0F1115 con letra E en #4A1C6B → glow effect
    draw.rounded_rectangle([0,0,191,191], radius=32, fill=(15,17,21,255))
    draw.rounded_rectangle([6,6,185,185], radius=28, fill=(74,28,107,255))
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 120)
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0),"E",font=font)
    tw,th = bbox[2]-bbox[0],bbox[3]-bbox[1]
    draw.text(((192-tw)//2-bbox[0]+2,(192-th)//2-bbox[1]),"E",font=font,fill=(255,255,255,255))
    buf=io.BytesIO(); img.save(buf,"PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control":"public,max-age=86400"})


@app.get("/icon-512.png")
def icon_512():
    from fastapi.responses import Response
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (512,512), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0,0,511,511], radius=80, fill=(15,17,21,255))
    draw.rounded_rectangle([16,16,495,495], radius=70, fill=(74,28,107,255))
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 320)
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0),"E",font=font)
    tw,th = bbox[2]-bbox[0],bbox[3]-bbox[1]
    draw.text(((512-tw)//2-bbox[0]+5,(512-th)//2-bbox[1]),"E",font=font,fill=(255,255,255,255))
    buf=io.BytesIO(); img.save(buf,"PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control":"public,max-age=86400"})

# TD-010A: HTML_UPLOAD extraído a templates/upload.html
import pathlib as _pathlib_upload
HTML_UPLOAD = (_pathlib_upload.Path(__file__).parent / 'templates' / 'upload.html').read_text(encoding='utf-8')


@app.get("/")
def root():
    return RedirectResponse(url="/home", status_code=302)

@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    return HTML_UPLOAD


@app.get("/api")
def api_status():
    return {"status":"ok","service":"Epoch API","version":"6.4",
            "db": "connected" if get_db() else "in-memory"}


@app.get("/health")
def health():
    conn = get_db()
    db_ok = False
    db_detail = "configured but unavailable" if DATABASE_URL else "no DATABASE_URL"
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sessions_clean_compat")
                count = cur.fetchone()[0]
            db_ok = True
            db_detail = f"{count} sesiones"
        except Exception as e:
            db_detail = str(e)
    return {
        "api": "ok",
        "db": "ok" if db_ok else "error",
        "db_detail": db_detail,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/admin/healthcheck")
def admin_healthcheck():
    """v6.4: estado simple — healthy / degraded / broken."""
    checks = {}
    # DB Railway
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        checks["db"] = True
    except Exception:
        checks["db"] = False
    # Endpoints críticos del SPA (funciones internas, no HTTP)
    for name, path in [("dashboard", "/gpt/dashboard"), ("capacidades", "/gpt/capacidades")]:
        checks[name] = checks["db"]  # dependen de DB; sin DB cuentan como caídos
    ok = sum(1 for v in checks.values() if v)
    total = len(checks)
    status = "healthy" if ok == total else "broken" if ok == 0 else "degraded"
    return {"status": status, "checks": checks, "version": "6.4"}

# ─────────────────────────────────────────────────────────────────────────────
# /dashboard  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/analyze-fit")
async def analyze_fit(file: UploadFile = File(...), include_records: bool = Query(False)):
    logger.info(f"UPLOAD start filename={file.filename}")
    try:
        # Bloque A: lectura ACOTADA — nunca se carga más del límite+1 en memoria.
        # (Content-Length es spoofeable; esto limita el consumo real, no la promesa.)
        content = await file.read(_UPLOAD_MAX_BYTES + 1)
        if len(content) > _UPLOAD_MAX_BYTES:
            raise HTTPException(413, "File too large (max 30 MB).")
        filename = (file.filename or "").lower()
        fit_bytes = extract_fit_from_zip(content) if filename.endswith(".zip") else content
        result = parse_fit(fit_bytes, include_records=True)
        sid = str(uuid.uuid4())[:8]
        store_session(sid, {"session_id":sid,"filename":file.filename,
                              "uploaded_at":datetime.now(timezone.utc).isoformat(),"result":result})
        # Persist to DB
        # Compute SHA256 hash of file for reliable duplicate detection
        import hashlib
        file_hash = hashlib.sha256(fit_bytes).hexdigest()

        conn = get_db()
        duplicate_sid = None
        db_saved = False
        db_error = None
        if conn:
            try:
                # Check for duplicate before saving
                s = result.get("session", {})
                duplicate_sid = check_duplicate_session(
                    conn,
                    s.get("start_time"),
                    s.get("duration_s"),
                    s.get("distance_km"),
                    file_hash=file_hash
                )
                if not duplicate_sid:
                    save_session_db(conn, sid, file.filename, result, file_hash=file_hash)
                    records = result.get("records", [])
                    if records:
                        save_records_db(conn, sid, records)
                    # Detect achievements
                    new_achievements = detect_and_save_achievements(conn, sid, result)
                    if new_achievements:
                        result["achievements"] = new_achievements
                    db_saved = True
                else:
                    logger.info(f"DUPLICATE detected: {file.filename} matches existing {duplicate_sid}")
                    db_saved = True
            except Exception as e:
                db_error = str(e)
                logger.error(f"DB save error filename={file.filename} error={e}")
        logger.info(f"UPLOAD ok session_id={sid} filename={file.filename} duplicate={duplicate_sid}")
        r = {k:v for k,v in result.items() if k != "records"}
        achievements = result.get("achievements", [])
        if duplicate_sid:
            return {"session_id": duplicate_sid,
                    "message": f"⚠️ This activity was already uploaded (session_id: {duplicate_sid}). Opening existing session.",
                    "charts_url": f"/charts/{duplicate_sid}",
                    "duplicate": True,
                    "persisted": db_saved,
                    "storage": "postgres" if db_saved else "memory",
                    "achievements": [],
                    **r}
        message = (
            f"✅ Guardado permanentemente. Pasa el session_id '{sid}' al GPT."
            if db_saved else
            f"⚠️ Analyzed, but NOT stored permanently because the database is unavailable. Temporary session_id: '{sid}'."
        )
        return {"session_id":sid,
                "message": message,
                "charts_url":f"/charts/{sid}",
                "duplicate": False,
                "persisted": db_saved,
                "storage": "postgres" if db_saved else "memory",
                "db_error": db_error,
                "achievements": achievements,
                **r}
    except Exception as e:
        logger.error(f"UPLOAD error filename={file.filename} error={e}")
        raise HTTPException(500, str(e))


@app.get("/result/{session_id}")
def get_result(session_id: str):
    # Try memory first
    entry = RESULTS_STORE.get(session_id)
    if entry:
        r = {k:v for k,v in entry["result"].items() if k != "records"}
        return r
    # Try DB
    conn = get_db()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT result_json FROM sessions_clean_compat WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    return {k:v for k,v in data.items() if k != "records"}
        except Exception as e:
            logger.info(f"DB read error: {e}")
    raise HTTPException(404, f"session_id '{session_id}' no encontrado. Puede haber expirado — vuelve a subir el archivo.")


# GET /routes                → routers/activities.py (TD-010A)
# GET /route/{route_id}      → routers/activities.py (TD-010A)
# GET /route/{route_id}/matched → routers/activities.py (TD-010A)
# def _list_sessions_with_telemetry → routers/activities.py (TD-010A)
# GET /sessions              → routers/activities.py (TD-010A)
# GET /charts/{session_id}   → routers/activities.py (TD-010A)


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 1 — Endpoints nuevos: post_session, gear, maintenance, recovery
# ═══════════════════════════════════════════════════════════════════════════════

# PostSessionIn, GearIn, GearUpdate, MaintenanceIn, RecoveryIn → shared/models.py (TD-010A)


# POST /post-session/{session_id} → routers/data_entry.py (TD-010A)
# GET  /post-session/{session_id} → routers/data_entry.py (TD-010A)


# PUT  /gear/{gear_id}      → routers/data_entry.py (TD-010A)
# GET  /gear/alerts         → routers/data_entry.py (TD-010A)
# GET  /maintenance         → routers/data_entry.py (TD-010A)
# POST /recovery            → routers/data_entry.py (TD-010A)
# GET  /recovery            → routers/data_entry.py (TD-010A)


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 2 — Endpoints: stats/monthly, stats/efficiency, sessions/recent
# ═══════════════════════════════════════════════════════════════════════════════


# GET /stats/yearly         → routers/activities.py (TD-010A)
# GET /stats/records        → routers/activities.py (TD-010A)
# GET /stats/monthly        → routers/activities.py (TD-010A)
# GET /stats/efficiency     → routers/activities.py (TD-010A)
# GET /sessions/recent      → routers/activities.py (TD-010A)


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 3 — Pantalla Actividades
# ═══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# E26B — GOAL REGISTRY
# ADR-011: el usuario define sus metas; el sistema solo sugiere.
# ═══════════════════════════════════════════════════════════════════════════════

# GoalCreate, GoalUpdate → shared/models.py (TD-010A)


# GET/POST/PUT/DELETE /gpt/goals → routers/data_entry.py (TD-010A)


# /activities  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────




# ── gpt_analytics endpoints → routers/gpt_analytics.py (TD-010A) ─────────────
# /gpt/month-summary, /gpt/efficiency-trend, /gpt/zones-summary,
# /gpt/cadence-trend, /gpt/weekly-report, /gpt/adaptive-coach,
# /gpt/fueling-log, /gpt/gel-tests, /gpt/weight-trend,
# /gpt/tests (POST+GET), /gpt/dashboard, /gpt/historical-progress,
# /gpt/month-compare, /gpt/fitness-timeline, /gpt/athletic-history,
# /gpt/calendar-heatmap, /gpt/trends, /gpt/rebuild-snapshots,
# /gpt/environment-summary, /gpt/athletic-status,
# /gpt/correlaciones, /gpt/correlations, /gpt/tendencia, /gpt/mars-context


# GET /gpt/session-analysis/{session_id} → routers/activities.py (TD-010A)




# GET /api/fuerza-records   → routers/data_entry.py (TD-010A)
# GET /api/wellness-records  → routers/data_entry.py (TD-010A)



# GET /gpt/matched-rides/{session_id} → routers/activities.py (TD-010A)
# GET /gpt/route-history               → routers/activities.py (TD-010A)


# APP_FULL_HTML extraído a templates/app.html — TD-010A
import pathlib as _pathlib
APP_FULL_HTML = _pathlib.Path(__file__).parent / "templates" / "app.html"
APP_FULL_HTML = APP_FULL_HTML.read_text(encoding="utf-8")


def _full_app_response():
    return HTMLResponse(APP_FULL_HTML)

# Clean direct routes — no override needed
@app.get("/sesion", response_class=HTMLResponse)
@app.get("/plan", response_class=HTMLResponse)
@app.get("/app", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/activities", response_class=HTMLResponse)
@app.get("/gear", response_class=HTMLResponse)
@app.get("/calendar", response_class=HTMLResponse)
@app.get("/performance", response_class=HTMLResponse)
@app.get("/fuerza", response_class=HTMLResponse)
@app.get("/wellness", response_class=HTMLResponse)
@app.get("/progress", response_class=HTMLResponse)
@app.get("/eficiencia", response_class=HTMLResponse)
@app.get("/correlaciones", response_class=HTMLResponse)
@app.get("/nutricion", response_class=HTMLResponse)
@app.get("/perfil", response_class=HTMLResponse)
@app.get("/coach", response_class=HTMLResponse)
@app.get("/capacidades", response_class=HTMLResponse)
@app.get("/metas", response_class=HTMLResponse)
def serve_app():
    return HTMLResponse(APP_FULL_HTML)


# ── V8 — Legales: aviso de privacidad, términos y descargo (borradores) ──────

LEGAL_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Epoch — Legales</title><link rel="stylesheet" href="/static/app.css">
<style>body{overflow:auto!important}.legal{max-width:640px;margin:0 auto;padding:24px 18px 60px}
.legal h1{font-size:24px;margin-bottom:4px}.legal h2{font-size:15px;margin:22px 0 8px;color:#fb923c}
.legal p,.legal li{font-size:13px;line-height:1.65;color:#c8cbd2;margin-bottom:8px}
.legal .tag{display:inline-block;background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.35);border-radius:8px;padding:2px 10px;font-size:10px;font-weight:800;margin-bottom:14px}
.legal a{color:#4a9eff}</style></head><body><div class="legal">
<h1>Legales</h1>
<span class="tag">BORRADOR — pendiente de revisión por un profesional legal antes de abrir a más usuarios</span>

<h2>1. Aviso de Privacidad</h2>
<p>Epoch procesa datos de actividad física (frecuencia cardiaca, GPS, cadencia, potencia,
temperatura, peso, sueño y bienestar) que tú decides conectar desde servicios como Strava
o subir manualmente (archivos FIT de Garmin).</p>
<ul>
<li>Tus datos se usan exclusivamente para generar tus propios análisis dentro de Epoch.</li>
<li>No se venden, no se comparten con terceros y no se usan para publicidad.</li>
<li>Se almacenan en infraestructura de Railway y Supabase (proveedores de hosting/base de datos).</li>
<li>Puedes solicitar la eliminación de tus datos en cualquier momento.</li>
<li>Los datos de salud son sensibles: al usar Epoch aceptas su procesamiento para estos fines.</li>
</ul>

<h2>2. Descargo de responsabilidad (métricas y análisis)</h2>
<p>Las lecturas de Epoch — capacidades, zonas, eficiencia, deriva cardiaca, calidad de
intervalos, readiness, proyecciones y sugerencias del Coach — son <b>estimaciones
basadas en heurísticas y en los datos disponibles</b>. No son mediciones de laboratorio
ni verdades absolutas.</p>
<ul>
<li>Pueden existir errores: sensores imprecisos, datos faltantes o supuestos del modelo.</li>
<li>Por eso cada lectura declara su <b>confianza</b> (alta/media/baja) y <b>qué falta</b> para leer mejor.</li>
<li>Factores externos (calor, frío, viento, altitud, hidratación, estrés) influyen en los
resultados; Epoch los incorpora como contexto cuando los conoce, pero no siempre los conoce.</li>
<li>Verifica la información antes de tomar decisiones importantes basadas en ella.</li>
</ul>

<h2>3. No es consejo médico</h2>
<p>Epoch no es un dispositivo médico ni sustituye la opinión de profesionales de la salud
o del deporte. Las sugerencias son orientación de entrenamiento, no prescripción. Ante
cualquier síntoma, molestia o duda de salud, consulta a un profesional.</p>

<h2>4. Fuentes y métodos</h2>
<p>Para quien quiera cuestionar el método (bienvenido): los datos provienen de Strava API
y archivos FIT de Garmin; los análisis se calculan con reglas declaradas y verificables
(zonas por frecuencia cardiaca, comparaciones por ruta + intención, deriva cardiaca por
mitades de sesión, bloques desde laps). Epoch prefiere decir "no lo sé" antes que inventar.</p>

<h2>5. Términos de uso (resumen)</h2>
<ul>
<li>Epoch se ofrece "tal cual", sin garantías de disponibilidad ni exactitud.</li>
<li>Eres responsable de las decisiones que tomes con base en la información.</li>
<li>Estos textos cambiarán antes de abrir Epoch a más usuarios, con revisión legal formal
(aviso de privacidad conforme a la LFPDPPP en México y/o GDPR si aplica).</li>
</ul>

<p style="margin-top:24px"><a href="/perfil">← Volver a Epoch</a></p>
</div></body></html>"""


@app.get("/legal", response_class=HTMLResponse)
def legal_page():
    return HTMLResponse(LEGAL_HTML)
