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

from fastapi import FastAPI, UploadFile, File, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from typing import Optional, List
from pydantic import BaseModel, Field
import tempfile, os, zipfile, math, statistics, uuid, json
from datetime import datetime, timezone
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

from db import DATABASE_URL, db_conn, get_db, _init_db, _ensure_gear_service_table, _ensure_nutrition_table, _ensure_weight_table, _ensure_wellness_table, _ensure_fuerza_table, _ensure_accidents_table, _ensure_garmin_staging_tables, _ensure_clean_sessions_table, _ensure_clean_sessions_compat_view, RESULTS_STORE_MAX
from mars_context import MARS_PROFILE_DEFAULT, MARS_ZONES as MARS_ZONES_PROFILE, _ensure_profile_table, _get_profile, get_zone_label, analyze_session_quick

SESSION_READ_TABLE = "sessions_clean_compat"

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
  const meta=[{key:'Fecha',val:(s.start_time||'').slice(0,10)},{key:'Distancia',val:s.distance_km?s.distance_km+' km':'—'},{key:'Duración',val:s.duration_hms||'—'},{key:'FC prom.',val:s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—'}];
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


@app.get("/route/{route_id}/matched")
def get_route_matched(route_id: str, limit: int = 40):
    """Matched rides by route_id, with duplicate rows hidden for comparison only."""
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
                raise HTTPException(404, "Ruta no encontrada")

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


@app.get("/sessions")
def list_sessions(
    sport: Optional[str] = None,
    month: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "recent"
):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence, workout_name, route_id
            FROM sessions_clean_compat
            WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        if month:
            query += " AND LEFT(start_time, 7) = %s"
            params.append(month)
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
            for k, v in s.items():
                if hasattr(v, "isoformat"):
                    s[k] = v.isoformat()
            sessions_list.append(s)

        # Total count
        count_query = "SELECT COUNT(*) FROM sessions_clean_compat WHERE start_time IS NOT NULL"
        count_params = []
        if sport:
            count_query += " AND sport = %s"
            count_params.append(sport)
        if month:
            count_query += " AND LEFT(start_time, 7) = %s"
            count_params.append(month)
        with conn.cursor() as cur:
            cur.execute(count_query, count_params)
            total = cur.fetchone()[0]

        return {"sessions": sessions_list, "total": total, "limit": limit, "offset": offset}
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
                    cur.execute("SELECT result_json FROM sessions_clean_compat WHERE session_id=%s",(session_id,))
                    row = cur.fetchone()
                    if row:
                        result = json.loads(row[0])
                        entry = {"result": result}
            except Exception as _e: logger.error(f'Silent error: {_e}')
    if not entry:
        raise HTTPException(404, f"session_id '{session_id}' no encontrado.")

    result  = entry["result"]
    session = result["session"]
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
                        FROM session_records
                        WHERE session_id=%s
                        ORDER BY t ASC
                    """, (session_id,))
                    db_records = cur.fetchall()
                if db_records:
                    records = [
                        {"heart_rate_bpm": r[1], "speed_kmh": r[2],
                         "cadence_rpm": r[3], "altitude_m": r[4],
                         "lat": r[5], "lon": r[6], "_t": r[0]}
                        for r in db_records
                    ]
                    times = [r[0] for r in db_records]
                    hr = [r[1] for r in db_records]
                    cad = [r[3] for r in db_records]
                    spd = [r[2] for r in db_records]
                    alt = [r[4] for r in db_records]
                    temp = [None] * len(db_records)
            except Exception as e:
                logger.info(f"session_records load error: {e}")
    if not records:
        return HTMLResponse("<h2 style='color:white;background:#111;padding:20px;font-family:sans-serif'>Gráficas no disponibles.<br><small style='opacity:.6;font-size:14px'>Vuelve a subir el archivo .fit para ver las gráficas.</small></h2>")

    # Si los records ya tienen _t (vienen de session_records), usar esos arrays directamente
    from_db = records and "_t" in records[0]
    if not from_db:
        times, hr, cad, spd, alt, temp = [], [], [], [], [], []
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
    # else: times, hr, cad, spd, alt, temp already set from session_records

    data_js = f"""
const times={json.dumps(times)};
const hrData={json.dumps(hr)};
const cadData={json.dumps(cad)};
const spdData={json.dumps(spd)};
const altData={json.dumps(alt)};
const tempData={json.dumps(temp)};
const zonesData={json.dumps(result.get('zones',[]))};
const sessionInfo={json.dumps(session)};
const insights={json.dumps(insights)};
"""

    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Charts {session.get('start_time','')[:10]}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap');
  :root{{--bg:#0f0f0f;--s:#1a1a1a;--b:#2a2a2a;--a:#e8593c;--a2:#f2a623;--t:#e8e6e0;--m:#6b6b6b;--g:#3dd68c}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--t);font-family:'DM Sans',sans-serif;padding:16px}}
  .ttl{{font-size:17px;font-weight:500;margin-bottom:4px}}
  .sub{{font-family:'DM Mono',monospace;font-size:10px;color:var(--m);margin-bottom:16px}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}}
  .st{{background:var(--s);border-radius:10px;padding:10px;border:1px solid var(--b)}}
  .sk{{font-family:'DM Mono',monospace;font-size:9px;color:var(--m);text-transform:uppercase;margin-bottom:3px}}
  .sv{{font-size:15px;font-weight:500}}
  .cw{{background:var(--s);border-radius:12px;padding:14px;margin-bottom:10px;border:1px solid var(--b)}}
  .ct{{font-family:'DM Mono',monospace;font-size:9px;color:var(--m);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}}
  canvas{{max-height:130px}}
  .insight{{margin-top:8px;padding:10px 12px;background:#111;border-radius:8px;border-left:3px solid var(--a2);font-size:12px;color:var(--m);line-height:1.5}}
  .zrow{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:12px}}
  .zi{{background:var(--s);border-radius:8px;padding:8px 10px;text-align:center;border:1px solid var(--b)}}
  .zn{{font-size:10px;color:var(--m);margin-bottom:3px}}
  .zm{{font-size:13px;font-weight:500}}
  .zb{{height:3px;border-radius:2px;margin-top:5px}}
</style></head><body>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
  <button onclick="if(history.length>1){{history.back()}}else{{location.href='/home'}}" style="background:#1a1a1a;border:1px solid #2a2a2a;color:#e8e6e0;padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;font-family:inherit">← Volver</button>
  <a href="/home" style="color:#6b6b6b;font-size:12px;text-decoration:none">🏠 Home</a>
  <a href="/activities" style="color:#6b6b6b;font-size:12px;text-decoration:none">📋 Actividades</a>
</div>
<div class="ttl" id="ttl">—</div>
<div class="sub">session_id: {session_id}</div>
<div class="stats">
  <div class="st"><div class="sk">Distancia</div><div class="sv" id="sd">—</div></div>
  <div class="st"><div class="sk">Duración</div><div class="sv" id="sdur">—</div></div>
  <div class="st"><div class="sk">FC prom.</div><div class="sv" id="shr">—</div></div>
  <div class="st"><div class="sk">Vel prom.</div><div class="sv" id="sspd">—</div></div>
</div>
<div class="cw"><div class="ct">Frecuencia cardíaca (bpm)</div><canvas id="cHR"></canvas></div>
<div class="cw"><div class="ct">Velocidad (km/h)</div><canvas id="cSpd"></canvas></div>
<div class="cw"><div class="ct">Cadencia (rpm)</div><canvas id="cCad"></canvas></div>
<div class="cw"><div class="ct">Altimetría (m)</div><canvas id="cAlt"></canvas></div>
<div class="cw"><div class="ct">Temperatura (°C)</div><canvas id="cTmp"></canvas></div>
<div class="cw">
  <div class="ct">Zonas Mars</div>
  <div class="zrow" id="zrow"></div>
</div>
<div class="cw">
  <div class="ct">Insights automáticos</div>
  <div id="insightsBox"></div>
</div>
<div class="cw">
  <div class="ct">Análisis Perfil Mars</div>
  <div id="marsAnalysis" style="color:#8b929f;font-size:13px">Cargando análisis...</div>
</div>
<script>
{data_js}
document.getElementById('ttl').textContent=(sessionInfo.workout_name||sessionInfo.start_time||'').slice(0,40)||'Sesión';
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
function ds(a,n){{if(a.length<=n)return a;const s=a.length/n;return Array.from({{length:n}},(_,i)=>a[Math.floor(i*s)]);}}
const N=400;
const lbl=ds(times,N).map(s=>{{const m=Math.floor(s/60),sc=s%60;return m+':'+String(sc).padStart(2,'0');}});
function mk(id,data,color,mn,mx,unit){{
  new Chart(document.getElementById(id),{{type:'line',
    data:{{labels:lbl,datasets:[{{data:ds(data,N),borderColor:color,borderWidth:1.5,pointRadius:0,fill:true,backgroundColor:color+'22',tension:0.2}}]}},
    options:{{responsive:true,maintainAspectRatio:true,
      interaction:{{mode:'index',intersect:false}},
      plugins:{{
        legend:{{display:false}},
        tooltip:{{
          backgroundColor:'#1a1a1a',borderColor:'#2a2a2a',borderWidth:1,
          titleColor:'#6b6b6b',bodyColor:'#e8e6e0',
          callbacks:{{label:ctx=>ctx.parsed.y!=null?(Math.round(ctx.parsed.y*10)/10)+(unit||''):''}}
        }}
      }},
      scales:{{x:{{ticks:{{color:'#6b6b6b',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'#1a1a1a'}}}},
               y:{{min:mn,max:mx,ticks:{{color:'#6b6b6b',font:{{size:9}}}},grid:{{color:'#2a2a2a'}}}}}}}}}});}}
const hrc=ds(hrData,N).map(v=>{{
  if(!v)return '#6b6b6b';
  if(v<=108)return '#4a9eff';if(v<=133)return '#888';if(v<=150)return '#3dd68c';
  if(v<=160)return '#f2a623';if(v<=168)return '#e8593c';return '#ff3b3b';
}});
new Chart(document.getElementById('cHR'),{{type:'bar',
  data:{{labels:lbl,datasets:[{{data:ds(hrData,N),backgroundColor:hrc,borderWidth:0}}]}},
  options:{{responsive:true,maintainAspectRatio:true,
    interaction:{{mode:'index',intersect:false}},
    plugins:{{
      legend:{{display:false}},
      tooltip:{{
        backgroundColor:'#1a1a1a',borderColor:'#2a2a2a',borderWidth:1,
        titleColor:'#6b6b6b',bodyColor:'#e8e6e0',
        callbacks:{{label:ctx=>ctx.parsed.y!=null?Math.round(ctx.parsed.y)+' bpm':''}}
      }}
    }},
    scales:{{x:{{ticks:{{color:'#6b6b6b',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'#1a1a1a'}}}},
             y:{{min:60,ticks:{{color:'#6b6b6b',font:{{size:9}}}},grid:{{color:'#2a2a2a'}}}}}}}}}});
mk('cSpd',spdData,'#4a9eff',0,null,' km/h');
mk('cCad',cadData,'#f2a623',0,null,' rpm');
mk('cAlt',altData,'#3dd68c',null,null,' m');
mk('cTmp',tempData,'#888',null,null,' °C');
const zc=['#4a9eff','#888','#3dd68c','#f2a623','#e8593c','#ff3b3b'];
const zrow=document.getElementById('zrow');
zonesData.forEach((z,i)=>{{
  zrow.innerHTML+=`<div class="zi"><div class="zn">${{z.name}}</div><div class="zm">${{z.minutes}}m</div><div class="zb" style="background:${{zc[i]||'#888'}};width:${{Math.max(z.percent,3)}}%"></div></div>`;
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
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
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
            cur.execute("""
                SELECT session_id, start_time, sport, distance_km, duration_s,
                       avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                       workout_name, route_id, result_json
                FROM sessions_clean_compat WHERE session_id = %s
            """, [session_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"session_id '{session_id}' no encontrado")
            cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"

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
                JOIN sessions_clean_compat s ON sr.session_id = s.session_id
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
        result["semana_actual"] = {
            "sesiones": int(wk[0] or 0),
            "km": float(wk[1] or 0),
            "horas": float(wk[2] or 0),
            "ascenso_m": int(wk[3] or 0)
        }

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
            d["km_remaining"] = max(0, km_limit - km_used) if km_limit else None
            d["pct_used"] = pct
            d["status"] = status
            d["status_emoji"] = "🔴" if status=="red" else ("🟡" if status=="yellow" else "🟢")
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

        return {
            "total_km_bici": round(total_km_db),
            "componentes": components,
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
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
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
    """Genera snapshot semanal del atleta cada domingo."""
    try:
        week_start = (datetime.now(timezone.utc).date() - __import__('datetime').timedelta(days=7)).strftime('%Y-%m-%d')
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(SUM(distance_km)::numeric, 2) as km,
                    ROUND(SUM(duration_s)/3600.0::numeric, 2) as hours,
                    COUNT(*) as sessions,
                    ROUND(AVG(avg_hr_bpm)::numeric, 1) as avg_hr,
                    ROUND(AVG(avg_speed_kmh)::numeric, 2) as avg_speed,
                    ROUND(AVG(avg_cadence)::numeric, 1) as avg_cad,
                    ROUND((AVG(avg_speed_kmh)/NULLIF(AVG(avg_hr_bpm),0))::numeric, 5) as efficiency
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND start_time::timestamp >= NOW() - INTERVAL '7 days'
                  AND start_time IS NOT NULL
            """)
            row = cur.fetchone()
            if not row or not row[0]:
                return
            km, hours, sessions, avg_hr, avg_speed, avg_cad, eff = row
            # Get latest weight from post_session
            cur.execute("""
                SELECT weight_before FROM post_session
                WHERE weight_before IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
            """)
            wrow = cur.fetchone()
            weight = float(wrow[0]) if wrow else None
            # Get Z2 pct
            cur.execute("""
                SELECT ROUND(AVG(CASE WHEN avg_hr_bpm BETWEEN 134 AND 150 THEN 1.0 ELSE 0.0 END)*100::numeric, 1)
                FROM sessions_clean_compat
                WHERE sport='cycling' AND start_time::timestamp >= NOW() - INTERVAL '7 days'
            """)
            z2row = cur.fetchone()
            pct_z2 = float(z2row[0]) if z2row and z2row[0] else None
            fitness_score = round(float(km or 0) * 0.5 + float(hours or 0) * 2 + (float(pct_z2 or 0) * 0.1), 1)
            cur.execute("""
                INSERT INTO athlete_snapshots
                    (week_start, weight_kg, km_week, hours_week, sessions, avg_hr,
                     avg_speed, avg_cadence, pct_z2, efficiency, fitness_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (week_start) DO UPDATE SET
                    km_week=EXCLUDED.km_week, hours_week=EXCLUDED.hours_week,
                    fitness_score=EXCLUDED.fitness_score
            """, (week_start, weight, km, hours, sessions, avg_hr,
                  avg_speed, avg_cad, pct_z2, eff, fitness_score))
            logger.info(f"Weekly snapshot saved: {week_start} km={km}")
    except Exception as e:
        logger.error(f"Weekly snapshot error: {e}")


@app.get("/admin/generate-snapshot")
def admin_generate_snapshot(token: str = None):
    """Genera manualmente el snapshot semanal del atleta."""
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
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


@app.get("/admin/diagnostics")
def admin_diagnostics(token: str = None):
    """Diagnóstico rápido de la API y conteos de todas las tablas."""
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
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
                  "garmin_export_sleep", "clean_sessions"]
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
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
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
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
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
    if token != os.environ.get("ADMIN_TOKEN","mars2026"):
        raise HTTPException(401,"Token requerido")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_clean_sessions_table(conn)
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


