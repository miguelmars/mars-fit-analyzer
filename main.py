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

# ── Route matching ────────────────────────────────────────────────────────────

def coords_within_meters(lat1, lon1, lat2, lon2, meters):
    """Check if two coordinates are within N meters of each other."""
    if None in (lat1, lon1, lat2, lon2):
        return False
    dlat = abs(lat1 - lat2) * 111000
    dlon = abs(lon1 - lon2) * 111000 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(dlat**2 + dlon**2) <= meters


def route_signature(start_lat, start_lon, end_lat, end_lon, distance_km, ascent_m):
    """Generate a fuzzy route signature for matching (kept for derived_insights)."""
    if not start_lat or not start_lon:
        return None
    slat = round(start_lat * 200) / 200
    slon = round(start_lon * 200) / 200
    elat = round(end_lat * 200) / 200  if end_lat else slat
    elon = round(end_lon * 200) / 200  if end_lon else slon
    dist_bucket = round(distance_km / 3) * 3
    asc_bucket  = round(ascent_m / 100) * 100
    return f"{slat:.3f},{slon:.3f}|{elat:.3f},{elon:.3f}|{dist_bucket}|{asc_bucket}"


def find_or_create_route(conn, start_lat, start_lon, end_lat, end_lon,
                          distance_km, ascent_m, workout_name, sport=""):
    """
    Match session to existing route or create new one.

    Matching criteria (ALL must pass):
      1. Same sport
      2. Start coords within 500m
      3. End coords within 500m (if available)
      4. Distance within 15%
      5. Ascent within 20% (only if ascent > 50m)
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

        route_id = str(uuid.uuid4())[:8]
        if workout_name and workout_name not in ("", "None"):
            base = workout_name.replace("Atizapán de Zaragoza - ", "").replace("Atizapán de Zaragoza", "").strip()
            name = base if base else f"Ruta {round(distance_km)}km"
        else:
            name = f"Ruta {round(distance_km)}km +{ascent_m}m"

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO routes
                    (route_id, name, distance_km, ascent_m, created_at,
                     sample_lat, sample_lon, end_lat, end_lon, sport)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (route_id, name, round(distance_km, 1), ascent_m,
                  datetime.now(timezone.utc),
                  start_lat, start_lon, end_lat, end_lon, sport_norm))
        return {"route_id": route_id, "name": name}

    except Exception as e:
        logger.info(f"find_or_create_route error: {e}")
        return None


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

SESSION_READ_TABLE = "sessions_clean_compat"
TELEMETRY_INDEX_CACHE = {"loaded_at": 0, "by_id": None, "by_day": None}

def _telemetry_match_sql(alias="sessions_clean_compat"):
    return f"""
        (
            sr.session_id IN ({alias}.session_id, REPLACE({alias}.session_id, 'session:', ''))
        )
    """


def _telemetry_exists_sql(alias="sessions_clean_compat"):
    return f"EXISTS(SELECT 1 FROM session_records sr WHERE {_telemetry_match_sql(alias)})"


def _telemetry_points_sql(alias="sessions_clean_compat"):
    return f"(SELECT COUNT(*) FROM session_records sr WHERE {_telemetry_match_sql(alias)})"


def _telemetry_map_sql(alias="sessions_clean_compat"):
    return f"EXISTS(SELECT 1 FROM session_records sr WHERE {_telemetry_match_sql(alias)} AND sr.lat IS NOT NULL AND sr.lon IS NOT NULL)"


def _sport_filter_sql(sport, table_alias=None):
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


def _parse_dt_loose(value):
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


def _telemetry_match_for_session(session, old_records_by_id, old_records_by_day):
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
    by_id = {}
    by_day = {}
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


def _extract_calories_from_payload(payload):
    """Recover calories from Garmin/FIT JSON when clean_sessions.calories is empty."""
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


def _enrich_session_dict(session):
    """Normalize clean-session data before returning it to UI/GPT endpoints."""
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
            session["efficiency"] = round(float(session["avg_speed_kmh"]) / float(session["avg_hr_bpm"]), 4)
        except Exception:
            session["efficiency"] = None
    session["telemetry_status"] = (
        "available" if session.get("has_telemetry")
        else "summary_only"
    )
    session["charts_status"] = (
        "available" if session.get("has_telemetry")
        else "needs_original_fit_tcx"
    )
    return session

class GearServiceIn(BaseModel):
    service_type: str
    gear_id: str = None
    gear_name: str = None
    description: str = None
    shop: str = None
    date: str = None
    km_at_service: float = None
    cost_mxn: float = None
    notes: str = None

class AccidentIn(BaseModel):
    date: str
    description: str = None
    damage: str = None
    repair: str = None
    cost_mxn: float = None
    km_at_accident: int = None
    notes: str = None


class NutritionIn(BaseModel):
    date: str
    session_id: str = None
    moment: str = "durante"
    gel_type: str = None
    gel_count: int = None
    agua_ml: int = None
    carbos_g: float = None
    notas: str = None
    gi_response: str = None
    energy_response: str = None


RESULTS_STORE = {}  # In-memory cache for recent FIT analysis results

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="4.0")

try:
    from strava.webhook import router as strava_router
    app.include_router(strava_router)
    print("✅ Strava router cargado OK")
except Exception as _strava_err:
    import traceback
    print(f"❌ ERROR Strava router: {_strava_err}")
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

class WeightIn(BaseModel):
    date: str
    weight_kg: float
    waist_cm: float = None
    body_fat_pct: float = None
    notes: str = None

class WellnessIn(BaseModel):
    date: str
    category: str = None
    compex_program: str = None
    muscle_zone: list = None
    duration_min: int = None
    ceragem_duration_min: int = None
    ceragem_sensation_before: int = None
    ceragem_sensation_after: int = None
    sleep_hours: float = None
    sleep_quality: str = None
    hr_rest: int = None
    garmin_sleep_score: int = None
    pain_zone: str = None
    pain_level: int = None
    pain_start: str = None
    pain_end: str = None
    pain_type: str = None
    stress_level: int = None
    stress_cause: str = None
    notes: str = None
    fatigue: int = None

class FuerzaIn(BaseModel):
    date: str
    category: str = None
    subcategory: str = None
    muscle_groups: list = None
    intensity: int = None
    duration_min: int = None
    sets: int = None
    reps: int = None
    weight_kg: float = None
    exercise: str = None
    notes: str = None
    rpe: int = None
    fatigue_before: int = None
    fatigue_after: int = None


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


@app.get("/routes")
def list_routes():
    conn = get_db()
    if not conn:
        return {"error": "Base de datos no conectada. Configura DATABASE_URL."}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.route_id, r.name, r.distance_km, r.ascent_m,
                       COUNT(s.session_id) as times,
                       MIN(s.start_time) as first_ride,
                       MAX(s.start_time) as last_ride,
                       AVG(s.avg_hr_bpm) as avg_hr,
                       AVG(s.avg_speed_kmh) as avg_spd,
                       MODE() WITHIN GROUP (ORDER BY s.sport) as sport
                FROM routes r
                LEFT JOIN sessions_clean_compat s ON s.route_id = r.route_id
                GROUP BY r.route_id, r.name, r.distance_km, r.ascent_m
                ORDER BY times DESC
            """)
            rows = cur.fetchall()
            return [{"route_id":r[0],"name":r[1],"distance_km":r[2],"ascent_m":r[3],
                     "times_ridden":r[4],"first_ride":str(r[5])[:10] if r[5] else None,
                     "last_ride":str(r[6])[:10] if r[6] else None,
                     "avg_hr_bpm":round(r[7],1) if r[7] else None,
                     "avg_speed_kmh":round(r[8],1) if r[8] else None,
                     "sport":r[9]}
                    for r in rows]
    except Exception as e:
        return {"error": str(e)}


@app.get("/route/{route_id}")
def get_route(route_id: str):
    conn = get_db()
    if not conn:
        return {"error": "Base de datos no conectada."}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, distance_km, ascent_m FROM routes WHERE route_id=%s", (route_id,))
            route = cur.fetchone()
            if not route:
                raise HTTPException(404, "Ruta no encontrada")
            cur.execute("""
                SELECT session_id, start_time, avg_hr_bpm, avg_speed_kmh,
                       avg_cadence, duration_s, workout_name
                FROM sessions_clean_compat WHERE route_id=%s
                ORDER BY start_time ASC
            """, (route_id,))
            rides = cur.fetchall()

        sessions_list = [{"session_id":r[0],"date":str(r[1])[:10] if r[1] else None,
                          "avg_hr_bpm":r[2],"avg_speed_kmh":r[3],
                          "avg_cadence":r[4],"duration_s":r[5],"workout_name":r[6]}
                         for r in rides]

        # Compute progress
        progress = {}
        if len(sessions_list) >= 2:
            first = sessions_list[0]; last = sessions_list[-1]
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                hr_delta = last["avg_hr_bpm"] - first["avg_hr_bpm"]
                progress["hr_change_bpm"] = hr_delta
                progress["hr_note"] = (
                    f"FC bajó {abs(hr_delta)} bpm → mejora aeróbica real" if hr_delta < -3
                    else f"FC subió {hr_delta} bpm" if hr_delta > 3
                    else "FC estable entre primera y última ejecución"
                )
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                spd_delta = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 1)
                progress["speed_change_kmh"] = spd_delta
                progress["speed_note"] = (
                    f"Velocidad aumentó {spd_delta} km/h en {len(sessions_list)} ejecuciones"
                    if spd_delta > 0.5 else "Velocidad estable"
                )

        return {"route_id":route_id,"name":route[0],"distance_km":route[1],
                "ascent_m":route[2],"times_ridden":len(sessions_list),
                "rides":sessions_list,"progress":progress}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


