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

# ── Constants ─────────────────────────────────────────────────────────────────
SEMICIRCLES_TO_DEG = 180 / 2**31

MARS_ZONES = [
    {"zone": 1, "name": "Z1 Recuperación", "bpm_low": 0,   "bpm_high": 108},
    {"zone": 2, "name": "Z2 Aeróbico",     "bpm_low": 134, "bpm_high": 150},
    {"zone": 3, "name": "Z3 Tempo",        "bpm_low": 150, "bpm_high": 160},
    {"zone": 4, "name": "Z4 Umbral",       "bpm_low": 160, "bpm_high": 168},
    {"zone": 5, "name": "Z5 Máximo",       "bpm_low": 169, "bpm_high": 999},
]

# ── Route matching ────────────────────────────────────────────────────────────

def route_signature(start_lat, start_lon, end_lat, end_lon, distance_km, ascent_m):
    """Generate a fuzzy route signature for matching."""
    if not start_lat or not start_lon:
        return None
    # Round coords to ~500m grid
    slat = round(start_lat * 200) / 200
    slon = round(start_lon * 200) / 200
    elat = round(end_lat * 200) / 200  if end_lat else slat
    elon = round(end_lon * 200) / 200  if end_lon else slon
    # Round distance to nearest 3km bucket and ascent to nearest 100m
    dist_bucket = round(distance_km / 3) * 3
    asc_bucket  = round(ascent_m / 100) * 100
    return f"{slat:.3f},{slon:.3f}|{elat:.3f},{elon:.3f}|{dist_bucket}|{asc_bucket}"