@app.get("/admin/backup")
def admin_backup(token: str = ""):
    """Exporta todas las tablas en JSON. Requiere token de entorno ADMIN_TOKEN."""
    import os
    admin_token = os.environ.get("ADMIN_TOKEN", "mars2026")
    if token != admin_token:
        raise HTTPException(403, "Token inválido. Usa ?token=TU_TOKEN")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    tables = ["sessions", "routes", "post_session", "gear", "maintenance",
              "recovery", "fuerza", "wellness", "accidents",
              "athlete_profile", "athlete_tests", "achievements", "athlete_snapshots",
              "garmin_export_activities", "garmin_export_gear", "garmin_export_sleep",
              "clean_sessions"]
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
def session_detail(session_id: str):
    sid = json.dumps(session_id)
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
    pre{{white-space:pre-wrap;word-break:break-word;color:#cfd3dc}}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <a href="/home">← Volver</a>
      <a id="charts" href="#">Graficas</a>
    </div>
    <section class="card">
      <div class="label">Sesion</div>
      <h1 id="title">Cargando...</h1>
      <div class="grid" id="metrics"></div>
    </section>
    <section class="card">
      <div class="label">Detalle</div>
      <pre id="raw"></pre>
    </section>
  </main>
  <script>
    const sid = {sid};
    document.getElementById('charts').href = '/charts/' + sid;
    const hms = s => {{
      s = Number(s || 0);
      return String(Math.floor(s/3600)).padStart(2,'0') + 'h ' + String(Math.floor((s%3600)/60)).padStart(2,'0') + 'm';
    }};
    fetch('/result/' + sid).then(async r => {{
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }}).then(data => {{
      const s = data.session || {{}};
      document.getElementById('title').textContent = s.workout_name || s.sport || sid;
      document.getElementById('metrics').innerHTML = [
        ['Distancia', (s.distance_km ?? '--') + ' km'],
        ['Duracion', s.duration_hms || hms(s.duration_s)],
        ['FC prom', (s.avg_hr_bpm ?? '--') + ' bpm'],
        ['Vel prom', (s.avg_speed_kmh ?? '--') + ' km/h'],
        ['Ascenso', '+' + (s.ascent_m ?? '--') + ' m'],
        ['Cadencia', (s.avg_cadence_rpm ?? s.avg_cadence ?? '--') + ' rpm']
      ].map(([k,v]) => `<div class="metric"><div class="label">${{k}}</div><div class="value">${{v}}</div></div>`).join('');
      document.getElementById('raw').textContent = JSON.stringify(data, null, 2);
    }}).catch(err => {{
      document.getElementById('title').textContent = 'No se pudo cargar';
      document.getElementById('raw').textContent = String(err.message || err);
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
# CORRELACIONES FC vs PESO vs TEMPERATURA
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/gpt/correlations")
def gpt_correlations(weeks: int = 12):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            # Weekly FC + speed + cadence
            cur.execute("""
                SELECT
                    DATE_TRUNC('week', start_time::timestamp)::date as week,
                    ROUND(AVG(avg_hr_bpm)::numeric,1)        as avg_hr,
                    ROUND(AVG(avg_speed_kmh)::numeric,2)     as avg_spd,
                    ROUND(AVG(avg_cadence)::numeric,1)       as avg_cad,
                    COUNT(*)                                  as sessions,
                    ROUND(SUM(distance_km)::numeric,1)       as km
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND start_time::timestamp >= NOW() - (%s || ' weeks')::interval
                  AND avg_hr_bpm IS NOT NULL
                GROUP BY week ORDER BY week
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            weekly = []
            for r in rows:
                d = dict(zip(cols, r))
                for k, v in d.items():
                    if hasattr(v, 'isoformat'): d[k] = v.isoformat()
                    if hasattr(v, '__float__') and v is not None:
                        try: d[k] = float(v)
                        except: pass
                weekly.append(d)

        # Weight data
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date::text, weight_kg::float
                    FROM weight_log
                    WHERE date >= CURRENT_DATE - (%s || ' weeks')::interval::interval
                    ORDER BY date
                """, (weeks,))
                wrows = cur.fetchall()
            weight = [{"date": r[0], "kg": float(r[1])} for r in wrows if r[1]]
        except:
            weight = []

        # Efficiency ratio per week
        for w in weekly:
            if w.get('avg_hr') and w.get('avg_spd') and w['avg_hr'] > 0:
                w['efficiency'] = round(w['avg_spd'] / w['avg_hr'], 4)
            else:
                w['efficiency'] = None

        # Correlation coefficient FC vs efficiency
        hr_vals = [w['avg_hr'] for w in weekly if w.get('avg_hr') and w.get('efficiency')]
        eff_vals = [w['efficiency'] for w in weekly if w.get('avg_hr') and w.get('efficiency')]
        corr = None
        if len(hr_vals) >= 3:
            n = len(hr_vals)
            mx = sum(hr_vals)/n; my = sum(eff_vals)/n
            num = sum((x-mx)*(y-my) for x,y in zip(hr_vals,eff_vals))
            den = (sum((x-mx)**2 for x in hr_vals) * sum((y-my)**2 for y in eff_vals)) ** 0.5
            if den > 0: corr = round(num/den, 3)

        return {
            "semanas": len(weekly),
            "weekly": weekly,
            "weight": weight,
            "correlation_hr_efficiency": corr,
            "interpretation": (
                "FC y eficiencia correlacionan negativamente — bajar FC mejora rendimiento" if corr and corr < -0.3
                else "FC y eficiencia correlacionan positivamente" if corr and corr > 0.3
                else "Sin correlacion clara aun — necesitas mas semanas de datos"
            )
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
    if token != os.environ.get("ADMIN_TOKEN","mars2026"): raise HTTPException(401,"Token requerido")
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
def get_matched_rides(session_id: str):
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

        if dist < 2:
            return {"matched": [], "route_name": "Sesion muy corta", "count": 0}

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


APP_FULL_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora Mars</title>
<style>
:root{
  --bg:#08090b;--bg2:#0f1115;--card:#151820;--card2:#1d222c;--line:rgba(255,255,255,.09);
  --text:#f7f7f4;--muted:#8e95a3;--muted2:#59616f;
  --home:#ffffff;--bike:#e8593c;--fuerza:#c8f135;--well:#4a9eff;--stats:#a78bfa;--gear:#f59e0b;--cal:#22d3ee;
  --theme:var(--home);--shadow:0 18px 50px rgba(0,0,0,.42);--glow:0 0 32px color-mix(in srgb,var(--theme) 28%,transparent);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}html,body{height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",sans-serif;overflow:hidden}button,input,select,textarea{font-family:inherit}.app{height:100dvh;display:flex;flex-direction:column;background:radial-gradient(900px 430px at 50% -180px,color-mix(in srgb,var(--theme) 18%,transparent),transparent 62%),linear-gradient(180deg,#0c0e12,#07080a);transition:background .28s ease}.top{height:62px;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:calc(env(safe-area-inset-top) + 10px) 16px 8px}.brand{display:flex;gap:10px;align-items:center;flex:1;min-width:0}.logo{width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,var(--theme),color-mix(in srgb,var(--theme) 52%,#000));box-shadow:var(--glow);color:#08090b;font-size:19px;font-weight:950;display:flex;align-items:center;justify-content:center;flex-shrink:0}.brand small{display:block;font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.03em}.brand strong{display:block;font-size:16px;letter-spacing:-.03em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.icon{width:38px;height:38px;border-radius:14px;border:1px solid var(--line);background:rgba(255,255,255,.045);color:var(--text);font-size:18px;flex-shrink:0}.back-icon{visibility:hidden}.content{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding:10px 14px 94px}.screen{display:none;animation:fade .22s ease}.screen.active{display:block}@keyframes fade{from{opacity:.35;transform:translateY(8px)}to{opacity:1;transform:none}}
.hero{border-radius:28px;padding:20px 20px 18px;margin-bottom:14px;position:relative;overflow:hidden;background:linear-gradient(135deg,color-mix(in srgb,var(--theme) 24%,#15171c),#14161b 55%,#0d0f13);border:1px solid color-mix(in srgb,var(--theme) 22%,var(--line));box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-42px;top:-45px;width:142px;height:142px;border-radius:50%;background:color-mix(in srgb,var(--theme) 18%,transparent);filter:blur(8px)}.kicker{font-size:10px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:color-mix(in srgb,var(--theme) 88%,#fff);margin-bottom:8px}.hero h1{font-size:30px;line-height:.98;letter-spacing:-.06em;margin-bottom:8px}.hero p{font-size:13px;line-height:1.45;color:#c8cbd2;max-width:330px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:22px;padding:16px;margin-bottom:12px}.mini{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:17px;padding:14px;min-height:88px}.label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:900;margin-bottom:7px}.value{font-size:28px;font-weight:950;letter-spacing:-.05em;color:var(--theme);line-height:1}.unit{font-size:11px;color:var(--muted);margin-top:3px}.head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.head h3{font-size:14px;letter-spacing:-.02em}.head span{font-size:11px;color:var(--muted)}.row{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:none}.r-ico{width:42px;height:42px;border-radius:15px;background:color-mix(in srgb,var(--theme) 14%,#111);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}.r-main{flex:1;min-width:0}.r-title{font-size:14px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.r-sub{font-size:11px;color:var(--muted);margin-top:3px}.r-val{text-align:right;font-size:14px;font-weight:950;color:var(--theme)}.pillbar{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;padding-bottom:8px;margin-bottom:10px}.pillbar::-webkit-scrollbar{display:none}.pill{padding:8px 14px;border-radius:999px;background:rgba(255,255,255,.045);border:1px solid var(--line);font-size:12px;font-weight:900;color:var(--muted);white-space:nowrap}.pill.on{background:var(--theme);border-color:var(--theme);color:#08090b}.upload{border:1.6px dashed color-mix(in srgb,var(--theme) 44%,var(--line));background:color-mix(in srgb,var(--theme) 7%,var(--card));border-radius:24px;padding:25px 18px;text-align:center;margin-bottom:12px;display:block}.upload input{display:none}.upload .big{font-size:40px;margin-bottom:8px}.upload h3{font-size:17px;margin-bottom:4px}.upload p{font-size:12px;color:var(--muted)}input,select,textarea{width:100%;background:#242832;border:1px solid var(--line);border-radius:14px;padding:12px 13px;color:var(--text);font-size:14px;outline:none}input:focus,select:focus,textarea:focus{border-color:var(--theme);box-shadow:0 0 0 3px color-mix(in srgb,var(--theme) 17%,transparent)}.field{margin-bottom:10px}.field label{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:900;margin:0 0 5px 2px}.btn{width:100%;border:none;border-radius:16px;padding:15px;background:var(--theme);color:#08090b;font-size:15px;font-weight:950}.btn2{background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--text)}.bnav{position:fixed;left:10px;right:10px;bottom:calc(env(safe-area-inset-bottom) + 10px);height:66px;background:rgba(17,19,24,.88);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.14);border-radius:24px;display:flex;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;z-index:80;box-shadow:0 16px 40px rgba(0,0,0,.45)}.nav{flex:0 0 auto;min-width:52px;background:transparent;border:none;color:var(--muted);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:9px;font-weight:900;text-transform:uppercase}.nav svg{opacity:.5;transition:transform .2s,filter .2s,opacity .2s}.nav.active{color:var(--theme)}.nav.active svg{opacity:1;transform:translateY(-2px);filter:drop-shadow(0 0 5px var(--theme))}.tabs{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:0 -2px 12px;padding:0 2px}.tabs::-webkit-scrollbar{display:none}.tab{border:none;background:rgba(255,255,255,.045);border:1px solid var(--line);color:var(--muted);border-radius:999px;padding:9px 14px;font-size:12px;font-weight:900;white-space:nowrap}.tab.on{background:var(--theme);color:#08090b;border-color:var(--theme)}.calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.day{aspect-ratio:1;border-radius:10px;background:#1d222c;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--muted);font-weight:800}.day.hot{background:color-mix(in srgb,var(--theme) 38%,#1d222c);color:#08090b;border-color:var(--theme)}.gearbar{height:7px;border-radius:4px;background:#303642;overflow:hidden;margin-top:8px}.gearfill{height:100%;background:var(--theme);border-radius:4px}.bodymap{height:240px;border-radius:24px;background:radial-gradient(circle at 50% 35%,color-mix(in srgb,var(--theme) 20%,transparent),transparent 35%),linear-gradient(180deg,#11141a,#0c0e12);border:1px solid var(--line);position:relative;margin-bottom:12px;overflow:hidden}.human{position:absolute;left:50%;top:22px;transform:translateX(-50%);width:100px;height:205px}.human div{position:absolute;background:#343943}.human .h{left:36px;top:0;width:28px;height:28px;border-radius:50%}.human .t{left:22px;top:34px;width:56px;height:72px;border-radius:28px 28px 18px 18px;background:linear-gradient(180deg,var(--theme),color-mix(in srgb,var(--theme) 45%,#111))}.human .a{top:46px;width:21px;height:88px;border-radius:15px}.human .al{left:0;transform:rotate(12deg)}.human .ar{right:0;transform:rotate(-12deg)}.human .l{top:112px;width:28px;height:92px;border-radius:18px;background:linear-gradient(180deg,var(--theme),#343943)}.human .ll{left:20px}.human .lr{right:20px}.mq{position:absolute;inset:14px;display:flex;flex-direction:column;justify-content:space-between;font-size:12px;color:#d8dde7}.mq div{display:flex;justify-content:space-between}.mq strong{font-size:19px;color:var(--theme)}.ring{width:94px;height:94px;border-radius:50%;background:conic-gradient(var(--theme) var(--p,75%),rgba(255,255,255,.08) 0);display:flex;align-items:center;justify-content:center;position:relative;margin:auto;box-shadow:var(--glow)}.ring:before{content:"";position:absolute;width:68px;height:68px;border-radius:50%;background:var(--card)}.ring span{position:relative;font-size:25px;font-weight:950;color:var(--theme)}.toast{position:fixed;top:calc(env(safe-area-inset-top) + 14px);left:50%;transform:translateX(-50%);background:var(--theme);color:#08090b;padding:10px 18px;border-radius:999px;z-index:999;font-size:13px;font-weight:950;display:none;white-space:nowrap}.loading{padding:24px;color:var(--muted);font-size:13px;text-align:center}.spin{display:inline-block;width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--theme);border-radius:50%;animation:spin .75s linear infinite;vertical-align:middle;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}
@media(min-width:900px){
  body{background:#050608}
  .app{max-width:430px;margin:0 auto;border-left:1px solid var(--line);border-right:1px solid var(--line);box-shadow:0 0 80px rgba(0,0,0,.55)}
  .top{width:100%;padding-left:16px;padding-right:16px}
  .content{width:100%;padding-left:14px;padding-right:14px;padding-bottom:94px;display:block}
  .screen{padding:0}
  .bnav{left:50%;right:auto;width:min(430px,calc(100vw - 20px));transform:translateX(-50%);bottom:calc(env(safe-area-inset-bottom) + 10px);border-radius:24px;border:1px solid rgba(255,255,255,.14);height:66px;background:rgba(17,19,24,.88)}
  .nav .lbl{font-size:8px}
}
@media(min-width:1200px){
  .app{max-width:440px}
  .bnav{width:440px}
}
</style>
</head>
<body>
<div class="app">
  <div class="top"><button class="icon back-icon" id="backBtn" onclick="appBack()" aria-label="Volver">‹</button><div class="brand"><div class="logo">M</div><div><small id="kicker">Bitácora Mars</small><strong id="title">Home</strong></div></div><button class="icon" onclick="refresh()" aria-label="Actualizar">↻</button></div>
  <main class="content">
    <section class="screen active" id="s-home"><div class="hero"><div class="kicker">Home</div><h1 id="greeting-h1">Buenos días,<br>Mars.</h1><p>Resumen vivo de carga, recuperación, última sesión y accesos rápidos.</p></div><div id="home-data" class="loading"><span class="spin"></span>Cargando...</div></section>
    <section class="screen" id="s-dashboard"><div class="hero"><div class="kicker">Dashboard / naranja</div><h1>Estado<br>actual.</h1><p>Fitness, fatiga, Z2 y carga semanal en una vista rápida.</p></div><div id="dash-data" class="loading"><span class="spin"></span>Cargando...</div></section>
    <section class="screen" id="s-activities"><div class="hero"><div class="kicker">Actividades / bici</div><h1>Sesiones<br>recientes.</h1><p>Sube FIT/ZIP y revisa tus últimas actividades sin salir de la app.</p></div><label class="upload"><input type="file" accept=".fit,.zip" onchange="uploadFit(this.files[0])"><div class="big">↑</div><h3>Subir FIT / ZIP</h3><p>Garmin Connect export</p></label><div id="upload-result"></div><div class="card"><div class="head"><h3>Últimas actividades</h3><span id="act-count">—</span></div><div id="act-list" class="loading"><span class="spin"></span>Cargando...</div></div></section>
    <section class="screen" id="s-gear"><div class="hero"><div class="kicker">Gear / mantenimiento</div><h1>La<br>Rarotonga.</h1><p>Componentes, alertas, kilometraje y vida útil de cadena, llantas y accesorios.</p></div><div id="gear-data" class="loading"><span class="spin"></span>Cargando...</div>
  <div id="gear-history"></div>
  <div class="card" style="margin-top:4px">
    <div class="head"><h3>Registrar servicio</h3></div>
    <div class="grid2">
      <div class="field"><label>Tipo</label><select id="gs-type">
  <option value="cambio_cadena">Cambio cadena</option>
  <option value="cambio_llanta_del">Cambio llanta delantera</option>
  <option value="cambio_llanta_tra">Cambio llanta trasera</option>
  <option value="cambio_cassette">Cambio cassette</option>
  <option value="cambio_pastillas">Cambio pastillas freno</option>
  <option value="cambio_cables">Cambio cables/fundas</option>
  <option value="cambio_bartape">Cambio bartape</option>
  <option value="cambio_potencia">Cambio potencia</option>
  <option value="cambio_manubrio">Cambio manubrio</option>
  <option value="cambio_silla">Cambio silla</option>
  <option value="cambio_tija">Cambio tija</option>
  <option value="ajuste_transmision">Ajuste transmision</option>
  <option value="ajuste_frenos">Ajuste frenos</option>
  <option value="ajuste_horquilla">Ajuste horquilla</option>
  <option value="lubricacion">Lubricacion cadena</option>
  <option value="lavado">Lavado completo</option>
  <option value="revision_general">Revision general</option>
  <option value="instalacion_potenciometro">Instalacion potenciometro</option>
  <option value="otro">Otro</option>
</select></div>
      <div class="field"><label>Componente</label><input id="gs-comp" placeholder="Shimano HG601"></div>
      <div class="field"><label>Km actuales</label><input type="number" id="gs-km" placeholder="12500"></div>
      <div class="field"><label>Costo MXN</label><input type="number" id="gs-cost" placeholder="450"></div>
    </div>
    <div class="field"><label>Notas</label><input id="gs-notes" placeholder="Taller, condicion..."></div>
    <button class="btn" onclick="saveGearService()">Registrar servicio</button>
  </div>
</section>
    <section class="screen" id="s-calendar"><div class="hero"><div class="kicker">Calendario / heatmap</div><h1>Constancia<br>visual.</h1><p>Mapa de actividad para detectar ritmo, huecos y semanas fuertes.</p></div><div id="cal-data" class="loading"><span class="spin"></span>Cargando...</div></section>
    <section class="screen" id="s-performance"><div class="hero"><div class="kicker">Performance / púrpura</div><h1>Récords y<br>progreso.</h1><p>VO2Max, carga, eficiencia aeróbica y marcas personales.</p></div><div id="perf-data" class="loading"><span class="spin"></span>Cargando...</div></section>
    <section class="screen" id="s-fuerza"><div class="hero"><div class="kicker">Fuerza / verde lima</div><h1>Mapa<br>muscular.</h1><p>Compex, gimnasio, pliometría, core y bandas por grupo muscular.</p></div><div class="bodymap"><div class="mq"><div>Quads <strong id="mq-q">—</strong></div><div>Glutes <strong id="mq-g">—</strong></div><div>Core <strong id="mq-c">—</strong></div><div>Calves <strong id="mq-ca">—</strong></div></div><div class="human"><div class="h"></div><div class="t"></div><div class="a al"></div><div class="a ar"></div><div class="l ll"></div><div class="l lr"></div></div></div><div class="card"><div class="head"><h3>Registro rápido</h3><span>hoy</span></div><div class="field"><label>Categoría</label><select id="fv-cat"><option value="compex">Compex</option><option value="gym">Gimnasio</option><option value="plyo">Pliometría</option><option value="core">Core</option><option value="bands">Bandas</option></select></div><div class="field"><label>Grupos musculares</label><input id="fv-muscles" placeholder="quadriceps, glutes"></div><div class="grid2"><div class="field"><label>Intensidad</label><input type="number" id="fv-intensity" placeholder="58"></div><div class="field"><label>Duración</label><input type="number" id="fv-duration" placeholder="20"></div></div><button class="btn" onclick="saveFuerza()">Guardar fuerza</button></div><div id="fuerza-data"></div></section>
    <section class="screen" id="s-wellness"><div class="hero"><div class="kicker">Wellness / azul</div><h1>Recupera<br>mejor.</h1><p>Sueño, estrés, molestias, Ceragem, pistola y Compex Recovery.</p></div><div id="well-data" class="loading"><span class="spin"></span>Cargando...</div><div class="card"><div class="head"><h3>Registro rápido</h3><span>hoy</span></div><div class="field"><label>Tipo</label><select id="wv-cat"><option value="sleep">Sueño</option><option value="compex_recovery">Compex Recovery</option><option value="massage_gun">Pistola</option><option value="ceragem">Ceragem</option><option value="pain">Molestia</option><option value="stress">Estrés</option></select></div><div class="grid2"><div class="field"><label>Duración / horas</label><input type="number" step="0.5" id="wv-duration" placeholder="7.5"></div><div class="field"><label>Fatiga</label><input type="number" id="wv-fatigue" placeholder="5"></div></div><button class="btn" onclick="saveWellness()">Guardar wellness</button></div></section>
  
    <section class="screen" id="s-eficiencia">
      <div class="hero"><div class="kicker">Eficiencia aerobica</div><h1>Vel/FC<br>ratio.</h1><p>Rendimiento por latido. Objetivo: 0.155+. Base: 0.1483.</p></div>
      <div id="eff-data"><div class="loading"><span class="spin"></span>Cargando...</div></div>
    </section>
    <section class="screen" id="s-correlaciones">
      <div class="hero"><div class="kicker">Correlaciones</div><h1>FC · Peso ·<br>Render.</h1><p>Como impacta tu peso en FC y eficiencia semana a semana.</p></div>
      <div id="corr-data"><div class="loading"><span class="spin"></span>Cargando...</div></div>
    </section>
    <section class="screen" id="s-nutricion">
      <div class="hero"><div class="kicker">Nutricion</div><h1>Geles<br>y agua.</h1><p>Registro de geles caseros, hidratacion y respuesta GI.</p></div>
      <div id="nutri-summary"><div class="loading"><span class="spin"></span></div></div>
      <div class="card" style="margin-top:4px">
        <div class="head"><h3>Registro rapido</h3></div>
        <div class="grid2">
          <div class="field"><label>Tipo gel</label><select id="nf-type"><option value="agave_casero">Agave casero</option><option value="miel_casero">Miel casero</option><option value="comercial">Comercial</option><option value="fecha">Fechas/fruta</option></select></div>
          <div class="field"><label>Momento</label><select id="nf-moment"><option value="pre">Pre</option><option value="durante">Durante</option><option value="post">Post</option></select></div>
          <div class="field"><label>Cantidad</label><input type="number" id="nf-count" placeholder="2"></div>
          <div class="field"><label>Agua (ml)</label><input type="number" id="nf-agua" placeholder="500"></div>
          <div class="field"><label>Carbos (g)</label><input type="number" id="nf-carbos" placeholder="40"></div>
          <div class="field"><label>Resp. GI</label><select id="nf-gi"><option value="sin_problemas">Sin problemas</option><option value="inflamacion">Inflamacion</option><option value="nausea">Nausea</option></select></div>
        </div>
        <div class="field"><label>Energia</label><select id="nf-energy"><option value="estable">Estable</option><option value="gradual">Gradual</option><option value="caida">Caida</option></select></div>
        <div class="field"><label>Notas</label><input id="nf-notes" placeholder="Sensaciones, timing..."></div>
        <button class="btn" onclick="saveNutricion()">Guardar nutricion</button>
      </div>
    </section>
  
    <section class="screen" id="s-perfil">
      <div class="hero"><div class="kicker">Perfil Mars</div><h1>Tu data,<br>tu historia.</h1><p>Zonas, bici, objetivos, gel, plan Garmin y estado actual en un solo lugar.</p></div>
      <div id="perfil-data"><div class="loading"><span class="spin"></span>Cargando...</div></div>
    </section>
  
    <section class="screen" id="s-coach">
      <div class="hero"><div class="kicker">Coach</div><h1>Tu plan,<br>hoy.</h1><p>Recomendacion basada en tu perfil, historial, zonas y estado actual.</p></div>
      <div id="coach-data"><div class="loading"><span class="spin"></span>Cargando...</div></div>
    </section>
  
    <section class="screen" id="s-progress">
      <div class="hero"><div class="kicker">Progress</div><h1>Tu evolucion<br>real.</h1><p>Peso, eficiencia, tendencias y rendimiento desde tu historial.</p></div>
      <div id="progress-data"><div class="loading"><span class="spin"></span>Cargando...</div></div>
    </section>
  </main>
  <nav class="bnav"><button class="nav active" data-s="home" onclick="go('home')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg><span class="lbl">Inicio</span></button><button class="nav" data-s="activities" onclick="go('activities')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><path d="M15 6a1 1 0 1 0 0-2 1 1 0 0 0 0 2"/><path d="M10 7l-2.5 5H5.5m4.5-5l5 3 3.5 7.5M10 7l2-5"/></svg><span class="lbl">Bici</span></button><button class="nav" data-s="dashboard" onclick="go('dashboard')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg><span class="lbl">Stats</span></button><button class="nav" data-s="gear" onclick="go('gear')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><span class="lbl">Gear</span></button><button class="nav" data-s="calendar" onclick="go('calendar')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg><span class="lbl">Cal</span></button><button class="nav" data-s="fuerza" onclick="go('fuerza')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 6.5h11M6.5 17.5h11M5 12h14"/><circle cx="3.5" cy="12" r="1.5"/><circle cx="20.5" cy="12" r="1.5"/><circle cx="3.5" cy="6.5" r="1.5"/><circle cx="20.5" cy="6.5" r="1.5"/><circle cx="3.5" cy="17.5" r="1.5"/><circle cx="20.5" cy="17.5" r="1.5"/></svg><span class="lbl">Fuerza</span></button><button class="nav" data-s="wellness" onclick="go('wellness')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg><span class="lbl">Wellness</span></button><button class="nav" data-s="performance" onclick="go('performance')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg><span class="lbl">Récords</span></button><button class="nav" data-s="eficiencia" onclick="go('eficiencia')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg><span class="lbl">Carga</span></button><button class="nav" data-s="correlaciones" onclick="go('correlaciones')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2 20 6 12 10 16 14 10 18 14 22 4"/></svg><span class="lbl">Correl.</span></button><button class="nav" data-s="nutricion" onclick="go('nutricion')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 10 10c0 5.52-4.48 10-10 10S2 17.52 2 12"/><path d="M12 6v6l4 2"/></svg><span class="lbl">Nutri.</span></button><button class="nav" data-s="perfil" onclick="go('perfil')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg><span class="lbl">Perfil</span></button><button class="nav" data-s="coach" onclick="go('coach')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2z"/><path d="M12 8v4l3 3"/></svg><span class="lbl">Coach</span></button><button class="nav" data-s="progress" onclick="go('progress')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg><span class="lbl">Progreso</span></button></nav>
</div><div class="toast" id="toast"></div>
<script>
const API=window.location.origin;
const THEME={home:'#c8cbd2',perfil:'#3dd68c',coach:'#4a9eff',progress:'#a78bfa',dashboard:'#e8593c',activities:'#e8593c',gear:'#f59e0b',calendar:'#22d3ee',performance:'#a78bfa',fuerza:'#c8f135',wellness:'#4a9eff',eficiencia:'#3dd68c',correlaciones:'#a78bfa',nutricion:'#f59e0b'};
const TITLE={home:['Bitácora Mars','Home'],perfil:['Perfil Mars','atleta'],coach:['Coach','recomendacion'],progress:['Progress','evolucion'],dashboard:['Dashboard','stats'],activities:['Bici','sesiones'],gear:['Gear','mantenimiento'],calendar:['Calendario','heatmap'],performance:['Récords','personales'],fuerza:['Fuerza','Compex'],wellness:['Wellness','recuperación'],eficiencia:['Eficiencia','aeróbica'],correlaciones:['Correlaciones','FC · Peso'],nutricion:['Nutrición','geles']};
let current='home';
let navStack=[];
function $(id){return document.getElementById(id)}
function setTheme(s){document.documentElement.style.setProperty('--theme',THEME[s]||'#fff');$('kicker').textContent=TITLE[s][0];$('title').textContent=TITLE[s][1]}
function updateBack(){const b=$('backBtn');if(b)b.style.visibility=current==='home'?'hidden':'visible'}
function go(s,push=true){if(push&&current&&current!==s)navStack.push(current);current=s;setTheme(s);document.querySelectorAll('.screen').forEach(x=>x.classList.toggle('active',x.id==='s-'+s));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.s===s));updateBack();load(s)}
function appBack(){const prev=navStack.pop();if(prev){go(prev,false);return}if(current!=='home'){go('home',false);return}if(history.length>1)history.back()}
function refresh(){load(current)}
function toast(m){const t=$('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2400)}
function metric(l,v,u){return `<div class="mini"><div class="label">${l}</div><div class="value">${v??'—'}</div><div class="unit">${u||''}</div></div>`}
function row(i,t,s,v){return `<div class="row"><div class="r-ico">${i}</div><div class="r-main"><div class="r-title">${t||'—'}</div><div class="r-sub">${s||''}</div></div><div class="r-val">${v||''}</div></div>`}
function date(v){return fmtDate(v)}
function fmtDate(v){
  if(!v)return '—';
  try{
    const d=new Date((v+'').slice(0,10)+'T12:00:00');
    const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
  }catch(e){return (v+'').slice(0,10)}
}
function fmtShort(v){
  if(!v)return '—';
  try{
    const d=new Date((v+'').slice(0,10)+'T12:00:00');
    const M=['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
    return d.getDate()+' '+M[d.getMonth()];
  }catch(e){return (v+'').slice(5,10)}
}
function hms(sec){sec=parseInt(sec)||0;const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60);return h?`${h}h ${String(m).padStart(2,'0')}m`:`${m}m`}
async function load(s){if(s==='home')return loadHome();if(s==='dashboard')return loadDash();if(s==='activities')return loadActs();if(s==='gear')return loadGear();if(s==='calendar')return loadCal();if(s==='performance')return loadPerf();if(s==='fuerza')return loadFuerza();if(s==='wellness')return loadWell();if(s==='eficiencia')return loadEficiencia();if(s==='correlaciones')return loadCorrelaciones();if(s==='nutricion')return loadNutricion();if(s==='perfil')return loadPerfil();if(s==='coach')return loadCoach();if(s==='progress')return loadProgress();}
async function loadHome(){
  try{
    const [d,w,mp,wh]=await Promise.all([
      fetch(API+'/gpt/dashboard').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/mars-context').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()).catch(()=>({}))
    ]);
    const a=d.athlete||{},s=d.semana_actual||{},z=d.z2_check||{};
    const mpz=mp.zonas_ciclismo||{},z2=mpz.z2||[134,150];
    const usingFallback=!mp.zonas_ciclismo;
    const plan=mp.plan_garmin||{},bici=mp.bici||{};
    const peso=wh.current_kg||(mp.athlete||{}).peso_actual_kg||89.1;
    const pesoObj=(mp.athlete||{}).peso_objetivo_kg||80;
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const effPct=Math.min(100,Math.round((effActual/(mp.eff_obj||0.155))*100));
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const pains=(w.molestias_activas||[]).length;
    $('home-data').innerHTML=
      '<div class="grid2">'+
        metric('Km semana',Number(s.km||0).toFixed(0),'km')+
        metric('Horas',Number(s.horas||0).toFixed(1),'h')+
        metric('Sesiones',s.sesiones||0,'semana')+
        metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(0)+'%','4 semanas')+
      '</div>'+
      (usingFallback?'<div style="background:rgba(232,89,60,.1);border:1px solid rgba(232,89,60,.3);border-radius:10px;padding:8px 12px;font-size:11px;color:#e8593c;margin-bottom:8px">⚠ Usando datos de respaldo — revisar conexion</div>':'')+'<div class="card" style="margin-bottom:8px">'+
        '<div style="display:flex;flex-wrap:wrap;gap:6px">'+
          '<span style="background:rgba(74,158,255,.1);color:#4a9eff;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+(plan.fase||'Base')+'</span>'+
          '<span style="background:rgba(61,214,140,.1);color:#3dd68c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Z2: '+z2[0]+'–'+z2[1]+' bpm</span>'+
          '<span style="background:rgba(245,158,11,.1);color:#f59e0b;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">Cad: '+(mp.cadencia_obj||100)+' rpm</span>'+
          '<span style="background:rgba(232,89,60,.1);color:#e8593c;padding:3px 8px;border-radius:12px;font-size:11px;font-weight:800">'+(bici.nombre||'Rarotonga')+' '+(bici.km||716)+' km</span>'+
        '</div>'+
      '</div>'+
      '<div class="card"><div class="head"><h3>Estado del atleta</h3><span>'+(a.fitness||'—')+'</span></div>'+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M5 7l1.5 1.5L9 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>','Fitness',d.recommendation||'Sin recomendacion',a.mars_index?Number(a.mars_index).toFixed(1):'—')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2v5l3 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/></svg>','Fatiga',a.fatiga||'—','')+
        row('<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 3c0 0-4 3-4 6a4 4 0 008 0c0-3-4-6-4-6z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>','Molestias',pains?pains+' activa(s)':'Sin alertas','')+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Peso</div><div class="r-sub">M1: '+pesoObj+' kg · Final: '+((mp.athlete||{}).peso_meta_final_kg||70)+' kg'+(pesoDiff>0?' · faltan '+pesoDiff+' kg':' meta M1 lograda')+'</div></div><div class="r-val" style="color:#3dd68c">'+peso+' kg</div></div>'+
        '<div class="row"><div class="r-ico">·</div><div class="r-main"><div class="r-title">Eficiencia vel/FC</div><div class="r-sub">base 0.1483 → obj 0.155</div></div><div style="text-align:right"><div style="font-size:13px;font-weight:800;color:#a78bfa">'+effActual.toFixed(4)+'</div><div style="font-size:10px;color:var(--muted)">'+effPct+'%</div></div></div>'+
      '</div>';
  }catch(e){$('home-data').innerHTML='<div class="card" style="color:var(--muted)">Error: '+e.message+'</div>';}
}

async function loadDash(){try{const d=await fetch(API+'/gpt/dashboard').then(r=>r.json());const a=d.athlete||{},s=d.semana_actual||{},c=d.carga||{},z=d.z2_check||{};const icoKm='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M5 7h4M9 7l-2-2M9 7l-2 2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';const icoTime='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M7 4v3l2 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';const icoCal='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 2c0 0-4 3.5-4 6.5a4 4 0 008 0C11 5.5 7 2 7 2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';$('dash-data').innerHTML=`<div class="grid2">${metric('Fitness',a.fitness||'—','Mars Index '+(a.mars_index||'—'))}${metric('Fatiga',a.fatiga||'—','TSB '+(c.tsb||0))}${metric('Carga',c.estado||'—','actual')}${metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(1)+'%','4 semanas')}</div><div class="card"><div class="head"><h3>Semana actual</h3><span>${s.sesiones||0} sesiones</span></div>${row(icoKm,'Distancia semanal','Acumulado Garmin',Number(s.km||0).toFixed(1)+' km')}${row(icoTime,'Tiempo semanal','Horas de carga',Number(s.horas||0).toFixed(1)+' h')}${row(icoCal,'Calorías','Estimado semanal',s.calorias||'—')}</div>`}catch(e){$('dash-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
async function loadActs(){
  try{
    const d=await fetch(API+'/sessions?sport=cycling&limit=20').then(r=>r.json());
    const arr=d.sessions||d||[];
    $('act-count').textContent=arr.length;
    if(!arr.length){$('act-list').innerHTML='<div style="color:var(--muted);padding:20px;text-align:center">Sin actividades</div>';return;}
    $('act-list').innerHTML=arr.map(function(s){
      return '<div class="row" style="align-items:flex-start">'+
        '<div class="r-ico" style="background:rgba(232,89,60,.1);width:42px;height:42px;border-radius:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:2px">'+
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#e8593c" stroke-width="1.8" stroke-linecap="round"><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><path d="M10 7L7.5 12H5.5m4.5-5l5 3 3.5 7.5M10 7l2-5"/></svg>'+
        '</div>'+
        '<div class="r-main" style="flex:1;min-width:0;cursor:pointer" onclick="window.location.href=\x27/charts/'+s.session_id+'\x27">'+
          '<div class="r-title">'+(s.workout_name||'Ciclismo')+'</div>'+
          '<div class="r-sub">'+fmtDate(s.start_time)+'</div>'+
          '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:3px;font-size:11px;color:var(--muted)">'+
            '<span>'+(s.duration_hms||hms(s.duration_s)||'--')+'</span>'+
            '<span>FC '+(s.avg_hr_bpm||'--')+' bpm</span>'+
            '<span>+'+(s.ascent_m||0)+' m</span>'+
          '</div>'+
        '</div>'+
        '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0">'+
          '<div style="font-size:17px;font-weight:950;color:var(--theme)">'+(s.distance_km||'--')+' km</div>'+
          '<div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end">'+
            '<button onclick="event.stopPropagation();window.location.href=\x27/charts/'+s.session_id+'\x27" style="background:rgba(255,255,255,.08);border:none;border-radius:8px;padding:4px 8px;color:var(--text);font-size:10px;font-weight:800;cursor:pointer">ver</button>'+
            '<button onclick="event.stopPropagation();openMatched(\x27'+s.session_id+'\x27,\x27'+(s.route_id||'')+'\x27)" style="background:rgba(232,89,60,.12);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer">comparar</button>'+
          '</div>'+
        '</div>'+
      '</div>';
    }).join('');
  }catch(e){$('act-list').innerHTML='<div style="color:var(--muted)">'+e.message+'</div>'}
}

function openMatched(sessionId, routeId){
  if(routeId){
    window.location.href='/route/'+routeId+'/matched';
  }else{
    window.location.href='/gpt/matched-rides/'+sessionId;
  }
}

async function uploadFit(file){
  if(!file)return;
  $('upload-result').innerHTML='';$('upload-result').innerHTML='<div class="card">Procesando '+file.name+'...</div>';
  const fd=new FormData();
  fd.append('file',file);
  try{
    const d=await fetch(API+'/analyze-fit',{method:'POST',body:fd}).then(r=>r.json());
    $('upload-result').innerHTML='';
    const s=d.session||{};
    let html='<div class="card"><div class="head"><h3>'+(d.duplicate?'Sesion existente':'Sesion guardada')+'</h3><span>'+d.session_id+'</span></div>'+
      '<div class="grid2">'+
        metric('Distancia',s.distance_km||'—','km')+
        metric('FC prom.',s.avg_hr_bpm||'—','bpm')+
        metric('Duracion',s.duration_hms||'—','')+
        metric('Ascenso',s.ascent_m||'—','m')+
      '</div>'+
      '<button class="btn" style="margin-top:8px" onclick="window.location.href=\x27/charts/'+d.session_id+'\x27">Ver graficas</button>'+
      '<button class="btn btn2" style="margin-top:4px" onclick="navigator.clipboard.writeText(\x27'+d.session_id+'\x27);toast(\x27ID copiado\x27)">Copiar session_id</button>'+
    '</div>';
    $('upload-result').innerHTML=html;
    // Auto-load matched rides
    try{
      const mr=await fetch(API+'/gpt/matched-rides/'+d.session_id).then(r=>r.json());
      if(mr.matched&&mr.matched.length>0){
        const vs=mr.vs_historical||{};
        const verdictCol=vs.verdict==='mejoraste'?'#3dd68c':vs.verdict==='bajaste'?'#e8593c':'#f59e0b';
        let mrHtml='<div class="card" style="margin-top:8px">'+
          '<div class="head"><h3>Matched Rides</h3><span style="color:'+verdictCol+'">'+vs.verdict+'</span></div>'+
          '<div style="font-size:12px;color:var(--muted);margin-bottom:10px">'+mr.route_label+' · '+mr.count+' ejecuciones anteriores</div>';
        if(vs.speed_delta!=null){
          mrHtml+='<div style="display:flex;gap:10px;margin-bottom:10px">'+
            '<div style="flex:1;background:rgba(61,214,140,.08);border-radius:8px;padding:8px;text-align:center">'+
              '<div style="font-size:10px;color:var(--muted)">VELOCIDAD HOY</div>'+
              '<div style="font-size:16px;font-weight:950;color:#3dd68c">'+(mr.reference.avg_speed_kmh||0).toFixed(1)+' km/h</div>'+
              '<div style="font-size:10px;color:'+(vs.speed_delta>=0?'#3dd68c':'#e8593c')+'">'+(vs.speed_delta>=0?'+':'')+vs.speed_delta+' vs hist.</div>'+
            '</div>'+
            '<div style="flex:1;background:rgba(74,158,255,.08);border-radius:8px;padding:8px;text-align:center">'+
              '<div style="font-size:10px;color:var(--muted)">FC HOY</div>'+
              '<div style="font-size:16px;font-weight:950;color:#4a9eff">'+(mr.reference.avg_hr_bpm||0).toFixed(0)+' bpm</div>'+
              '<div style="font-size:10px;color:var(--muted)">hist: '+(vs.avg_hr_hist||'—')+'</div>'+
            '</div>'+
          '</div>';
        }
        mrHtml+='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:11px">'+
          '<tr style="color:var(--muted)"><td style="padding:4px">Fecha</td><td style="padding:4px;text-align:right">km/h</td><td style="padding:4px;text-align:right">FC</td><td style="padding:4px;text-align:right">Efic.</td></tr>'+
          mr.matched.slice(0,6).map(function(m){
            return '<tr style="border-top:1px solid rgba(255,255,255,.06)">'+
              '<td style="padding:4px;color:var(--muted)">'+fmtShort(m.dt)+'</td>'+
              '<td style="padding:4px;text-align:right;font-weight:800">'+(m.avg_speed_kmh||'—')+'</td>'+
              '<td style="padding:4px;text-align:right">'+(m.avg_hr_bpm||'—')+'</td>'+
              '<td style="padding:4px;text-align:right;color:#3dd68c">'+(m.efficiency||'—')+'</td>'+
            '</tr>';
          }).join('')+
        '</table></div></div>';
        $('upload-result').innerHTML+= mrHtml;
      }
    }catch(me){/* matched rides optional */}
    loadActs();
  }catch(e){$('upload-result').innerHTML='<div class="card">Error: '+e.message+'</div>'}
}

async function loadGear(){loadGearHistory();try{const d=await fetch(API+'/gpt/gear-status').then(r=>r.json()).catch(()=>null);const a=await fetch(API+'/gpt/gear-alerts').then(r=>r.json()).catch(()=>({alerts:[]}));const items=(d&&d.components)||d?.gear||[];let html=`<div class="card"><div class="head"><h3>Alertas</h3><span>${(a.alerts||[]).length}</span></div>${(a.alerts||[]).length?(a.alerts||[]).map(x=>row('',x.name||x.type||'Alerta',x.message||x.detail||'',x.km_left?x.km_left+' km':'' )).join(''):'Sin alertas de equipo'}</div>`;html+=`<div class="card"><div class="head"><h3>Componentes</h3><span>${items.length||0}</span></div>${items.length?items.map(g=>{let pct=Math.min(100,Math.round(((g.km_current||g.current_km||0)/(g.km_limit||g.limit_km||4500))*100));return `<div class="row"><div class="r-ico">—</div><div class="r-main"><div class="r-title">${g.name||g.type||'Componente'}</div><div class="r-sub">${g.km_current||g.current_km||0} / ${g.km_limit||g.limit_km||'—'} km<div class="gearbar"><div class="gearfill" style="width:${pct}%"></div></div></div></div><div class="r-val">${pct}%</div></div>`}).join(''):'Sin componentes registrados'}</div>`;$('gear-data').innerHTML=html}catch(e){$('gear-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
async function loadCal(){
  try{
    const d=await fetch(API+'/gpt/calendar-heatmap?months=3').then(r=>r.json());
    let days=d.days||d.calendar||[];
    if(!Array.isArray(days)){
      if(d.heatmap&&typeof d.heatmap==='object'){
        days=Object.entries(d.heatmap).sort((a,b)=>a[0]>b[0]?1:-1).map(function(e){return{date:e[0],count:e[1]||0}});
      } else {
        const arr=Object.values(d).find(function(v){return Array.isArray(v)&&v.length>0});
        if(arr)days=arr;
      }
    }
    const recent=days.slice(-42);
    const active=recent.filter(function(x){return(x.count||x.sessions||0)>0}).length;
    const total=recent.reduce(function(a,x){return a+(x.count||x.sessions||0)},0);
    const DOWS=['L','M','X','J','V','S','D'];
    $('cal-data').innerHTML=
      '<div class="grid2"><div class="card-sm"><div class="kl">Dias activos</div><div class="kv" style="color:#22d3ee">'+active+'</div><div class="ku">6 semanas</div></div><div class="card-sm"><div class="kl">Sesiones</div><div class="kv">'+total+'</div></div></div>'+
      '<div class="card"><div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:4px">'+DOWS.map(function(d){return'<div style="text-align:center;font-size:9px;color:var(--muted);font-weight:900">'+d+'</div>'}).join('')+'</div>'+
      '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px">'+recent.map(function(x){
        var n=x.count||x.sessions||0;
        var bg=n===0?'rgba(255,255,255,.04)':n===1?'rgba(34,211,238,.2)':n===2?'rgba(34,211,238,.4)':'rgba(34,211,238,.7)';
        return'<div style="aspect-ratio:1;border-radius:4px;background:'+bg+';display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--muted)">'+(x.date?(x.date+'').slice(8,10):'')+'</div>';
      }).join('')+'</div></div>';
  }catch(e){$('cal-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadPerf(){
  try{
    const d=await fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json());
    const r=d.records||{},c=d.carga||{},eff=d.eficiencia_aerobica||{};
    function recRow(label,rec,val,col){
      if(!rec)return '';
      return '<div class="row"><div class="r-ico" style="background:color-mix(in srgb,'+col+' 12%,#111);width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0"><div style="width:8px;height:8px;border-radius:50%;background:'+col+'"></div></div><div class="r-main"><div class="r-title">'+label+'</div><div class="r-sub">'+fmtDate(rec.date)+'</div></div><div class="r-val" style="color:'+col+'">'+val+'</div></div>';
    }
    $('perf-data').innerHTML='<div class="grid2">'+
      metric('VO2Max',d.vo2max_estimado||'—','estimado')+
      metric('TSB',c.tsb||0,c.estado||'carga')+
      metric('Cadencia',d.cadencia_trend||'—','trend')+
      metric('Eficiencia',eff.delta_pct_6_meses??'—','6 meses')+
    '</div><div class="card"><div class="head"><h3>Records personales</h3><span>ciclismo</span></div>'+
      recRow('Mayor distancia',r.max_distance,Number((r.max_distance||{}).value||0).toFixed(1)+' km','#e8593c')+
      recRow('Mayor ascenso',r.max_ascent,'+'+parseInt((r.max_ascent||{}).value||0)+' m','#c8f135')+
      recRow('Mayor velocidad',r.max_speed,Number((r.max_speed||{}).value||0).toFixed(1)+' km/h','#a78bfa')+
      recRow('Sesion mas larga',r.max_duration,hms((r.max_duration||{}).value||0),'#4a9eff')+
    '</div>';
  // Add route history
    try{
      const rh=await fetch(API+'/gpt/route-history?weeks=26').then(r=>r.json());
      if(rh.routes&&rh.routes.length>0){
        let rhHtml='<div class="card" style="margin-top:8px"><div class="head"><h3>Progresion por ruta</h3><span>'+rh.routes.length+' rutas</span></div>';
        rh.routes.forEach(function(r){
          rhHtml+='<div style="padding:10px 0;border-bottom:1px solid rgba(255,255,255,.07)">'+
            '<div style="display:flex;justify-content:space-between;margin-bottom:4px">'+
              '<div style="font-size:13px;font-weight:800">'+r.label+'</div>'+
              '<div style="font-size:11px;color:var(--muted)">'+parseInt(r.n)+' veces</div>'+
            '</div>'+
            '<div style="display:flex;gap:16px;font-size:11px;color:var(--muted)">'+
              '<span>Mejor: <b style="color:#3dd68c">'+(r.best_speed||'—')+' km/h</b></span>'+
              '<span>Prom: '+(r.avg_speed||'—')+' km/h</span>'+
              '<span>FC: '+(r.avg_hr||'—')+' bpm</span>'+
            '</div>'+
          '</div>';
        });
        rhHtml+='</div>';
        $('perf-data').innerHTML+= rhHtml;
      }
    }catch(re){}
  }catch(e){$('perf-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadFuerza(){
  try{
    const [d,hist]=await Promise.all([
      fetch(API+'/gpt/fuerza-summary?weeks=8').then(r=>r.json()),
      fetch(API+'/api/fuerza-records?limit=10').then(r=>r.json()).catch(()=>({}))
    ]);
    const items=hist.records||[];
    $('fuerza-data').innerHTML=
      '<div class="grid2">'+metric('Sesiones 8s',d.total_sesiones||0,'')+metric('Horas total',d.total_horas||0,'')+'</div>'+
      (items.length?'<div class="card"><div class="head"><h3>Ultimas sesiones</h3></div>'+
        items.map(e=>'<div class="row"><div class="r-main"><div class="r-title">'+(e.category||'Fuerza')+(e.subcategory?' · '+e.subcategory:'')+
          (e.intensity?' · Int '+e.intensity:'')+'</div>'+
          '<div class="r-sub">'+fmtDate(e.date)+(e.duration_min?' · '+e.duration_min+' min':'')+'</div></div>'+
          '<button onclick="deleteFuerzaRec('+e.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer;flex-shrink:0">borrar</button>'+
        '</div>').join('')+'</div>':'');
  }catch(e){}
}

async function saveFuerza(){const today=new Date().toISOString().slice(0,10);const body={date:today,category:$('fv-cat').value,muscle_groups:$('fv-muscles').value.split(',').map(x=>x.trim()).filter(Boolean),intensity:parseInt($('fv-intensity').value)||null,duration_min:parseInt($('fv-duration').value)||null};const d=await fetch(API+'/fuerza',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());toast(d.ok?'Fuerza guardada':'Error');loadFuerza()}
async function loadWell(){
  try{
    const [d,hist]=await Promise.all([
      fetch(API+'/gpt/wellness-summary?weeks=4').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/api/wellness-records?limit=10').then(r=>r.json()).catch(()=>({}))
    ]);
    const items=hist.registros||hist.records||hist.data||[];
    const pains=d.molestias_activas||[];
    const sleep=d.sueno_promedio_horas||'—';
    const stress=d.estres_promedio||'—';
    const fc=d.fc_reposo_promedio||'—';
    $('well-data').innerHTML=
      '<div class="grid2">'+
        metric('Sueno prom',sleep,'h/noche')+
        metric('FC reposo',fc,'bpm')+
        metric('Estres prom',stress,'/10')+
        metric('Molestias',pains.length,pains.length?'activas':'sin alertas')+
      '</div>'+
      (pains.length?'<div class="card" style="border-color:rgba(232,89,60,.3)"><div class="head"><h3 style="color:#e8593c">Molestias activas</h3><span>'+pains.length+'</span></div>'+
        pains.map(p=>'<div class="row"><div class="r-main"><div class="r-title">'+(p.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(p.pain_level||'?')+'/10</div></div>').join('')+'</div>':'')+
      (items.length?'<div class="card"><div class="head"><h3>Ultimos registros</h3></div>'+
        items.map(e=>'<div class="row"><div class="r-main"><div class="r-title">'+(e.category||'Wellness')+'</div>'+
          '<div class="r-sub">'+fmtDate(e.date)+(e.sleep_hours?' · '+e.sleep_hours+'h sueno':'')+(e.fatigue?' · fat '+e.fatigue:'')+'</div></div>'+
          '<button onclick="deleteWellnessRec('+e.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer;flex-shrink:0">borrar</button>'+
        '</div>').join('')+'</div>':'');
  }catch(e){$('well-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function saveWellness(){const today=new Date().toISOString().slice(0,10);const cat=$('wv-cat').value;const dur=parseFloat($('wv-duration').value)||null;const body={date:today,category:cat,fatigue:parseInt($('wv-fatigue').value)||null};if(cat==='sleep')body.sleep_hours=dur;else body.duration_min=dur;const d=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());toast(d.ok?'Wellness guardado':'Error');loadWell()}

async function loadEficiencia(){
  const el=$('eff-data');
  try{
    const [eff,perf]=await Promise.all([
      fetch(API+'/gpt/efficiency-trend?sport=cycling').then(r=>r.json()),
      fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json()),
    ]);
    const trend=eff.trend||eff.eficiencia||[];
    const carga=perf.carga||{};
    const tsbCol=carga.tsb>5?'#3dd68c':carga.tsb<-15?'#e8593c':'#4a9eff';
    function sparkSVG(vals,col,h){
      if(!vals||vals.length<2)return '';
      const mx=Math.max(...vals),mn=Math.min(...vals),rng=mx-mn||0.001,W=280;
      const pts=vals.map((v,i)=>Math.round(i/(vals.length-1)*W)+','+Math.round(h-(v-mn)/rng*(h-8)-4)).join(' ');
      const lx=W,ly=Math.round(h-(vals[vals.length-1]-mn)/rng*(h-8)-4);
      return '<svg viewBox="0 0 '+W+' '+h+'" style="width:100%;height:'+h+'px"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".5"/><circle cx="'+lx+'" cy="'+ly+'" r="4" fill="'+col+'"/></svg>';
    }
    const effVals=trend.map(t=>parseFloat(t.ratio||t.efficiency||t.vel_fc_ratio||0)).filter(v=>v>0);
    const curr=effVals.length?effVals[effVals.length-1]:null;
    const base=0.1483,target=0.155;
    const delta=curr?+(curr-base).toFixed(4):null;
    const pct=curr?Math.min(100,Math.round((curr/target)*100)):0;
    const cHist=carga.history||[];
    const ctlV=cHist.map(h=>h.ctl).filter(Boolean);
    el.innerHTML=`
      <div class="grid2">
        <div class="card-sm"><div class="kl">Ratio actual</div><div class="kv" style="color:#3dd68c">${curr?curr.toFixed(4):'—'}</div><div class="ku">${delta!=null?(delta>=0?'+':'')+delta+' vs base':''}</div></div>
        <div class="card-sm"><div class="kl">Objetivo</div><div class="kv" style="color:#3dd68c">0.155</div><div class="ku">${pct}% logrado</div></div>
      </div>
      <div class="card">
        <div class="head"><h3>Eficiencia vel/FC — ${trend.length} semanas</h3><span style="color:#3dd68c">${pct}%</span></div>
        <div class="pbar"><div class="pfill" style="width:${pct}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:10px"><span>Base: 0.1483</span><span>Objetivo: 0.155+</span></div>
        ${sparkSVG(effVals,'#3dd68c',60)}
      </div>
      <div class="card">
        <div class="head"><h3>Carga ATL/CTL/TSB</h3><span style="color:${tsbCol}">${carga.estado||'—'}</span></div>
        <div class="grid2" style="margin-bottom:8px">
          ${metric('ATL agudo',carga.atl||'—','')}${metric('CTL cronico',carga.ctl||'—','')}
          ${metric('TSB','<span style="color:'+tsbCol+'">'+(carga.tsb||0)+'</span>','')}${metric('Estado','<span style="color:'+tsbCol+'">'+(carga.tsb>5?'Listo':carga.tsb<-15?'Recuperar':'Normal')+'</span>','')}
        </div>
        ${ctlV.length>=2?sparkSVG(ctlV,'#4a9eff',50):'<div style="color:var(--muted);font-size:12px">Sin historial de carga</div>'}
      </div>`;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadCorrelaciones(){
  const el=$('corr-data');
  try{
    const d=await fetch(API+'/gpt/correlations?weeks=12').then(r=>r.json());
    const weekly=d.weekly||[],weight=d.weight||[];
    const corr=d.correlation_hr_efficiency;
    const corrCol=corr&&corr<-0.3?'#3dd68c':corr&&corr>0.3?'#e8593c':'var(--muted)';
    function scatterSVG(xs,ys,col){
      if(!xs||xs.length<3)return '<div style="color:var(--muted);font-size:12px;padding:8px">Necesitas mas semanas de datos</div>';
      const W=280,H=110,p=20;
      const mxx=Math.max(...xs),mnx=Math.min(...xs),rx=mxx-mnx||1;
      const mxy=Math.max(...ys),mny=Math.min(...ys),ry=mxy-mny||0.001;
      const dots=xs.map((x,i)=>{const px=p+Math.round((x-mnx)/rx*(W-p*2));const py=H-p-Math.round((ys[i]-mny)/ry*(H-p*2));return '<circle cx="'+px+'" cy="'+py+'" r="3" fill="'+col+'" opacity=".7"/>';}).join('');
      return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:'+H+'px"><line x1="'+p+'" y1="'+(H-p)+'" x2="'+(W-p)+'" y2="'+(H-p)+'" stroke="rgba(255,255,255,.08)"/><line x1="'+p+'" y1="'+p+'" x2="'+p+'" y2="'+(H-p)+'" stroke="rgba(255,255,255,.08)"/>'+dots+'</svg>';
    }
    const hrs=weekly.map(w=>w.avg_hr).filter(Boolean);
    const effs=weekly.map(w=>w.efficiency).filter(Boolean);
    const wkgs=weekly.map(w=>{const cl=weight.reduce((a,b)=>Math.abs(new Date(b.date)-new Date(w.week))<Math.abs(new Date(a.date)-new Date(w.week))?b:a,weight[0]||{});return cl?.kg||null;}).filter(Boolean);
    el.innerHTML=`
      <div class="card">
        <div class="head"><h3>FC vs Eficiencia aerobica</h3><span style="color:${corrCol}">${corr!=null?'r='+corr:'—'}</span></div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:10px">${d.interpretation||''}</div>
        ${scatterSVG(hrs,effs,'#3dd68c')}
        <div style="font-size:10px;color:var(--muted);margin-top:4px">Cada punto = 1 semana · eje X: FC prom · eje Y: vel/FC</div>
      </div>
      ${wkgs.length>=3?`<div class="card"><div class="head"><h3>Peso vs FC promedio</h3></div>${scatterSVG(wkgs,hrs,'#4a9eff')}<div style="font-size:10px;color:var(--muted);margin-top:4px">Eje X: peso kg · Eje Y: FC prom bpm</div></div>`:'<div class="card"><div style="color:var(--muted);font-size:13px;padding:8px">Agrega mas registros de peso para la correlacion</div></div>'}
      <div class="grid2">
        ${metric('FC prom 12s',hrs.length?+(hrs.reduce((a,b)=>a+b,0)/hrs.length).toFixed(1):'—','bpm obj &lt;135')}
        ${metric('Efic. prom',effs.length?+(effs.reduce((a,b)=>a+b,0)/effs.length).toFixed(4):'—','obj 0.155+')}
      </div>`;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadNutricion(){
  const el=$('nutri-summary');
  try{
    const d=await fetch(API+'/nutrition/summary?weeks=8').then(r=>r.json());
    const tipos=d.por_tipo||[];
    const GL={agave_casero:'Agave casero',miel_casero:'Miel casero',comercial:'Comercial',fecha:'Fechas/fruta',ninguno:'Sin gel'};
    el.innerHTML=tipos.length?`<div class="card"><div class="head"><h3>Geles 8 semanas</h3><span>${tipos.reduce((a,t)=>a+(t.usos||0),0)} registros</span></div>${tipos.map(t=>row('·',GL[t.gel_type]||t.gel_type,t.usos+' usos'+(t.gi_issues>0?' · '+t.gi_issues+' GI':''),t.avg_carbos?t.avg_carbos.toFixed(0)+'g':'')  ).join('')}</div>`
    :'<div class="card" style="margin-bottom:12px"><div style="color:var(--muted);text-align:center;padding:12px">Sin registros aun</div></div>';
  }catch(e){el.innerHTML=''}
}
async function saveNutricion(){
  const body={date:new Date().toISOString().slice(0,10),gel_type:$('nf-type').value,moment:$('nf-moment').value,gel_count:parseInt($('nf-count').value)||null,agua_ml:parseInt($('nf-agua').value)||null,carbos_g:parseFloat($('nf-carbos').value)||null,gi_response:$('nf-gi').value,energy_response:$('nf-energy').value,notas:$('nf-notes').value||null};
  const d=await fetch(API+'/nutrition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  toast(d.ok?'Nutricion guardada':'Error','#f59e0b');
  if(d.ok)loadNutricion();
}
async function loadGearHistory(){
  const el=$('gear-history');
  if(!el)return;
  try{
    const d=await fetch(API+'/gear/service-history?limit=15').then(r=>r.json());
    const hist=d.historial||[];
    if(!hist.length){el.innerHTML='';return;}
    const SL={cambio_cadena:'Cambio cadena',cambio_llanta_del:'Llanta del.',cambio_llanta_tra:'Llanta tra.',cambio_cassette:'Cassette',ajuste_transmision:'Ajuste transmision',lubricacion:'Lubricacion'};
    el.innerHTML='<div class="card"><div class="head"><h3>Historial servicios</h3><span>'+hist.length+'</span></div>'+hist.map(s=>'<div class="row"><div class="r-main"><div class="r-title">'+(SL[s.service_type]||s.service_type||'Servicio')+'</div><div class="r-sub">'+fmtDate(s.date)+(s.km_at_service?' · '+Number(s.km_at_service).toLocaleString()+' km':'')+(s.days_since!=null?' · hace '+s.days_since+'d':'')+'</div></div><div style="text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:4px">'+( s.cost_mxn?'<div style="font-size:12px;font-weight:800;color:#f59e0b">$'+s.cost_mxn+'</div>':'')+' <button onclick="deleteGearServiceRec('+s.id+')" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer">borrar</button></div></div>').join('')+'</div>';
  }catch(e){el.innerHTML=''}
}
async function saveGearService(){
  const body={service_type:$('gs-type').value,gear_name:$('gs-comp').value||null,date:new Date().toISOString().slice(0,10),km_at_service:parseFloat($('gs-km').value)||null,cost_mxn:parseFloat($('gs-cost').value)||null,notes:$('gs-notes').value||null};
  const d=await fetch(API+'/gear/service',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  toast(d.ok?'Servicio registrado':'Error','#f59e0b');
  if(d.ok){loadGear();loadGearHistory();}
}


(function(){
  const h=new Date().getHours();
  const g=h<12?'Buenos dias,':h<19?'Buenas tardes,':'Buenas noches,';
  const el=document.getElementById('greeting-h1');
  if(el)el.innerHTML=g+'<br>Mars.';
})();


async function deleteFuerzaRec(id){
  if(!confirm('Borrar este registro?'))return;
  const d=await fetch(API+'/fuerza/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Registro borrado');loadFuerza();}
  else toast('Error al borrar');
}
async function deleteWellnessRec(id){
  if(!confirm('Borrar este registro?'))return;
  const d=await fetch(API+'/wellness/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Registro borrado');loadWell();}
  else toast('Error al borrar');
}
async function deleteNutricionRec(id){
  if(!confirm('Borrar?'))return;
  const d=await fetch(API+'/nutrition/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Borrado');loadNutricion();}
  else toast('Error al borrar');
}
async function deleteGearServiceRec(id){
  if(!confirm('Borrar servicio?'))return;
  const d=await fetch(API+'/gear/service/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Borrado');loadGearHistory();}
  else toast('Error al borrar');
}


async function loadPerfil(){
  const el=$('perfil-data');
  try{
    const [p,w]=await Promise.all([
      fetch(API+'/gpt/mars-context').then(r=>r.json()),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()).catch(()=>({}))
    ]);
    const a=p.athlete||{};
    const zc=p.zonas_ciclismo||{};
    const zr=p.zonas_running||{};
    const bici=p.bici||{};
    const plan=p.plan_garmin||{};
    const nut=p.nutricion||{};
    const objetivos=p.objetivos||[];
    const rutas=p.rutas||[];
    const peso=w.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const effActual=p.eff_actual||p.eff_base||0.1483;
    const effObj=p.eff_obj||0.155;
    const effPct=Math.min(100,Math.round((effActual/effObj)*100));

    function zoneBar(label,lo,hi,col){
      return '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07)">'+
        '<div style="width:8px;height:8px;border-radius:50%;background:'+col+';flex-shrink:0"></div>'+
        '<div style="flex:1;font-size:12px;color:var(--muted)">'+label+'</div>'+
        '<div style="font-size:13px;font-weight:800;color:'+col+'">'+lo+'–'+hi+' bpm</div></div>';
    }

    el.innerHTML=`
      <!-- PESO -->
      <div class="card">
        <div class="head"><h3>Peso</h3><span style="color:#3dd68c">${peso} kg</span></div>
        <div class="pbar"><div class="pfill" style="width:${Math.max(0,100-Math.round((pesoDiff/20)*100))}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px">
          <span>Actual: ${peso} kg</span>
          <span>Objetivo: ${pesoObj} kg · faltan ${Math.max(0,pesoDiff)} kg</span>
        </div>
      </div>

      <!-- ZONAS CICLISMO -->
      <div class="card">
        <div class="head"><h3>Zonas FC ciclismo</h3><span style="color:var(--theme)">LT ${zc.lt_bpm||168} bpm</span></div>
        ${zoneBar('Z1 Recuperacion',0,zc.z1?zc.z1[1]:109,'#8b929f')}
        ${zoneBar('Z2 Aerobico',zc.z2?zc.z2[0]:109,zc.z2?zc.z2[1]:134,'#3dd68c')}
        ${zoneBar('Z3 Tempo',zc.z3?zc.z3[0]:134,zc.z3?zc.z3[1]:150,'#f59e0b')}
        ${zoneBar('Z4 Umbral',zc.z4?zc.z4[0]:150,zc.z4?zc.z4[1]:160,'#e8593c')}
        ${zoneBar('Z5 Maximo',zc.z5?zc.z5[0]:160,zc.z5?zc.z5[1]:168,'#c026d3')}
        <div style="font-size:11px;color:var(--muted);margin-top:8px">Max HR: ${zc.max_hr||196} bpm · Base % de LT</div>
      </div>

      <!-- ZONAS RUNNING -->
      <div class="card">
        <div class="head"><h3>Zonas FC running</h3><span style="color:var(--theme)">LT ${zr.lt_bpm||173} bpm</span></div>
        ${zoneBar('Z1 Recuperacion',0,zr.z1?zr.z1[1]:112,'#8b929f')}
        ${zoneBar('Z2 Aerobico',zr.z2?zr.z2[0]:112,zr.z2?zr.z2[1]:138,'#3dd68c')}
        ${zoneBar('Z3 Tempo',zr.z3?zr.z3[0]:138,zr.z3?zr.z3[1]:154,'#f59e0b')}
        ${zoneBar('Z4 Umbral',zr.z4?zr.z4[0]:154,zr.z4?zr.z4[1]:164,'#e8593c')}
        ${zoneBar('Z5 Maximo',zr.z5?zr.z5[0]:164,zr.z5?zr.z5[1]:173,'#c026d3')}
        <div style="font-size:11px;color:var(--muted);margin-top:8px">Max HR: ${zr.max_hr||194} bpm</div>
      </div>

      <!-- BICI + GEAR -->
      <div class="card">
        <div class="head"><h3>Bici actual</h3><span style="color:#f59e0b">${bici.nombre||'Rarotonga'}</span></div>
        <div class="row"><div class="r-main"><div class="r-title">${bici.marca||'Orbea Avant Aluminio 2019'}</div><div class="r-sub">${bici.llantas||'Vittoria Corsa N.EXT 700C x26'}</div><div class="r-sub">Primer uso: ${fmtDate(bici.primer_uso)}</div></div><div class="r-val" style="color:#f59e0b">${bici.km||716.6} km</div></div>
      </div>

      <!-- PLAN GARMIN -->
      <div class="card">
        <div class="head"><h3>Plan Garmin</h3><span style="color:#4a9eff">${plan.nombre||'Garmin Coach'}</span></div>
        <div style="padding:8px 0">
          <div style="font-size:13px;font-weight:800;margin-bottom:4px">${plan.fase||'Base aerobica'}</div>
          <div style="font-size:12px;color:var(--muted)">${plan.desc||''}</div>
        </div>
        <div style="font-size:12px;color:#4a9eff;font-weight:700">Cadencia objetivo: ${p.cadencia_obj||100} rpm</div>
      </div>

      <!-- EFICIENCIA -->
      <div class="card">
        <div class="head"><h3>Eficiencia aerobica</h3><span style="color:#3dd68c">${effPct}%</span></div>
        <div class="pbar"><div class="pfill" style="width:${effPct}%;background:#3dd68c"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px">
          <span>Actual: ${effActual.toFixed(4)}</span><span>Base: ${(p.eff_base||0.1483).toFixed(4)}</span><span>Obj: ${(p.eff_obj||0.155).toFixed(3)}+</span>
        </div>
      </div>

      <!-- RUTAS -->
      <div class="card">
        <div class="head"><h3>Rutas de referencia</h3></div>
        ${rutas.map(r=>'<div class="row"><div class="r-main"><div class="r-title">'+r.nombre+'</div><div class="r-sub">'+r.desc+'</div></div><div class="r-val" style="color:var(--theme)">'+r.km+' km</div></div>').join('')||'<div style="color:var(--muted);font-size:12px">Sin rutas definidas</div>'}
      </div>

      <!-- NUTRICION -->
      <div class="card">
        <div class="head"><h3>Estrategia de gel</h3><span style="color:#f59e0b">casero</span></div>
        <div style="font-size:13px;margin-bottom:8px">${nut.gel||'60% apple juice + 40% agave + sal'}</div>
        <div class="grid2">
          ${metric('Carbos',nut.carbos_g||40,'g / gel')}
          ${metric('Agua',nut.agua_ml_h||500,'ml/h')}
        </div>
        <div style="font-size:11px;color:var(--muted)">${nut.timing||'Cada 45-60 min'}</div>
      </div>

      <!-- OBJETIVOS -->
      <div class="card">
        <div class="head"><h3>Objetivos activos</h3></div>
        ${objetivos.map((o,i)=>'<div style="display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07)"><div style="width:20px;height:20px;border-radius:50%;background:rgba(61,214,140,.15);border:1px solid rgba(61,214,140,.3);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:900;color:#3dd68c;flex-shrink:0">'+(i+1)+'</div><div style="font-size:13px;flex:1">'+(o.o||o.objetivo||o)+'</div></div>').join('')}
      </div>
    `;
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}


async function loadCoach(){
  const el=$('coach-data');
  try{
    const [mp,d,w,wh]=await Promise.all([
      fetch(API+'/gpt/mars-context').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/dashboard').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/weight/history?limit=1').then(r=>r.json()).catch(()=>({}))
    ]);
    const z=mp.zonas_ciclismo||{};const _fallback=!mp.zonas_ciclismo,z2=z.z2||[134,150],z3=z.z3||[151,160];
    const plan=mp.plan_garmin||{},a=mp.athlete||{},bici=mp.bici||{};
    const nut=mp.nutricion||{},rutas=mp.rutas||[];
    const atleta=d.athlete||{};
    const pctZ2=parseFloat((d.z2_check||{}).pct_z2_4_semanas||0);
    const peso=wh.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const fatiga=atleta.fatiga||'—';
    const marsIndex=parseFloat(atleta.mars_index||0);
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const molestias=w.molestias_activas||[];
    const pains=molestias.length;
    let rec='',recCol='#4a9eff',recTitle='Seguir plan';
    if(pains>0){rec='Molestia(s): '+molestias.map(function(m){return m.pain_zone||'zona'}).join(', ')+'. Sesion Z1 max 108 bpm. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+'.';recCol='#e8593c';recTitle='Recuperar — molestias';}
    else if(fatiga==='alta'){rec='Fatiga alta. Compex '+(mp.compex?mp.compex.recovery[0]:'Active Recovery')+'. FC max 108 bpm, 30-40 min max.';recCol='#f59e0b';recTitle='Recuperar — fatiga';}
    else if(pctZ2<60){rec='Z2 en '+pctZ2.toFixed(0)+'% — bajo 70-80% objetivo. Salida Z2: '+(rutas[0]?rutas[0].nombre+' ~'+rutas[0].km+' km':'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm. Cadencia '+(mp.cadencia_obj||100)+' rpm. Gel '+(nut.gel||'agave casero')+' cada 45 min.';recCol='#3dd68c';recTitle='Construir Z2';}
    else if(effActual<(mp.eff_obj||0.155)*0.95){rec='Eficiencia '+effActual.toFixed(4)+' bajo obj '+(mp.eff_obj||0.155)+'. Sesion Z2 cadencia '+(mp.cadencia_obj||100)+' rpm, '+(rutas[0]?rutas[0].nombre:'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm.';recCol='#a78bfa';recTitle='Mejorar eficiencia';}
    else if(marsIndex>20){rec='Mars Index '+marsIndex.toFixed(1)+'. Agrega Tempo Z3 '+z3[0]+'–'+z3[1]+' bpm en los ultimos 20-30 min.';recCol='#a78bfa';recTitle='Subir intensidad';}
    else{rec='Continua '+( plan.nombre||'Garmin Coach')+' fase '+(plan.fase||'Base')+'. '+(rutas[0]?rutas[0].nombre+' ~'+rutas[0].km+' km':'~21 km')+'. FC '+z2[0]+'–'+z2[1]+' bpm, cadencia '+(mp.cadencia_obj||100)+' rpm.';recCol='#4a9eff';recTitle='Seguir plan';}
    el.innerHTML=
      '<div class="grid2">'+metric('Mars Index',marsIndex.toFixed(1),'fitness '+(atleta.fitness||'—'))+metric('Fatiga',fatiga,pctZ2.toFixed(0)+'% Z2')+metric('Peso',peso+' kg','→ '+pesoObj+' kg')+metric('Efic.',effActual.toFixed(4),'obj '+(mp.eff_obj||0.155)+'+')+
      '</div>'+
      '<div class="card" style="border-left:3px solid '+recCol+'"><div class="head"><h3 style="color:'+recCol+'">'+recTitle+'</h3><span style="color:var(--muted)">'+(plan.fase||'Base')+'</span></div><div style="font-size:13px;line-height:1.65;margin-top:6px">'+rec+'</div></div>'+
      (pains?'<div class="card" style="border-color:rgba(232,89,60,.3)"><div class="head"><h3 style="color:#e8593c">Molestias</h3><span>'+pains+'</span></div>'+molestias.map(function(m){return'<div class="row"><div class="r-main"><div class="r-title">'+(m.pain_zone||'—')+'</div></div><div class="r-val" style="color:#e8593c">Nivel '+(m.pain_level||'?')+'/10</div></div>';}).join('')+'</div>':'')+
      '<div class="card"><div class="head"><h3>Zonas hoy</h3></div>'+
        [['Z2 Aerobico',z2[0]+'–'+z2[1],'#3dd68c'],['Z3 Tempo',z3[0]+'-'+z3[1],'#f59e0b'],['Z4 Umbral',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:12px">'+zz[0]+'</span><span style="font-size:12px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
        '<div style="padding:7px 0;font-size:12px;color:#f59e0b;font-weight:800">Cadencia: '+(mp.cadencia_obj||100)+' rpm</div>'+
      '</div>'+
      '<div class="card"><div class="head"><h3>Gel y nutricion</h3></div><div style="font-size:13px;margin-bottom:4px">'+(nut.gel||'60% apple juice + 40% agave + sal')+'</div><div style="font-size:11px;color:var(--muted)">'+(nut.timing||'Cada 45-60 min')+' · '+(nut.agua_ml_h||500)+' ml/h</div></div>'+
      '<div class="card"><div class="head"><h3>Bici</h3><span style="color:#f59e0b">'+(bici.nombre||'Rarotonga')+'</span></div><div style="font-size:12px;color:var(--muted)">'+(bici.marca||'Orbea Avant 2019')+' · '+(bici.km||716)+' km</div></div>';
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}



async function loadProgress(){
  const el=$('progress-data');
  try{
    const [mp,tr,perf,wh]=await Promise.all([
      fetch(API+'/gpt/mars-context').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/trends?weeks=8').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/weight/history?limit=10').then(r=>r.json()).catch(()=>({}))
    ]);
    const a=mp.athlete||{},z=mp.zonas_ciclismo||{},plan=mp.plan_garmin||{};
    const z2=z.z2||[134,150];
    const peso=wh.current_kg||a.peso_actual_kg||89.1;
    const pesoObj=a.peso_objetivo_kg||80;
    const pesoDiff=+(peso-pesoObj).toFixed(1);
    const pesoPct=Math.max(0,Math.min(100,Math.round(100-(pesoDiff/20)*100)));
    const wvals=(wh.historial||[]).slice(-8).map(function(e){return e.weight_kg}).filter(Boolean);
    const effActual=mp.eff_actual||mp.eff_base||0.1483;
    const effObj=mp.eff_obj||0.155;
    const effPct=Math.min(100,Math.round((effActual/effObj)*100));
    const recs=(perf.records)||{};
    const weeks=tr.weeks||tr.tendencia||[];
    const kmVals=weeks.map(function(w){return parseFloat(w.km||w.distance_km||0)}).filter(function(v){return v>0});
    const effVals=weeks.map(function(w){return parseFloat(w.efficiency||w.eficiencia||0)}).filter(function(v){return v>0});
    function spark(vals,col,h){
      if(!vals||vals.length<2)return '';
      const mx=Math.max(...vals),mn=Math.min(...vals),rng=mx-mn||0.001,W=100;
      const pts=vals.map(function(v,i){return Math.round(i/(vals.length-1)*W)+','+Math.round(h-(v-mn)/rng*(h-4)-2)}).join(' ');
      return '<svg viewBox="0 0 '+W+' '+h+'" style="width:100%;height:'+h+'px"><polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.8" stroke-linecap="round"/></svg>';
    }
    el.innerHTML=
      '<div class="card"><div class="head"><h3>Plan activo</h3><span style="color:#4a9eff">'+(plan.fase||'Base aerobica')+'</span></div><div style="font-size:13px">'+(plan.nombre||'Garmin Coach Time Trial')+'</div><div style="font-size:11px;color:var(--muted);margin-top:2px">'+(plan.desc||'')+'</div></div>'+
      '<div class="card"><div class="head"><h3>Peso</h3><span style="color:#3dd68c">'+peso+' kg</span></div><div class="pbar"><div class="pfill" style="width:'+pesoPct+'%;background:#3dd68c"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+peso+' kg</span><span>Obj: '+pesoObj+' kg · '+(pesoDiff>0?'faltan '+pesoDiff+' kg':'meta lograda')+'</span></div>'+spark(wvals,'#3dd68c',28)+'</div>'+
      '<div class="card"><div class="head"><h3>Eficiencia vel/FC</h3><span style="color:#a78bfa">'+effPct+'%</span></div><div class="pbar"><div class="pfill" style="width:'+effPct+'%;background:#a78bfa"></div></div><div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px;margin-bottom:6px"><span>Actual: '+effActual.toFixed(4)+'</span><span>Obj: '+effObj+'+</span></div>'+spark(effVals,'#a78bfa',28)+'</div>'+
      (kmVals.length>=2?'<div class="card"><div class="head"><h3>Km por semana</h3><span>'+(kmVals[kmVals.length-1]||0).toFixed(0)+' km</span></div>'+spark(kmVals,'#e8593c',36)+'<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px"><span>'+weeks.length+' semanas</span><span>Max: '+Math.max(...kmVals).toFixed(0)+' km</span><span>Prom: '+(kmVals.reduce(function(a,b){return a+b},0)/kmVals.length).toFixed(0)+' km</span></div></div>':'')+
      '<div class="card"><div class="head"><h3>Zonas ciclismo</h3><span style="color:#3dd68c">LT '+(z.lt_bpm||168)+' bpm</span></div>'+
        [['Z1','0–108','#8b929f'],['Trans','109–133','#f59e0b'],['Z2',z2[0]+'–'+z2[1],'#3dd68c'],['Z3',(z.z3?z.z3[0]:151)+'-'+(z.z3?z.z3[1]:160),'#f59e0b'],['Z4',(z.z4?z.z4[0]:161)+'-'+(z.z4?z.z4[1]:168),'#e8593c'],['Z5','169+','#c026d3']].map(function(zz){return'<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05)"><span style="font-size:11px">'+zz[0]+'</span><span style="font-size:11px;font-weight:800;color:'+zz[2]+'">'+zz[1]+' bpm</span></div>';}).join('')+
      '</div>'+
      (Object.keys(recs).length?'<div class="card"><div class="head"><h3>Records</h3></div>'+
        (recs.max_distance?'<div class="row"><div class="r-main"><div class="r-title">Mayor distancia</div><div class="r-sub">'+fmtDate((recs.max_distance||{}).date)+'</div></div><div class="r-val" style="color:#e8593c">'+Number(((recs.max_distance||{}).value)||0).toFixed(1)+' km</div></div>':'')+
        (recs.max_speed?'<div class="row"><div class="r-main"><div class="r-title">Mayor velocidad</div><div class="r-sub">'+fmtDate((recs.max_speed||{}).date)+'</div></div><div class="r-val" style="color:#a78bfa">'+Number(((recs.max_speed||{}).value)||0).toFixed(1)+' km/h</div></div>':'')+
      '</div>':'');
  }catch(e){el.innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

go('home',false);
</script>
</body>
</html>"""


def _full_app_response():
    return HTMLResponse(APP_FULL_HTML)

# Clean direct routes — no override needed
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
def serve_app():
    return HTMLResponse(APP_FULL_HTML)
