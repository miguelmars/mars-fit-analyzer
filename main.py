"""
FIT Analyzer API — Mars Edition v4.0
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
from fastapi.responses import HTMLResponse
from typing import Optional, List
from pydantic import BaseModel, Field
import tempfile, os, zipfile, math, statistics, uuid, json
from datetime import datetime, timezone, timedelta
import logging
from logging.handlers import RotatingFileHandler

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = "logs"
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
    {"zone": 1, "name": "Z1 Recuperación", "bpm_low": 0,   "bpm_high": 108},
    {"zone": 2, "name": "Z2 Aeróbico",     "bpm_low": 134, "bpm_high": 150},
    {"zone": 3, "name": "Z3 Tempo",        "bpm_low": 151, "bpm_high": 160},
    {"zone": 4, "name": "Z4 Umbral",       "bpm_low": 161, "bpm_high": 168},
    {"zone": 5, "name": "Z5 Máximo",       "bpm_low": 169, "bpm_high": 999},
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
                raise HTTPException(400, "El ZIP no contiene ningún .fit")
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
                insights["hr_drift_note"] = f"Deriva cardíaca de +{drift} bpm entre inicio y final — posible acumulación de fatiga o calor"
            elif drift < -5:
                insights["hr_drift_note"] = f"FC bajó {abs(drift)} bpm hacia el final — buena recuperación o descenso de intensidad"
            else:
                insights["hr_drift_note"] = f"FC estable a lo largo de la sesión (deriva de {drift} bpm)"

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
            "note": f"Mejor ventana aeróbica Z2: {round(best_window_spd,1)} km/h en minuto ~{best_window_start//60}"
        }

    # Cadence drops (likely traffic/stops)
    cad_vals = [r.get("cadence_rpm") or 0 for r in records]
    drops = sum(1 for i in range(1, len(cad_vals))
                if cad_vals[i-1] > 40 and cad_vals[i] < 5)
    if drops > 0:
        insights["traffic_stops_approx"] = drops
        insights["traffic_note"] = f"~{drops} paradas o interrupciones detectadas (caídas de cadencia a 0)"

    # Altitude impact
    alts = [r.get("altitude_m") for r in records if r.get("altitude_m")]
    if alts:
        alt_range = max(alts) - min(alts)
        insights["altitude_range_m"] = round(alt_range, 0)
        if alt_range > 80:
            insights["altitude_note"] = f"Desnivel dinámico de {round(alt_range)}m — subidas y bajadas afectan velocidad y FC"

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
            "zone_model":     "Zonas oficiales Mars por bpm",
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
                    "Usar derived_insights para contextualizar sin que Mars tenga que explicar nada.",
                    "traffic_stops_approx indica paradas por tráfico — no interpretar como fatiga.",
                    "hr_drift_note ya tiene la interpretación de la deriva cardíaca.",
                    "best_aerobic_window es la mejor ventana Z2 real de la sesión.",
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

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="4.0")

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

try:
    from routers.gpt_history import router as gpt_history_router
    app.include_router(gpt_history_router)
    print("✅ GPT History router cargado OK")
except Exception as _gh_err:
    import traceback
    print(f"❌ ERROR GPT History router: {_gh_err}")
    traceback.print_exc()

try:
    from routers.gpt_coaching import router as gpt_coaching_router
    app.include_router(gpt_coaching_router)
    print("✅ GPT Coaching router cargado OK")
except Exception as _gc_err:
    import traceback
    print(f"❌ ERROR GPT Coaching router: {_gc_err}")
    traceback.print_exc()

try:
    from routers.gpt_patterns import router as gpt_patterns_router
    app.include_router(gpt_patterns_router)
    print("✅ GPT Patterns router cargado OK")
except Exception as _gp_err:
    import traceback
    print(f"❌ ERROR GPT Patterns router: {_gp_err}")
    traceback.print_exc()

try:
    from routers.gpt_dashboard import router as gpt_dashboard_router
    app.include_router(gpt_dashboard_router)
    print("✅ GPT Dashboard router cargado OK")
except Exception as _gd_err:
    import traceback
    print(f"❌ ERROR GPT Dashboard router: {_gd_err}")
    traceback.print_exc()

DARK_CSS = """
:root{
  --bg:#08090b;
  --bg2:#0f1115;
  --surface:#15171c;
  --surface2:#1c1f26;
  --surface3:#242832;
  --stroke:rgba(255,255,255,.08);
  --stroke2:rgba(255,255,255,.15);
  --text:#f7f7f4;
  --muted:#8e95a3;
  --muted2:#5f6673;
  --bike:#e8593c;
  --fuerza:#c8f135;
  --wellness:#4a9eff;
  --stats:#a78bfa;
  --coach:#f59e0b;
  --green:#3dd68c;
  --red:#e8593c;
  --yellow:#f59e0b;
  --accent:#e8593c;
  --radius-xl:26px;
  --radius-lg:20px;
  --radius-md:14px;
  --shadow:0 18px 50px rgba(0,0,0,.4);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Inter','Segoe UI',sans-serif;min-height:100vh}
button,input,select,textarea{font-family:inherit}

/* ── Top nav bar ── */
.topnav{
  height:56px;
  background:rgba(15,17,21,.92);
  backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--stroke2);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;
  position:sticky;top:0;z-index:100;
}
.topnav-brand{display:flex;align-items:center;gap:10px}
.topnav-logo{width:34px;height:34px;border-radius:12px;background:var(--bike);
  display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:950;color:#08090b}
.topnav-title{font-size:15px;font-weight:800;letter-spacing:-.025em}
.topnav-sub{font-size:10px;color:var(--muted);font-weight:600}
.topnav-links{display:flex;gap:2px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.topnav-links::-webkit-scrollbar{display:none}
.topnav-link{padding:6px 12px;border-radius:10px;font-size:12px;font-weight:700;color:var(--muted);
  text-decoration:none;white-space:nowrap;transition:all .15s}
.topnav-link:hover{background:var(--surface2);color:var(--text)}
.topnav-link.active{background:var(--bike);color:#08090b}
.topnav-back{padding:7px 14px;border-radius:12px;background:rgba(255,255,255,.06);
  border:1px solid var(--stroke2);color:var(--text);font-size:12px;font-weight:700;
  text-decoration:none;display:flex;align-items:center;gap:5px}
@media(max-width:600px){
  .topnav-sub{display:none}
  .topnav-link{padding:5px 9px;font-size:11px}
}

/* ── Page layout ── */
.page{max-width:1040px;margin:0 auto;padding:24px 16px 40px}
.page-hdr{margin-bottom:20px}
.page-title{font-size:26px;font-weight:900;letter-spacing:-.04em;line-height:1.1;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted)}

/* ── Cards ── */
.card{background:linear-gradient(180deg,var(--surface2),var(--surface));
  border:1px solid var(--stroke);border-radius:var(--radius-lg);padding:18px;margin-bottom:14px}
.card-title{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:14px}
.card-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-hdr h3{font-size:14px;font-weight:800;letter-spacing:-.02em}
.card-hdr span{font-size:11px;color:var(--muted)}

/* ── KPI grids ── */
.kpi-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.kpi-grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px}
.kpi-grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
@media(max-width:700px){.kpi-grid-4{grid-template-columns:1fr 1fr}.kpi-grid-3{grid-template-columns:1fr 1fr}}
.kpi-box{background:var(--surface2);border:1px solid var(--stroke);border-radius:var(--radius-md);padding:14px}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;margin-bottom:6px}
.kpi-val{font-size:28px;font-weight:900;letter-spacing:-.04em;line-height:1;color:var(--text)}
.kpi-unit{font-size:11px;color:var(--muted);margin-top:2px}
.kpi-delta{font-size:12px;font-weight:700;margin-top:4px}
.delta-pos{color:var(--green)}
.delta-neg{color:var(--red)}

