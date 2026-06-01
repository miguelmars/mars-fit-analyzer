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
DATABASE_URL = os.environ.get("DATABASE_URL")

db_conn = None

def get_db():
    global db_conn
    if not DATABASE_URL:
        return None
    if db_conn is not None:
        try:
            # Test connection is still alive
            with db_conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            logger.warning("DB connection lost — reconnecting")
            db_conn = None
    if db_conn is None:
        try:
            import psycopg2
            url = DATABASE_URL
            if '?' not in url:
                url += '?sslmode=require'
            db_conn = psycopg2.connect(url)
            db_conn.autocommit = True
            _init_db(db_conn)
            logger.info("DB connected successfully")
        except Exception as e:
            logger.error(f"DB connection failed: {e}")
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
                sample_lon   FLOAT,
                end_lat      FLOAT,
                end_lon      FLOAT,
                sport        TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS session_records (
                session_id  TEXT NOT NULL,
                t           INT NOT NULL,
                hr          SMALLINT,
                speed       DECIMAL(5,2),
                cadence     SMALLINT,
                altitude    DECIMAL(7,2),
                lat         DECIMAL(10,6),
                lon         DECIMAL(10,6),
                power       SMALLINT,
                PRIMARY KEY (session_id, t)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS post_session (
                session_id    TEXT PRIMARY KEY,
                rpe           SMALLINT,
                weight_before DECIMAL(5,2),
                weight_after  DECIMAL(5,2),
                water_liters  DECIMAL(4,2),
                sweat_rate    DECIMAL(4,2),
                caffeine_mg   SMALLINT,
                food_before   TEXT[],
                gels          SMALLINT,
                bars          SMALLINT,
                electrolytes  BOOLEAN,
                digestion     TEXT,
                sleep_hours   DECIMAL(4,2),
                sleep_quality TEXT,
                conditions    TEXT[],
                notes         TEXT,
                gel_type      TEXT,
                gel_recipe    TEXT,
                gel_carbs_g   SMALLINT,
                gel_sodium_mg SMALLINT,
                gel_timing    TEXT,
                gi_response   TEXT,
                energy_response TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gear (
                gear_id        TEXT PRIMARY KEY,
                name           TEXT,
                type           TEXT,
                bike_id        TEXT,
                installed_date DATE,
                km_at_install  INT DEFAULT 0,
                km_limit       INT,
                retired_date   DATE,
                retired_reason TEXT,
                notes          TEXT,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS maintenance (
                id             SERIAL PRIMARY KEY,
                bike_id        TEXT,
                gear_id        TEXT,
                type           TEXT,
                description    TEXT,
                date           DATE,
                km_at_service  INT,
                cost_mxn       DECIMAL(10,2),
                shop           TEXT,
                notes          TEXT,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recovery (
                id             SERIAL PRIMARY KEY,
                date           DATE,
                type           TEXT,
                duration_min   SMALLINT,
                muscle_zone    TEXT[],
                compex_program TEXT,
                notes          TEXT,
                fatigue        SMALLINT,
                muscle_pain    SMALLINT,
                mental_state   SMALLINT,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS athlete_profile (
                id              SERIAL PRIMARY KEY,
                name            TEXT DEFAULT 'Mars',
                age             SMALLINT,
                weight_kg       DECIMAL(5,2),
                height_cm       SMALLINT,
                hr_rest         SMALLINT,
                hr_max          SMALLINT,
                hr_lt           SMALLINT DEFAULT 168,
                ftp_watts       SMALLINT,
                vo2max          DECIMAL(5,2),
                active_bike_id  TEXT,
                goals           TEXT[],
                injuries        TEXT[],
                notes           TEXT,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Add gel columns to post_session if missing (migration)
        for col_def in [
            ("gel_type", "TEXT"),
            ("gel_recipe", "TEXT"),
            ("gel_carbs_g", "SMALLINT"),
            ("gel_sodium_mg", "SMALLINT"),
            ("gel_timing", "TEXT"),
            ("gi_response", "TEXT"),
            ("energy_response", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE post_session ADD COLUMN IF NOT EXISTS {col_def[0]} {col_def[1]}")
            except Exception:
                pass

        cur.execute("""
            CREATE TABLE IF NOT EXISTS athlete_tests (
                id            SERIAL PRIMARY KEY,
                date          DATE NOT NULL,
                type          TEXT NOT NULL,
                result_value  DECIMAL(8,3),
                result_unit   TEXT,
                route_id      TEXT,
                duration_s    INT,
                avg_hr_bpm    SMALLINT,
                avg_speed_kmh DECIMAL(5,2),
                avg_cadence   SMALLINT,
                conditions    TEXT,
                notes         TEXT,
                raw_data      JSONB,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fuerza (
                id              SERIAL PRIMARY KEY,
                date            DATE NOT NULL,
                category        TEXT NOT NULL,
                subcategory     TEXT,
                muscle_groups   TEXT[],
                intensity       SMALLINT,
                duration_min    SMALLINT,
                sets            SMALLINT,
                reps            SMALLINT,
                weight_kg       DECIMAL(6,2),
                exercise        TEXT,
                notes           TEXT,
                rpe             SMALLINT,
                fatigue_before  SMALLINT,
                fatigue_after   SMALLINT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wellness (
                id                       SERIAL PRIMARY KEY,
                date                     DATE NOT NULL,
                category                 TEXT NOT NULL,
                compex_program           TEXT,
                muscle_zone              TEXT[],
                duration_min             SMALLINT,
                ceragem_duration_min     SMALLINT,
                ceragem_sensation_before SMALLINT,
                ceragem_sensation_after  SMALLINT,
                sleep_hours              DECIMAL(4,2),
                sleep_quality            TEXT,
                hr_rest                  SMALLINT,
                garmin_sleep_score       SMALLINT,
                pain_zone                TEXT,
                pain_level               SMALLINT,
                pain_start               DATE,
                pain_end                 DATE,
                pain_type                TEXT,
                stress_level             SMALLINT,
                stress_cause             TEXT,
                notes                    TEXT,
                fatigue                  SMALLINT,
                created_at               TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accidents (
                id             SERIAL PRIMARY KEY,
                date           DATE NOT NULL,
                description    TEXT,
                damage         TEXT,
                repair         TEXT,
                cost_mxn       DECIMAL(10,2),
                km_at_accident INT,
                notes          TEXT,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Add brand/model to gear if missing
        for col, typ in [("brand","TEXT"),("model","TEXT")]:
            try:
                cur.execute(f"ALTER TABLE gear ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass

# ═══════════════════════════════════════════════════════════════════════════════
# DB — Conexión y tablas
# ═══════════════════════════════════════════════════════════════════════════════

# In-memory fallback
RESULTS_STORE: dict = {}
RESULTS_STORE_MAX = 5  # Máximo de sesiones en memoria

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

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── PWA ───────────────────────────────────────────────────────────────────────

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
    sw_code = """const CACHE='bitacora-v1';
const PAGES=['/home','/activities','/fuerza','/wellness','/gear','/progress','/calendar','/performance'];
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
    svg = b"""<svg xmlns='http://www.w3.org/2000/svg' width='192' height='192' viewBox='0 0 192 192'><rect width='192' height='192' rx='32' fill='#e8593c'/><text x='96' y='130' font-family='Arial' font-size='110' font-weight='bold' fill='white' text-anchor='middle'>M</text></svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/icon-512.png")
def icon_512():
    from fastapi.responses import Response
    svg = b"""<svg xmlns='http://www.w3.org/2000/svg' width='512' height='512' viewBox='0 0 512 512'><rect width='512' height='512' rx='80' fill='#e8593c'/><text x='256' y='350' font-family='Arial' font-size='300' font-weight='bold' fill='white' text-anchor='middle'>M</text></svg>"""
    return Response(content=svg, media_type="image/svg+xml")

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


@app.get("/health")
def health():
    conn = get_db()
    db_ok = False
    db_detail = "no DATABASE_URL"
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sessions")
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
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
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
const API=window.location.origin;
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
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
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
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
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
const API=window.location.origin;
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
        <a class="act-link" href="/session/${s.session_id}">Ver detalle →</a>
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
        conn = get_db()
        if conn:
            try:
                save_session_db(conn, sid, file.filename, result)
                records = result.get("records", [])
                if records:
                    save_records_db(conn, sid, records)
            except Exception as e:
                logger.error(f"DB save error filename={file.filename} error={e}")
        logger.info(f"UPLOAD ok session_id={sid} filename={file.filename}")
        # Return without full records to keep response light
        r = {k:v for k,v in result.items() if k != "records"}
        return {"session_id":sid,
                "message":f"Guardado. Pasa el session_id '{sid}' al GPT.",
                "charts_url":f"/charts/{sid}",
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
                print(f"session_records load error: {e}")
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
  <button onclick="history.back()" style="background:#1a1a1a;border:1px solid #2a2a2a;color:#e8e6e0;padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;font-family:inherit">← Volver</button>
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
            FROM sessions
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
            cur.execute(f"SELECT distance_km, start_time, session_id FROM sessions {where} AND distance_km IS NOT NULL ORDER BY distance_km DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_distance"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Sesión más larga
            cur.execute(f"SELECT duration_s, start_time, session_id FROM sessions {where} AND duration_s IS NOT NULL ORDER BY duration_s DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_duration"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Mayor ascenso
            cur.execute(f"SELECT ascent_m, start_time, session_id FROM sessions {where} AND ascent_m IS NOT NULL ORDER BY ascent_m DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_ascent"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # Mayor velocidad promedio
            cur.execute(f"SELECT avg_speed_kmh, start_time, session_id FROM sessions {where} AND avg_speed_kmh IS NOT NULL ORDER BY avg_speed_kmh DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_speed"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            # FC mínima promedio (mejor forma aeróbica)
            cur.execute(f"SELECT avg_hr_bpm, start_time, session_id FROM sessions {where} AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 60 AND distance_km > 20 ORDER BY avg_hr_bpm ASC LIMIT 1", params)
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
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END)::numeric, 2) as eficiencia
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
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
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
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
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
const API=window.location.origin;
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
      <div class="act-card" onclick="location.href='/session/${s.session_id}'">
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
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END)::numeric, 2) as eficiencia
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
                    SELECT result_json FROM sessions
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
                FROM sessions
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
                JOIN sessions s ON sr.session_id = s.session_id
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
                FROM sessions WHERE route_id = %s AND start_time IS NOT NULL
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
                    FROM sessions
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
                FROM sessions WHERE sport = %s AND start_time IS NOT NULL
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
                JOIN sessions s ON ps.session_id = s.session_id
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
                JOIN sessions s ON ps.session_id = s.session_id
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
                JOIN sessions s ON ps.session_id = s.session_id
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
                JOIN sessions s ON ps.session_id = s.session_id
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
                FROM sessions WHERE sport=%s AND start_time IS NOT NULL
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
                FROM post_session ps JOIN sessions s ON ps.session_id=s.session_id
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
                FROM sessions
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
                SELECT result_json FROM sessions
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
                FROM sessions
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
                    FROM sessions
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
                FROM sessions
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
                FROM sessions
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
                FROM sessions
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
            FROM sessions
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


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page():
    return HTMLResponse(CALENDAR_HTML)


CALENDAR_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Calendario</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--mono:'Cabinet Grotesk',sans-serif;--serif:'Instrument Serif',serif;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
  --c0:#edeae4;--c1:#c6e48b;--c2:#7bc96f;--c3:#239a3b;--c4:#196127;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
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
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
/* Year selector */
.year-nav{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.year-btn{width:32px;height:32px;border-radius:8px;border:1px solid var(--border);background:var(--surface2);
  font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.year-btn:hover{background:var(--text);color:white;border-color:var(--text)}
.year-display{font-size:22px;font-weight:700;min-width:70px;text-align:center}
/* Summary strip */
.summary-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.sum-box{background:var(--surface2);border-radius:10px;padding:12px 14px}
.sum-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:3px}
.sum-value{font-size:22px;font-weight:700}
.sum-unit{font-size:10px;color:var(--muted)}
/* Heatmap */
.heatmap-wrap{overflow-x:auto;padding-bottom:8px}
.heatmap{display:flex;gap:3px;align-items:flex-start}
.month-col{display:flex;flex-direction:column;gap:2px}
.month-label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;height:12px}
.week-col{display:flex;flex-direction:column;gap:2px}
.day-cell{width:12px;height:12px;border-radius:2px;cursor:pointer;transition:transform .1s;position:relative}
.day-cell:hover{transform:scale(1.4);z-index:10}
.day-cell[data-i="0"]{background:var(--c0)}
.day-cell[data-i="1"]{background:var(--c1)}
.day-cell[data-i="2"]{background:var(--c2)}
.day-cell[data-i="3"]{background:var(--c3)}
.day-cell[data-i="4"]{background:var(--c4)}
.day-empty{width:12px;height:12px}
/* Legend */
.legend{display:flex;align-items:center;gap:6px;margin-top:12px;font-size:10px;color:var(--muted)}
.legend-cell{width:12px;height:12px;border-radius:2px}
/* Tooltip */
.tooltip{position:fixed;background:var(--text);color:white;padding:8px 12px;border-radius:8px;
  font-size:11px;line-height:1.6;pointer-events:none;z-index:1000;display:none;white-space:nowrap}
/* Monthly breakdown */
.months-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.month-card{background:var(--surface2);border-radius:10px;padding:14px;cursor:pointer;transition:all .15s;border:2px solid transparent}
.month-card:hover{border-color:var(--accent)}
.mc-name{font-size:11px;color:var(--muted);margin-bottom:6px}
.mc-km{font-size:18px;font-weight:700}
.mc-meta{font-size:11px;color:var(--muted);margin-top:2px}
.mc-bar{height:3px;background:var(--border);border-radius:2px;margin-top:8px;overflow:hidden}
.mc-fill{height:100%;background:var(--accent);border-radius:2px}
/* Loading */
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.summary-strip{grid-template-columns:repeat(2,1fr)}.months-grid{grid-template-columns:repeat(2,1fr)}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/dashboard" class="nav-link">Rutas</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link active">Calendario</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Calendario de entrenamiento</div>
  <div class="page-sub">Historial 2018 — 2026</div>

  <div class="year-nav">
    <button class="year-btn" onclick="changeYear(-1)">‹</button>
    <div class="year-display" id="yearDisplay">2026</div>
    <button class="year-btn" onclick="changeYear(1)">›</button>
  </div>

  <div class="summary-strip" id="summaryStrip">
    <div class="sum-box"><div class="sum-label">Días activos</div><div class="sum-value" id="sDias">—</div></div>
    <div class="sum-box"><div class="sum-label">Kilómetros</div><div class="sum-value" id="sKm">—</div><div class="sum-unit">km</div></div>
    <div class="sum-box"><div class="sum-label">Horas</div><div class="sum-value" id="sHrs">—</div><div class="sum-unit">h</div></div>
    <div class="sum-box"><div class="sum-label">Sesiones</div><div class="sum-value" id="sSes">—</div></div>
  </div>

  <div class="card">
    <div class="card-title">Actividad del año</div>
    <div class="heatmap-wrap">
      <div id="heatmap" class="heatmap"><div class="loading"><div class="spinner"></div></div></div>
    </div>
    <div class="legend">
      <span>Menos</span>
      <div class="legend-cell" style="background:var(--c0)"></div>
      <div class="legend-cell" style="background:var(--c1)"></div>
      <div class="legend-cell" style="background:var(--c2)"></div>
      <div class="legend-cell" style="background:var(--c3)"></div>
      <div class="legend-cell" style="background:var(--c4)"></div>
      <span>Más</span>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Por mes</div>
    <div id="monthsGrid" class="months-grid"><div class="loading"><div class="spinner"></div></div></div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
const API = window.location.origin;
let currentYear = new Date().getFullYear();
let heatData = {};

const MONTHS = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
const MONTH_FULL = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];

function changeYear(d) {
  currentYear = Math.max(2018, Math.min(new Date().getFullYear(), currentYear + d));
  document.getElementById('yearDisplay').textContent = currentYear;
  loadCalendar();
}

async function loadCalendar() {
  document.getElementById('heatmap').innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  document.getElementById('monthsGrid').innerHTML = '<div class="loading"><div class="spinner"></div></div>';

  try {
    const r = await fetch(API + '/gpt/calendar-heatmap?year=' + currentYear);
    const data = await r.json();

    // Update summary
    document.getElementById('sDias').textContent = data.dias_activos || 0;
    document.getElementById('sKm').textContent = data.total_km || 0;
    document.getElementById('sHrs').textContent = data.total_horas || 0;
    document.getElementById('sSes').textContent = data.total_sesiones || 0;

    // Build day map
    heatData = {};
    (data.days || []).forEach(d => { heatData[d.dia] = d; });

    renderHeatmap();
    renderMonths(data.days || []);
  } catch(e) {
    document.getElementById('heatmap').innerHTML = '<p style="color:var(--muted);font-size:13px">Error: ' + e.message + '</p>';
  }
}

function renderHeatmap() {
  const el = document.getElementById('heatmap');
  const jan1 = new Date(currentYear, 0, 1);
  const startDow = jan1.getDay(); // 0=sun

  // Build weeks array
  const weeks = [];
  let week = new Array(startDow).fill(null); // pad start

  const isLeap = (currentYear % 4 === 0 && currentYear % 100 !== 0) || currentYear % 400 === 0;
  const daysInYear = isLeap ? 366 : 365;

  for(let d = 0; d < daysInYear; d++) {
    const dt = new Date(currentYear, 0, d + 1);
    const iso = dt.toISOString().slice(0, 10);
    week.push(iso);
    if(week.length === 7) { weeks.push(week); week = []; }
  }
  if(week.length) { while(week.length < 7) week.push(null); weeks.push(week); }

  // Group weeks by month for labels
  let html = '';
  let lastMonth = -1;
  let monthStartWeek = 0;
  const monthWeeks = {}; // month -> first week index

  weeks.forEach((wk, wi) => {
    wk.forEach(iso => {
      if(iso) {
        const m = parseInt(iso.slice(5,7)) - 1;
        if(m !== lastMonth) { monthWeeks[m] = wi; lastMonth = m; }
      }
    });
  });

  // Render
  html = '<div style="display:flex;flex-direction:column;gap:2px;margin-right:4px">';
  html += '<div style="height:16px"></div>'; // space for month labels
  ['L','M','X','J','V','S','D'].forEach(d => {
    html += '<div style="width:12px;height:12px;font-size:9px;color:var(--muted);display:flex;align-items:center;justify-content:center">' + d + '</div>';
  });
  html += '</div>';

  weeks.forEach((wk, wi) => {
    // Find month label for this week
    let monthLabel = '';
    Object.entries(monthWeeks).forEach(([m, startW]) => {
      if(parseInt(startW) === wi) monthLabel = MONTHS[parseInt(m)];
    });

    html += '<div class="week-col">';
    html += '<div class="month-label">' + monthLabel + '</div>';
    wk.forEach(iso => {
      if(!iso) {
        html += '<div class="day-empty"></div>';
      } else {
        const d = heatData[iso];
        const intensity = d ? d.intensity : 0;
        const title = d ? (iso + ' — ' + d.km + ' km · ' + d.sesiones + ' ses · ' + (d.deportes||'')) : iso;
        html += '<div class="day-cell" data-i="' + intensity + '" data-date="' + iso + '" data-tip="' + title + '" onmouseenter="showTip(event,this)" onmouseleave="hideTip()" onclick="goSession('' + iso + '')"></div>';
      }
    });
    html += '</div>';
  });

  el.innerHTML = html;
}

function renderMonths(days) {
  const byMonth = {};
  days.forEach(d => {
    const m = parseInt(d.dia.slice(5,7)) - 1;
    if(!byMonth[m]) byMonth[m] = {km:0, horas:0, sesiones:0, dias:0};
    byMonth[m].km += parseFloat(d.km||0);
    byMonth[m].horas += parseFloat(d.horas||0);
    byMonth[m].sesiones += parseInt(d.sesiones||0);
    byMonth[m].dias++;
  });

  const maxKm = Math.max(...Object.values(byMonth).map(m => m.km), 1);
  let html = '';
  for(let m = 0; m < 12; m++) {
    const d = byMonth[m] || {km:0, horas:0, sesiones:0, dias:0};
    const pct = Math.round(d.km / maxKm * 100);
    html += '<div class="month-card" onclick="goMonth(' + m + ')">' +
      '<div class="mc-name">' + MONTH_FULL[m] + '</div>' +
      '<div class="mc-km">' + d.km.toFixed(0) + ' <span style="font-size:11px;font-weight:400;color:var(--muted)">km</span></div>' +
      '<div class="mc-meta">' + d.horas.toFixed(1) + 'h · ' + d.sesiones + ' sesiones · ' + d.dias + ' días</div>' +
      '<div class="mc-bar"><div class="mc-fill" style="width:' + pct + '%"></div></div>' +
      '</div>';
  }
  document.getElementById('monthsGrid').innerHTML = html || '<p style="color:var(--muted);font-size:13px">Sin actividad este año</p>';
}

function showTip(e, el) {
  const tip = document.getElementById('tooltip');
  const d = heatData[el.dataset.date];
  if(!d) { tip.style.display='none'; return; }
  tip.innerHTML = '<strong>' + el.dataset.date + '</strong><br>' +
    d.km + ' km · ' + d.horas + 'h · ' + d.sesiones + ' sesión(es)<br>' +
    (d.fc_avg ? 'FC avg: ' + d.fc_avg + ' bpm' : '') +
    (d.deportes ? '<br>' + d.deportes : '');
  tip.style.display = 'block';
  tip.style.left = (e.clientX + 12) + 'px';
  tip.style.top = (e.clientY - 10) + 'px';
}

function hideTip() {
  document.getElementById('tooltip').style.display = 'none';
}

function goSession(iso) {
  // Link to activities filtered by date
  window.location.href = '/activities?date=' + iso;
}

function goMonth(m) {
  const month = m + 1;
  window.location.href = '/activities?month=' + currentYear + '-' + String(month).padStart(2,'0');
}

loadCalendar();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 8 — Perfil de Rendimiento
# ═══════════════════════════════════════════════════════════════════════════════

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
                    FROM sessions WHERE sport=%s AND {col} IS NOT NULL
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
                    FROM sessions WHERE sport=%s AND start_time IS NOT NULL
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
                SELECT avg_speed_kmh, avg_hr_bpm FROM sessions
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
                SELECT session_id, start_time, result_json FROM sessions
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
                FROM sessions WHERE sport=%s AND start_time IS NOT NULL
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
                FROM sessions
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
                FROM sessions
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


@app.get("/performance", response_class=HTMLResponse)
def performance_page():
    return HTMLResponse(PERFORMANCE_HTML)


PERFORMANCE_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Rendimiento</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--blue:#2563eb;--mono:'Cabinet Grotesk',sans-serif;
  --serif:'Instrument Serif',serif;--shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px;flex-wrap:wrap}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.three-col{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px}
.kpi-box{background:var(--surface2);border-radius:10px;padding:16px}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:4px}
.kpi-value{font-size:26px;font-weight:700;line-height:1}
.kpi-unit{font-size:11px;color:var(--muted)}
.kpi-delta{font-size:12px;font-weight:600;margin-top:4px}
.delta-pos{color:var(--green)}
.delta-neg{color:var(--accent)}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
.badge-fresh{background:#e8f5e9;color:var(--green)}
.badge-load{background:#fff8e8;color:#7a5200}
.badge-tired{background:#ffeaea;color:var(--accent)}
.records-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px}
.rec-card{background:var(--surface2);border-radius:10px;padding:14px;cursor:pointer;transition:border .15s;border:1px solid transparent}
.rec-card:hover{border-color:var(--accent)}
.rec-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.rec-value{font-size:19px;font-weight:700}
.rec-unit{font-size:10px;color:var(--muted)}
.rec-date{font-size:10px;color:var(--muted);margin-top:3px}
.rank-section{margin-bottom:12px}
.rank-title{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.rank-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border);cursor:pointer}
.rank-row:last-child{border-bottom:none}
.rank-num{width:20px;height:20px;border-radius:50%;background:var(--surface2);font-size:10px;font-weight:700;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.rank-info{flex:1;min-width:0}
.rank-name{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rank-meta{font-size:11px;color:var(--muted)}
.rank-badge{font-size:11px;font-weight:700;color:var(--blue);white-space:nowrap}
.dc-row{padding:10px 12px;background:var(--surface2);border-radius:8px;border-left:3px solid var(--accent2);
  font-size:12px;margin-bottom:8px;line-height:1.5}
.dc-date{font-size:10px;color:var(--muted);margin-bottom:2px}
.chart-wrap{height:200px;position:relative}
.chart-wrap-sm{height:160px;position:relative}
.weekly-table{width:100%;border-collapse:collapse;font-size:12px}
.weekly-table th{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:500;text-align:left;padding:0 8px 8px 0;border-bottom:1px solid var(--border)}
.weekly-table td{padding:7px 8px 7px 0;border-bottom:1px solid var(--border)}
.weekly-table tr:last-child td{border-bottom:none}
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.two-col,.three-col{grid-template-columns:1fr}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link active">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Perfil de rendimiento</div>
  <div class="page-sub">VO2Max · Carga · Eficiencia aeróbica · Récords · Desacople cardíaco</div>
  <div id="content"><div class="loading"><div class="spinner"></div>Cargando perfil...</div></div>
</div>

<script>
const API = window.location.origin;

const REC_DEFS = [
  {key:'max_distance', label:'Mayor distancia', unit:'km', fmt: v => Number(v).toFixed(1)},
  {key:'max_duration', label:'Sesión más larga', unit:'', fmt: v => { const s=parseInt(v); return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m'; }},
  {key:'max_ascent', label:'Mayor ascenso', unit:'m', fmt: v => parseInt(v)+''},
  {key:'max_speed', label:'Mayor velocidad', unit:'km/h', fmt: v => Number(v).toFixed(1)},
  {key:'min_avg_hr', label:'FC mínima sesión', unit:'bpm', fmt: v => parseInt(v)+''},
  {key:'max_cadence', label:'Cadencia max', unit:'rpm', fmt: v => parseInt(v)+''},
];

const RANK_CATS = [
  {key:'mejor_eficiencia', label:'Mejor eficiencia FC/vel', metaFn: s => (s.eficiencia_ratio||0).toFixed(4) + ' vel/bpm'},
  {key:'mayor_distancia', label:'Mayor distancia', metaFn: s => (s.distance_km||'—') + ' km'},
  {key:'mayor_ascenso', label:'Mayor ascenso', metaFn: s => (s.ascent_m||'—') + ' m'},
  {key:'mayor_velocidad', label:'Mayor velocidad', metaFn: s => (s.avg_speed_kmh||'—') + ' km/h'},
];

async function load() {
  try {
    const r = await fetch(API + '/gpt/performance-profile?sport=cycling');
    const d = await r.json();
    render(d);
  } catch(e) {
    document.getElementById('content').innerHTML = '<p style="color:var(--muted)">Error: ' + e.message + '</p>';
  }
}

function fmtDur(s) {
  s = parseInt(s||0);
  return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m';
}

function render(d) {
  const carga = d.carga || {};
  const eff = d.eficiencia_aerobica || {};
  const recs = d.records || {};
  const ranking = d.ranking || {};
  const weekly = d.carga_semanal || [];
  const monthly = eff.mensual || [];
  const dc = d.desacople_cardiaco || [];

  const badgeClass = carga.estado === 'fresco' ? 'badge-fresh' : carga.estado === 'fatigado' ? 'badge-tired' : 'badge-load';
  const effDeltaClass = (eff.delta_pct_6_meses||0) >= 0 ? 'delta-pos' : 'delta-neg';
  const cadClass = (d.cadencia_trend||'').startsWith('+') ? 'delta-pos' : 'delta-neg';
  const fcVelClass = (d.fc_vel_ratio_tendencia||'').startsWith('+') ? 'delta-pos' : 'delta-neg';

  // Records
  const recsHtml = REC_DEFS.map(rd => {
    const v = recs[rd.key];
    if(!v) return '';
    return '<div class="rec-card" onclick="location.href='/session/'+v.session_id+''">' +
      '<div class="rec-label">'+rd.label+'</div>' +
      '<div class="rec-value">'+rd.fmt(v.value)+' <span class="rec-unit">'+rd.unit+'</span></div>' +
      '<div class="rec-date">'+v.date+(v.name?' · '+v.name.slice(0,20):'')+'</div></div>';
  }).join('');

  // Ranking
  const rankingHtml = RANK_CATS.map(cat => {
    const data = ranking[cat.key];
    if(!data || !data.sessions || !data.sessions.length) return '';
    return '<div class="rank-section"><div class="rank-title">'+cat.label+'</div>' +
      data.sessions.map((s,i) => '<div class="rank-row" onclick="location.href='/session/'+s.session_id+''">' +
        '<div class="rank-num">'+(i+1)+'</div>' +
        '<div class="rank-info"><div class="rank-name">'+(s.workout_name||'Sesión sin nombre')+'</div>' +
        '<div class="rank-meta">'+(s.start_time||'').slice(0,10)+' · '+(s.distance_km||'—')+' km · FC '+(s.avg_hr_bpm||'—')+' bpm</div></div>' +
        '<div class="rank-badge">'+cat.metaFn(s)+'</div></div>'
      ).join('') + '</div>';
  }).join('');

  // Weekly table
  const weeklyHtml = weekly.length ? '<table class="weekly-table"><thead><tr>' +
    '<th>Semana</th><th>Ses</th><th>km</th><th>Horas</th><th>FC</th><th>Vel</th><th>Cad</th></tr></thead><tbody>' +
    weekly.map(w => '<tr><td>'+w.semana+'</td><td>'+w.ses+'</td><td>'+w.km+'</td><td>'+w.horas+'</td>' +
      '<td>'+(w.fc||'—')+'</td><td>'+(w.vel||'—')+'</td><td>'+(w.cad||'—')+'</td></tr>').join('') +
    '</tbody></table>' : '<p style="color:var(--muted);font-size:13px">Sin datos recientes</p>';

  // Desacople
  const dcHtml = dc.length
    ? dc.map(x => '<div class="dc-row"><div class="dc-date">'+x.date+'</div>'+x.nota+'</div>').join('')
    : '<p style="color:var(--muted);font-size:13px">Sin datos (requiere sesiones >1.5h con FIT procesado)</p>';

  document.getElementById('content').innerHTML = `
    <div class="three-col">
      <div class="kpi-box">
        <div class="kpi-label">VO2Max estimado</div>
        <div class="kpi-value">${d.vo2max_estimado||'—'} <span class="kpi-unit">ml/kg/min</span></div>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">${d.vo2max_nota||''}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-label">Eficiencia aeróbica</div>
        <div class="kpi-value ${effDeltaClass}">${eff.delta_pct_6_meses != null ? (eff.delta_pct_6_meses >= 0 ? '+' : '') + eff.delta_pct_6_meses + '%' : '—'}</div>
        <div class="kpi-delta" style="color:var(--muted)">últimos 6 meses</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-label">Cadencia tendencia</div>
        <div class="kpi-value ${cadClass}">${d.cadencia_trend||'—'}</div>
        <div class="kpi-delta" style="color:var(--muted)">vs primer mes</div>
      </div>
    </div>

    <div class="two-col">
      <div class="card">
        <div class="card-title">Carga de entrenamiento</div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          <div><div class="kpi-label">ATL 7d</div><div style="font-size:22px;font-weight:700">${carga.atl_7d||0}</div><div style="font-size:10px;color:var(--muted)">carga aguda</div></div>
          <div><div class="kpi-label">CTL sem</div><div style="font-size:22px;font-weight:700">${carga.ctl_semana||0}</div><div style="font-size:10px;color:var(--muted)">carga crónica</div></div>
          <div><div class="kpi-label">TSB</div><div style="font-size:22px;font-weight:700">${carga.tsb||0}</div>
            <div><span class="badge ${badgeClass}">${carga.estado||'—'}</span></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">FC/Vel ratio — 8 semanas</div>
        <div style="font-size:36px;font-weight:700;text-align:center;padding:12px 0" class="${fcVelClass}">
          ${d.fc_vel_ratio_tendencia||'—'}
        </div>
        <div style="font-size:12px;color:var(--muted);text-align:center">mayor = mejor eficiencia aeróbica</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Eficiencia aeróbica mensual (vel ÷ FC)</div>
      <div class="chart-wrap"><canvas id="effChart"></canvas></div>
    </div>

    <div class="card">
      <div class="card-title">Carga semanal — últimas 8 semanas</div>
      ${weeklyHtml}
    </div>

    <div class="card">
      <div class="card-title">Récords personales</div>
      <div class="records-grid">${recsHtml}</div>
    </div>

    <div class="card">
      <div class="card-title">Ranking de sesiones</div>
      ${rankingHtml}
    </div>

    <div class="card">
      <div class="card-title">Desacople cardíaco (sesiones >1.5h)</div>
      ${dcHtml}
    </div>
  `;

  // Eficiencia chart
  if(monthly.length) {
    const ctx = document.getElementById('effChart').getContext('2d');
    new Chart(ctx, {
      type: 'line',
      data: {
        labels: monthly.map(m => m.mes),
        datasets: [
          {label:'FC bpm', data: monthly.map(m => m.fc), borderColor:'#e8593c', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'yfc'},
          {label:'Vel km/h', data: monthly.map(m => m.vel), borderColor:'#2563eb', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'yspd'},
          {label:'Cadencia rpm', data: monthly.map(m => m.cad), borderColor:'#1a9e6e', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'ycad'},
        ]
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        interaction:{mode:'index', intersect:false},
        plugins:{legend:{labels:{font:{family:'Cabinet Grotesk',size:11}}}},
        scales:{
          x:{grid:{display:false},ticks:{font:{family:'Cabinet Grotesk',size:10}}},
          yfc:{position:'left',title:{display:true,text:'bpm',font:{size:10}},ticks:{font:{size:10}},grid:{color:'#f0ede8'}},
          yspd:{position:'right',title:{display:true,text:'km/h',font:{size:10}},ticks:{font:{size:10}},grid:{display:false}},
          ycad:{display:false}
        }
      }
    });
  }
}

load();
</script>
</body>
</html>"""




# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 11 — Wellness y Recuperación
# ═══════════════════════════════════════════════════════════════════════════════

class WellnessIn(BaseModel):
    date: str
    category: str
    compex_program: Optional[str] = None
    muscle_zone: Optional[List[str]] = None
    duration_min: Optional[int] = Field(None, ge=1, le=300)
    ceragem_duration_min: Optional[int] = Field(None, ge=1, le=300)
    ceragem_sensation_before: Optional[int] = Field(None, ge=1, le=10)
    ceragem_sensation_after: Optional[int] = Field(None, ge=1, le=10)
    sleep_hours: Optional[float] = Field(None, ge=0, le=16)
    sleep_quality: Optional[str] = None
    hr_rest: Optional[int] = Field(None, ge=30, le=120)
    garmin_sleep_score: Optional[int] = Field(None, ge=0, le=100)
    pain_zone: Optional[str] = None
    pain_level: Optional[int] = Field(None, ge=0, le=10)
    pain_start: Optional[str] = None
    pain_end: Optional[str] = None
    pain_type: Optional[str] = None
    stress_level: Optional[int] = Field(None, ge=0, le=10)
    stress_cause: Optional[str] = None
    notes: Optional[str] = None
    fatigue: Optional[int] = Field(None, ge=0, le=10)


def _ensure_wellness_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wellness (
                id                      SERIAL PRIMARY KEY,
                date                    DATE NOT NULL,
                category                TEXT NOT NULL,
                compex_program          TEXT,
                muscle_zone             TEXT[],
                duration_min            SMALLINT,
                ceragem_duration_min    SMALLINT,
                ceragem_sensation_before SMALLINT,
                ceragem_sensation_after  SMALLINT,
                sleep_hours             DECIMAL(4,2),
                sleep_quality           TEXT,
                hr_rest                 SMALLINT,
                garmin_sleep_score      SMALLINT,
                pain_zone               TEXT,
                pain_level              SMALLINT,
                pain_start              DATE,
                pain_end                DATE,
                pain_type               TEXT,
                stress_level            SMALLINT,
                stress_cause            TEXT,
                notes                   TEXT,
                fatigue                 SMALLINT,
                created_at              TIMESTAMPTZ DEFAULT NOW()
            )
        """)


# ── POST /wellness ────────────────────────────────────────────────────────────

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

@app.get("/wellness", response_class=HTMLResponse)
def wellness_page():
    return HTMLResponse(WELLNESS_HTML)


WELLNESS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Wellness</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--teal:#0d9488;--purple:#7c3aed;
  --mono:'Cabinet Grotesk',sans-serif;--serif:'Instrument Serif',serif;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px;flex-wrap:wrap}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
.summary-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.sum-box{background:var(--surface2);border-radius:10px;padding:14px}
.sum-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:3px}
.sum-value{font-size:22px;font-weight:700}
.sum-unit{font-size:10px;color:var(--muted)}
.tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}
.tab.active{background:var(--text);color:white;border-color:var(--text)}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group.full{grid-column:1/-1}
.form-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.form-input{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;
  font-family:var(--mono);font-size:13px;color:var(--text);outline:none;transition:border .15s}
.form-input:focus{border-color:var(--teal)}
.muscle-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.muscle-chip{padding:4px 12px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;
  border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}
.muscle-chip.selected{background:var(--teal);border-color:var(--teal);color:white}
.save-btn{padding:10px 24px;background:var(--teal);color:white;border:none;border-radius:8px;
  font-family:var(--mono);font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s;margin-top:12px}
.save-btn:hover{opacity:.85}
.pain-card{padding:12px 14px;background:#fff5f3;border-radius:10px;border-left:4px solid var(--accent);margin-bottom:8px}
.pain-zone{font-size:13px;font-weight:700}
.pain-meta{font-size:11px;color:var(--muted);margin-top:3px}
.entry-row{padding:10px 0;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;gap:12px}
.entry-row:last-child{border-bottom:none}
.entry-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  font-size:16px;flex-shrink:0}
.entry-info{flex:1}
.entry-title{font-size:13px;font-weight:600}
.entry-meta{font-size:11px;color:var(--muted);margin-top:2px}
.cat-fields{display:none}
.cat-fields.show{display:contents}
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--teal);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.summary-row{grid-template-columns:1fr 1fr}.form-grid{grid-template-columns:1fr}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link active">Wellness</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Wellness y recuperación</div>
  <div class="page-sub">Compex Recovery · Ceragem · Sueño · Molestias · Estrés</div>

  <div id="summarySection"><div class="loading"><div class="spinner"></div></div></div>

  <!-- Registrar -->
  <div class="card">
    <div class="card-title">Registrar sesión de recuperación</div>
    <div class="tabs">
      <div class="tab active" data-cat="compex_recovery" onclick="setCat('compex_recovery')">Compex</div>
      <div class="tab" data-cat="massage_gun" onclick="setCat('massage_gun')">Pistola</div>
      <div class="tab" data-cat="ceragem" onclick="setCat('ceragem')">Ceragem</div>
      <div class="tab" data-cat="foam_roller" onclick="setCat('foam_roller')">Foam</div>
      <div class="tab" data-cat="sleep" onclick="setCat('sleep')">Sueño</div>
      <div class="tab" data-cat="pain" onclick="setCat('pain')">Molestia</div>
      <div class="tab" data-cat="stress" onclick="setCat('stress')">Estrés</div>
    </div>

    <div class="form-grid">
      <div class="form-group">
        <label class="form-label">Fecha</label>
        <input class="form-input" id="wDate" type="date">
      </div>
      <div class="form-group">
        <label class="form-label">Fatiga general (1-10)</label>
        <input class="form-input" id="wFatigue" type="number" min="1" max="10" placeholder="5">
      </div>

      <!-- Compex Recovery -->
      <div class="form-group cat-fields compex_recovery show">
        <label class="form-label">Programa</label>
        <select class="form-input" id="wCompexProgram">
          <option value="active_recovery">Active Recovery</option>
          <option value="recovery">Recovery</option>
          <option value="massage">Massage</option>
          <option value="potentiation">Potentiation</option>
        </select>
      </div>
      <div class="form-group cat-fields compex_recovery show">
        <label class="form-label">Duración (min)</label>
        <input class="form-input" id="wDuration" type="number" placeholder="20">
      </div>

      <!-- Ceragem -->
      <div class="form-group cat-fields ceragem">
        <label class="form-label">Duración (min)</label>
        <input class="form-input" id="wCeragemDur" type="number" placeholder="40">
      </div>
      <div class="form-group cat-fields ceragem">
        <label class="form-label">Sensación antes (1-10)</label>
        <input class="form-input" id="wCeragemBefore" type="number" min="1" max="10" placeholder="6">
      </div>
      <div class="form-group cat-fields ceragem">
        <label class="form-label">Sensación después (1-10)</label>
        <input class="form-input" id="wCeragemAfter" type="number" min="1" max="10" placeholder="8">
      </div>

      <!-- Sueño -->
      <div class="form-group cat-fields sleep">
        <label class="form-label">Horas dormidas</label>
        <input class="form-input" id="wSleepHours" type="number" step="0.5" placeholder="7.5">
      </div>
      <div class="form-group cat-fields sleep">
        <label class="form-label">Calidad</label>
        <select class="form-input" id="wSleepQuality">
          <option value="">— selecciona —</option>
          <option value="bueno">Bueno</option>
          <option value="regular">Regular</option>
          <option value="malo">Malo</option>
        </select>
      </div>
      <div class="form-group cat-fields sleep">
        <label class="form-label">FC reposo (bpm)</label>
        <input class="form-input" id="wHrRest" type="number" placeholder="52">
      </div>
      <div class="form-group cat-fields sleep">
        <label class="form-label">Score Garmin</label>
        <input class="form-input" id="wGarminScore" type="number" placeholder="78">
      </div>

      <!-- Molestia/Dolor -->
      <div class="form-group cat-fields pain">
        <label class="form-label">Zona</label>
        <input class="form-input" id="wPainZone" placeholder="Isquiotibial izquierdo">
      </div>
      <div class="form-group cat-fields pain">
        <label class="form-label">Nivel (1-10)</label>
        <input class="form-input" id="wPainLevel" type="number" min="1" max="10" placeholder="4">
      </div>
      <div class="form-group cat-fields pain">
        <label class="form-label">Tipo</label>
        <select class="form-input" id="wPainType">
          <option value="muscular">Muscular</option>
          <option value="articular">Articular</option>
          <option value="tendon">Tendón</option>
          <option value="overuse">Sobreuso</option>
          <option value="other">Otro</option>
        </select>
      </div>
      <div class="form-group cat-fields pain">
        <label class="form-label">Fecha inicio</label>
        <input class="form-input" id="wPainStart" type="date">
      </div>

      <!-- Estrés -->
      <div class="form-group cat-fields stress">
        <label class="form-label">Nivel (1-10)</label>
        <input class="form-input" id="wStressLevel" type="number" min="1" max="10" placeholder="6">
      </div>
      <div class="form-group cat-fields stress">
        <label class="form-label">Causa</label>
        <select class="form-input" id="wStressCause">
          <option value="">— selecciona —</option>
          <option value="work">Trabajo</option>
          <option value="travel">Viaje</option>
          <option value="illness">Enfermedad</option>
          <option value="fatigue">Fatiga acumulada</option>
          <option value="personal">Personal</option>
          <option value="sleep">Falta de sueño</option>
        </select>
      </div>
    </div>

    <!-- Zona muscular para compex/pistola/foam -->
    <div id="muscleSection" style="margin-top:12px">
      <div class="form-label" style="margin-bottom:8px">Zona muscular</div>
      <div class="muscle-chips">
        <div class="muscle-chip" data-m="quadriceps" onclick="toggleMuscle('quadriceps')">Cuádriceps</div>
        <div class="muscle-chip" data-m="hamstrings" onclick="toggleMuscle('hamstrings')">Isquios</div>
        <div class="muscle-chip" data-m="glutes" onclick="toggleMuscle('glutes')">Glúteos</div>
        <div class="muscle-chip" data-m="calves" onclick="toggleMuscle('calves')">Pantorrillas</div>
        <div class="muscle-chip" data-m="lower_back" onclick="toggleMuscle('lower_back')">Lumbar</div>
        <div class="muscle-chip" data-m="upper_back" onclick="toggleMuscle('upper_back')">Espalda alta</div>
        <div class="muscle-chip" data-m="shoulders" onclick="toggleMuscle('shoulders')">Hombros</div>
        <div class="muscle-chip" data-m="neck" onclick="toggleMuscle('neck')">Cuello</div>
        <div class="muscle-chip" data-m="it_band" onclick="toggleMuscle('it_band')">IT Band</div>
        <div class="muscle-chip" data-m="full_legs" onclick="toggleMuscle('full_legs')">Piernas completas</div>
      </div>
    </div>

    <div class="form-group full" style="margin-top:12px">
      <label class="form-label">Notas</label>
      <input class="form-input" id="wNotes" placeholder="Sensaciones, observaciones...">
    </div>

    <button class="save-btn" onclick="saveWellness()">Guardar registro</button>
    <span id="saveMsg" style="margin-left:12px;font-size:12px;color:var(--teal);display:none">✓ Guardado</span>
  </div>
</div>

<script>
const API = window.location.origin;
let currentCat = 'compex_recovery';
let selectedMuscles = new Set();

const CAT_ICONS = {
  compex_recovery:'⚡', massage_gun:'🔫', ceragem:'🛏',
  foam_roller:'🟫', sleep:'😴', pain:'🔴', stress:'😤', illness:'🤒', stretching:'🧘'
};
const CAT_LABELS = {
  compex_recovery:'Compex Recovery', massage_gun:'Pistola de masaje',
  ceragem:'Ceragem', foam_roller:'Foam Roller', sleep:'Sueño',
  pain:'Molestia/Dolor', stress:'Estrés', illness:'Enfermedad', stretching:'Estiramientos'
};
const MUSCLE_LABELS = {
  quadriceps:'Cuádriceps', hamstrings:'Isquios', glutes:'Glúteos',
  calves:'Pantorrillas', lower_back:'Lumbar', upper_back:'Espalda alta',
  shoulders:'Hombros', neck:'Cuello', it_band:'IT Band', full_legs:'Piernas completas'
};

document.getElementById('wDate').value = new Date().toISOString().slice(0,10);

function setCat(cat) {
  currentCat = cat;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.cat === cat));
  document.querySelectorAll('.cat-fields').forEach(el => {
    el.classList.remove('show');
  });
  document.querySelectorAll('.cat-fields.' + cat).forEach(el => el.classList.add('show'));
  // Show muscle section for recovery types
  const showMuscles = ['compex_recovery','massage_gun','foam_roller','stretching'].includes(cat);
  document.getElementById('muscleSection').style.display = showMuscles ? 'block' : 'none';
}

function toggleMuscle(m) {
  const chip = document.querySelector('.muscle-chip[data-m="'+m+'"]');
  if(selectedMuscles.has(m)) { selectedMuscles.delete(m); chip.classList.remove('selected'); }
  else { selectedMuscles.add(m); chip.classList.add('selected'); }
}

async function loadSummary() {
  try {
    const r = await fetch(API + '/gpt/wellness-summary?weeks=4');
    const d = await r.json();
    renderSummary(d);
  } catch(e) {
    document.getElementById('summarySection').innerHTML = '<p style="color:var(--muted);font-size:13px;padding:12px">Sin registros aún.</p>';
  }
}

function renderSummary(d) {
  const byCat = d.por_categoria || {};
  const pain = d.molestias_activas || [];
  const history = d.historial_reciente || [];

  const painHtml = pain.length
    ? pain.map(p => '<div class="pain-card"><div class="pain-zone">🔴 '+p.pain_zone+'</div>' +
        '<div class="pain-meta">Nivel '+p.pain_level+'/10 · '+(p.pain_type||'')+(p.pain_start?' · desde '+p.pain_start:'')+'</div>' +
        (p.notes ? '<div style="font-size:11px;color:var(--muted);margin-top:3px">'+p.notes+'</div>' : '') +
        '</div>').join('')
    : '<p style="color:var(--green);font-size:12px;padding:4px 0">✓ Sin molestias activas</p>';

  const histHtml = history.length
    ? history.map(e => '<div class="entry-row">' +
        '<div class="entry-icon" style="background:var(--surface2)">'+(CAT_ICONS[e.category]||'💊')+'</div>' +
        '<div class="entry-info">' +
          '<div class="entry-title">'+(CAT_LABELS[e.category]||e.category)+'</div>' +
          '<div class="entry-meta">'+(e.date||'').slice(0,10) +
            (e.compex_program ? ' · '+e.compex_program : '') +
            (e.duration_min ? ' · '+e.duration_min+' min' : '') +
            (e.sleep_hours ? ' · '+e.sleep_hours+'h sueño' : '') +
            (e.sleep_quality ? ' · '+e.sleep_quality : '') +
            (e.hr_rest ? ' · FC reposo '+e.hr_rest+' bpm' : '') +
            (e.pain_zone ? ' · '+e.pain_zone+' nivel '+e.pain_level : '') +
            (e.stress_level ? ' · estrés '+e.stress_level+'/10' : '') +
          '</div>' +
          (e.notes ? '<div style="font-size:11px;color:var(--muted);margin-top:2px">'+e.notes+'</div>' : '') +
        '</div></div>'
      ).join('')
    : '<p style="color:var(--muted);font-size:13px">Sin registros recientes.</p>';

  const catHtml = Object.entries(byCat).slice(0,4).map(([cat, count]) =>
    '<div class="sum-box"><div class="sum-label">'+(CAT_LABELS[cat]||cat).slice(0,14)+'</div>' +
    '<div class="sum-value">'+count+'</div><div class="sum-unit">sesiones</div></div>'
  ).join('');

  document.getElementById('summarySection').innerHTML =
    '<div class="summary-row">' +
      '<div class="sum-box"><div class="sum-label">Sueño promedio</div>' +
        '<div class="sum-value">'+(d.sueno_promedio_horas||'—')+'</div><div class="sum-unit">h</div></div>' +
      '<div class="sum-box"><div class="sum-label">FC reposo</div>' +
        '<div class="sum-value">'+(d.fc_reposo_promedio||'—')+'</div><div class="sum-unit">bpm</div></div>' +
      '<div class="sum-box"><div class="sum-label">Fatiga promedio</div>' +
        '<div class="sum-value">'+(d.fatiga_promedio||'—')+'</div><div class="sum-unit">/10</div></div>' +
      '<div class="sum-box"><div class="sum-label">Ceragem delta</div>' +
        '<div class="sum-value" style="color:var(--teal)">'+(d.ceragem_delta_sensacion != null ? (d.ceragem_delta_sensacion > 0 ? '+' : '')+d.ceragem_delta_sensacion : '—')+'</div>' +
        '<div class="sum-unit">sensación</div></div>' +
    '</div>' +

    (pain.length ? '<div class="card"><div class="card-title">⚠️ Molestias activas</div>'+painHtml+'</div>' : '') +

    '<div class="card"><div class="card-title">Historial reciente</div>' +
    '<div>'+histHtml+'</div></div>';
}

async function saveWellness() {
  const body = {
    date: document.getElementById('wDate').value,
    category: currentCat,
    fatigue: parseInt(document.getElementById('wFatigue').value)||null,
    notes: document.getElementById('wNotes').value||null,
    muscle_zone: [...selectedMuscles]
  };
  if(!body.date) { alert('Fecha requerida'); return; }

  if(currentCat === 'compex_recovery') {
    body.compex_program = document.getElementById('wCompexProgram').value;
    body.duration_min = parseInt(document.getElementById('wDuration').value)||null;
  } else if(currentCat === 'ceragem') {
    body.ceragem_duration_min = parseInt(document.getElementById('wCeragemDur').value)||null;
    body.ceragem_sensation_before = parseInt(document.getElementById('wCeragemBefore').value)||null;
    body.ceragem_sensation_after = parseInt(document.getElementById('wCeragemAfter').value)||null;
  } else if(currentCat === 'sleep') {
    body.sleep_hours = parseFloat(document.getElementById('wSleepHours').value)||null;
    body.sleep_quality = document.getElementById('wSleepQuality').value||null;
    body.hr_rest = parseInt(document.getElementById('wHrRest').value)||null;
    body.garmin_sleep_score = parseInt(document.getElementById('wGarminScore').value)||null;
  } else if(currentCat === 'pain') {
    body.pain_zone = document.getElementById('wPainZone').value||null;
    body.pain_level = parseInt(document.getElementById('wPainLevel').value)||null;
    body.pain_type = document.getElementById('wPainType').value||null;
    body.pain_start = document.getElementById('wPainStart').value||null;
  } else if(currentCat === 'stress') {
    body.stress_level = parseInt(document.getElementById('wStressLevel').value)||null;
    body.stress_cause = document.getElementById('wStressCause').value||null;
  } else if(['massage_gun','foam_roller','stretching'].includes(currentCat)) {
    body.duration_min = parseInt(document.getElementById('wDuration').value)||null;
  }

  try {
    const r = await fetch(API + '/wellness', {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const res = await r.json();
    if(res.ok) {
      const msg = document.getElementById('saveMsg');
      msg.style.display = 'inline';
      setTimeout(() => msg.style.display='none', 3000);
      selectedMuscles.clear();
      document.querySelectorAll('.muscle-chip').forEach(c => c.classList.remove('selected'));
      document.getElementById('wNotes').value = '';
      loadSummary();
    }
  } catch(e) { alert('Error: '+e.message); }
}

loadSummary();
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 10 — Fuerza
# ═══════════════════════════════════════════════════════════════════════════════

class FuerzaIn(BaseModel):
    date: str
    category: str
    subcategory: Optional[str] = None
    muscle_groups: Optional[List[str]] = None
    intensity: Optional[int] = Field(None, ge=0, le=999)
    duration_min: Optional[int] = Field(None, ge=1, le=300)
    sets: Optional[int] = Field(None, ge=0, le=20)
    reps: Optional[int] = Field(None, ge=0, le=200)
    weight_kg: Optional[float] = Field(None, ge=0, le=300)
    exercise: Optional[str] = None
    notes: Optional[str] = None
    rpe: Optional[int] = Field(None, ge=1, le=10)
    fatigue_before: Optional[int] = Field(None, ge=1, le=10)
    fatigue_after: Optional[int] = Field(None, ge=1, le=10)


def _ensure_fuerza_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fuerza (
                id              SERIAL PRIMARY KEY,
                date            DATE NOT NULL,
                category        TEXT NOT NULL,
                subcategory     TEXT,
                muscle_groups   TEXT[],
                intensity       SMALLINT,
                duration_min    SMALLINT,
                sets            SMALLINT,
                reps            SMALLINT,
                weight_kg       DECIMAL(6,2),
                exercise        TEXT,
                notes           TEXT,
                rpe             SMALLINT,
                fatigue_before  SMALLINT,
                fatigue_after   SMALLINT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)


# ── POST /fuerza ──────────────────────────────────────────────────────────────

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

@app.get("/fuerza", response_class=HTMLResponse)
def fuerza_page():
    return HTMLResponse(FUERZA_HTML)


FUERZA_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Fuerza</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--blue:#2563eb;--purple:#7c3aed;
  --mono:'Cabinet Grotesk',sans-serif;--serif:'Instrument Serif',serif;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px;flex-wrap:wrap}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
.tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;
  border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}
.tab.active{background:var(--text);color:white;border-color:var(--text)}
.summary-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.sum-box{background:var(--surface2);border-radius:10px;padding:14px}
.sum-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:3px}
.sum-value{font-size:22px;font-weight:700}
.sum-unit{font-size:10px;color:var(--muted)}
.session-list{display:flex;flex-direction:column;gap:8px}
.ses-card{background:var(--surface2);border-radius:10px;padding:14px;border-left:4px solid var(--purple)}
.ses-card.compex{border-left-color:#7c3aed}
.ses-card.gym{border-left-color:#2563eb}
.ses-card.plyo{border-left-color:#1a9e6e}
.ses-card.core{border-left-color:#f2a623}
.ses-card.bands{border-left-color:#e8593c}
.ses-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px}
.ses-date{font-size:11px;color:var(--muted)}
.ses-cat{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  padding:2px 8px;border-radius:4px;background:var(--surface)}
.ses-muscles{font-size:12px;font-weight:600;margin-bottom:3px}
.ses-meta{font-size:11px;color:var(--muted)}
.muscle-progress{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.muscle-card{background:var(--surface2);border-radius:10px;padding:14px}
.muscle-name{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px}
.intensity-bar-wrap{height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin-bottom:6px}
.intensity-bar{height:100%;border-radius:4px;background:var(--purple)}
.intensity-val{font-size:20px;font-weight:700}
.intensity-label{font-size:10px;color:var(--muted)}
.chart-wrap{height:200px;position:relative}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group.full{grid-column:1/-1}
.form-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.form-input{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;
  font-family:var(--mono);font-size:13px;color:var(--text);outline:none;transition:border .15s}
.form-input:focus{border-color:var(--accent)}
.muscle-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.muscle-chip{padding:4px 12px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;
  border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}
.muscle-chip.selected{background:var(--purple);border-color:var(--purple);color:white}
.save-btn{padding:10px 24px;background:var(--accent);color:white;border:none;border-radius:8px;
  font-family:var(--mono);font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s;margin-top:12px}
.save-btn:hover{opacity:.85}
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.cat-section{margin-bottom:8px}
.cat-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
/* Compex fields */
.compex-fields, .gym-fields, .plyo-fields{display:none}
.compex-fields.show, .gym-fields.show, .plyo-fields.show{display:contents}
@media(max-width:600px){.summary-row{grid-template-columns:1fr 1fr}.form-grid{grid-template-columns:1fr}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link active">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Entrenamiento de fuerza</div>
  <div class="page-sub">Compex · Gimnasio · Pliometría · Core · Funcional</div>

  <div id="summarySection"><div class="loading"><div class="spinner"></div></div></div>

  <!-- Registrar sesión -->
  <div class="card">
    <div class="card-title">Registrar sesión de fuerza</div>

    <div class="tabs">
      <div class="tab active" data-cat="compex" onclick="setCat('compex')">Compex</div>
      <div class="tab" data-cat="gym" onclick="setCat('gym')">Gimnasio</div>
      <div class="tab" data-cat="plyo" onclick="setCat('plyo')">Pliometría</div>
      <div class="tab" data-cat="core" onclick="setCat('core')">Core</div>
      <div class="tab" data-cat="bands" onclick="setCat('bands')">Bandas</div>
      <div class="tab" data-cat="functional" onclick="setCat('functional')">Funcional</div>
    </div>

    <div class="form-grid">
      <div class="form-group">
        <label class="form-label">Fecha</label>
        <input class="form-input" id="fDate" type="date">
      </div>
      <div class="form-group">
        <label class="form-label">Duración (min)</label>
        <input class="form-input" id="fDuration" type="number" placeholder="32">
      </div>

      <!-- Compex específico -->
      <div class="form-group compex-fields show">
        <label class="form-label">Programa Compex</label>
        <select class="form-input" id="fSubcat">
          <option value="strength">Strength</option>
          <option value="explosive_strength">Explosive Strength</option>
          <option value="resistance">Resistance</option>
          <option value="strength_endurance">Strength Endurance</option>
          <option value="active_recovery">Active Recovery</option>
          <option value="massage">Massage</option>
        </select>
      </div>
      <div class="form-group compex-fields show">
        <label class="form-label">Intensidad alcanzada</label>
        <input class="form-input" id="fIntensity" type="number" min="1" max="100" placeholder="58">
      </div>

      <!-- Gym específico -->
      <div class="form-group gym-fields">
        <label class="form-label">Ejercicio</label>
        <input class="form-input" id="fExercise" placeholder="Sentadilla, Peso muerto...">
      </div>
      <div class="form-group gym-fields">
        <label class="form-label">Series × Reps × kg</label>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px">
          <input class="form-input" id="fSets" type="number" placeholder="3">
          <input class="form-input" id="fReps" type="number" placeholder="12">
          <input class="form-input" id="fWeight" type="number" step="0.5" placeholder="0">
        </div>
      </div>
    </div>

    <!-- Grupos musculares -->
    <div style="margin-top:12px">
      <div class="form-label" style="margin-bottom:8px">Grupos musculares</div>
      <div class="muscle-chips">
        <div class="muscle-chip" data-m="quadriceps" onclick="toggleMuscle('quadriceps')">Cuádriceps</div>
        <div class="muscle-chip" data-m="hamstrings" onclick="toggleMuscle('hamstrings')">Isquios</div>
        <div class="muscle-chip" data-m="glutes" onclick="toggleMuscle('glutes')">Glúteos</div>
        <div class="muscle-chip" data-m="calves" onclick="toggleMuscle('calves')">Pantorrillas</div>
        <div class="muscle-chip" data-m="shoulders" onclick="toggleMuscle('shoulders')">Hombros</div>
        <div class="muscle-chip" data-m="core" onclick="toggleMuscle('core')">Core</div>
        <div class="muscle-chip" data-m="back" onclick="toggleMuscle('back')">Espalda</div>
        <div class="muscle-chip" data-m="chest" onclick="toggleMuscle('chest')">Pecho</div>
        <div class="muscle-chip" data-m="arms" onclick="toggleMuscle('arms')">Brazos</div>
        <div class="muscle-chip" data-m="full_body" onclick="toggleMuscle('full_body')">Cuerpo completo</div>
      </div>
    </div>

    <div class="form-grid" style="margin-top:12px">
      <div class="form-group">
        <label class="form-label">RPE (1-10)</label>
        <input class="form-input" id="fRpe" type="number" min="1" max="10" placeholder="7">
      </div>
      <div class="form-group">
        <label class="form-label">Fatiga previa (1-10)</label>
        <input class="form-input" id="fFatigue" type="number" min="1" max="10" placeholder="4">
      </div>
      <div class="form-group full">
        <label class="form-label">Notas</label>
        <input class="form-input" id="fNotes" placeholder="Sensaciones, progresión, molestias...">
      </div>
    </div>

    <button class="save-btn" onclick="saveFuerza()">Guardar sesión</button>
    <span id="saveMsg" style="margin-left:12px;font-size:12px;color:var(--green);display:none">✓ Guardado</span>
  </div>
</div>

<script>
const API = window.location.origin;
let currentCat = 'compex';
let selectedMuscles = new Set();

const CAT_LABELS = {
  compex:'Compex', gym:'Gimnasio', plyo:'Pliometría',
  core:'Core', bands:'Bandas', functional:'Funcional'
};

const MUSCLE_LABELS = {
  quadriceps:'Cuádriceps', hamstrings:'Isquios', glutes:'Glúteos',
  calves:'Pantorrillas', shoulders:'Hombros', core:'Core',
  back:'Espalda', chest:'Pecho', arms:'Brazos', full_body:'Cuerpo completo'
};

// Set today's date
document.getElementById('fDate').value = new Date().toISOString().slice(0,10);

function setCat(cat) {
  currentCat = cat;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.cat === cat));
  document.querySelectorAll('.compex-fields').forEach(el => el.classList.toggle('show', cat === 'compex'));
  document.querySelectorAll('.gym-fields').forEach(el => el.classList.toggle('show', cat === 'gym'));
}

function toggleMuscle(m) {
  const chip = document.querySelector('.muscle-chip[data-m="'+m+'"]');
  if(selectedMuscles.has(m)) { selectedMuscles.delete(m); chip.classList.remove('selected'); }
  else { selectedMuscles.add(m); chip.classList.add('selected'); }
}

async function loadSummary() {
  try {
    const r = await fetch(API + '/gpt/fuerza-summary?weeks=8');
    const d = await r.json();
    renderSummary(d);
  } catch(e) {
    document.getElementById('summarySection').innerHTML = '<p style="color:var(--muted);font-size:13px">Sin registros aún.</p>';
  }
}

function renderSummary(d) {
  const byCat = d.por_categoria || {};
  const sessions = d.sesiones_recientes || [];
  const compexProg = d.compex_progresion || {};

  // Category breakdown
  const catHtml = Object.entries(byCat).map(([cat, data]) =>
    '<div class="sum-box"><div class="sum-label">'+(CAT_LABELS[cat]||cat)+'</div>' +
    '<div class="sum-value">'+data.sesiones+'</div>' +
    '<div class="sum-unit">'+Math.round(data.minutos/60*10)/10+' h</div></div>'
  ).join('');

  // Compex muscle progress
  const muscleHtml = Object.entries(compexProg).map(([muscle, months]) => {
    const last = months[months.length-1];
    const first = months[0];
    const pct = Math.round((last.max_intensity / 100) * 100);
    const delta = last.max_intensity - first.max_intensity;
    return '<div class="muscle-card">' +
      '<div class="muscle-name">'+(MUSCLE_LABELS[muscle]||muscle)+'</div>' +
      '<div class="intensity-bar-wrap"><div class="intensity-bar" style="width:'+pct+'%"></div></div>' +
      '<div class="intensity-val">'+last.max_intensity+'</div>' +
      '<div class="intensity-label">intensidad máx' + (delta > 0 ? ' <span style="color:var(--green)">+'+delta+'</span>' : '') + '</div>' +
      '</div>';
  }).join('');

  // Recent sessions
  const sesHtml = sessions.map(s => {
    const muscles = (s.muscle_groups || []).map(m => MUSCLE_LABELS[m]||m).join(', ');
    return '<div class="ses-card '+(s.category||'')+'">' +
      '<div class="ses-header">' +
        '<div><div class="ses-muscles">'+(muscles||s.exercise||'—')+'</div>' +
        '<div class="ses-date">'+(s.date||'').slice(0,10)+'</div></div>' +
        '<span class="ses-cat">'+(CAT_LABELS[s.category]||s.category)+'</span>' +
      '</div>' +
      '<div class="ses-meta">' +
        (s.subcategory ? s.subcategory+' · ' : '') +
        (s.intensity ? 'intensidad '+s.intensity+' · ' : '') +
        (s.duration_min ? s.duration_min+' min' : '') +
        (s.sets ? ' · '+s.sets+'×'+s.reps+(s.weight_kg&&s.weight_kg>0?' @'+s.weight_kg+'kg':'') : '') +
        (s.rpe ? ' · RPE '+s.rpe : '') +
      '</div>' +
      (s.notes ? '<div style="font-size:11px;color:var(--muted);margin-top:4px">'+s.notes+'</div>' : '') +
      '</div>';
  }).join('');

  document.getElementById('summarySection').innerHTML =
    '<div class="summary-row">' +
      '<div class="sum-box"><div class="sum-label">Sesiones totales</div><div class="sum-value">'+d.total_sesiones+'</div></div>' +
      '<div class="sum-box"><div class="sum-label">Horas totales</div><div class="sum-value">'+d.total_horas+'</div><div class="sum-unit">h</div></div>' +
      catHtml +
    '</div>' +

    (Object.keys(compexProg).length ? '<div class="card"><div class="card-title">Progresión Compex por músculo</div><div class="muscle-progress">'+muscleHtml+'</div></div>' : '') +

    (sessions.length ? '<div class="card"><div class="card-title">Sesiones recientes</div><div class="session-list">'+sesHtml+'</div></div>' : '');
}

async function saveFuerza() {
  const body = {
    date: document.getElementById('fDate').value,
    category: currentCat,
    subcategory: currentCat === 'compex' ? document.getElementById('fSubcat').value : null,
    muscle_groups: [...selectedMuscles],
    intensity: currentCat === 'compex' ? parseInt(document.getElementById('fIntensity').value)||null : null,
    duration_min: parseInt(document.getElementById('fDuration').value)||null,
    exercise: currentCat === 'gym' ? document.getElementById('fExercise').value||null : null,
    sets: parseInt(document.getElementById('fSets').value)||null,
    reps: parseInt(document.getElementById('fReps').value)||null,
    weight_kg: parseFloat(document.getElementById('fWeight').value)||null,
    rpe: parseInt(document.getElementById('fRpe').value)||null,
    fatigue_before: parseInt(document.getElementById('fFatigue').value)||null,
    notes: document.getElementById('fNotes').value||null
  };
  if(!body.date) { alert('Fecha requerida'); return; }
  try {
    const r = await fetch(API + '/fuerza', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const res = await r.json();
    if(res.ok) {
      const msg = document.getElementById('saveMsg');
      msg.style.display = 'inline';
      setTimeout(() => msg.style.display='none', 3000);
      selectedMuscles.clear();
      document.querySelectorAll('.muscle-chip').forEach(c => c.classList.remove('selected'));
      ['fDuration','fIntensity','fExercise','fSets','fReps','fWeight','fRpe','fFatigue','fNotes'].forEach(id => {
        const el = document.getElementById(id); if(el) el.value='';
      });
      loadSummary();
    }
  } catch(e) { alert('Error: '+e.message); }
}

loadSummary();
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 9 — Bicicleta y Mantenimiento
# ═══════════════════════════════════════════════════════════════════════════════

class GearIn(BaseModel):
    gear_id: Optional[str] = None
    name: str
    type: str  # chain, tire_front, tire_rear, cassette, pedals, cleats, cable, brake_pad, other
    bike_id: Optional[str] = "orbea-avant-2019"
    installed_date: Optional[str] = None
    km_at_install: Optional[int] = 0
    km_limit: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    notes: Optional[str] = None

class MaintenanceIn(BaseModel):
    bike_id: Optional[str] = "orbea-avant-2019"
    gear_id: Optional[str] = None
    type: str  # chain_change, tire_change, cable_change, derailleur_adj, brake_adj, accident, other
    description: str
    date: str
    km_at_service: Optional[int] = None
    cost_mxn: Optional[float] = None
    shop: Optional[str] = None
    notes: Optional[str] = None

class AccidentIn(BaseModel):
    date: str
    description: str
    damage: Optional[str] = None
    repair: Optional[str] = None
    cost_mxn: Optional[float] = None
    km_at_accident: Optional[int] = None
    notes: Optional[str] = None


# ── Tabla accidentes (migrate) ────────────────────────────────────────────────

def _ensure_accidents_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accidents (
                id           SERIAL PRIMARY KEY,
                date         DATE NOT NULL,
                description  TEXT,
                damage       TEXT,
                repair       TEXT,
                cost_mxn     DECIMAL(10,2),
                km_at_accident INT,
                notes        TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Add columns to gear table if missing
        for col, typ in [("brand","TEXT"),("model","TEXT")]:
            try:
                cur.execute(f"ALTER TABLE gear ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass

# ── GET /gpt/gear-status ──────────────────────────────────────────────────────

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
                SELECT COALESCE(SUM(distance_km),0) FROM sessions
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

@app.get("/gear", response_class=HTMLResponse)
def gear_page():
    return HTMLResponse(GEAR_HTML)


GEAR_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Equipo</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--green:#1a9e6e;--yellow:#f2a623;
  --mono:'Cabinet Grotesk',sans-serif;--serif:'Instrument Serif',serif;
  --shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
nav{background:var(--surface);border-bottom:1px solid var(--border);padding:0 28px;height:56px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.nav-logo{display:flex;align-items:center;gap:10px}
.nav-icon{width:32px;height:32px;background:var(--accent);border-radius:8px;display:flex;align-items:center;justify-content:center}
.nav-icon svg{width:18px;height:18px;stroke:white;fill:none;stroke-width:2;stroke-linecap:round}
.nav-name{font-weight:700;font-size:16px;letter-spacing:-.02em}
.nav-sub{font-size:11px;color:var(--muted)}
.nav-links{display:flex;gap:4px;flex-wrap:wrap}
.nav-link{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
.summary-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.sum-box{background:var(--surface2);border-radius:10px;padding:14px}
.sum-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:3px}
.sum-value{font-size:22px;font-weight:700}
.sum-unit{font-size:10px;color:var(--muted)}
/* Component cards */
.components-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.comp-card{background:var(--surface2);border-radius:12px;padding:16px;border-left:4px solid var(--border)}
.comp-card.red{border-left-color:var(--accent)}
.comp-card.yellow{border-left-color:var(--yellow)}
.comp-card.green{border-left-color:var(--green)}
.comp-name{font-size:13px;font-weight:700;margin-bottom:2px}
.comp-type{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px}
.comp-bar-wrap{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:6px}
.comp-bar{height:100%;border-radius:3px;transition:width .5s ease}
.comp-bar.red{background:var(--accent)}
.comp-bar.yellow{background:var(--yellow)}
.comp-bar.green{background:var(--green)}
.comp-meta{font-size:11px;color:var(--muted);display:flex;justify-content:space-between}
.comp-km{font-size:12px;font-weight:600;margin-top:4px}
/* Maintenance table */
.maint-table{width:100%;border-collapse:collapse;font-size:12px}
.maint-table th{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:500;text-align:left;padding:0 8px 8px 0;border-bottom:1px solid var(--border)}
.maint-table td{padding:8px 8px 8px 0;border-bottom:1px solid var(--border)}
.maint-table tr:last-child td{border-bottom:none}
/* Add form */
.form-section{background:var(--surface2);border-radius:10px;padding:16px;margin-bottom:12px}
.form-section-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:12px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group.full{grid-column:1/-1}
.form-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.form-input{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;
  font-family:var(--mono);font-size:13px;color:var(--text);outline:none;transition:border .15s}
.form-input:focus{border-color:var(--accent)}
.save-btn{padding:10px 20px;background:var(--accent);color:white;border:none;border-radius:8px;
  font-family:var(--mono);font-size:12px;font-weight:700;cursor:pointer;transition:opacity .15s}
.save-btn:hover{opacity:.85}
.type-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;background:var(--surface);color:var(--muted)}
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:600px){.summary-row{grid-template-columns:1fr 1fr}.form-grid{grid-template-columns:1fr}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link active">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Equipo y mantenimiento</div>
  <div class="page-sub">Orbea Avant 2019 · Shimano Claris 8v</div>

  <div id="mainContent"><div class="loading"><div class="spinner"></div>Cargando equipo...</div></div>

  <!-- Agregar componente -->
  <div class="card" style="margin-top:16px">
    <div class="card-title">Registrar componente</div>
    <div class="form-section">
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">Nombre</label>
          <input class="form-input" id="gName" placeholder="Cadena Shimano CN-HG71">
        </div>
        <div class="form-group">
          <label class="form-label">Tipo</label>
          <select class="form-input" id="gType">
            <option value="chain">Cadena</option>
            <option value="tire_front">Llanta delantera</option>
            <option value="tire_rear">Llanta trasera</option>
            <option value="cassette">Cassette</option>
            <option value="pedals">Pedales</option>
            <option value="cleats">Calas</option>
            <option value="cable">Cable</option>
            <option value="brake_pad">Pastillas de freno</option>
            <option value="other">Otro</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Fecha instalación</label>
          <input class="form-input" id="gDate" type="date">
        </div>
        <div class="form-group">
          <label class="form-label">km al instalar</label>
          <input class="form-input" id="gKmInstall" type="number" placeholder="0">
        </div>
        <div class="form-group">
          <label class="form-label">Vida útil (km)</label>
          <input class="form-input" id="gKmLimit" type="number" placeholder="2500">
        </div>
        <div class="form-group">
          <label class="form-label">Notas</label>
          <input class="form-input" id="gNotes" placeholder="Vittoria Rubino Pro...">
        </div>
      </div>
      <button class="save-btn" style="margin-top:12px" onclick="saveGear()">Agregar componente</button>
    </div>
  </div>

  <!-- Registrar mantenimiento -->
  <div class="card">
    <div class="card-title">Registrar mantenimiento</div>
    <div class="form-section">
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">Tipo</label>
          <select class="form-input" id="mType">
            <option value="chain_change">Cambio de cadena</option>
            <option value="tire_change">Cambio de llanta</option>
            <option value="cable_change">Cambio de cable</option>
            <option value="derailleur_adj">Ajuste desviador</option>
            <option value="brake_adj">Ajuste frenos</option>
            <option value="accident">Accidente/caída</option>
            <option value="other">Otro</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Fecha</label>
          <input class="form-input" id="mDate" type="date">
        </div>
        <div class="form-group full">
          <label class="form-label">Descripción</label>
          <input class="form-input" id="mDesc" placeholder="Cadena a 2,500 km, desgaste normal">
        </div>
        <div class="form-group">
          <label class="form-label">km al servicio</label>
          <input class="form-input" id="mKm" type="number" placeholder="2500">
        </div>
        <div class="form-group">
          <label class="form-label">Costo (MXN)</label>
          <input class="form-input" id="mCost" type="number" placeholder="350">
        </div>
        <div class="form-group">
          <label class="form-label">Taller / lugar</label>
          <input class="form-input" id="mShop" placeholder="Casa / taller X">
        </div>
      </div>
      <button class="save-btn" style="margin-top:12px" onclick="saveMaintenance()">Guardar mantenimiento</button>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;

const TYPE_LABELS = {
  chain:'Cadena', tire_front:'Llanta delantera', tire_rear:'Llanta trasera',
  cassette:'Cassette', pedals:'Pedales', cleats:'Calas', cable:'Cable',
  brake_pad:'Pastillas', other:'Otro'
};

async function load() {
  try {
    const r = await fetch(API + '/gpt/gear-status');
    const d = await r.json();
    render(d);
  } catch(e) {
    document.getElementById('mainContent').innerHTML = '<p style="color:var(--muted)">Error: '+e.message+'</p>';
  }
}

function render(d) {
  const comps = d.componentes || [];
  const maint = d.mantenimiento_reciente || [];
  const acc = d.accidentes || [];
  const alerts = d.alertas || [];

  // Summary
  const alertCount = alerts.length;
  const redCount = comps.filter(c => c.status === 'red').length;

  // Components
  const compsHtml = comps.length ? comps.map(c => {
    const pct = c.pct_used || 0;
    const kmRem = c.km_remaining;
    return '<div class="comp-card '+c.status+'">' +
      '<div class="comp-name">'+c.status_emoji+' '+c.name+'</div>' +
      '<div class="comp-type">'+(TYPE_LABELS[c.type]||c.type)+'</div>' +
      (c.km_limit ? '<div class="comp-bar-wrap"><div class="comp-bar '+c.status+'" style="width:'+Math.min(100,pct)+'%"></div></div>' : '') +
      '<div class="comp-meta">' +
        '<span>'+(c.installed_date||'').slice(0,10)+'</span>' +
        '<span>'+(c.km_limit ? pct+'%' : 'sin límite')+'</span>' +
      '</div>' +
      '<div class="comp-km">'+(c.km_used||0)+' km usados'+(kmRem!=null ? ' · '+kmRem+' restantes' : '')+'</div>' +
      (c.notes ? '<div style="font-size:11px;color:var(--muted);margin-top:4px">'+c.notes+'</div>' : '') +
      '<div style="display:flex;gap:6px;margin-top:8px">' +
        '<button onclick="editKmLimit(''+c.gear_id+'','+c.km_limit+')" style="padding:3px 10px;font-size:10px;font-family:var(--mono);border:1px solid var(--border);border-radius:6px;background:var(--surface);cursor:pointer">Editar km límite</button>' +
        '<button onclick="retireGear(''+c.gear_id+'',''+c.name+'')" style="padding:3px 10px;font-size:10px;font-family:var(--mono);border:1px solid var(--accent);border-radius:6px;background:white;color:var(--accent);cursor:pointer">Retirar</button>' +
      '</div>' +
      '</div>';
  }).join('') : '<p style="color:var(--muted);font-size:13px">Sin componentes registrados. Agrega uno abajo.</p>';

  // Maintenance table
  const maintHtml = maint.length ? '<table class="maint-table"><thead><tr>' +
    '<th>Fecha</th><th>Tipo</th><th>Descripción</th><th>km</th><th>Costo MXN</th></tr></thead><tbody>' +
    maint.map(m => '<tr><td>'+(m.date||'').slice(0,10)+'</td>' +
      '<td><span class="type-badge">'+m.type+'</span></td>' +
      '<td>'+m.description+'</td>' +
      '<td>'+(m.km_at_service||'—')+'</td>' +
      '<td>'+(m.cost_mxn ? '$'+m.cost_mxn : '—')+'</td></tr>'
    ).join('') + '</tbody></table>'
    : '<p style="color:var(--muted);font-size:13px">Sin registros de mantenimiento.</p>';

  // Accidents
  const accHtml = acc.length ? acc.map(a =>
    '<div style="padding:10px 12px;background:var(--surface2);border-radius:8px;border-left:3px solid var(--accent);margin-bottom:8px;font-size:12px">' +
    '<strong>'+(a.date||'').slice(0,10)+'</strong> — '+a.description+
    (a.damage ? '<br>Daño: '+a.damage : '') +
    (a.repair ? '<br>Reparación: '+a.repair : '') +
    (a.cost_mxn ? '<br>Costo: $'+a.cost_mxn+' MXN' : '') +
    '</div>'
  ).join('') : '<p style="color:var(--muted);font-size:13px">Sin accidentes registrados.</p>';

  document.getElementById('mainContent').innerHTML = `
    <div class="summary-row">
      <div class="sum-box"><div class="sum-label">km totales bici</div><div class="sum-value">${Math.round(d.total_km_bici||0)}</div><div class="sum-unit">km</div></div>
      <div class="sum-box"><div class="sum-label">Alertas activas</div><div class="sum-value" style="color:${alertCount>0?'var(--accent)':'var(--green)'}">${alertCount}</div></div>
      <div class="sum-box"><div class="sum-label">Costo total</div><div class="sum-value">$${Math.round(d.costo_total_mxn||0)}</div><div class="sum-unit">MXN</div></div>
      <div class="sum-box"><div class="sum-label">Costo/km</div><div class="sum-value">$${d.costo_por_km_mxn||0}</div><div class="sum-unit">MXN/km</div></div>
    </div>

    <div class="card">
      <div class="card-title">Componentes activos</div>
      <div class="components-grid">${compsHtml}</div>
    </div>

    <div class="card">
      <div class="card-title">Historial de mantenimiento</div>
      ${maintHtml}
    </div>

    <div class="card">
      <div class="card-title">Accidentes y caídas</div>
      ${accHtml}
    </div>
  `;
}

async function editKmLimit(gearId, current) {
  const val = prompt('Nuevo límite de km (actual: ' + (current||'sin límite') + '):', current||'');
  if(val === null) return;
  const km = parseInt(val);
  if(isNaN(km)) { alert('Número inválido'); return; }
  const r = await fetch(API + '/gear/' + gearId, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({km_limit: km})
  });
  const d = await r.json();
  if(d.ok) load();
  else alert('Error: ' + JSON.stringify(d));
}

async function retireGear(gearId, name) {
  if(!confirm('¿Retirar "' + name + '"? Se marcará como inactivo.')) return;
  const today = new Date().toISOString().slice(0,10);
  const r = await fetch(API + '/gear/' + gearId, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({retired_date: today})
  });
  const d = await r.json();
  if(d.ok) load();
  else alert('Error: ' + JSON.stringify(d));
}

async function saveGear() {
  const body = {
    name: document.getElementById('gName').value,
    type: document.getElementById('gType').value,
    installed_date: document.getElementById('gDate').value || null,
    km_at_install: parseInt(document.getElementById('gKmInstall').value) || 0,
    km_limit: parseInt(document.getElementById('gKmLimit').value) || null,
    notes: document.getElementById('gNotes').value || null
  };
  if(!body.name) { alert('Nombre requerido'); return; }
  const r = await fetch(API + '/gear', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await r.json();
  if(d.ok) { load(); ['gName','gDate','gKmInstall','gKmLimit','gNotes'].forEach(id => document.getElementById(id).value=''); }
  else alert('Error guardando');
}

async function saveMaintenance() {
  const body = {
    type: document.getElementById('mType').value,
    description: document.getElementById('mDesc').value,
    date: document.getElementById('mDate').value,
    km_at_service: parseInt(document.getElementById('mKm').value) || null,
    cost_mxn: parseFloat(document.getElementById('mCost').value) || null,
    shop: document.getElementById('mShop').value || null
  };
  if(!body.description || !body.date) { alert('Descripción y fecha requeridas'); return; }
  const r = await fetch(API + '/maintenance', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await r.json();
  if(d.ok) { load(); ['mDesc','mDate','mKm','mCost','mShop'].forEach(id => document.getElementById(id).value=''); }
  else alert('Error guardando');
}

load();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN / BACKUP / DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/diagnostics")
def admin_diagnostics():
    """Diagnóstico rápido de la API y conteos de todas las tablas."""
    conn = get_db()
    if not conn:
        return {"api": "ok", "db": "error", "detail": "DB no disponible"}
    try:
        counts = {}
        tables = ["sessions", "session_records", "post_session", "gear",
                  "maintenance", "fuerza", "wellness", "accidents",
                  "athlete_profile", "athlete_tests", "recovery"]
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


@app.get("/admin/backup")
def admin_backup():
    """Exporta todas las tablas en JSON. Usar para respaldo manual."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    tables = ["sessions", "routes", "post_session", "gear", "maintenance",
              "recovery", "fuerza", "wellness", "accidents",
              "athlete_profile", "athlete_tests"]
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

@app.get("/progress", response_class=HTMLResponse)
def progress_page():
    return HTMLResponse(PROGRESS_HTML)


PROGRESS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Progreso Histórico</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--blue:#2563eb;--mono:'Cabinet Grotesk',sans-serif;
  --serif:'Instrument Serif',serif;--shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
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
.page{max-width:960px;margin:0 auto;padding:28px}
.page-title{font-family:var(--serif);font-style:italic;font-size:28px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--muted);margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:16px}
.sport-tabs{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}
.sport-tab{padding:6px 16px;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}
.sport-tab.active{background:var(--text);color:white;border-color:var(--text)}
.chart-wrap{height:200px;position:relative}
.chart-wrap-tall{height:260px;position:relative}
.loading{text-align:center;padding:40px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
/* Baseline hero */
.baseline-hero{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:0}
.bl-side{background:var(--surface2);border-radius:10px;padding:16px}
.bl-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:10px}
.bl-metrics{display:flex;flex-direction:column;gap:8px}
.bl-row{display:flex;justify-content:space-between;align-items:center}
.bl-key{font-size:12px;color:var(--muted)}
.bl-val{font-size:14px;font-weight:700}
.bl-delta{font-size:12px;font-weight:600;padding:2px 8px;border-radius:4px}
.delta-pos{background:#e8f5e9;color:var(--green)}
.delta-neg{background:#ffeaea;color:var(--accent)}
.delta-neu{background:var(--surface2);color:var(--muted)}
/* Signal card */
.signal-card{padding:16px 20px;border-radius:10px;margin-bottom:16px;font-size:14px;font-weight:600;line-height:1.4}
.signal-ok{background:#e8f5e9;border-left:4px solid var(--green);color:#1a5c35}
.signal-progress{background:#fff8e8;border-left:4px solid var(--accent2);color:#7a5200}
.signal-stable{background:var(--surface2);border-left:4px solid var(--muted);color:var(--muted)}
/* Mars Index bars */
.mars-index{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.idx-item{display:flex;flex-direction:column;gap:6px}
.idx-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.idx-bar-wrap{height:8px;background:var(--border);border-radius:4px;overflow:hidden}
.idx-bar{height:100%;border-radius:4px;transition:width 1s ease}
.idx-val{font-size:12px;font-weight:600;color:var(--muted)}
/* Year cards */
.year-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.year-card{background:var(--surface2);border-radius:10px;padding:14px}
.year-label{font-size:11px;color:var(--muted);margin-bottom:4px}
.year-km{font-size:22px;font-weight:700;line-height:1}
.year-unit{font-size:10px;color:var(--muted)}
.year-meta{font-size:11px;color:var(--muted);margin-top:4px}
/* Records */
.records-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.rec-card{background:var(--surface2);border-radius:10px;padding:14px}
.rec-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.rec-value{font-size:20px;font-weight:700}
.rec-unit{font-size:11px;color:var(--muted)}
.rec-date{font-size:11px;color:var(--muted);margin-top:4px}
@media(max-width:600px){.baseline-hero{grid-template-columns:1fr}.mars-index{grid-template-columns:1fr}}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/dashboard" class="nav-link">Rutas</a>
    <a href="/progress" class="nav-link active">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Progreso histórico</div>
  <div class="page-sub">Línea base: mayo 2026 · señal clave: 20-21 km/h con menos de 140 bpm</div>

  <div class="sport-tabs" id="sportTabs">
    <div class="sport-tab active" data-sport="cycling" onclick="setSport('cycling')">Ciclismo</div>
    <div class="sport-tab" data-sport="running" onclick="setSport('running')">Running</div>
    <div class="sport-tab" data-sport="training" onclick="setSport('training')">Entrenamiento</div>
  </div>

  <!-- Signal aeróbica -->
  <div id="signalCard"></div>

  <!-- Comparativa vs baseline -->
  <div class="card">
    <div class="card-title">vs Línea base mayo 2026</div>
    <div id="baselineSection"><div class="loading"><div class="spinner"></div></div></div>
  </div>

  <!-- Indicadores Mars -->
  <div class="card">
    <div class="card-title">Indicador Mars</div>
    <div id="marsIndex" class="mars-index"><div class="loading"><div class="spinner"></div></div></div>
  </div>

  <!-- Eficiencia histórica -->
  <div class="card">
    <div class="card-title">Eficiencia aeróbica mensual (km/h ÷ bpm)</div>
    <div class="chart-wrap-tall"><canvas id="effChart"></canvas></div>
  </div>

  <!-- Volumen por año -->
  <div class="card">
    <div class="card-title">Volumen por año</div>
    <div id="yearCards" class="year-grid"><div class="loading"><div class="spinner"></div></div></div>
    <div class="chart-wrap"><canvas id="yearChart"></canvas></div>
  </div>

  <!-- Récords personales -->
  <div class="card">
    <div class="card-title">Récords personales</div>
    <div id="recordsGrid" class="records-grid"><div class="loading"><div class="spinner"></div></div></div>
  </div>
</div>

<script>
const API = window.location.origin;
let currentSport = 'cycling';
let yearChartInst = null, effChartInst = null;

function setSport(sport) {
  currentSport = sport;
  document.querySelectorAll('.sport-tab').forEach(t => t.classList.toggle('active', t.dataset.sport === sport));
  loadAll();
}

async function loadAll() {
  loadBaseline();
  loadEfficiency();
  loadYearStats();
  loadRecords();
}

function fmt(v, dec=1) { return v != null ? Number(v).toFixed(dec) : '—'; }
function deltaClass(v, inverse=false) {
  if(v == null) return 'delta-neu';
  const pos = inverse ? v < 0 : v > 0;
  return pos ? 'delta-pos' : (v === 0 ? 'delta-neu' : 'delta-neg');
}
function deltaStr(v, unit='') {
  if(v == null) return '—';
  return (v > 0 ? '+' : '') + Number(v).toFixed(2) + unit;
}

async function loadBaseline() {
  try {
    const r = await fetch(API + '/gpt/baseline-compare?sport=' + currentSport);
    const d = await r.json();
    const bl = d.baseline || {};
    const ac = d.actual_4_semanas || {};
    const dl = d.deltas || {};

    // Signal card
    const sigEl = document.getElementById('signalCard');
    const sigClass = d.estado === 'mejorando' ? 'signal-ok' : d.estado === 'en_progreso' ? 'signal-progress' : 'signal-stable';
    sigEl.innerHTML = '<div class="signal-card ' + sigClass + '">' + (d.senal_aerobica || '') + '</div>';

    // Baseline vs actual table
    const bsEl = document.getElementById('baselineSection');
    const rows = [
      {k:'fc_promedio', label:'FC promedio', unit:'bpm', inv:true},
      {k:'vel_promedio', label:'Velocidad promedio', unit:'km/h', inv:false},
      {k:'cadencia_promedio', label:'Cadencia promedio', unit:'rpm', inv:false},
      {k:'eficiencia_ratio', label:'Eficiencia (vel/FC)', unit:'', inv:false, dec:4},
    ];
    const makeCol = (label, vals) => '<div class="bl-side"><div class="bl-label">' + label + '</div><div class="bl-metrics">' +
      rows.map(row => {
        const v = vals[row.k];
        return '<div class="bl-row"><span class="bl-key">' + row.label + '</span><span class="bl-val">' + fmt(v, row.dec||1) + ' <span style="font-weight:400;font-size:10px;color:var(--muted)">' + row.unit + '</span></span></div>';
      }).join('') + '</div></div>';

    const deltaCol = '<div class="bl-side" style="background:#f9f7f4"><div class="bl-label">Delta vs base</div><div class="bl-metrics">' +
      rows.map(row => {
        const dv = dl[row.k + '_delta'];
        const dc = deltaClass(dv, row.inv);
        return '<div class="bl-row"><span class="bl-key">' + row.label + '</span><span class="bl-delta ' + dc + '">' + deltaStr(dv, ' ' + row.unit) + '</span></div>';
      }).join('') + '<div class="bl-row" style="margin-top:8px"><span class="bl-key" style="font-size:11px">Sesiones (4 sem)</span><span class="bl-val">' + (ac.sesiones_4_semanas||0) + '</span></div>' +
      '<div class="bl-row"><span class="bl-key" style="font-size:11px">km (4 sem)</span><span class="bl-val">' + fmt(ac.km_4_semanas,1) + '</span></div>' +
      '</div></div>';

    bsEl.innerHTML = '<div class="baseline-hero">' + makeCol('Mayo 2026 (base)', bl) + deltaCol + '</div>';

    // Mars Index
    const idxEl = document.getElementById('marsIndex');
    const ind = d;
    // Compute scores from deltas
    const fcDelta = dl['fc_promedio_delta'] || 0;
    const velDelta = dl['vel_promedio_delta'] || 0;
    const cadDelta = dl['cadencia_promedio_delta'] || 0;
    const effDelta = dl['eficiencia_ratio_pct'] || 0;

    const motorScore = Math.max(0, Math.min(100, 50 + effDelta * 5));
    const cadScore = Math.max(0, Math.min(100, 50 + (Number(ac.cadencia_promedio||74) - 74) * 4));
    const fcScore = Math.max(0, Math.min(100, 50 + (-fcDelta) * 3));
    const velScore = Math.max(0, Math.min(100, 50 + velDelta * 10));

    const bars = [
      {label:'Motor aeróbico', score:motorScore, color:'#2563eb'},
      {label:'Cadencia', score:cadScore, color:'#1a9e6e'},
      {label:'FC relativa', score:fcScore, color:'#f2a623'},
      {label:'Velocidad', score:velScore, color:'#e8593c'},
    ];
    idxEl.innerHTML = bars.map(b => '<div class="idx-item">' +
      '<div class="idx-label">' + b.label + '</div>' +
      '<div class="idx-bar-wrap"><div class="idx-bar" style="width:' + b.score + '%;background:' + b.color + '"></div></div>' +
      '<div class="idx-val">' + Math.round(b.score) + '/100</div>' +
      '</div>').join('');

  } catch(e) {
    document.getElementById('baselineSection').innerHTML = '<p style="color:var(--muted);font-size:13px">Error: ' + e.message + '</p>';
  }
}

async function loadEfficiency() {
  try {
    const r = await fetch(API + '/gpt/historical-progress?sport=' + currentSport + '&months=24');
    const d = await r.json();
    const months = (d.data || []).filter(m => m.sesiones >= 2);
    if(!months.length) return;
    if(effChartInst) effChartInst.destroy();
    const ctx = document.getElementById('effChart').getContext('2d');
    effChartInst = new Chart(ctx, {
      type: 'line',
      data: {
        labels: months.map(m => m.mes),
        datasets: [
          {label:'FC promedio', data: months.map(m => m.fc_promedio),
           borderColor:'#e8593c', backgroundColor:'transparent', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'yfc'},
          {label:'Velocidad km/h', data: months.map(m => m.vel_promedio),
           borderColor:'#2563eb', backgroundColor:'transparent', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'yspd'},
          {label:'Cadencia rpm', data: months.map(m => m.cadencia_promedio),
           borderColor:'#1a9e6e', backgroundColor:'transparent', borderWidth:2, pointRadius:3, tension:0.3, yAxisID:'ycad'},
        ]
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        interaction:{mode:'index', intersect:false},
        plugins:{legend:{labels:{font:{family:'Cabinet Grotesk',size:11}}}},
        scales:{
          x:{grid:{display:false}, ticks:{font:{family:'Cabinet Grotesk',size:10}, maxTicksLimit:12}},
          yfc:{position:'left', title:{display:true,text:'bpm',font:{family:'Cabinet Grotesk',size:10}}, ticks:{font:{family:'Cabinet Grotesk',size:10}}, grid:{color:'#f0ede8'}},
          yspd:{position:'right', title:{display:true,text:'km/h',font:{family:'Cabinet Grotesk',size:10}}, ticks:{font:{family:'Cabinet Grotesk',size:10}}, grid:{display:false}},
          ycad:{display:false}
        }
      }
    });
  } catch(e) {}
}

async function loadYearStats() {
  const el = document.getElementById('yearCards');
  try {
    const sportParam = currentSport === 'all' ? '' : '&sport=' + currentSport;
    const r = await fetch(API + '/stats/yearly?limit=20' + sportParam);
    const data = await r.json();
    const years = data.years || [];
    if(!years.length) { el.innerHTML = '<p style="color:var(--muted);font-size:13px">Sin datos</p>'; return; }
    el.innerHTML = years.map(y =>
      '<div class="year-card"><div class="year-label">' + y.year + '</div>' +
      '<div class="year-km">' + (y.km_total||0) + '</div><div class="year-unit">km</div>' +
      '<div class="year-meta">' + (y.horas_total||0) + 'h · ' + (y.sesiones||0) + ' ses</div></div>'
    ).join('');
    if(yearChartInst) yearChartInst.destroy();
    const ctx = document.getElementById('yearChart').getContext('2d');
    yearChartInst = new Chart(ctx, {
      type:'bar',
      data:{
        labels: years.map(y => y.year),
        datasets:[{
          label:'km', data: years.map(y => y.km_total||0),
          backgroundColor: years.map((_,i) => i===years.length-1 ? 'rgba(232,89,60,.9)' : 'rgba(232,89,60,.35)'),
          borderRadius:4
        }]
      },
      options:{responsive:true, maintainAspectRatio:false,
        plugins:{legend:{display:false}},
        scales:{x:{grid:{display:false},ticks:{font:{family:'Cabinet Grotesk',size:11}}},
                y:{grid:{color:'#f0ede8'},ticks:{font:{family:'Cabinet Grotesk',size:11}}}}}
    });
  } catch(e) { el.innerHTML = '<p style="color:var(--muted);font-size:13px">Error</p>'; }
}

async function loadRecords() {
  const el = document.getElementById('recordsGrid');
  try {
    const r = await fetch(API + '/stats/records?sport=' + currentSport);
    const data = await r.json();
    const recs = data.records || {};
    el.innerHTML = [
      {key:'max_distance', label:'Mayor distancia', unit:'km', fmt: v => Number(v).toFixed(1)},
      {key:'max_duration', label:'Sesión más larga', unit:'', fmt: v => { const s=parseInt(v); return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m'; }},
      {key:'max_ascent', label:'Mayor ascenso', unit:'m', fmt: v => parseInt(v)},
      {key:'max_speed', label:'Mayor velocidad', unit:'km/h', fmt: v => Number(v).toFixed(1)},
      {key:'min_hr', label:'FC mínima sesión', unit:'bpm', fmt: v => parseInt(v)},
    ].map(r => {
      const v = recs[r.key];
      if(!v) return '';
      return '<div class="rec-card"><div class="rec-label">' + r.label + '</div>' +
             '<div class="rec-value">' + r.fmt(v.value) + ' <span class="rec-unit">' + r.unit + '</span></div>' +
             '<div class="rec-date">' + (v.date||'') + '</div></div>';
    }).join('');
  } catch(e) { el.innerHTML = '<p style="color:var(--muted);font-size:13px">Error cargando récords</p>'; }
}

loadAll();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# ETAPA 3b — Pantalla Detalle de Sesión
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_detail(session_id: str):
    html = SESSION_DETAIL_HTML.replace("__SESSION_ID__", session_id)
    return HTMLResponse(html)


SESSION_DETAIL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bitácora">
<meta name="theme-color" content="#1a1816">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js')}</script>
<title>Bitácora — Sesión</title>
<link href="https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@400;500;700;900&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#f5f3ef;--surface:#fff;--surface2:#f0ede8;--border:#e8e4de;
  --text:#1a1816;--muted:#9a9590;--accent:#e8593c;--accent2:#f2a623;
  --green:#1a9e6e;--blue:#2563eb;--mono:'Cabinet Grotesk',sans-serif;
  --serif:'Instrument Serif',serif;--shadow:0 1px 3px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}

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

.page{max-width:900px;margin:0 auto;padding:28px}

/* HERO */
.hero{background:linear-gradient(135deg,#1a1816 0%,#2d2926 100%);border-radius:16px;
  padding:28px;color:white;margin-bottom:24px;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;inset:0;
  background-image:repeating-linear-gradient(45deg,transparent,transparent 8px,rgba(255,255,255,.03) 8px,rgba(255,255,255,.03) 9px)}
.hero-inner{position:relative;z-index:1}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:20px;gap:16px}
.hero-sport{font-size:9px;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
  background:rgba(232,89,60,.3);color:#ff9980;padding:3px 10px;border-radius:4px;display:inline-block;margin-bottom:8px}
.hero-name{font-family:var(--serif);font-style:italic;font-size:26px;line-height:1.2;margin-bottom:4px}
.hero-date{font-size:12px;opacity:.5}
.hero-back{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);
  border-radius:8px;color:white;font-family:var(--mono);font-size:11px;padding:6px 14px;
  cursor:pointer;text-decoration:none;white-space:nowrap;transition:background .15s}
.hero-back:hover{background:rgba(255,255,255,.2)}
.hero-metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:16px}
.hm{}
.hm-label{font-size:9px;text-transform:uppercase;letter-spacing:.12em;opacity:.5;margin-bottom:4px}
.hm-value{font-size:20px;font-weight:700;line-height:1}
.hm-unit{font-size:10px;opacity:.5}

/* CARDS */
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}
.card-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  color:var(--muted);margin-bottom:16px}

/* ZONES */
.zones-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.zone-card{background:var(--surface2);border-radius:10px;padding:12px}
.zone-name{font-size:10px;color:var(--muted);margin-bottom:4px}
.zone-time{font-size:18px;font-weight:700;margin-bottom:2px}
.zone-pct{font-size:11px;color:var(--muted);margin-bottom:8px}
.zone-bar{height:4px;border-radius:2px;background:var(--border);overflow:hidden}
.zone-fill{height:100%;border-radius:2px;transition:width 1s ease}

/* LAPS */
.laps-table{width:100%;border-collapse:collapse;font-size:12px}
.laps-table th{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);
  font-weight:500;text-align:left;padding:0 8px 10px 0;border-bottom:2px solid var(--border)}
.laps-table td{padding:9px 8px 9px 0;border-bottom:1px solid var(--border)}
.laps-table tr:last-child td{border-bottom:none}
.laps-table tr:hover td{background:var(--surface2)}

/* INSIGHTS */
.insight{padding:12px 14px;background:var(--surface2);border-radius:8px;
  border-left:3px solid var(--accent2);font-size:13px;color:var(--text);
  line-height:1.5;margin-bottom:8px}

/* CHARTS */
.chart-wrap{height:140px;position:relative;margin-bottom:16px}
.chart-label{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:8px}

/* NO DATA */
.no-charts{background:var(--surface2);border-radius:10px;padding:24px;text-align:center}
.no-charts p{font-size:13px;color:var(--muted);margin-bottom:16px}
.upload-btn{display:inline-block;background:var(--accent);color:white;border:none;
  border-radius:8px;padding:10px 20px;font-family:var(--mono);font-size:13px;font-weight:500;
  cursor:pointer;text-decoration:none;transition:opacity .15s}
.upload-btn:hover{opacity:.85}

/* LOADING */
.loading{text-align:center;padding:60px;color:var(--muted)}
.spinner{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 14px}
@keyframes spin{to{transform:rotate(360deg)}}

/* POST-SESSION FORM */
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group.full{grid-column:1/-1}
.form-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.form-input{background:var(--surface2);border:1px solid var(--border);border-radius:8px;
  padding:9px 12px;font-family:var(--mono);font-size:13px;color:var(--text);outline:none;transition:border .15s}
.form-input:focus{border-color:var(--accent)}
.form-textarea{resize:vertical;min-height:72px}
.rpe-grid{display:grid;grid-template-columns:repeat(10,1fr);gap:4px}
.rpe-btn{padding:8px 0;border:1px solid var(--border);border-radius:6px;background:var(--surface2);
  font-family:var(--mono);font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;text-align:center;transition:all .15s}
.rpe-btn:hover{border-color:var(--accent);color:var(--accent)}
.rpe-btn.selected{background:var(--accent);border-color:var(--accent);color:white}
.conditions-grid{display:flex;flex-wrap:wrap;gap:6px}
.cond-btn{padding:5px 12px;border:1px solid var(--border);border-radius:20px;background:var(--surface2);
  font-family:var(--mono);font-size:11px;color:var(--muted);cursor:pointer;transition:all .15s}
.cond-btn.selected{background:var(--text);border-color:var(--text);color:white}
.save-btn{width:100%;padding:12px;background:var(--accent);color:white;border:none;border-radius:10px;
  font-family:var(--mono);font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s;margin-top:4px}
.save-btn:hover{opacity:.85}
.save-btn:disabled{opacity:.5;cursor:not-allowed}
.form-saved{background:#e8f5e9;border:1px solid #a5d6a7;border-radius:8px;padding:10px 14px;
  font-size:13px;color:#2e7d32;text-align:center;margin-top:8px;display:none}
.sweat-info{background:var(--surface2);border-radius:8px;padding:10px 14px;font-size:12px;
  color:var(--muted);margin-top:8px;display:none}
.form-section-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  color:var(--muted);margin:16px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}

@media(max-width:700px){
  .hero-metrics{grid-template-columns:repeat(3,1fr)}
  .zones-grid{grid-template-columns:repeat(2,1fr)}
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
    <a href="/activities" class="nav-link">Actividades</a>
    <a href="/dashboard" class="nav-link">Rutas</a>
    <a href="/progress" class="nav-link">Progreso</a>
    <a href="/calendar" class="nav-link">Calendario</a>
    <a href="/performance" class="nav-link">Rendimiento</a>
    <a href="/gear" class="nav-link">Equipo</a>
    <a href="/fuerza" class="nav-link">Fuerza</a>
    <a href="/wellness" class="nav-link">Wellness</a>
  </div>
</nav>

<div class="page" id="page">
  <div class="loading"><div class="spinner"></div>Cargando sesión...</div>
</div>

<script>
const API = window.location.origin;
const SESSION_ID = '__SESSION_ID__';
const $ = id => document.getElementById(id);

const SPORT_LABELS = {cycling:'Ciclismo',running:'Running',walking:'Caminata',
  training:'Entrenamiento',swimming:'Natación',generic:'Genérico'};
const ZONE_COLORS = ['#4a9eff','#888','#3dd68c','#f2a623','#e8593c','#ff3b3b'];

function fmtDate(iso) {
  if(!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('es-MX', {weekday:'long',year:'numeric',month:'long',day:'numeric'});
}

function fmtDur(s) {
  if(!s) return '—';
  return String(Math.floor(s/3600)).padStart(2,'0') + 'h ' + String(Math.floor((s%3600)/60)).padStart(2,'0') + 'm';
}

async function loadSession() {
  try {
    // Load session data from DB
    const [sessRes, resultRes] = await Promise.all([
      fetch(`${API}/sessions?limit=1&offset=0`).then(r=>r.json()),
      fetch(`${API}/gpt/session/${SESSION_ID}`).then(r=>r.json())
    ]);

    const s = resultRes;
    if(s.detail) throw new Error(s.detail);

    // Try to get result_json data (zones, laps, insights)
    let zones = s.zones || [];
    let laps = s.laps || [];
    let insights = s.insights || {};

    // Also try /result endpoint for richer data
    try {
      const rich = await fetch(`${API}/result/${SESSION_ID}`).then(r=>r.json());
      if(rich.zones) zones = rich.zones;
      if(rich.laps) laps = rich.laps;
      if(rich.derived_insights) insights = rich.derived_insights;
    } catch(e) {}

    renderSession(s, zones, laps, insights);
  } catch(e) {
    $('page').innerHTML = `<div class="loading" style="color:var(--accent)">${e.message}</div>`;
  }
}

function renderSession(s, zones, laps, insights) {
  const sport = s.sport || '';
  const sportLabel = SPORT_LABELS[sport] || sport;
  const dur = s.duration_s || 0;

  const metricsHtml = sport === 'cycling' || sport === 'running' ? `
    <div class="hm"><div class="hm-label">Distancia</div><div class="hm-value">${s.distance_km||'—'}</div><div class="hm-unit">km</div></div>
    <div class="hm"><div class="hm-label">Duración</div><div class="hm-value">${s.duration_hms||'—'}</div></div>
    <div class="hm"><div class="hm-label">FC prom</div><div class="hm-value">${s.avg_hr_bpm||'—'}</div><div class="hm-unit">bpm</div></div>
    <div class="hm"><div class="hm-label">Velocidad</div><div class="hm-value">${s.avg_speed_kmh||'—'}</div><div class="hm-unit">km/h</div></div>
    <div class="hm"><div class="hm-label">Ascenso</div><div class="hm-value">${s.ascent_m?'+'+s.ascent_m:'—'}</div><div class="hm-unit">m</div></div>
    <div class="hm"><div class="hm-label">Cadencia</div><div class="hm-value">${s.avg_cadence||'—'}</div><div class="hm-unit">rpm</div></div>
  ` : `
    <div class="hm"><div class="hm-label">Duración</div><div class="hm-value">${s.duration_hms||'—'}</div></div>
    <div class="hm"><div class="hm-label">FC prom</div><div class="hm-value">${s.avg_hr_bpm||'—'}</div><div class="hm-unit">bpm</div></div>
    <div class="hm"><div class="hm-label">Distancia</div><div class="hm-value">${s.distance_km||'—'}</div><div class="hm-unit">km</div></div>
  `;

  // Zones
  const zonesHtml = zones.length ? zones.filter(z=>z.zone>0).map((z,i) => `
    <div class="zone-card">
      <div class="zone-name">${z.name}</div>
      <div class="zone-time">${z.minutes}m</div>
      <div class="zone-pct">${z.percent}%</div>
      <div class="zone-bar"><div class="zone-fill" style="width:${Math.max(z.percent,2)}%;background:${ZONE_COLORS[i]||'#888'}"></div></div>
    </div>`).join('') : '<div style="color:var(--muted);font-size:13px">Sin datos de zonas</div>';

  // Laps
  const lapsHtml = laps.length ? `
    <table class="laps-table">
      <thead><tr>
        <th>Lap</th><th>Duración</th><th>Distancia</th><th>FC avg</th><th>Vel avg</th>
      </tr></thead>
      <tbody>
        ${laps.map(l=>`<tr>
          <td>${l.lap}</td>
          <td>${l.duration_mmss||'—'}</td>
          <td>${l.distance_km||'—'} km</td>
          <td>${l.avg_hr_bpm||'—'} bpm</td>
          <td>${l.avg_speed_kmh||'—'} km/h</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '<div style="color:var(--muted);font-size:13px">Sin datos de laps</div>';

  // Insights
  const insightKeys = ['hr_drift_note','best_aerobic_window','traffic_note','altitude_note'];
  const insightsHtml = insightKeys
    .filter(k => insights[k])
    .map(k => {
      const val = typeof insights[k] === 'object' ? insights[k].note : insights[k];
      return `<div class="insight">${val}</div>`;
    }).join('') || '<div style="color:var(--muted);font-size:13px">Sin insights disponibles</div>';

  $('page').innerHTML = `
    <div class="hero">
      <div class="hero-inner">
        <div class="hero-top">
          <div>
            <div class="hero-sport">${sportLabel.toUpperCase()}</div>
            <div class="hero-name">${s.workout_name||'Sesión sin nombre'}</div>
            <div class="hero-date">${fmtDate(s.start_time)} · ${SESSION_ID}</div>
          </div>
          <a href="/activities" class="hero-back">← Actividades</a>
        </div>
        <div class="hero-metrics">${metricsHtml}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Gráficas de la sesión</div>
      <div id="chartsSection">
        <div class="no-charts">
          <p>Las gráficas detalladas requieren que el archivo FIT esté en memoria.<br>
          Vuelve a subir el archivo para ver FC, velocidad, cadencia y altimetría.</p>
          <a href="/" class="upload-btn">Subir archivo FIT</a>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Zonas Mars</div>
      <div class="zones-grid">${zonesHtml}</div>
    </div>

    <div class="card">
      <div class="card-title">Laps</div>
      ${lapsHtml}
    </div>

    <div class="card">
      <div class="card-title">Insights automáticos</div>
      ${insightsHtml}
    </div>

    <div class="card" id="postSessionCard">
      <div class="card-title">Registro post-sesión</div>
      <div id="postSessionForm">
        <div class="form-section-title">Esfuerzo percibido (RPE)</div>
        <div class="rpe-grid" id="rpeGrid">
          <div class="rpe-btn" data-rpe="1" onclick="selectRpe(1)">1</div>
          <div class="rpe-btn" data-rpe="2" onclick="selectRpe(2)">2</div>
          <div class="rpe-btn" data-rpe="3" onclick="selectRpe(3)">3</div>
          <div class="rpe-btn" data-rpe="4" onclick="selectRpe(4)">4</div>
          <div class="rpe-btn" data-rpe="5" onclick="selectRpe(5)">5</div>
          <div class="rpe-btn" data-rpe="6" onclick="selectRpe(6)">6</div>
          <div class="rpe-btn" data-rpe="7" onclick="selectRpe(7)">7</div>
          <div class="rpe-btn" data-rpe="8" onclick="selectRpe(8)">8</div>
          <div class="rpe-btn" data-rpe="9" onclick="selectRpe(9)">9</div>
          <div class="rpe-btn" data-rpe="10" onclick="selectRpe(10)">10</div>
        </div>

        <div class="form-section-title">Peso</div>
        <div class="form-grid">
          <div class="form-group">
            <label class="form-label">Peso antes (kg)</label>
            <input class="form-input" id="weightBefore" type="number" step="0.1" placeholder="89.0">
          </div>
          <div class="form-group">
            <label class="form-label">Peso después (kg)</label>
            <input class="form-input" id="weightAfter" type="number" step="0.1" placeholder="88.2">
          </div>
        </div>

        <div class="form-section-title">Hidratación y nutrición</div>
        <div class="form-grid">
          <div class="form-group">
            <label class="form-label">Agua (litros)</label>
            <input class="form-input" id="waterLiters" type="number" step="0.1" placeholder="1.5">
          </div>
          <div class="form-group">
            <label class="form-label">Cafeína (mg)</label>
            <input class="form-input" id="caffeineMg" type="number" step="50" placeholder="200">
          </div>
          <div class="form-group">
            <label class="form-label">Geles</label>
            <input class="form-input" id="gels" type="number" step="1" placeholder="0">
          </div>
          <div class="form-group">
            <label class="form-label">Barras</label>
            <input class="form-input" id="bars" type="number" step="1" placeholder="0">
          </div>
        </div>

        <div class="form-section-title">Detalle del gel</div>
        <div class="form-grid">
          <div class="form-group">
            <label class="form-label">Tipo de gel</label>
            <select class="form-input" id="gelType">
              <option value="">— ninguno —</option>
              <option value="casero-agave">Casero agave + Tree Top</option>
              <option value="casero-miel">Casero miel + Tree Top</option>
              <option value="casero-datiles">Casero dátiles</option>
              <option value="barrita-casera">Barrita casera</option>
              <option value="comercial">Gel comercial</option>
              <option value="otro">Otro</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Minuto de toma</label>
            <input class="form-input" id="gelTiming" type="text" placeholder="ej. minuto 60">
          </div>
          <div class="form-group full">
            <label class="form-label">Receta / detalle</label>
            <input class="form-input" id="gelRecipe" type="text" placeholder="40ml agave + 60ml Tree Top + 0.5g sal">
          </div>
          <div class="form-group">
            <label class="form-label">Carbos estimados (g)</label>
            <input class="form-input" id="gelCarbs" type="number" step="1" placeholder="40">
          </div>
          <div class="form-group">
            <label class="form-label">Sodio estimado (mg)</label>
            <input class="form-input" id="gelSodium" type="number" step="10" placeholder="200">
          </div>
        </div>
        <div class="form-grid" style="margin-top:8px">
          <div class="form-group">
            <label class="form-label">Respuesta GI</label>
            <select class="form-input" id="giResponse">
              <option value="">— selecciona —</option>
              <option value="sin-problemas">Sin problemas</option>
              <option value="inflamacion-leve">Inflamación leve</option>
              <option value="inflamacion-fuerte">Inflamación fuerte</option>
              <option value="nausea">Náusea</option>
              <option value="calambres">Calambres</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Respuesta energética</label>
            <select class="form-input" id="energyResponse">
              <option value="">— selecciona —</option>
              <option value="subidón-estable">Subidón y estable</option>
              <option value="subidón-caída">Subidón y caída</option>
              <option value="gradual">Gradual y sostenido</option>
              <option value="sin-efecto">Sin efecto notable</option>
              <option value="negativo">Negativo / bajón</option>
            </select>
          </div>
        </div>

        <div class="form-section-title">Sueño previo</div>
        <div class="form-grid">
          <div class="form-group">
            <label class="form-label">Horas de sueño</label>
            <input class="form-input" id="sleepHours" type="number" step="0.5" placeholder="7.5">
          </div>
          <div class="form-group">
            <label class="form-label">Calidad del sueño</label>
            <select class="form-input" id="sleepQuality">
              <option value="">— selecciona —</option>
              <option value="bueno">Bueno</option>
              <option value="regular">Regular</option>
              <option value="malo">Malo</option>
            </select>
          </div>
        </div>

        <div class="form-section-title">Condiciones</div>
        <div class="conditions-grid" id="condGrid">
          <div class="cond-btn" data-cond="calor" onclick="toggleCond('calor')">calor</div>
          <div class="cond-btn" data-cond="frio" onclick="toggleCond('frio')">frío</div>
          <div class="cond-btn" data-cond="viento" onclick="toggleCond('viento')">viento</div>
          <div class="cond-btn" data-cond="lluvia" onclick="toggleCond('lluvia')">lluvia</div>
          <div class="cond-btn" data-cond="humedad" onclick="toggleCond('humedad')">humedad</div>
          <div class="cond-btn" data-cond="trafico" onclick="toggleCond('trafico')">tráfico</div>
          <div class="cond-btn" data-cond="cansancio" onclick="toggleCond('cansancio')">cansancio</div>
          <div class="cond-btn" data-cond="piernas-pesadas" onclick="toggleCond('piernas-pesadas')">piernas pesadas</div>
          <div class="cond-btn" data-cond="bien" onclick="toggleCond('bien')">bien</div>
        </div>

        <div class="form-section-title" style="margin-top:16px">Notas</div>
        <div class="form-group">
          <textarea class="form-input form-textarea" id="notes" placeholder="Cómo se sintió la sesión, observaciones..."></textarea>
        </div>

        <div id="sweatInfo" class="sweat-info"></div>
        <button class="save-btn" id="saveBtn" onclick="savePostSession()">Guardar registro</button>
        <div id="formSaved" class="form-saved">✓ Registro guardado correctamente</div>
      </div>
    </div>
  `;

  // Try to load charts if available
  fetch(`${API}/charts/${SESSION_ID}`).then(r => {
    if(r.ok) return r.text();
    throw new Error('no charts');
  }).then(html => {
    if(html && !html.includes('Sin datos') && !html.includes('no disponibles')) {
      const iframe = document.createElement('iframe');
      iframe.src = `/charts/${SESSION_ID}`;
      iframe.style.cssText = 'width:100%;height:500px;border:none;border-radius:8px';
      $('chartsSection').innerHTML = '';
      $('chartsSection').appendChild(iframe);
    }
  }).catch(() => {});

  // Load existing post-session data if any
  loadPostSession();
}

// ── POST-SESSION FORM ──────────────────────────────────────────────────────
let selectedRpe = null;
let selectedConds = new Set();

function selectRpe(n) {
  selectedRpe = n;
  document.querySelectorAll('.rpe-btn').forEach(b => {
    b.classList.toggle('selected', parseInt(b.dataset.rpe) === n);
  });
}

function toggleCond(c) {
  const btn = document.querySelector('.cond-btn[data-cond="'+c+'"]');
  if(selectedConds.has(c)) { selectedConds.delete(c); btn.classList.remove('selected'); }
  else { selectedConds.add(c); btn.classList.add('selected'); }
}

async function loadPostSession() {
  try {
    const r = await fetch(API + '/post-session/' + SESSION_ID);
    if(!r.ok) return;
    const d = await r.json();
    if(d.rpe) selectRpe(d.rpe);
    if(d.weight_before) document.getElementById('weightBefore').value = d.weight_before;
    if(d.weight_after) document.getElementById('weightAfter').value = d.weight_after;
    if(d.water_liters) document.getElementById('waterLiters').value = d.water_liters;
    if(d.caffeine_mg) document.getElementById('caffeineMg').value = d.caffeine_mg;
    if(d.gels != null) document.getElementById('gels').value = d.gels;
    if(d.bars != null) document.getElementById('bars').value = d.bars;
    if(d.sleep_hours) document.getElementById('sleepHours').value = d.sleep_hours;
    if(d.sleep_quality) document.getElementById('sleepQuality').value = d.sleep_quality;
    if(d.notes) document.getElementById('notes').value = d.notes;
    if(d.gel_type) document.getElementById('gelType').value = d.gel_type;
    if(d.gel_recipe) document.getElementById('gelRecipe').value = d.gel_recipe;
    if(d.gel_carbs_g) document.getElementById('gelCarbs').value = d.gel_carbs_g;
    if(d.gel_sodium_mg) document.getElementById('gelSodium').value = d.gel_sodium_mg;
    if(d.gel_timing) document.getElementById('gelTiming').value = d.gel_timing;
    if(d.gi_response) document.getElementById('giResponse').value = d.gi_response;
    if(d.energy_response) document.getElementById('energyResponse').value = d.energy_response;
    if(d.conditions && Array.isArray(d.conditions)) {
      d.conditions.forEach(c => { selectedConds.add(c); const b = document.querySelector('.cond-btn[data-cond="'+c+'"]'); if(b) b.classList.add('selected'); });
    }
    if(d.sweat_rate) showSweatRate(d.sweat_rate);
  } catch(e) {}
}

function showSweatRate(rate) {
  const el = document.getElementById('sweatInfo');
  if(!el) return;
  el.style.display = 'block';
  el.innerHTML = 'Tasa de sudoración calculada: <strong>' + rate + ' L/h</strong>';
}

async function savePostSession() {
  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  btn.textContent = 'Guardando...';
  const wb = parseFloat(document.getElementById('weightBefore').value) || null;
  const wa = parseFloat(document.getElementById('weightAfter').value) || null;
  const body = {
    rpe: selectedRpe,
    weight_before: wb,
    weight_after: wa,
    water_liters: parseFloat(document.getElementById('waterLiters').value) || null,
    caffeine_mg: parseInt(document.getElementById('caffeineMg').value) || null,
    gels: parseInt(document.getElementById('gels').value) || null,
    bars: parseInt(document.getElementById('bars').value) || null,
    gel_type: document.getElementById('gelType').value || null,
    gel_recipe: document.getElementById('gelRecipe').value || null,
    gel_carbs_g: parseInt(document.getElementById('gelCarbs').value) || null,
    gel_sodium_mg: parseInt(document.getElementById('gelSodium').value) || null,
    gel_timing: document.getElementById('gelTiming').value || null,
    gi_response: document.getElementById('giResponse').value || null,
    energy_response: document.getElementById('energyResponse').value || null,
    sleep_hours: parseFloat(document.getElementById('sleepHours').value) || null,
    sleep_quality: document.getElementById('sleepQuality').value || null,
    conditions: selectedConds.size ? [...selectedConds] : null,
    notes: document.getElementById('notes').value || null
  };
  try {
    const r = await fetch(API + '/post-session/' + SESSION_ID, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const d = await r.json();
    if(d.ok) {
      document.getElementById('formSaved').style.display = 'block';
      if(d.sweat_rate) showSweatRate(d.sweat_rate);
      btn.textContent = 'Actualizar registro';
    } else {
      btn.textContent = 'Error — reintentar';
    }
  } catch(e) {
    btn.textContent = 'Error — reintentar';
  }
  btn.disabled = false;
}

loadSession();
</script>
</body>
</html>"""