def _route_matched_html(route_id: str):
    rid = json.dumps(route_id)
    return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Comparativa de ruta</title>
  <style>
    body{{margin:0;background:#08090b;color:#f7f7f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
    main{{max-width:980px;margin:0 auto;padding:18px 14px 90px}}
    a,button{{color:#e8593c;text-decoration:none;font-weight:850}}
    button{{background:#15171c;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:9px 12px;cursor:pointer}}
    .top{{display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:14px}}
    .card{{background:#15171c;border:1px solid rgba(255,255,255,.09);border-radius:16px;padding:16px;margin:12px 0}}
    .muted{{color:#8e95a3}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
    .metric{{--mc:#8e95a3;background:linear-gradient(180deg,color-mix(in srgb,var(--mc) 10%,#1c1f26),#1c1f26);border:1px solid color-mix(in srgb,var(--mc) 24%,rgba(255,255,255,.09));border-radius:12px;padding:13px;position:relative;overflow:hidden}}
    .metric:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--mc)}}
    .metric.speed{{--mc:#3dd68c}} .metric.time{{--mc:#4a9eff}} .metric.hr{{--mc:#e8593c}} .metric.efficiency{{--mc:#a78bfa}} .metric.overall{{--mc:#f5c542}}
    .metric .value{{color:var(--mc)}}
    .label{{font-size:11px;color:#8e95a3;font-weight:850;text-transform:uppercase;letter-spacing:.06em}}
    .value{{font-size:22px;font-weight:950;margin-top:4px}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th{{text-align:left;color:#8e95a3;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.1)}}
    td{{padding:11px 8px;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}}
    td:first-child,th:first-child{{white-space:normal}}
    .right{{text-align:right}}
    .good{{color:#3dd68c}} .warn{{color:#f59e0b}}
    .badge{{--bc:#f59e0b;display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:950;margin-left:7px;vertical-align:middle;background:color-mix(in srgb,var(--bc) 14%,transparent);color:var(--bc);border:1px solid color-mix(in srgb,var(--bc) 32%,transparent)}}
    .badge-dot{{width:6px;height:6px;border-radius:50%;background:var(--bc);box-shadow:0 0 8px color-mix(in srgb,var(--bc) 65%,transparent)}}
    .badge.speed{{--bc:#3dd68c}} .badge.time{{--bc:#4a9eff}} .badge.hr{{--bc:#e8593c}} .badge.efficiency{{--bc:#a78bfa}} .badge.overall{{--bc:#f5c542}}
    .mark{{--mc:#f59e0b;display:block;margin-top:3px;font-size:10px;font-weight:900;color:var(--mc)}}
    .mark.speed{{--mc:#3dd68c}} .mark.time{{--mc:#4a9eff}} .mark.hr{{--mc:#e8593c}} .mark.efficiency{{--mc:#a78bfa}} .mark.overall{{--mc:#f5c542}}
    tr.pr-row{{background:linear-gradient(90deg,rgba(255,255,255,.035),transparent 48%)}}
    tr.overall-row{{background:linear-gradient(90deg,rgba(245,197,66,.13),transparent 50%);box-shadow:inset 3px 0 0 #f5c542}}
    .scroll{{overflow-x:auto}}
  </style>
</head>
<body>
<main>
  <div class="top">
    <button onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
    <a href="/activities">Actividades</a>
  </div>
  <section class="card">
    <div class="label">Matched rides</div>
    <h1 id="title">Cargando comparativa...</h1>
    <p id="note" class="muted"></p>
  </section>
  <section class="grid" id="leaders"></section>
  <section class="card">
    <div class="scroll">
      <table>
        <thead><tr><th>Fecha</th><th class="right">Vel.</th><th class="right">Tiempo</th><th class="right">FC</th><th class="right">Cad.</th><th class="right">Efic.</th></tr></thead>
        <tbody id="rows"><tr><td colspan="6" class="muted">Cargando...</td></tr></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const rid = {rid};
const hms = s => {{
  s = Number(s || 0);
  return String(Math.floor(s/3600)).padStart(2,'0') + ':' + String(Math.floor((s%3600)/60)).padStart(2,'0');
}};
const fmtDate = v => {{
  if(!v) return '—';
  try {{
    const d = new Date(String(v).slice(0,10) + 'T12:00:00');
    const months = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
  }} catch(e) {{
    return String(v);
  }}
}};
const metric = (k,v,u='',cls='',extra='') => `<div class="metric ${{cls}}"><div class="label">${{k}}${{extra}}</div><div class="value">${{v}}</div><div class="muted">${{u}}</div></div>`;
const badge = (txt, cls='') => `<span class="badge ${{cls}}"><span class="badge-dot"></span>${{txt}}</span>`;
fetch('/route/' + encodeURIComponent(rid) + '/matched?format=json').then(async r => {{
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}}).then(d => {{
  const route = d.route || {{}};
  document.getElementById('title').textContent = route.name || 'Ruta Garmin';
  document.getElementById('note').textContent = d.message || ((route.distance_km || '—') + ' km · ' + (d.counts?.unique_efforts || 0) + ' esfuerzos comparables' + (d.counts?.duplicates_hidden ? ' · ' + d.counts.duplicates_hidden + ' duplicados ocultos' : ''));
  const l = d.leaders || {{}};
  const efforts = d.efforts || [];
  const leaderIds = {{
    speed: l.best_speed?.session_id,
    time: l.best_time?.session_id,
    hr: l.lowest_hr?.session_id,
    efficiency: l.best_efficiency?.session_id
  }};
  const rankScore = (arr, id, key, dir='desc') => {{
    const sorted = arr.filter(x => x[key] !== null && x[key] !== undefined && Number(x[key]) > 0)
      .slice()
      .sort((a,b) => dir === 'asc' ? Number(a[key]) - Number(b[key]) : Number(b[key]) - Number(a[key]));
    const idx = sorted.findIndex(x => x.session_id === id);
    return idx >= 0 ? idx + 1 : sorted.length + 1;
  }};
  const scored = efforts.map(e => ({{
    id:e.session_id,
    score:
      rankScore(efforts,e.session_id,'avg_speed_kmh','desc') +
      rankScore(efforts,e.session_id,'duration_s','asc') +
      rankScore(efforts,e.session_id,'avg_hr_bpm','asc') +
      rankScore(efforts,e.session_id,'efficiency','desc')
  }})).sort((a,b)=>a.score-b.score);
  leaderIds.overall = scored[0]?.id;
  const cardClass = id => id && id === leaderIds.overall ? 'overall' : '';
  const leaderBadge = id => id && id === leaderIds.overall ? badge('MEJOR','overall') : '';
  document.getElementById('leaders').innerHTML = [
    metric('Mejor velocidad', l.best_speed?.avg_speed_kmh ? l.best_speed.avg_speed_kmh + ' km/h' : '—', fmtDate(l.best_speed?.date), cardClass(l.best_speed?.session_id) || 'speed', leaderBadge(l.best_speed?.session_id)),
    metric('Mejor tiempo', l.best_time?.duration_s ? hms(l.best_time.duration_s) : '—', fmtDate(l.best_time?.date), cardClass(l.best_time?.session_id) || 'time', leaderBadge(l.best_time?.session_id)),
    metric('FC mas baja', l.lowest_hr?.avg_hr_bpm ? l.lowest_hr.avg_hr_bpm + ' bpm' : '—', fmtDate(l.lowest_hr?.date), cardClass(l.lowest_hr?.session_id) || 'hr', leaderBadge(l.lowest_hr?.session_id)),
    metric('Mejor eficiencia', l.best_efficiency?.efficiency || '—', fmtDate(l.best_efficiency?.date), cardClass(l.best_efficiency?.session_id) || 'efficiency', leaderBadge(l.best_efficiency?.session_id))
  ].join('');
  const marksFor = e => {{
    const marks = [];
    if(e.session_id === leaderIds.overall) marks.push('mejor general');
    if(e.session_id === leaderIds.speed) marks.push('velocidad');
    if(e.session_id === leaderIds.time) marks.push('tiempo');
    if(e.session_id === leaderIds.hr) marks.push('FC');
    if(e.session_id === leaderIds.efficiency) marks.push('eficiencia');
    return marks;
  }};
  const badgesFor = e => {{
    const out = [];
    if(e.session_id === leaderIds.overall) out.push(badge('MEJOR','overall'));
    if(e.session_id === leaderIds.speed) out.push(badge('VEL','speed'));
    if(e.session_id === leaderIds.time) out.push(badge('TIEMPO','time'));
    if(e.session_id === leaderIds.hr) out.push(badge('FC','hr'));
    if(e.session_id === leaderIds.efficiency) out.push(badge('EFIC','efficiency'));
    return out.join('');
  }};
  const mark = (e, key, label) => e.session_id === leaderIds[key] ? `<span class="mark ${{key}}">${{label}}</span>` : '';
  document.getElementById('rows').innerHTML = efforts.length ? efforts.map(e => `
    <tr class="${{e.session_id===leaderIds.overall ? 'overall-row' : (marksFor(e).length ? 'pr-row' : '')}}" onclick="location.href='/session/${{e.session_id}}'" style="cursor:pointer">
      <td><strong>${{fmtDate(e.date)}}</strong>${{badgesFor(e)}}<div class="muted">${{e.workout_name || ''}}</div><div class="muted">${{marksFor(e).join(' · ')}}</div></td>
      <td class="right good">${{e.avg_speed_kmh || '—'}} km/h${{mark(e,'speed','mejor')}}</td>
      <td class="right">${{e.duration_hms || hms(e.duration_s)}}${{mark(e,'time','mejor')}}</td>
      <td class="right">${{e.avg_hr_bpm || '—'}}${{mark(e,'hr','mas baja')}}</td>
      <td class="right">${{e.avg_cadence || '—'}}</td>
      <td class="right warn">${{e.efficiency || '—'}}${{mark(e,'efficiency','mejor')}}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="muted">No hay esfuerzos comparables para esta ruta con el filtro actual.</td></tr>';
}}).catch(err => {{
  document.getElementById('title').textContent = 'No se pudo cargar';
  document.getElementById('note').textContent = err.message || String(err);
  document.getElementById('rows').innerHTML = '<tr><td colspan="6" class="muted">Sin datos.</td></tr>';
}});
</script>
</body>
</html>""")


@app.get("/route/{route_id}/matched")
def get_route_matched(route_id: str, request: Request, limit: int = 40, min_distance_km: float = 15.0, format: str = "auto"):
    """Matched rides by route_id, with duplicate rows hidden for comparison only."""
    wants_html = "text/html" in request.headers.get("accept", "") and format != "json"
    if wants_html:
        return _route_matched_html(route_id)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    limit = max(1, min(limit, 200))
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, distance_km, ascent_m, sport
                FROM routes WHERE route_id=%s
            """, (route_id,))
            route = cur.fetchone()
            if not route:
                cur.execute("""
                    SELECT
                        COALESCE(MAX(workout_name), 'Ruta Garmin') AS name,
                        ROUND(AVG(distance_km)::numeric, 2)::float AS distance_km,
                        ROUND(AVG(ascent_m)::numeric, 0)::int AS ascent_m,
                        MAX(sport) AS sport
                    FROM sessions_clean_compat
                    WHERE route_id=%s
                """, (route_id,))
                route = cur.fetchone()
                if not route or route[1] is None:
                    raise HTTPException(404, "Ruta no encontrada")

            route_distance = float(route[1] or 0)
            route_sport = str(route[3] or "").lower()
            if route_sport in ("cycling", "ciclismo", "biking") and route_distance < min_distance_km:
                return {
                    "route": {
                        "route_id": route_id,
                        "name": route[0],
                        "distance_km": route_distance,
                        "ascent_m": int(route[2]) if route[2] is not None else None,
                        "sport": route[3],
                    },
                    "counts": {"raw_records": 0, "unique_efforts": 0, "duplicates_hidden": 0},
                    "leaders": {},
                    "progress": {},
                    "efforts": [],
                    "status": "filtered_short_route",
                    "message": f"Ruta filtrada: para bici se priorizan rutas de {min_distance_km:g} km o mas. Puedes bajar min_distance_km si quieres revisar esta ruta.",
                }

            cur.execute("""
                SELECT session_id, start_time::text, sport, distance_km::float,
                       duration_s::int, ascent_m::int, avg_hr_bpm::float,
                       avg_speed_kmh::float, avg_cadence::float, workout_name
                FROM sessions_clean_compat
                WHERE route_id=%s
                  AND start_time IS NOT NULL
                ORDER BY start_time ASC, session_id ASC
            """, (route_id,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        efforts_by_key = {}
        duplicate_count = 0
        for row in rows:
            s = dict(zip(cols, row))
            date = str(s.get("start_time") or "")[:10]
            duration_s = int(s.get("duration_s") or 0)
            distance_km = float(s.get("distance_km") or 0)
            ascent_m = int(s.get("ascent_m") or 0)
            avg_hr = float(s.get("avg_hr_bpm") or 0)
            avg_speed = float(s.get("avg_speed_kmh") or 0)
            avg_cadence = s.get("avg_cadence")
            efficiency = round(avg_speed / avg_hr, 4) if avg_speed > 0 and avg_hr > 0 else None

            effort = {
                "session_id": s.get("session_id"),
                "date": date,
                "start_time": s.get("start_time"),
                "sport": s.get("sport"),
                "distance_km": round(distance_km, 2),
                "duration_s": duration_s,
                "duration_hms": f"{int(duration_s//3600):02d}h {int((duration_s%3600)//60):02d}m",
                "ascent_m": ascent_m,
                "avg_hr_bpm": round(avg_hr, 1) if avg_hr else None,
                "avg_speed_kmh": round(avg_speed, 2) if avg_speed else None,
                "avg_cadence": round(float(avg_cadence), 1) if avg_cadence is not None else None,
                "efficiency": efficiency,
                "workout_name": s.get("workout_name") or "",
                "duplicate_session_ids": [],
                "duplicate_count": 0,
            }

            # Same-day identical FIT imports collapse into one display effort.
            key = (
                date,
                round(distance_km, 2),
                round(duration_s / 5) * 5,
                round(avg_hr, 0),
                round(avg_speed, 1),
                round(ascent_m / 5) * 5,
            )
            if key in efforts_by_key:
                efforts_by_key[key]["duplicate_session_ids"].append(effort["session_id"])
                efforts_by_key[key]["duplicate_count"] += 1
                duplicate_count += 1
            else:
                efforts_by_key[key] = effort

        efforts = list(efforts_by_key.values())
        efforts.sort(key=lambda x: x.get("start_time") or "")

        valid_speed = [e for e in efforts if e.get("avg_speed_kmh")]
        valid_hr = [e for e in efforts if e.get("avg_hr_bpm")]
        valid_duration = [e for e in efforts if e.get("duration_s")]
        valid_eff = [e for e in efforts if e.get("efficiency")]

        best_speed = max(valid_speed, key=lambda e: e["avg_speed_kmh"]) if valid_speed else None
        best_time = min(valid_duration, key=lambda e: e["duration_s"]) if valid_duration else None
        lowest_hr = min(valid_hr, key=lambda e: e["avg_hr_bpm"]) if valid_hr else None
        best_efficiency = max(valid_eff, key=lambda e: e["efficiency"]) if valid_eff else None

        progress = {}
        if len(efforts) >= 2:
            first, last = efforts[0], efforts[-1]
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                progress["speed_delta_kmh"] = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 2)
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                progress["hr_delta_bpm"] = round(last["avg_hr_bpm"] - first["avg_hr_bpm"], 1)
            if first.get("efficiency") and last.get("efficiency"):
                progress["efficiency_delta"] = round(last["efficiency"] - first["efficiency"], 4)
            progress["first_session_id"] = first.get("session_id")
            progress["last_session_id"] = last.get("session_id")

        recent_efforts = list(reversed(efforts))[:limit]

        return {
            "route": {
                "route_id": route_id,
                "name": route[0],
                "distance_km": float(route[1]) if route[1] is not None else None,
                "ascent_m": int(route[2]) if route[2] is not None else None,
                "sport": route[3],
            },
            "counts": {
                "raw_records": len(rows),
                "unique_efforts": len(efforts),
                "duplicates_hidden": duplicate_count,
            },
            "leaders": {
                "best_speed": best_speed,
                "best_time": best_time,
                "lowest_hr": lowest_hr,
                "best_efficiency": best_efficiency,
            },
            "progress": progress,
            "efforts": recent_efforts,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


def _list_sessions_with_telemetry(conn, sport=None, month=None, limit=20, offset=0, sort="recent"):
    query = """
        SELECT session_id, start_time, sport, distance_km, duration_s,
               avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence, workout_name,
               route_id, calories, source, quality, result_json
        FROM sessions_clean_compat
        WHERE start_time IS NOT NULL
    """
    params = []
    sport_sql, sport_params = _sport_filter_sql(sport)
    query += sport_sql
    params.extend(sport_params)
    if month:
        query += " AND LEFT(start_time, 7) = %s"
        params.append(month)
    if sort == "distance":
        query += " ORDER BY distance_km DESC NULLS LAST"
    elif sort == "duration":
        query += " ORDER BY duration_s DESC NULLS LAST"
    else:
        query += " ORDER BY start_time DESC"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    old_by_id, old_by_day = _load_old_telemetry_index(conn)
    matched = []
    seen = set()
    for row in rows:
        s = dict(zip(cols, row))
        match = _telemetry_match_for_session(s, old_by_id, old_by_day)
        if not match or s.get("session_id") in seen:
            continue
        seen.add(s.get("session_id"))
        s["has_telemetry"] = True
        s["telemetry_points"] = match.get("telemetry_points") or 0
        s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)
        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        s.pop("result_json", None)
        matched.append(s)

    total = len(matched)
    return {"sessions": matched[offset:offset+limit], "total": total, "limit": limit, "offset": offset, "telemetry": True}


@app.get("/sessions")
def list_sessions(
    sport: Optional[str] = None,
    month: Optional[str] = None,
    telemetry: Optional[bool] = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "recent"
):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        if telemetry is True:
            return _list_sessions_with_telemetry(conn, sport=sport, month=month, limit=limit, offset=offset, sort=sort)
        has_telemetry_sql = _telemetry_exists_sql()
        telemetry_points_sql = _telemetry_points_sql()
        has_map_sql = _telemetry_map_sql()
        query = f"""
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence, workout_name,
                   route_id, calories, source, quality, result_json,
                   {has_telemetry_sql} AS has_telemetry,
                   {telemetry_points_sql} AS telemetry_points,
                   {has_map_sql} AS has_map
            FROM sessions_clean_compat
            WHERE start_time IS NOT NULL
        """
        params = []
        sport_sql, sport_params = _sport_filter_sql(sport)
        query += sport_sql
        params.extend(sport_params)
        if month:
            query += " AND LEFT(start_time, 7) = %s"
            params.append(month)
        if telemetry is True:
            query += f" AND {has_telemetry_sql}"
        elif telemetry is False:
            query += f" AND NOT {has_telemetry_sql}"
        if sort == "recent":
            query += " ORDER BY start_time DESC"
        elif sort == "distance":
            query += " ORDER BY distance_km DESC NULLS LAST"
        elif sort == "duration":
            query += " ORDER BY duration_s DESC NULLS LAST"
        else:
            query += " ORDER BY start_time DESC"
        query += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        sessions_list = []
        for row in rows:
            s = dict(zip(cols, row))
            dur = s.get("duration_s") or 0
            s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            _enrich_session_dict(s)
            for k, v in s.items():
                if hasattr(v, "isoformat"):
                    s[k] = v.isoformat()
            s.pop("result_json", None)
            sessions_list.append(s)

        # Total count
        count_query = "SELECT COUNT(*) FROM sessions_clean_compat WHERE start_time IS NOT NULL"
        count_params = []
        count_sport_sql, count_sport_params = _sport_filter_sql(sport)
        count_query += count_sport_sql
        count_params.extend(count_sport_params)
        if month:
            count_query += " AND LEFT(start_time, 7) = %s"
            count_params.append(month)
        if telemetry is True:
            count_query += f" AND {has_telemetry_sql}"
        elif telemetry is False:
            count_query += f" AND NOT {has_telemetry_sql}"
        with conn.cursor() as cur:
            cur.execute(count_query, count_params)
            total = cur.fetchone()[0]

        return {"sessions": sessions_list, "total": total, "limit": limit, "offset": offset, "telemetry": telemetry}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/charts/{session_id}", response_class=HTMLResponse)
def get_charts(session_id: str):
    entry = RESULTS_STORE.get(session_id)
    if not entry:
        conn = get_db()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT result_json, start_time, sport, distance_km, duration_s,
                               ascent_m, avg_hr_bpm, avg_speed_kmh, avg_cadence,
                               workout_name, calories, source, quality
                        FROM sessions_clean_compat WHERE session_id=%s
                    """,(session_id,))
                    row = cur.fetchone()
                    if row:
                        result = json.loads(row[0]) if row[0] else {}
                        if "session" not in result:
                            result["session"] = {
                                "start_time": row[1],
                                "sport": row[2],
                                "distance_km": row[3],
                                "duration_s": row[4],
                                "ascent_m": row[5],
                                "avg_hr_bpm": row[6],
                                "avg_speed_kmh": row[7],
                                "avg_cadence_rpm": row[8],
                                "workout_name": row[9],
                                "calories_kcal": row[10],
                                "source": row[11],
                                "quality": row[12],
                            }
                        entry = {"result": result}
            except Exception as _e: logger.error(f'Silent error: {_e}')
    if not entry:
        raise HTTPException(404, f"session_id '{session_id}' no encontrado.")

    result  = entry["result"]
    session = result["session"]
    if not session.get("calories_kcal") and not session.get("calories"):
        recovered_cal = _extract_calories_from_payload(result)
        if recovered_cal:
            session["calories_kcal"] = recovered_cal
    records = result.get("records", [])
    insights = result.get("derived_insights", {})

    if not records:
        # Try loading from session_records table
        conn2 = get_db()
        if conn2:
            try:
                with conn2.cursor() as cur:
                    cur.execute("""
                        SELECT t, hr, speed, cadence, altitude, lat, lon
                        FROM session_records sr
                        WHERE sr.session_id IN (%s, REPLACE(%s, 'session:', ''))
                           OR sr.session_id IN (
                                SELECT old_s.session_id
                                FROM sessions_clean_compat c
                                JOIN sessions old_s
                                 ON NULLIF(old_s.start_time, '') IS NOT NULL
                                 AND NULLIF(c.start_time, '') IS NOT NULL
                                 AND (
                                      ABS(EXTRACT(EPOCH FROM (c.start_time::timestamp - old_s.start_time::timestamp))) <= 5
                                      OR ABS(ABS(EXTRACT(EPOCH FROM (c.start_time::timestamp - old_s.start_time::timestamp))) - 21600) <= 5
                                 )
                                 AND ABS(COALESCE(c.distance_km, 0) - COALESCE(old_s.distance_km, 0)) <= 0.02
                                 AND ABS(COALESCE(c.duration_s, 0) - COALESCE(old_s.duration_s, 0)) <= 180
                                WHERE c.session_id = %s
                           )
                        ORDER BY t ASC
                    """, (session_id, session_id, session_id))
                    db_records = cur.fetchall()
                if db_records:
                    def db_num(v):
                        try:
                            return float(v) if v is not None else None
                        except Exception:
                            return None
                    records = [
                        {"heart_rate_bpm": db_num(r[1]), "speed_kmh": db_num(r[2]),
                         "cadence_rpm": db_num(r[3]), "altitude_m": db_num(r[4]),
                         "lat": db_num(r[5]), "lon": db_num(r[6]), "_t": db_num(r[0])}
                        for r in db_records
                    ]
                    times = [db_num(r[0]) for r in db_records]
                    hr = [db_num(r[1]) for r in db_records]
                    cad = [db_num(r[3]) for r in db_records]
                    spd = [db_num(r[2]) for r in db_records]
                    alt = [db_num(r[4]) for r in db_records]
                    lat = [db_num(r[5]) for r in db_records]
                    lon = [db_num(r[6]) for r in db_records]
                    temp = [None] * len(db_records)
            except Exception as e:
                logger.info(f"session_records load error: {e}")
    if not records:
        src = session.get("source", "")
        is_garmin_export = str(src).startswith("garmin")
        msg = (
            "Esta sesión fue importada desde Garmin Connect. "
            "Las gráficas segundo a segundo solo están disponibles para actividades subidas como archivo FIT."
            if is_garmin_export else
            "No hay datos de telemetría disponibles para esta sesión."
        )

        def _fmt_dur(s):
            if not s: return "—"
            s = int(s)
            h, m = s // 3600, (s % 3600) // 60
            return f"{h}h {m:02d}m" if h else f"{m}m"

        def _stat(label, val, unit=""):
            v = f"{val} {unit}".strip() if val else "—"
            return f'<div class="stat"><div class="stat-l">{label}</div><div class="stat-v">{v}</div></div>'

        stats_html = (
            _stat("Fecha", str(session.get("start_time", ""))[:10]) +
            _stat("Deporte", session.get("sport", "").replace("_", " ").title()) +
            _stat("Distancia", f"{session.get('distance_km', '')}", "km") +
            _stat("Duración", _fmt_dur(session.get("duration_s"))) +
            _stat("FC prom.", session.get("avg_hr_bpm", ""), "bpm") +
            _stat("Vel. prom.", session.get("avg_speed_kmh", ""), "km/h") +
            _stat("Cadencia", session.get("avg_cadence_rpm") or session.get("avg_cadence", ""), "rpm") +
            _stat("Desnivel", session.get("ascent_m", ""), "m")
        )
        sid_js = json.dumps(session_id)
        return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Resumen de sesión</title>
  <style>
    body{{margin:0;background:#08090b;color:#f7f7f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
    main{{max-width:860px;margin:0 auto;padding:22px 16px 96px}}
    .top{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:18px}}
    a,button{{color:#e8593c;text-decoration:none;font-weight:850}}
    button,.btn{{background:#15171c;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:10px 12px;cursor:pointer;display:inline-block}}
    .card{{background:#15171c;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:22px;margin:12px 0}}
    h2{{font-size:22px;font-weight:850;margin:0 0 14px}}
    .notice{{color:#8d8d96;font-size:14px;line-height:1.5;margin-bottom:16px;padding:10px 14px;background:rgba(255,255,255,.04);border-radius:8px;border-left:3px solid #34343a}}
    .stats{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px}}
    .stat{{background:#0d0f13;border-radius:10px;padding:12px 14px}}
    .stat-l{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8d8d96;margin-bottom:4px}}
    .stat-v{{font-size:20px;font-weight:750}}
    .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}}
    @media(max-width:480px){{.stats{{grid-template-columns:1fr 1fr}}}}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <button onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
      <a href="/activities">Actividades</a>
    </div>
    <section class="card">
      <h2>{session.get('workout_name') or 'Resumen de sesión'}</h2>
      <div class="notice">{msg}</div>
      <div class="stats">{stats_html}</div>
      <div class="actions">
        <a class="btn" href="/activities">Actividades</a>
        <a class="btn" href="/activities?telemetry=1">Ver sesiones con curvas</a>
      </div>
    </section>
  </main>
  <script>const sid={sid_js};</script>
</body>
</html>""")

    # Si los records ya tienen _t (vienen de session_records), usar esos arrays directamente
    from_db = records and "_t" in records[0]
    if not from_db:
        times, hr, cad, spd, alt, temp, lat, lon = [], [], [], [], [], [], [], []
        start_ts = None
        for r in records:
            try:
                from datetime import datetime as dt
                t = dt.fromisoformat(r.get("timestamp",""))
                if start_ts is None: start_ts = t
                elapsed = int((t-start_ts).total_seconds())
            except:
                elapsed = len(times)
            times.append(elapsed)
            hr.append(r.get("heart_rate_bpm"))
            cad.append(r.get("cadence_rpm"))
            spd.append(r.get("speed_kmh"))
            alt.append(r.get("altitude_m"))
            temp.append(r.get("temperature_c"))
            lat.append(r.get("lat"))
            lon.append(r.get("lon"))
    # else: times, hr, cad, spd, alt, temp already set from session_records

    data_js = f"""
const times={json.dumps(times)};
const hrData={json.dumps(hr)};
const cadData={json.dumps(cad)};
const spdData={json.dumps(spd)};
const altData={json.dumps(alt)};
const tempData={json.dumps(temp)};
const latData={json.dumps(lat)};
const lonData={json.dumps(lon)};
const zonesData={json.dumps(result.get('zones',[]))};
const sessionInfo={json.dumps(session)};
const insights={json.dumps(insights)};
"""

    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Graficas Mars</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{{--bg:#000;--s:#050505;--s2:#0d0d0f;--b:#242428;--line:#34343a;--a:#e8593c;--blue:#2f9bff;--green:#37d46f;--red:#ff6670;--orange:#ff8a1f;--gray:#7c7c83;--t:#f6f6f7;--m:#8d8d96}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",sans-serif;padding:0}}
  main{{max-width:920px;margin:0 auto;padding:14px 16px 80px}}
  .top{{position:sticky;top:0;z-index:5;background:rgba(0,0,0,.88);backdrop-filter:blur(18px);border-bottom:1px solid var(--b);padding:12px 16px;display:flex;align-items:center;gap:12px;justify-content:space-between}}
  .top-left{{display:flex;align-items:center;gap:10px}}
  .back{{background:#0d0d0f;border:1px solid var(--line);color:#ff7058;border-radius:12px;padding:9px 13px;font-size:14px;font-weight:850;cursor:pointer}}
  a{{color:#ff7058;text-decoration:none;font-weight:850}}
  .hero{{padding:20px 0 18px;border-bottom:10px solid #0d0d0f;margin-bottom:18px}}
  .ttl{{font-size:34px;font-weight:850;line-height:1.05;margin-bottom:8px}}
  .sub{{font-size:14px;color:var(--m);line-height:1.35}}
  .mode{{display:grid;grid-template-columns:1fr 1fr;gap:0;background:#151519;border:1px solid var(--b);border-radius:12px;padding:3px;margin:16px 0 4px;max-width:560px}}
  .mode button{{border:none;border-radius:9px;padding:9px;background:transparent;color:#d7d7dc;font-size:15px;font-weight:750}}
  .mode button.on{{background:#5e5e66;color:white}}
  .summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
  .mini{{background:#09090b;border:1px solid var(--b);border-radius:14px;padding:12px}}
  .mini-k{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--m);font-weight:850}}
  .mini-v{{font-size:22px;font-weight:850;margin-top:5px}}
  .section{{border-top:1px solid var(--b);padding:30px 0 26px}}
  .section:first-of-type{{border-top:0}}
  .section-title{{font-size:28px;font-weight:850;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  .metric-pair{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:18px}}
  .big-stat{{border-top:1px solid var(--line);padding-top:12px}}
  .big-val{{font-size:39px;font-weight:350;letter-spacing:0;line-height:1}}
  .big-unit{{font-size:18px;color:#d7d7dc;margin-left:4px}}
  .big-label{{font-size:17px;color:var(--m);margin-top:6px}}
  .chart-wrap{{height:260px;position:relative}}
  .chart-wrap.compact{{height:220px}}
  canvas{{width:100%!important;height:100%!important}}
  .mapbox{{height:240px;border-radius:16px;background:#090b0f;border:1px solid var(--b);overflow:hidden;position:relative;margin-top:10px}}
  .mapbox svg{{width:100%;height:100%;display:block}}
  .mapnote{{position:absolute;left:12px;bottom:10px;color:#d7d7dc;font-size:12px;background:rgba(0,0,0,.72);padding:6px 10px;border-radius:999px}}
  .te-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:22px}}
  .te-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle}}
  .effect-row{{margin:24px 0}}
  .effect-label{{font-size:25px;margin-bottom:10px}}
  .effect-bar{{height:11px;border-radius:8px;display:grid;grid-template-columns:1.1fr 1.1fr 1.1fr 1.1fr 1.1fr .12fr;gap:8px;position:relative}}
  .effect-bar span{{border-radius:3px;background:#777}}
  .effect-bar .b{{background:#399cff}} .effect-bar .g{{background:#56d878}} .effect-bar .o{{background:#ff9b38}} .effect-bar .r{{background:#ff4545}}
  .knob{{position:absolute;top:50%;width:33px;height:33px;border-radius:50%;background:#56d878;border:4px solid #101010;transform:translate(-50%,-50%);box-shadow:0 0 0 1px rgba(255,255,255,.25)}}
  .zones-list{{display:flex;flex-direction:column;gap:18px;margin-top:4px}}
  .zone-row{{display:grid;grid-template-columns:minmax(160px,1fr) minmax(150px,2fr) 64px 52px;gap:12px;align-items:center}}
  .zone-name{{font-size:22px}}
  .zone-sub{{font-size:16px;color:var(--m);margin-left:4px}}
  .zone-track{{height:10px;border-radius:99px;background:#626267;overflow:hidden}}
  .zone-fill{{height:100%;border-radius:99px;background:var(--zc)}}
  .zone-time,.zone-pct{{font-size:20px;text-align:right;color:#e7e7eb}}
  .insight{{margin-top:8px;padding:12px 14px;background:#0d0d0f;border-radius:10px;border-left:3px solid var(--orange);font-size:14px;color:#c8c8cf;line-height:1.5}}
  @media(max-width:640px){{main{{padding:10px 14px 70px}}.ttl{{font-size:30px}}.summary{{grid-template-columns:1fr 1fr}}.metric-pair,.te-grid{{grid-template-columns:1fr 1fr;gap:16px}}.big-val{{font-size:34px}}.chart-wrap{{height:230px}}.zone-row{{grid-template-columns:1fr;gap:7px}}.zone-time,.zone-pct{{text-align:left;font-size:17px}}}}
</style></head><body>
<div class="top">
  <div class="top-left">
    <button class="back" onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
  </div>
  <a href="/activities">Actividades</a>
</div>
<main>
<section class="hero">
  <div class="ttl" id="ttl">—</div>
  <div class="sub" id="subline">Gráficas por tiempo</div>
  <div class="mode"><button class="on">Tiempo</button><button>Distancia</button></div>
  <div class="summary">
    <div class="mini"><div class="mini-k">Distancia</div><div class="mini-v" id="sd">—</div></div>
    <div class="mini"><div class="mini-k">Duración</div><div class="mini-v" id="sdur">—</div></div>
    <div class="mini"><div class="mini-k">FC prom.</div><div class="mini-v" id="shr">—</div></div>
    <div class="mini"><div class="mini-k">Vel prom.</div><div class="mini-v" id="sspd">—</div></div>
  </div>
</section>
<section class="section"><div class="section-title">Mapa</div><div class="mapbox" id="mapBox"><div class="mapnote">Cargando mapa...</div></div></section>
<section class="section"><div class="section-title">Elevation</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="altMin">—</span><span class="big-unit">m</span></div><div class="big-label">Minimum</div></div><div class="big-stat"><div><span class="big-val" id="altMax">—</span><span class="big-unit">m</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cAlt"></canvas></div></section>
<section class="section"><div class="section-title">Speed</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="spdAvg">—</span><span class="big-unit">km/h</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="spdMax">—</span><span class="big-unit">km/h</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cSpd"></canvas></div></section>
<section class="section"><div class="section-title">Heart Rate</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="hrAvg">—</span><span class="big-unit">bpm</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="hrMax">—</span><span class="big-unit">bpm</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cHR"></canvas></div></section>
<section class="section"><div class="section-title">Training Effect</div><div class="te-grid"><div class="big-stat"><div><span class="big-val" id="teAer">—</span></div><div class="big-label"><span class="te-dot" style="background:#56d878"></span>Aerobic</div></div><div class="big-stat"><div><span class="big-val" id="teAna">—</span></div><div class="big-label"><span class="te-dot" style="background:#777"></span>Anaerobic</div></div></div><div class="effect-row"><div class="effect-label">Aerobic</div><div class="effect-bar" id="barAer"><span></span><span></span><span class="b"></span><span class="g"></span><span class="o"></span><span class="r"></span><i class="knob"></i></div></div><div class="effect-row"><div class="effect-label">Anaerobic</div><div class="effect-bar" id="barAna"><span></span><span></span><span class="b"></span><span class="g"></span><span class="o"></span><span class="r"></span><i class="knob"></i></div></div></section>
<section class="section"><div class="section-title">Cadence</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="cadAvg">—</span><span class="big-unit">rpm</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="cadMax">—</span><span class="big-unit">rpm</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cCad"></canvas></div></section>
<section class="section"><div class="section-title">Temperature</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="tmpMin">—</span><span class="big-unit">°C</span></div><div class="big-label">Minimum</div></div><div class="big-stat"><div><span class="big-val" id="tmpMax">—</span><span class="big-unit">°C</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap compact"><canvas id="cTmp"></canvas></div></section>
<section class="section"><div class="section-title">Time in Heart Rate Zones</div><div class="zones-list" id="zrow"></div></section>
<section class="section"><div class="section-title">Insights</div><div id="insightsBox"></div></section>
<section class="section"><div class="section-title">Análisis Perfil Mars</div><div id="marsAnalysis" style="color:#c8c8cf;font-size:15px">Cargando análisis...</div></section>
</main>
<script>
{data_js}
function fmtDate(v){{
  if(!v)return '—';
  try{{
    const d=new Date(String(v).slice(0,10)+'T12:00:00');
    const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
  }}catch(e){{return String(v);}}
}}
document.getElementById('ttl').textContent=(sessionInfo.workout_name||fmtDate(sessionInfo.start_time)||'Sesión').slice(0,40)||'Sesión';
document.getElementById('sd').textContent=sessionInfo.distance_km?sessionInfo.distance_km+' km':'—';
(async function(){{
  try{{
    var sid=location.pathname.split('/').pop();
    var resp=await fetch('/gpt/session-analysis/'+sid);
    var a=await resp.json();
    if(a&&a.analysis){{
      var an=a.analysis;
      document.getElementById('marsAnalysis').innerHTML=
        '<div style="display:flex;flex-direction:column;gap:8px">'+
        '<div style="padding:8px 12px;background:rgba(61,214,140,.08);border-left:3px solid #3dd68c;border-radius:6px"><b style="color:#3dd68c">Zona:</b> '+an.zone+'</div>'+
        '<div style="padding:8px 12px;background:rgba(74,158,255,.08);border-left:3px solid #4a9eff;border-radius:6px"><b style="color:#4a9eff">Cadencia:</b> '+an.cadence+'</div>'+
        '<div style="padding:8px 12px;background:rgba(167,139,250,.08);border-left:3px solid #a78bfa;border-radius:6px"><b style="color:#a78bfa">Eficiencia:</b> '+an.efficiency+'</div>'+
        '<div style="padding:8px 12px;background:rgba(232,89,60,.08);border-left:3px solid #e8593c;border-radius:6px"><b style="color:#e8593c">vs Historico:</b> '+an.comparison+'</div>'+
        '</div>';
    }}else{{document.getElementById('marsAnalysis').textContent='Sin datos de analisis';}}
  }}catch(e){{document.getElementById('marsAnalysis').textContent='Analisis no disponible';}}
}})();
document.getElementById('sdur').textContent=sessionInfo.duration_hms||'—';
document.getElementById('shr').textContent=sessionInfo.avg_hr_bpm?sessionInfo.avg_hr_bpm+' bpm':'—';
document.getElementById('sspd').textContent=sessionInfo.avg_speed_kmh?sessionInfo.avg_speed_kmh+' km/h':'—';
const clean = a => (a||[]).map(Number).filter(Number.isFinite);
const avg = a => {{const v=clean(a);return v.length?v.reduce((x,y)=>x+y,0)/v.length:null;}};
const minv = a => {{const v=clean(a);return v.length?Math.min(...v):null;}};
const maxv = a => {{const v=clean(a);return v.length?Math.max(...v):null;}};
const setText = (id,val,dec=0) => {{
  const el=document.getElementById(id);
  if(!el)return;
  el.textContent=val==null?'—':Number(val).toFixed(dec).replace(/\\.0$/,'');
}};
setText('altMin',minv(altData),0); setText('altMax',maxv(altData),0);
setText('spdAvg',sessionInfo.avg_speed_kmh || avg(spdData),1); setText('spdMax',sessionInfo.max_speed_kmh || maxv(spdData),1);
setText('hrAvg',sessionInfo.avg_hr_bpm || avg(hrData),0); setText('hrMax',sessionInfo.max_hr_bpm || maxv(hrData),0);
setText('cadAvg',sessionInfo.avg_cadence_rpm || sessionInfo.avg_cadence || avg(cadData),0); setText('cadMax',sessionInfo.max_cadence_rpm || maxv(cadData),0);
setText('tmpMin',minv(tempData),0); setText('tmpMax',maxv(tempData),0);
const teAer = sessionInfo.training_effect_aerobic ?? sessionInfo.total_training_effect ?? null;
const teAna = sessionInfo.training_effect_anaerobic ?? sessionInfo.total_anaerobic_training_effect ?? null;
setText('teAer',teAer,1); setText('teAna',teAna,1);
function setKnob(id,val){{
  const bar=document.getElementById(id), k=bar&&bar.querySelector('.knob');
  if(!k)return;
  const pct=Math.max(3,Math.min(97,((Number(val)||0)/5)*100));
  k.style.left=pct+'%';
  if(!val) k.style.background='#777';
}}
setKnob('barAer',teAer); setKnob('barAna',teAna);
function drawMap(){{
  const pts=[];
  for(let i=0;i<latData.length;i++){{
    const la=Number(latData[i]), lo=Number(lonData[i]);
    if(Number.isFinite(la)&&Number.isFinite(lo)) pts.push([la,lo]);
  }}
  const box=document.getElementById('mapBox');
  if(pts.length<2){{
    box.innerHTML='<div class="mapnote">Sin puntos GPS suficientes para mapa.</div>';
    return;
  }}
  const lats=pts.map(p=>p[0]), lons=pts.map(p=>p[1]);
  const minLat=Math.min(...lats), maxLat=Math.max(...lats), minLon=Math.min(...lons), maxLon=Math.max(...lons);
  const pad=18, w=720, h=220;
  const dx=maxLon-minLon || 0.00001, dy=maxLat-minLat || 0.00001;
  const path=pts.map((p,i)=>{{
    const x=pad+((p[1]-minLon)/dx)*(w-pad*2);
    const y=h-pad-((p[0]-minLat)/dy)*(h-pad*2);
    return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);
  }}).join(' ');
  const start=pts[0], end=pts[pts.length-1];
  const px=p=>pad+((p[1]-minLon)/dx)*(w-pad*2);
  const py=p=>h-pad-((p[0]-minLat)/dy)*(h-pad*2);
  box.innerHTML=`<svg viewBox="0 0 ${{w}} ${{h}}" preserveAspectRatio="none">
    <path d="${{path}}" fill="none" stroke="#e8593c" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${{px(start)}}" cy="${{py(start)}}" r="6" fill="#3dd68c"/>
    <circle cx="${{px(end)}}" cy="${{py(end)}}" r="6" fill="#f59e0b"/>
  </svg><div class="mapnote">${{pts.length.toLocaleString('es-MX')}} puntos GPS</div>`;
}}
drawMap();
function ds(a,n){{if(a.length<=n)return a;const s=a.length/n;return Array.from({{length:n}},(_,i)=>a[Math.floor(i*s)]);}}
const N=400;
const lbl=ds(times,N).map(s=>{{const m=Math.floor(s/60),sc=s%60;return m+':'+String(sc).padStart(2,'0');}});
const avgLinePlugin={{
  id:'avgLine',
  afterDatasetsDraw(chart,args,opts){{
    if(opts.value==null)return;
    const y=chart.scales.y.getPixelForValue(opts.value);
    const ctx=chart.ctx;
    ctx.save();
    ctx.strokeStyle='rgba(255,255,255,.7)';
    ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(chart.chartArea.left,y);ctx.lineTo(chart.chartArea.right,y);ctx.stroke();
    ctx.restore();
  }}
}};
Chart.register(avgLinePlugin);
function chartOpts(unit,min,max,avgValue){{
  return {{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{display:false}},avgLine:{{value:avgValue}},tooltip:{{backgroundColor:'#101014',borderColor:'#363640',borderWidth:1,titleColor:'#9a9aa3',bodyColor:'#fff',callbacks:{{label:ctx=>ctx.parsed.y!=null?(Math.round(ctx.parsed.y*10)/10)+(unit||''):''}}}}}},
    scales:{{x:{{ticks:{{color:'#8d8d96',font:{{size:11}},maxTicksLimit:6}},grid:{{display:false}}}},y:{{min:min,max:max,ticks:{{color:'#8d8d96',font:{{size:11}}}},grid:{{color:'rgba(255,255,255,.08)'}}}}}}}};
}}
function areaChart(id,data,color,unit,min=null,max=null,avgValue=null,stepped=false){{
  const el=document.getElementById(id); if(!el)return;
  const ctx=el.getContext('2d');
  const grad=ctx.createLinearGradient(0,0,0,260);
  grad.addColorStop(0,color+'cc'); grad.addColorStop(.55,color+'66'); grad.addColorStop(1,color+'10');
  new Chart(el,{{type:'line',data:{{labels:lbl,datasets:[{{data:ds(data,N),borderColor:color,borderWidth:1.8,pointRadius:0,fill:true,backgroundColor:grad,tension:stepped?0:.22,stepped:stepped}}]}},options:chartOpts(unit,min,max,avgValue)}});}}
function barChart(id,data,color,unit,min=null,max=null,avgValue=null){{
  const el=document.getElementById(id); if(!el)return;
  new Chart(el,{{type:'bar',data:{{labels:lbl,datasets:[{{data:ds(data,N),backgroundColor:color+'dd',borderWidth:0,barPercentage:1,categoryPercentage:1}}]}},options:chartOpts(unit,min,max,avgValue)}});}}
areaChart('cAlt',altData,'#37d46f',' m',null,null,avg(altData));
areaChart('cSpd',spdData,'#2f9bff',' km/h',0,null,avg(spdData));
areaChart('cHR',hrData,'#ff6670',' bpm',60,null,avg(hrData));
barChart('cCad',cadData,'#ff8a1f',' rpm',0,null,avg(cadData));
areaChart('cTmp',tempData,'#777',' °C',null,null,avg(tempData),true);
const zc={{0:'#8d8d96',1:'#f6f6f7',2:'#2f9bff',3:'#1fb95f',4:'#ff8a1f',5:'#ff4545'}};
const zrow=document.getElementById('zrow');
zonesData.forEach((z,i)=>{{
  const zone=z.zone||i;
  const secs=Math.round((Number(z.minutes)||0)*60);
  const mm=Math.floor(secs/60), ss=String(secs%60).padStart(2,'0');
  const pct=Math.round(Number(z.percent)||0);
  zrow.innerHTML+=`<div class="zone-row" style="--zc:${{zc[zone]||'#777'}}"><div><span class="zone-name">Zone ${{zone}}</span><span class="zone-sub">${{z.bpm_low||''}}${{z.bpm_high?' - '+z.bpm_high:''}} bpm · ${{(z.name||'').replace('Z'+zone,'').trim()}}</span></div><div class="zone-track"><div class="zone-fill" style="width:${{Math.max(pct,1)}}%"></div></div><div class="zone-time">${{mm}}:${{ss}}</div><div class="zone-pct">${{pct}}%</div></div>`;
}});
const ib=document.getElementById('insightsBox');
const keys=['hr_drift_note','best_aerobic_window','traffic_note','altitude_note'];
keys.forEach(k=>{{if(insights[k]){{
  const val=typeof insights[k]==='object'?insights[k].note:insights[k];
  ib.innerHTML+=`<div class="insight">${{val}}</div>`;
}}}});
if(!ib.innerHTML)ib.innerHTML='<div class="insight" style="color:#444">Sin insights adicionales.</div>';
</script></body></html>"""



# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 1 — Endpoints nuevos: post_session, gear, maintenance, recovery
# ═══════════════════════════════════════════════════════════════════════════════

# ── Pydantic Models ───────────────────────────────────────────────────────────

class PostSessionIn(BaseModel):
    rpe: Optional[int] = Field(None, ge=1, le=10)
    weight_before: Optional[float] = Field(None, gt=0, lt=200)
    weight_after: Optional[float] = Field(None, gt=0, lt=200)
    water_liters: Optional[float] = Field(None, ge=0, le=10)
    caffeine_mg: Optional[int] = Field(None, ge=0, le=1000)
    food_before: Optional[List[str]] = None
    gels: Optional[int] = Field(None, ge=0, le=20)
    bars: Optional[int] = Field(None, ge=0, le=20)
    electrolytes: Optional[bool] = None
    digestion: Optional[str] = None
    sleep_hours: Optional[float] = Field(None, ge=0, le=16)
    sleep_quality: Optional[str] = None
    conditions: Optional[List[str]] = None
    notes: Optional[str] = None
    gel_type: Optional[str] = None
    gel_recipe: Optional[str] = None
    gel_carbs_g: Optional[int] = Field(None, ge=0, le=150)
    gel_sodium_mg: Optional[int] = Field(None, ge=0, le=3000)
    gel_timing: Optional[str] = None
    gi_response: Optional[str] = None
    energy_response: Optional[str] = None

class GearIn(BaseModel):
    gear_id: str
    name: Optional[str] = None
    type: Optional[str] = None
    bike_id: Optional[str] = None
    installed_date: Optional[str] = None
    km_at_install: Optional[int] = None
    km_limit: Optional[int] = None
    notes: Optional[str] = None

class GearUpdate(BaseModel):
    retired_date: Optional[str] = None
    retired_reason: Optional[str] = None
    km_limit: Optional[int] = None
    notes: Optional[str] = None

class MaintenanceIn(BaseModel):
    bike_id: Optional[str] = None
    gear_id: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    km_at_service: Optional[int] = None
    cost_mxn: Optional[float] = None
    shop: Optional[str] = None
    notes: Optional[str] = None

class RecoveryIn(BaseModel):
    date: Optional[str] = None
    type: Optional[str] = None
    duration_min: Optional[int] = None
    muscle_zone: Optional[List[str]] = None
    compex_program: Optional[str] = None
    notes: Optional[str] = None
    fatigue: Optional[int] = None
    muscle_pain: Optional[int] = None
    mental_state: Optional[int] = None


# ── POST /post-session/{session_id} ──────────────────────────────────────────

@app.post("/post-session/{session_id}")
def save_post_session(session_id: str, body: PostSessionIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")

    sweat_rate = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT duration_s FROM sessions_clean_compat WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
        if row and row[0] and body.weight_before and body.weight_after and body.water_liters:
            duration_h = row[0] / 3600
            if duration_h > 0:
                sweat_rate = round(
                    (body.weight_before - body.weight_after + body.water_liters) / duration_h, 2
                )
    except:
        pass

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO post_session (
                    session_id, rpe, weight_before, weight_after, water_liters, sweat_rate,
                    caffeine_mg, food_before, gels, bars, electrolytes, digestion,
                    sleep_hours, sleep_quality, conditions, notes,
                    gel_type, gel_recipe, gel_carbs_g, gel_sodium_mg,
                    gel_timing, gi_response, energy_response, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    rpe=EXCLUDED.rpe, weight_before=EXCLUDED.weight_before,
                    weight_after=EXCLUDED.weight_after, water_liters=EXCLUDED.water_liters,
                    sweat_rate=EXCLUDED.sweat_rate, caffeine_mg=EXCLUDED.caffeine_mg,
                    food_before=EXCLUDED.food_before, gels=EXCLUDED.gels, bars=EXCLUDED.bars,
                    electrolytes=EXCLUDED.electrolytes, digestion=EXCLUDED.digestion,
                    sleep_hours=EXCLUDED.sleep_hours, sleep_quality=EXCLUDED.sleep_quality,
                    conditions=EXCLUDED.conditions, notes=EXCLUDED.notes,
                    gel_type=EXCLUDED.gel_type, gel_recipe=EXCLUDED.gel_recipe,
                    gel_carbs_g=EXCLUDED.gel_carbs_g, gel_sodium_mg=EXCLUDED.gel_sodium_mg,
                    gel_timing=EXCLUDED.gel_timing, gi_response=EXCLUDED.gi_response,
                    energy_response=EXCLUDED.energy_response
            """, (
                session_id, body.rpe, body.weight_before, body.weight_after,
                body.water_liters, sweat_rate, body.caffeine_mg,
                body.food_before, body.gels, body.bars, body.electrolytes,
                body.digestion, body.sleep_hours, body.sleep_quality,
                body.conditions, body.notes,
                body.gel_type, body.gel_recipe, body.gel_carbs_g, body.gel_sodium_mg,
                body.gel_timing, body.gi_response, body.energy_response
            ))
        return {"ok": True, "session_id": session_id, "sweat_rate": sweat_rate}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /post-session/{session_id} ───────────────────────────────────────────

@app.get("/post-session/{session_id}")
def get_post_session(session_id: str):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM post_session WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No hay registro post-sesión para {session_id}")
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── PUT /gear/{gear_id} ───────────────────────────────────────────────────────

@app.put("/gear/{gear_id}")
def update_gear(gear_id: str, body: GearUpdate):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nada que actualizar")
    try:
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gear SET {set_clause} WHERE gear_id=%s",
                list(updates.values()) + [gear_id]
            )
            if cur.rowcount == 0:
                raise HTTPException(404, f"gear_id '{gear_id}' no encontrado")
        return {"ok": True, "gear_id": gear_id, "updated": list(updates.keys())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gear/alerts ──────────────────────────────────────────────────────────

@app.get("/gear/alerts")
def gear_alerts(bike_id: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT g.gear_id, g.name, g.type, g.bike_id,
                   g.km_at_install, g.km_limit, g.installed_date, g.retired_date,
                   COALESCE(
                       (SELECT MAX(m.km_at_service) FROM maintenance m
                        WHERE m.gear_id = g.gear_id),
                       g.km_at_install, 0
                   ) as last_km
            FROM gear g
            WHERE g.retired_date IS NULL
              AND g.km_limit IS NOT NULL
        """
        params = []
        if bike_id:
            query += " AND g.bike_id=%s"
            params.append(bike_id)

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        alerts = []
        for row in rows:
            item = dict(zip(cols, row))
            km_install = item.get("km_at_install") or 0
            last_km = item.get("last_km") or km_install
            km_used = max(0, last_km - km_install)
            km_limit = item["km_limit"]
            pct = round(km_used / km_limit * 100, 1) if km_limit else 0
            if pct >= 60:
                status = "red" if pct >= 85 else "yellow"
                label = "Cambiar ahora" if pct >= 85 else "Revisar pronto"
                alerts.append({**item, "km_used": km_used, "pct_used": pct,
                                "status": status, "label": label})

        alerts.sort(key=lambda x: x["pct_used"], reverse=True)
        return {"alerts": alerts, "total": len(alerts)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /maintenance ──────────────────────────────────────────────────────────

@app.get("/maintenance")
def list_maintenance(bike_id: Optional[str] = None, gear_id: Optional[str] = None, limit: int = 50):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM maintenance WHERE 1=1"
        params = []
        if bike_id:
            query += " AND bike_id=%s"
            params.append(bike_id)
        if gear_id:
            query += " AND gear_id=%s"
            params.append(gear_id)
        query += " ORDER BY date DESC NULLS LAST LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"maintenance": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /recovery ────────────────────────────────────────────────────────────

@app.post("/recovery")
def add_recovery(body: RecoveryIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO recovery (date, type, duration_min, muscle_zone,
                    compex_program, notes, fatigue, muscle_pain, mental_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                body.date, body.type, body.duration_min, body.muscle_zone,
                body.compex_program, body.notes, body.fatigue,
                body.muscle_pain, body.mental_state
            ))
            new_id = cur.fetchone()[0]
        return {"ok": True, "id": new_id}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /recovery ─────────────────────────────────────────────────────────────

@app.get("/recovery")
def list_recovery(limit: int = 30, type: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM recovery WHERE 1=1"
        params = []
        if type:
            query += " AND type=%s"
            params.append(type)
        query += " ORDER BY date DESC NULLS LAST LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"recovery": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 2 — Endpoints: stats/monthly, stats/efficiency, sessions/recent
# ═══════════════════════════════════════════════════════════════════════════════


# ── GET /stats/yearly ─────────────────────────────────────────────────────────

@app.get("/stats/yearly")
def stats_yearly(sport: Optional[str] = None, limit: int = 20):
    """Volumen por año — km, horas, sesiones, FC promedio."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT
                EXTRACT(YEAR FROM start_time::timestamp)::int as year,
                COUNT(*) as sesiones,
                ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) as km_total,
                ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas_total,
                ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                ROUND(SUM(COALESCE(ascent_m,0))::numeric, 0) as ascenso_total
            FROM sessions_clean_compat
            WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " GROUP BY year ORDER BY year ASC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        years = [dict(zip(cols, r)) for r in rows]
        return {"sport": sport or "all", "years": years}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /stats/records ────────────────────────────────────────────────────────

@app.get("/stats/records")
def stats_records(sport: Optional[str] = None):
    """Récords personales históricos."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        where = "WHERE start_time IS NOT NULL"
        params = []
        if sport:
            where += " AND sport = %s"
            params.append(sport)

        records = {}
        with conn.cursor() as cur:
            # Mayor distancia
            cur.execute(f"SELECT distance_km, start_time, session_id FROM sessions_clean_compat {where} AND distance_km IS NOT NULL ORDER BY distance_km DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_distance"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Sesión más larga
            cur.execute(f"SELECT duration_s, start_time, session_id FROM sessions_clean_compat {where} AND duration_s IS NOT NULL ORDER BY duration_s DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_duration"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Mayor ascenso
            cur.execute(f"SELECT ascent_m, start_time, session_id FROM sessions_clean_compat {where} AND ascent_m IS NOT NULL ORDER BY ascent_m DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_ascent"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Mayor velocidad promedio
            cur.execute(f"SELECT avg_speed_kmh, start_time, session_id FROM sessions_clean_compat {where} AND avg_speed_kmh IS NOT NULL ORDER BY avg_speed_kmh DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_speed"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # FC mínima promedio (mejor forma aeróbica)
            cur.execute(f"SELECT avg_hr_bpm, start_time, session_id FROM sessions_clean_compat {where} AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 60 AND distance_km > 20 ORDER BY avg_hr_bpm ASC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["min_hr"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

        return {"sport": sport or "all", "records": records}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/stats/monthly")
def stats_monthly(year: int = None, month: int = None, sport: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    today = date.today()
    if not year: year = today.year
    if not month: month = today.month
    month_str = f"{year}-{month:02d}"
    try:
        query = """
            SELECT COUNT(*) as sesiones,
                   ROUND(SUM(distance_km)::numeric, 1) as km_total,
                   ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas_total,
                   ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                   ROUND(SUM(ascent_m)::numeric, 0) as ascenso_total
            FROM sessions_clean_compat
            WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
        """
        params = [month_str]
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        result = dict(zip(cols, row))
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sport, COUNT(*) as sesiones,
                       ROUND(SUM(distance_km)::numeric, 1) as km
                FROM sessions_clean_compat
                WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
                  AND sport IS NOT NULL AND sport != ''
                GROUP BY sport ORDER BY sesiones DESC
            """, [month_str])
            rows = cur.fetchall()
            cols2 = [d[0] for d in cur.description]
        result["by_sport"] = [dict(zip(cols2, r)) for r in rows]
        result["year"] = year
        result["month"] = month
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/stats/efficiency")
def stats_efficiency(weeks: int = 8, sport: str = "cycling"):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('week', start_time::timestamp), 'YYYY-MM-DD') as semana,
                    COUNT(*) as sesiones,
                    ROUND(AVG(avg_speed_kmh)::numeric, 1) as vel_promedio,
                    ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                    ROUND(SUM(distance_km)::numeric, 1) as km_total,
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END)::numeric, 2) as eficiencia
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                GROUP BY semana ORDER BY semana ASC
            """, (sport, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"sport": sport, "weeks": weeks, "data": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sessions/recent")
def sessions_recent(limit: int = 1, sport: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = f"""
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                   workout_name, route_id, calories, source, quality, result_json,
                   {_telemetry_exists_sql()} AS has_telemetry,
                   {_telemetry_points_sql()} AS telemetry_points,
                   {_telemetry_map_sql()} AS has_map
            FROM sessions_clean_compat WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " ORDER BY start_time DESC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        sessions_list = []
        for row in rows:
            s = dict(zip(cols, row))
            dur = s.get("duration_s") or 0
            s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            for k, v in s.items():
                if hasattr(v, 'isoformat'):
                    s[k] = v.isoformat()
            sessions_list.append(s)
        return {"sessions": sessions_list, "total": len(sessions_list)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 3 — Pantalla Actividades
# ═══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# E26B — GOAL REGISTRY
# ADR-011: el usuario define sus metas; el sistema solo sugiere.
# ═══════════════════════════════════════════════════════════════════════════════

class GoalCreate(BaseModel):
    event_name:  str
    event_type:  str = "cycling"
    event_date:  Optional[str] = None   # ISO date "YYYY-MM-DD"
    distance_km: Optional[float] = None
    elevation_m: Optional[float] = None
    priority:    int = 1
    notes:       Optional[str] = None
    scenario_key: Optional[str] = None  # link a Readiness Scenario si aplica

class GoalUpdate(BaseModel):
    event_name:  Optional[str] = None
    event_date:  Optional[str] = None
    distance_km: Optional[float] = None
    elevation_m: Optional[float] = None
    priority:    Optional[int] = None
    status:      Optional[str] = None   # active | completed | cancelled
    notes:       Optional[str] = None


@app.get("/gpt/goals")
def get_goals(status: str = "active"):
    """Lista metas del atleta. ADR-011: solo las que el usuario creó."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, event_name, event_type, event_date,
                   distance_km, elevation_m, priority, status,
                   notes, scenario_key, created_at
            FROM mars_goals
            WHERE status = %s
            ORDER BY priority ASC, event_date ASC NULLS LAST
        """, (status,))
        rows = cur.fetchall()
    goals = []
    for r in rows:
        goals.append({
            "id":           r[0],
            "event_name":   r[1],
            "event_type":   r[2],
            "event_date":   r[3].isoformat() if r[3] else None,
            "distance_km":  r[4],
            "elevation_m":  r[5],
            "priority":     r[6],
            "status":       r[7],
            "notes":        r[8],
            "scenario_key": r[9],
            "created_at":   r[10].isoformat() if r[10] else None,
        })
    return {"goals": goals, "total": len(goals)}


@app.post("/gpt/goals")
def create_goal(goal: GoalCreate):
    """Crea una meta activa. ADR-011: acción explícita del usuario."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mars_goals
                (event_name, event_type, event_date, distance_km,
                 elevation_m, priority, notes, scenario_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
        """, (
            goal.event_name, goal.event_type,
            goal.event_date, goal.distance_km,
            goal.elevation_m, goal.priority,
            goal.notes, goal.scenario_key,
        ))
        row = cur.fetchone()
    return {
        "status":     "created",
        "id":         row[0],
        "event_name": goal.event_name,
        "created_at": row[1].isoformat(),
    }


@app.put("/gpt/goals/{goal_id}")
def update_goal(goal_id: int, update: GoalUpdate):
    """Actualiza una meta (status, fecha, notas, etc.)."""
    fields, values = [], []
    if update.event_name  is not None: fields.append("event_name=%s");  values.append(update.event_name)
    if update.event_date  is not None: fields.append("event_date=%s");  values.append(update.event_date)
    if update.distance_km is not None: fields.append("distance_km=%s"); values.append(update.distance_km)
    if update.elevation_m is not None: fields.append("elevation_m=%s"); values.append(update.elevation_m)
    if update.priority    is not None: fields.append("priority=%s");    values.append(update.priority)
    if update.status      is not None: fields.append("status=%s");      values.append(update.status)
    if update.notes       is not None: fields.append("notes=%s");       values.append(update.notes)
    if not fields:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    fields.append("updated_at=NOW()")
    values.append(goal_id)
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE mars_goals SET {', '.join(fields)} WHERE id=%s RETURNING id",
            values
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Meta no encontrada")
    return {"status": "updated", "id": goal_id}


@app.delete("/gpt/goals/{goal_id}")
def delete_goal(goal_id: int):
    """Elimina una meta."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mars_goals WHERE id=%s RETURNING id", (goal_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Meta no encontrada")
        conn.commit()
    return {"status": "deleted", "id": goal_id}


# /activities  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/gpt/month-summary")
def gpt_month_summary(year: int = None, month: int = None):
    """Resumen del mes para GPT — km, horas, FC, sesiones, desglose por deporte."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    today = date.today()
    if not year: year = today.year
    if not month: month = today.month
    month_str = f"{year}-{month:02d}"
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as sesiones,
                       ROUND(SUM(distance_km)::numeric, 1) as km_total,
                       ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas_total,
                       ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                       ROUND(SUM(ascent_m)::numeric, 0) as ascenso_total,
                       ROUND(AVG(avg_speed_kmh)::numeric, 1) as vel_promedio
                FROM sessions_clean_compat
                WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
            """, [month_str])
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        summary = dict(zip(cols, row))

        with conn.cursor() as cur:
            cur.execute("""
                SELECT sport, COUNT(*) as sesiones,
                       ROUND(SUM(distance_km)::numeric, 1) as km,
                       ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas
                FROM sessions_clean_compat
                WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
                  AND sport IS NOT NULL AND sport != ''
                GROUP BY sport ORDER BY sesiones DESC
            """, [month_str])
            rows = cur.fetchall()
            cols2 = [d[0] for d in cur.description]
        summary["por_deporte"] = [dict(zip(cols2, r)) for r in rows]
        summary["mes"] = month_str
        return summary
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/latest-session")
def gpt_latest_session(sport: Optional[str] = None):
    """Última sesión para GPT — métricas completas de la actividad más reciente."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                   workout_name, route_id
            FROM sessions_clean_compat WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " ORDER BY start_time DESC LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Sin sesiones")
            cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        if not s.get("has_telemetry"):
            old_by_id, old_by_day = _load_old_telemetry_index(conn)
            match = _telemetry_match_for_session(s, old_by_id, old_by_day)
            if match:
                s["has_telemetry"] = True
                s["telemetry_points"] = match.get("telemetry_points") or 0
                s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)
        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        s.pop("result_json", None)
        return s
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/session/{session_id}")
def gpt_session(session_id: str):
    """Detalle de una sesión específica para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT session_id, start_time, sport, distance_km, duration_s,
                       avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                       workout_name, route_id, result_json, calories, source, quality,
                       {_telemetry_exists_sql()} AS has_telemetry,
                       {_telemetry_points_sql()} AS telemetry_points,
                       {_telemetry_map_sql()} AS has_map
                FROM sessions_clean_compat WHERE session_id = %s
            """, [session_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"session_id '{session_id}' no encontrado")
            cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        if not s.get("has_telemetry"):
            old_by_id, old_by_day = _load_old_telemetry_index(conn)
            match = _telemetry_match_for_session(s, old_by_id, old_by_day)
            if match:
                s["has_telemetry"] = True
                s["telemetry_points"] = match.get("telemetry_points") or 0
                s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)

        # Extraer zonas e insights del result_json
        if s.get("result_json"):
            try:
                rj = json.loads(s["result_json"])
                s["zones"] = rj.get("zones", [])
                s["insights"] = rj.get("derived_insights", {})
                s["laps"] = rj.get("laps", [])
            except:
                pass
        del s["result_json"]

        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        return s
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/route-progress/{route_id}")
def gpt_route_progress(route_id: str):
    """Progreso en una ruta específica para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT route_id, name, distance_km, ascent_m FROM routes WHERE route_id = %s", [route_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"route_id '{route_id}' no encontrado")
            route = {"route_id": row[0], "name": row[1], "distance_km": row[2], "ascent_m": row[3]}

            cur.execute("""
                SELECT start_time, avg_hr_bpm, avg_speed_kmh, avg_cadence, duration_s
                FROM sessions_clean_compat
                WHERE route_id = %s AND start_time IS NOT NULL
                ORDER BY start_time ASC
            """, [route_id])
            rides = cur.fetchall()
            ride_cols = [d[0] for d in cur.description]

        ride_list = []
        for r in rides:
            rd = dict(zip(ride_cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"):
                    rd[k] = v.isoformat()
            ride_list.append(rd)

        # Calcular progreso
        if len(ride_list) >= 2:
            first = ride_list[0]
            last = ride_list[-1]
            hr_delta = None
            spd_delta = None
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                hr_delta = round(last["avg_hr_bpm"] - first["avg_hr_bpm"], 0)
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                spd_delta = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 1)
            route["progreso"] = {
                "veces_rodada": len(ride_list),
                "primera_fecha": ride_list[0]["start_time"],
                "ultima_fecha": ride_list[-1]["start_time"],
                "delta_fc_bpm": hr_delta,
                "delta_velocidad_kmh": spd_delta,
            }
        else:
            route["progreso"] = {"veces_rodada": len(ride_list)}

        route["historial"] = ride_list[-10:]  # últimas 10
        return route
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/gear-alerts")
def gpt_gear_alerts():
    """Alertas de componentes cerca del límite para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.gear_id, g.name, g.type, g.bike_id,
                       g.km_at_install, g.km_limit,
                       COALESCE(
                           (SELECT MAX(m.km_at_service) FROM maintenance m WHERE m.gear_id = g.gear_id),
                           g.km_at_install, 0
                       ) as last_km
                FROM gear g
                WHERE g.retired_date IS NULL AND g.km_limit IS NOT NULL
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        alerts = []
        for row in rows:
            item = dict(zip(cols, row))
            km_install = item.get("km_at_install") or 0
            last_km = item.get("last_km") or km_install
            km_used = max(0, last_km - km_install)
            km_limit = item["km_limit"]
            pct = round(km_used / km_limit * 100, 1) if km_limit else 0
            item["km_used"] = km_used
            item["pct_used"] = pct
            item["status"] = "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            item["label"] = "Cambiar ahora" if pct >= 85 else "Revisar pronto" if pct >= 60 else "OK"
            alerts.append(item)

        alerts.sort(key=lambda x: x["pct_used"], reverse=True)
        critical = [a for a in alerts if a["status"] in ("red", "yellow")]
        return {
            "total_componentes": len(alerts),
            "alertas_criticas": len(critical),
            "alertas": critical,
            "todos": alerts
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/efficiency-trend")
def gpt_efficiency_trend(weeks: int = 8, sport: str = "cycling"):
    """Tendencia de eficiencia aeróbica para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('week', start_time::timestamp), 'YYYY-MM-DD') as semana,
                    COUNT(*) as sesiones,
                    ROUND(AVG(avg_speed_kmh)::numeric, 1) as vel_promedio,
                    ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                    ROUND(SUM(distance_km)::numeric, 1) as km_total,
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END)::numeric, 2) as eficiencia
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                GROUP BY semana ORDER BY semana ASC
            """, (sport, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]

        # Tendencia simple
        trend = "sin datos"
        if len(data) >= 2:
            first_eff = float(data[0]["eficiencia"] or 0)
            last_eff = float(data[-1]["eficiencia"] or 0)
            delta = round(last_eff - first_eff, 2)
            trend = f"+{delta}" if delta > 0 else str(delta)

        return {"sport": sport, "weeks": weeks, "tendencia": trend, "data": data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/zones-summary ────────────────────────────────────────────────────

@app.get("/gpt/zones-summary")
def gpt_zones_summary(sport: str = "cycling"):
    """Tiempo acumulado por zona Mars — últimas 4 y 12 semanas."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        result = {}
        for label, weeks in [("ultimas_4_semanas", 4), ("ultimas_12_semanas", 12)]:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT result_json FROM sessions_clean_compat
                    WHERE sport = %s AND start_time IS NOT NULL
                      AND result_json IS NOT NULL
                      AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                """, (sport, weeks))
                rows = cur.fetchall()
            zone_totals = {}
            for (rj_str,) in rows:
                try:
                    rj = json.loads(rj_str)
                    for z in rj.get("zones", []):
                        zn = z.get("name", f"Z{z.get('zone','?')}")
                        zone_totals[zn] = zone_totals.get(zn, 0) + float(z.get("minutes", 0))
                except Exception:
                    continue
            total_min = sum(zone_totals.values()) or 1
            zones_out = [
                {"zona": k, "minutos": round(v, 1), "horas": round(v/60, 2),
                 "porcentaje": round(v/total_min*100, 1)}
                for k, v in sorted(zone_totals.items())
            ]
            result[label] = {"sesiones_con_datos": len(rows), "total_horas": round(total_min/60, 2), "zonas": zones_out}
        z2_4w = next((z["porcentaje"] for z in result["ultimas_4_semanas"]["zonas"] if "Z2" in z["zona"] or "Aeróbico" in z["zona"]), 0)
        z2_12w = next((z["porcentaje"] for z in result["ultimas_12_semanas"]["zonas"] if "Z2" in z["zona"] or "Aeróbico" in z["zona"]), 0)
        result["z2_check"] = {"pct_4_semanas": z2_4w, "pct_12_semanas": z2_12w,
                               "nota": "Óptimo Z2 para base aeróbica: 70-80% del tiempo total"}
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/cadence-trend ────────────────────────────────────────────────────

@app.get("/gpt/cadence-trend")
def gpt_cadence_trend(weeks: int = 8, sport: str = "cycling"):
    """Tendencia de cadencia semanal — promedio, máxima, tiempo >85 y >95 rpm."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT TO_CHAR(DATE_TRUNC('week', start_time::timestamp), 'YYYY-MM-DD') as semana,
                       COUNT(*) as sesiones,
                       ROUND(AVG(avg_cadence)::numeric, 1) as cadencia_promedio,
                       MAX(avg_cadence) as cadencia_max_sesion
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND avg_cadence IS NOT NULL AND avg_cadence > 0
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                GROUP BY semana ORDER BY semana ASC
            """, (sport, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND((AVG(CASE WHEN cadence > 85 THEN 1.0 ELSE 0.0 END)*100)::numeric,1) as pct_sobre_85,
                       ROUND((AVG(CASE WHEN cadence > 95 THEN 1.0 ELSE 0.0 END)*100)::numeric,1) as pct_sobre_95,
                       COUNT(*) as registros_totales
                FROM session_records sr
                JOIN sessions_clean_compat s ON sr.session_id IN (s.session_id, REPLACE(s.session_id, 'session:', ''))
                WHERE s.sport = %s AND sr.cadence IS NOT NULL AND sr.cadence > 0
                  AND s.start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
            """, (sport, weeks))
            rec_row = cur.fetchone()
        tendencia = "sin datos"
        if len(data) >= 2:
            first = float(data[0].get("cadencia_promedio") or 0)
            last = float(data[-1].get("cadencia_promedio") or 0)
            delta = round(last - first, 1)
            tendencia = f"+{delta} rpm" if delta > 0 else f"{delta} rpm"
        return {
            "sport": sport, "weeks": weeks, "tendencia": tendencia,
            "records_detalle": {
                "pct_sobre_85_rpm": float(rec_row[0] or 0),
                "pct_sobre_95_rpm": float(rec_row[1] or 0),
                "registros_analizados": rec_row[2] or 0
            },
            "data": data
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/route-compare/{route_id} ────────────────────────────────────────

@app.get("/gpt/route-compare/{route_id}")
def gpt_route_compare(route_id: str):
    """Comparador de ruta — primera vez, mejor tiempo, mejor FC, última vez, evolución %."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT route_id, name, distance_km, ascent_m FROM routes WHERE route_id = %s", [route_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"route_id '{route_id}' no encontrado")
            route = {"route_id": row[0], "name": row[1], "distance_km": row[2], "ascent_m": row[3]}
            cur.execute("""
                SELECT session_id, start_time, duration_s, avg_hr_bpm, avg_speed_kmh, avg_cadence
                FROM sessions_clean_compat WHERE route_id = %s AND start_time IS NOT NULL
                ORDER BY start_time ASC
            """, [route_id])
            rides = cur.fetchall()
            cols = [d[0] for d in cur.description]
        if not rides:
            raise HTTPException(404, f"Sin sesiones para route_id '{route_id}'")
        ride_list = []
        for r in rides:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = v.isoformat()
            ride_list.append(rd)
        valid_dur = [r for r in ride_list if r.get("duration_s")]
        valid_hr  = [r for r in ride_list if r.get("avg_hr_bpm")]
        valid_spd = [r for r in ride_list if r.get("avg_speed_kmh")]
        mejor_tiempo = min(valid_dur, key=lambda x: x["duration_s"]) if valid_dur else None
        mejor_fc     = min(valid_hr,  key=lambda x: x["avg_hr_bpm"]) if valid_hr  else None
        mejor_vel    = max(valid_spd, key=lambda x: x["avg_speed_kmh"]) if valid_spd else None
        primera = ride_list[0]
        ultima  = ride_list[-1]
        evolucion = {}
        if primera.get("avg_hr_bpm") and ultima.get("avg_hr_bpm"):
            d = ultima["avg_hr_bpm"] - primera["avg_hr_bpm"]
            evolucion["fc_delta_bpm"] = round(d, 0)
            evolucion["fc_pct"] = round(d / primera["avg_hr_bpm"] * 100, 1)
        if primera.get("avg_speed_kmh") and ultima.get("avg_speed_kmh"):
            d = ultima["avg_speed_kmh"] - primera["avg_speed_kmh"]
            evolucion["vel_delta_kmh"] = round(d, 1)
            evolucion["vel_pct"] = round(d / primera["avg_speed_kmh"] * 100, 1)
        if primera.get("duration_s") and ultima.get("duration_s"):
            d = ultima["duration_s"] - primera["duration_s"]
            evolucion["tiempo_delta_s"] = d
            evolucion["tiempo_pct"] = round(d / primera["duration_s"] * 100, 1)
        return {"ruta": route, "total_veces": len(ride_list), "primera_vez": primera,
                "ultima_vez": ultima, "mejor_tiempo": mejor_tiempo,
                "mejor_fc": mejor_fc, "mejor_velocidad": mejor_vel, "evolucion": evolucion}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/weekly-report ────────────────────────────────────────────────────

@app.get("/gpt/weekly-report")
def gpt_weekly_report(sport: str = "cycling"):
    """Reporte semanal — km, horas, ascenso, eficiencia, mejor/peor sesión, vs semana anterior."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        def week_stats(offset_weeks=0):
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id, start_time, workout_name, distance_km, duration_s,
                           avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence
                    FROM sessions_clean_compat
                    WHERE sport = %s AND start_time IS NOT NULL
                      AND start_time::timestamp >= DATE_TRUNC('week', NOW()) - (%s * INTERVAL '1 week')
                      AND start_time::timestamp <  DATE_TRUNC('week', NOW()) - (%s * INTERVAL '1 week') + INTERVAL '1 week'
                    ORDER BY start_time ASC
                """, (sport, offset_weeks, offset_weeks))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
            sessions = []
            for r in rows:
                rd = dict(zip(cols, r))
                for k, v in rd.items():
                    if hasattr(v, "isoformat"): rd[k] = v.isoformat()
                sessions.append(rd)
            return sessions

        def summarize(sessions):
            if not sessions: return None
            km = sum(s.get("distance_km") or 0 for s in sessions)
            dur_s = sum(s.get("duration_s") or 0 for s in sessions)
            asc = sum(s.get("ascent_m") or 0 for s in sessions)
            hrs  = [s.get("avg_hr_bpm") for s in sessions if s.get("avg_hr_bpm")]
            spds = [s.get("avg_speed_kmh") for s in sessions if s.get("avg_speed_kmh")]
            fc_avg  = round(sum(hrs)/len(hrs), 0) if hrs else None
            spd_avg = round(sum(spds)/len(spds), 1) if spds else None
            eff = round(spd_avg/fc_avg*100, 2) if (spd_avg and fc_avg) else None
            return {"sesiones": len(sessions), "km_total": round(km,1),
                    "horas_total": round(dur_s/3600,2), "ascenso_total": round(asc,0),
                    "fc_promedio": fc_avg, "vel_promedio": spd_avg, "eficiencia": eff}

        esta = week_stats(0)
        ant  = week_stats(1)
        stats_esta = summarize(esta)
        stats_ant  = summarize(ant)
        mejor    = max(esta, key=lambda s: s.get("distance_km") or 0) if esta else None
        mas_dura = max(esta, key=lambda s: s.get("avg_hr_bpm") or 0) if esta else None
        comparativa = {}
        if stats_esta and stats_ant:
            for k in ("km_total", "horas_total", "eficiencia"):
                v_e = stats_esta.get(k) or 0
                v_a = stats_ant.get(k) or 0
                if v_a: comparativa[k+"_delta_pct"] = round((v_e-v_a)/v_a*100, 1)
        return {"sport": sport, "esta_semana": stats_esta, "semana_anterior": stats_ant,
                "mejor_sesion": mejor, "sesion_mas_dura": mas_dura, "comparativa": comparativa}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/athlete-status ───────────────────────────────────────────────────

@app.get("/gpt/athlete-status")
def gpt_athlete_status(sport: str = "cycling"):
    """Estado actual del atleta — fitness, fatiga, eficiencia aeróbica, cadencia, recomendación."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric,1) as km_2w,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric/4.0,1) as km_4w_avg,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_base,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks' THEN 1 END) as sesiones_2w
                FROM sessions_clean_compat WHERE sport = %s AND start_time IS NOT NULL
            """, (sport,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        km_2w = float(d.get("km_2w") or 0); km_4w_avg = float(d.get("km_4w_avg") or 0)
        fc_rec = float(d.get("fc_reciente") or 0); fc_base = float(d.get("fc_base") or 0)
        spd_rec = float(d.get("spd_reciente") or 0); spd_base = float(d.get("spd_base") or 0)
        cad_rec = float(d.get("cad_reciente") or 0); cad_base = float(d.get("cad_base") or 0)
        fitness = "estable"
        if km_4w_avg > 0:
            p = (km_2w - km_4w_avg) / km_4w_avg * 100
            if p > 10: fitness = "subiendo"
            elif p < -15: fitness = "bajando"
        fatiga = "baja"
        if fc_base > 0 and spd_base > 0:
            eff_rec  = spd_rec  / fc_rec  if fc_rec  else 0
            eff_base = spd_base / fc_base
            if eff_base > 0:
                ep = (eff_rec - eff_base) / eff_base * 100
                if ep < -5: fatiga = "alta"
                elif ep < -2: fatiga = "moderada"
        eff_rv = round(spd_rec/fc_rec*100, 2) if fc_rec else None
        eff_bv = round(spd_base/fc_base*100, 2) if fc_base else None
        eff_str = None
        if eff_rv and eff_bv:
            delta = round(eff_rv - eff_bv, 2)
            eff_str = f"+{delta}%" if delta >= 0 else f"{delta}%"
        cad_str = None
        if cad_rec and cad_base:
            delta = round(cad_rec - cad_base, 1)
            cad_str = f"+{delta} rpm" if delta >= 0 else f"{delta} rpm"
        rec = "continuar Z2"
        if fatiga == "alta": rec = "reducir intensidad, sesión de recuperación"
        elif fatiga == "moderada": rec = "mantener Z2, evitar Z4-Z5 esta semana"
        elif fitness == "subiendo" and fatiga == "baja": rec = "buena forma, puedes agregar una sesión tempo Z3"
        return {"sport": sport, "fitness": fitness, "fatiga": fatiga,
                "aerobic_efficiency": eff_str, "eficiencia_reciente": eff_rv,
                "cadence_trend": cad_str, "cadencia_reciente_rpm": cad_rec,
                "km_ultimas_2_semanas": km_2w, "sesiones_2_semanas": int(d.get("sesiones_2w") or 0),
                "recommendation": rec}
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# E29 — ADAPTIVE COACHING ENGINE
# Meta activa → semanas al evento → fase → prescripción + capacidad limitante
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/gpt/adaptive-coach")
def adaptive_coach():
    """
    E29 — Adaptive Coaching Engine.
    Combina meta activa (E26B) + capacidades (CE) + carga reciente → plan semanal.
    """
    from datetime import date as _date
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")

    # ── 1. Meta activa principal ─────────────────────────────────────────────
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, event_name, event_type, event_date,
                   distance_km, elevation_m, priority, notes
            FROM mars_goals
            WHERE status = 'active'
            ORDER BY priority ASC LIMIT 1
        """)
        gr = cur.fetchone()

    goal = None
    weeks_to_event = None
    phase = "base"

    if gr:
        goal = {
            "id": gr[0], "event_name": gr[1], "event_type": gr[2],
            "event_date": gr[3].isoformat() if gr[3] else None,
            "distance_km": gr[4], "elevation_m": gr[5], "notes": gr[7],
        }
        if gr[3]:
            days = (gr[3] - _date.today()).days
            weeks_to_event = max(0, days // 7)
            if weeks_to_event > 16:   phase = "base"
            elif weeks_to_event > 8:  phase = "build"
            elif weeks_to_event > 3:  phase = "peak"
            else:                     phase = "taper"

    # ── 2. Carga reciente (4 semanas) ────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                ROUND(SUM(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN COALESCE(distance_km,0) ELSE 0 END)::numeric/4, 1) as km_week,
                COUNT(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks' THEN 1 END)/4.0 as sess_week,
                ROUND(AVG(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN avg_hr_bpm END)::numeric, 0) as avg_hr,
                ROUND(AVG(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN avg_speed_kmh END)::numeric, 1) as avg_spd
            FROM sessions_clean_compat
            WHERE sport = 'cycling' AND start_time IS NOT NULL
        """)
        sr = cur.fetchone()

    km_week   = float(sr[0] or 0)
    sess_week = round(float(sr[1] or 0), 1)
    avg_hr    = float(sr[2] or 0)
    avg_spd   = float(sr[3] or 0)
    eff       = round(avg_spd / avg_hr * 100, 4) if avg_hr > 0 else 0

    # ── 3. Fase → prescripción ───────────────────────────────────────────────
    _PHASES = {
        "base":  {"label": "Base — volumen aeróbico Z2",
                  "km_mult": 1.10, "sess": 4, "z2_pct": 80,
                  "intensidad": "80% Z2 · 20% Z1",
                  "foco": "Volumen aeróbico, cadencia 95-100 rpm, salidas largas"},
        "build": {"label": "Build — intensidad sobre la base",
                  "km_mult": 1.05, "sess": 5, "z2_pct": 70,
                  "intensidad": "70% Z2 · 20% Z3 · 10% Z4",
                  "foco": "Tempo intervals, fuerza en subidas, sesiones de 2h+"},
        "peak":  {"label": "Peak — afinar forma pre-evento",
                  "km_mult": 0.85, "sess": 4, "z2_pct": 75,
                  "intensidad": "75% Z2 · 15% Z3 · 10% Z4-Z5",
                  "foco": "Simular ritmo del evento, calidad sobre volumen"},
        "taper": {"label": "Taper — descanso y activación",
                  "km_mult": 0.60, "sess": 3, "z2_pct": 80,
                  "intensidad": "80% Z1-Z2 · activaciones cortas",
                  "foco": "Piernas frescas, movilidad, hidratación y nutrición"},
    }
    ph = _PHASES[phase]
    prescription = {
        "fase":       phase,
        "fase_label": ph["label"],
        "km_objetivo":    round(km_week * ph["km_mult"], 0) if km_week > 0 else None,
        "sesiones":       ph["sess"],
        "z2_pct_objetivo": ph["z2_pct"],
        "intensidad":     ph["intensidad"],
        "foco_semana":    ph["foco"],
    }

    # ── 4. Capacidad limitante ───────────────────────────────────────────────
    limiting = None
    limiting_score = None
    try:
        from capability_engine import calculate_capabilities
        caps_data = calculate_capabilities(conn)
        caps = caps_data.get("capacidades", {})
        goal_type = (goal or {}).get("event_type", "cycling")
        relevance = {
            "cycling": ["motor_aerobico", "composicion_corporal", "escalada"],
            "gravel":  ["motor_aerobico", "escalada", "fuerza"],
            "running": ["motor_aerobico", "composicion_corporal", "recuperacion"],
            "climbing":["escalada", "fuerza", "motor_aerobico"],
        }.get(goal_type, ["motor_aerobico", "composicion_corporal"])

        best_gap, best_cap = 0, None
        for cname in relevance:
            cap = caps.get(cname, {})
            score = cap.get("score")  # None = sin datos, no confundir con 0
            if score is None:
                continue   # ignorar capacidades sin datos suficientes
            gap = 100 - float(score)
            if gap > best_gap:
                best_gap, best_cap = gap, cname
        if best_cap:
            limiting = best_cap
            limiting_score = round(float(caps.get(best_cap, {}).get("score") or 0), 1)
    except Exception:
        pass

    # ── 5. Mensaje integrador ────────────────────────────────────────────────
    if goal and weeks_to_event is not None:
        mensaje = (f"{goal['event_name']} en {weeks_to_event} semanas — fase {phase}. "
                   f"Objetivo: {prescription['km_objetivo']} km/sem, "
                   f"{ph['z2_pct']}% Z2.")
    elif goal:
        mensaje = f"Meta activa: {goal['event_name']}. Sin fecha — fase base por defecto."
    else:
        mensaje = "Sin metas activas. Agrega una meta en la sección Metas para coaching personalizado."

    return {
        "goal":             goal,
        "weeks_to_event":   weeks_to_event,
        "phase":            phase,
        "prescription":     prescription,
        "training_status":  {"km_semana": km_week, "sesiones_semana": sess_week,
                             "eficiencia": eff},
        "limiting_capability": limiting,
        "limiting_score":   limiting_score,
        "mensaje":          mensaje,
    }


# ── GET /gpt/fueling-log ──────────────────────────────────────────────────────

@app.get("/gpt/fueling-log")
def gpt_fueling_log(limit: int = 20):
    """Historial de nutrición post-sesión — geles, barras, agua, cafeína, CHO/hora, detalle de gel."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ps.session_id, s.start_time, s.distance_km, s.duration_s,
                       s.avg_hr_bpm, s.avg_speed_kmh,
                       ps.gels, ps.bars, ps.water_liters, ps.caffeine_mg,
                       ps.electrolytes, ps.digestion, ps.rpe, ps.notes,
                       ps.gel_type, ps.gel_recipe, ps.gel_carbs_g, ps.gel_sodium_mg,
                       ps.gel_timing, ps.gi_response, ps.energy_response
                FROM post_session ps
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
                WHERE ps.gels IS NOT NULL OR ps.bars IS NOT NULL OR ps.water_liters IS NOT NULL
                ORDER BY s.start_time DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        entries = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = v.isoformat()
            dur_h = (rd.get("duration_s") or 0) / 3600
            # Usar gel_carbs_g si existe, sino estimado por defecto
            gel_cho = (rd.get("gel_carbs_g") or 25) * (rd.get("gels") or 0)
            bar_cho = (rd.get("bars") or 0) * 40
            cho = gel_cho + bar_cho
            rd["cho_g"] = cho
            rd["cho_g_hora"] = round(cho/dur_h, 1) if dur_h > 0 else None
            entries.append(rd)
        avg_gels  = round(sum(e.get("gels") or 0 for e in entries)/len(entries), 1) if entries else None
        avg_water = round(sum(e.get("water_liters") or 0 for e in entries)/len(entries), 2) if entries else None
        return {"registros": len(entries), "promedio_geles_por_sesion": avg_gels,
                "promedio_agua_litros": avg_water, "historial": entries}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/gel-tests ────────────────────────────────────────────────────────

@app.get("/gpt/gel-tests")
def gpt_gel_tests():
    """Comparativa de geles — qué tipo funcionó mejor por respuesta GI y energética."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ps.gel_type, ps.gel_recipe, ps.gel_carbs_g, ps.gel_sodium_mg,
                       ps.gel_timing, ps.gi_response, ps.energy_response,
                       ps.rpe, s.distance_km, s.duration_s, s.avg_hr_bpm,
                       s.avg_speed_kmh, s.start_time, ps.session_id
                FROM post_session ps
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
                WHERE ps.gel_type IS NOT NULL
                ORDER BY s.start_time DESC
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        tests = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = v.isoformat()
            tests.append(rd)

        # Agrupar por tipo de gel
        by_type = {}
        for t in tests:
            gt = t.get("gel_type") or "desconocido"
            if gt not in by_type:
                by_type[gt] = {"uses": 0, "gi_ok": 0, "gi_issues": 0, "energy_good": 0, "energy_bad": 0, "sessions": []}
            by_type[gt]["uses"] += 1
            gi = t.get("gi_response") or ""
            if "problemas" in gi or gi == "":
                by_type[gt]["gi_ok"] += 1
            else:
                by_type[gt]["gi_issues"] += 1
            en = t.get("energy_response") or ""
            if "estable" in en or "gradual" in en:
                by_type[gt]["energy_good"] += 1
            elif "caída" in en or "negativo" in en or "sin efecto" in en:
                by_type[gt]["energy_bad"] += 1
            by_type[gt]["sessions"].append({
                "fecha": t.get("start_time","")[:10],
                "distancia": t.get("distance_km"),
                "gi": gi,
                "energia": en,
                "rpe": t.get("rpe")
            })

        # Score simple por tipo (mayor = mejor)
        resumen = []
        for gt, data in by_type.items():
            uses = data["uses"]
            gi_score = round(data["gi_ok"] / uses * 100) if uses else 0
            en_score = round(data["energy_good"] / uses * 100) if uses else 0
            score = round((gi_score + en_score) / 2)
            resumen.append({
                "gel_type": gt,
                "usos": uses,
                "gi_ok_pct": gi_score,
                "energia_ok_pct": en_score,
                "score": score,
                "recomendacion": "✅ Seguir usando" if score >= 70 else ("⚠️ Revisar" if score >= 40 else "❌ Evitar"),
                "historial": data["sessions"]
            })
        resumen.sort(key=lambda x: x["score"], reverse=True)

        return {
            "total_pruebas": len(tests),
            "tipos_probados": len(by_type),
            "ranking": resumen
        }
    except Exception as e:
        raise HTTPException(500, str(e))



# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 5 — Perfil del Atleta, Tests, Weight Trend, Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class AthleteProfileIn(BaseModel):
    age: Optional[int] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[int] = None
    hr_rest: Optional[int] = None
    hr_max: Optional[int] = None
    hr_lt: Optional[int] = None
    ftp_watts: Optional[int] = None
    vo2max: Optional[float] = None
    active_bike_id: Optional[str] = None
    goals: Optional[List[str]] = None
    injuries: Optional[List[str]] = None
    notes: Optional[str] = None

class AthleteTestIn(BaseModel):
    date: str
    type: str  # hr_drift, ftp, subida_referencia, cadencia, vo2max
    result_value: Optional[float] = None
    result_unit: Optional[str] = None
    route_id: Optional[str] = None
    duration_s: Optional[int] = None
    avg_hr_bpm: Optional[int] = None
    avg_speed_kmh: Optional[float] = None
    avg_cadence: Optional[int] = None
    conditions: Optional[str] = None
    notes: Optional[str] = None
    raw_data: Optional[dict] = None


# ── GET/POST /gpt/athlete-profile ────────────────────────────────────────────

@app.get("/gpt/athlete-profile")
def get_athlete_profile():
    """Perfil centralizado del atleta — edad, peso, FC, FTP, zonas, objetivos, lesiones."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM athlete_profile ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                # Retorna perfil base de Mars con lo que ya sabemos
                return {
                    "name": "Mars",
                    "age": 47,
                    "hr_lt": 168,
                    "zones": {
                        "Z1": "0-108 bpm",
                        "Z2": "134-150 bpm (Aeróbico)",
                        "Z3": "151-160 bpm (Tempo)",
                        "Z4": "161-168 bpm (Umbral)",
                        "Z5": "169+ bpm (Máximo)"
                    },
                    "active_bike": "Orbea Aluminum Road 2020 matte black",
                    "nota": "Perfil no guardado aún — usar POST /gpt/athlete-profile para inicializarlo"
                }
            cols = [d[0] for d in cur.description]
        profile = dict(zip(cols, row))
        for k, v in profile.items():
            if hasattr(v, "isoformat"):
                profile[k] = v.isoformat()
        # Agregar zonas derivadas del hr_lt
        lt = profile.get("hr_lt") or 168
        profile["zones"] = {
            "Z1": f"0-{lt-60} bpm (Recuperación)",
            "Z2": f"{lt-34}-{lt-18} bpm (Aeróbico)",
            "Z3": f"{lt-17}-{lt-8} bpm (Tempo)",
            "Z4": f"{lt-7}-{lt} bpm (Umbral)",
            "Z5": f"{lt+1}+ bpm (Máximo)"
        }
        return profile
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/gpt/athlete-profile")
def save_athlete_profile(body: AthleteProfileIn):
    """Guarda o actualiza el perfil del atleta."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM athlete_profile LIMIT 1")
            exists = cur.fetchone()
            if exists:
                fields = {k: v for k, v in body.dict().items() if v is not None}
                if not fields:
                    raise HTTPException(400, "Sin campos para actualizar")
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                set_clause += ", updated_at=NOW()"
                cur.execute(f"UPDATE athlete_profile SET {set_clause} WHERE id=%s",
                            list(fields.values()) + [exists[0]])
                return {"ok": True, "action": "updated", "fields": list(fields.keys())}
            else:
                cur.execute("""
                    INSERT INTO athlete_profile
                        (name, age, weight_kg, height_cm, hr_rest, hr_max, hr_lt,
                         ftp_watts, vo2max, active_bike_id, goals, injuries, notes, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, ("Mars", body.age, body.weight_kg, body.height_cm,
                      body.hr_rest, body.hr_max, body.hr_lt or 168,
                      body.ftp_watts, body.vo2max, body.active_bike_id,
                      body.goals, body.injuries, body.notes))
                return {"ok": True, "action": "created"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/weight-trend ─────────────────────────────────────────────────────

@app.get("/gpt/weight-trend")
def gpt_weight_trend(limit: int = 30):
    """Historial de peso desde registros post-sesión — tendencia y delta total."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ps.session_id, s.start_time,
                       ps.weight_before, ps.weight_after,
                       ps.sweat_rate, s.distance_km, s.duration_s
                FROM post_session ps
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
                WHERE ps.weight_before IS NOT NULL
                ORDER BY s.start_time DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        entries = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"):
                    rd[k] = v.isoformat()
            entries.append(rd)

        # Invertir para orden cronológico
        entries = list(reversed(entries))

        tendencia = "sin datos"
        delta_kg = None
        if len(entries) >= 2:
            w_first = float(entries[0].get("weight_before") or 0)
            w_last  = float(entries[-1].get("weight_before") or 0)
            if w_first and w_last:
                delta_kg = round(w_last - w_first, 2)
                tendencia = f"{'+' if delta_kg > 0 else ''}{delta_kg} kg desde primera medición"

        # Promedio últimas 4 semanas
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(ps.weight_before)::numeric, 2)
                FROM post_session ps
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
                WHERE ps.weight_before IS NOT NULL
                  AND s.start_time::timestamp >= NOW() - INTERVAL '4 weeks'
            """)
            avg_row = cur.fetchone()

        return {
            "registros": len(entries),
            "tendencia": tendencia,
            "delta_kg_total": delta_kg,
            "peso_promedio_4_semanas": float(avg_row[0]) if avg_row and avg_row[0] else None,
            "historial": entries
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET/POST /gpt/tests ───────────────────────────────────────────────────────

@app.post("/gpt/tests")
def save_test(body: AthleteTestIn):
    """Guarda un test de rendimiento (HR Drift, FTP, subida referencia, etc.)."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_tests
                    (date, type, result_value, result_unit, route_id, duration_s,
                     avg_hr_bpm, avg_speed_kmh, avg_cadence, conditions, notes, raw_data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.date, body.type, body.result_value, body.result_unit,
                  body.route_id, body.duration_s, body.avg_hr_bpm,
                  body.avg_speed_kmh, body.avg_cadence, body.conditions,
                  body.notes, json.dumps(body.raw_data) if body.raw_data else None))
            test_id = cur.fetchone()[0]
        return {"ok": True, "id": test_id}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/tests")
def get_tests(type: Optional[str] = None, limit: int = 20):
    """Lista tests de rendimiento con comparativa entre fechas."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM athlete_tests WHERE 1=1"
        params = []
        if type:
            query += " AND type = %s"
            params.append(type)
        query += " ORDER BY date DESC LIMIT %s"
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        tests = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"):
                    rd[k] = v.isoformat()
            tests.append(rd)

        # Agrupar por tipo para comparativa
        by_type = {}
        for t in reversed(tests):  # cronológico
            tp = t["type"]
            if tp not in by_type:
                by_type[tp] = []
            by_type[tp].append(t)

        comparativa = {}
        for tp, items in by_type.items():
            if len(items) >= 2 and items[0].get("result_value") and items[-1].get("result_value"):
                delta = round(float(items[-1]["result_value"]) - float(items[0]["result_value"]), 3)
                comparativa[tp] = {
                    "primer_test": items[0]["date"],
                    "ultimo_test": items[-1]["date"],
                    "veces": len(items),
                    "delta": f"{'+' if delta > 0 else ''}{delta} {items[0].get('result_unit','')}"
                }

        return {"total": len(tests), "comparativa": comparativa, "tests": tests}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/dashboard ────────────────────────────────────────────────────────

@app.get("/gpt/dashboard")
def gpt_dashboard(sport: str = "cycling"):
    """Dashboard completo — una sola llamada para el estado actual de Mars."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        result = {}

        # 1. Athlete status (fitness/fatigue/efficiency/cadence)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric,1) as km_2w,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(duration_s,0) ELSE 0 END)::numeric/3600,2) as hrs_2w,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric/4,1) as km_4w_avg,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_rec,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_rec,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_rec,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_base,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '7 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric, 1) as atl_7d,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '42 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric / 6.0, 1) as ctl_42d,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '1 week' THEN 1 END) as ses_7d,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks' THEN 1 END) as ses_2w
                FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
            """, (sport,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))

        km_2w = float(d.get("km_2w") or 0)
        km_4w  = float(d.get("km_4w_avg") or 0)
        fc_rec  = float(d.get("fc_rec") or 0)
        fc_base = float(d.get("fc_base") or 0)
        spd_rec  = float(d.get("spd_rec") or 0)
        spd_base = float(d.get("spd_base") or 0)
        cad_rec  = float(d.get("cad_rec") or 0)
        cad_base = float(d.get("cad_base") or 0)

        fitness = "estable"
        if km_4w > 0:
            p = (km_2w - km_4w) / km_4w * 100
            fitness = "subiendo" if p > 10 else ("bajando" if p < -15 else "estable")

        fatiga = "baja"
        if fc_base > 0 and spd_base > 0:
            er = spd_rec / fc_rec if fc_rec else 0
            eb = spd_base / fc_base
            if eb > 0:
                ep = (er - eb) / eb * 100
                fatiga = "alta" if ep < -5 else ("moderada" if ep < -2 else "baja")

        eff_rec  = round(spd_rec  / fc_rec  * 100, 2) if fc_rec  else None
        eff_base = round(spd_base / fc_base * 100, 2) if fc_base else None
        eff_delta = None
        if eff_rec and eff_base:
            d2 = round(eff_rec - eff_base, 2)
            eff_delta = f"{'+' if d2 >= 0 else ''}{d2}%"

        cad_delta = None
        if cad_rec and cad_base:
            d2 = round(cad_rec - cad_base, 1)
            cad_delta = f"{'+' if d2 >= 0 else ''}{d2} rpm"

        # Mars Index = eficiencia normalizada por ascenso implícito
        # Formula: (vel/fc*100) * (1 + sesiones_2w*0.02) — simple, sin temperatura aún
        mars_index = None
        if eff_rec:
            mars_index = round(eff_rec * (1 + int(d.get("ses_2w") or 0) * 0.02), 2)

        atl = float(d.get("atl_7d") or 0)
        ctl = float(d.get("ctl_42d") or 0)
        tsb = round(ctl - atl, 1)
        carga_estado = "fresco" if tsb > 5 else ("recuperar" if tsb < -15 else "en carga")

        result["athlete"] = {
            "fitness": fitness,
            "fatiga": fatiga,
            "eficiencia": eff_delta,
            "cadencia": cad_delta,
            "km_2_semanas": km_2w,
            "horas_2_semanas": float(d.get("hrs_2w") or 0),
            "sesiones_7_dias": int(d.get("ses_7d") or 0),
            "mars_index": mars_index
        }
        result["carga"] = {
            "atl": atl,
            "ctl": ctl,
            "tsb": tsb,
            "estado": carga_estado,
            "nota": "Estimación por duración y FC; se refina cuando tengamos potencia."
        }

        # 2. Peso reciente
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ps.weight_before, s.start_time
                FROM post_session ps JOIN sessions_clean_compat s ON ps.session_id=s.session_id
                WHERE ps.weight_before IS NOT NULL
                ORDER BY s.start_time DESC LIMIT 1
            """)
            w_row = cur.fetchone()
        result["peso_reciente"] = {
            "kg": float(w_row[0]) if w_row else None,
            "fecha": w_row[1].isoformat() if w_row and hasattr(w_row[1], "isoformat") else None
        }

        # 3. Reporte semanal
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as ses,
                       ROUND(SUM(distance_km)::numeric,1) as km,
                       ROUND(SUM(COALESCE(duration_s,0))::numeric/3600,2) as hrs,
                       ROUND(SUM(ascent_m)::numeric,0) as asc_m
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= DATE_TRUNC('week', NOW())
            """, (sport,))
            wk = cur.fetchone()
        weekly_calories = 0
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(calories),0)
                    FROM clean_sessions
                    WHERE sport=%s AND start_time IS NOT NULL
                      AND start_time >= DATE_TRUNC('week', NOW())
                """, (sport,))
                weekly_calories = int(cur.fetchone()[0] or 0)
        except Exception:
            weekly_calories = 0
        result["semana_actual"] = {
            "sesiones": int(wk[0] or 0),
            "km": float(wk[1] or 0),
            "horas": float(wk[2] or 0),
            "ascenso_m": int(wk[3] or 0),
            "calorias": weekly_calories
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE calories IS NOT NULL) AS with_calories,
                        COALESCE(SUM(calories),0) AS total_calories,
                        ROUND(AVG(CASE WHEN distance_km > 0 AND calories IS NOT NULL THEN calories / distance_km END)::numeric,1) AS kcal_per_km,
                        MAX(calories) AS max_session_calories
                    FROM clean_sessions
                    WHERE sport=%s AND start_time IS NOT NULL
                """, (sport,))
                cal_row = cur.fetchone()
            kcal_per_km = float(cal_row[2] or 0)
            result["calorias_audit"] = {
                "sesiones_con_calorias": int(cal_row[0] or 0),
                "calorias_historicas": int(cal_row[1] or 0),
                "kcal_por_km_promedio": kcal_per_km,
                "max_calorias_sesion": int(cal_row[3] or 0),
                "validacion": "ok" if kcal_per_km >= 12 else "revisar_posible_division_por_10",
                "nota": "En ciclismo, un promedio muy bajo de kcal/km seria senal de calorias divididas por 10."
            }
        except Exception:
            result["calorias_audit"] = {"validacion": "no_disponible"}

        # 4. Próximo mantenimiento (gear más cercano al límite)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.name, g.km_limit,
                       COALESCE((SELECT MAX(km_at_service) FROM maintenance m WHERE m.gear_id=g.gear_id),
                                g.km_at_install, 0) as last_km,
                       g.km_at_install
                FROM gear g
                WHERE g.retired_date IS NULL AND g.km_limit IS NOT NULL
                ORDER BY (COALESCE((SELECT MAX(km_at_service) FROM maintenance m2 WHERE m2.gear_id=g.gear_id),
                                   g.km_at_install,0) - g.km_at_install)::float / g.km_limit DESC
                LIMIT 1
            """)
            gear_row = cur.fetchone()
        if gear_row:
            km_used = max(0, (gear_row[2] or 0) - (gear_row[3] or 0))
            pct = round(km_used / gear_row[1] * 100, 1) if gear_row[1] else 0
            result["proximo_mantenimiento"] = {
                "componente": gear_row[0],
                "pct_usado": pct,
                "km_restantes": max(0, gear_row[1] - km_used),
                "status": "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            }
        else:
            result["proximo_mantenimiento"] = None

        # 5. Z2 check últimas 4 semanas
        with conn.cursor() as cur:
            cur.execute("""
                SELECT result_json FROM sessions_clean_compat
                WHERE sport=%s AND result_json IS NOT NULL
                  AND start_time::timestamp >= NOW()-INTERVAL '4 weeks'
            """, (sport,))
            rj_rows = cur.fetchall()
        z2_min = 0; total_min = 0
        for (rj_str,) in rj_rows:
            try:
                rj = json.loads(rj_str)
                for z in rj.get("zones", []):
                    m = float(z.get("minutes", 0))
                    total_min += m
                    if "Z2" in z.get("name","") or "Aeróbico" in z.get("name",""):
                        z2_min += m
            except Exception:
                continue
        result["z2_check"] = {
            "pct_z2_4_semanas": round(z2_min/total_min*100, 1) if total_min else None,
            "nota": "Óptimo: 70-80%"
        }

        # 6. Recomendación
        rec = "continuar Z2"
        if fatiga == "alta":
            rec = "reducir intensidad — sesión de recuperación activa"
        elif fatiga == "moderada":
            rec = "mantener Z2, evitar Z4-Z5"
        elif fitness == "subiendo" and fatiga == "baja":
            rec = "buena forma — puedes agregar una sesión tempo Z3"
        elif result["semana_actual"]["sesiones"] == 0:
            rec = "sin sesiones esta semana — retomar entrenamiento"
        result["recommendation"] = rec

        return result
    except Exception as e:
        raise HTTPException(500, str(e))




# ── GET /gpt/historical-progress ─────────────────────────────────────────────

@app.get("/gpt/historical-progress")
def gpt_historical_progress(sport: str = "cycling", months: int = 12):
    """Progreso mensual histórico con eficiencia aeróbica real (km/h / bpm)."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('month', start_time::timestamp), 'YYYY-MM') as mes,
                    COUNT(*) as sesiones,
                    ROUND(SUM(distance_km)::numeric, 1) as km_total,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric/3600, 1) as horas_total,
                    ROUND(SUM(COALESCE(ascent_m,0))::numeric, 0) as ascenso_total,
                    ROUND(AVG(avg_hr_bpm)::numeric, 1) as fc_promedio,
                    ROUND(AVG(avg_speed_kmh)::numeric, 2) as vel_promedio,
                    ROUND(AVG(avg_cadence)::numeric, 1) as cadencia_promedio,
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm)
                        ELSE 0 END)::numeric, 4) as eficiencia_ratio
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 month')
                GROUP BY mes ORDER BY mes ASC
            """, (sport, months))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]

        # Línea base = primer mes con datos suficientes (min 3 sesiones)
        baseline = next((m for m in data if (m.get("sesiones") or 0) >= 3), None)
        baseline_month = baseline["mes"] if baseline else None

        # Comparar cada mes vs línea base
        for m in data:
            if baseline and m["mes"] != baseline_month:
                if baseline.get("fc_promedio") and m.get("fc_promedio"):
                    m["fc_delta"] = round(float(m["fc_promedio"]) - float(baseline["fc_promedio"]), 1)
                if baseline.get("vel_promedio") and m.get("vel_promedio"):
                    m["vel_delta"] = round(float(m["vel_promedio"]) - float(baseline["vel_promedio"]), 2)
                if baseline.get("cadencia_promedio") and m.get("cadencia_promedio"):
                    m["cadencia_delta"] = round(float(m["cadencia_promedio"]) - float(baseline["cadencia_promedio"]), 1)
                if baseline.get("eficiencia_ratio") and m.get("eficiencia_ratio"):
                    b = float(baseline["eficiencia_ratio"])
                    c = float(m["eficiencia_ratio"])
                    m["eficiencia_pct"] = round((c - b) / b * 100, 1) if b else None

        # Último mes vs línea base como resumen
        ultimo = data[-1] if data else {}
        resumen = {}
        if baseline and ultimo and ultimo.get("mes") != baseline_month:
            resumen["baseline_month"] = baseline_month
            resumen["fc_delta"] = ultimo.get("fc_delta")
            resumen["vel_delta"] = ultimo.get("vel_delta")
            resumen["cadencia_delta"] = ultimo.get("cadencia_delta")
            resumen["eficiencia_pct"] = ultimo.get("eficiencia_pct")

        return {"sport": sport, "months": months, "baseline_month": baseline_month,
                "resumen_vs_baseline": resumen, "data": data}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/month-compare ────────────────────────────────────────────────────

@app.get("/gpt/month-compare")
def gpt_month_compare(sport: str = "cycling", month_a: str = None, month_b: str = None):
    """Compara dos meses específicos en todas las métricas. Default: mes actual vs mayo 2026."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    today = date.today()
    if not month_a:
        month_a = "2026-05"  # Línea base Mars
    if not month_b:
        month_b = f"{today.year}-{today.month:02d}"
    try:
        def get_month_stats(month_str):
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) as sesiones,
                           ROUND(SUM(distance_km)::numeric,1) as km_total,
                           ROUND(SUM(COALESCE(duration_s,0))::numeric/3600,1) as horas_total,
                           ROUND(SUM(COALESCE(ascent_m,0))::numeric,0) as ascenso_total,
                           ROUND(AVG(avg_hr_bpm)::numeric,1) as fc_promedio,
                           ROUND(AVG(avg_speed_kmh)::numeric,2) as vel_promedio,
                           ROUND(AVG(avg_cadence)::numeric,1) as cadencia_promedio,
                           ROUND((CASE WHEN AVG(avg_hr_bpm)>0
                               THEN AVG(avg_speed_kmh)/AVG(avg_hr_bpm)
                               ELSE 0 END)::numeric,4) as eficiencia_ratio
                    FROM sessions_clean_compat
                    WHERE sport=%s AND LEFT(start_time,7)=%s AND start_time IS NOT NULL
                """, (sport, month_str))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

        stats_a = get_month_stats(month_a)
        stats_b = get_month_stats(month_b)

        # Deltas
        deltas = {}
        for k in ("fc_promedio", "vel_promedio", "cadencia_promedio", "km_total", "horas_total", "eficiencia_ratio"):
            va = float(stats_a.get(k) or 0)
            vb = float(stats_b.get(k) or 0)
            if va:
                deltas[k + "_delta"] = round(vb - va, 2)
                deltas[k + "_pct"] = round((vb - va) / va * 100, 1)

        # Señal clave Mars: ¿20-21 km/h con menos de 140 bpm?
        aerobic_signal = None
        if stats_b.get("vel_promedio") and stats_b.get("fc_promedio"):
            v = float(stats_b["vel_promedio"])
            fc = float(stats_b["fc_promedio"])
            if 20 <= v <= 22 and fc < 140:
                aerobic_signal = "✅ Motor aeróbico mejorando: %.1f km/h con %.0f bpm" % (v, fc)
            elif v >= 20:
                aerobic_signal = "⚠️ %.1f km/h pero FC aún en %.0f bpm — seguir en Z2" % (v, fc)

        return {
            "sport": sport,
            "mes_base": month_a,
            "mes_actual": month_b,
            "stats_base": stats_a,
            "stats_actual": stats_b,
            "deltas": deltas,
            "senal_aerobica": aerobic_signal
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/fitness-timeline ─────────────────────────────────────────────────

@app.get("/gpt/fitness-timeline")
def gpt_fitness_timeline(sport: str = "cycling"):
    """Timeline de fitness: eficiencia aeróbica mensual normalizada 0-100."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('month', start_time::timestamp), 'YYYY-MM') as mes,
                    COUNT(*) as sesiones,
                    ROUND(AVG(avg_hr_bpm)::numeric,1) as fc_promedio,
                    ROUND(AVG(avg_speed_kmh)::numeric,2) as vel_promedio,
                    ROUND(AVG(avg_cadence)::numeric,1) as cadencia_promedio,
                    ROUND((AVG(avg_speed_kmh)/NULLIF(AVG(avg_hr_bpm),0))::numeric,4) as eff_ratio
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL AND avg_hr_bpm > 0
                GROUP BY mes ORDER BY mes ASC
            """, (sport,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]

        # Normalizar eficiencia 0-100
        ratios = [float(m["eff_ratio"]) for m in data if m.get("eff_ratio")]
        if ratios:
            min_r, max_r = min(ratios), max(ratios)
            rng = max_r - min_r or 1
            for m in data:
                if m.get("eff_ratio"):
                    m["fitness_score"] = round((float(m["eff_ratio"]) - min_r) / rng * 100, 1)

        # Indicador global Mars (últimos 2 meses)
        recientes = [m for m in data if m.get("sesiones", 0) >= 2][-2:]
        indicadores = {}
        if recientes:
            last = recientes[-1]
            fc = float(last.get("fc_promedio") or 0)
            vel = float(last.get("vel_promedio") or 0)
            cad = float(last.get("cadencia_promedio") or 0)
            fs = float(last.get("fitness_score") or 0)

            indicadores["motor_aerobico"] = min(100, round(fs))
            indicadores["cadencia"] = min(100, round((cad - 60) / (95 - 60) * 100)) if cad > 60 else 0
            indicadores["consistencia"] = min(100, round(float(last.get("sesiones", 0)) / 12 * 100))
            indicadores["tendencia"] = "mejorando" if len(recientes) == 2 and float(recientes[-1].get("eff_ratio") or 0) > float(recientes[-2].get("eff_ratio") or 0) else "estable"

        return {"sport": sport, "timeline": data, "indicadores_mars": indicadores}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/athletic-history")
def gpt_athletic_history():
    """Historia atlética completa desde clean_sessions: todos los deportes vivos."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS sesiones,
                    COUNT(*) FILTER (WHERE avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0) AS con_fc,
                    COUNT(*) FILTER (WHERE distance_km IS NOT NULL AND distance_km > 0) AS con_distancia,
                    COUNT(*) FILTER (WHERE calories IS NOT NULL AND calories > 0) AS con_calorias,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(SUM(COALESCE(calories,0))::numeric, 0) AS calorias,
                    MIN(start_time) AS desde,
                    MAX(start_time) AS hasta
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
            """)
            totals = dict(zip([d[0] for d in cur.description], cur.fetchone()))

            cur.execute("""
                SELECT
                    CASE
                        WHEN sport IN ('running','trail_running','treadmill_running') THEN 'running'
                        WHEN sport IN ('cycling','indoor_cycling') THEN 'cycling'
                        WHEN sport IN ('indoor_cardio') THEN 'indoor_cardio'
                        WHEN sport IN ('strength_training') THEN 'strength'
                        WHEN sport IN ('lap_swimming','open_water_swimming') THEN 'swimming'
                        WHEN sport IN ('walking') THEN 'walking'
                        WHEN sport IN ('yoga') THEN 'mobility'
                        WHEN sport IN ('multi_sport','bikeToRunTransition_v2') THEN 'multi_sport'
                        ELSE 'other'
                    END AS grupo,
                    COUNT(*) AS sesiones,
                    COUNT(*) FILTER (WHERE avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0) AS con_fc,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(AVG(NULLIF(avg_hr_bpm,0))::numeric, 1) AS fc_promedio,
                    ROUND(AVG(NULLIF(avg_speed_kmh,0))::numeric, 2) AS velocidad_promedio,
                    ROUND((AVG(NULLIF(avg_speed_kmh,0))/NULLIF(AVG(NULLIF(avg_hr_bpm,0)),0))::numeric, 4) AS eficiencia,
                    MIN(start_time) AS desde,
                    MAX(start_time) AS hasta
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY grupo
                ORDER BY sesiones DESC
            """)
            by_group = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

            cur.execute("""
                SELECT
                    EXTRACT(YEAR FROM start_time::timestamp)::int AS year,
                    CASE
                        WHEN sport IN ('running','trail_running','treadmill_running') THEN 'running'
                        WHEN sport IN ('cycling','indoor_cycling') THEN 'cycling'
                        WHEN sport IN ('indoor_cardio') THEN 'indoor_cardio'
                        WHEN sport IN ('strength_training') THEN 'strength'
                        WHEN sport IN ('lap_swimming','open_water_swimming') THEN 'swimming'
                        WHEN sport IN ('walking') THEN 'walking'
                        WHEN sport IN ('yoga') THEN 'mobility'
                        WHEN sport IN ('multi_sport','bikeToRunTransition_v2') THEN 'multi_sport'
                        ELSE 'other'
                    END AS grupo,
                    COUNT(*) AS sesiones,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(AVG(NULLIF(avg_hr_bpm,0))::numeric, 1) AS fc_promedio,
                    ROUND(AVG(NULLIF(avg_speed_kmh,0))::numeric, 2) AS velocidad_promedio,
                    ROUND((AVG(NULLIF(avg_speed_kmh,0))/NULLIF(AVG(NULLIF(avg_hr_bpm,0)),0))::numeric, 4) AS eficiencia
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY year, grupo
                ORDER BY year ASC, grupo ASC
            """)
            yearly = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

            cur.execute("""
                SELECT sport, COUNT(*) AS sesiones
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY sport
                ORDER BY sesiones DESC
            """)
            raw_sports = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        def clean_value(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            try:
                import decimal
                if isinstance(v, decimal.Decimal):
                    return float(v)
            except Exception:
                pass
            return v

        def clean_obj(obj):
            return {k: clean_value(v) for k, v in obj.items()}

        by_group = [clean_obj(x) for x in by_group]
        yearly = [clean_obj(x) for x in yearly]
        raw_sports = [clean_obj(x) for x in raw_sports]
        totals = clean_obj(totals)

        running = next((x for x in by_group if x.get("grupo") == "running"), {})
        cycling = next((x for x in by_group if x.get("grupo") == "cycling"), {})
        swimming = next((x for x in by_group if x.get("grupo") == "swimming"), {})
        strength = next((x for x in by_group if x.get("grupo") == "strength"), {})
        story = [
            "2018-2020: base amplia con running, caminatas, ciclismo, cardio indoor y primeras transiciones.",
            "2021-2022: aparecen bloques de natación, movilidad/yoga y menor volumen de carrera.",
            "2023-2026: ciclismo y fuerza toman más peso, con clean_sessions como índice maestro vivo.",
        ]
        return {
            "ok": True,
            "totals": totals,
            "by_group": by_group,
            "raw_sports": raw_sports,
            "yearly": yearly,
            "running": running,
            "cycling": cycling,
            "swimming": swimming,
            "strength": strength,
            "story": story,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/capacidades")
def gpt_capacidades():
    """Seis capacidades personales con score, confianza, anclas y limitantes."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import calculate_capabilities

        return calculate_capabilities(conn)
    except Exception as e:
        logger.error(f"Capability engine error: {e}")
        raise HTTPException(500, str(e))


def _normalize_capability_name(nombre: str) -> str:
    normalized = nombre.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "aerobico": "motor_aerobico",
        "motor": "motor_aerobico",
        "composicion": "composicion_corporal",
        "nutricion": "nutricion_deportiva",
    }
    return aliases.get(normalized, normalized)


def _find_capability(result: dict, nombre: str) -> dict:
    normalized = _normalize_capability_name(nombre)
    capability = next(
        (item for item in result["capabilities"] if item["key"] == normalized),
        None,
    )
    if not capability:
        raise HTTPException(404, "Capacidad no encontrada")
    return capability


def _previous_capability_run(conn, capability_key: str) -> Optional[dict]:
    _ensure_capability_runs_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT capability_json
            FROM capability_runs
            WHERE capability_key = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (capability_key,))
        row = cur.fetchone()
    return row[0] if row else None


@app.get("/gpt/capacidad/{nombre}/history")
def gpt_capability_history(nombre: str):
    """Annual capability history built from sustainable 12-week blocks."""
    normalized = _normalize_capability_name(nombre)
    _HISTORY_SUPPORTED = {"motor_aerobico", "composicion_corporal", "escalada"}
    if normalized not in _HISTORY_SUPPORTED:
        raise HTTPException(
            501,
            "Esta capacidad todavía no tiene historia anual implementada.",
        )
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        if normalized == "composicion_corporal":
            from capability_engine import calculate_body_composition_history
            return calculate_body_composition_history(conn)
        if normalized == "escalada":
            from capability_engine import calculate_climbing_history
            return calculate_climbing_history(conn)
        from capability_engine import calculate_aerobic_history
        return calculate_aerobic_history(conn)
    except Exception as e:
        logger.error(f"Capability history error: {e}")
        raise HTTPException(500, str(e))


@app.get("/gpt/readiness")
def gpt_readiness(evento: str = "escalera_al_infierno"):
    """Readiness score for a specific event based on current capability scores."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import calculate_readiness
        return calculate_readiness(conn, evento)
    except Exception as e:
        logger.error(f"Readiness error: {e}")
        raise HTTPException(500, str(e))


@app.get("/gpt/readiness/eventos")
def gpt_readiness_eventos():
    """List all supported readiness events."""
    from capability_engine import READINESS_EVENTS
    return {
        "eventos": [
            {"key": k, "nombre": v["nombre"], "tipo": v["tipo"], "descripcion": v["descripcion"]}
            for k, v in READINESS_EVENTS.items()
        ]
    }


@app.get("/gpt/patron-historico")
def gpt_patron_historico(top_n: int = 5):
    """Find historical periods most similar to current fitness state and classify their trajectories."""
    if top_n < 1 or top_n > 10:
        raise HTTPException(400, "top_n debe estar entre 1 y 10")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from capability_engine import calculate_historical_similarity
    return calculate_historical_similarity(conn)


@app.get("/gpt/academia/{key}")
def gpt_academia(key: str):
    """Return educational content for a capability key, glossary term, or 'glosario' for all terms."""
    from capability_engine import academia
    return academia(key)


@app.get("/gpt/lt-detect")
def gpt_lt_detect(sport: str = "cycling"):
    """Estimate lactate threshold from historical session data (diagnostic, read-only)."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport debe ser 'cycling' o 'running'")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import lt_detect_from_records
        return lt_detect_from_records(conn, sport=sport)
    except Exception as e:
        logger.error(f"LT detect error: {e}")
        raise HTTPException(500, str(e))


@app.get("/gpt/capacidad/{nombre}/validation")
def gpt_capability_validation(nombre: str):
    """Audit arithmetic, anchors and indicator changes without writing data."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import calculate_capabilities, validate_capability

        result = calculate_capabilities(conn)
        capability = _find_capability(result, nombre)
        previous = _previous_capability_run(conn, capability["key"])
        return {
            "ok": True,
            "capability": capability,
            "validation": validate_capability(capability, previous),
            "recorded": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Capability validation error: {e}")
        raise HTTPException(500, str(e))


@app.post("/admin/capability-validation/{nombre}")
def admin_capability_validation(
    nombre: str,
    execute: bool = False,
    token: str = None,
):
    """Validate a capability and optionally record the official execution."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token or token != admin_token:
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import calculate_capabilities, validate_capability
        from psycopg2.extras import Json

        _ensure_capability_runs_table(conn)
        result = calculate_capabilities(conn)
        capability = _find_capability(result, nombre)
        previous = _previous_capability_run(conn, capability["key"])
        validation = validate_capability(capability, previous)
        recorded = False
        if execute:
            if validation["blockers"]:
                raise HTTPException(
                    409,
                    "La validación tiene bloqueos y no puede registrarse como oficial.",
                )
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO capability_runs (
                        capability_key, capability_version, score, confidence,
                        maturity, validation_status, capability_json, validation_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                """, (
                    capability["key"],
                    result["version"],
                    capability.get("score"),
                    capability.get("confidence"),
                    capability.get("maturity"),
                    validation["status"],
                    Json(capability),
                    Json(validation),
                ))
                run_id, created_at = cur.fetchone()
            conn.commit()
            recorded = True
        else:
            run_id, created_at = None, None
        return {
            "ok": True,
            "recorded": recorded,
            "run_id": run_id,
            "created_at": created_at.isoformat() if created_at else None,
            "capability": capability,
            "validation": validation,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"Admin capability validation error: {e}")
        raise HTTPException(500, str(e))


@app.get("/gpt/capacidad/{nombre}")
def gpt_capacidad(nombre: str):
    """Detalle de una capacidad por key estable."""
    result = gpt_capacidades()
    capability = _find_capability(result, nombre)
    return {
        "ok": True,
        "version": result["version"],
        "generated_from": result["generated_from"],
        "rules": result["rules"],
        "capability": capability,
    }


# ── GET /gpt/baseline-compare ─────────────────────────────────────────────────

@app.get("/gpt/baseline-compare")
def gpt_baseline_compare(sport: str = "cycling"):
    """Compara métricas actuales vs línea base mayo 2026 con señal aeróbica clave."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        # Línea base hardcoded Mayo 2026 Mars
        BASELINE = {
            "mes": "2026-05",
            "fc_promedio": 140.0,
            "vel_promedio": 20.5,
            "cadencia_promedio": 74.0,
            "eficiencia_ratio": 20.5 / 140.0
        }

        # Sobreescribir con datos reales de mayo si existen
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(avg_hr_bpm)::numeric,1),
                       ROUND(AVG(avg_speed_kmh)::numeric,2),
                       ROUND(AVG(avg_cadence)::numeric,1),
                       COUNT(*)
                FROM sessions_clean_compat
                WHERE sport=%s AND LEFT(start_time,7)='2026-05' AND start_time IS NOT NULL
            """, (sport,))
            row = cur.fetchone()
        if row and row[3] and row[3] >= 3:
            BASELINE["fc_promedio"] = float(row[0] or BASELINE["fc_promedio"])
            BASELINE["vel_promedio"] = float(row[1] or BASELINE["vel_promedio"])
            BASELINE["cadencia_promedio"] = float(row[2] or BASELINE["cadencia_promedio"])
            BASELINE["eficiencia_ratio"] = BASELINE["vel_promedio"] / BASELINE["fc_promedio"]

        # Últimas 4 semanas
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(avg_hr_bpm)::numeric,1),
                       ROUND(AVG(avg_speed_kmh)::numeric,2),
                       ROUND(AVG(avg_cadence)::numeric,1),
                       COUNT(*),
                       ROUND(SUM(distance_km)::numeric,1)
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW() - INTERVAL '4 weeks'
            """, (sport,))
            row2 = cur.fetchone()

        actual = {
            "fc_promedio": float(row2[0] or 0),
            "vel_promedio": float(row2[1] or 0),
            "cadencia_promedio": float(row2[2] or 0),
            "sesiones_4_semanas": int(row2[3] or 0),
            "km_4_semanas": float(row2[4] or 0)
        }
        if actual["fc_promedio"] > 0:
            actual["eficiencia_ratio"] = round(actual["vel_promedio"] / actual["fc_promedio"], 4)

        # Deltas vs baseline
        deltas = {}
        for k in ("fc_promedio", "vel_promedio", "cadencia_promedio", "eficiencia_ratio"):
            b = BASELINE.get(k, 0)
            a = actual.get(k, 0)
            if b:
                deltas[k + "_delta"] = round(a - b, 2)
                deltas[k + "_pct"] = round((a - b) / b * 100, 1)

        # Señal Mars: 20-21 km/h < 140 bpm
        fc = actual["fc_promedio"]
        vel = actual["vel_promedio"]
        if vel >= 20 and fc < 140:
            senal = "✅ Motor aeróbico mejorando — %.1f km/h con %.0f bpm" % (vel, fc)
            estado = "mejorando"
        elif vel >= 19 and fc < 145:
            senal = "🔄 En progreso — %.1f km/h con %.0f bpm" % (vel, fc)
            estado = "en_progreso"
        elif actual["sesiones_4_semanas"] < 2:
            senal = "⚠️ Datos insuficientes (menos de 2 sesiones en 4 semanas)"
            estado = "datos_insuficientes"
        else:
            senal = "📊 Seguir acumulando Z2 — %.1f km/h con %.0f bpm" % (vel, fc)
            estado = "estable"

        return {
            "sport": sport,
            "baseline": BASELINE,
            "actual_4_semanas": actual,
            "deltas": deltas,
            "estado": estado,
            "senal_aerobica": senal
        }
    except Exception as e:
        raise HTTPException(500, str(e))



# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 7 — Calendario
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/gpt/calendar-heatmap")
def gpt_calendar_heatmap(year: int = None, sport: str = None):
    """Datos para heatmap anual — sesiones por día con intensidad."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    if not year:
        year = date.today().year
    try:
        query = """
            SELECT
                DATE(start_time::timestamp) as dia,
                COUNT(*) as sesiones,
                ROUND(SUM(distance_km)::numeric, 1) as km,
                ROUND(SUM(COALESCE(duration_s,0))::numeric/3600, 2) as horas,
                ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_avg,
                STRING_AGG(DISTINCT sport, ',') as deportes
            FROM sessions_clean_compat
            WHERE EXTRACT(YEAR FROM start_time::timestamp) = %s
              AND start_time IS NOT NULL
        """
        params = [year]
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " GROUP BY dia ORDER BY dia ASC"
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        days = []
        for r in rows:
            rd = dict(zip(cols, r))
            rd["dia"] = str(rd["dia"])
            # Intensidad 0-4 basada en km
            km = float(rd.get("km") or 0)
            if km == 0: intensity = 1
            elif km < 20: intensity = 1
            elif km < 40: intensity = 2
            elif km < 60: intensity = 3
            else: intensity = 4
            rd["intensity"] = intensity
            days.append(rd)
        # Totales del año
        total_km = sum(float(d.get("km") or 0) for d in days)
        total_h = sum(float(d.get("horas") or 0) for d in days)
        total_ses = sum(int(d.get("sesiones") or 0) for d in days)
        return {
            "year": year,
            "sport": sport or "all",
            "dias_activos": len(days),
            "total_km": round(total_km, 1),
            "total_horas": round(total_h, 1),
            "total_sesiones": total_ses,
            "days": days
        }
    except Exception as e:
        raise HTTPException(500, str(e))



# ─────────────────────────────────────────────────────────────────────────────
# /calendar  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

CALENDAR_HTML = ""  # keep variable for compatibility


@app.get("/gpt/performance-profile")
def gpt_performance_profile(sport: str = "cycling"):
    """Perfil de rendimiento completo — VO2Max, carga, desacople, eficiencia aeróbica, cadencia, ranking."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        # 1. Récords personales
        records = {}
        with conn.cursor() as cur:
            for metric, col, order, extra in [
                ("max_distance", "distance_km", "DESC", ""),
                ("max_duration", "duration_s", "DESC", ""),
                ("max_ascent", "ascent_m", "DESC", ""),
                ("max_speed", "avg_speed_kmh", "DESC", ""),
                ("min_avg_hr", "avg_hr_bpm", "ASC", "AND distance_km > 20"),
                ("max_cadence", "avg_cadence", "DESC", ""),
            ]:
                cur.execute(f"""
                    SELECT {col}, start_time, session_id, workout_name
                    FROM sessions_clean_compat WHERE sport=%s AND {col} IS NOT NULL
                    AND start_time IS NOT NULL {extra}
                    ORDER BY {col} {order} LIMIT 1
                """, (sport,))
                row = cur.fetchone()
                if row:
                    records[metric] = {
                        "value": float(row[0]) if isinstance(row[0], (int, float)) else row[0],
                        "date": str(row[1])[:10],
                        "session_id": row[2],
                        "name": row[3] or ""
                    }

        # 2. Ranking mejores sesiones por categoría
        ranking = {}
        with conn.cursor() as cur:
            for cat, col, order, label in [
                ("mejor_eficiencia", "avg_speed_kmh/NULLIF(avg_hr_bpm,0)", "DESC", "Mejor eficiencia FC/vel"),
                ("mayor_distancia", "distance_km", "DESC", "Mayor distancia"),
                ("mayor_ascenso", "ascent_m", "DESC", "Mayor ascenso"),
                ("mayor_velocidad", "avg_speed_kmh", "DESC", "Mayor velocidad"),
            ]:
                cur.execute(f"""
                    SELECT session_id, start_time, workout_name, distance_km,
                           avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence
                    FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
                    AND distance_km > 10 AND avg_hr_bpm IS NOT NULL
                    ORDER BY {col} {order} LIMIT 3
                """, (sport,))
                rows = cur.fetchall()
                cols2 = [d[0] for d in cur.description]
                ranking[cat] = {
                    "label": label,
                    "sessions": [dict(zip(cols2, r)) for r in rows]
                }
                for s in ranking[cat]["sessions"]:
                    for k, v in s.items():
                        if hasattr(v, "isoformat"): s[k] = v.isoformat()
                    if s.get("avg_hr_bpm") and s.get("avg_speed_kmh"):
                        s["eficiencia_ratio"] = round(float(s["avg_speed_kmh"]) / float(s["avg_hr_bpm"]), 4)

        # 3. VO2Max estimado
        vo2max_est = None
        with conn.cursor() as cur:
            cur.execute("""
                SELECT avg_speed_kmh, avg_hr_bpm FROM sessions_clean_compat
                WHERE sport=%s AND avg_speed_kmh IS NOT NULL AND avg_hr_bpm > 100
                  AND distance_km > 25 AND start_time IS NOT NULL
                ORDER BY start_time DESC LIMIT 10
            """, (sport,))
            rows = cur.fetchall()
        if rows:
            estimates = [10.8 * float(r[0]) / float(r[1]) * 3.5 for r in rows if r[1]]
            vo2max_est = round(sum(estimates) / len(estimates), 1)

        # 4. Desacople cardíaco de result_json
        cardiac_decoupling = []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, start_time, result_json FROM sessions_clean_compat
                WHERE sport=%s AND duration_s > 5400 AND result_json IS NOT NULL
                  AND start_time IS NOT NULL
                ORDER BY start_time DESC LIMIT 8
            """, (sport,))
            rows = cur.fetchall()
        for sid, st, rj_str in rows:
            try:
                rj = json.loads(rj_str)
                insights = rj.get("derived_insights", {})
                drift = insights.get("hr_drift_note")
                if drift:
                    cardiac_decoupling.append({
                        "session_id": sid,
                        "date": str(st)[:10],
                        "nota": drift
                    })
            except Exception:
                continue

        # 5. Carga ATL/CTL/TSB
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '7 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric, 1) as atl,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '42 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric / 6.0, 1) as ctl,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '7 days' THEN 1 END) as ses_7d
                FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
            """, (sport,))
            row = cur.fetchone()
        atl = float(row[0] or 0)
        ctl = float(row[1] or 0)
        tsb = round(ctl - atl, 1)
        load_status = "fresco" if tsb > 5 else ("fatigado" if tsb < -15 else "en carga")

        # 6. Eficiencia aeróbica mensual (vel/FC × 100)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('month', start_time::timestamp), 'YYYY-MM') as mes,
                    ROUND(AVG(avg_speed_kmh)::numeric, 2) as vel,
                    ROUND(AVG(avg_hr_bpm)::numeric, 1) as fc,
                    ROUND(AVG(avg_cadence)::numeric, 1) as cad,
                    COUNT(*) as ses,
                    ROUND((AVG(avg_speed_kmh)/NULLIF(AVG(avg_hr_bpm),0))::numeric, 4) as eff
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW() - INTERVAL '6 months'
                  AND avg_hr_bpm > 0
                GROUP BY mes ORDER BY mes ASC
            """, (sport,))
            rows = cur.fetchall()
            cols3 = [d[0] for d in cur.description]
        monthly_eff = [dict(zip(cols3, r)) for r in rows]

        # Eficiencia delta vs primer mes
        eff_delta = None
        if len(monthly_eff) >= 2:
            e0 = float(monthly_eff[0].get("eff") or 0)
            e1 = float(monthly_eff[-1].get("eff") or 0)
            if e0:
                eff_delta = round((e1 - e0) / e0 * 100, 1)

        # 7. Carga semanal últimas 8 semanas
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('week', start_time::timestamp), 'YYYY-MM-DD') as semana,
                    COUNT(*) as ses,
                    ROUND(SUM(distance_km)::numeric, 1) as km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric/3600, 1) as horas,
                    ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc,
                    ROUND(AVG(avg_speed_kmh)::numeric, 1) as vel,
                    ROUND(AVG(avg_cadence)::numeric, 1) as cad
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW()-INTERVAL '8 weeks'
                GROUP BY semana ORDER BY semana ASC
            """, (sport,))
            rows = cur.fetchall()
            cols4 = [d[0] for d in cur.description]
        weekly_load = [dict(zip(cols4, r)) for r in rows]

        # FC/vel ratio tendencia
        fc_vel_trend = "sin datos"
        if len(weekly_load) >= 2:
            f = weekly_load[0]; l = weekly_load[-1]
            fc_f = float(f.get("fc") or 0); vel_f = float(f.get("vel") or 0)
            fc_l = float(l.get("fc") or 0); vel_l = float(l.get("vel") or 0)
            if fc_f and vel_f and fc_l and vel_l:
                r_f = vel_f / fc_f; r_l = vel_l / fc_l
                delta = round((r_l - r_f) / r_f * 100, 1)
                fc_vel_trend = f"+{delta}%" if delta >= 0 else f"{delta}%"

        # 8. Evolución cadencia mensual
        cad_trend = "sin datos"
        cad_months = [(m["mes"], float(m.get("cad") or 0)) for m in monthly_eff if m.get("cad")]
        if len(cad_months) >= 2:
            delta_cad = round(cad_months[-1][1] - cad_months[0][1], 1)
            cad_trend = f"+{delta_cad} rpm" if delta_cad >= 0 else f"{delta_cad} rpm"

        return {
            "sport": sport,
            "vo2max_estimado": vo2max_est,
            "vo2max_nota": "Estimación Firstbeat (vel/FC). No reemplaza test real.",
            "eficiencia_aerobica": {
                "delta_pct_6_meses": eff_delta,
                "mensual": monthly_eff
            },
            "cadencia_trend": cad_trend,
            "carga": {
                "atl_7d": atl,
                "ctl_semana": ctl,
                "tsb": tsb,
                "estado": load_status,
                "sesiones_7_dias": int(row[2] or 0)
            },
            "fc_vel_ratio_tendencia": fc_vel_trend,
            "carga_semanal": weekly_load,
            "desacople_cardiaco": cardiac_decoupling,
            "ranking": ranking,
            "records": records
        }
    except Exception as e:
        raise HTTPException(500, str(e))



# ─────────────────────────────────────────────────────────────────────────────
# /performance  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

PERFORMANCE_HTML = ""  # keep for compatibility


@app.post("/weight")
def add_weight(body: WeightIn):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    _ensure_weight_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO weight_log (date,weight_kg,waist_cm,body_fat_pct,notes) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (body.date, body.weight_kg, body.waist_cm, body.body_fat_pct, body.notes))
            wid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": wid}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.get("/weight/history")
def get_weight_history(limit: int = 60):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    _ensure_weight_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT date,weight_kg,waist_cm,body_fat_pct,notes FROM weight_log ORDER BY date DESC LIMIT %s", (limit,))
            rows = cur.fetchall(); cols = [d[0] for d in cur.description]
        entries = [dict(zip(cols,r)) for r in rows]
        for e in entries:
            for k,v in e.items():
                if hasattr(v,'isoformat'): e[k]=v.isoformat()
                if hasattr(v,'__float__') and v is not None:
                    try: e[k]=float(v)
                    except: pass
        asc = list(reversed(entries))
        delta = None
        if len(asc)>=2:
            w0=float(asc[0].get('weight_kg') or 0); w1=float(asc[-1].get('weight_kg') or 0)
            if w0 and w1: delta=round(w1-w0,2)
        current = float(entries[0]['weight_kg']) if entries and entries[0].get('weight_kg') else None
        return {"current_kg":current,"delta_kg":delta,"registros":len(entries),"historial":asc}
    except Exception as e: raise HTTPException(500, str(e))

# ── Gear service history ──────────────────────────────────────────────────────


@app.post("/gear/service")
def add_gear_service(body: GearServiceIn):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    _ensure_gear_service_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO gear_service (gear_id,gear_name,service_type,description,date,km_at_service,cost_mxn,shop,notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (body.gear_id,body.gear_name,body.service_type,body.description,
                 body.date,body.km_at_service,body.cost_mxn,body.shop,body.notes))
            sid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": sid}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.get("/gear/service-history")
def get_gear_service_history(limit: int = 50):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    _ensure_gear_service_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT gs.*, (CURRENT_DATE - gs.date) as days_since
                FROM gear_service gs ORDER BY gs.date DESC LIMIT %s""", (limit,))
            rows = cur.fetchall(); cols = [d[0] for d in cur.description]
        entries = []
        for r in rows:
            d = dict(zip(cols,r))
            for k,v in d.items():
                if hasattr(v,'isoformat'): d[k]=v.isoformat()
                if hasattr(v,'__float__') and v is not None:
                    try: d[k]=float(v)
                    except: pass
            entries.append(d)
        return {"registros":len(entries),"historial":entries}
    except Exception as e: raise HTTPException(500, str(e))

# ── Trends — sparklines 8 semanas ─────────────────────────────────────────────

@app.get("/gpt/trends")
def gpt_trends(weeks: int = 8):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DATE_TRUNC('week',start_time::timestamp)::date as week,
                    ROUND(AVG(avg_hr_bpm)::numeric,1) as avg_hr,
                    ROUND(AVG(avg_cadence)::numeric,1) as avg_cad,
                    ROUND(SUM(duration_s)/3600.0::numeric,2) as hours,
                    ROUND(SUM(distance_km)::numeric,1) as km,
                    COUNT(*) as sessions
                FROM sessions_clean_compat WHERE sport='cycling'
                  AND start_time::timestamp >= NOW() - (%s || ' weeks')::interval
                GROUP BY week ORDER BY week""", (weeks,))
            rows = cur.fetchall(); cols = [d[0] for d in cur.description]
            weekly = []
            for r in rows:
                d = dict(zip(cols,r))
                for k,v in d.items():
                    if hasattr(v,'isoformat'): d[k]=v.isoformat()
                    if hasattr(v,'__float__') and v is not None:
                        try: d[k]=float(v)
                        except: pass
                weekly.append(d)
        _ensure_weight_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT date::text, weight_kg::float, waist_cm::float
                FROM weight_log WHERE date >= CURRENT_DATE - (%s || ' weeks')::interval::interval
                ORDER BY date""", (weeks,))
            wrows = cur.fetchall()
        weight = [{"date":r[0],"kg":float(r[1]) if r[1] else None,"waist":float(r[2]) if r[2] else None} for r in wrows]
        def spark(arr,key):
            vals=[x.get(key) for x in arr if x.get(key) is not None]
            if not vals: return {"data":[],"min":None,"max":None,"last":None,"delta":None}
            return {"data":vals,"min":min(vals),"max":max(vals),"last":vals[-1],
                    "delta":round(vals[-1]-vals[0],2) if len(vals)>1 else 0}
        return {"semanas":len(weekly),"weekly":weekly,"weight":weight,
                "sparklines":{"hr":spark(weekly,"avg_hr"),"cad":spark(weekly,"avg_cad"),
                    "hours":spark(weekly,"hours"),"km":spark(weekly,"km"),"weight":spark(weight,"kg")}}
    except Exception as e: raise HTTPException(500, str(e))

@app.post("/wellness")
def save_wellness(body: WellnessIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_wellness_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wellness (date, category, compex_program, muscle_zone,
                    duration_min, ceragem_duration_min, ceragem_sensation_before,
                    ceragem_sensation_after, sleep_hours, sleep_quality, hr_rest,
                    garmin_sleep_score, pain_zone, pain_level, pain_start, pain_end,
                    pain_type, stress_level, stress_cause, notes, fatigue)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.date, body.category, body.compex_program, body.muscle_zone,
                  body.duration_min, body.ceragem_duration_min,
                  body.ceragem_sensation_before, body.ceragem_sensation_after,
                  body.sleep_hours, body.sleep_quality, body.hr_rest,
                  body.garmin_sleep_score, body.pain_zone, body.pain_level,
                  body.pain_start, body.pain_end, body.pain_type,
                  body.stress_level, body.stress_cause, body.notes, body.fatigue))
            wid = cur.fetchone()[0]
        return {"ok": True, "id": wid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/wellness-summary ─────────────────────────────────────────────────

@app.get("/gpt/wellness-summary")
def gpt_wellness_summary(weeks: int = 4):
    """Resumen de recuperación — sueño, Compex, Ceragem, molestias activas, fatiga."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_wellness_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM wellness
                WHERE date >= CURRENT_DATE - (%s * INTERVAL '1 week')
                ORDER BY date DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        entries = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = str(v)
            entries.append(rd)

        # Sueño promedio
        sleep = [e for e in entries if e.get("sleep_hours")]
        sleep_avg = round(sum(float(e["sleep_hours"]) for e in sleep) / len(sleep), 1) if sleep else None
        hr_rest_avg = None
        hr_rows = [e for e in entries if e.get("hr_rest")]
        if hr_rows:
            hr_rest_avg = round(sum(int(e["hr_rest"]) for e in hr_rows) / len(hr_rows), 0)

        # Sesiones por categoría
        by_cat = {}
        for e in entries:
            cat = e.get("category","otro")
            by_cat[cat] = by_cat.get(cat, 0) + 1

        # Molestias activas (pain sin pain_end o pain_end en el futuro)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pain_zone, pain_level, pain_type, pain_start, pain_end, notes
                FROM wellness
                WHERE category='pain' AND pain_zone IS NOT NULL
                  AND (pain_end IS NULL OR pain_end >= CURRENT_DATE)
                ORDER BY pain_start DESC
            """)
            pain_rows = cur.fetchall()
            pain_cols = [d[0] for d in cur.description]
        active_pain = [dict(zip(pain_cols, r)) for r in pain_rows]
        for p in active_pain:
            for k, v in p.items():
                if hasattr(v, "isoformat"): p[k] = str(v)

        # Fatiga promedio últimas 2 semanas
        fatigue_entries = [e for e in entries if e.get("fatigue")]
        fatigue_avg = round(sum(int(e["fatigue"]) for e in fatigue_entries) / len(fatigue_entries), 1) if fatigue_entries else None

        # Ceragem: sensación promedio antes vs después
        ceragem = [e for e in entries if e.get("category") == "ceragem"]
        ceragem_delta = None
        if ceragem:
            antes = [float(e["ceragem_sensation_before"]) for e in ceragem if e.get("ceragem_sensation_before")]
            despues = [float(e["ceragem_sensation_after"]) for e in ceragem if e.get("ceragem_sensation_after")]
            if antes and despues:
                ceragem_delta = round(sum(despues)/len(despues) - sum(antes)/len(antes), 1)

        return {
            "semanas": weeks,
            "total_sesiones": len(entries),
            "por_categoria": by_cat,
            "sueno_promedio_horas": sleep_avg,
            "fc_reposo_promedio": hr_rest_avg,
            "fatiga_promedio": fatigue_avg,
            "ceragem_delta_sensacion": ceragem_delta,
            "molestias_activas": active_pain,
            "historial_reciente": entries[:15]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /wellness ─────────────────────────────────────────────────────────────


@app.post("/fuerza")
def save_fuerza(body: FuerzaIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_fuerza_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fuerza (date, category, subcategory, muscle_groups,
                    intensity, duration_min, sets, reps, weight_kg, exercise,
                    notes, rpe, fatigue_before, fatigue_after)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.date, body.category, body.subcategory, body.muscle_groups,
                  body.intensity, body.duration_min, body.sets, body.reps,
                  body.weight_kg, body.exercise, body.notes, body.rpe,
                  body.fatigue_before, body.fatigue_after))
            fid = cur.fetchone()[0]
        return {"ok": True, "id": fid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/fuerza-summary ───────────────────────────────────────────────────

@app.get("/gpt/fuerza-summary")
def gpt_fuerza_summary(weeks: int = 8):
    """Resumen de fuerza — sesiones, progresión por músculo, Compex intensidad."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_fuerza_table(conn)
    try:
        # Sesiones recientes
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, date, category, subcategory, muscle_groups,
                       intensity, duration_min, sets, reps, weight_kg,
                       exercise, rpe, notes
                FROM fuerza
                WHERE date >= CURRENT_DATE - (%s * INTERVAL '1 week')
                ORDER BY date DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        sessions = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = str(v)
            sessions.append(rd)

        # Por categoría
        by_cat = {}
        for s in sessions:
            cat = s.get("category", "otro")
            if cat not in by_cat:
                by_cat[cat] = {"sesiones": 0, "minutos": 0}
            by_cat[cat]["sesiones"] += 1
            by_cat[cat]["minutos"] += int(s.get("duration_min") or 0)

        # Progresión Compex por músculo (intensidad máxima por mes)
        compex_progress = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('month', date), 'YYYY-MM') as mes,
                    UNNEST(muscle_groups) as musculo,
                    MAX(intensity) as max_intensity,
                    COUNT(*) as sesiones
                FROM fuerza
                WHERE category = 'compex' AND intensity IS NOT NULL
                GROUP BY mes, musculo ORDER BY mes ASC
            """)
            cp_rows = cur.fetchall()
        for mes, musculo, max_int, ses in cp_rows:
            if musculo not in compex_progress:
                compex_progress[musculo] = []
            compex_progress[musculo].append({
                "mes": mes,
                "max_intensity": int(max_int or 0),
                "sesiones": int(ses or 0)
            })

        # Totales
        total_ses = len(sessions)
        total_min = sum(int(s.get("duration_min") or 0) for s in sessions)

        return {
            "semanas": weeks,
            "total_sesiones": total_ses,
            "total_horas": round(total_min / 60, 1),
            "por_categoria": by_cat,
            "compex_progresion": compex_progress,
            "sesiones_recientes": sessions[:10]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /fuerza-page ──────────────────────────────────────────────────────────


@app.get("/gpt/gear-status")
def gpt_gear_status():
    """Estado completo del gear — odómetro por componente, alertas y costo/km."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    _ensure_gear_activity_links_table(conn)
    try:
        # Total km en Supabase
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(distance_km),0) FROM sessions_clean_compat
                WHERE sport='cycling' AND start_time IS NOT NULL
            """)
            total_km_db = float(cur.fetchone()[0] or 0)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.gear_id, g.name, g.type, g.bike_id, g.brand, g.model,
                       g.installed_date, g.km_at_install, g.km_limit,
                       g.retired_date, g.notes,
                       COALESCE((SELECT MAX(km_at_service) FROM maintenance m
                                  WHERE m.gear_id=g.gear_id), g.km_at_install, 0) as last_service_km
                FROM gear g WHERE g.retired_date IS NULL
                ORDER BY g.type, g.name
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        components = []
        for r in rows:
            d = dict(zip(cols, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"): d[k] = str(v)
            km_install = int(d.get("km_at_install") or 0)
            km_used = max(0, round(total_km_db) - km_install)
            km_limit = d.get("km_limit")
            pct = round(km_used / km_limit * 100, 1) if km_limit else None
            status = "green"
            if pct is not None:
                status = "red" if pct >= 85 else ("yellow" if pct >= 60 else "green")
            d["km_used"] = km_used
            d["km_used_source"] = "bike_odometer_estimate"
            d["km_used_note"] = "Aproximado por km total de bici menos km de instalación; será exacto cuando existan enlaces actividad-pieza."
            d["km_remaining"] = max(0, km_limit - km_used) if km_limit else None
            d["pct_used"] = pct
            d["status"] = status
            d["status_emoji"] = "🔴" if status=="red" else ("🟡" if status=="yellow" else "🟢")
            components.append(d)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_gear_id, name, type, model, status, date_begin, max_distance_km
                FROM garmin_export_gear
                ORDER BY type, name
            """)
            garmin_rows = cur.fetchall()
            garmin_cols = [d[0] for d in cur.description]
        garmin_catalog = []
        garmin_active = []
        garmin_retired = []
        existing_names = {str(c.get("name") or "").strip().lower() for c in components}
        for r in garmin_rows:
            d = dict(zip(garmin_cols, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"): d[k] = str(v)
            status_raw = str(d.get("status") or "").lower()
            is_retired = "retired" in status_raw or "inactive" in status_raw
            d["source"] = "garmin_export"
            d["gear_id"] = f"garmin:{d.get('source_gear_id')}"
            d["km_used"] = None
            d["km_remaining"] = None
            d["pct_used"] = None
            d["status_emoji"] = "retirado" if is_retired else "activo"
            d["km_limit"] = float(d["max_distance_km"]) if d.get("max_distance_km") is not None else None
            garmin_catalog.append(d)
            if is_retired:
                garmin_retired.append(d)
            else:
                garmin_active.append(d)
            if not is_retired and str(d.get("name") or "").strip().lower() not in existing_names:
                components.append(d)

        # Mantenimiento reciente
        with conn.cursor() as cur:
            cur.execute("""
                SELECT type, description, date, km_at_service, cost_mxn, shop
                FROM maintenance ORDER BY date DESC LIMIT 10
            """)
            maint_rows = cur.fetchall()
            maint_cols = [d[0] for d in cur.description]
        maintenance = [dict(zip(maint_cols, r)) for r in maint_rows]
        for m in maintenance:
            for k, v in m.items():
                if hasattr(v, "isoformat"): m[k] = str(v)

        # Costo total y por km
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(cost_mxn),0) FROM maintenance")
            total_cost = float(cur.fetchone()[0] or 0)
        cost_per_km = round(total_cost / total_km_db, 2) if total_km_db > 0 else 0

        # Accidentes
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accidents ORDER BY date DESC")
            acc_rows = cur.fetchall()
            acc_cols = [d[0] for d in cur.description]
        accidents = [dict(zip(acc_cols, r)) for r in acc_rows]
        for a in accidents:
            for k, v in a.items():
                if hasattr(v, "isoformat"): a[k] = str(v)

        alerts = [c for c in components if c["status"] in ("red","yellow")]
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gear_activity_links")
            gear_links_count = int(cur.fetchone()[0] or 0)

        return {
            "total_km_bici": round(total_km_db),
            "componentes": components,
            "garmin_catalog": garmin_catalog,
            "garmin_active": garmin_active,
            "garmin_retired": garmin_retired,
            "gear_model_note": "Garmin export trae catalogo de piezas, pero no siempre liga actividad a pieza. La siguiente fase debe guardar activity_id -> gear_id para kilometraje real por componente.",
            "future_component_km_model": {
                "table_ready": True,
                "existing_links": gear_links_count,
                "activity_gear_links": ["session_id", "source_activity_id", "gear_id", "role"],
                "component_distance_rollup": "sumar distance_km de clean_sessions enlazadas a cada gear_id"
            },
            "alertas": alerts,
            "mantenimiento_reciente": maintenance,
            "costo_total_mxn": total_cost,
            "costo_por_km_mxn": cost_per_km,
            "accidentes": accidents
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /gear ────────────────────────────────────────────────────────────────

@app.post("/gear")
def add_gear(body: GearIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    import uuid
    gear_id = body.gear_id or str(uuid.uuid4())[:8]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gear (gear_id, name, type, bike_id, installed_date,
                    km_at_install, km_limit, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (gear_id) DO UPDATE SET
                    name=EXCLUDED.name, type=EXCLUDED.type,
                    installed_date=EXCLUDED.installed_date,
                    km_at_install=EXCLUDED.km_at_install,
                    km_limit=EXCLUDED.km_limit, notes=EXCLUDED.notes
            """, (gear_id, body.name, body.type, body.bike_id or "orbea-avant-2019",
                  body.installed_date, body.km_at_install or 0, body.km_limit, body.notes))
        return {"ok": True, "gear_id": gear_id}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /maintenance ─────────────────────────────────────────────────────────

@app.post("/maintenance")
def add_maintenance(body: MaintenanceIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO maintenance (bike_id, gear_id, type, description, date,
                    km_at_service, cost_mxn, shop, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.bike_id, body.gear_id, body.type, body.description,
                  body.date, body.km_at_service, body.cost_mxn, body.shop, body.notes))
            mid = cur.fetchone()[0]
        return {"ok": True, "id": mid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /accidents ───────────────────────────────────────────────────────────

@app.post("/accidents")
def add_accident(body: AccidentIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO accidents (date, description, damage, repair,
                    cost_mxn, km_at_accident, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.date, body.description, body.damage, body.repair,
                  body.cost_mxn, body.km_at_accident, body.notes))
            aid = cur.fetchone()[0]
        return {"ok": True, "id": aid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gear-page ────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# /gear  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

GEAR_HTML = ""  # keep for compatibility


@app.get("/admin/health")
def admin_health(token: str = None):
    """Estado de salud del sistema — tamaño DB, última sesión, tiempos de respuesta."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    import time
    conn = get_db()
    if not conn:
        return {"api": "ok", "db": "error"}
    try:
        t0 = time.time()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clean_sessions")
            sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM session_records")
            records = cur.fetchone()[0]
            cur.execute("SELECT MAX(start_time), MAX(created_at) FROM clean_sessions")
            row = cur.fetchone()
            last_session = str(row[0])[:10] if row[0] else None
            last_upload = str(row[1])[:19] if row[1] else None
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            db_size = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM achievements")
            achievements = cur.fetchone()[0]
        db_ms = round((time.time() - t0) * 1000, 1)
        return {
            "api": "ok", "db": "ok",
            "db_size": db_size,
            "sessions": sessions,
            "session_records": records,
            "achievements": achievements,
            "last_session_date": last_session,
            "last_upload": last_upload,
            "db_response_ms": db_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Health error: {e}")
        return {"api": "ok", "db": "error", "detail": str(e)}


@app.get("/admin/achievements")
def get_achievements(limit: int = 20):
    """Últimos hitos detectados automáticamente."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.session_id, a.date, a.type, a.metric,
                       a.value, a.prev_best, a.description
                FROM achievements a
                ORDER BY a.date DESC, a.id DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        result = [dict(zip(cols, r)) for r in rows]
        for item in result:
            for k, v in item.items():
                if hasattr(v, "isoformat"): item[k] = str(v)
        return {"achievements": result, "total": len(result)}
    except Exception as e:
        raise HTTPException(500, str(e))


def detect_and_save_achievements(conn, session_id, result):
    """Detecta récords automáticamente al subir un FIT y los guarda en achievements."""
    s = result.get("session", {})
    if not s.get("start_time"):
        return []
    new_achievements = []
    checks = [
        ("max_distance", "distance_km", s.get("distance_km"), "Mayor distancia"),
        ("max_ascent", "ascent_m", s.get("ascent_m"), "Mayor ascenso"),
        ("max_speed", "avg_speed_kmh", s.get("avg_speed_kmh"), "Mayor velocidad promedio"),
        ("best_efficiency", "efficiency", 
         round(float(s.get("avg_speed_kmh",0)) / float(s.get("avg_hr_bpm",1)), 4) if s.get("avg_hr_bpm") else None,
         "Mejor eficiencia aeróbica"),
    ]
    sport = s.get("sport", "cycling")
    try:
        with conn.cursor() as cur:
            for ach_type, col, value, label in checks:
                if not value:
                    continue
                fval = float(value)
                if col == "efficiency":
                    cur.execute("""
                        SELECT MAX(avg_speed_kmh/NULLIF(avg_hr_bpm,0))
                        FROM sessions_clean_compat WHERE sport=%s AND session_id != %s
                        AND avg_hr_bpm > 0
                    """, (sport, session_id))
                else:
                    cur.execute(f"""
                        SELECT MAX({col}) FROM sessions_clean_compat
                        WHERE sport=%s AND session_id != %s
                    """, (sport, session_id))
                row = cur.fetchone()
                prev_best = float(row[0]) if row and row[0] else None
                if prev_best is None or fval > prev_best:
                    date_val = s.get("start_time","")[:10]
                    fmt_val = f"{fval:.2f}" if col not in ("ascent_m",) else f"{int(fval)} m"
                    desc = f"🏆 {label}: {fmt_val}"
                    if prev_best:
                        diff = fval - prev_best
                        desc += f" (+{diff:.2f} vs anterior)"
                    cur.execute("""
                        INSERT INTO achievements (session_id, date, type, metric, value, prev_best, description)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (session_id, date_val, ach_type, col, fval, prev_best, desc))
                    new_achievements.append(desc)
                    logger.info(f"Achievement: {desc}")
    except Exception as e:
        logger.error(f"Achievement detection error: {e}")
    return new_achievements


def generate_weekly_snapshot(conn):
    """Genera o actualiza la semana más reciente usando la columna limpia."""
    try:
        from tools.backfill_athlete_snapshots import build_snapshots, write_snapshots

        snapshots = build_snapshots(conn)
        if not snapshots:
            return
        latest = snapshots[-1]
        write_snapshots(conn, [latest])
        logger.info(
            "Weekly snapshot saved: %s sessions=%s km=%s",
            latest["week_start"],
            latest["sessions"],
            latest["km_week"],
        )
    except Exception as e:
        logger.error(f"Weekly snapshot error: {e}")


@app.get("/admin/generate-snapshot")
def admin_generate_snapshot(token: str = None):
    """Genera manualmente el snapshot semanal del atleta."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        generate_weekly_snapshot(conn)
        # Get latest snapshot to return
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM athlete_snapshots ORDER BY week_start DESC LIMIT 1")
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        snap = dict(zip(cols, row)) if row else {}
        for k, v in snap.items():
            if hasattr(v, "isoformat"): snap[k] = str(v)
        return {"ok": True, "snapshot": snap}
    except Exception as e:
        logger.error(f"Generate snapshot error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/backfill-snapshots")
def admin_backfill_snapshots(token: str = None):
    """
    Genera TODOS los snapshots semanales históricos desde clean_sessions.
    Necesario una sola vez si athlete_snapshots está vacío.
    Sin esto las capacidades muestran score=None.
    """
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.backfill_athlete_snapshots import build_snapshots, write_snapshots
        snapshots = build_snapshots(conn)
        if not snapshots:
            return {"ok": True, "message": "Sin sesiones en clean_sessions — backfill de Strava pendiente", "count": 0}
        write_snapshots(conn, snapshots)
        return {
            "ok": True,
            "count": len(snapshots),
            "first_week": str(snapshots[0]["week_start"]),
            "last_week":  str(snapshots[-1]["week_start"]),
            "message": f"{len(snapshots)} semanas de snapshots generadas. Las capacidades ahora tienen datos."
        }
    except Exception as e:
        logger.error(f"Backfill snapshots error: {e}", exc_info=True)
        raise HTTPException(500, str(e))


@app.get("/admin/diagnostics")
def admin_diagnostics(token: str = None):
    """Diagnóstico rápido de la API y conteos de todas las tablas."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        return {"api": "ok", "db": "error", "detail": "DB no disponible"}
    try:
        counts = {}
        tables = ["sessions", "session_records", "post_session", "gear",
                  "maintenance", "fuerza", "wellness", "accidents",
                  "athlete_profile", "athlete_tests", "recovery",
                  "achievements", "athlete_snapshots",
                  "garmin_export_activities", "garmin_export_gear",
                  "garmin_export_sleep", "clean_sessions", "zone_models",
                  "session_environment", "capability_runs"]
        with conn.cursor() as cur:
            for table in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                except Exception:
                    counts[table] = "tabla no existe"
        return {
            "api": "ok",
            "db": "ok",
            **counts,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Diagnostics error: {e}")
        return {"api": "ok", "db": "error", "detail": str(e)}


@app.get("/admin/garmin-staging")
def admin_garmin_staging(token: str = None):
    """Conteos de staging Garmin sin mezclar con sesiones actuales."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_garmin_staging_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities")
            activities_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities WHERE is_probable_real_activity IS TRUE")
            real_candidates = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_gear")
            gear_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_sleep")
            sleep_count = cur.fetchone()[0]
            cur.execute("""
                SELECT sport, COUNT(*)
                FROM garmin_export_activities
                GROUP BY sport
                ORDER BY COUNT(*) DESC
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT MIN(start_time_local)::text, MAX(start_time_local)::text
                FROM garmin_export_activities
            """)
            date_min, date_max = cur.fetchone()
            cur.execute("""
                SELECT confidence, COUNT(*)
                FROM garmin_export_sleep
                GROUP BY confidence
                ORDER BY COUNT(*) DESC
            """)
            sleep_confidence = [{"confidence": r[0], "count": r[1]} for r in cur.fetchall()]
        return {
            "ok": True,
            "activities": activities_count,
            "real_activity_candidates": real_candidates,
            "gear": gear_count,
            "sleep": sleep_count,
            "date_min": date_min,
            "date_max": date_max,
            "by_sport": by_sport,
            "sleep_confidence": sleep_confidence,
        }
    except Exception as e:
        logger.error(f"Garmin staging diagnostics error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/garmin-compare")
def admin_garmin_compare(token: str = None):
    """Compara staging Garmin contra sessions actuales para decidir limpieza."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_garmin_staging_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sessions")
            current_sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities")
            staging_activities = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*)
                FROM garmin_export_activities g
                JOIN sessions s
                  ON s.sport = g.sport
                 AND ABS(COALESCE(s.distance_km, 0) - COALESCE(g.distance_km::float, 0)) < 0.05
                 AND ABS(COALESCE(s.duration_s, 0) - COALESCE(g.duration_s, 0)) <= 5
                 AND LEFT(COALESCE(s.start_time, ''), 10) = LEFT(g.start_time_local::text, 10)
            """)
            fuzzy_matches = cur.fetchone()[0]
            cur.execute("""
                SELECT LEFT(COALESCE(start_time, ''), 10) AS date,
                       sport,
                       ROUND(COALESCE(distance_km, 0)::numeric, 2) AS distance_km,
                       COALESCE(duration_s, 0) AS duration_s,
                       COUNT(*) AS duplicates
                FROM sessions
                GROUP BY 1, 2, 3, 4
                HAVING COUNT(*) > 1
                ORDER BY duplicates DESC
                LIMIT 20
            """)
            duplicate_groups = [
                {
                    "date": r[0],
                    "sport": r[1],
                    "distance_km": float(r[2]) if r[2] is not None else None,
                    "duration_s": r[3],
                    "duplicates": r[4],
                }
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT sport, COUNT(*)
                FROM sessions
                GROUP BY sport
                ORDER BY COUNT(*) DESC
            """)
            current_by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT sport, COUNT(*)
                FROM garmin_export_activities
                GROUP BY sport
                ORDER BY COUNT(*) DESC
            """)
            staging_by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
        return {
            "ok": True,
            "current_sessions": current_sessions,
            "garmin_staging_activities": staging_activities,
            "fuzzy_matches_staging_to_current": fuzzy_matches,
            "current_by_sport": current_by_sport,
            "staging_by_sport": staging_by_sport,
            "top_duplicate_groups_current_sessions": duplicate_groups,
            "verdict": "Usar staging Garmin como indice limpio y limpiar sessions despues de revisar duplicados.",
        }
    except Exception as e:
        logger.error(f"Garmin compare error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/clean-sessions")
def admin_clean_sessions(token: str = None):
    """Estado de la capa limpia de sesiones."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_clean_sessions_table(conn)
        _ensure_gear_activity_links_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clean_sessions")
            total = cur.fetchone()[0]
            cur.execute("""
                SELECT source, quality, COUNT(*)
                FROM clean_sessions
                GROUP BY source, quality
                ORDER BY source, quality
            """)
            by_source_quality = [
                {"source": r[0], "quality": r[1], "count": r[2]}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT sport, COUNT(*)
                FROM clean_sessions
                GROUP BY sport
                ORDER BY COUNT(*) DESC
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT MIN(start_time)::text, MAX(start_time)::text
                FROM clean_sessions
            """)
            date_min, date_max = cur.fetchone()
            cur.execute("""
                SELECT COUNT(*)
                FROM sessions
                WHERE COALESCE(start_time, '') = ''
                  AND COALESCE(sport, '') = ''
                  AND COALESCE(distance_km, 0) = 0
                  AND COALESCE(duration_s, 0) = 0
            """)
            current_junk_zero_empty = cur.fetchone()[0]
        return {
            "ok": True,
            "clean_sessions": total,
            "date_min": date_min,
            "date_max": date_max,
            "by_source_quality": by_source_quality,
            "by_sport": by_sport,
            "current_junk_zero_empty": current_junk_zero_empty,
            "verdict": "clean_sessions es la capa recomendada para fase 1/2; sessions queda como tabla historica cruda.",
        }
    except Exception as e:
        logger.error(f"Clean sessions diagnostics error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/zone-models")
def admin_zone_models(token: str = None):
    """List versioned zone models and recalculation coverage."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.recalculate_mars_zones import ensure_zone_models

        ensure_zone_models(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT model_id, sport, lt_bpm, max_hr_bpm, method, zones_json,
                       effective_from, effective_to, status, source, notes, created_at
                FROM zone_models
                ORDER BY sport, effective_from, model_id
            """)
            columns = [description[0] for description in cur.description]
            models = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE zone_model_used IS NOT NULL) AS calculated,
                    COUNT(*) FILTER (WHERE zone_confidence LIKE 'telemetry_%') AS telemetry,
                    COUNT(*) FILTER (WHERE zone_confidence = 'session_average_estimate') AS estimated,
                    COUNT(*) FILTER (WHERE zone_confidence = 'no_heart_rate') AS no_hr
                FROM clean_sessions
                WHERE sport IN (
                    'cycling', 'indoor_cycling',
                    'running', 'trail_running', 'treadmill_running'
                )
            """)
            calculated, telemetry, estimated, no_hr = cur.fetchone()
        for model in models:
            for key, value in list(model.items()):
                if hasattr(value, "isoformat"):
                    model[key] = value.isoformat()
        return {
            "ok": True,
            "models": models,
            "coverage": {
                "calculated": calculated,
                "telemetry": telemetry,
                "session_average_estimate": estimated,
                "no_heart_rate": no_hr,
            },
            "rules": {
                "original_is_immutable": True,
                "score_and_confidence_are_separate": True,
                "models_are_append_only": True,
            },
        }
    except Exception as e:
        logger.error(f"Zone model diagnostics error: {e}")
        raise HTTPException(500, str(e))


@app.post("/admin/recalculate-zones")
def admin_recalculate_zones(execute: bool = False, token: str = None):
    """Preview or execute versioned Mars zone recalculation."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.recalculate_mars_zones import recalculate_zones

        result = recalculate_zones(conn, execute=execute)
        result["next_step"] = (
            "Validate /admin/zone-models and snapshot coverage before Capability Engine."
            if execute
            else "Re-run with execute=true only after reviewing this dry-run summary."
        )
        return result
    except Exception as e:
        logger.error(f"Zone recalculation error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/recalculate-zones/preview")
def admin_recalculate_zones_preview(token: str = None):
    """Safe read-only preview of the Mars zone recalculation population."""
    return admin_recalculate_zones(execute=False, token=token)


@app.post("/admin/session-environment")
def admin_session_environment(execute: bool = False, token: str = None):
    """Preview or build altitude/location context without changing source sessions."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.backfill_session_environment import backfill_session_environment

        return backfill_session_environment(conn, execute=execute)
    except Exception as e:
        logger.error(f"Session environment error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/session-environment/preview")
def admin_session_environment_preview(token: str = None):
    """Read-only coverage preview for session_environment."""
    return admin_session_environment(execute=False, token=token)


@app.get("/gpt/environment-summary")
def gpt_environment_summary():
    """Altitude exposure and travel context from session_environment."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_session_environment_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS sessions,
                    COUNT(avg_altitude_m) AS with_altitude,
                    ROUND(AVG(habitual_altitude_m)::numeric, 0) AS habitual_altitude_m,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'above_habitual', 'well_above_habitual'
                    )) AS above_habitual,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'below_habitual', 'well_below_habitual'
                    )) AS below_habitual
                FROM session_environment
            """)
            sessions, with_altitude, habitual, above, below = cur.fetchone()
            cur.execute("""
                SELECT altitude_band, COUNT(*)
                FROM session_environment
                GROUP BY altitude_band
                ORDER BY COUNT(*) DESC
            """)
            bands = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("""
                SELECT country_code, region_label, COUNT(*)
                FROM session_environment
                WHERE country_code IS NOT NULL
                GROUP BY country_code, region_label
                ORDER BY COUNT(*) DESC
            """)
            countries = [
                {"code": row[0], "label": row[1], "sessions": row[2]}
                for row in cur.fetchall()
            ]
            cur.execute("""
                SELECT se.clean_session_id, cs.start_time, cs.name, cs.sport,
                       se.avg_altitude_m, se.max_altitude_m, se.ascent_m,
                       se.country_code, se.region_label, se.relative_altitude_band,
                       se.prior_21d_exposure_days, se.acclimatization_status,
                       se.altitude_confidence
                FROM session_environment se
                JOIN clean_sessions cs USING (clean_session_id)
                WHERE se.avg_altitude_m IS NOT NULL
                ORDER BY se.avg_altitude_m DESC
                LIMIT 12
            """)
            columns = [description[0] for description in cur.description]
            highest = [dict(zip(columns, row)) for row in cur.fetchall()]
        for item in highest:
            if item.get("start_time"):
                item["start_time"] = item["start_time"].isoformat()
            for key in ("avg_altitude_m", "max_altitude_m", "altitude_confidence"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
        return {
            "ok": True,
            "sessions": sessions,
            "with_altitude": with_altitude,
            "coverage_pct": round(with_altitude / sessions * 100, 1) if sessions else 0,
            "habitual_altitude_m": float(habitual) if habitual is not None else None,
            "above_habitual": above,
            "below_habitual": below,
            "altitude_bands": bands,
            "countries": countries,
            "highest_sessions": highest,
            "interpretation": {
                "absolute_altitude": "Elevation above sea level.",
                "ascent": "Accumulated climbing inside the session.",
                "acclimatization": "Comparable-altitude training days in the prior 21 days.",
                "performance_use": "Context for comparison; it does not automatically add fitness points.",
            },
        }
    except Exception as e:
        logger.error(f"Environment summary error: {e}")
        raise HTTPException(500, str(e))


@app.get("/gpt/session-environment/{session_id}")
def gpt_session_environment(session_id: str):
    """Environmental context for one clean session."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT se.*, cs.start_time, cs.name, cs.sport, cs.distance_km
            FROM session_environment se
            JOIN clean_sessions cs USING (clean_session_id)
            WHERE se.clean_session_id = %s
               OR cs.original_session_id = %s
               OR cs.source_activity_id = %s
            ORDER BY CASE WHEN se.clean_session_id = %s THEN 0 ELSE 1 END
            LIMIT 1
        """, (session_id, session_id, session_id, session_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Contexto ambiental no encontrado")
        columns = [description[0] for description in cur.description]
    result = dict(zip(columns, row))
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif hasattr(value, "as_tuple"):
            result[key] = float(value)
    return {
        "ok": True,
        "environment": result,
        "interpretation": {
            "altitude_band": "Absolute elevation above sea level.",
            "relative_altitude_band": "Difference versus this athlete's habitual altitude.",
            "acclimatization_status": "Estimated only from recent comparable training exposure.",
        },
    }


@app.get("/admin/phase1-audit")
def admin_phase1_audit(token: str = None):
    """Auditoría compacta de la columna Fase 1: data, gear, rutas y telemetría."""
    if token != os.environ.get("ADMIN_TOKEN"):
        raise HTTPException(401, "Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_clean_sessions_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE calories IS NOT NULL AND calories > 0) AS with_calories,
                    COALESCE(SUM(calories), 0) AS calories_total,
                    COUNT(*) FILTER (WHERE start_lat IS NOT NULL AND start_lon IS NOT NULL) AS with_start_gps,
                    COUNT(*) FILTER (WHERE end_lat IS NOT NULL AND end_lon IS NOT NULL) AS with_end_gps,
                    COUNT(*) FILTER (WHERE route_id IS NOT NULL) AS with_route_id,
                    MIN(start_time)::text AS date_min,
                    MAX(start_time)::text AS date_max
                FROM clean_sessions
            """)
            total, with_cal, cal_total, start_gps, end_gps, with_route, date_min, date_max = cur.fetchone()

            cur.execute("""
                SELECT sport, COUNT(*)
                FROM clean_sessions
                GROUP BY sport
                ORDER BY COUNT(*) DESC
                LIMIT 12
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("""
                SELECT
                    COUNT(*) AS gear_total,
                    COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) NOT LIKE '%retired%') AS active_or_unknown,
                    COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) LIKE '%retired%') AS retired
                FROM garmin_export_gear
            """)
            gear_total, gear_active, gear_retired = cur.fetchone()

            cur.execute("""
                SELECT name, type, status, max_distance_km
                FROM garmin_export_gear
                ORDER BY
                    CASE WHEN LOWER(COALESCE(status,'')) LIKE '%retired%' THEN 1 ELSE 0 END,
                    name
                LIMIT 12
            """)
            gear_sample = [
                {"name": r[0], "type": r[1], "status": r[2], "max_distance_km": float(r[3]) if r[3] is not None else None}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT COUNT(*), COUNT(DISTINCT gear_id), COUNT(DISTINCT COALESCE(session_id, source_activity_id))
                FROM gear_activity_links
            """)
            gear_links_total, gear_links_items, gear_links_activities = cur.fetchone()

            cur.execute("""
                WITH grouped AS (
                    SELECT route_id, MAX(sport) AS sport, COUNT(*) AS efforts,
                           ROUND(AVG(distance_km)::numeric, 2) AS distance_km,
                           ROUND(AVG(ascent_m)::numeric, 0) AS ascent_m,
                           MIN(start_time)::date AS first_ride,
                           MAX(start_time)::date AS last_ride
                    FROM clean_sessions
                    WHERE route_id IS NOT NULL
                    GROUP BY route_id
                )
                SELECT
                    COUNT(*) AS routes_total,
                    COUNT(*) FILTER (WHERE efforts >= 2) AS matched_routes,
                    COALESCE(SUM(efforts) FILTER (WHERE efforts >= 2), 0) AS matched_efforts
                FROM grouped
            """)
            routes_total, matched_routes, matched_efforts = cur.fetchone()

            cur.execute("""
                WITH grouped AS (
                    SELECT route_id, MAX(sport) AS sport, COUNT(*) AS efforts,
                           ROUND(AVG(distance_km)::numeric, 2) AS distance_km,
                           ROUND(AVG(ascent_m)::numeric, 0) AS ascent_m,
                           MIN(start_time)::date AS first_ride,
                           MAX(start_time)::date AS last_ride
                    FROM clean_sessions
                    WHERE route_id IS NOT NULL
                    GROUP BY route_id
                )
                SELECT route_id, sport, efforts, distance_km, ascent_m, first_ride::text, last_ride::text
                FROM grouped
                WHERE efforts >= 2
                ORDER BY efforts DESC, last_ride DESC
                LIMIT 10
            """)
            matched_sample = [
                {
                    "route_id": r[0],
                    "sport": r[1],
                    "efforts": r[2],
                    "distance_km": float(r[3]) if r[3] is not None else None,
                    "ascent_m": int(r[4]) if r[4] is not None else None,
                    "first_ride": r[5],
                    "last_ride": r[6],
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT
                    COUNT(*) AS records,
                    COUNT(DISTINCT session_id) AS sessions_with_records,
                    COUNT(DISTINCT session_id) FILTER (WHERE lat IS NOT NULL AND lon IS NOT NULL) AS sessions_with_map_points,
                    COUNT(DISTINCT session_id) FILTER (WHERE altitude IS NOT NULL) AS sessions_with_altimetry,
                    COUNT(DISTINCT session_id) FILTER (WHERE power IS NOT NULL) AS sessions_with_power
                FROM session_records
            """)
            records, sessions_records, sessions_map, sessions_alt, sessions_power = cur.fetchone()

        return {
            "ok": True,
            "clean_sessions": {
                "total": total,
                "date_min": date_min,
                "date_max": date_max,
                "by_sport": by_sport,
            },
            "calories": {
                "sessions_with_calories": with_cal,
                "coverage_pct": round((with_cal / total * 100), 1) if total else 0,
                "total_kcal": int(cal_total or 0),
                "source": "Garmin account export / FIT summary when available",
            },
            "gear": {
                "garmin_items": gear_total,
                "active_or_unknown": gear_active,
                "retired": gear_retired,
                "activity_links_ready": True,
                "activity_links_total": gear_links_total,
                "linked_gear_items": gear_links_items,
                "linked_activities": gear_links_activities,
                "sample": gear_sample,
                "note": "Garmin export trae catálogo de gear, pero no siempre trae asignación por actividad; esa liga fina requiere activity FIT/TCX/GPX o API Garmin.",
            },
            "matched_rides": {
                "sessions_with_route_id": with_route,
                "routes_total": routes_total,
                "routes_with_2_or_more_efforts": matched_routes,
                "matched_efforts": matched_efforts,
                "sample": matched_sample,
                "note": "Con export resumido se agrupa por firma aproximada de inicio/fin/distancia/desnivel. Para igualar Strava con más precisión necesitamos la trayectoria completa.",
            },
            "maps_and_charts": {
                "sessions_with_start_gps": start_gps,
                "sessions_with_end_gps": end_gps,
                "session_record_points": records,
                "sessions_with_time_series": sessions_records,
                "sessions_with_map_points": sessions_map,
                "sessions_with_altimetry_points": sessions_alt,
                "sessions_with_power_points": sessions_power,
                "note": "El resumen Garmin da inicio/fin y métricas. Mapa, altimetría y gráficas por segundo viven en el FIT/TCX/GPX original.",
            },
            "verdict": "La columna Fase 1 debe usar clean_sessions como índice maestro; usar session_records sólo cuando exista archivo original para mapas/gráficas.",
        }
    except Exception as e:
        logger.error(f"Phase 1 audit error: {e}")
        raise HTTPException(500, str(e))


@app.get("/admin/backup")
def admin_backup(token: str = ""):
    """Exporta todas las tablas en JSON. Requiere token de entorno ADMIN_TOKEN."""
    import os
    admin_token = os.environ.get("ADMIN_TOKEN")
    if token != admin_token:
        raise HTTPException(403, "Token inválido. Usa ?token=TU_TOKEN")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    tables = ["sessions", "routes", "post_session", "gear", "maintenance",
              "recovery", "fuerza", "wellness", "accidents",
              "athlete_profile", "athlete_tests", "achievements", "athlete_snapshots",
              "garmin_export_activities", "garmin_export_gear", "garmin_export_sleep",
              "clean_sessions", "gear_activity_links", "zone_models",
              "session_environment", "capability_runs"]
    backup = {}
    try:
        with conn.cursor() as cur:
            for table in tables:
                try:
                    cur.execute(f"SELECT * FROM {table}")
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    clean = []
                    for row in rows:
                        item = dict(zip(cols, row))
                        for k, v in item.items():
                            if hasattr(v, "isoformat"):
                                item[k] = v.isoformat()
                        clean.append(item)
                    backup[table] = clean
                except Exception as te:
                    backup[table] = {"error": str(te)}
        logger.info(f"Backup generated: {sum(len(v) if isinstance(v,list) else 0 for v in backup.values())} total rows")
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tables": backup
        }
    except Exception as e:
        logger.error(f"Backup error: {e}")
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 6 — Progreso Histórico
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_detail(session_id: str, debug: bool = False):
    sid = json.dumps(session_id)
    debug_block = """
      <details>
        <summary>Datos tecnicos</summary>
        <pre id="raw"></pre>
      </details>
    """ if debug else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Sesion Mars</title>
  <style>
    body{{margin:0;background:#08090b;color:#f7f7f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
    main{{max-width:860px;margin:0 auto;padding:22px 16px 96px}}
    a{{color:#e8593c;text-decoration:none;font-weight:800}}
    .top{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}}
    .card{{background:#15171c;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:18px;margin:12px 0}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
    .metric{{background:#1c1f26;border-radius:12px;padding:14px}}
    .label{{color:#8e95a3;font-size:12px;font-weight:700;text-transform:uppercase}}
    .value{{font-size:24px;font-weight:900;margin-top:4px}}
    .note{{color:#cfd3dc;line-height:1.45;font-size:15px}}
    .chips{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}
    .chip{{background:rgba(232,89,60,.12);color:#e8593c;border-radius:999px;padding:7px 10px;font-size:12px;font-weight:800}}
    details{{margin-top:12px;color:#8e95a3}}
    pre{{white-space:pre-wrap;word-break:break-word;color:#cfd3dc;max-height:280px;overflow:auto}}
    .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}
    .btn{{display:inline-block;background:#1c1f26;border:1px solid rgba(255,255,255,.11);border-radius:10px;padding:10px 12px}}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <a href="/dashboard" onclick="if(history.length>1){{history.back();return false;}}">← Volver</a>
      <a id="charts" href="#">Gráficas</a>
    </div>
    <section class="card">
      <div class="label">Sesion</div>
      <h1 id="title">Cargando...</h1>
      <div class="grid" id="metrics"></div>
    </section>
    <section class="card">
      <div class="label">Detalle</div>
      <div id="summary" class="note">Cargando detalle...</div>
      <div class="chips" id="chips"></div>
      {debug_block}
    </section>
  </main>
  <script>
    const sid = {sid};
    document.getElementById('charts').href = '/charts/' + sid;
    const hms = s => {{
      s = Number(s || 0);
      return String(Math.floor(s/3600)).padStart(2,'0') + 'h ' + String(Math.floor((s%3600)/60)).padStart(2,'0') + 'm';
    }};
    const fmtDate = v => {{
      if(!v) return '—';
      try {{
        const d = new Date(String(v).slice(0,10) + 'T12:00:00');
        const months = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
        return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
      }} catch(e) {{
        return String(v);
      }}
    }};
    fetch('/gpt/session/' + sid).then(async r => {{
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }}).then(data => {{
      const s = data.session || data || {{}};
      document.getElementById('title').textContent = s.workout_name || s.sport || sid;
      document.getElementById('metrics').innerHTML = [
        ['Distancia', (s.distance_km ?? '--') + ' km'],
        ['Duracion', s.duration_hms || hms(s.duration_s)],
        ['FC prom', (s.avg_hr_bpm ?? '--') + ' bpm'],
        ['Vel prom', (s.avg_speed_kmh ?? '--') + ' km/h'],
        ['Ascenso', '+' + (s.ascent_m ?? '--') + ' m'],
        ['Cadencia', (s.avg_cadence_rpm ?? s.avg_cadence ?? '--') + ' rpm'],
        ['Calorias', (s.calories ?? s.calories_kcal) ? (s.calories ?? s.calories_kcal) + ' kcal' : 'sin dato'],
        ['Eficiencia', (s.efficiency ?? '--')]
      ].map(([k,v]) => `<div class="metric"><div class="label">${{k}}</div><div class="value">${{v}}</div></div>`).join('');
      const telemetry = s.has_telemetry
        ? (s.has_map ? 'Esta actividad tiene telemetría segundo a segundo: curvas y mapa disponibles.' : 'Esta actividad tiene curvas segundo a segundo, pero no suficientes puntos GPS para mapa.')
        : 'Esta actividad viene del resumen limpio Garmin. Todavía no tiene FIT/TCX original enlazado, por eso no hay mapa ni curvas segundo a segundo.';
      const cal = (s.calories ?? s.calories_kcal);
      const calNote = cal ? ' Calorías Garmin: ' + cal + ' kcal.' : ' Calorías no disponibles en esta sesión.';
      document.getElementById('summary').textContent = telemetry + calNote;
      document.getElementById('chips').innerHTML = [
        s.start_time ? 'Fecha: ' + fmtDate(s.start_time) : null,
        s.sport ? 'Deporte: ' + s.sport : null,
        s.route_id ? 'Ruta: ' + s.route_id : null,
        s.source ? 'Fuente: ' + s.source : null,
        s.quality ? 'Calidad: ' + s.quality : null,
        s.has_telemetry ? 'Telemetria: ' + (s.telemetry_points || 0) + ' puntos' : 'Sin telemetria original',
        s.has_map ? 'Mapa disponible' : 'Sin mapa'
      ].filter(Boolean).map(x => '<span class="chip">' + x + '</span>').join('');
      const detail = document.querySelector('.card:nth-of-type(2)');
      if(detail){{
        const actions = document.createElement('div');
        actions.className = 'actions';
        actions.innerHTML = [
          '<a class="btn" href="/charts/' + sid + '">Graficas</a>',
          s.route_id ? '<a class="btn" href="/route/' + s.route_id + '/matched">Comparar ruta</a>' : ''
        ].join('');
        const target = detail.querySelector('details');
        if(target){{ detail.insertBefore(actions, target); }}
        else{{ detail.appendChild(actions); }}
      }}
      const raw = document.getElementById('raw');
      if(raw) raw.textContent = JSON.stringify(data, null, 2);
    }}).catch(err => {{
      document.getElementById('title').textContent = 'No se pudo cargar';
      document.getElementById('summary').textContent = 'No encontré esta actividad en la capa limpia. Puede faltar sincronizar el session_id o enlazar el archivo original.';
      const raw = document.getElementById('raw');
      if(raw) raw.textContent = String(err.message || err);
    }});
  </script>
</body>
</html>""")


# Full legacy SESSION_DETAIL_HTML was removed; this route now serves a compact DB-backed detail view.


# ═══════════════════════════════════════════════════════════════════════════════
# UI FINAL — App completa unificada
# Rediseña /home, /dashboard, /activities, /gear, /calendar, /performance y /app
# con una sola experiencia móvil tipo PWA.
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# INTELIGENCIA FASE 2 — CORRELACIONES Y TENDENCIA
# ═══════════════════════════════════════════════════════════════════════════════


def _pearson(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xvals = [x for x, _ in pairs]
    yvals = [y for _, y in pairs]
    mx = sum(xvals) / len(xvals)
    my = sum(yvals) / len(yvals)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = (
        sum((x - mx) ** 2 for x in xvals) *
        sum((y - my) ** 2 for y in yvals)
    ) ** 0.5
    return round(numerator / denominator, 4) if denominator else None


def _linear_regression(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    denominator = sum((x - mx) ** 2 for x, _ in pairs)
    if not denominator:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / denominator
    return {"slope": slope, "intercept": my - slope * mx}


def _solve_3x3(matrix, vector):
    augmented = [list(map(float, row)) + [float(value)] for row, value in zip(matrix, vector)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][3] for row in range(3)]


def _multiple_regression_hr_speed_temperature(rows):
    # HR = intercept + speed_coefficient * speed + temperature_coefficient * temperature
    if len(rows) < 12:
        return None
    n = float(len(rows))
    speeds = [float(row["speed"]) for row in rows]
    temperatures = [float(row["temperature"]) for row in rows]
    hrs = [float(row["hr"]) for row in rows]
    matrix = [
        [n, sum(speeds), sum(temperatures)],
        [sum(speeds), sum(x * x for x in speeds), sum(x * t for x, t in zip(speeds, temperatures))],
        [sum(temperatures), sum(x * t for x, t in zip(speeds, temperatures)), sum(t * t for t in temperatures)],
    ]
    vector = [
        sum(hrs),
        sum(x * y for x, y in zip(speeds, hrs)),
        sum(t * y for t, y in zip(temperatures, hrs)),
    ]
    coefficients = _solve_3x3(matrix, vector)
    if not coefficients:
        return None
    predicted = [
        coefficients[0] + coefficients[1] * speed + coefficients[2] * temperature
        for speed, temperature in zip(speeds, temperatures)
    ]
    mean_hr = sum(hrs) / len(hrs)
    total_variance = sum((hr - mean_hr) ** 2 for hr in hrs)
    residual_variance = sum((hr - estimate) ** 2 for hr, estimate in zip(hrs, predicted))
    r2 = 1 - residual_variance / total_variance if total_variance else None
    return {
        "intercept": coefficients[0],
        "bpm_per_kmh": coefficients[1],
        "bpm_per_celsius": coefficients[2],
        "r2": r2,
    }


def _evidence_level(sample_size, correlation=None):
    if sample_size < 8:
        return "insuficiente"
    if sample_size < 20:
        return "baja"
    if sample_size < 50:
        return "media"
    if correlation is not None and abs(correlation) < 0.15:
        return "media"
    return "alta"


def _model_evidence_level(sample_size, r2):
    if sample_size < 12 or r2 is None:
        return "insuficiente"
    if r2 < 0.15:
        return "baja"
    if r2 < 0.35:
        return "media"
    return "alta"


def _average(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else None


ATHLETIC_LOAD_WEIGHTS = {
    "cycling": 1.0,
    "indoor_cycling": 0.9,
    "running": 1.15,
    "trail_running": 1.2,
    "treadmill_running": 1.1,
    "lap_swimming": 1.0,
    "open_water_swimming": 1.1,
    "indoor_cardio": 0.8,
    "strength_training": 0.8,
    "walking": 0.5,
    "yoga": 0.4,
    "multi_sport": 1.05,
    "bikeToRunTransition_v2": 0.7,
}

ATHLETIC_SPORT_NAMES = {
    "cycling": "ciclismo",
    "indoor_cycling": "bici interior",
    "running": "correr",
    "trail_running": "trail running",
    "treadmill_running": "caminadora",
    "lap_swimming": "natacion",
    "open_water_swimming": "aguas abiertas",
    "indoor_cardio": "cardio interior",
    "strength_training": "fuerza",
    "walking": "caminata",
    "yoga": "movilidad",
    "multi_sport": "multideporte",
    "bikeToRunTransition_v2": "transicion bici-correr",
}


def _athletic_load_hours(sport_breakdown):
    breakdown = sport_breakdown or {}
    return sum(
        float((values or {}).get("hours") or 0) * ATHLETIC_LOAD_WEIGHTS.get(sport, 0.65)
        for sport, values in breakdown.items()
    )


@app.get("/gpt/athletic-status")
def gpt_athletic_status():
    """Lectura semanal de carga, continuidad y diversidad de toda la actividad."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT week_start, week_end, hours_week, sessions, active_days,
                       calories_week, sport_breakdown
                FROM athlete_snapshots
                WHERE week_end < CURRENT_DATE
                ORDER BY week_start DESC
                LIMIT 16
            """)
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT week_start, week_end, hours_week, sessions, active_days,
                       calories_week, sport_breakdown
                FROM athlete_snapshots
                ORDER BY week_start DESC
                LIMIT 1
            """)
            current_row = cur.fetchone()
            current = dict(zip([d[0] for d in cur.description], current_row)) if current_row else None

        if len(rows) < 8:
            return {"ok": False, "estado": "datos_insuficientes", "semanas_disponibles": len(rows)}

        recent = list(reversed(rows[:4]))
        previous = list(reversed(rows[4:8]))

        def summarize(block):
            discipline_hours = {}
            for row in block:
                for sport, values in (row.get("sport_breakdown") or {}).items():
                    discipline_hours[sport] = (
                        discipline_hours.get(sport, 0)
                        + float((values or {}).get("hours") or 0)
                    )
            dominant_sport = (
                max(discipline_hours, key=discipline_hours.get)
                if discipline_hours else None
            )
            return {
                "desde": block[0]["week_start"].isoformat(),
                "hasta": block[-1]["week_end"].isoformat(),
                "carga_equivalente_h_promedio": round(
                    _average([_athletic_load_hours(row["sport_breakdown"]) for row in block]), 2
                ),
                "horas_promedio": round(_average([row["hours_week"] for row in block]), 2),
                "sesiones_promedio": round(_average([row["sessions"] for row in block]), 1),
                "dias_activos_promedio": round(_average([row["active_days"] for row in block]), 1),
                "calorias_promedio": round(_average([row["calories_week"] for row in block]), 0),
                "disciplina_dominante": dominant_sport,
                "horas_por_disciplina": {
                    sport: round(hours, 2)
                    for sport, hours in sorted(discipline_hours.items())
                },
            }

        recent_summary = summarize(recent)
        previous_summary = summarize(previous)

        def pct_change(current_value, previous_value):
            if current_value is None or previous_value in (None, 0):
                return None
            return (current_value - previous_value) / abs(previous_value) * 100

        load_delta = pct_change(
            recent_summary["carga_equivalente_h_promedio"],
            previous_summary["carga_equivalente_h_promedio"],
        )
        active_days_delta = (
            recent_summary["dias_activos_promedio"] - previous_summary["dias_activos_promedio"]
        )
        session_delta = pct_change(
            recent_summary["sesiones_promedio"],
            previous_summary["sesiones_promedio"],
        )

        chronological = list(reversed(rows))
        active_streak = 0
        for row in reversed(chronological):
            if int(row.get("sessions") or 0) <= 0:
                break
            active_streak += 1
        recent_empty_weeks = sum(1 for row in rows[:8] if int(row.get("sessions") or 0) == 0)

        current_breakdown = (current or {}).get("sport_breakdown") or {}
        current_disciplines = [
            sport for sport, values in current_breakdown.items()
            if int((values or {}).get("sessions") or 0) > 0
        ]
        current_load = _athletic_load_hours(current_breakdown)
        previous_load = previous_summary["carga_equivalente_h_promedio"] or 0
        recent_load = recent_summary["carga_equivalente_h_promedio"] or 0
        discipline_change = bool(
            recent_summary["disciplina_dominante"]
            and previous_summary["disciplina_dominante"]
            and recent_summary["disciplina_dominante"] != previous_summary["disciplina_dominante"]
        )

        if load_delta is not None and load_delta > 200 and (
            previous_load < 2 or previous_load < recent_load * 0.35
        ):
            state = "regreso_tras_pausa"
            direction = "ascendente_con_cautela"
            explanation = (
                "Volviste a entrenar con regularidad despues de un bloque de carga baja. "
                "La prioridad es sostener esta frecuencia antes de volver a subir."
            )
            recommendation = "Mantener una semana similar, sin aumentar volumen ni intensidad."
        elif load_delta is not None and load_delta > 60 and discipline_change:
            state = "transicion_de_disciplina"
            direction = "ascendente_con_cautela"
            explanation = (
                "La carga subio al pasar de "
                f"{ATHLETIC_SPORT_NAMES.get(previous_summary['disciplina_dominante'], previous_summary['disciplina_dominante'])} a "
                f"{ATHLETIC_SPORT_NAMES.get(recent_summary['disciplina_dominante'], recent_summary['disciplina_dominante'])}. "
                "El porcentaje refleja un cambio de disciplina, no solo mas entrenamiento."
            )
            recommendation = "Consolidar la nueva disciplina antes de aumentar otra vez la carga."
        elif load_delta is not None and load_delta > 60:
            state = "pico_de_carga"
            direction = "en_observacion"
            explanation = "La carga total crecio rapidamente frente al bloque anterior."
            recommendation = "Consolidar la carga y priorizar recuperacion antes de otro aumento."
        elif load_delta is not None and load_delta < -40:
            state = "descarga_o_pausa"
            direction = "descendente"
            explanation = "La carga total bajo claramente durante las ultimas cuatro semanas."
            recommendation = "Distinguir si es una descarga planeada o una perdida de continuidad."
        elif active_streak >= 4 and active_days_delta >= 0:
            state = "continuidad_solida"
            direction = "estable_positiva"
            explanation = "La frecuencia se mantiene y ya existe una base continua de varias semanas."
            recommendation = "Mantener la constancia y cambiar solo una variable a la vez."
        else:
            state = "construyendo_continuidad"
            direction = "estable"
            explanation = "La actividad esta presente, pero la continuidad todavia puede consolidarse."
            recommendation = "Priorizar dias activos regulares antes de perseguir mas volumen."

        def serialize_week(row):
            if not row:
                return None
            return {
                "week_start": row["week_start"].isoformat(),
                "week_end": row["week_end"].isoformat(),
                "hours": float(row.get("hours_week") or 0),
                "load_equivalent_hours": round(_athletic_load_hours(row.get("sport_breakdown")), 2),
                "sessions": int(row.get("sessions") or 0),
                "active_days": int(row.get("active_days") or 0),
                "sport_breakdown": row.get("sport_breakdown") or {},
            }

        return {
            "ok": True,
            "estado": state,
            "direccion": direction,
            "explicacion": explanation,
            "recomendacion": recommendation,
            "ultimas_4": recent_summary,
            "anteriores_4": previous_summary,
            "cambios": {
                "carga_pct": round(load_delta, 1) if load_delta is not None else None,
                "dias_activos": round(active_days_delta, 1),
                "sesiones_pct": round(session_delta, 1) if session_delta is not None else None,
            },
            "continuidad": {
                "racha_semanas_activas": active_streak,
                "semanas_vacias_ultimas_8": recent_empty_weeks,
            },
            "transicion_disciplina": {
                "activa": discipline_change,
                "anterior": previous_summary["disciplina_dominante"],
                "actual": recent_summary["disciplina_dominante"],
            },
            "semana_actual": serialize_week(current),
            "carga_actual_equivalente_h": round(current_load, 2),
            "disciplinas_actuales": current_disciplines,
            "metodo_carga": {
                "unidad": "horas_equivalentes",
                "pesos": ATHLETIC_LOAD_WEIGHTS,
                "nota": "Proxy transparente para comparar disciplinas; no es una medida medica.",
            },
            "historial": [serialize_week(row) for row in chronological[-12:]],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/correlaciones")
@app.get("/gpt/correlations")
def gpt_correlations(sport: str = "cycling"):
    """Cuatro relaciones personales basadas en snapshots y sesiones limpias."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport debe ser cycling o running")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        efficiency_column = "cycling_efficiency" if sport == "cycling" else "running_efficiency"
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    week_start, km_week, hours_week, sessions, active_days,
                    weight_kg, avg_hr, cycling_efficiency, running_efficiency,
                    sport_breakdown
                FROM athlete_snapshots
                ORDER BY week_start
            """)
            snapshot_columns = [d[0] for d in cur.description]
            snapshots = [dict(zip(snapshot_columns, row)) for row in cur.fetchall()]

            sport_values = (
                ("cycling", "indoor_cycling")
                if sport == "cycling"
                else ("running", "trail_running", "treadmill_running")
            )
            cur.execute("""
                WITH ordered AS (
                    SELECT
                        start_time,
                        sport,
                        avg_hr_bpm::float AS hr,
                        avg_speed_kmh::float AS speed,
                        efficiency_speed_hr::float AS efficiency,
                        EXTRACT(EPOCH FROM (
                            start_time - LAG(start_time) OVER (ORDER BY start_time)
                        )) / 86400.0 AS gap_days
                    FROM clean_sessions
                    WHERE sport IN %s
                      AND start_time IS NOT NULL
                      AND avg_hr_bpm BETWEEN 60 AND 210
                      AND avg_speed_kmh > 0
                )
                SELECT gap_days, hr, speed, efficiency
                FROM ordered
                WHERE gap_days IS NOT NULL AND gap_days BETWEEN 0 AND 14
                ORDER BY start_time
            """, (sport_values,))
            rest_rows = [
                {"gap_days": float(row[0]), "hr": float(row[1]), "speed": float(row[2]),
                 "efficiency": float(row[3]) if row[3] is not None else float(row[2]) / float(row[1])}
                for row in cur.fetchall()
            ]

            heat_rows = []
            if sport == "cycling":
                cur.execute("""
                    SELECT
                        avg_speed_kmh::float AS speed,
                        avg_hr_bpm::float AS hr,
                        COALESCE(
                            (
                                NULLIF(raw_json->>'minTemperature','')::numeric +
                                NULLIF(raw_json->>'maxTemperature','')::numeric
                            ) / 2,
                            NULLIF(raw_json->>'minTemperature','')::numeric,
                            NULLIF(raw_json->>'maxTemperature','')::numeric
                        )::float AS temperature
                    FROM clean_sessions
                    WHERE sport IN ('cycling','indoor_cycling')
                      AND source='garmin_export'
                      AND avg_speed_kmh BETWEEN 8 AND 55
                      AND avg_hr_bpm BETWEEN 70 AND 200
                      AND (
                        raw_json ? 'minTemperature' OR raw_json ? 'maxTemperature'
                      )
                """)
                heat_rows = [
                    {"speed": float(row[0]), "hr": float(row[1]), "temperature": float(row[2])}
                    for row in cur.fetchall()
                    if row[2] is not None and -10 <= float(row[2]) <= 50
                ]

        rest_buckets = {
            "menos_de_1_dia": (0, 1),
            "1_dia": (1, 2),
            "2_dias": (2, 3),
            "3_dias": (3, 4),
            "4_a_7_dias": (4, 8),
            "8_a_14_dias": (8, 15),
        }
        rest_summary = []
        for label, (low, high) in rest_buckets.items():
            rows = [row for row in rest_rows if low <= row["gap_days"] < high]
            if not rows:
                continue
            rest_summary.append({
                "rango": label,
                "sesiones": len(rows),
                "fc_promedio": round(_average([row["hr"] for row in rows]), 1),
                "eficiencia_promedio": round(_average([row["efficiency"] for row in rows]), 5),
            })
        eligible_rest = [row for row in rest_summary if row["sesiones"] >= 8]
        optimal_rest = max(eligible_rest, key=lambda row: row["eficiencia_promedio"]) if eligible_rest else None

        def sport_volume(snapshot, metric):
            breakdown = snapshot.get("sport_breakdown") or {}
            sport_names = (
                ("cycling", "indoor_cycling")
                if sport == "cycling"
                else ("running", "trail_running", "treadmill_running")
            )
            return sum(float((breakdown.get(name) or {}).get(metric) or 0) for name in sport_names)

        volume_pairs = []
        for previous, current in zip(snapshots, snapshots[1:]):
            efficiency = current.get(efficiency_column)
            if efficiency is not None:
                volume_pairs.append((sport_volume(previous, "km"), float(efficiency)))
        volume_corr = _pearson(
            [pair[0] for pair in volume_pairs],
            [pair[1] for pair in volume_pairs],
        )
        volume_buckets = [(0, 25), (25, 50), (50, 100), (100, 150), (150, 200), (200, 100000)]
        volume_summary = []
        for low, high in volume_buckets:
            rows = [eff for km, eff in volume_pairs if low <= km < high]
            if rows:
                volume_summary.append({
                    "km_semana_anterior": f"{low}-{high if high < 100000 else '+'}",
                    "semanas": len(rows),
                    "eficiencia_promedio": round(_average(rows), 5),
                })
        eligible_volume = [row for row in volume_summary if row["semanas"] >= 6]
        optimal_volume = max(eligible_volume, key=lambda row: row["eficiencia_promedio"]) if eligible_volume else None

        weight_rows = [
            {
                "year": row["week_start"].year,
                "week_start": row["week_start"],
                "weight": float(row["weight_kg"]),
                "efficiency": float(row[efficiency_column]),
            }
            for row in snapshots
            if row.get("weight_kg") is not None and row.get(efficiency_column) is not None
        ]
        weight_by_year = {}
        for row in weight_rows:
            weight_by_year.setdefault(row["year"], []).append(row)
        weight_year_summary = []
        adjusted_weights = []
        adjusted_efficiencies = []
        for year, rows in sorted(weight_by_year.items()):
            weights = [row["weight"] for row in rows]
            efficiencies = [row["efficiency"] for row in rows]
            year_corr = _pearson(weights, efficiencies)
            year_regression = _linear_regression(weights, efficiencies)
            weight_year_summary.append({
                "year": year,
                "weeks": len(rows),
                "correlation": round(year_corr, 4) if year_corr is not None else None,
                "change_efficiency_per_kg": (
                    round(year_regression["slope"], 6) if year_regression else None
                ),
            })
            if len(rows) < 6:
                continue
            mean_weight = _average(weights)
            mean_efficiency = _average(efficiencies)
            adjusted_weights.extend(weight - mean_weight for weight in weights)
            adjusted_efficiencies.extend(efficiency - mean_efficiency for efficiency in efficiencies)
        weight_corr = _pearson(
            adjusted_weights,
            adjusted_efficiencies,
        )
        weight_regression = _linear_regression(
            adjusted_weights,
            adjusted_efficiencies,
        )

        heat_model = _multiple_regression_hr_speed_temperature(heat_rows)
        heat_confidence = _model_evidence_level(
            len(heat_rows),
            heat_model.get("r2") if heat_model else None,
        )
        weight_slope = weight_regression["slope"] if weight_regression else None
        weight_direction = (
            "menor_peso_mejor_eficiencia" if weight_slope is not None and weight_slope < 0
            else "menor_peso_peor_eficiencia" if weight_slope is not None and weight_slope > 0
            else "sin_direccion"
        )
        weight_confounded = weight_direction == "menor_peso_peor_eficiencia"
        weight_usable_years = [
            row for row in weight_year_summary
            if row["weeks"] >= 6 and row["change_efficiency_per_kg"] is not None
        ]
        weight_evidence = (
            "insuficiente"
            if len(adjusted_weights) < 20 or len(weight_usable_years) < 2
            else "baja"
            if weight_corr is None or abs(weight_corr) < 0.2
            else "media"
        )
        weight_recommendable = bool(
            weight_regression
            and not weight_confounded
            and weight_evidence == "media"
        )
        return {
            "ok": True,
            "sport": sport,
            "metodo": "historia personal; asociaciones observacionales, no causalidad",
            "descanso_optimo": {
                "muestra": len(rest_rows),
                "por_rango": rest_summary,
                "mejor_rango": optimal_rest,
                "confianza": _evidence_level(len(rest_rows)),
                "lectura": (
                    f"El mejor rendimiento observado aparece tras {optimal_rest['rango'].replace('_',' ')}."
                    if optimal_rest else "Todavia no hay suficientes sesiones por rango."
                ),
            },
            "volumen_optimo": {
                "muestra_semanas": len(volume_pairs),
                "correlacion_km_previos_eficiencia": volume_corr,
                "por_rango": volume_summary,
                "mejor_rango": optimal_volume,
                "confianza": _evidence_level(len(volume_pairs), volume_corr),
                "lectura": (
                    f"El rango con mejor eficiencia posterior fue {optimal_volume['km_semana_anterior']} km."
                    if optimal_volume else "Todavia no hay un rango dominante con muestra suficiente."
                ),
            },
            "costo_calor": {
                "muestra_sesiones": len(heat_rows),
                "bpm_por_10c_a_misma_velocidad": (
                    round(heat_model["bpm_per_celsius"] * 10, 2) if heat_model else None
                ),
                "bpm_por_kmh": round(heat_model["bpm_per_kmh"], 3) if heat_model else None,
                "r2": round(heat_model["r2"], 3) if heat_model and heat_model["r2"] is not None else None,
                "confianza": heat_confidence,
                "usable_para_recomendacion": heat_confidence in ("media", "alta"),
                "lectura": (
                    f"Diez grados adicionales se asocian con "
                    f"{heat_model['bpm_per_celsius'] * 10:+.1f} bpm a velocidad comparable; "
                    f"el modelo explica {heat_model['r2'] * 100:.1f}% de la variacion."
                    if heat_model else "No hay muestra suficiente para aislar temperatura y velocidad."
                ),
            },
            "peso_rendimiento": {
                "muestra_semanas": len(weight_rows),
                "muestra_ajustada_semanas": len(adjusted_weights),
                "metodo": "efectos_fijos_por_anio",
                "por_anio": weight_year_summary,
                "correlacion": weight_corr,
                "cambio_eficiencia_por_kg": (
                    round(weight_regression["slope"], 6) if weight_regression else None
                ),
                "cambio_estimado_al_bajar_1kg": (
                    round(-weight_regression["slope"], 6) if weight_regression else None
                ),
                "direccion_observada": weight_direction,
                "posibles_confusores": (
                    ["ruta", "volumen", "estado_de_forma"]
                    if weight_confounded else []
                ),
                "usable_para_recomendacion": weight_recommendable,
                "confianza": (
                    "baja_por_confusores"
                    if weight_confounded
                    else weight_evidence
                ),
                "lectura": (
                    "Aun controlando por año, la asociacion sale en direccion contraintuitiva; "
                    "no debe usarse para recomendar peso hasta controlar ruta, volumen y estado de forma."
                    if weight_confounded else
                    f"Comparando semanas dentro del mismo año, bajar 1 kg se asocia con "
                    f"{-weight_regression['slope']:+.5f} de eficiencia {sport}."
                    if weight_regression else "Faltan semanas con peso y eficiencia coincidentes."
                ),
            },
            "notas": [
                "Descanso compara el intervalo previo con la eficiencia de la siguiente sesion.",
                "Volumen usa km de la semana anterior contra eficiencia de la semana siguiente.",
                "Calor controla estadisticamente la velocidad, pero no ruta, viento ni altimetria.",
                "Peso no se interpola y se compara dentro de cada año para evitar mezclar epocas.",
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/tendencia")
def gpt_tendencia(sport: str = "cycling"):
    """Compara las ultimas cuatro semanas completas contra las cuatro anteriores."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport debe ser cycling o running")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    efficiency_column = "cycling_efficiency" if sport == "cycling" else "running_efficiency"
    sport_values = (
        ("cycling", "indoor_cycling")
        if sport == "cycling"
        else ("running", "trail_running", "treadmill_running")
    )
    sport_label = "ciclismo" if sport == "cycling" else "correr"
    comparable_best_sports = ("cycling",) if sport == "cycling" else sport_values
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    a.week_start, a.week_end, a.km_week, a.hours_week, a.sessions,
                    a.sport_breakdown,
                    (
                        SELECT SUM(cs.avg_hr_bpm * cs.duration_s) / NULLIF(SUM(cs.duration_s),0)
                        FROM clean_sessions cs
                        WHERE cs.start_date BETWEEN a.week_start AND a.week_end
                          AND cs.sport IN %s
                          AND cs.avg_hr_bpm > 0
                          AND cs.duration_s > 0
                    ) AS avg_hr,
                    a.{efficiency_column} AS efficiency
                FROM athlete_snapshots a
                WHERE a.week_end < CURRENT_DATE
                ORDER BY a.week_start DESC
                LIMIT 8
            """, (sport_values,))
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute(f"""
                SELECT
                    a.week_start, a.week_end, a.km_week, a.hours_week, a.sessions,
                    a.sport_breakdown,
                    (
                        SELECT SUM(cs.avg_hr_bpm * cs.duration_s) / NULLIF(SUM(cs.duration_s),0)
                        FROM clean_sessions cs
                        WHERE cs.start_date BETWEEN a.week_start AND a.week_end
                          AND cs.sport IN %s
                          AND cs.avg_hr_bpm > 0
                          AND cs.duration_s > 0
                    ) AS avg_hr,
                    a.{efficiency_column} AS efficiency
                FROM athlete_snapshots a
                ORDER BY a.week_start DESC
                LIMIT 1
            """, (sport_values,))
            current_row = cur.fetchone()
            current_week = dict(zip([d[0] for d in cur.description], current_row)) if current_row else None
            cur.execute("""
                WITH weekly AS (
                    SELECT
                        DATE_TRUNC('week', start_time)::date AS week_start,
                        (
                            SUM((avg_speed_kmh / avg_hr_bpm) * duration_s) /
                            NULLIF(SUM(duration_s), 0)
                        )::float AS efficiency
                    FROM clean_sessions
                    WHERE sport IN %s
                      AND start_time IS NOT NULL
                      AND avg_speed_kmh > 0
                      AND avg_hr_bpm BETWEEN 60 AND 210
                      AND duration_s > 0
                    GROUP BY DATE_TRUNC('week', start_time)::date
                    HAVING COUNT(*) >= 2 AND SUM(duration_s) >= 7200
                ),
                benchmark AS (
                    SELECT PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY efficiency) AS efficiency
                    FROM weekly
                )
                SELECT weekly.week_start, weekly.efficiency
                FROM weekly, benchmark
                ORDER BY ABS(weekly.efficiency - benchmark.efficiency), weekly.week_start DESC
                LIMIT 1
            """, (comparable_best_sports,))
            best_row = cur.fetchone()
            best_week = (
                {"week_start": best_row[0], "efficiency": float(best_row[1])}
                if best_row else None
            )

        if len(rows) < 8:
            return {
                "ok": False,
                "estado": "datos_insuficientes",
                "semanas_disponibles": len(rows),
                "necesarias": 8,
            }
        recent = list(reversed(rows[:4]))
        previous = list(reversed(rows[4:8]))

        def block_sport_hours(row):
            breakdown = row.get("sport_breakdown") or {}
            return sum(
                float((breakdown.get(name) or {}).get("hours") or 0)
                for name in sport_values
            )

        def block_sport_km(row):
            breakdown = row.get("sport_breakdown") or {}
            return sum(
                float((breakdown.get(name) or {}).get("km") or 0)
                for name in sport_values
            )

        def block_summary(block):
            return {
                "desde": block[0]["week_start"].isoformat(),
                "hasta": block[-1]["week_end"].isoformat(),
                "km_promedio": round(_average([block_sport_km(row) for row in block]), 1),
                "horas_promedio": round(_average([block_sport_hours(row) for row in block]), 2),
                "sesiones_promedio": round(_average([row["sessions"] for row in block]), 1),
                "fc_promedio": (
                    round(_average([row["avg_hr"] for row in block]), 1)
                    if _average([row["avg_hr"] for row in block]) is not None else None
                ),
                "eficiencia_promedio": (
                    round(_average([row["efficiency"] for row in block]), 5)
                    if _average([row["efficiency"] for row in block]) is not None else None
                ),
            }

        recent_summary = block_summary(recent)
        previous_summary = block_summary(previous)
        recent_sport_hours = sum(block_sport_hours(row) for row in recent)
        previous_sport_hours = sum(block_sport_hours(row) for row in previous)

        def pct_change(current, prior):
            if current is None or prior in (None, 0):
                return None
            return (current - prior) / abs(prior) * 100

        efficiency_delta = pct_change(
            recent_summary["eficiencia_promedio"],
            previous_summary["eficiencia_promedio"],
        )
        hr_delta = (
            recent_summary["fc_promedio"] - previous_summary["fc_promedio"]
            if recent_summary["fc_promedio"] is not None and previous_summary["fc_promedio"] is not None
            else None
        )
        volume_delta = pct_change(
            recent_summary["horas_promedio"],
            previous_summary["horas_promedio"],
        )
        extreme_load = volume_delta is not None and volume_delta > 200
        load_context = None
        if extreme_load:
            previous_hours = previous_summary["horas_promedio"] or 0
            recent_hours = recent_summary["horas_promedio"] or 0
            load_context = (
                "regreso_tras_pausa"
                if previous_hours < 2 or previous_hours < recent_hours * 0.35
                else "pico_atipico"
            )

        signals = []
        score = 0
        if efficiency_delta is not None:
            if efficiency_delta >= 2:
                signals.append("eficiencia_mejora")
                score += 1
            elif efficiency_delta <= -2:
                signals.append("eficiencia_baja")
                score -= 1
        if hr_delta is not None:
            if hr_delta <= -2:
                signals.append("fc_baja")
                score += 1
            elif hr_delta >= 2:
                signals.append("fc_sube")
                score -= 1
        if volume_delta is not None:
            if 5 <= volume_delta <= 30:
                signals.append("volumen_sube_controlado")
                score += 1
            elif volume_delta < -20:
                signals.append("volumen_baja")
                score -= 1
            elif volume_delta > 30:
                signals.append("volumen_sube_fuerte")

        overload = (
            hr_delta is not None and hr_delta >= 3 and
            efficiency_delta is not None and efficiency_delta <= -2
        )
        abrupt_load = volume_delta is not None and volume_delta > 50
        if recent_sport_hours <= 0:
            state = "sin_actividad_reciente"
            direction = "pausa"
            explanation = (
                f"No hay sesiones recientes de {sport_label}. "
                "La historia sigue disponible, pero no existe una tendencia actual que interpretar."
            )
        elif previous_sport_hours <= 0:
            state = "regreso_tras_pausa"
            direction = "ascendente_con_cautela"
            explanation = (
                f"Las sesiones recientes de {sport_label} representan un regreso tras un bloque sin actividad."
            )
        elif overload:
            state = "posible_sobrecarga"
            direction = "descendente"
            explanation = "La FC subio mientras la eficiencia bajo; conviene reducir carga y observar recuperacion."
        elif extreme_load and load_context == "regreso_tras_pausa":
            state = "regreso_tras_pausa"
            direction = "ascendente_con_cautela"
            explanation = (
                "El aumento porcentual es grande porque el bloque anterior tuvo muy poco volumen. "
                "Es un regreso tras pausa: la respuesta aerobica es positiva, pero conviene consolidar."
            )
        elif extreme_load:
            state = "pico_atipico"
            direction = "en_observacion"
            explanation = (
                "El volumen reciente es un pico atipico frente al bloque anterior. "
                "No conviene usar el porcentaje aislado ni volver a aumentar la carga de inmediato."
            )
        elif abrupt_load and efficiency_delta is not None and efficiency_delta >= 2:
            state = "respuesta_positiva_carga_alta"
            direction = "ascendente_con_cautela"
            explanation = (
                "La eficiencia mejoro, pero el volumen subio bruscamente. "
                "La respuesta es positiva; conviene consolidar la carga antes de aumentarla otra vez."
            )
        elif abrupt_load:
            state = "carga_en_observacion"
            direction = "incierta"
            explanation = (
                "El volumen subio bruscamente y todavia no hay una mejora aerobica clara. "
                "Conviene observar recuperacion y no aumentar carga esta semana."
            )
        elif score >= 2:
            state = "mejorando"
            direction = "ascendente"
            explanation = "La respuesta aerobica reciente mejora frente al bloque anterior."
        elif score <= -2:
            state = "retrocediendo"
            direction = "descendente"
            explanation = "Dos o mas senales empeoraron frente a las cuatro semanas anteriores."
        else:
            state = "estable"
            direction = "lateral"
            explanation = "Los cambios recientes son mixtos o pequenos; no hay una direccion fuerte."

        def serialize_week(row):
            if not row:
                return None
            return {
                key: value.isoformat() if hasattr(value, "isoformat") else float(value)
                if hasattr(value, "__float__") and value is not None else value
                for key, value in row.items()
            }

        current_efficiency = (
            float(current_week["efficiency"])
            if current_week and current_week.get("efficiency") is not None else None
        )
        best_efficiency = best_week["efficiency"] if best_week else None
        best_form_pct = (
            current_efficiency / best_efficiency * 100
            if current_efficiency is not None and best_efficiency else None
        )
        return {
            "ok": True,
            "sport": sport,
            "estado": state,
            "direccion": direction,
            "score": score,
            "explicacion": explanation,
            "senales": signals,
            "actividad_reciente": recent_sport_hours > 0,
            "ultimas_4": recent_summary,
            "anteriores_4": previous_summary,
            "cambios": {
                "eficiencia_pct": round(efficiency_delta, 2) if efficiency_delta is not None else None,
                "fc_bpm": round(hr_delta, 1) if hr_delta is not None else None,
                "volumen_horas_pct": round(volume_delta, 2) if volume_delta is not None else None,
            },
            "alerta_carga": {
                "activa": abrupt_load,
                "umbral_pct": 50,
                "contexto": load_context,
                "lectura": (
                    "Regreso tras pausa: el porcentaje se amplifica por el bajo volumen previo."
                    if load_context == "regreso_tras_pausa"
                    else "Pico atipico frente al bloque anterior; no sumar mas carga hasta consolidar."
                    if load_context == "pico_atipico"
                    else "Aumento brusco frente al bloque anterior; no sumar mas carga hasta consolidar."
                    if abrupt_load else "Cambio de carga dentro del rango de observacion."
                ),
            },
            "mejor_version": {
                "eficiencia_historica": round(best_efficiency, 5) if best_efficiency is not None else None,
                "semana": best_week["week_start"].isoformat() if best_week else None,
                "eficiencia_actual": round(current_efficiency, 5) if current_efficiency is not None else None,
                "porcentaje_mejor_forma": round(best_form_pct, 1) if best_form_pct is not None else None,
                "criterio": "percentil 90 de semanas con al menos 2 sesiones y 2 horas comparables",
            },
            "semana_actual": serialize_week(current_week),
            "nota": "Indicador observacional; no sustituye evaluacion medica ni mide por si solo fatiga.",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# NUTRICION — geles y registro pre/post sesion
# ═══════════════════════════════════════════════════════════════════════════════


@app.post("/nutrition")
def add_nutrition(body: NutritionIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_nutrition_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO nutrition_log
                    (date, session_id, moment, gel_type, gel_count, agua_ml, carbos_g, notas, gi_response, energy_response)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.date, body.session_id, body.moment, body.gel_type,
                  body.gel_count, body.agua_ml, body.carbos_g, body.notas,
                  body.gi_response, body.energy_response))
            nid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": nid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))

@app.get("/nutrition/summary")
def get_nutrition_summary(weeks: int = 8):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_nutrition_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT gel_type, COUNT(*) as usos,
                       AVG(gel_count) as avg_gels,
                       AVG(carbos_g) as avg_carbos,
                       COUNT(CASE WHEN gi_response IN ('nausea','inflamacion') THEN 1 END) as gi_issues
                FROM nutrition_log
                WHERE date >= CURRENT_DATE - (%s || ' weeks')::interval::interval
                  AND gel_type IS NOT NULL
                GROUP BY gel_type ORDER BY usos DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        gels = [dict(zip(cols, r)) for r in rows]
        for g in gels:
            for k, v in g.items():
                if hasattr(v, '__float__') and v is not None:
                    try: g[k] = float(v)
                    except: pass
        return {"semanas": weeks, "por_tipo": gels}
    except Exception as e:
        raise HTTPException(500, str(e))




# ═══════════════════════════════════════════════════════════════════════════════
# DELETE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM session_records WHERE session_id=%s", (session_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.delete("/fuerza/{record_id}")
def delete_fuerza(record_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fuerza WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.delete("/wellness/{record_id}")
def delete_wellness_record(record_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wellness WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.delete("/nutrition/{record_id}")
def delete_nutrition_record(record_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM nutrition_log WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.delete("/gear/service/{record_id}")
def delete_gear_service(record_id: int):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gear_service WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))

@app.delete("/gear/{gear_id}")
def delete_gear_component(gear_id: str):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gear WHERE gear_id=%s", (gear_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))




# ═══════════════════════════════════════════════════════════════════════════════
# PERFIL MARS COMPLETO
# ═══════════════════════════════════════════════════════════════════════════════




@app.post("/admin/import-mars-profile")
def import_mars_profile(token: str = None):
    if token != os.environ.get("ADMIN_TOKEN"): raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn: raise HTTPException(503,"DB no disponible")
    _ensure_profile_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO athlete_profile_full (profile_key,data) VALUES ('mars',%s::jsonb)
                ON CONFLICT (profile_key) DO UPDATE SET data=%s::jsonb,updated_at=NOW()""",
                (MARS_PROFILE_DEFAULT,MARS_PROFILE_DEFAULT))
        conn.commit()
        return {"ok":True,"msg":"Perfil Mars importado"}
    except Exception as e:
        conn.rollback(); raise HTTPException(500,str(e))

@app.get("/gpt/mars-context")
def gpt_mars_context():
    conn = get_db()
    if not conn: raise HTTPException(503,"DB no disponible")
    try:
        p = _get_profile(conn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT weight_kg FROM weight_log ORDER BY date DESC LIMIT 1")
                row = cur.fetchone()
            if row: p["athlete"]["peso_actual_kg"] = float(row[0])
        except: pass
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT ROUND(AVG(avg_speed_kmh/avg_hr_bpm)::numeric,4)
                    FROM sessions_clean_compat WHERE sport='cycling' AND avg_hr_bpm>0 AND avg_speed_kmh>0
                    AND start_time::timestamp>=NOW()-'4 weeks'::interval""")
                row = cur.fetchone()
            if row and row[0]: p["eff_actual"] = float(row[0])
        except: pass
        z = p.get("zonas_ciclismo",{})
        z2 = z.get("z2",[134,150])
        a = p.get("athlete",{})
        plan = p.get("plan_garmin",{})
        p["context_msg"] = (f"Plan {plan.get('nombre','Garmin Coach')} · fase {plan.get('fase','Base')} · "
            f"Z2 ciclismo: {z2[0]}-{z2[1]} bpm · Cadencia obj: {p.get('cadencia_obj',100)} rpm · "
            f"Peso: {a.get('peso_actual_kg',89.1)} kg · objetivo {a.get('peso_objetivo_kg',80)} kg")
        return p
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/gpt/session-analysis/{session_id}")
def analyze_session_vs_profile(session_id: str):
    conn = get_db()
    if not conn: raise HTTPException(503,"DB no disponible")
    try:
        p = _get_profile(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT session_id,start_time,distance_km,duration_s,
                avg_hr_bpm,avg_speed_kmh,avg_cadence,ascent_m,workout_name
                FROM sessions_clean_compat WHERE session_id=%s""",(session_id,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        if not row: raise HTTPException(404,"Sesion no encontrada")
        s = dict(zip(cols,row))
        for k,v in s.items():
            if hasattr(v,'isoformat'): s[k]=v.isoformat()
            elif hasattr(v,'__float__') and v is not None:
                try: s[k]=float(v)
                except: pass
        with conn.cursor() as cur:
            cur.execute("""SELECT ROUND(AVG(avg_hr_bpm)::numeric,1),ROUND(AVG(avg_speed_kmh)::numeric,2),
                ROUND(AVG(avg_cadence)::numeric,1),ROUND(AVG(distance_km)::numeric,1),COUNT(*)
                FROM sessions_clean_compat WHERE sport='cycling'
                AND start_time::timestamp>=NOW()-'90 days'::interval AND session_id!=%s""",(session_id,))
            hrow = cur.fetchone()
        hist = {"avg_hr":float(hrow[0]) if hrow[0] else None,"avg_spd":float(hrow[1]) if hrow[1] else None,
                "avg_cad":float(hrow[2]) if hrow[2] else None,"avg_dist":float(hrow[3]) if hrow[3] else None,"n":hrow[4]}
        z = p.get("zonas_ciclismo",{})
        hr = s.get("avg_hr_bpm") or 0
        spd = s.get("avg_speed_kmh") or 0
        cadval = s.get("avg_cadence") or 0
        z2lo,z2hi = z.get("z2",[134,150])
        z3hi = z.get("z3",[151,160])[1]
        trans = z.get("transition",[109,133])
        if hr < trans[0]: zone_eval="Z1 recuperacion — muy facil"
        elif hr <= trans[1]: zone_eval="Transicion — calentamiento/enfriamiento"
        elif hr < z2lo: zone_eval="Z1 muy facil — sube intensidad"
        elif hr <= z2hi: zone_eval="Z2 perfecto — motor aerobico"
        elif hr <= z3hi: zone_eval="Z3 tempo — buen umbral"
        else: zone_eval="Z4/Z5 — alta intensidad, recupera"
        cad_obj = p.get("cadencia_obj",100)
        cad_eval = f"Cadencia {cadval:.0f} rpm — {'+' if cadval>cad_obj else ''}{cadval-cad_obj:.0f} vs obj {cad_obj}"
        eff = round(spd/hr,4) if hr>0 and spd>0 else None
        eff_base = p.get("eff_base",0.1483)
        eff_eval = f"Eficiencia {eff:.4f} ({'+' if eff and eff>eff_base else ''}{(eff-eff_base):.4f} vs base)" if eff else "Sin datos eficiencia"
        comp = []
        if hist["avg_hr"] and hr: comp.append(f"FC {'+' if hr-hist['avg_hr']>0 else ''}{hr-hist['avg_hr']:.1f} bpm vs hist")
        if hist["avg_spd"] and spd: comp.append(f"vel {'+' if spd-hist['avg_spd']>0 else ''}{spd-hist['avg_spd']:.1f} km/h vs hist")
        if hist["avg_cad"] and cadval: comp.append(f"cad {'+' if cadval-hist['avg_cad']>0 else ''}{cadval-hist['avg_cad']:.1f} rpm vs hist")
        return {"session":s,"analysis":{"zone":zone_eval,"cadence":cad_eval,"efficiency":eff_eval,
                "comparison":" · ".join(comp) or "Sin historial suficiente"},"historical":hist}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))




@app.get("/api/fuerza-records")
def api_fuerza_records(limit: int = 10):
    conn = get_db()
    if not conn: return {"records": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, date::text, category, subcategory, 
                muscle_group, intensity, duration_min, notes
                FROM fuerza ORDER BY date DESC, id DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"records": [dict(zip(cols, r)) for r in rows]}
    except: return {"records": []}

@app.get("/api/wellness-records")
def api_wellness_records(limit: int = 10):
    conn = get_db()
    if not conn: return {"records": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, date::text, category, sleep_hours, 
                fatigue, pain_zone, pain_level, notes
                FROM wellness ORDER BY date DESC, id DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"records": [dict(zip(cols, r)) for r in rows]}
    except: return {"records": []}




# ═══════════════════════════════════════════════════════════════════════════════
# MATCHED RIDES — detecta rutas similares y muestra progresión histórica
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/gpt/matched-rides/{session_id}")
def get_matched_rides(session_id: str, min_distance_km: float = 15.0):
    """Encuentra sesiones históricas con distancia ±15% y ascenso ±20%."""
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        # Get reference session
        with conn.cursor() as cur:
            cur.execute("""
                SELECT distance_km, ascent_m, avg_hr_bpm, avg_speed_kmh,
                       avg_cadence, duration_s, start_time::text, workout_name
                FROM sessions_clean_compat WHERE session_id=%s
            """, (session_id,))
            ref = cur.fetchone()
        if not ref:
            raise HTTPException(404, "Sesion no encontrada")

        dist, asc, hr_ref, spd_ref, cad_ref, dur_ref, dt_ref, name_ref = ref
        dist = float(dist or 0)
        asc = float(asc or 0)

        if dist < min_distance_km:
            return {
                "matched": [],
                "route_name": "Sesion corta filtrada",
                "route_label": f"Ruta corta ~{round(dist)} km",
                "count": 0,
                "status": "filtered_short_route",
                "message": f"Para bici se priorizan comparativas de {min_distance_km:g} km o mas. Puedes bajar min_distance_km si quieres revisar esta sesion."
            }

        # Find matches: distance ±15%, ascent ±25%
        dist_lo, dist_hi = dist * 0.85, dist * 1.15
        asc_lo = max(0, asc * 0.75)
        asc_hi = asc * 1.25 + 50  # +50m buffer

        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, start_time::text as dt,
                       distance_km::float, ascent_m::float,
                       avg_hr_bpm::float, avg_speed_kmh::float,
                       avg_cadence::float, duration_s::int,
                       workout_name
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND distance_km BETWEEN %s AND %s
                  AND (ascent_m IS NULL OR ascent_m BETWEEN %s AND %s)
                  AND session_id != %s
                ORDER BY start_time DESC
                LIMIT 20
            """, (dist_lo, dist_hi, asc_lo, asc_hi, session_id))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        matches = []
        for row in rows:
            m = dict(zip(cols, row))
            # Calculate efficiency ratio
            if m.get('avg_hr_bpm') and m.get('avg_speed_kmh') and m['avg_hr_bpm'] > 0:
                m['efficiency'] = round(float(m['avg_speed_kmh']) / float(m['avg_hr_bpm']), 4)
            else:
                m['efficiency'] = None
            dur = int(m.get("duration_s") or 0)
            m["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            matches.append(m)

        # Ref session efficiency
        ref_eff = round(float(spd_ref) / float(hr_ref), 4) if hr_ref and spd_ref and float(hr_ref) > 0 else None

        # Determine route name from most common workout_name or distance
        route_label = f"~{round(dist)} km"
        if dist < 25: route_label = f"Ruta corta ~{round(dist)} km"
        elif dist < 40: route_label = f"Ruta media ~{round(dist)} km"
        else: route_label = f"Ruta larga ~{round(dist)} km"

        # Stats vs historical
        if matches:
            avg_spd_hist = sum(float(m['avg_speed_kmh'] or 0) for m in matches) / len(matches)
            avg_hr_hist = sum(float(m['avg_hr_bpm'] or 0) for m in matches if m['avg_hr_bpm']) / max(1, len([m for m in matches if m['avg_hr_bpm']]))
            avg_eff_hist = sum(m['efficiency'] for m in matches if m['efficiency']) / max(1, len([m for m in matches if m['efficiency']]))
            spd_delta = round(float(spd_ref or 0) - avg_spd_hist, 2) if spd_ref else None
            eff_delta = round(ref_eff - avg_eff_hist, 4) if ref_eff and avg_eff_hist else None
            verdict = "mejoraste" if (spd_delta and spd_delta > 0.3) else "similar" if (spd_delta and abs(spd_delta) <= 0.3) else "bajaste"
        else:
            avg_spd_hist = avg_hr_hist = avg_eff_hist = spd_delta = eff_delta = None
            verdict = "primera vez en esta ruta"

        return {
            "session_id": session_id,
            "reference": {
                "date": dt_ref, "distance_km": dist, "ascent_m": asc,
                "avg_speed_kmh": float(spd_ref or 0), "avg_hr_bpm": float(hr_ref or 0),
                "avg_cadence": float(cad_ref or 0), "efficiency": ref_eff,
                "workout_name": name_ref
            },
            "route_label": route_label,
            "matched": matches,
            "count": len(matches),
            "vs_historical": {
                "avg_speed_hist": round(avg_spd_hist, 2) if avg_spd_hist else None,
                "avg_hr_hist": round(avg_hr_hist, 1) if avg_hr_hist else None,
                "avg_eff_hist": round(avg_eff_hist, 4) if avg_eff_hist else None,
                "speed_delta": spd_delta,
                "efficiency_delta": eff_delta,
                "verdict": verdict
            }
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/gpt/route-history")
def get_route_history(min_km: float = 5, weeks: int = 26):
    """Lista rutas agrupadas por distancia con progresión histórica."""
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(distance_km::numeric) as dist_bucket,
                    COUNT(*) as n,
                    MIN(start_time::text) as first_date,
                    MAX(start_time::text) as last_date,
                    ROUND(AVG(avg_speed_kmh)::numeric, 2) as avg_speed,
                    ROUND(MAX(avg_speed_kmh)::numeric, 2) as best_speed,
                    ROUND(AVG(avg_hr_bpm)::numeric, 1) as avg_hr,
                    ROUND(MIN(avg_hr_bpm)::numeric, 1) as best_hr,
                    ROUND(AVG(avg_cadence)::numeric, 1) as avg_cad,
                    ROUND(AVG(ascent_m)::numeric, 0) as avg_asc
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND distance_km >= %s
                  AND start_time::timestamp >= NOW() - (%s || ' weeks')::interval
                GROUP BY dist_bucket
                HAVING COUNT(*) >= 2
                ORDER BY n DESC, dist_bucket
            """, (min_km, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        routes = []
        for row in rows:
            d = dict(zip(cols, row))
            for k, v in d.items():
                if hasattr(v, '__float__') and v is not None:
                    try: d[k] = float(v)
                    except: pass
            km = d.get('dist_bucket', 0)
            if km < 25: d['label'] = f"Ruta corta ~{int(km)} km"
            elif km < 40: d['label'] = f"Ruta media ~{int(km)} km"
            else: d['label'] = f"Ruta larga ~{int(km)} km"
            routes.append(d)

        return {"routes": routes, "total": len(routes)}
    except Exception as e:
        raise HTTPException(500, str(e))


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