/* ── Tables ── */
.data-table{width:100%;border-collapse:collapse;font-size:13px}
.data-table th{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:700;text-align:left;padding:0 10px 10px 0;border-bottom:1px solid var(--stroke)}
.data-table td{padding:11px 10px 11px 0;border-bottom:1px solid var(--stroke)}
.data-table tr:last-child td{border-bottom:none}
.data-table tr:hover td{background:rgba(255,255,255,.03)}

/* ── Row items ── */
.row-item{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--stroke)}
.row-item:last-child{border-bottom:none}
.row-ico{width:40px;height:40px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
.row-main{flex:1;min-width:0}
.row-title{font-size:14px;font-weight:700}
.row-sub{font-size:11px;color:var(--muted);margin-top:2px}
.row-val{font-size:14px;font-weight:900;color:var(--accent);text-align:right}

/* ── Progress bars ── */
.prog-wrap{height:8px;background:var(--surface3);border-radius:4px;overflow:hidden;margin:6px 0}
.prog-fill{height:100%;border-radius:4px;transition:width .5s ease}
.prog-green{background:var(--green)}
.prog-yellow{background:var(--yellow)}
.prog-red{background:var(--red)}
.prog-bike{background:var(--bike)}
.prog-blue{background:var(--wellness)}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:800}
.badge-green{background:rgba(61,214,140,.15);color:var(--green);border:1px solid rgba(61,214,140,.3)}
.badge-red{background:rgba(232,89,60,.15);color:var(--red);border:1px solid rgba(232,89,60,.3)}
.badge-yellow{background:rgba(245,158,11,.15);color:var(--yellow);border:1px solid rgba(245,158,11,.3)}
.badge-blue{background:rgba(74,158,255,.15);color:var(--wellness);border:1px solid rgba(74,158,255,.3)}

