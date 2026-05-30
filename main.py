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
from pydantic import BaseModel
import tempfile, os, zipfile, math, statistics, uuid, json
from datetime import datetime, timezone

try:
    import fitparse
except ImportError:
    raise RuntimeError("pip install fitparse")

# ── Database setup ────────────────────────────────────────────────────────────
# Uses PostgreSQL (Supabase) when DATABASE_URL is set, otherwise in-memory dict
DATABASE_URL = os.environ.get("DATABASE_URL")

db_conn = None

def get_db():
    global db_conn
    if not DATABASE_URL:
        return None
    if db_conn is None:
        try:
            import psycopg2
            url = DATABASE_URL
            if '?' not in url:
                url += '?sslmode=require'
            db_conn = psycopg2.connect(url)
            db_conn.autocommit = True
            _init_db(db_conn)
            print("DB connected successfully")
        except Exception as e:
            print(f"DB connection failed: {e}")
            return None
    return db_conn

def _init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                filename     TEXT,
                uploaded_at  TIMESTAMPTZ,
                start_time   TEXT,
                sport        TEXT,
                distance_km  FLOAT,
                duration_s   INT,
                ascent_m     INT,
                avg_hr_bpm   INT,
                avg_speed_kmh FLOAT,
                avg_cadence  INT,
                workout_name TEXT,
                start_lat    FLOAT,
                start_lon    FLOAT,
                end_lat      FLOAT,
                end_lon      FLOAT,
                route_id     TEXT,
                result_json  TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                route_id     TEXT PRIMARY KEY,
                name         TEXT,
                distance_km  FLOAT,
                ascent_m     INT,
                created_at   TIMESTAMPTZ,
                sample_lat   FLOAT,
                sample_lon   FLOAT
            )
        """)

# In-memory fallback
RESULTS_STORE: dict = {}
RESULTS_STORE_MAX = 10  # Máximo de sesiones en memoria

def store_session(sid, data):
    """Guarda sesión en memoria limitando a RESULTS_STORE_MAX entradas."""
    RESULTS_STORE[sid] = data
    if len(RESULTS_STORE) > RESULTS_STORE_MAX:
        # Eliminar la entrada más antigua
        oldest = next(iter(RESULTS_STORE))
        del RESULTS_STORE[oldest]

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
        print(f"find_or_create_route error: {e}")
        return None


def save_session_db(conn, session_id, filename, result):
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
             workout_name, start_lat, start_lon, end_lat, end_lon, route_id, result_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            json.dumps({k: v for k, v in result.items() if k != "records"})
        ))


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

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

HTML_UPLOAD = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>FIT Uploader — Mars</title>
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
  const sid=data.session_id,s=data.session||{};
  $('sessionIdValue').textContent=sid;$('hintId').textContent=sid;
  $('chartsBtn').href='/charts/'+sid;
  const meta=[{key:'Fecha',val:(s.start_time||'').slice(0,10)},{key:'Distancia',val:s.distance_km?s.distance_km+' km':'—'},{key:'Duración',val:s.duration_hms||'—'},{key:'FC prom.',val:s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—'}];
  $('resultMeta').innerHTML=meta.map(m=>`<div class="meta-item"><div class="meta-key">${m.key}</div><div class="meta-val">${m.val}</div></div>`).join('');
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



@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bitácora — Mars</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #f5f3ef;
  --surface: #ffffff;
  --surface2: #f0ede8;
  --border: #e8e4de;
  --text: #1a1816;
  --muted: #9a9590;
  --accent: #e8593c;
  --accent2: #f2a623;
  --green: #1a9e6e;
  --blue: #2563eb;
  --red: #dc2626;
  --mono: 'Cabinet Grotesk', sans-serif;
  --serif: 'Instrument Serif', serif;
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 4px 12px rgba(0,0,0,.04);
  --shadow-lg: 0 4px 24px rgba(0,0,0,.08);
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: var(--mono); min-height: 100vh; }

/* ── NAV ── */
nav {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 28px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: var(--shadow);
}
.nav-logo { display: flex; align-items: center; gap: 10px; }
.nav-icon {
  width: 32px; height: 32px;
  background: var(--accent);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
}
.nav-icon svg { width: 18px; height: 18px; stroke: white; fill: none; stroke-width: 2; stroke-linecap: round; }
.nav-name { font-weight: 700; font-size: 16px; letter-spacing: -.02em; }
.nav-sub { font-size: 11px; color: var(--muted); }
.nav-right { display: flex; gap: 8px; align-items: center; }
.nav-stat { font-size: 12px; color: var(--muted); padding: 4px 12px; background: var(--surface2); border-radius: 20px; }
.nav-stat strong { color: var(--text); }

/* ── LAYOUT ── */
.layout { display: grid; grid-template-columns: 280px 1fr; min-height: calc(100vh - 56px); }

/* ── SIDEBAR ── */
aside {
  background: var(--surface);
  border-right: 1px solid var(--border);
  padding: 20px 0;
  position: sticky;
  top: 56px;
  height: calc(100vh - 56px);
  overflow-y: auto;
}
.sidebar-section { padding: 0 16px; margin-bottom: 20px; }
.sidebar-label { font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); font-weight: 500; padding: 0 8px; margin-bottom: 6px; }
.sport-btn {
  width: 100%; text-align: left; background: none; border: none;
  padding: 8px 12px; border-radius: 8px; cursor: pointer;
  font-family: var(--mono); font-size: 13px; font-weight: 500;
  color: var(--muted); display: flex; align-items: center; gap: 10px;
  transition: all .15s;
}
.sport-btn:hover { background: var(--surface2); color: var(--text); }
.sport-btn.active { background: var(--text); color: white; }
.sport-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.sport-count { margin-left: auto; font-size: 11px; opacity: .6; }

.sidebar-divider { height: 1px; background: var(--border); margin: 12px 16px; }

.filter-row { padding: 0 16px 4px; }
.filter-lbl { font-size: 11px; color: var(--muted); margin-bottom: 6px; display: block; }
.filter-input {
  width: 100%; padding: 7px 10px;
  border: 1px solid var(--border); border-radius: 8px;
  font-family: var(--mono); font-size: 12px; color: var(--text);
  background: var(--surface2); outline: none;
  transition: border-color .15s;
}
.filter-input:focus { border-color: var(--accent); }

.sort-btns { display: flex; flex-direction: column; gap: 2px; padding: 0 16px; }
.sort-btn {
  width: 100%; text-align: left; background: none; border: none;
  padding: 7px 12px; border-radius: 8px; cursor: pointer;
  font-family: var(--mono); font-size: 12px; color: var(--muted);
  transition: all .15s;
}
.sort-btn:hover { background: var(--surface2); color: var(--text); }
.sort-btn.active { color: var(--accent); font-weight: 700; }

/* ── MAIN ── */
.main-content { padding: 28px; overflow-y: auto; }

/* ── HERO STATS ── */
.hero-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 14px; margin-bottom: 28px; }
.hero-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 18px 20px;
  box-shadow: var(--shadow);
}
.hero-card:first-child { border-left: 3px solid var(--accent); }
.hero-card:nth-child(2) { border-left: 3px solid var(--accent2); }
.hero-card:nth-child(3) { border-left: 3px solid var(--green); }
.hero-card:nth-child(4) { border-left: 3px solid var(--blue); }
.hero-label { font-size: 10px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); font-weight: 500; margin-bottom: 8px; }
.hero-value { font-family: var(--serif); font-size: 36px; letter-spacing: -.03em; line-height: 1; }
.hero-unit { font-size: 11px; color: var(--muted); margin-top: 2px; }

/* ── SECTION HEADER ── */
.section-hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.section-title { font-size: 13px; font-weight: 700; letter-spacing: -.01em; }
.section-meta { font-size: 11px; color: var(--muted); }
.btn-refresh { background: none; border: 1px solid var(--border); border-radius: 8px; padding: 5px 12px; font-family: var(--mono); font-size: 11px; color: var(--muted); cursor: pointer; transition: all .15s; }
.btn-refresh:hover { border-color: var(--accent); color: var(--accent); }

/* ── ROUTE CARDS ── */
.routes-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px,1fr)); gap: 14px; }

.route-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  cursor: pointer;
  transition: transform .15s, box-shadow .15s, border-color .15s;
  box-shadow: var(--shadow);
}
.route-card:hover { transform: translateY(-2px); box-shadow: var(--shadow-lg); border-color: #d4d0ca; }
.route-card.active { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(232,89,60,.12); }

.rc-banner {
  height: 80px;
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: flex-end;
  padding: 12px 16px;
}
.rc-banner-bg {
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, #1a1816 0%, #3a3330 100%);
}
.rc-banner-pattern {
  position: absolute;
  inset: 0;
  opacity: .15;
  background-image: repeating-linear-gradient(45deg, transparent, transparent 4px, rgba(255,255,255,.1) 4px, rgba(255,255,255,.1) 5px);
}
.rc-sport-tag {
  position: relative;
  z-index: 1;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  padding: 3px 8px;
  border-radius: 4px;
  color: white;
}
.rc-times {
  position: relative;
  z-index: 1;
  margin-left: auto;
  font-family: var(--serif);
  font-style: italic;
  font-size: 28px;
  color: white;
  line-height: 1;
}
.rc-times span { font-size: 11px; font-family: var(--mono); font-style: normal; opacity: .6; }

.rc-body { padding: 14px 16px; }
.rc-name { font-family: var(--serif); font-style: italic; font-size: 17px; margin-bottom: 12px; line-height: 1.2; }
.rc-metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 12px; }
.rc-metric-label { font-size: 9px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 2px; }
.rc-metric-value { font-size: 15px; font-weight: 700; }
.rc-metric-unit { font-size: 9px; color: var(--muted); }
.rc-footer { display: flex; gap: 6px; align-items: center; padding-top: 10px; border-top: 1px solid var(--border); }
.rc-date { font-size: 10px; color: var(--muted); }
.rc-arrow { margin-left: auto; font-size: 12px; color: var(--muted); }

/* ── DETAIL PANEL ── */
.detail-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  margin-bottom: 28px;
  overflow: hidden;
  display: none;
  box-shadow: var(--shadow-lg);
}
.detail-panel.show { display: block; }

.dp-hero {
  background: linear-gradient(135deg, #1a1816 0%, #2d2926 100%);
  padding: 28px;
  color: white;
  position: relative;
  overflow: hidden;
}
.dp-hero::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image: repeating-linear-gradient(45deg, transparent, transparent 8px, rgba(255,255,255,.03) 8px, rgba(255,255,255,.03) 9px);
}
.dp-hero-content { position: relative; z-index: 1; }
.dp-name { font-family: var(--serif); font-style: italic; font-size: 28px; margin-bottom: 16px; }
.dp-stats-row { display: flex; gap: 28px; flex-wrap: wrap; }
.dp-stat-label { font-size: 9px; text-transform: uppercase; letter-spacing: .15em; opacity: .5; margin-bottom: 4px; }
.dp-stat-value { font-size: 22px; font-weight: 700; line-height: 1; }
.dp-stat-unit { font-size: 10px; opacity: .6; }
.dp-close-btn {
  position: absolute; top: 20px; right: 20px; z-index: 2;
  background: rgba(255,255,255,.1); border: 1px solid rgba(255,255,255,.2);
  border-radius: 8px; color: white; font-family: var(--mono); font-size: 11px;
  padding: 6px 12px; cursor: pointer; transition: background .15s;
}
.dp-close-btn:hover { background: rgba(255,255,255,.2); }

.dp-body { padding: 24px 28px; }

/* progress */
.progress-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 28px; }
.prog-card { background: var(--surface2); border-radius: 12px; padding: 16px; }
.prog-card-label { font-size: 10px; text-transform: uppercase; letter-spacing: .12em; color: var(--muted); font-weight: 500; margin-bottom: 12px; }
.prog-change { font-size: 24px; font-weight: 900; letter-spacing: -.03em; line-height: 1; margin-bottom: 4px; }
.prog-change.up { color: var(--green); }
.prog-change.down { color: var(--red); }
.prog-change.flat { color: var(--muted); }
.prog-note { font-size: 11px; color: var(--muted); line-height: 1.4; margin-bottom: 12px; }
.prog-bar-bg { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
.prog-bar-fill { height: 100%; border-radius: 3px; transition: width 1.2s cubic-bezier(.4,0,.2,1); width: 0; }

/* chart */
.chart-section { margin-bottom: 28px; }
.chart-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 12px; }
.chart-wrap { background: var(--surface2); border-radius: 12px; padding: 16px; height: 160px; }

/* rides table */
.rides-section { }
.rides-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: 12px; }
.rides-table { width: 100%; border-collapse: collapse; }
.rides-table th { font-size: 10px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); font-weight: 500; text-align: left; padding: 0 10px 10px 0; border-bottom: 2px solid var(--border); }
.rides-table td { padding: 10px 10px 10px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.rides-table tr:last-child td { border-bottom: none; }
.rides-table tr:hover td { background: var(--surface2); }
.td-badge { display: inline-block; font-size: 9px; padding: 2px 7px; border-radius: 4px; font-weight: 700; margin-left: 6px; }
.badge-first { background: #fef3c7; color: #92400e; }
.badge-last { background: #dcfce7; color: #166534; }

/* sport colors */
.sport-cycling { background: rgba(232,89,60,.15); color: var(--accent); }
.sport-running { background: rgba(26,158,110,.15); color: var(--green); }
.sport-walking { background: rgba(37,99,235,.15); color: var(--blue); }
.sport-strength,.sport-strength_training { background: rgba(242,166,35,.15); color: #b45309; }
.sport-yoga { background: rgba(168,85,247,.15); color: #7c3aed; }
.sport-default { background: rgba(154,149,144,.15); color: var(--muted); }

.sport-dot-cycling { background: var(--accent); }
.sport-dot-running { background: var(--green); }
.sport-dot-walking { background: var(--blue); }
.sport-dot-strength_training { background: var(--accent2); }
.sport-dot-yoga { background: #a855f7; }
.sport-dot-default { background: var(--muted); }

/* loading */
.loading { text-align: center; padding: 60px; color: var(--muted); }
.spinner { width: 24px; height: 24px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; margin: 0 auto 14px; }
@keyframes spin { to { transform: rotate(360deg); } }
.empty { text-align: center; padding: 60px; color: var(--muted); font-size: 13px; }

@media(max-width:900px) {
  .layout { grid-template-columns: 1fr; }
  aside { position: static; height: auto; border-right: none; border-bottom: 1px solid var(--border); padding: 12px 0; }
  .sidebar-section { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px; }
  .sport-btn { width: auto; padding: 5px 12px; font-size: 11px; }
  .hero-grid { grid-template-columns: 1fr 1fr; }
  .progress-grid { grid-template-columns: 1fr; }
  .dp-stats-row { gap: 16px; }
}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <div class="nav-icon">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 0 20"/><path d="M8 12h8"/><path d="M12 8v8"/></svg>
    </div>
    <div>
      <div class="nav-name">Bitácora</div>
      <div class="nav-sub">Mars Training Log</div>
    </div>
  </div>
  <div class="nav-right">
    <div class="nav-stat" id="navSess">— <strong>sesiones</strong></div>
    <div class="nav-stat" id="navRoutes">— <strong>rutas</strong></div>
  </div>
</nav>

<div class="layout">

  <!-- SIDEBAR -->
  <aside>
    <div class="sidebar-section">
      <div class="sidebar-label">Deporte</div>
      <button class="sport-btn active" id="btnAll" onclick="setSport('all',this)">
        <span class="sport-dot" style="background:var(--text)"></span>
        Todos
        <span class="sport-count" id="cntAll">—</span>
      </button>
      <div id="sportBtns"></div>
    </div>

    <div class="sidebar-divider"></div>

    <div class="filter-row">
      <label class="filter-lbl">Distancia mínima</label>
      <input class="filter-input" type="number" id="minDist" value="0" min="0" placeholder="0 km" onchange="renderRoutes()">
    </div>

    <div class="sidebar-divider"></div>

    <div class="sidebar-section" style="padding-bottom:4px">
      <div class="sidebar-label">Ordenar por</div>
    </div>
    <div class="sort-btns">
      <button class="sort-btn active" id="sortTimes" onclick="setSort('times',this)">↓ Más veces</button>
      <button class="sort-btn" id="sortDist" onclick="setSort('distance',this)">↓ Mayor distancia</button>
      <button class="sort-btn" id="sortRecent" onclick="setSort('recent',this)">↓ Más reciente</button>
    </div>
  </aside>

  <!-- MAIN -->
  <div class="main-content">

    <div class="hero-grid">
      <div class="hero-card"><div class="hero-label">Rutas</div><div class="hero-value" id="stRoutes">—</div><div class="hero-unit">identificadas</div></div>
      <div class="hero-card"><div class="hero-label">Sesiones</div><div class="hero-value" id="stSess">—</div><div class="hero-unit">actividades</div></div>
      <div class="hero-card"><div class="hero-label">FC promedio</div><div class="hero-value" id="stHR">—</div><div class="hero-unit">bpm global</div></div>
      <div class="hero-card"><div class="hero-label">Vel. promedio</div><div class="hero-value" id="stSpd">—</div><div class="hero-unit">km/h global</div></div>
    </div>

    <!-- Detail panel -->
    <div class="detail-panel" id="detailPanel">
      <div class="dp-hero" id="dpHero">
        <button class="dp-close-btn" onclick="closeDetail()">✕ Cerrar</button>
        <div class="dp-hero-content">
          <div class="dp-name" id="dpName">—</div>
          <div class="dp-stats-row" id="dpStats"></div>
        </div>
      </div>
      <div class="dp-body" id="dpBody"></div>
    </div>

    <div class="section-hdr">
      <div class="section-title">Rutas <span class="section-meta" id="routeCount"></span></div>
      <button class="btn-refresh" onclick="loadRoutes()">↻ Actualizar</button>
    </div>
    <div id="routesContainer"><div class="loading"><div class="spinner"></div>Cargando...</div></div>

  </div>
</div>

<script>
const API='https://mars-fit-analyzer-production.up.railway.app';
let allRoutes=[], activeSport='all', activeSort='times';
const $=id=>document.getElementById(id);

const SPORT_LABELS={'cycling':'Ciclismo','running':'Running','walking':'Caminata','strength_training':'Fuerza','yoga':'Yoga','trail_running':'Trail','indoor_cardio':'Cardio','lap_swimming':'Natación','other':'Otro'};
const sportLabel=s=>SPORT_LABELS[s]||(s?s.charAt(0).toUpperCase()+s.slice(1):'—');
const sportDotClass=s=>`sport-dot-${['cycling','running','walking','strength_training','yoga'].includes(s)?s:'default'}`;
const sportTagClass=s=>`sport-${['cycling','running','walking','strength_training','yoga'].includes(s)?s:'default'}`;

function setSport(sp,btn){
  activeSport=sp;
  document.querySelectorAll('.sport-btn').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderRoutes();
}

function setSort(val,btn){
  activeSort=val;
  document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderRoutes();
}

async function loadRoutes(){
  $('routesContainer').innerHTML='<div class="loading"><div class="spinner"></div>Cargando rutas...</div>';
  try{
    allRoutes=await fetch(`${API}/routes`).then(r=>r.json());

    // Build sidebar sport buttons
    const sportCounts={};
    allRoutes.forEach(r=>{ if(r.sport) sportCounts[r.sport]=(sportCounts[r.sport]||0)+(r.times_ridden||1); });
    $('cntAll').textContent=allRoutes.length;

    const sbContainer=$('sportBtns');
    sbContainer.innerHTML='';
    Object.entries(sportCounts).sort((a,b)=>b[1]-a[1]).forEach(([sp,cnt])=>{
      const btn=document.createElement('button');
      btn.className='sport-btn';
      btn.innerHTML=`<span class="sport-dot ${sportDotClass(sp)}"></span>${sportLabel(sp)}<span class="sport-count">${allRoutes.filter(r=>r.sport===sp).length}</span>`;
      btn.onclick=()=>setSport(sp,btn);
      sbContainer.appendChild(btn);
    });

    // Hero
    const total=allRoutes.reduce((a,r)=>a+(r.times_ridden||0),0);
    const hrs=allRoutes.filter(r=>r.avg_hr_bpm);
    const spds=allRoutes.filter(r=>r.avg_speed_kmh&&r.avg_speed_kmh>2);
    const avgHR=hrs.length?Math.round(hrs.reduce((a,r)=>a+r.avg_hr_bpm,0)/hrs.length):0;
    const avgSpd=spds.length?(spds.reduce((a,r)=>a+r.avg_speed_kmh,0)/spds.length).toFixed(1):0;

    $('stRoutes').textContent=allRoutes.length;
    $('stSess').textContent=total;
    $('stHR').textContent=avgHR||'—';
    $('stSpd').textContent=avgSpd||'—';
    $('navSess').innerHTML=`${total} <strong>sesiones</strong>`;
    $('navRoutes').innerHTML=`${allRoutes.length} <strong>rutas</strong>`;

    renderRoutes();
  }catch(e){
    $('routesContainer').innerHTML=`<div class="empty">Error cargando datos: ${e.message}</div>`;
  }
}

function renderRoutes(){
  const minDist=parseFloat($('minDist').value)||0;
  let filtered=allRoutes.filter(r=>{
    if(activeSport!=='all'&&r.sport!==activeSport) return false;
    if((r.distance_km||0)<minDist) return false;
    return true;
  });

  if(activeSort==='times') filtered.sort((a,b)=>(b.times_ridden||0)-(a.times_ridden||0));
  else if(activeSort==='distance') filtered.sort((a,b)=>(b.distance_km||0)-(a.distance_km||0));
  else filtered.sort((a,b)=>(b.last_ride||'').localeCompare(a.last_ride||''));

  $('routeCount').textContent=`(${filtered.length})`;

  if(!filtered.length){
    $('routesContainer').innerHTML='<div class="empty">No hay rutas con estos filtros.</div>';
    return;
  }

  const grid=document.createElement('div');
  grid.className='routes-grid';

  // Banner colors per sport
  const bannerColors={'cycling':'linear-gradient(135deg,#1a0f0a 0%,#3d1a10 100%)','running':'linear-gradient(135deg,#0a1a12 0%,#103d25 100%)','walking':'linear-gradient(135deg,#0a0f1a 0%,#10203d 100%)','strength_training':'linear-gradient(135deg,#1a150a 0%,#3d2e10 100%)','yoga':'linear-gradient(135deg,#140a1a 0%,#2e103d 100%)'};

  filtered.forEach(r=>{
    const card=document.createElement('div');
    card.className='route-card';
    card.setAttribute('data-id',r.route_id);
    card.onclick=()=>loadDetail(r.route_id,r.name,r.sport);

    const bg=bannerColors[r.sport]||'linear-gradient(135deg,#1a1816 0%,#2d2926 100%)';
    card.innerHTML=`
      <div class="rc-banner" style="background:${bg}">
        <div class="rc-banner-pattern"></div>
        <span class="rc-sport-tag ${sportTagClass(r.sport)}">${sportLabel(r.sport)}</span>
        <div class="rc-times">${r.times_ridden||0}<span> veces</span></div>
      </div>
      <div class="rc-body">
        <div class="rc-name">${r.name||'Ruta sin nombre'}</div>
        <div class="rc-metrics">
          <div><div class="rc-metric-label">Distancia</div><div class="rc-metric-value">${(r.distance_km||0).toFixed(1)}<span class="rc-metric-unit"> km</span></div></div>
          <div><div class="rc-metric-label">FC prom.</div><div class="rc-metric-value">${r.avg_hr_bpm||'—'}<span class="rc-metric-unit"> bpm</span></div></div>
          <div><div class="rc-metric-label">Vel. prom.</div><div class="rc-metric-value">${r.avg_speed_kmh&&r.avg_speed_kmh>0?(+r.avg_speed_kmh).toFixed(1):'—'}<span class="rc-metric-unit"> km/h</span></div></div>
        </div>
        <div class="rc-footer">
          <span class="rc-date">${r.first_ride||'—'} → ${r.last_ride||'—'}</span>
          <span class="rc-arrow">→</span>
        </div>
      </div>`;
    grid.appendChild(card);
  });

  $('routesContainer').innerHTML='';
  $('routesContainer').appendChild(grid);
}

let activeChart=null;

async function loadDetail(id,name,sport){
  document.querySelectorAll('.route-card').forEach(c=>c.classList.remove('active'));
  const card=document.querySelector(`[data-id="${id}"]`);
  if(card){card.classList.add('active');card.scrollIntoView({behavior:'smooth',block:'nearest'});}

  const bannerColors={'cycling':'linear-gradient(135deg,#1a0f0a 0%,#3d1a10 100%)','running':'linear-gradient(135deg,#0a1a12 0%,#103d25 100%)','walking':'linear-gradient(135deg,#0a0f1a 0%,#10203d 100%)','strength_training':'linear-gradient(135deg,#1a150a 0%,#3d2e10 100%)','yoga':'linear-gradient(135deg,#140a1a 0%,#2e103d 100%)'};
  $('dpHero').style.background=bannerColors[sport]||'linear-gradient(135deg,#1a1816 0%,#2d2926 100%)';
  $('dpName').textContent=name||'Ruta';
  $('dpStats').innerHTML='<div style="opacity:.4;font-size:12px">Cargando...</div>';
  $('dpBody').innerHTML='<div class="loading"><div class="spinner"></div>Cargando historial...</div>';
  $('detailPanel').classList.add('show');
  $('detailPanel').scrollIntoView({behavior:'smooth',block:'start'});

  if(activeChart){activeChart.destroy();activeChart=null;}

  try{
    const d=await fetch(`${API}/route/${id}`).then(r=>r.json());
    const rides=d.rides||[], p=d.progress||{};

    $('dpStats').innerHTML=`
      <div><div class="dp-stat-label">Distancia</div><div class="dp-stat-value">${(d.distance_km||0).toFixed(1)} <span class="dp-stat-unit">km</span></div></div>
      <div><div class="dp-stat-label">Ascenso</div><div class="dp-stat-value">+${d.ascent_m||'—'} <span class="dp-stat-unit">m</span></div></div>
      <div><div class="dp-stat-label">Ejecuciones</div><div class="dp-stat-value">${d.times_ridden||0} <span class="dp-stat-unit">veces</span></div></div>
    `;

    let html='';

    // Progress cards
    if(p.hr_note||p.speed_note){
      html+=`<div class="progress-grid">`;
      if(p.speed_note){
        const up=p.speed_change_kmh>0;
        html+=`<div class="prog-card">
          <div class="prog-card-label">Velocidad promedio</div>
          <div class="prog-change ${up?'up':'down'}">${up?'+':''}${(p.speed_change_kmh||0).toFixed(1)} km/h</div>
          <div class="prog-note">${p.speed_note}</div>
          <div class="prog-bar-bg"><div class="prog-bar-fill" style="background:${up?'var(--green)':'var(--red)'};width:${Math.min(Math.abs(p.speed_change_kmh||0)*20,100)}%"></div></div>
        </div>`;
      }
      if(p.hr_note){
        const imp=p.hr_change_bpm<0;
        html+=`<div class="prog-card">
          <div class="prog-card-label">Frecuencia cardíaca</div>
          <div class="prog-change ${imp?'up':'down'}">${p.hr_change_bpm>0?'+':''}${p.hr_change_bpm||0} bpm</div>
          <div class="prog-note">${p.hr_note}</div>
          <div class="prog-bar-bg"><div class="prog-bar-fill" style="background:${imp?'var(--green)':'var(--red)'};width:${Math.min(Math.abs(p.hr_change_bpm||0)*5,100)}%"></div></div>
        </div>`;
      }
      html+=`</div>`;
    }

    // Chart FC over time
    if(rides.length>2){
      html+=`<div class="chart-section">
        <div class="chart-title">FC promedio por ejecución</div>
        <div class="chart-wrap"><canvas id="chartFC"></canvas></div>
      </div>`;
    }

    // Table
    if(rides.length){
      html+=`<div class="rides-section">
        <div class="rides-title">Historial de ejecuciones</div>
        <table class="rides-table">
          <thead><tr><th>Fecha</th><th>Entrenamiento</th><th>FC avg</th><th>Vel avg</th><th>Cadencia</th></tr></thead>
          <tbody>
          ${rides.map((r,i)=>`<tr>
            <td>${r.date||'—'}${i===0?'<span class="td-badge badge-first">Primera</span>':''}${i===rides.length-1?'<span class="td-badge badge-last">Última</span>':''}</td>
            <td style="color:var(--muted);font-size:11px">${(r.workout_name||'—').slice(0,30)}</td>
            <td><strong>${r.avg_hr_bpm||'—'}</strong> <span style="color:var(--muted);font-size:10px">bpm</span></td>
            <td><strong>${r.avg_speed_kmh?(+r.avg_speed_kmh).toFixed(1):'—'}</strong> <span style="color:var(--muted);font-size:10px">km/h</span></td>
            <td><strong>${r.avg_cadence||'—'}</strong> <span style="color:var(--muted);font-size:10px">rpm</span></td>
          </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
    }

    $('dpBody').innerHTML=html;

    // Animate bars
    setTimeout(()=>{
      document.querySelectorAll('.prog-bar-fill').forEach(b=>{
        const w=b.style.width;b.style.width='0';
        setTimeout(()=>b.style.width=w,50);
      });
    },100);

    // Render chart
    if(rides.length>2){
      const labels=rides.map(r=>r.date||'').map(d=>d.slice(5));
      const hrData=rides.map(r=>r.avg_hr_bpm||null);
      const spdData=rides.map(r=>r.avg_speed_kmh>0?r.avg_speed_kmh:null);

      activeChart=new Chart(document.getElementById('chartFC'),{
        type:'line',
        data:{
          labels,
          datasets:[
            {label:'FC avg',data:hrData,borderColor:'#e8593c',backgroundColor:'rgba(232,89,60,.08)',borderWidth:2,pointRadius:4,pointBackgroundColor:'#e8593c',tension:.3,yAxisID:'y'},
            {label:'Vel avg',data:spdData,borderColor:'#2563eb',backgroundColor:'rgba(37,99,235,.06)',borderWidth:2,pointRadius:4,pointBackgroundColor:'#2563eb',tension:.3,yAxisID:'y2'},
          ]
        },
        options:{
          responsive:true,maintainAspectRatio:false,
          interaction:{mode:'index',intersect:false},
          plugins:{legend:{display:true,labels:{font:{family:"'Cabinet Grotesk'",size:11},usePointStyle:true,boxWidth:8}}},
          scales:{
            x:{ticks:{font:{family:"'Cabinet Grotesk'",size:10},color:'#9a9590'},grid:{color:'rgba(0,0,0,.04)'}},
            y:{position:'left',ticks:{font:{family:"'Cabinet Grotesk'",size:10},color:'#e8593c'},grid:{color:'rgba(0,0,0,.04)'},title:{display:true,text:'bpm',font:{size:9},color:'#e8593c'}},
            y2:{position:'right',ticks:{font:{family:"'Cabinet Grotesk'",size:10},color:'#2563eb'},grid:{display:false},title:{display:true,text:'km/h',font:{size:9},color:'#2563eb'}},
          }
        }
      });
    }

  }catch(e){
    $('dpBody').innerHTML=`<div class="empty">Error: ${e.message}</div>`;
  }
}

function closeDetail(){
  $('detailPanel').classList.remove('show');
  document.querySelectorAll('.route-card').forEach(c=>c.classList.remove('active'));
  if(activeChart){activeChart.destroy();activeChart=null;}
}

loadRoutes();
</script>
</body>
</html>
"""