def find_or_create_route(conn, start_lat, start_lon, end_lat, end_lon,
                          distance_km, ascent_m, workout_name):
    """Match session to existing route or create new one."""
    if not conn or not start_lat:
        return None

    sig = route_signature(start_lat, start_lon, end_lat, end_lon, distance_km, ascent_m)
    if not sig:
        return None

    with conn.cursor() as cur:
        # Try to find matching route within tolerance
        tol = 3.0  # km tolerance
        cur.execute("""
            SELECT route_id, name FROM routes
            WHERE ABS(distance_km - %s) < %s
              AND ABS(sample_lat - %s) < 0.01
              AND ABS(sample_lon - %s) < 0.01
            ORDER BY ABS(distance_km - %s) ASC
            LIMIT 1
        """, (distance_km, tol, start_lat, start_lon, distance_km))
        row = cur.fetchone()
        if row:
            return {"route_id": row[0], "name": row[1]}

        # Create new route
        route_id = str(uuid.uuid4())[:8]
        # Generate name from workout_name or distance
        if workout_name and workout_name not in ("", "None"):
            base = workout_name.replace("Atizapán de Zaragoza - ", "").replace("Atizapán de Zaragoza", "").strip()
            name = base if base else f"Ruta {round(distance_km)}km"
        else:
            name = f"Ruta {round(distance_km)}km +{ascent_m}m"

        cur.execute("""
            INSERT INTO routes (route_id, name, distance_km, ascent_m, created_at, sample_lat, sample_lon)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (route_id, name, round(distance_km, 1), ascent_m,
              datetime.now(timezone.utc), start_lat, start_lon))
        return {"route_id": route_id, "name": name}


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
        s.get("workout_name", "")
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
            json.dumps(result)
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
    """Dashboard de rutas y progreso."""
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mars — Bitácora de Entrenamiento</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Azeret+Mono:wght@300;400;500;700&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #0a0a0a;
  --surface:  #111111;
  --surface2: #181818;
  --border:   #222222;
  --accent:   #e8593c;
  --accent2:  #f2a623;
  --green:    #3dd68c;
  --blue:     #4a9eff;
  --text:     #e8e6e0;
  --muted:    #555555;
  --mono:     'Azeret Mono', monospace;
  --serif:    'Instrument Serif', serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── NOISE OVERLAY ── */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
  pointer-events: none;
  z-index: 0;
}

/* ── HEADER ── */
header {
  position: sticky;
  top: 0;
  z-index: 100;
  padding: 0 32px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  background: rgba(10,10,10,0.92);
  backdrop-filter: blur(12px);
}

.logo {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.logo-mark {
  font-family: var(--serif);
  font-style: italic;
  font-size: 20px;
  color: var(--text);
  letter-spacing: -0.02em;
}

.logo-sub {
  font-size: 9px;
  letter-spacing: 0.2em;
  color: var(--muted);
  text-transform: uppercase;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 20px;
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.header-stat {
  font-size: 10px;
  color: var(--muted);
  letter-spacing: 0.1em;
}

.header-stat span {
  color: var(--text);
  font-weight: 500;
}

/* ── MAIN ── */
main {
  position: relative;
  z-index: 1;
  max-width: 1200px;
  margin: 0 auto;
  padding: 40px 32px;
}

/* ── HERO STATS ── */
.hero {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr 1fr;
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  margin-bottom: 48px;
}

.hero-stat {
  background: var(--surface);
  padding: 28px 24px;
  position: relative;
  overflow: hidden;
  transition: background 0.2s;
}

.hero-stat:hover { background: var(--surface2); }

.hero-stat::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
}

.hero-stat:nth-child(1)::before { background: var(--accent); }
.hero-stat:nth-child(2)::before { background: var(--accent2); }
.hero-stat:nth-child(3)::before { background: var(--green); }
.hero-stat:nth-child(4)::before { background: var(--blue); }

.hs-label {
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 12px;
}

.hs-value {
  font-family: var(--serif);
  font-size: 42px;
  letter-spacing: -0.03em;
  line-height: 1;
  margin-bottom: 6px;
}

.hs-unit {
  font-size: 11px;
  color: var(--muted);
}

/* ── SECTION TITLE ── */
.section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 20px;
}

.section-title {
  font-size: 11px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--muted);
}

.section-action {
  font-size: 10px;
  color: var(--accent);
  cursor: pointer;
  letter-spacing: 0.1em;
  text-decoration: none;
  border: none;
  background: none;
  font-family: var(--mono);
}

/* ── ROUTES GRID ── */
.routes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
  margin-bottom: 48px;
}

.route-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s, transform 0.15s;
  position: relative;
  overflow: hidden;
}

.route-card:hover {
  border-color: #333;
  background: var(--surface2);
  transform: translateY(-1px);
}

.route-card.active {
  border-color: var(--accent);
}

.route-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}

.route-name {
  font-family: var(--serif);
  font-style: italic;
  font-size: 17px;
  color: var(--text);
  line-height: 1.2;
  max-width: 70%;
}

.route-times {
  font-size: 10px;
  color: var(--muted);
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 4px 10px;
  white-space: nowrap;
}

.route-times span {
  color: var(--accent2);
  font-weight: 500;
}

.route-metrics {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
  margin-bottom: 16px;
}

.rm-item {}

.rm-label {
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 4px;
}

.rm-value {
  font-size: 16px;
  font-weight: 500;
  color: var(--text);
}

.rm-unit {
  font-size: 10px;
  color: var(--muted);
}

.route-progress {
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

.rp-label {
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}

.rp-badges {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.badge {
  font-size: 10px;
  padding: 4px 10px;
  border-radius: 20px;
  letter-spacing: 0.05em;
}

.badge-green { background: rgba(61,214,140,0.12); color: var(--green); border: 1px solid rgba(61,214,140,0.2); }
.badge-red   { background: rgba(232,89,60,0.12);  color: var(--accent); border: 1px solid rgba(232,89,60,0.2); }
.badge-muted { background: rgba(255,255,255,0.04); color: var(--muted); border: 1px solid var(--border); }

/* ── ROUTE DETAIL PANEL ── */
.detail-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px;
  margin-bottom: 48px;
  overflow: hidden;
  display: none;
}

.detail-panel.show { display: block; }

.dp-header {
  padding: 24px 28px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.dp-title {
  font-family: var(--serif);
  font-style: italic;
  font-size: 22px;
}

.dp-close {
  background: none;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 11px;
  padding: 6px 14px;
  cursor: pointer;
  transition: color 0.2s, border-color 0.2s;
}

.dp-close:hover { color: var(--text); border-color: #444; }

.dp-body { padding: 28px; }

/* rides table */
.rides-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.rides-table th {
  text-align: left;
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--muted);
  padding: 0 12px 12px 0;
  border-bottom: 1px solid var(--border);
}

.rides-table td {
  padding: 12px 12px 12px 0;
  border-bottom: 1px solid var(--border);
  color: var(--text);
}

.rides-table tr:last-child td { border-bottom: none; }

.rides-table tr:hover td { color: white; }

.td-date { color: var(--muted) !important; font-size: 11px; }

.trend-up   { color: var(--green); }
.trend-down { color: var(--accent); }
.trend-flat { color: var(--muted); }

/* progress bars */
.progress-section {
  margin-top: 28px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}

.prog-item {}
.prog-label {
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
  display: flex;
  justify-content: space-between;
}

.prog-bar-bg {
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 6px;
}

.prog-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 1s ease;
}

.prog-note {
  font-size: 10px;
  color: var(--muted);
  line-height: 1.4;
}

/* ── LOADING ── */
.loading {
  text-align: center;
  padding: 60px 0;
  color: var(--muted);
  font-size: 11px;
  letter-spacing: 0.2em;
}

.spinner {
  width: 24px;
  height: 24px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 16px;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* ── EMPTY ── */
.empty {
  text-align: center;
  padding: 60px 0;
  color: var(--muted);
  font-size: 12px;
}

/* ── RESPONSIVE ── */
@media (max-width: 768px) {
  main { padding: 24px 16px; }
  .hero { grid-template-columns: 1fr 1fr; }
  .routes-grid { grid-template-columns: 1fr; }
  .progress-section { grid-template-columns: 1fr; }
  header { padding: 0 16px; }
  .header-stat { display: none; }
}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-mark">Bitácora</div>
    <div class="logo-sub">Training Log</div>
  </div>
  <div class="header-right">
    <div class="status-dot"></div>
    <div class="header-stat" id="hdrSessions">— <span>sesiones</span></div>
    <div class="header-stat" id="hdrRoutes">— <span>rutas</span></div>
  </div>
</header>

<main>

  <!-- Hero Stats -->
  <div class="hero" id="heroStats">
    <div class="hero-stat">
      <div class="hs-label">Rutas identificadas</div>
      <div class="hs-value" id="statRoutes">—</div>
      <div class="hs-unit">rutas únicas</div>
    </div>
    <div class="hero-stat">
      <div class="hs-label">Sesiones totales</div>
      <div class="hs-value" id="statSessions">—</div>
      <div class="hs-unit">actividades</div>
    </div>
    <div class="hero-stat">
      <div class="hs-label">FC promedio global</div>
      <div class="hs-value" id="statHR">—</div>
      <div class="hs-unit">bpm promedio</div>
    </div>
    <div class="hero-stat">
      <div class="hs-label">Vel. promedio global</div>
      <div class="hs-value" id="statSpd">—</div>
      <div class="hs-unit">km/h promedio</div>
    </div>
  </div>

  <!-- Routes -->
  <div class="section-header">
    <div class="section-title">Rutas</div>
    <button class="section-action" onclick="loadRoutes()">↻ Actualizar</button>
  </div>

  <div id="routesContainer">
    <div class="loading">
      <div class="spinner"></div>
      Cargando rutas...
    </div>
  </div>

  <!-- Detail Panel -->
  <div class="detail-panel" id="detailPanel">
    <div class="dp-header">
      <div class="dp-title" id="dpTitle">—</div>
      <button class="dp-close" onclick="closeDetail()">✕ Cerrar</button>
    </div>
    <div class="dp-body" id="dpBody"></div>
  </div>

</main>

<script>
const API = 'https://mars-fit-analyzer-production.up.railway.app';

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function fmtHR(v)  { return v ? `${Math.round(v)}` : '—'; }
function fmtSpd(v) { return v ? v.toFixed(1) : '—'; }
function fmtKm(v)  { return v ? v.toFixed(1) : '—'; }

function progressBadge(progress) {
  if (!progress) return '';
  const badges = [];

  if (progress.speed_change_kmh !== undefined) {
    const d = progress.speed_change_kmh;
    if (d > 0.5) badges.push(`<span class="badge badge-green">+${d.toFixed(1)} km/h vel</span>`);
    else if (d < -0.5) badges.push(`<span class="badge badge-red">${d.toFixed(1)} km/h vel</span>`);
    else badges.push(`<span class="badge badge-muted">vel estable</span>`);
  }

  if (progress.hr_change_bpm !== undefined) {
    const d = progress.hr_change_bpm;
    if (d < -3) badges.push(`<span class="badge badge-green">${d} bpm FC ↓</span>`);
    else if (d > 3) badges.push(`<span class="badge badge-red">+${d} bpm FC ↑</span>`);
    else badges.push(`<span class="badge badge-muted">FC estable</span>`);
  }

  return badges.join('');
}

async function loadRoutes() {
  const container = document.getElementById('routesContainer');
  container.innerHTML = '<div class="loading"><div class="spinner"></div>Cargando rutas...</div>';

  try {
    const routes = await fetchJSON(`${API}/routes`);

    if (!routes.length) {
      container.innerHTML = '<div class="empty">No hay rutas aún. Sube actividades para empezar.</div>';
      return;
    }

    // Hero stats
    const totalSessions = routes.reduce((a, r) => a + (r.times_ridden || 0), 0);
    const avgHR  = routes.filter(r => r.avg_hr_bpm).reduce((a,r,_,arr) => a + r.avg_hr_bpm/arr.filter(x=>x.avg_hr_bpm).length, 0);
    const avgSpd = routes.filter(r => r.avg_speed_kmh).reduce((a,r,_,arr) => a + r.avg_speed_kmh/arr.filter(x=>x.avg_speed_kmh).length, 0);

    document.getElementById('statRoutes').textContent   = routes.length;
    document.getElementById('statSessions').textContent = totalSessions;
    document.getElementById('statHR').textContent       = Math.round(avgHR) || '—';
    document.getElementById('statSpd').textContent      = avgSpd.toFixed(1) || '—';
    document.getElementById('hdrSessions').innerHTML    = `${totalSessions} <span>sesiones</span>`;
    document.getElementById('hdrRoutes').innerHTML      = `${routes.length} <span>rutas</span>`;

    // Sort by times ridden
    routes.sort((a, b) => (b.times_ridden || 0) - (a.times_ridden || 0));

    const grid = document.createElement('div');
    grid.className = 'routes-grid';
    grid.id = 'routesGrid';

    routes.forEach(route => {
      const card = document.createElement('div');
      card.className = 'route-card';
      card.setAttribute('data-id', route.route_id);
      card.onclick = () => loadRouteDetail(route.route_id, route.name);

      card.innerHTML = `
        <div class="route-header">
          <div class="route-name">${route.name || 'Ruta sin nombre'}</div>
          <div class="route-times"><span>${route.times_ridden || 0}</span> veces</div>
        </div>
        <div class="route-metrics">
          <div class="rm-item">
            <div class="rm-label">Distancia</div>
            <div class="rm-value">${fmtKm(route.distance_km)}<span class="rm-unit"> km</span></div>
          </div>
          <div class="rm-item">
            <div class="rm-label">FC prom.</div>
            <div class="rm-value">${fmtHR(route.avg_hr_bpm)}<span class="rm-unit"> bpm</span></div>
          </div>
          <div class="rm-item">
            <div class="rm-label">Vel. prom.</div>
            <div class="rm-value">${fmtSpd(route.avg_speed_kmh)}<span class="rm-unit"> km/h</span></div>
          </div>
        </div>
        <div class="route-progress">
          <div class="rp-label">Fechas</div>
          <div class="rp-badges">
            <span class="badge badge-muted">${route.first_ride || '—'} → ${route.last_ride || '—'}</span>
          </div>
        </div>
      `;
      grid.appendChild(card);
    });

    container.innerHTML = '';
    container.appendChild(grid);

  } catch (e) {
    container.innerHTML = `<div class="empty">Error cargando rutas: ${e.message}</div>`;
  }
}

async function loadRouteDetail(routeId, routeName) {
  // Mark active card
  document.querySelectorAll('.route-card').forEach(c => c.classList.remove('active'));
  const card = document.querySelector(`[data-id="${routeId}"]`);
  if (card) {
    card.classList.add('active');
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  const panel = document.getElementById('detailPanel');
  const body  = document.getElementById('dpBody');
  document.getElementById('dpTitle').textContent = routeName || 'Ruta';
  body.innerHTML = '<div class="loading"><div class="spinner"></div>Cargando historial...</div>';
  panel.classList.add('show');
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  try {
    const data = await fetchJSON(`${API}/route/${routeId}`);
    const rides = data.rides || [];
    const prog  = data.progress || {};

    let html = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:28px">
        <div>
          <div class="rm-label">Distancia</div>
          <div style="font-size:24px;font-family:var(--serif);font-style:italic">${fmtKm(data.distance_km)} <span style="font-size:13px;color:var(--muted)">km</span></div>
        </div>
        <div>
          <div class="rm-label">Ascenso</div>
          <div style="font-size:24px;font-family:var(--serif);font-style:italic">+${data.ascent_m || '—'} <span style="font-size:13px;color:var(--muted)">m</span></div>
        </div>
        <div>
          <div class="rm-label">Ejecuciones</div>
          <div style="font-size:24px;font-family:var(--serif);font-style:italic">${data.times_ridden || 0} <span style="font-size:13px;color:var(--muted)">veces</span></div>
        </div>
      </div>
    `;

    if (prog.hr_note || prog.speed_note) {
      html += `
        <div class="progress-section">
          ${prog.speed_note ? `
            <div class="prog-item">
              <div class="prog-label"><span>Velocidad</span><span class="${prog.speed_change_kmh > 0 ? 'trend-up' : prog.speed_change_kmh < 0 ? 'trend-down' : 'trend-flat'}">${prog.speed_change_kmh > 0 ? '+' : ''}${(prog.speed_change_kmh||0).toFixed(1)} km/h</span></div>
              <div class="prog-bar-bg"><div class="prog-bar-fill" style="width:${Math.min(Math.abs(prog.speed_change_kmh||0)*20,100)}%;background:${prog.speed_change_kmh > 0 ? 'var(--green)' : 'var(--accent)'}"></div></div>
              <div class="prog-note">${prog.speed_note}</div>
            </div>
          ` : ''}
          ${prog.hr_note ? `
            <div class="prog-item">
              <div class="prog-label"><span>FC</span><span class="${prog.hr_change_bpm < 0 ? 'trend-up' : prog.hr_change_bpm > 0 ? 'trend-down' : 'trend-flat'}">${prog.hr_change_bpm > 0 ? '+' : ''}${prog.hr_change_bpm||0} bpm</span></div>
              <div class="prog-bar-bg"><div class="prog-bar-fill" style="width:${Math.min(Math.abs(prog.hr_change_bpm||0)*5,100)}%;background:${prog.hr_change_bpm < 0 ? 'var(--green)' : 'var(--accent)'}"></div></div>
              <div class="prog-note">${prog.hr_note}</div>
            </div>
          ` : ''}
        </div>
      `;
    }

    if (rides.length) {
      html += `
        <div style="margin-top:28px">
          <div class="section-title" style="margin-bottom:14px">Historial de ejecuciones</div>
          <table class="rides-table">
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Entrenamiento</th>
                <th>FC avg</th>
                <th>Vel avg</th>
                <th>Cadencia</th>
              </tr>
            </thead>
            <tbody>
              ${rides.map((r, i) => {
                const isFirst = i === 0;
                const isLast  = i === rides.length - 1;
                return `
                  <tr>
                    <td class="td-date">${r.date || '—'} ${isFirst ? '<span style="color:var(--muted);font-size:9px">primera</span>' : ''} ${isLast ? '<span style="color:var(--accent2);font-size:9px">última</span>' : ''}</td>
                    <td style="color:var(--muted);font-size:11px">${(r.workout_name || '—').slice(0,30)}</td>
                    <td>${r.avg_hr_bpm || '—'} <span style="color:var(--muted);font-size:10px">bpm</span></td>
                    <td>${r.avg_speed_kmh ? r.avg_speed_kmh.toFixed(1) : '—'} <span style="color:var(--muted);font-size:10px">km/h</span></td>
                    <td>${r.avg_cadence || '—'} <span style="color:var(--muted);font-size:10px">rpm</span></td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>
      `;
    }

    body.innerHTML = html;

    // Animate bars
    setTimeout(() => {
      document.querySelectorAll('.prog-bar-fill').forEach(b => {
        const w = b.style.width;
        b.style.width = '0%';
        setTimeout(() => b.style.width = w, 50);
      });
    }, 100);

  } catch (e) {
    body.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

function closeDetail() {
  document.getElementById('detailPanel').classList.remove('show');
  document.querySelectorAll('.route-card').forEach(c => c.classList.remove('active'));
}

// Init
loadRoutes();
</script>
</body>
</html>
"""

@app.post("/analyze-fit")
async def analyze_fit(file: UploadFile = File(...), include_records: bool = Query(False)):
    content  = await file.read()
    filename = (file.filename or "").lower()
    fit_bytes = extract_fit_from_zip(content) if filename.endswith(".zip") else content
    result = parse_fit(fit_bytes, include_records=True)
    sid = str(uuid.uuid4())[:8]
    RESULTS_STORE[sid] = {"session_id":sid,"filename":file.filename,
                          "uploaded_at":datetime.now(timezone.utc).isoformat(),"result":result}
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
                       AVG(s.avg_speed_kmh) as avg_spd
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
                     "avg_speed_kmh":round(r[8],1) if r[8] else None}
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
def list_sessions():
    conn = get_db()
    db_sessions = []
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT session_id, filename, uploaded_at, distance_km, start_time
                               FROM sessions ORDER BY start_time DESC LIMIT 50""")
                db_sessions = [{"session_id":r[0],"filename":r[1],
                                "uploaded_at":str(r[2]),"distance_km":r[3],
                                "start_time":str(r[4])[:10] if r[4] else None}
                               for r in cur.fetchall()]
        except: pass
    mem_sessions = [{"session_id":k,"filename":v["filename"],
                     "uploaded_at":v["uploaded_at"],
                     "distance_km":v["result"]["session"].get("distance_km"),
                     "start_time":v["result"]["session"].get("start_time",""[:10])}
                    for k,v in RESULTS_STORE.items()]
    return {"db": db_sessions, "memory": mem_sessions}


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
        return HTMLResponse("<h2 style='color:white;background:#111;padding:20px'>Sin datos de records. Vuelve a subir el archivo.</h2>")

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