/* ── Filters ── */
.filter-bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.filter-chip{padding:7px 14px;border-radius:20px;font-size:12px;font-weight:700;cursor:pointer;
  border:1px solid var(--stroke);color:var(--muted);background:var(--surface);transition:all .15s}
.filter-chip:hover{border-color:var(--stroke2);color:var(--text)}
.filter-chip.on{background:var(--bike);border-color:var(--bike);color:#08090b}
.filter-input{background:var(--surface2);border:1px solid var(--stroke);border-radius:12px;
  padding:9px 14px;font-size:13px;color:var(--text);outline:none;width:100%;margin-bottom:14px}
.filter-input:focus{border-color:var(--bike)}

/* ── Spinner ── */
.spinner{width:24px;height:24px;border:2px solid var(--stroke);border-top-color:var(--bike);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading{text-align:center;padding:50px;color:var(--muted);font-size:13px}

/* ── Calendar ── */
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:8px}
.cal-day-hdr{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);text-align:center;padding:6px 0}
.cal-cell{aspect-ratio:1;border-radius:10px;display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;
  background:var(--surface2);border:1px solid var(--stroke);color:var(--muted2)}
.cal-cell:hover{border-color:var(--stroke2);color:var(--text)}
.cal-cell.has-data{color:var(--text)}
.cal-cell.today{border-color:var(--bike);color:var(--bike)}
.cal-cell .dot{width:5px;height:5px;border-radius:50%;margin:1px auto 0;background:var(--bike)}
.c0{background:var(--surface2)}
.c1{background:rgba(232,89,60,.12);color:var(--text)}
.c2{background:rgba(232,89,60,.25);color:var(--text)}
.c3{background:rgba(232,89,60,.45);color:var(--text)}
.c4{background:rgba(232,89,60,.72);color:#fff}

/* ── Heatmap legend ── */
.hm-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);margin-top:8px}
.hm-swatch{width:14px;height:14px;border-radius:4px}