@app.get("/home", response_class=HTMLResponse)
def home_page():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bitácora — Home</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:#f5f3ef; --surface:#fff; --surface2:#f0ede8; --border:#e8e4de;
  --text:#1a1816; --muted:#9a9590; --accent:#e8593c; --accent2:#f2a623;
  --green:#1a9e6e; --blue:#2563eb; --mono:'Cabinet Grotesk',sans-serif;
  --serif:'Instrument Serif',serif; --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
  --shadow-lg:0 4px 24px rgba(0,0,0,.08);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}

/* NAV */
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}

/* LAYOUT */
.page{max-width:1100px;margin:0 auto;padding:28px}

/* PLAN BANNER */
.plan-banner{background:linear-gradient(135deg,#1a1816 0%,#2d2926 100%);border-radius:16px;padding:24px 28px;
  color:white;margin-bottom:24px;position:relative;overflow:hidden}
.plan-banner::before{content:'';position:absolute;inset:0;
  background-image:repeating-linear-gradient(45deg,transparent,transparent 8px,rgba(255,255,255,.03) 8px,rgba(255,255,255,.03) 9px)}
.plan-inner{position:relative;z-index:1;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
.plan-left{}
.plan-tag{font-size:9px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
  background:rgba(232,89,60,.3);color:#ff9980;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:10px}
.plan-name{font-family:var(--serif);font-style:italic;font-size:22px;margin-bottom:6px}
.plan-meta{font-size:12px;opacity:.6}
.plan-right{display:flex;align-items:center;gap:20px}
.plan-progress{text-align:right}
.plan-weeks{font-family:var(--serif);font-size:48px;line-height:1;letter-spacing:-.03em}
.plan-weeks span{font-size:16px;opacity:.5;font-family:var(--mono);font-style:normal}
.plan-bar-wrap{width:200px}
.plan-bar-label{font-size:10px;opacity:.5;margin-bottom:6px;text-align:right}
.plan-bar-bg{height:6px;background:rgba(255,255,255,.15);border-radius:3px;overflow:hidden}
.plan-bar-fill{height:100%;background:var(--accent);border-radius:3px;transition:width 1s ease}

/* STATS GRID */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px;box-shadow:var(--shadow)}
.stat-card:nth-child(1){border-left:3px solid var(--accent)}
.stat-card:nth-child(2){border-left:3px solid var(--accent2)}
.stat-card:nth-child(3){border-left:3px solid var(--green)}
.stat-card:nth-child(4){border-left:3px solid var(--blue)}
.stat-label{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:500;margin-bottom:8px}
.stat-value{font-family:var(--serif);font-size:36px;letter-spacing:-.03em;line-height:1}
.stat-unit{font-size:11px;color:var(--muted);margin-top:2px}

/* TWO COL */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}

/* CARD */
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}