/* ── Gear bars ── */
.gear-card{background:var(--surface2);border:1px solid var(--stroke);border-radius:var(--radius-md);
  padding:16px;border-left:3px solid var(--stroke)}
.gear-card.warn-red{border-left-color:var(--red)}
.gear-card.warn-yellow{border-left-color:var(--yellow)}
.gear-card.warn-green{border-left-color:var(--green)}
.gear-name{font-size:14px;font-weight:800;margin-bottom:2px}
.gear-type{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px}
.gear-km{font-size:12px;font-weight:700;color:var(--text)}
.gear-limit{font-size:11px;color:var(--muted)}

/* ── Performance zone bars ── */
.zone-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--stroke)}
.zone-row:last-child{border-bottom:none}
.zone-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.zone-lbl{font-size:12px;color:var(--muted);flex:1;font-weight:600}
.zone-bar-wrap{flex:2;height:6px;background:var(--surface3);border-radius:3px;overflow:hidden}
.zone-bar-fill{height:100%;border-radius:3px}
.zone-time{font-size:11px;font-weight:800;color:var(--muted);width:52px;text-align:right}

/* ── Two-col layout ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}.three-col{grid-template-columns:1fr 1fr}}

/* ── Activity list ── */
.act-card{background:linear-gradient(180deg,var(--surface2),var(--surface));
  border:1px solid var(--stroke);border-radius:var(--radius-md);
  overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s;
  margin-bottom:10px;display:grid;grid-template-columns:4px 1fr}
.act-card:hover{transform:translateY(-1px);border-color:var(--stroke2)}
.act-stripe{background:var(--bike)}
.act-stripe.running{background:var(--green)}
.act-stripe.walking{background:var(--wellness)}
.act-stripe.training{background:var(--yellow)}
.act-body{padding:14px 16px;display:flex;align-items:center;gap:14px}
.act-info{flex:1;min-width:0}
.act-name{font-size:14px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.act-date{font-size:11px;color:var(--muted);margin-bottom:8px}
.act-metrics{display:flex;gap:16px;flex-wrap:wrap}
.act-metric-val{font-size:13px;font-weight:800;line-height:1}
.act-metric-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-top:1px}
.act-right{text-align:right;flex-shrink:0}
.act-sport-tag{font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
  padding:3px 8px;border-radius:6px;background:rgba(232,89,60,.15);color:var(--bike);margin-bottom:6px;display:inline-block}
.act-sport-tag.running{background:rgba(61,214,140,.15);color:var(--green)}
.act-sport-tag.walking{background:rgba(74,158,255,.15);color:var(--wellness)}
.act-link{font-size:11px;color:var(--muted);text-decoration:none;font-weight:700}
.act-link:hover{color:var(--bike)}

/* ── Pagination ── */
.pagination{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:20px}
.pag-btn{background:var(--surface2);border:1px solid var(--stroke);border-radius:10px;
  padding:8px 18px;font-size:13px;font-weight:700;color:var(--text);cursor:pointer;transition:all .15s}
.pag-btn:hover{border-color:var(--bike);color:var(--bike)}
.pag-btn:disabled{opacity:.3;cursor:not-allowed}
.pag-info{font-size:12px;color:var(--muted)}

/* ── Sidebar ── */
.layout-sidebar{display:grid;grid-template-columns:220px 1fr;gap:20px}
@media(max-width:800px){.layout-sidebar{grid-template-columns:1fr}}
.sidebar{background:var(--surface);border:1px solid var(--stroke);border-radius:var(--radius-lg);
  padding:16px;height:fit-content;position:sticky;top:72px}
@media(max-width:800px){.sidebar{position:static}}
.sidebar-section{margin-bottom:16px}
.sidebar-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:8px;display:block}
.sidebar-divider{height:1px;background:var(--stroke);margin:12px 0}
.sport-btn{width:100%;text-align:left;background:none;border:none;padding:8px 12px;border-radius:10px;
  cursor:pointer;font-size:13px;font-weight:700;color:var(--muted);display:flex;align-items:center;gap:10px;transition:all .15s}