/* EFICIENCIA CHART */
.chart-wrap{height:160px;position:relative}

/* ULTIMA ACTIVIDAD */
.act-hero{background:linear-gradient(135deg,#1a1816,#2d2926);border-radius:10px;padding:16px;color:white;margin-bottom:14px;position:relative;overflow:hidden}
.act-hero::before{content:'';position:absolute;inset:0;background-image:repeating-linear-gradient(45deg,transparent,transparent 6px,rgba(255,255,255,.03) 6px,rgba(255,255,255,.03) 7px)}
.act-inner{position:relative;z-index:1}
.act-sport{font-size:9px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
  background:rgba(232,89,60,.3);color:#ff9980;padding:3px 8px;border-radius:4px;display:inline-block;margin-bottom:8px}
.act-name{font-family:var(--serif);font-style:italic;font-size:18px;margin-bottom:12px;line-height:1.2}
.act-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.act-metric-label{font-size:9px;text-transform:uppercase;letter-spacing:.1em;opacity:.5;margin-bottom:3px}
.act-metric-value{font-size:16px;font-weight:700}
.act-metric-unit{font-size:9px;opacity:.5}
.act-date{font-size:11px;opacity:.5;margin-top:10px}
.act-footer{display:flex;justify-content:space-between;align-items:center}
.act-link{font-size:11px;color:var(--accent);text-decoration:none;font-weight:500}
.act-link:hover{text-decoration:underline}

/* BY SPORT */
.sport-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.sport-row:last-child{border-bottom:none}
.sport-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sport-name{font-size:13px;flex:1}
.sport-count{font-size:12px;color:var(--muted)}
.sport-km{font-size:12px;font-weight:600;min-width:60px;text-align:right}

/* SKELETON */
.skel{background:linear-gradient(90deg,var(--surface2) 25%,var(--border) 50%,var(--surface2) 75%);
  background-size:200% 100%;animation:shimmer 1.2s infinite;border-radius:6px}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

@media(max-width:800px){
  .stats-grid{grid-template-columns:1fr 1fr}
  .two-col{grid-template-columns:1fr}
  .plan-right{width:100%}
  .plan-bar-wrap{flex:1}
}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <div class="nav-icon">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 0 20"/><path d="M8 12h8"/><path d="M12 8v8"/></svg>
    </div>
    <div><div class="nav-name">Bitácora</div><div class="nav-sub">Mars Training Log</div></div>
  </div>
  <div class="nav-links">
    <a href="/home" class="nav-link active">Home</a>
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/dashboard" class="nav-link">Rutas</a>
  </div>
</nav>

<div class="page">

  <!-- PLAN BANNER -->
  <div class="plan-banner">
    <div class="plan-inner">
      <div class="plan-left">
        <div class="plan-tag">Plan activo</div>
        <div class="plan-name">Time Trial Plan — Garmin Coach</div>
        <div class="plan-meta">Phase 1 Base · Semana actual</div>
      </div>
      <div class="plan-right">
        <div class="plan-progress">
          <div class="plan-weeks"><span>Semana </span><span id="planWeek">4</span><span> / 22</span></div>
        </div>
        <div class="plan-bar-wrap">
          <div class="plan-bar-label" id="planPct">—% completado</div>
          <div class="plan-bar-bg"><div class="plan-bar-fill" id="planBar" style="width:0%"></div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- STATS MES -->
  <div class="stats-grid">
    <div class="stat-card"><div class="stat-label">Kilómetros</div><div class="stat-value" id="stKm">—</div><div class="stat-unit" id="stMes">este mes</div></div>
    <div class="stat-card"><div class="stat-label">Horas</div><div class="stat-value" id="stHrs">—</div><div class="stat-unit">horas de entreno</div></div>
    <div class="stat-card"><div class="stat-label">FC promedio</div><div class="stat-value" id="stFC">—</div><div class="stat-unit">bpm este mes</div></div>
    <div class="stat-card"><div class="stat-label">Sesiones</div><div class="stat-value" id="stSess">—</div><div class="stat-unit">actividades</div></div>
  </div>

  <!-- TWO COL -->
  <div class="two-col">

    <!-- EFICIENCIA AEROBICA -->
    <div class="card">
      <div class="card-title">Eficiencia aeróbica — últimas 8 semanas <span style="color:var(--accent);font-size:9px;margin-left:4px">CYCLING</span></div>
      <div class="chart-wrap"><canvas id="effChart"></canvas></div>
    </div>

    <!-- ULTIMA ACTIVIDAD -->
    <div class="card">
      <div class="card-title">Última actividad</div>
      <div id="lastActContainer"><div style="color:var(--muted);font-size:13px">Cargando...</div></div>
    </div>

  </div>

  <!-- DESGLOSE POR DEPORTE -->
  <div class="card">
    <div class="card-title">Este mes — por deporte</div>
    <div id="bySportContainer"><div style="color:var(--muted);font-size:13px">Cargando...</div></div>
  </div>

</div>

<script>
const API='https://mars-fit-analyzer-production.up.railway.app';
const $=id=>document.getElementById(id);
const MES_NAMES=['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
const SPORT_LABELS={cycling:'Ciclismo',running:'Running',walking:'Caminata',training:'Entrenamiento',swimming:'Natación',generic:'Genérico'};
const SPORT_COLORS={cycling:'#e8593c',running:'#1a9e6e',walking:'#2563eb',training:'#f2a623',swimming:'#8b5cf6',generic:'#9a9590'};

// Plan progress
const PLAN_START = new Date('2026-05-04');
const PLAN_WEEKS = 22;
function updatePlan(){
  const now = new Date();
  const diffMs = now - PLAN_START;
  const week = Math.max(1, Math.min(PLAN_WEEKS, Math.ceil(diffMs / (7*24*3600*1000))));
  const pct = Math.round(week / PLAN_WEEKS * 100);
  $('planWeek').textContent = week;
  $('planPct').textContent = pct + '% completado';
  setTimeout(()=>{ $('planBar').style.width = pct + '%'; }, 100);
}

async function loadMonthly(){
  const now = new Date();
  const y = now.getFullYear(), m = now.getMonth()+1;
  try{
    const d = await fetch(`${API}/stats/monthly?year=${y}&month=${m}`).then(r=>r.json());
    $('stKm').textContent = d.km_total ?? '—';
    $('stHrs').textContent = d.horas_total ?? '—';
    $('stFC').textContent = d.fc_promedio ?? '—';
    $('stSess').textContent = d.sesiones ?? '—';
    $('stMes').textContent = MES_NAMES[m-1] + ' ' + y;

    // By sport
    const bs = d.by_sport || [];
    if(bs.length){
      $('bySportContainer').innerHTML = bs.map(s=>`
        <div class="sport-row">
          <div class="sport-dot" style="background:${SPORT_COLORS[s.sport]||'#9a9590'}"></div>
          <div class="sport-name">${SPORT_LABELS[s.sport]||s.sport}</div>
          <div class="sport-count">${s.sesiones} sesiones</div>
          <div class="sport-km">${s.km??'—'} km</div>
        </div>`).join('');
    } else {
      $('bySportContainer').innerHTML = '<div style="color:var(--muted);font-size:13px">Sin datos este mes</div>';
    }
  }catch(e){
    $('stKm').textContent=$('stHrs').textContent=$('stFC').textContent=$('stSess').textContent='—';
  }
}

async function loadEfficiency(){
  try{
    const d = await fetch(`${API}/stats/efficiency?weeks=8&sport=cycling`).then(r=>r.json());
    const weeks = d.data || [];
    const labels = weeks.map(w=>w.semana.slice(5)); // MM-DD
    const eff = weeks.map(w=>w.eficiencia);
    const km = weeks.map(w=>w.km_total);

    new Chart($('effChart'),{
      type:'bar',
      data:{
        labels,
        datasets:[
          {label:'Eficiencia (vel/FC×100)',data:eff,backgroundColor:weeks.map((_,i)=>i===weeks.length-1?'rgba(232,89,60,.9)':'rgba(232,89,60,.35)'),borderRadius:4,order:1},
          {label:'km',data:km,type:'line',borderColor:'rgba(242,166,35,.8)',borderWidth:1.5,pointRadius:0,tension:.3,yAxisID:'y2',order:0}
        ]
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>ctx.dataset.label+': '+ctx.parsed.y}}},
        scales:{
          x:{ticks:{color:'#9a9590',font:{size:9}},grid:{display:false}},
          y:{ticks:{color:'#9a9590',font:{size:9}},grid:{color:'#f0ede8'},title:{display:true,text:'Eficiencia',color:'#9a9590',font:{size:9}}},
          y2:{position:'right',ticks:{color:'#f2a623',font:{size:9}},grid:{display:false},title:{display:true,text:'km',color:'#f2a623',font:{size:9}}}
        }
      }
    });
  }catch(e){}
}

async function loadLastActivity(){
  try{
    const d = await fetch(`${API}/sessions/recent?limit=1`).then(r=>r.json());
    const s = (d.sessions||[])[0];
    if(!s){ $('lastActContainer').innerHTML='<div style="color:var(--muted);font-size:13px">Sin actividades</div>'; return; }
    const fecha = s.start_time ? s.start_time.slice(0,10) : '—';
    const dur = s.duration_hms || '—';
    $('lastActContainer').innerHTML=`
      <div class="act-hero">
        <div class="act-inner">
          <div class="act-sport">${(SPORT_LABELS[s.sport]||s.sport||'').toUpperCase()}</div>
          <div class="act-name">${s.workout_name||'Sesión sin nombre'}</div>
          <div class="act-metrics">
            <div><div class="act-metric-label">Distancia</div><div class="act-metric-value">${s.distance_km??'—'}</div><div class="act-metric-unit">km</div></div>
            <div><div class="act-metric-label">FC prom</div><div class="act-metric-value">${s.avg_hr_bpm??'—'}</div><div class="act-metric-unit">bpm</div></div>
            <div><div class="act-metric-label">Velocidad</div><div class="act-metric-value">${s.avg_speed_kmh??'—'}</div><div class="act-metric-unit">km/h</div></div>
          </div>
          <div class="act-date">${fecha} · ${dur}</div>
        </div>
      </div>
      <div class="act-footer">
        <span style="font-size:11px;color:var(--muted)">Ascenso: ${s.ascent_m??'—'}m · Cadencia: ${s.avg_cadence??'—'} rpm</span>
        <a class="act-link" href="/charts/${s.session_id}">Ver gráficas →</a>
      </div>`;
  }catch(e){}
}

updatePlan();
loadMonthly();
loadEfficiency();
loadLastActivity();
</script>
</body>
</html>"""


@app.post("/analyze-fit")
async def analyze_fit(file: UploadFile = File(...), include_records: bool = Query(False)):
    content  = await file.read()
    filename = (file.filename or "").lower()
    fit_bytes = extract_fit_from_zip(content) if filename.endswith(".zip") else content
    result = parse_fit(fit_bytes, include_records=True)
    sid = str(uuid.uuid4())[:8]
    store_session(sid, {"session_id":sid,"filename":file.filename,
                          "uploaded_at":datetime.now(timezone.utc).isoformat(),"result":result})
    # Persist to DB
    conn = get_db()
    if conn:
        try:
            save_session_db(conn, sid, file.filename, result)
        except Exception as e:
            print(f"DB save error: {e}")
    # Return without full records to keep response light
    r = {k:v for k,v in result.items() if k != "records"}
    return {"session_id":sid,
            "message":f"Guardado. Pasa el session_id '{sid}' al GPT.",
            "charts_url":f"/charts/{sid}",
            **r}


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
                cur.execute("SELECT result_json FROM sessions WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
                if row:
                    data = json.loads(row[0])
                    return {k:v for k,v in data.items() if k != "records"}
        except Exception as e:
            print(f"DB read error: {e}")
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
                LEFT JOIN sessions s ON s.route_id = r.route_id
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
                FROM sessions WHERE route_id=%s
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
            FROM sessions
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
        count_query = "SELECT COUNT(*) FROM sessions WHERE start_time IS NOT NULL"
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
                    cur.execute("SELECT result_json FROM sessions WHERE session_id=%s",(session_id,))
                    row = cur.fetchone()
                    if row:
                        result = json.loads(row[0])
                        entry = {"result": result}
            except: pass
    if not entry:
        raise HTTPException(404, f"session_id '{session_id}' no encontrado.")

    result  = entry["result"]
    session = result["session"]
    records = result.get("records", [])
    insights = result.get("derived_insights", {})

    if not records:
        return HTMLResponse("<h2 style='color:white;background:#111;padding:20px;font-family:sans-serif'>Gráficas no disponibles para sesiones históricas.<br><small style='opacity:.6;font-size:14px'>Vuelve a subir el archivo .fit para ver las gráficas.</small></h2>")

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
<script>
{data_js}
document.getElementById('ttl').textContent=(sessionInfo.workout_name||sessionInfo.start_time||'').slice(0,40)||'Sesión';
document.getElementById('sd').textContent=sessionInfo.distance_km?sessionInfo.distance_km+' km':'—';
document.getElementById('sdur').textContent=sessionInfo.duration_hms||'—';
document.getElementById('shr').textContent=sessionInfo.avg_hr_bpm?sessionInfo.avg_hr_bpm+' bpm':'—';
document.getElementById('sspd').textContent=sessionInfo.avg_speed_kmh?sessionInfo.avg_speed_kmh+' km/h':'—';
function ds(a,n){{if(a.length<=n)return a;const s=a.length/n;return Array.from({{length:n}},(_,i)=>a[Math.floor(i*s)]);}}
const N=400;
const lbl=ds(times,N).map(s=>{{const m=Math.floor(s/60),sc=s%60;return m+':'+String(sc).padStart(2,'0');}});
function mk(id,data,color,mn,mx){{
  new Chart(document.getElementById(id),{{type:'line',
    data:{{labels:lbl,datasets:[{{data:ds(data,N),borderColor:color,borderWidth:1.5,pointRadius:0,fill:true,backgroundColor:color+'22',tension:0.2}}]}},
    options:{{responsive:true,maintainAspectRatio:true,plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{color:'#6b6b6b',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'#1a1a1a'}}}},
               y:{{min:mn,max:mx,ticks:{{color:'#6b6b6b',font:{{size:9}}}},grid:{{color:'#2a2a2a'}}}}}}}}}});}}
const hrc=ds(hrData,N).map(v=>{{
  if(!v)return '#6b6b6b';
  if(v<=108)return '#4a9eff';if(v<=133)return '#888';if(v<=150)return '#3dd68c';
  if(v<=160)return '#f2a623';if(v<=168)return '#e8593c';return '#ff3b3b';
}});
new Chart(document.getElementById('cHR'),{{type:'bar',
  data:{{labels:lbl,datasets:[{{data:ds(hrData,N),backgroundColor:hrc,borderWidth:0}}]}},
  options:{{responsive:true,maintainAspectRatio:true,plugins:{{legend:{{display:false}}}},
    scales:{{x:{{ticks:{{color:'#6b6b6b',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'#1a1a1a'}}}},
             y:{{min:60,ticks:{{color:'#6b6b6b',font:{{size:9}}}},grid:{{color:'#2a2a2a'}}}}}}}}}});
mk('cSpd',spdData,'#4a9eff',0,null);
mk('cCad',cadData,'#f2a623',0,null);
mk('cAlt',altData,'#3dd68c',null,null);
mk('cTmp',tempData,'#888',null,null);
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
    rpe: Optional[int] = None
    weight_before: Optional[float] = None
    weight_after: Optional[float] = None
    water_liters: Optional[float] = None
    caffeine_mg: Optional[int] = None
    food_before: Optional[List[str]] = None
    gels: Optional[int] = None
    bars: Optional[int] = None
    electrolytes: Optional[bool] = None
    digestion: Optional[str] = None
    sleep_hours: Optional[float] = None
    sleep_quality: Optional[str] = None
    conditions: Optional[List[str]] = None
    notes: Optional[str] = None

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
            cur.execute("SELECT duration_s FROM sessions WHERE session_id=%s", (session_id,))
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
                    sleep_hours, sleep_quality, conditions, notes, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    rpe=EXCLUDED.rpe, weight_before=EXCLUDED.weight_before,
                    weight_after=EXCLUDED.weight_after, water_liters=EXCLUDED.water_liters,
                    sweat_rate=EXCLUDED.sweat_rate, caffeine_mg=EXCLUDED.caffeine_mg,
                    food_before=EXCLUDED.food_before, gels=EXCLUDED.gels, bars=EXCLUDED.bars,
                    electrolytes=EXCLUDED.electrolytes, digestion=EXCLUDED.digestion,
                    sleep_hours=EXCLUDED.sleep_hours, sleep_quality=EXCLUDED.sleep_quality,
                    conditions=EXCLUDED.conditions, notes=EXCLUDED.notes
            """, (
                session_id, body.rpe, body.weight_before, body.weight_after,
                body.water_liters, sweat_rate, body.caffeine_mg,
                body.food_before, body.gels, body.bars, body.electrolytes,
                body.digestion, body.sleep_hours, body.sleep_quality,
                body.conditions, body.notes
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


# ── POST /gear ────────────────────────────────────────────────────────────────

@app.post("/gear")
def add_gear(body: GearIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gear (gear_id, name, type, bike_id, installed_date,
                    km_at_install, km_limit, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (gear_id) DO NOTHING
            """, (
                body.gear_id, body.name, body.type, body.bike_id,
                body.installed_date, body.km_at_install, body.km_limit, body.notes
            ))
        return {"ok": True, "gear_id": body.gear_id}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gear ─────────────────────────────────────────────────────────────────

@app.get("/gear")
def list_gear(bike_id: Optional[str] = None, active_only: bool = False):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM gear WHERE 1=1"
        params = []
        if bike_id:
            query += " AND bike_id=%s"
            params.append(bike_id)
        if active_only:
            query += " AND retired_date IS NULL"
        query += " ORDER BY installed_date DESC NULLS LAST"
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"gear": [dict(zip(cols, r)) for r in rows]}
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


# ── POST /maintenance ─────────────────────────────────────────────────────────

@app.post("/maintenance")
def add_maintenance(body: MaintenanceIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO maintenance (bike_id, gear_id, type, description,
                    date, km_at_service, cost_mxn, shop, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                body.bike_id, body.gear_id, body.type, body.description,
                body.date, body.km_at_service, body.cost_mxn, body.shop, body.notes
            ))
            new_id = cur.fetchone()[0]
        return {"ok": True, "id": new_id}
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
            FROM sessions
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
                FROM sessions
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
                    ROUND(CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END::numeric, 2) as eficiencia
                FROM sessions
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
            FROM sessions WHERE start_time IS NOT NULL
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

@app.get("/activities", response_class=HTMLResponse)
def activities_page():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bitácora — Actividades</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--blue:#2563eb;--mono:'Cabinet Grotesk',sans-serif;
  --serif:'Instrument Serif',serif;--shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}

/* NAV */
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;
  box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}

/* LAYOUT */
.layout{display:grid;grid-template-columns:240px 1fr;min-height:calc(100vh - 56px)}

/* SIDEBAR */
aside{background:var(--surface);border-right:1px solid var(--border);padding:20px 0;
  position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto}
.sb-section{padding:0 16px;margin-bottom:20px}
.sb-label{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
  font-weight:500;padding:0 8px;margin-bottom:8px}
.sb-divider{height:1px;background:var(--border);margin:12px 16px}

.sport-btn{width:100%;text-align:left;background:none;border:none;padding:8px 12px;
  border-radius:8px;cursor:pointer;font-family:var(--mono);font-size:13px;font-weight:500;
  color:var(--muted);display:flex;align-items:center;gap:10px;transition:all .15s}
.sport-btn:hover{background:var(--surface2);color:var(--text)}
.sport-btn.active{background:var(--text);color:white}
.sport-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sport-count{margin-left:auto;font-size:11px;opacity:.6}

.filter-row{padding:0 16px 8px}
.filter-lbl{font-size:11px;color:var(--muted);margin-bottom:6px;display:block}
.filter-input{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:8px;
  font-family:var(--mono);font-size:12px;color:var(--text);background:var(--surface2);
  outline:none;transition:border-color .15s}
.filter-input:focus{border-color:var(--accent)}
.filter-select{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:8px;
  font-family:var(--mono);font-size:12px;color:var(--text);background:var(--surface2);
  outline:none;cursor:pointer}

/* MAIN */
.main{padding:24px 28px}

/* HEADER */
.page-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.page-title{font-family:var(--serif);font-style:italic;font-size:24px}
.page-meta{font-size:12px;color:var(--muted)}

/* ACTIVITY LIST */
.act-list{display:flex;flex-direction:column;gap:10px}

.act-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  overflow:hidden;cursor:pointer;transition:transform .15s,box-shadow .15s,border-color .15s;
  box-shadow:var(--shadow);display:grid;grid-template-columns:6px 1fr}
.act-card:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,0,0,.08);border-color:#d4d0ca}

.act-stripe-cycling{background:var(--accent)}
.act-stripe-running{background:var(--green)}
.act-stripe-walking{background:var(--blue)}
.act-stripe-training{background:var(--accent2)}
.act-stripe-swimming{background:#8b5cf6}
.act-stripe-generic{background:var(--muted)}
.act-stripe-default{background:var(--muted)}

.act-body{padding:14px 18px;display:grid;grid-template-columns:1fr auto;align-items:center;gap:16px}
.act-left{}
.act-name{font-family:var(--serif);font-style:italic;font-size:16px;margin-bottom:4px;line-height:1.2}
.act-date{font-size:11px;color:var(--muted);margin-bottom:10px}
.act-metrics{display:flex;gap:20px;flex-wrap:wrap}
.act-metric{display:flex;flex-direction:column;gap:1px}
.act-metric-val{font-size:14px;font-weight:700;line-height:1}
.act-metric-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.act-right{display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.act-sport-tag{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:3px 8px;border-radius:4px}
.act-charts-link{font-size:11px;color:var(--accent);text-decoration:none;font-weight:500;white-space:nowrap}
.act-charts-link:hover{text-decoration:underline}

/* sport tag colors */
.tag-cycling{background:rgba(232,89,60,.12);color:var(--accent)}
.tag-running{background:rgba(26,158,110,.12);color:var(--green)}
.tag-walking{background:rgba(37,99,235,.12);color:var(--blue)}
.tag-training{background:rgba(242,166,35,.12);color:#b45309}
.tag-swimming{background:rgba(139,92,246,.12);color:#7c3aed}
.tag-generic,.tag-default{background:rgba(154,149,144,.12);color:var(--muted)}

/* PAGINATION */
.pagination{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:24px}
.pag-btn{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:7px 16px;font-family:var(--mono);font-size:12px;color:var(--text);
  cursor:pointer;transition:all .15s}
.pag-btn:hover{border-color:var(--accent);color:var(--accent)}
.pag-btn:disabled{opacity:.4;cursor:not-allowed}
.pag-info{font-size:12px;color:var(--muted)}

/* EMPTY / LOADING */
.loading{text-align:center;padding:60px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 14px}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{text-align:center;padding:60px;color:var(--muted);font-size:13px}

@media(max-width:900px){
  .layout{grid-template-columns:1fr}
  aside{position:static;height:auto;border-right:none;border-bottom:1px solid var(--border);padding:12px 0}
  .sb-section{display:flex;flex-wrap:wrap;gap:6px}
  .sport-btn{width:auto;padding:5px 12px;font-size:11px}
  .act-body{grid-template-columns:1fr}
  .act-right{flex-direction:row;align-items:center}
}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">
    <div class="nav-icon">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 0 20"/><path d="M8 12h8"/><path d="M12 8v8"/></svg>
    </div>
    <div><div class="nav-name">Bitácora</div><div class="nav-sub">Mars Training Log</div></div>
  </div>
  <div class="nav-links">
    <a href="/home" class="nav-link">Home</a>
    <a href="/activities" class="nav-link active">Actividades</a>
    <a href="/dashboard" class="nav-link">Rutas</a>
  </div>
</nav>

<div class="layout">
  <aside>
    <div class="sb-section">
      <div class="sb-label">Deporte</div>
      <button class="sport-btn active" onclick="setSport('',this)">
        <span class="sport-dot" style="background:var(--text)"></span>
        Todos
        <span class="sport-count" id="cntAll">—</span>
      </button>
      <div id="sportBtns"></div>
    </div>

    <div class="sb-divider"></div>

    <div class="filter-row">
      <label class="filter-lbl">Mes</label>
      <input class="filter-input" type="month" id="filterMonth" onchange="applyFilters()">
    </div>

    <div class="sb-divider"></div>

    <div class="filter-row">
      <label class="filter-lbl">Ordenar por</label>
      <select class="filter-select" id="sortSelect" onchange="applyFilters()">
        <option value="recent">Más reciente</option>
        <option value="distance">Mayor distancia</option>
        <option value="duration">Mayor duración</option>
      </select>
    </div>
  </aside>

  <div class="main">
    <div class="page-hdr">
      <div>
        <div class="page-title">Actividades</div>
        <div class="page-meta" id="pageMeta">Cargando...</div>
      </div>
    </div>
    <div id="actList"><div class="loading"><div class="spinner"></div>Cargando actividades...</div></div>
    <div class="pagination" id="pagination" style="display:none">
      <button class="pag-btn" id="btnPrev" onclick="prevPage()">← Anterior</button>
      <span class="pag-info" id="pagInfo"></span>
      <button class="pag-btn" id="btnNext" onclick="nextPage()">Siguiente →</button>
    </div>
  </div>
</div>

<script>
const API='https://mars-fit-analyzer-production.up.railway.app';
const $=id=>document.getElementById(id);
const LIMIT=20;
let currentOffset=0, currentSport='', currentMonth='', currentSort='recent', totalSessions=0;

const SPORT_LABELS={cycling:'Ciclismo',running:'Running',walking:'Caminata',
  training:'Entrenamiento',swimming:'Natación',generic:'Genérico','52':'Otro'};
const SPORT_COLORS={cycling:'#e8593c',running:'#1a9e6e',walking:'#2563eb',
  training:'#f2a623',swimming:'#8b5cf6',generic:'#9a9590'};

function sportLabel(s){ return SPORT_LABELS[s]||(s?s.charAt(0).toUpperCase()+s.slice(1):'—'); }
function stripeClass(s){ return ['cycling','running','walking','training','swimming','generic'].includes(s)?`act-stripe-${s}`:'act-stripe-default'; }
function tagClass(s){ return ['cycling','running','walking','training','swimming','generic'].includes(s)?`tag-${s}`:'tag-default'; }

function fmtDate(iso){
  if(!iso) return '—';
  const d=new Date(iso);
  return d.toLocaleDateString('es-MX',{weekday:'short',year:'numeric',month:'short',day:'numeric'});
}

function metricsByType(s){
  const sport=s.sport||'';
  if(sport==='cycling'||sport==='running'){
    return [
      {val: s.distance_km?s.distance_km+' km':'—', lbl:'Distancia'},
      {val: s.duration_hms||'—', lbl:'Duración'},
      {val: s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—', lbl:'FC prom'},
      {val: s.avg_speed_kmh?s.avg_speed_kmh+' km/h':'—', lbl:'Velocidad'},
      {val: s.ascent_m?'+'+s.ascent_m+'m':'—', lbl:'Ascenso'},
      {val: s.avg_cadence?s.avg_cadence+' rpm':'—', lbl:'Cadencia'},
    ];
  } else if(sport==='walking'){
    return [
      {val: s.distance_km?s.distance_km+' km':'—', lbl:'Distancia'},
      {val: s.duration_hms||'—', lbl:'Duración'},
      {val: s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—', lbl:'FC prom'},
    ];
  } else {
    return [
      {val: s.duration_hms||'—', lbl:'Duración'},
      {val: s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—', lbl:'FC prom'},
    ];
  }
}

async function loadSportCounts(){
  try{
    const d = await fetch(`${API}/stats/monthly?year=2026&month=1`).then(r=>r.json());
    // Get all-time counts via sessions endpoint
    const sports=['cycling','running','walking','training','swimming'];
    const container=$('sportBtns');
    container.innerHTML='';
    for(const sp of sports){
      const r = await fetch(`${API}/sessions?sport=${sp}&limit=1`).then(x=>x.json());
      if(r.total>0){
        const btn=document.createElement('button');
        btn.className='sport-btn';
        btn.innerHTML=`<span class="sport-dot" style="background:${SPORT_COLORS[sp]||'#9a9590'}"></span>${sportLabel(sp)}<span class="sport-count">${r.total}</span>`;
        btn.onclick=()=>setSport(sp,btn);
        container.appendChild(btn);
      }
    }
    // Total
    const total = await fetch(`${API}/sessions?limit=1`).then(x=>x.json());
    $('cntAll').textContent=total.total;
  }catch(e){}
}

async function loadActivities(){
  $('actList').innerHTML='<div class="loading"><div class="spinner"></div>Cargando...</div>';
  $('pagination').style.display='none';
  try{
    let url=`${API}/sessions?limit=${LIMIT}&offset=${currentOffset}&sort=${currentSort}`;
    if(currentSport) url+=`&sport=${currentSport}`;
    if(currentMonth) url+=`&month=${currentMonth}`;
    const d = await fetch(url).then(r=>r.json());
    totalSessions=d.total||0;
    const sessions=d.sessions||[];

    $('pageMeta').textContent=`${totalSessions.toLocaleString()} actividades${currentSport?' · '+sportLabel(currentSport):''}${currentMonth?' · '+currentMonth:''}`;

    if(!sessions.length){
      $('actList').innerHTML='<div class="empty">Sin actividades para este filtro</div>';
      return;
    }

    $('actList').innerHTML=sessions.map(s=>`
      <div class="act-card" onclick="location.href='/charts/${s.session_id}'">
        <div class="${stripeClass(s.sport)}"></div>
        <div class="act-body">
          <div class="act-left">
            <div class="act-name">${s.workout_name||'Sesión sin nombre'}</div>
            <div class="act-date">${fmtDate(s.start_time)}</div>
            <div class="act-metrics">
              ${metricsByType(s).map(m=>`
                <div class="act-metric">
                  <div class="act-metric-val">${m.val}</div>
                  <div class="act-metric-lbl">${m.lbl}</div>
                </div>`).join('')}
            </div>
          </div>
          <div class="act-right">
            <span class="act-sport-tag ${tagClass(s.sport)}">${sportLabel(s.sport)}</span>
            <a class="act-charts-link" href="/charts/${s.session_id}" onclick="event.stopPropagation()">Ver gráficas →</a>
          </div>
        </div>
      </div>`).join('');

    // Pagination
    if(totalSessions>LIMIT){
      $('pagination').style.display='flex';
      const page=Math.floor(currentOffset/LIMIT)+1;
      const totalPages=Math.ceil(totalSessions/LIMIT);
      $('pagInfo').textContent=`Página ${page} de ${totalPages}`;
      $('btnPrev').disabled=currentOffset===0;
      $('btnNext').disabled=currentOffset+LIMIT>=totalSessions;
    }
  }catch(e){
    $('actList').innerHTML=`<div class="empty">Error cargando actividades</div>`;
  }
}

function setSport(sp,btn){
  currentSport=sp;
  currentOffset=0;
  document.querySelectorAll('.sport-btn').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  loadActivities();
}

function applyFilters(){
  currentMonth=$('filterMonth').value;
  currentSort=$('sortSelect').value;
  currentOffset=0;
  loadActivities();
}

function prevPage(){ if(currentOffset>0){ currentOffset-=LIMIT; loadActivities(); window.scrollTo(0,0); } }
function nextPage(){ if(currentOffset+LIMIT<totalSessions){ currentOffset+=LIMIT; loadActivities(); window.scrollTo(0,0); } }

loadSportCounts();
loadActivities();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS PARA GPT — respuestas limpias y compactas para Amalgama
# ═══════════════════════════════════════════════════════════════════════════════

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
                FROM sessions
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
                FROM sessions
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
            FROM sessions WHERE start_time IS NOT NULL
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
                FROM sessions WHERE session_id = %s
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
                FROM sessions
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
                    ROUND(CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END::numeric, 2) as eficiencia
                FROM sessions
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