.sport-btn:hover{background:var(--surface2);color:var(--text)}
.sport-btn.on{background:var(--bike);color:#08090b}
.sport-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sport-count{margin-left:auto;font-size:11px;opacity:.7}
"""

DARK_NAV = """
<nav class="topnav">
  <div class="topnav-brand">
    <div class="topnav-logo">M</div>
    <div>
      <div class="topnav-title">Bitácora</div>
      <div class="topnav-sub">Mars Training</div>
    </div>
  </div>
  <div class="topnav-links" id="topnav-links">
    <a href="/home" class="topnav-link" id="nav-home">⌂ Home</a>
    <a href="/activities" class="topnav-link" id="nav-activities">Actividades</a>
    <a href="/dashboard" class="topnav-link" id="nav-dashboard">Rutas</a>
    <a href="/calendar" class="topnav-link" id="nav-calendar">Calendario</a>
    <a href="/performance" class="topnav-link" id="nav-performance">Rendimiento</a>
    <a href="/gear" class="topnav-link" id="nav-gear">Equipo</a>
    <a href="/progress" class="topnav-link">Progreso</a>
  </div>
</nav>
<script>
(function(){
  var path = window.location.pathname;
  var map = {'/activities':'nav-activities','/dashboard':'nav-dashboard','/calendar':'nav-calendar','/performance':'nav-performance','/gear':'nav-gear'};
  var id = map[path];
  if(id){ var el = document.getElementById(id); if(el) el.classList.add('active'); }
})();
</script>
"""
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── PWA ───────────────────────────────────────────────────────────────────────

# WeightIn, WellnessIn, FuerzaIn → shared/models.py (TD-010A)


@app.get("/manifest.json")
def pwa_manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "Bitácora Mars",
        "short_name": "Bitácora",
        "description": "Sistema de análisis de entrenamiento ciclista",
        "start_url": "/home",
        "display": "standalone",
        "background_color": "#f5f3ef",
        "theme_color": "#1a1816",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ],
        "shortcuts": [
            {"name": "Subir FIT", "url": "/", "description": "Subir archivo de entrenamiento"},
            {"name": "Actividades", "url": "/activities", "description": "Ver todas las sesiones"},
            {"name": "Fuerza", "url": "/fuerza", "description": "Registrar fuerza"},
            {"name": "Wellness", "url": "/wellness", "description": "Registrar recuperación"}
        ]
    })


@app.get("/sw.js")
def service_worker():
    from fastapi.responses import Response
    sw_code = """const CACHE='bitacora-v17';
const PAGES=['/home','/dashboard','/activities','/gear','/calendar','/performance','/fuerza','/wellness','/progress'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(PAGES)).then(()=>self.skipWaiting()));});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  if(e.request.url.includes('/gpt/')||e.request.url.includes('/analyze-fit')||e.request.url.includes('/admin/'))return;
  e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(CACHE).then(ch=>ch.put(e.request,c));return r;}).catch(()=>caches.match(e.request)));
});"""
    return Response(content=sw_code, media_type="application/javascript")


@app.get("/icon-192.png")
def icon_192():
    from fastapi.responses import Response
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (192,192), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0,0,191,191], radius=32, fill=(232,89,60,255))
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0),"M",font=font)
    tw,th = bbox[2]-bbox[0],bbox[3]-bbox[1]
    draw.text(((192-tw)//2-bbox[0],(192-th)//2-bbox[1]+10),"M",font=font,fill=(255,255,255,255))
    buf=io.BytesIO(); img.save(buf,"PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control":"public,max-age=86400"})


@app.get("/icon-512.png")
def icon_512():
    from fastapi.responses import Response
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (512,512), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0,0,511,511], radius=80, fill=(232,89,60,255))
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 290)
    except: font = ImageFont.load_default()
    bbox = draw.textbbox((0,0),"M",font=font)
    tw,th = bbox[2]-bbox[0],bbox[3]-bbox[1]
    draw.text(((512-tw)//2-bbox[0],(512-th)//2-bbox[1]+25),"M",font=font,fill=(255,255,255,255))
    buf=io.BytesIO(); img.save(buf,"PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers={"Cache-Control":"public,max-age=86400"})

HTML_UPLOAD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#0d0d0d">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Subir FIT</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');
  :root{--bg:#0f0f0f;--surface:#1a1a1a;--border:#2a2a2a;--accent:#e8593c;--accent2:#f2a623;--text:#e8e6e0;--muted:#6b6b6b;--success:#3dd68c;--mono:'DM Mono',monospace;--sans:'DM Sans',sans-serif}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px 20px}
  .container{width:100%;max-width:420px}
  .header{margin-bottom:36px;text-align:center}
  .logo{font-family:var(--mono);font-size:11px;letter-spacing:.2em;color:var(--muted);text-transform:uppercase;margin-bottom:12px}
  .title{font-size:28px;font-weight:300;letter-spacing:-.02em;line-height:1.2}
  .title span{color:var(--accent);font-weight:500}
  .drop-zone{border:1.5px dashed var(--border);border-radius:16px;padding:48px 24px;text-align:center;cursor:pointer;position:relative;background:var(--surface)}
  .drop-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
  .drop-icon{width:48px;height:48px;margin:0 auto 16px;border-radius:12px;background:#2a1f1a;display:flex;align-items:center;justify-content:center}
  .drop-icon svg{width:24px;height:24px;stroke:var(--accent);fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
  .drop-label{font-size:15px;color:var(--text);margin-bottom:6px}
  .drop-hint{font-size:12px;color:var(--muted);font-family:var(--mono)}
  .file-selected{margin-top:20px;padding:14px 16px;background:#1f1f1f;border-radius:10px;border:1px solid var(--border);display:none;align-items:center;gap:12px}
  .file-selected.show{display:flex}
  .file-icon{width:36px;height:36px;background:#2a1f1a;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .file-icon svg{width:18px;height:18px;stroke:var(--accent2);fill:none;stroke-width:1.5;stroke-linecap:round}
  .file-info{flex:1;min-width:0}
  .file-name{font-family:var(--mono);font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .file-size{font-size:11px;color:var(--muted);margin-top:2px}
  .btn-upload{width:100%;margin-top:16px;padding:16px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-family:var(--sans);font-size:15px;font-weight:500;cursor:pointer;display:none}
  .btn-upload.show{display:block}
  .progress-wrap{margin-top:16px;display:none}
  .progress-wrap.show{display:block}
  .progress-bar-bg{height:3px;background:var(--border);border-radius:2px;overflow:hidden;margin-bottom:10px}
  .progress-bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:2px;width:0%;transition:width .3s}
  .progress-label{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
  .result-card{margin-top:20px;padding:24px;background:#111f17;border:1px solid #1e3d2a;border-radius:16px;display:none}
  .result-card.show{display:block}
  .result-label{font-family:var(--mono);font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--success);margin-bottom:12px}
  .session-id-box{background:#0d1a12;border:1px solid #1e3d2a;border-radius:10px;padding:16px;display:flex;align-items:center;gap:12px;cursor:pointer}
  .session-id-value{font-family:var(--mono);font-size:22px;font-weight:500;color:var(--success);letter-spacing:.08em;flex:1}
  .copy-btn{padding:8px 14px;background:#1e3d2a;border:none;border-radius:8px;font-family:var(--mono);font-size:11px;color:var(--success);cursor:pointer}
  .copy-btn.copied{color:#fff;background:var(--success)}
  .result-meta{margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .meta-item{background:#0d1a12;border-radius:8px;padding:10px 12px}
  .meta-key{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px}
  .meta-val{font-size:14px;font-weight:500;color:var(--text)}
  .gpt-hint{margin-top:16px;padding:12px 14px;background:#1a1a1a;border-radius:10px;border-left:3px solid var(--accent2)}
  .gpt-hint p{font-size:12px;color:var(--muted);line-height:1.5}
  .gpt-hint code{font-family:var(--mono);color:var(--accent2);font-size:12px}
  .charts-btn{display:block;width:100%;margin-top:10px;padding:12px;background:transparent;border:1px solid #1e3d2a;border-radius:10px;color:var(--success);font-family:var(--sans);font-size:13px;text-align:center;text-decoration:none}
  .error-card{margin-top:16px;padding:16px;background:#1f1212;border:1px solid #3d1e1e;border-radius:12px;display:none}
  .error-card.show{display:block}
  .error-card p{font-family:var(--mono);font-size:12px;color:#f07070;line-height:1.5}
  .reset-btn{margin-top:20px;width:100%;padding:12px;background:transparent;border:1px solid var(--border);border-radius:10px;color:var(--muted);font-family:var(--sans);font-size:13px;cursor:pointer;display:none}
  .reset-btn.show{display:block}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">Mars Fit Analyzer</div>
    <h1 class="title">Sube tu<br><span>entrenamiento</span></h1>
  </div>
  <div class="drop-zone" id="dropZone">
    <input type="file" id="fileInput" accept=".zip,.fit"/>
    <div class="drop-icon"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></div>
    <p class="drop-label">Toca para seleccionar archivo</p>
    <p class="drop-hint">.zip o .fit de Garmin Connect</p>
  </div>
  <div class="file-selected" id="fileSelected">
    <div class="file-icon"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>
    <div class="file-info"><div class="file-name" id="fileName">—</div><div class="file-size" id="fileSize">—</div></div>
  </div>
  <button class="btn-upload" id="btnUpload" onclick="uploadFile()">Analizar sesión →</button>
  <div class="progress-wrap" id="progressWrap">
    <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill"></div></div>
    <p class="progress-label" id="progressLabel">Procesando...</p>
  </div>
  <div class="result-card" id="resultCard">
    <div class="result-label">✓ Listo — copia este ID al GPT</div>
    <div class="session-id-box" onclick="copyId()">
      <div class="session-id-value" id="sessionIdValue">—</div>
      <button class="copy-btn" id="copyBtn">Copiar</button>
    </div>
    <div class="result-meta" id="resultMeta"></div>
    <div class="gpt-hint"><p>Pega esto al GPT:<br><code>Analiza mi sesión. session_id: <span id="hintId">—</span></code></p></div>
    <a class="charts-btn" id="chartsBtn" href="#" target="_blank">Ver gráficas →</a>
  </div>
  <div class="error-card" id="errorCard"><p id="errorMsg">Error</p></div>
  <button class="reset-btn" id="resetBtn" onclick="reset()">Subir otro archivo</button>
</div>
<script>
const API='';
let selectedFile=null;
const $=id=>document.getElementById(id);
function fmtDate(v){
  if(!v)return '—';
  try{
    const d=new Date(String(v).slice(0,10)+'T12:00:00');
    const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
  }catch(e){return String(v);}
}
$('fileInput').addEventListener('change',e=>{const f=e.target.files[0];if(f)selectFile(f);});
function selectFile(f){selectedFile=f;$('fileName').textContent=f.name;$('fileSize').textContent=(f.size/1024).toFixed(1)+' KB';$('fileSelected').classList.add('show');$('btnUpload').classList.add('show');hide('resultCard');hide('errorCard');hide('resetBtn');}
async function uploadFile(){
  if(!selectedFile)return;
  $('btnUpload').disabled=true;show('progressWrap');
  $('progressFill').style.width='30%';$('progressLabel').textContent='Enviando...';
  const form=new FormData();form.append('file',selectedFile);
  try{
    $('progressFill').style.width='60%';$('progressLabel').textContent='Procesando .fit...';
    const res=await fetch(API+'/analyze-fit',{method:'POST',body:form});
    $('progressFill').style.width='90%';
    if(!res.ok){const err=await res.json().catch(()=>({detail:'Error'}));throw new Error(err.detail||'HTTP '+res.status);}
    const data=await res.json();
    $('progressFill').style.width='100%';$('progressLabel').textContent='¡Listo!';
    setTimeout(()=>{hide('progressWrap');showResult(data);},400);
  }catch(err){hide('progressWrap');$('btnUpload').disabled=false;$('errorMsg').textContent='Error: '+err.message;show('errorCard');show('resetBtn');}
}
function showResult(data){
  const sid=data.session_id,s=data.session||{},isDup=data.duplicate===true;
  $('sessionIdValue').textContent=sid;$('hintId').textContent=sid;
  $('chartsBtn').href='/charts/'+sid;
  const meta=[{key:'Fecha',val:fmtDate(s.start_time)},{key:'Distancia',val:s.distance_km?s.distance_km+' km':'—'},{key:'Duración',val:s.duration_hms||'—'},{key:'FC prom.',val:s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—'}];
  $('resultMeta').innerHTML=meta.map(m=>`<div class="meta-item"><div class="meta-key">${m.key}</div><div class="meta-val">${m.val}</div></div>`).join('');
  if(isDup){
    const prev=$('resultCard').querySelector('.dup-warn');if(prev)prev.remove();
    const warn=document.createElement('div');
    warn.className='dup-warn';
    warn.style.cssText='background:#fff8e1;border:1px solid #f2a623;border-radius:8px;padding:10px 14px;font-size:12px;color:#7a5200;margin-bottom:12px';
    warn.textContent='Esta actividad ya estaba registrada. Mostrando la sesión existente.';
    $('resultCard').insertBefore(warn,$('resultCard').firstChild);
  }
  show('resultCard');show('resetBtn');hide('btnUpload');
}
function copyId(){const sid=$('sessionIdValue').textContent;navigator.clipboard.writeText(sid).then(()=>{const b=$('copyBtn');b.textContent='✓ Copiado';b.classList.add('copied');setTimeout(()=>{b.textContent='Copiar';b.classList.remove('copied');},2000);});}
function reset(){selectedFile=null;$('fileInput').value='';hide('fileSelected');hide('btnUpload');hide('resultCard');hide('errorCard');hide('resetBtn');hide('progressWrap');$('btnUpload').disabled=false;$('progressFill').style.width='0%';}
function show(id){$(id).classList.add('show');}function hide(id){$(id).classList.remove('show');}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return HTML_UPLOAD


@app.get("/api")
def api_status():
    return {"status":"ok","service":"FIT Analyzer API — Mars Edition","version":"4.0",
            "db": "connected" if get_db() else "in-memory"}


@app.get("/health")
def health():
    conn = get_db()
    db_ok = False
    db_detail = "no DATABASE_URL"
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

# ─────────────────────────────────────────────────────────────────────────────
# /dashboard  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/analyze-fit")
async def analyze_fit(file: UploadFile = File(...), include_records: bool = Query(False)):
    logger.info(f"UPLOAD start filename={file.filename}")
    try:
        content  = await file.read()
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
                    "message": f"⚠️ Esta actividad ya fue subida (session_id: {duplicate_sid}). Abriendo sesión existente.",
                    "charts_url": f"/charts/{duplicate_sid}",
                    "duplicate": True,
                    "persisted": db_saved,
                    "storage": "postgres" if db_saved else "memory",
                    "achievements": [],
                    **r}
        message = (
            f"✅ Guardado permanentemente. Pasa el session_id '{sid}' al GPT."
            if db_saved else
            f"⚠️ Analizado, pero NO quedó permanente porque la base de datos no está disponible. session_id temporal: '{sid}'."
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
