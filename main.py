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
        # Add file_hash to sessions if missing
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS file_hash TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_file_hash ON sessions(file_hash)")
        except Exception:
            pass
        # Add achievements table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id           SERIAL PRIMARY KEY,
                session_id   TEXT,
                date         DATE,
                type         TEXT,
                metric       TEXT,
                value        DECIMAL(12,4),
                prev_best    DECIMAL(12,4),
                description  TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Add athlete_snapshots table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS athlete_snapshots (
                id           SERIAL PRIMARY KEY,
                week_start   DATE UNIQUE,
                weight_kg    DECIMAL(5,2),
                km_week      DECIMAL(8,2),
                hours_week   DECIMAL(6,2),
                sessions     INT,
                avg_hr       DECIMAL(5,1),
                avg_speed    DECIMAL(5,2),
                avg_cadence  DECIMAL(5,1),
                pct_z2       DECIMAL(5,2),
                efficiency   DECIMAL(8,5),
                fitness_score DECIMAL(6,2),
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)

# ═══════════════════════════════════════════════════════════════════════════════
# DB — Conexión y tablas
# ═══════════════════════════════════════════════════════════════════════════════

        # Índices para performance
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_sessions_start_time ON sessions(start_time)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_sport ON sessions(sport)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_route_id ON sessions(route_id)",
            "CREATE INDEX IF NOT EXISTS idx_records_session_t ON session_records(session_id, t)",
            "CREATE INDEX IF NOT EXISTS idx_post_session_id ON post_session(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_fuerza_date ON fuerza(date)",
            "CREATE INDEX IF NOT EXISTS idx_wellness_date ON wellness(date)",
        ]:
            try:
                cur.execute(idx_sql)
            except Exception:
                pass

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
    sw_code = """const CACHE='bitacora-v12';
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
    const warn=document.createElement('div');
    warn.style.cssText='background:#fff8e1;border:1px solid #f2a623;border-radius:8px;padding:10px 14px;font-size:12px;color:#7a5200;margin-bottom:12px';
    warn.textContent='⚠️ Esta actividad ya estaba registrada. Mostrando la sesión existente.';
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

# ─────────────────────────────────────────────────────────────────────────────
# /dashboard  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js')}}</script>
<title>Bitácora — Rutas</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{DARK_CSS}
.routes-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:14px}}
.route-card{{background:linear-gradient(180deg,var(--surface2),var(--surface));border:1px solid var(--stroke);
  border-radius:var(--radius-lg);overflow:hidden;cursor:pointer;transition:transform .15s,border-color .15s}}
.route-card:hover{{transform:translateY(-2px);border-color:var(--stroke2)}}
.route-card.sel{{border-color:var(--bike);box-shadow:0 0 0 2px rgba(232,89,60,.18)}}
.rc-banner{{height:72px;background:linear-gradient(135deg,rgba(232,89,60,.25),rgba(232,89,60,.05));
  display:flex;align-items:center;justify-content:space-between;padding:12px 16px;position:relative}}
.rc-sport{{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;
  color:var(--bike);background:rgba(232,89,60,.15);border:1px solid rgba(232,89,60,.3);
  border-radius:6px;padding:3px 8px}}
.rc-times{{font-size:28px;font-weight:900;color:var(--text)}}
.rc-times span{{font-size:11px;font-family:monospace;color:var(--muted);font-style:normal;opacity:.7}}
.rc-body{{padding:14px 16px}}
.rc-name{{font-size:16px;font-weight:800;letter-spacing:-.02em;margin-bottom:10px}}
.rc-metrics{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}}
.rc-metric-label{{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:2px}}
.rc-metric-value{{font-size:16px;font-weight:800}}
.rc-metric-unit{{font-size:10px;color:var(--muted)}}
.rc-dates{{font-size:11px;color:var(--muted);display:flex;justify-content:space-between}}
/* Detail panel */
#detail-panel{{background:linear-gradient(180deg,var(--surface2),var(--surface));
  border:1px solid var(--stroke);border-radius:var(--radius-lg);padding:20px;display:none}}
#detail-panel.show{{display:block}}
.dp-title{{font-size:20px;font-weight:900;letter-spacing:-.03em;margin-bottom:4px}}
.dp-sub{{font-size:12px;color:var(--muted);margin-bottom:16px}}
.dp-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}}
.dp-stat-label{{font-size:9px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:3px}}
.dp-stat-value{{font-size:22px;font-weight:900}}
.dp-stat-unit{{font-size:11px;color:var(--muted)}}
.rides-table{{width:100%;border-collapse:collapse;font-size:12px}}
.rides-table th{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:700;text-align:left;padding:0 8px 10px 0;border-bottom:1px solid var(--stroke)}}
.rides-table td{{padding:9px 8px 9px 0;border-bottom:1px solid var(--stroke)}}
.rides-table tr:last-child td{{border-bottom:none}}
.td-badge{{font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;
  padding:2px 7px;border-radius:4px;margin-left:8px}}
.badge-first{{background:rgba(61,214,140,.15);color:var(--green)}}
.badge-last{{background:rgba(232,89,60,.15);color:var(--red)}}
.chart-wrap{{height:160px;margin:14px 0}}
</style>
</head>
<body>
{DARK_NAV}
<div class="page">
  <div class="page-hdr">
    <div class="page-title">Rutas</div>
    <div class="page-sub">Historial y progreso por ruta identificada</div>
  </div>

  <!-- Hero stats -->
  <div class="kpi-grid-4" id="route-stats">
    <div class="kpi-box"><div class="kpi-label">Total rutas</div><div class="kpi-val" id="sk-routes" style="color:var(--bike)">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Más recorrida</div><div class="kpi-val" style="font-size:14px;padding-top:4px" id="sk-top">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Más larga</div><div class="kpi-val" style="font-size:20px" id="sk-longest">— <span style="font-size:11px;color:var(--muted)">km</span></div></div>
    <div class="kpi-box"><div class="kpi-label">Mayor ascenso</div><div class="kpi-val" style="font-size:20px" id="sk-ascent">— <span style="font-size:11px;color:var(--muted)">m</span></div></div>
  </div>

  <!-- Filter -->
  <div class="filter-bar" id="sport-filter"></div>

  <div class="layout-sidebar">
    <!-- Route list -->
    <div>
      <div id="routes-list"><div class="loading"><div class="spinner"></div>Cargando rutas...</div></div>
    </div>
    <!-- Detail panel -->
    <div>
      <div id="detail-panel">
        <div class="dp-title" id="dp-name">—</div>
        <div class="dp-sub" id="dp-sub">Selecciona una ruta para ver el progreso</div>
        <div class="dp-grid">
          <div class="kpi-box"><div class="dp-stat-label">Distancia</div><div class="dp-stat-value" id="dp-dist">—</div><div class="dp-stat-unit">km</div></div>
          <div class="kpi-box"><div class="dp-stat-label">Ascenso</div><div class="dp-stat-value" id="dp-asc">—</div><div class="dp-stat-unit">m</div></div>
          <div class="kpi-box"><div class="dp-stat-label">Veces</div><div class="dp-stat-value" id="dp-times">—</div><div class="dp-stat-unit">ejecuciones</div></div>
        </div>
        <div class="chart-wrap"><canvas id="route-chart"></canvas></div>
        <div class="card-title">Historial en esta ruta</div>
        <table class="rides-table"><thead><tr><th>Fecha</th><th>Duración</th><th>FC prom.</th><th>Vel.</th><th>Cadencia</th></tr></thead>
        <tbody id="dp-rides"></tbody></table>
      </div>
      <div id="detail-placeholder" style="display:flex;align-items:center;justify-content:center;height:220px;color:var(--muted);font-size:13px;flex-direction:column;gap:8px">
        <span style="font-size:32px">📍</span>Toca una ruta para ver su progreso
      </div>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;
let allRoutes = [], routeChart = null, activeSport = '';

function fmtDate(s){{
  if(!s)return '—';
  const d=new Date((s+'').slice(0,10)+'T12:00:00');
  const M=['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
  return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
}}
function sportColor(sp){{
  return sp==='running'?'var(--green)':sp==='walking'?'var(--wellness)':sp==='swimming'?'var(--stats)':'var(--bike)';
}}

async function loadRoutes(){{
  try{{
    const data = await fetch(API+'/routes').then(r=>r.json());
    allRoutes = Array.isArray(data)?data:[];
    buildSportFilter();
    renderRoutes(allRoutes);
    buildStats(allRoutes);
  }}catch(e){{
    document.getElementById('routes-list').innerHTML='<div style="color:var(--muted);padding:20px">'+e.message+'</div>';
  }}
}}

function buildStats(routes){{
  document.getElementById('sk-routes').textContent = routes.length;
  const top = routes.reduce((a,b)=>(b.times_ridden||0)>(a.times_ridden||0)?b:a, routes[0]||{{}});
  document.getElementById('sk-top').textContent = top.name||'—';
  const longest = routes.reduce((a,b)=>(b.distance_km||0)>(a.distance_km||0)?b:a, routes[0]||{{}});
  document.getElementById('sk-longest').innerHTML = (longest.distance_km||'—')+' <span style="font-size:11px;color:var(--muted)">km</span>';
  const mostAscent = routes.reduce((a,b)=>(b.ascent_m||0)>(a.ascent_m||0)?b:a, routes[0]||{{}});
  document.getElementById('sk-ascent').innerHTML = (mostAscent.ascent_m||'—')+' <span style="font-size:11px;color:var(--muted)">m</span>';
}}

function buildSportFilter(){{
  const sports = [...new Set(allRoutes.map(r=>r.sport).filter(Boolean))];
  const fb = document.getElementById('sport-filter');
  fb.innerHTML = '<div class="filter-chip on" onclick="setSport(\\'\\',this)">Todos <span style="opacity:.6">'+allRoutes.length+'</span></div>'
    + sports.map(sp=>'<div class="filter-chip" onclick="setSport(\\''+sp+'\\',this)">'+sp+' <span style="opacity:.6">'+allRoutes.filter(r=>r.sport===sp).length+'</span></div>').join('');
}}

function setSport(sp, el){{
  activeSport = sp;
  document.querySelectorAll('.filter-chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on');
  renderRoutes(sp ? allRoutes.filter(r=>r.sport===sp) : allRoutes);
}}

function renderRoutes(routes){{
  const el = document.getElementById('routes-list');
  if(!routes.length){{ el.innerHTML='<div style="color:var(--muted);padding:20px;text-align:center">Sin rutas</div>'; return; }}
  el.innerHTML = routes.map(r=>{{
    const col = sportColor(r.sport);
    return `<div class="route-card" id="rc-${{r.route_id}}" onclick="showDetail('${{r.route_id}}')">
      <div class="rc-banner">
        <span class="rc-sport">${{r.sport||'cycling'}}</span>
        <span class="rc-times">${{r.times_ridden||0}}<span> veces</span></span>
      </div>
      <div class="rc-body">
        <div class="rc-name">${{r.name||r.route_id}}</div>
        <div class="rc-metrics">
          <div><div class="rc-metric-label">Distancia</div><div class="rc-metric-value" style="color:${{col}}">${{(r.distance_km||0).toFixed(1)}}<span class="rc-metric-unit"> km</span></div></div>
          <div><div class="rc-metric-label">FC prom.</div><div class="rc-metric-value">${{r.avg_hr_bpm||'—'}}<span class="rc-metric-unit"> bpm</span></div></div>
          <div><div class="rc-metric-label">Vel. prom.</div><div class="rc-metric-value">${{r.avg_speed_kmh&&r.avg_speed_kmh>0?Number(r.avg_speed_kmh).toFixed(1):'—'}}<span class="rc-metric-unit"> km/h</span></div></div>
        </div>
        <div class="rc-dates"><span>${{fmtDate(r.first_ride)}}</span><span>→ ${{fmtDate(r.last_ride)}}</span></div>
      </div>
    </div>`;
  }}).join('');
}}

async function showDetail(routeId){{
  document.querySelectorAll('.route-card').forEach(c=>c.classList.remove('sel'));
  const card = document.getElementById('rc-'+routeId);
  if(card) card.classList.add('sel');
  document.getElementById('detail-placeholder').style.display='none';
  document.getElementById('detail-panel').classList.add('show');
  document.getElementById('dp-rides').innerHTML='<tr><td colspan="5" style="color:var(--muted);padding:20px">Cargando...</td></tr>';

  try{{
    const d = await fetch(API+'/route/'+routeId).then(r=>r.json());
    document.getElementById('dp-name').textContent = d.name||routeId;
    document.getElementById('dp-sub').textContent = (d.times_ridden||0)+' ejecuciones · '+fmtDate(d.rides?.[0]?.date)+' → '+fmtDate(d.rides?.at(-1)?.date);
    document.getElementById('dp-dist').textContent = (d.distance_km||'—');
    document.getElementById('dp-asc').textContent = (d.ascent_m||'—');
    document.getElementById('dp-times').textContent = (d.times_ridden||0);

    const rides = d.rides||[];
    // Chart: FC trend
    if(routeChart)routeChart.destroy();
    const ctx = document.getElementById('route-chart').getContext('2d');
    routeChart = new Chart(ctx,{{
      type:'line',
      data:{{
        labels:rides.map(r=>fmtDate(r.date)),
        datasets:[
          {{label:'FC prom',data:rides.map(r=>r.avg_hr_bpm),borderColor:'var(--bike)',backgroundColor:'rgba(232,89,60,.08)',tension:.4,pointRadius:3}},
          {{label:'Vel km/h',data:rides.map(r=>r.avg_speed_kmh),borderColor:'var(--wellness)',backgroundColor:'rgba(74,158,255,.08)',tension:.4,pointRadius:3,yAxisID:'y2'}}
        ]
      }},
      options:{{
        responsive:true,maintainAspectRatio:false,
        plugins:{{legend:{{labels:{{color:'#8e95a3',font:{{size:10}}}}}}}},
        scales:{{
          x:{{ticks:{{color:'#5f6673',font:{{size:9}},maxTicksLimit:6}},grid:{{color:'rgba(255,255,255,.04)'}}}},
          y:{{ticks:{{color:'#5f6673',font:{{size:9}}}},grid:{{color:'rgba(255,255,255,.04)'}}}},
          y2:{{position:'right',ticks:{{color:'#5f6673',font:{{size:9}}}},grid:{{display:false}}}}
        }}
      }}
    }});

    // Rides table
    document.getElementById('dp-rides').innerHTML = rides.map((r,i)=>
      `<tr>
        <td>${{fmtDate(r.date)}}${{i===0?'<span class="td-badge badge-first">Primera</span>':''}}${{i===rides.length-1?'<span class="td-badge badge-last">Última</span>':''}}</td>
        <td>${{r.duration_s?Math.floor(r.duration_s/3600)+'h '+String(Math.floor((r.duration_s%3600)/60)).padStart(2,'0')+'m':'—'}}</td>
        <td><strong>${{r.avg_hr_bpm||'—'}}</strong> <span style="color:var(--muted);font-size:10px">bpm</span></td>
        <td><strong>${{r.avg_speed_kmh?Number(r.avg_speed_kmh).toFixed(1):'—'}}</strong> <span style="color:var(--muted);font-size:10px">km/h</span></td>
        <td>${{r.avg_cadence||'—'}} <span style="color:var(--muted);font-size:10px">rpm</span></td>
      </tr>`
    ).join('');
  }}catch(e){{
    document.getElementById('dp-rides').innerHTML='<tr><td colspan="5" style="color:var(--muted)">'+e.message+'</td></tr>';
  }}
}}

loadRoutes();
</script>
</body>
</html>"""


@app.get("/home", response_class=HTMLResponse)
def home_page():
    return """<!DOCTYPE html>
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
<title>Bitácora Mars</title>
<style>
:root{--bg:#0d0d0d;--card:#1a1a1a;--card2:#222;--border:#2a2a2a;--text:#f0f0f0;--muted:#666;--muted2:#444;--bike:#e8593c;--fuerza:#a3e635;--wellness:#4a9eff;--stats:#a78bfa;--home:#f0f0f0}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif}
#app{display:flex;flex-direction:column;height:100vh;height:100dvh}
#content{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding-bottom:84px}
#bnav{position:fixed;bottom:0;left:0;right:0;height:68px;padding-bottom:env(safe-area-inset-bottom);background:#111;border-top:1px solid var(--border);display:flex;z-index:100}
.bn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;border:none;background:none;cursor:pointer;padding:8px 0}
.bn-ic{font-size:22px;line-height:1;transition:transform .2s}
.bn-lb{font-size:9px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);transition:color .2s}
.bn.active .bn-lb{color:var(--c)}
.bn.active .bn-ic{transform:scale(1.15)}
.screen{display:none;padding:16px}
.screen.active{display:block}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;padding-top:6px}
.hdr-greet{font-size:12px;color:var(--muted);margin-bottom:2px}
.hdr-name{font-size:24px;font-weight:800;letter-spacing:-.03em}
.hdr-dot{width:10px;height:10px;border-radius:50%;background:var(--c,var(--bike));animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.card{background:var(--card);border-radius:16px;padding:16px;margin-bottom:12px;border:1px solid var(--border)}
.card-sm{background:var(--card);border-radius:12px;padding:14px;border:1px solid var(--border)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}
.kpi-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:5px}
.kpi-val{font-size:28px;font-weight:800;letter-spacing:-.02em;line-height:1}
.kpi-unit{font-size:11px;color:var(--muted);margin-top:2px}
.sec-title{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
.banner{border-radius:16px;padding:16px 18px;margin-bottom:12px}
.banner-tag{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;opacity:.7;margin-bottom:4px}
.banner-title{font-size:16px;font-weight:700;letter-spacing:-.01em;line-height:1.3}
.act-row{display:flex;align-items:center;gap:12px;padding:11px 0;border-bottom:1px solid var(--border)}
.act-row:last-child{border-bottom:none}
.act-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.act-info{flex:1;min-width:0}
.act-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act-meta{font-size:11px;color:var(--muted);margin-top:2px}
.act-km{font-size:14px;font-weight:700}
.act-hr{font-size:11px;color:var(--muted)}
.upload-zone{border:2px dashed var(--border);border-radius:16px;padding:28px 20px;text-align:center;cursor:pointer;transition:all .2s;margin-bottom:12px}
.upload-zone.drag{border-color:var(--bike);background:rgba(232,89,60,.05)}
.upload-icon{font-size:36px;margin-bottom:8px}
.upload-title{font-size:16px;font-weight:700;margin-bottom:3px}
.upload-sub{font-size:12px;color:var(--muted)}
.prog-wrap{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin:6px 0}
.prog-fill{height:100%;border-radius:3px;transition:width .5s ease;background:var(--c,var(--bike))}
.result-card{background:var(--card);border-radius:16px;padding:16px;margin-bottom:12px;border:1px solid var(--bike);display:none}
.result-card.show{display:block}
.muscle-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.muscle-btn{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 8px;text-align:center;cursor:pointer;font-size:12px;font-weight:500;color:var(--muted)}
.muscle-btn.sel{border-color:var(--fuerza);background:rgba(163,230,53,.1);color:var(--fuerza)}
.int-display{font-size:52px;font-weight:900;text-align:center;color:var(--fuerza);letter-spacing:-.03em;margin:10px 0}
input[type=range]{width:100%;accent-color:var(--fuerza);height:4px;margin-bottom:4px}
input[type=number],select,textarea,input[type=text],input[type=date]{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:14px;color:var(--text);width:100%;outline:none;font-family:inherit;-webkit-appearance:none;appearance:none}
input:focus,select:focus,textarea:focus{border-color:var(--c,var(--bike))}
.form-group{margin-bottom:10px}
.form-label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:5px;display:block}
.btn-main{width:100%;padding:14px;border:none;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;background:var(--c,var(--bike));color:#000;font-family:inherit;margin-top:8px}
.btn-sec{background:var(--card2);color:var(--text);border:1px solid var(--border);border-radius:12px;padding:10px 16px;font-size:13px;font-weight:500;cursor:pointer;font-family:inherit;width:100%;margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{padding:6px 14px;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border);color:var(--muted);background:var(--card)}
.chip.on{background:var(--c,var(--bike));border-color:var(--c,var(--bike));color:#000}
.rpe-grid{display:grid;grid-template-columns:repeat(10,1fr);gap:3px;margin-bottom:10px}
.rpe-btn{padding:8px 0;border:1px solid var(--border);border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-align:center;background:var(--card);color:var(--muted)}
.rpe-btn.on{background:var(--c,var(--bike));border-color:var(--c,var(--bike));color:#000}
.wtabs{display:flex;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;padding-bottom:2px;margin-bottom:14px}
.wtabs::-webkit-scrollbar{display:none}
.wtab{padding:7px 16px;border-radius:20px;font-size:12px;font-weight:600;white-space:nowrap;cursor:pointer;border:1px solid var(--border);color:var(--muted);background:var(--card);flex-shrink:0}
.wtab.on{background:var(--wellness);border-color:var(--wellness);color:#000}
.rec-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.rec-row:last-child{border-bottom:none}
.rec-label{font-size:12px;color:var(--muted)}
.rec-val{font-size:15px;font-weight:700}
.rec-date{font-size:10px;color:var(--muted2);margin-top:1px;text-align:right}
.spin{display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--c,var(--bike));border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-wrap{display:flex;align-items:center;justify-content:center;padding:40px;gap:10px;color:var(--muted);font-size:13px}
.ok-toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:20px;font-size:13px;font-weight:700;z-index:999;display:none}
.z2-bar-wrap{height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin:6px 0}
.z2-bar-fill{height:100%;border-radius:4px;background:var(--wellness)}
</style>
</head>
<body>
<div id="app">
<div id="content">
<div class="screen active" id="sc-home" style="--c:var(--home)">
  <div class="hdr"><div><div class="hdr-greet" id="hdr-greet">Cargando...</div><div class="hdr-name">Mars 🚴</div></div><div class="hdr-dot" style="background:var(--bike)"></div></div>
  <div id="home-content"><div class="loading-wrap"><div class="spin" style="border-top-color:var(--bike)"></div> Cargando...</div></div>
</div>
<div class="screen" id="sc-bici" style="--c:var(--bike)">
  <div class="hdr"><div><div class="hdr-greet">Ciclismo</div><div class="hdr-name" style="color:var(--bike)">Subir sesión</div></div><div class="hdr-dot"></div></div>
  <div class="upload-zone" id="drop-zone" onclick="document.getElementById('fit-input').click()">
    <input type="file" id="fit-input" accept=".fit,.zip" style="display:none" onchange="handleFile(this.files[0])">
    <div class="upload-icon">🚴</div><div class="upload-title">Subir actividad</div><div class="upload-sub">Toca para seleccionar .FIT o .ZIP</div>
  </div>
  <div id="upload-progress" style="display:none"><div style="font-size:13px;color:var(--muted);margin-bottom:6px" id="upload-label">Procesando...</div><div class="prog-wrap"><div class="prog-fill" id="prog-fill" style="width:0%"></div></div></div>
  <div class="result-card" id="upload-result">
    <div id="dup-warning" style="display:none;background:rgba(242,166,35,.1);border:1px solid #f2a623;border-radius:10px;padding:10px;margin-bottom:12px;font-size:12px;color:#f2a623">⚠️ Esta actividad ya estaba registrada</div>
    <div class="sec-title" id="res-title">Sesión guardada ✅</div>
    <div class="grid2" id="res-meta"></div>
    <div style="margin-top:4px"><div class="sec-title">Post-sesión</div>
      <div class="form-group"><label class="form-label">RPE (esfuerzo 1-10)</label><div class="rpe-grid" id="rpe-grid"></div></div>
      <div class="grid2"><div class="form-group"><label class="form-label">Peso antes (kg)</label><input type="number" id="ps-wb" step="0.1" placeholder="89.0"></div><div class="form-group"><label class="form-label">Peso después</label><input type="number" id="ps-wa" step="0.1" placeholder="88.2"></div></div>
      <div class="grid2"><div class="form-group"><label class="form-label">Agua (L)</label><input type="number" id="ps-water" step="0.1" placeholder="1.5"></div><div class="form-group"><label class="form-label">Geles</label><input type="number" id="ps-gels" placeholder="0"></div></div>
      <div class="form-group"><label class="form-label">Gel usado</label><div class="chips" id="gel-chips"></div></div>
      <div class="grid2"><div class="form-group"><label class="form-label">Resp. GI</label><select id="ps-gi"><option value="">—</option><option value="sin-problemas">Sin problemas</option><option value="inflamacion-leve">Leve</option><option value="nausea">Náusea</option></select></div><div class="form-group"><label class="form-label">Energía</label><select id="ps-en"><option value="">—</option><option value="subidón-estable">Estable</option><option value="gradual">Gradual</option><option value="caída">Caída</option></select></div></div>
      <div class="grid2"><div class="form-group"><label class="form-label">Sueño (h)</label><input type="number" id="ps-sleep" step="0.5" placeholder="7.5"></div><div class="form-group"><label class="form-label">Calidad</label><select id="ps-sq"><option value="">—</option><option value="bueno">Bueno</option><option value="regular">Regular</option><option value="malo">Malo</option></select></div></div>
      <div class="form-group"><label class="form-label">Notas</label><textarea id="ps-notes" style="height:50px;resize:none" placeholder="Cómo se sintió..."></textarea></div>
      <button class="btn-main" onclick="savePost()">Guardar post-sesión</button></div>
  </div>
  <div style="margin-top:16px"><div class="sec-title">Últimas sesiones</div><div id="recent-sessions"><div class="loading-wrap"><div class="spin"></div></div></div></div>
</div>
<div class="screen" id="sc-fuerza" style="--c:var(--fuerza)">
  <div class="hdr"><div><div class="hdr-greet">Entrenamiento</div><div class="hdr-name" style="color:var(--fuerza)">Fuerza</div></div><div class="hdr-dot"></div></div>
  <div class="card">
    <div class="sec-title">Categoría</div><div class="chips" id="fcat-chips"></div>
    <div id="f-compex-fields"><div class="form-group"><label class="form-label">Programa Compex</label><select id="f-prog"><option value="strength">Strength</option><option value="explosive_strength">Explosive Strength</option><option value="resistance">Resistance</option><option value="strength_endurance">Strength Endurance</option></select></div><div class="form-group"><label class="form-label">Intensidad alcanzada</label><div class="int-display" id="int-display">58</div><input type="range" min="1" max="120" value="58" id="f-int-range" oninput="document.getElementById('int-display').textContent=this.value"></div></div>
    <div id="f-gym-fields" style="display:none"><div class="form-group"><label class="form-label">Ejercicio</label><input type="text" id="f-exc" placeholder="Sentadilla, Peso muerto..."></div><div class="grid2"><div class="form-group"><label class="form-label">Series</label><input type="number" id="f-sets" placeholder="3"></div><div class="form-group"><label class="form-label">Reps</label><input type="number" id="f-reps" placeholder="12"></div></div></div>
    <div class="sec-title" style="margin-top:4px">Músculos</div><div class="muscle-grid" id="muscle-grid"></div>
    <div class="grid2"><div class="form-group"><label class="form-label">Duración (min)</label><input type="number" id="f-dur" placeholder="32"></div><div class="form-group"><label class="form-label">RPE</label><input type="number" id="f-rpe" min="1" max="10" placeholder="7"></div></div>
    <div class="form-group"><label class="form-label">Notas</label><input type="text" id="f-notes" placeholder="Sensaciones..."></div>
    <button class="btn-main" onclick="saveFuerza()">Guardar sesión</button>
  </div>
  <div id="fuerza-summary"><div class="loading-wrap"><div class="spin" style="border-top-color:var(--fuerza)"></div></div></div>
</div>
<div class="screen" id="sc-wellness" style="--c:var(--wellness)">
  <div class="hdr"><div><div class="hdr-greet">Recuperación</div><div class="hdr-name" style="color:var(--wellness)">Wellness</div></div><div class="hdr-dot"></div></div>
  <div class="card">
    <div class="wtabs" id="wtabs"></div>
    <div id="well-fields"></div>
    <div class="form-group" id="well-muscle-wrap" style="display:none"><label class="form-label">Zona muscular</label><div class="chips" id="well-muscle-chips"></div></div>
    <div class="form-group"><label class="form-label">Fatiga general (1-10)</label><input type="number" id="w-fat" min="1" max="10" placeholder="5"></div>
    <div class="form-group"><label class="form-label">Notas</label><input type="text" id="w-notes" placeholder="Sensaciones..."></div>
    <button class="btn-main" onclick="saveWellness()">Guardar registro</button>
  </div>
  <div id="wellness-summary"><div class="loading-wrap"><div class="spin" style="border-top-color:var(--wellness)"></div></div></div>
</div>
<div class="screen" id="sc-stats" style="--c:var(--stats)">
  <div class="hdr"><div><div class="hdr-greet">Análisis</div><div class="hdr-name" style="color:var(--stats)">Estadísticas</div></div><div class="hdr-dot"></div></div>
  <div id="stats-content"><div class="loading-wrap"><div class="spin" style="border-top-color:var(--stats)"></div></div></div>
</div>
<div class="screen" id="sc-coach" style="--c:#f59e0b">
  <div class="hdr"><div><div class="hdr-greet">Análisis inteligente</div><div class="hdr-name" style="color:#f59e0b">Coach</div></div><div class="hdr-dot"></div></div>

  <!-- Estado actual -->
  <div id="coach-status"><div class="loading-wrap"><div class="spin" style="border-top-color:#f59e0b"></div></div></div>

  <!-- Analizador de planes -->
  <div class="card" style="margin-top:4px">
    <div class="sec-title" style="color:#f59e0b">Analizar plan de entrenamiento</div>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px;line-height:1.5">Pega aquí un plan de entrenamiento — de TrainingPeaks, Garmin Connect, un PDF, o cualquier fuente. El sistema lo analizará contra tu estado actual y te dirá si te conviene.</p>
    <div class="form-group"><label class="form-label">Plan / descripción / semanas de entrenamiento</label><textarea id="coach-plan-text" style="height:120px;resize:none" placeholder="Ej: Semana 1: Lunes Z2 60min, Miércoles Intervalos 4x8min Z4, Viernes Z2 45min, Sábado largo Z2 90min...&#10;&#10;O pega el texto completo de cualquier plan."></textarea></div>
    <div class="form-group"><label class="form-label">Objetivo de este plan</label>
      <select id="coach-goal">
        <option value="base_aerobica">Construir base aeróbica (Z2)</option>
        <option value="time_trial">Time Trial / Velocidad</option>
        <option value="resistencia">Resistencia / Fondo</option>
        <option value="subidas">Subidas / Potencia</option>
        <option value="recuperacion">Recuperación activa</option>
        <option value="otro">Otro</option>
      </select>
    </div>
    <div class="form-group"><label class="form-label">Duración del plan (semanas)</label>
      <input type="number" id="coach-weeks" placeholder="8" min="1" max="52">
    </div>
    <button class="btn-main" style="background:#f59e0b" onclick="analyzePlan()">Analizar con Amalgama →</button>
  </div>

  <!-- Buscar planes -->
  <div class="card">
    <div class="sec-title" style="color:#f59e0b">Buscar planes recomendados</div>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px;line-height:1.5">Dile a Amalgama qué tipo de plan buscas y lo analizará para ti.</p>
    <div class="chips" id="plan-type-chips"></div>
    <button class="btn-main" style="background:#f59e0b" onclick="searchPlans()">Buscar y analizar →</button>
  </div>

  <!-- Objetivos -->
  <div class="card">
    <div class="sec-title" style="color:#f59e0b">Mis objetivos</div>
    <div id="coach-goals-display"><div class="loading-wrap" style="padding:16px"><div class="spin" style="border-top-color:#f59e0b"></div></div></div>
  </div>
</div>
</div>
<nav id="bnav">
  <button class="bn active" data-screen="home" style="--c:var(--home)" onclick="switchScreen('home')"><span class="bn-ic">⊙</span><span class="bn-lb">Home</span></button>
  <button class="bn" data-screen="bici" style="--c:var(--bike)" onclick="switchScreen('bici')"><span class="bn-ic">🚴</span><span class="bn-lb">Bici</span></button>
  <button class="bn" data-screen="fuerza" style="--c:var(--fuerza)" onclick="switchScreen('fuerza')"><span class="bn-ic">💪</span><span class="bn-lb">Fuerza</span></button>
  <button class="bn" data-screen="wellness" style="--c:var(--wellness)" onclick="switchScreen('wellness')"><span class="bn-ic">🫀</span><span class="bn-lb">Wellness</span></button>
  <button class="bn" data-screen="stats" style="--c:var(--stats)" onclick="switchScreen('stats')"><span class="bn-ic">📊</span><span class="bn-lb">Stats</span></button>
  <button class="bn" data-screen="coach" style="--c:#f59e0b" onclick="switchScreen('coach')"><span class="bn-ic">🧠</span><span class="bn-lb">Coach</span></button>
</nav>
</div>
<div class="ok-toast" id="ok-toast"></div>
<script>
const API=window.location.origin;
let currentSid=null,rpe=null;
const selGel=new Set(),selFM=new Set(),selWM=new Set();
const fCat={v:'compex'},wCat={v:'compex_recovery'};
const h=new Date().getHours();
document.getElementById('hdr-greet').textContent=h<12?'Buenos días':h<18?'Buenas tardes':'Buenas noches';
function switchScreen(name){
  document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.bn').forEach(b=>b.classList.toggle('active',b.dataset.screen===name));
  document.getElementById('sc-'+name).classList.add('active');
  if(name==='home'&&!document.getElementById('home-content').querySelector('.card'))loadHome();
  if(name==='bici'&&!document.getElementById('recent-sessions').querySelector('.act-row'))loadRecent();
  if(name==='fuerza'&&!document.getElementById('fuerza-summary').querySelector('.grid2'))loadFuerzaSummary();
  if(name==='wellness'&&!document.getElementById('wellness-summary').querySelector('.grid2'))loadWellnessSummary();
  if(name==='stats'&&!document.getElementById('stats-content').querySelector('.card'))loadStats();
  if(name==='coach')loadCoachStatus();
}
function toast(msg,color){const t=document.getElementById('ok-toast');t.textContent=msg;t.style.background=color||'var(--fuerza)';t.style.color='#000';t.style.display='block';setTimeout(()=>t.style.display='none',2500);}
async function loadHome(){
  const el=document.getElementById('home-content');
  try{
    const [dash,well]=await Promise.all([fetch(API+'/gpt/dashboard').then(r=>r.json()),fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()).catch(()=>({}))]);
    const a=dash.athlete||{},s=dash.semana_actual||{},z=dash.z2_check||{};
    const fitClass=a.fitness==='subiendo'?'color:var(--fuerza)':'color:var(--bike)';
    const fatColor=a.fatiga==='baja'?'var(--fuerza)':a.fatiga==='alta'?'var(--bike)':'var(--wellness)';
    const pain=(well.molestias_activas||[]);
    const z2pct=z.pct_z2_4_semanas?Number(z.pct_z2_4_semanas).toFixed(1):0;
    el.innerHTML=`
      <div class="banner" style="background:rgba(232,89,60,.1);border:1px solid rgba(232,89,60,.2)">
        <div class="banner-tag" style="color:var(--bike)">Estado actual</div>
        <div class="banner-title">${dash.recommendation||'Sin recomendación'}</div>
      </div>
      <div class="grid2">
        <div class="card-sm"><div class="kpi-label">Fitness</div><div class="kpi-val" style="font-size:20px;${fitClass}">${a.fitness||'—'}</div></div>
        <div class="card-sm"><div class="kpi-label">Fatiga</div><div class="kpi-val" style="font-size:20px;color:${fatColor}">${a.fatiga||'—'}</div></div>
      </div>
      <div class="card">
        <div class="sec-title">Esta semana</div>
        <div class="grid3">
          <div><div class="kpi-label">km</div><div class="kpi-val" style="color:var(--bike)">${Number(s.km||0).toFixed(0)}</div></div>
          <div><div class="kpi-label">horas</div><div class="kpi-val">${Number(s.horas||0).toFixed(1)}</div></div>
          <div><div class="kpi-label">sesiones</div><div class="kpi-val">${s.sesiones||0}</div></div>
        </div>
        <div class="kpi-label" style="margin-top:8px">Z2 · objetivo 70-80%</div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
          <div class="z2-bar-wrap" style="flex:1"><div class="z2-bar-fill" style="width:${Math.min(z2pct,100)}%"></div></div>
          <span style="font-size:14px;font-weight:800;color:var(--wellness)">${z2pct}%</span>
        </div>
      </div>
      <div class="grid2">
        <div class="card-sm"><div class="kpi-label">km 2 semanas</div><div class="kpi-val" style="color:var(--bike);font-size:22px">${Number(a.km_2_semanas||0).toFixed(0)}</div></div>
        <div class="card-sm"><div class="kpi-label">Mars Index</div><div class="kpi-val" style="font-size:22px">${a.mars_index?Number(a.mars_index).toFixed(1):'—'}</div></div>
      </div>
      ${pain.length?`<div class="card" style="border-color:var(--bike)"><div class="sec-title" style="color:var(--bike)">⚠️ Molestias activas</div>${pain.map(p=>`<div class="rec-row"><span class="rec-label">${p.pain_zone||'—'}</span><span style="color:var(--bike);font-weight:700">Nivel ${p.pain_level||'?'}/10</span></div>`).join('')}</div>`:''}
      <button class="btn-sec" onclick="switchScreen('bici')">+ Subir actividad de hoy</button>`;
  }catch(e){el.innerHTML=`<div style="color:var(--muted);font-size:13px;padding:20px;text-align:center">${e.message}</div>`;}
}
function handleFile(file){
  if(!file)return;
  document.getElementById('upload-progress').style.display='block';
  document.getElementById('upload-result').classList.remove('show');
  document.getElementById('upload-label').textContent='Procesando '+file.name+'...';
  document.getElementById('prog-fill').style.width='30%';
  const fd=new FormData();fd.append('file',file);
  fetch(API+'/analyze-fit',{method:'POST',body:fd})
    .then(r=>r.json())
    .then(d=>{document.getElementById('prog-fill').style.width='100%';setTimeout(()=>{document.getElementById('upload-progress').style.display='none';showUploadResult(d);},400);})
    .catch(e=>{document.getElementById('upload-progress').style.display='none';toast('Error: '+e.message,'var(--bike)');});
}
function showUploadResult(d){
  currentSid=d.session_id;const s=d.session||{},isDup=d.duplicate===true;
  document.getElementById('dup-warning').style.display=isDup?'block':'none';
  document.getElementById('res-title').textContent=isDup?'Sesión ya registrada ⚠️':'Sesión guardada ✅';
  document.getElementById('res-meta').innerHTML=[{l:'Fecha',v:(s.start_time||'').slice(0,10)},{l:'Distancia',v:s.distance_km?s.distance_km+' km':'—'},{l:'Duración',v:s.duration_hms||'—'},{l:'FC prom.',v:s.avg_hr_bpm?s.avg_hr_bpm+' bpm':'—'}].map(m=>`<div class="card-sm"><div class="kpi-label">${m.l}</div><div style="font-size:15px;font-weight:700">${m.v}</div></div>`).join('');
  document.getElementById('upload-result').classList.add('show');
  // Show achievement toasts
  const achievements=d.achievements||[];
  if(achievements.length){
    setTimeout(()=>{
      achievements.forEach((a,i)=>setTimeout(()=>toast(a,'#f59e0b'),i*800));
    },500);
  }
  loadRecent();
  // Refresh home if visible
  if(document.getElementById('sc-home').classList.contains('active'))loadHome();
}
function initRPE(){document.getElementById('rpe-grid').innerHTML=[1,2,3,4,5,6,7,8,9,10].map(n=>`<div class="rpe-btn" onclick="selRpe(${n},this)">${n}</div>`).join('');}
function selRpe(n,el){rpe=n;document.querySelectorAll('.rpe-btn').forEach(b=>b.classList.remove('on'));el.classList.add('on');}
function initGelChips(){document.getElementById('gel-chips').innerHTML=[['casero-agave','Agave'],['casero-miel','Miel'],['comercial','Comercial'],['ninguno','Ninguno']].map(([v,l])=>`<div class="chip" onclick="togChip(selGel,'${v}',this)">${l}</div>`).join('');}
function togChip(set,v,el){set.has(v)?(set.delete(v),el.classList.remove('on')):(set.add(v),el.classList.add('on'));}
async function savePost(){
  if(!currentSid){toast('Sube una actividad primero','var(--bike)');return;}
  const body={rpe,weight_before:parseFloat(document.getElementById('ps-wb').value)||null,weight_after:parseFloat(document.getElementById('ps-wa').value)||null,water_liters:parseFloat(document.getElementById('ps-water').value)||null,gels:parseInt(document.getElementById('ps-gels').value)||null,gel_type:[...selGel][0]||null,gi_response:document.getElementById('ps-gi').value||null,energy_response:document.getElementById('ps-en').value||null,sleep_hours:parseFloat(document.getElementById('ps-sleep').value)||null,sleep_quality:document.getElementById('ps-sq').value||null,notes:document.getElementById('ps-notes').value||null};
  try{const r=await fetch(API+'/post-session/'+currentSid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(d.ok){toast('✅ Post-sesión guardada','var(--bike)');loadHome();}else toast('Error guardando','var(--bike)');}catch(e){toast('Error: '+e.message,'var(--bike)');}
}
async function loadRecent(){
  const el=document.getElementById('recent-sessions');
  try{
    const r=await fetch(API+'/gpt/latest-session?sport=cycling');const d=await r.json();
    const sess=Array.isArray(d.sessions)?d.sessions:[d];
    if(!sess[0]||!sess[0].session_id){el.innerHTML='<div style="color:var(--muted);font-size:13px;text-align:center;padding:20px">Sin sesiones recientes</div>';return;}
    el.innerHTML='<div class="card">'+sess.slice(0,5).map(s=>`<div class="act-row" onclick="location.href='/session/${s.session_id}'"><div class="act-icon" style="background:rgba(232,89,60,.15)">🚴</div><div class="act-info"><div class="act-name">${s.workout_name||'Sesión ciclismo'}</div><div class="act-meta">${fmtDate(s.start_time)} · ${s.duration_hms||'—'}</div></div><div style="text-align:right"><div class="act-km" style="color:var(--bike)">${s.distance_km||'—'} km</div><div class="act-hr">${s.avg_hr_bpm||'—'} bpm</div></div></div>`).join('')+'</div>';
  }catch(e){el.innerHTML=`<div style="color:var(--muted);font-size:13px;padding:10px">${e.message}</div>`;}
}
const MUSCLES=[['quadriceps','Cuádriceps'],['hamstrings','Isquios'],['glutes','Glúteos'],['calves','Pantorrillas'],['core','Core'],['back','Espalda'],['shoulders','Hombros'],['chest','Pecho'],['arms','Brazos']];
const FCATS=[['compex','Compex'],['gym','Gimnasio'],['plyo','Pliometría'],['core','Core'],['bands','Bandas']];
function initFuerza(){
  document.getElementById('fcat-chips').innerHTML=FCATS.map(([v,l],i)=>`<div class="chip${i===0?' on':''}" onclick="setFCat('${v}',this)">${l}</div>`).join('');
  document.getElementById('muscle-grid').innerHTML=MUSCLES.map(([v,l])=>`<div class="muscle-btn" onclick="togMuscle('${v}',this)">${l}</div>`).join('');
}
function setFCat(v,el){fCat.v=v;document.querySelectorAll('#fcat-chips .chip').forEach(c=>c.classList.remove('on'));el.classList.add('on');document.getElementById('f-compex-fields').style.display=v==='compex'?'block':'none';document.getElementById('f-gym-fields').style.display=v==='gym'?'block':'none';}
function togMuscle(v,el){selFM.has(v)?(selFM.delete(v),el.classList.remove('sel')):(selFM.add(v),el.classList.add('sel'));}
async function saveFuerza(){
  const today=new Date().toISOString().slice(0,10);
  const body={date:today,category:fCat.v,subcategory:fCat.v==='compex'?document.getElementById('f-prog').value:null,intensity:fCat.v==='compex'?parseInt(document.getElementById('f-int-range').value)||null:null,exercise:fCat.v==='gym'?document.getElementById('f-exc').value||null:null,sets:parseInt(document.getElementById('f-sets').value)||null,reps:parseInt(document.getElementById('f-reps').value)||null,muscle_groups:[...selFM],duration_min:parseInt(document.getElementById('f-dur').value)||null,rpe:parseInt(document.getElementById('f-rpe').value)||null,notes:document.getElementById('f-notes').value||null};
  try{const r=await fetch(API+'/fuerza',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(d.ok){toast('💪 Fuerza guardada','var(--fuerza)');loadFuerzaSummary();selFM.clear();document.querySelectorAll('.muscle-btn').forEach(b=>b.classList.remove('sel'));}else toast('Error guardando','var(--bike)');}catch(e){toast('Error: '+e.message,'var(--bike)');}
}
async function loadFuerzaSummary(){
  const el=document.getElementById('fuerza-summary');
  try{
    const d=await fetch(API+'/gpt/fuerza-summary?weeks=8').then(r=>r.json());
    const prog=d.compex_progresion||{};
    const ML={quadriceps:'Cuádriceps',hamstrings:'Isquios',glutes:'Glúteos',calves:'Pantorrillas',core:'Core',back:'Espalda',shoulders:'Hombros'};
    const mh=Object.entries(prog).map(([m,months])=>{const last=months[months.length-1];const first=months[0];const delta=last.max_intensity-first.max_intensity;return `<div class="card-sm"><div class="kpi-label">${ML[m]||m}</div><div style="font-size:26px;font-weight:800;color:var(--fuerza)">${last.max_intensity}</div><div class="prog-wrap"><div class="prog-fill" style="width:${Math.min(last.max_intensity,100)}%;background:var(--fuerza)"></div></div><div style="font-size:11px;color:var(--muted)">${delta>=0?'+'+delta:delta} vs inicio</div></div>`;}).join('');
    el.innerHTML=`<div class="grid2" style="margin-bottom:12px"><div class="card-sm"><div class="kpi-label">Sesiones</div><div class="kpi-val" style="color:var(--fuerza)">${d.total_sesiones||0}</div></div><div class="card-sm"><div class="kpi-label">Horas totales</div><div class="kpi-val">${d.total_horas||0}</div></div></div>${mh?`<div class="sec-title">Progresión Compex</div><div class="grid2">${mh}</div>`:'<div style="color:var(--muted);font-size:13px;text-align:center;padding:20px">Sin sesiones aún</div>'}`;
  }catch(e){el.innerHTML='';}
}
const WCATS=[['compex_recovery','Compex Rec'],['massage_gun','Pistola'],['ceragem','Ceragem'],['foam_roller','Foam'],['sleep','Sueño'],['pain','Molestia'],['stress','Estrés']];
const WMUSCLES=[['quadriceps','Cuádriceps'],['hamstrings','Isquios'],['glutes','Glúteos'],['calves','Pantorrillas'],['lower_back','Lumbar'],['upper_back','Espalda alta'],['neck','Cuello'],['it_band','IT Band']];
const WF={compex_recovery:`<div class="form-group"><label class="form-label">Programa</label><select id="w-prog"><option value="active_recovery">Active Recovery</option><option value="recovery">Recovery</option><option value="massage">Massage</option></select></div><div class="form-group"><label class="form-label">Duración (min)</label><input type="number" id="w-dur" placeholder="20"></div>`,massage_gun:`<div class="form-group"><label class="form-label">Duración (min)</label><input type="number" id="w-dur" placeholder="15"></div>`,foam_roller:`<div class="form-group"><label class="form-label">Duración (min)</label><input type="number" id="w-dur" placeholder="10"></div>`,ceragem:`<div class="grid2"><div class="form-group"><label class="form-label">Duración (min)</label><input type="number" id="w-cd" placeholder="40"></div><div class="form-group"><label class="form-label">Sensación antes</label><input type="number" id="w-cb" min="1" max="10" placeholder="6"></div></div><div class="form-group"><label class="form-label">Sensación después</label><input type="number" id="w-ca" min="1" max="10" placeholder="8"></div>`,sleep:`<div class="grid2"><div class="form-group"><label class="form-label">Horas</label><input type="number" id="w-sh" step="0.5" placeholder="7.5"></div><div class="form-group"><label class="form-label">Calidad</label><select id="w-sq"><option value="">—</option><option value="bueno">Bueno</option><option value="regular">Regular</option><option value="malo">Malo</option></select></div></div><div class="grid2"><div class="form-group"><label class="form-label">FC reposo</label><input type="number" id="w-hr" placeholder="52"></div><div class="form-group"><label class="form-label">Score Garmin</label><input type="number" id="w-gs" placeholder="78"></div></div>`,pain:`<div class="grid2"><div class="form-group"><label class="form-label">Zona</label><input type="text" id="w-pz" placeholder="Isquiotibial izq."></div><div class="form-group"><label class="form-label">Nivel (1-10)</label><input type="number" id="w-pl" min="1" max="10" placeholder="4"></div></div>`,stress:`<div class="grid2"><div class="form-group"><label class="form-label">Nivel (1-10)</label><input type="number" id="w-sl" min="1" max="10" placeholder="6"></div><div class="form-group"><label class="form-label">Causa</label><select id="w-sc"><option value="">—</option><option value="work">Trabajo</option><option value="travel">Viaje</option><option value="illness">Enfermedad</option><option value="fatigue">Fatiga</option></select></div></div>`};
function initWellness(){document.getElementById('wtabs').innerHTML=WCATS.map(([v,l],i)=>`<div class="wtab${i===0?' on':''}" onclick="setWCat('${v}',this)">${l}</div>`).join('');document.getElementById('well-muscle-chips').innerHTML=WMUSCLES.map(([v,l])=>`<div class="chip" onclick="togChip(selWM,'${v}',this)">${l}</div>`).join('');document.getElementById('well-fields').innerHTML=WF.compex_recovery;document.getElementById('well-muscle-wrap').style.display='block';}
function setWCat(v,el){wCat.v=v;document.querySelectorAll('.wtab').forEach(t=>t.classList.remove('on'));el.classList.add('on');document.getElementById('well-fields').innerHTML=WF[v]||'';document.getElementById('well-muscle-wrap').style.display=['compex_recovery','massage_gun','foam_roller'].includes(v)?'block':'none';}
async function saveWellness(){
  const today=new Date().toISOString().slice(0,10);const cat=wCat.v;
  const body={date:today,category:cat,fatigue:parseInt(document.getElementById('w-fat').value)||null,notes:document.getElementById('w-notes').value||null,muscle_zone:[...selWM]};
  const g=id=>{const el=document.getElementById(id);return el?el.value:null;};
  const gi=id=>{const el=document.getElementById(id);return el?parseInt(el.value)||null:null;};
  const gf=id=>{const el=document.getElementById(id);return el?parseFloat(el.value)||null:null;};
  if(cat==='sleep'){body.sleep_hours=gf('w-sh');body.sleep_quality=g('w-sq');body.hr_rest=gi('w-hr');body.garmin_sleep_score=gi('w-gs');}
  else if(cat==='pain'){body.pain_zone=g('w-pz');body.pain_level=gi('w-pl');}
  else if(cat==='ceragem'){body.ceragem_duration_min=gi('w-cd');body.ceragem_sensation_before=gi('w-cb');body.ceragem_sensation_after=gi('w-ca');}
  else if(cat==='stress'){body.stress_level=gi('w-sl');body.stress_cause=g('w-sc');}
  else{body.compex_program=g('w-prog');body.duration_min=gi('w-dur');}
  try{const r=await fetch(API+'/wellness',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(d.ok){toast('🫀 Wellness guardado','var(--wellness)');loadWellnessSummary();selWM.clear();document.querySelectorAll('#well-muscle-chips .chip').forEach(c=>c.classList.remove('on'));}else toast('Error guardando','var(--bike)');}catch(e){toast('Error: '+e.message,'var(--bike)');}
}
async function loadWellnessSummary(){
  const el=document.getElementById('wellness-summary');
  try{const d=await fetch(API+'/gpt/wellness-summary?weeks=4').then(r=>r.json());const pain=d.molestias_activas||[];el.innerHTML=`<div class="grid2" style="margin:12px 0"><div class="card-sm"><div class="kpi-label">Sueño prom.</div><div class="kpi-val" style="color:var(--wellness)">${d.sueno_promedio_horas||'—'}<span class="kpi-unit"> h</span></div></div><div class="card-sm"><div class="kpi-label">FC reposo</div><div class="kpi-val">${d.fc_reposo_promedio||'—'}<span class="kpi-unit"> bpm</span></div></div></div>${pain.length?`<div class="card" style="border-color:var(--bike)"><div class="sec-title" style="color:var(--bike)">⚠️ Molestias activas</div>${pain.map(p=>`<div class="rec-row"><span class="rec-label">${p.pain_zone}</span><span style="color:var(--bike);font-weight:700">Nivel ${p.pain_level}/10</span></div>`).join('')}</div>`:'<div style="color:var(--fuerza);font-size:12px;padding:8px 0">✓ Sin molestias activas</div>'}`;
  }catch(e){el.innerHTML='';}
}
async function loadStats(){
  const el=document.getElementById('stats-content');
  try{const d=await fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json());const recs=d.records||{},carga=d.carga||{};const RDEFS=[{k:'max_distance',l:'Mayor distancia',f:v=>Number(v).toFixed(1)+' km'},{k:'max_duration',l:'Sesión más larga',f:v=>{const s=parseInt(v);return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m'}},{k:'max_ascent',l:'Mayor ascenso',f:v=>parseInt(v)+' m'},{k:'max_speed',l:'Mayor velocidad',f:v=>Number(v).toFixed(1)+' km/h'},{k:'min_avg_hr',l:'FC mínima',f:v=>parseInt(v)+' bpm'}];const tsbColor=carga.estado==='fresco'?'var(--fuerza)':carga.estado==='fatigado'?'var(--bike)':'var(--wellness)';el.innerHTML=`<div class="grid2"><div class="card-sm"><div class="kpi-label">VO2Max est.</div><div class="kpi-val" style="color:var(--stats)">${d.vo2max_estimado||'—'}</div><div class="kpi-unit">ml/kg/min</div></div><div class="card-sm"><div class="kpi-label">Estado carga</div><div class="kpi-val" style="font-size:18px;color:${tsbColor}">${carga.estado||'—'}</div><div class="kpi-unit">TSB ${carga.tsb||0}</div></div></div><div class="card"><div class="sec-title">Récords personales</div>${RDEFS.map(rd=>{const v=recs[rd.k];if(!v)return '';return `<div class="rec-row"><div><div class="rec-label">${rd.l}</div><div class="rec-date">${v.date}</div></div><div class="rec-val" style="color:var(--stats)">${rd.f(v.value)}</div></div>`;}).join('')}</div><div class="grid2"><div class="card-sm"><div class="kpi-label">Eficiencia 6m</div><div class="kpi-val" style="font-size:20px">${d.eficiencia_aerobica?.delta_pct_6_meses!=null?(d.eficiencia_aerobica.delta_pct_6_meses>=0?'+':'')+Number(d.eficiencia_aerobica.delta_pct_6_meses).toFixed(1)+'%':'—'}</div></div><div class="card-sm"><div class="kpi-label">Cadencia</div><div class="kpi-val" style="font-size:20px">${d.cadencia_trend||'—'}</div></div></div>`;
  }catch(e){el.innerHTML=`<div style="color:var(--muted);font-size:13px;padding:20px">${e.message}</div>`;}
}
const dz=document.getElementById('drop-zone');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');const f=e.dataTransfer.files[0];if(f)handleFile(f);});
// ── COACH ──
const PLAN_TYPES=[['z2_base','Base Z2'],['intervals','Intervalos'],['hills','Subidas'],['recovery','Recuperación'],['race_prep','Preparación carrera'],['custom','Personalizado']];
let selPlanType=new Set();

async function loadCoachStatus(){
  const el=document.getElementById('coach-status');
  try{
    const [dash,base]=await Promise.all([
      fetch(API+'/gpt/dashboard').then(r=>r.json()),
      fetch(API+'/gpt/baseline-compare').then(r=>r.json()).catch(()=>({}))
    ]);
    const a=dash.athlete||{},c=dash.carga||{};
    const tsb=c.tsb||0;
    const readyColor=tsb>5?'var(--fuerza)':tsb<-15?'var(--bike)':'#f59e0b';
    const readyMsg=tsb>5?'✅ Listo para carga':tsb<-15?'⚠️ Necesitas recuperación':'🔄 En carga moderada';
    el.innerHTML=`
      <div class="banner" style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25);margin-bottom:12px">
        <div class="banner-tag" style="color:#f59e0b">Estado para entrenar</div>
        <div class="banner-title" style="color:${readyColor}">${readyMsg}</div>
      </div>
      <div class="grid3">
        <div class="card-sm"><div class="kpi-label">TSB</div><div class="kpi-val" style="color:#f59e0b;font-size:24px">${tsb}</div><div class="kpi-unit">${c.estado||'—'}</div></div>
        <div class="card-sm"><div class="kpi-label">Fitness</div><div class="kpi-val" style="font-size:20px">${a.fitness||'—'}</div></div>
        <div class="card-sm"><div class="kpi-label">Fatiga</div><div class="kpi-val" style="font-size:20px">${a.fatiga||'—'}</div></div>
      </div>`;

    // Goals display
    const gd=document.getElementById('coach-goals-display');
    const goals=[
      {label:'FC objetivo',current:a.eficiencia||'—',target:'<135 bpm sostenido'},
      {label:'Cadencia',current:dash.cadencia_trend||'—',target:'80-85 rpm'},
      {label:'Z2 semanal',current:(dash.z2_check?.pct_z2_4_semanas||0).toFixed(1)+'%',target:'70-80%'},
      {label:'Eficiencia vel/FC',current:'0.148',target:'0.155+'},
    ];
    gd.innerHTML=goals.map(g=>`<div class="rec-row"><div><div style="font-size:13px;font-weight:600">${g.label}</div><div style="font-size:11px;color:var(--muted)">Objetivo: ${g.target}</div></div><div style="text-align:right"><div style="font-size:14px;font-weight:700;color:#f59e0b">${g.current}</div></div></div>`).join('');
  }catch(e){el.innerHTML=`<div style="color:var(--muted);font-size:13px;padding:20px">${e.message}</div>`;}
}

function initPlanChips(){
  document.getElementById('plan-type-chips').innerHTML=PLAN_TYPES.map(([v,l])=>`<div class="chip" onclick="togChip(selPlanType,'${v}',this)">${l}</div>`).join('');
}

function analyzePlan(){
  const plan=document.getElementById('coach-plan-text').value.trim();
  const goal=document.getElementById('coach-goal').value;
  const weeks=document.getElementById('coach-weeks').value;
  if(!plan){toast('Pega un plan primero','#f59e0b');return;}
  const goalLabels={base_aerobica:'base aeróbica Z2',time_trial:'Time Trial y velocidad',resistencia:'resistencia y fondo',subidas:'subidas y potencia',recuperacion:'recuperación activa',otro:'entrenamiento general'};
  const prompt=`Analiza este plan de entrenamiento para ${weeks||'varias'} semanas con objetivo de ${goalLabels[goal]||goal}. Compara contra mi estado actual usando getDashboard y getBaselineCompare. Dime: 1) ¿Es compatible con mi nivel actual? 2) ¿Qué semanas son más exigentes? 3) ¿Qué ajustaría para mis zonas Mars (LT=168, Z2=134-150 bpm)? 4) ¿Cuánto tiempo necesito para notar mejoras?\n\nPLAN:\n${plan}`;
  window.open('https://chatgpt.com/g/g-6a17395da59081919dc10076cd7304ff-amalgama?prompt='+encodeURIComponent(prompt),'_blank');
}

function searchPlans(){
  const types=[...selPlanType];
  if(!types.length){toast('Selecciona al menos un tipo','#f59e0b');return;}
  const typeLabels={z2_base:'base aeróbica Z2',intervals:'intervalos',hills:'subidas',recovery:'recuperación',race_prep:'preparación para carrera',custom:'personalizado'};
  const typeStr=types.map(t=>typeLabels[t]||t).join(', ');
  const prompt=`Busca en internet planes de entrenamiento ciclista para ${typeStr}. Luego usa getDashboard y getBaselineCompare para ver mi estado actual. Dame 2-3 opciones de planes recomendados que sean compatibles con mi nivel (FC promedio ${137} bpm, Z2 67%, eficiencia 0.148 vel/FC, plan Garmin Time Trial). Para cada opción explica por qué me conviene o no.`;
  window.open('https://chatgpt.com/g/g-6a17395da59081919dc10076cd7304ff-amalgama?prompt='+encodeURIComponent(prompt),'_blank');
}

function fmtDate(str){
  if(!str)return '—';
  const d=new Date(str.slice(0,10)+'T12:00:00');
  const MESES=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
  return d.getDate()+' '+MESES[d.getMonth()]+' '+d.getFullYear();
}
initRPE();initGelChips();initFuerza();initWellness();initPlanChips();loadHome();
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
        # Compute SHA256 hash of file for reliable duplicate detection
        import hashlib
        file_hash = hashlib.sha256(fit_bytes).hexdigest()

        conn = get_db()
        duplicate_sid = None
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
                else:
                    logger.info(f"DUPLICATE detected: {file.filename} matches existing {duplicate_sid}")
            except Exception as e:
                logger.error(f"DB save error filename={file.filename} error={e}")
        logger.info(f"UPLOAD ok session_id={sid} filename={file.filename} duplicate={duplicate_sid}")
        r = {k:v for k,v in result.items() if k != "records"}
        achievements = result.get("achievements", [])
        if duplicate_sid:
            return {"session_id": duplicate_sid,
                    "message": f"⚠️ Esta actividad ya fue subida (session_id: {duplicate_sid}). Abriendo sesión existente.",
                    "charts_url": f"/charts/{duplicate_sid}",
                    "duplicate": True,
                    "achievements": [],
                    **r}
        return {"session_id":sid,
                "message":f"✅ Guardado. Pasa el session_id '{sid}' al GPT.",
                "charts_url":f"/charts/{sid}",
                "duplicate": False,
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
                cur.execute("SELECT result_json FROM sessions WHERE session_id=%s", (session_id,))
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


# ─────────────────────────────────────────────────────────────────────────────
# /activities  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/activities", response_class=HTMLResponse)
def activities_page():
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js')}}</script>
<title>Bitácora — Actividades</title>
<style>{DARK_CSS}</style>
</head>
<body>
{DARK_NAV}
<div class="page">
  <div class="page-hdr">
    <div class="page-title">Actividades</div>
    <div class="page-sub" id="page-sub">Cargando...</div>
  </div>

  <div class="layout-sidebar">
    <!-- Sidebar filters -->
    <div class="sidebar">
      <div class="sidebar-section">
        <span class="sidebar-label">Deporte</span>
        <button class="sport-btn on" onclick="setSport('',this)">
          <span class="sport-dot" style="background:var(--text)"></span>Todos
          <span class="sport-count" id="cnt-all">—</span>
        </button>
        <div id="sport-btns"></div>
      </div>
      <div class="sidebar-divider"></div>
      <div class="sidebar-section">
        <span class="sidebar-label">Buscar</span>
        <input type="text" class="filter-input" placeholder="Nombre o fecha..." id="search-input" oninput="filterSessions()" style="margin:0">
      </div>
      <div class="sidebar-divider"></div>
      <div class="sidebar-section">
        <span class="sidebar-label">Ordenar</span>
        <button class="sport-btn on" onclick="setSort('recent',this)">Más reciente</button>
        <button class="sport-btn" onclick="setSort('distance',this)">Mayor distancia</button>
        <button class="sport-btn" onclick="setSort('duration',this)">Mayor duración</button>
        <button class="sport-btn" onclick="setSort('ascent',this)">Mayor ascenso</button>
      </div>
    </div>

    <!-- Activity list -->
    <div>
      <div id="act-list"><div class="loading"><div class="spinner"></div>Cargando actividades...</div></div>
      <div class="pagination" id="pagination" style="display:none">
        <button class="pag-btn" id="pag-prev" onclick="changePage(-1)">← Anterior</button>
        <span class="pag-info" id="pag-info"></span>
        <button class="pag-btn" id="pag-next" onclick="changePage(1)">Siguiente →</button>
      </div>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;
let allSessions = [], filtered = [], currentSort = 'recent', currentSport = '', currentPage = 0;
const PAGE_SIZE = 20;

function fmtDate(s){{
  if(!s)return '—';
  const d=new Date((s+'').slice(0,10)+'T12:00:00');
  const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
  return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
}}
function hms(s){{
  s=parseInt(s)||0;const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h?h+'h '+String(m).padStart(2,'0')+'m':m+'m';
}}
function sportTag(sp){{
  const tags={{cycling:'cycling',running:'running',walking:'walking',training:'training',swimming:'swimming'}};
  return tags[sp]||'default';
}}
function sportLabel(sp){{
  const labels={{cycling:'Ciclismo',running:'Carrera',walking:'Caminata',training:'Entrenamiento',swimming:'Natación',generic:'Genérico'}};
  return labels[sp]||sp||'Actividad';
}}
function sportDot(sp){{
  const cols={{cycling:'var(--bike)',running:'var(--green)',walking:'var(--wellness)',training:'var(--yellow)',swimming:'var(--stats)'}};
  return cols[sp]||'var(--muted)';
}}

async function loadSessions(){{
  try{{
    const d = await fetch(API+'/sessions?limit=500&sort=recent').then(r=>r.json());
    allSessions = d.sessions||[];
    buildSportBtns();
    filterSessions();
    document.getElementById('page-sub').textContent = allSessions.length+' actividades en total';
    document.getElementById('cnt-all').textContent = allSessions.length;
  }}catch(e){{
    document.getElementById('act-list').innerHTML='<div style="color:var(--muted);padding:20px">'+e.message+'</div>';
  }}
}}

function buildSportBtns(){{
  const sports = [...new Set(allSessions.map(s=>s.sport).filter(Boolean))];
  document.getElementById('sport-btns').innerHTML = sports.map(sp=>
    `<button class="sport-btn" onclick="setSport('${{sp}}',this)">
      <span class="sport-dot" style="background:${{sportDot(sp)}}"></span>${{sportLabel(sp)}}
      <span class="sport-count">${{allSessions.filter(s=>s.sport===sp).length}}</span>
    </button>`
  ).join('');
}}

function setSport(sp, el){{
  currentSport=sp; currentPage=0;
  document.querySelectorAll('.sport-btn').forEach(b=>b.classList.remove('on'));
  el.classList.add('on');
  filterSessions();
}}
function setSort(sort, el){{
  currentSort=sort; currentPage=0;
  document.querySelectorAll('.sidebar .sport-btn').forEach(b=>b.classList.remove('on'));
  el.classList.add('on');
  filterSessions();
}}

function filterSessions(){{
  const q = (document.getElementById('search-input').value||'').toLowerCase();
  filtered = allSessions.filter(s=>{{
    if(currentSport && s.sport!==currentSport)return false;
    if(q && !(s.workout_name||'').toLowerCase().includes(q) && !(s.start_time||'').includes(q))return false;
    return true;
  }});
  const sortFns={{
    recent:(a,b)=>new Date(b.start_time||0)-new Date(a.start_time||0),
    distance:(a,b)=>(b.distance_km||0)-(a.distance_km||0),
    duration:(a,b)=>(b.duration_s||0)-(a.duration_s||0),
    ascent:(a,b)=>(b.ascent_m||0)-(a.ascent_m||0),
  }};
  filtered.sort(sortFns[currentSort]||sortFns.recent);
  renderPage();
}}

function renderPage(){{
  const start = currentPage*PAGE_SIZE, end = start+PAGE_SIZE;
  const page = filtered.slice(start,end);
  const el = document.getElementById('act-list');
  if(!page.length){{el.innerHTML='<div style="color:var(--muted);text-align:center;padding:40px">Sin actividades</div>';return;}}

  el.innerHTML = page.map(s=>{{
    const tag = sportTag(s.sport);
    return `<div class="act-card" onclick="location.href='/charts/${{s.session_id}}'">
      <div class="act-stripe ${{tag}}"></div>
      <div class="act-body">
        <div class="act-info">
          <div class="act-name">${{s.workout_name||sportLabel(s.sport)||'Actividad'}}</div>
          <div class="act-date">${{fmtDate(s.start_time)}}</div>
          <div class="act-metrics">
            ${{s.distance_km?`<div><div class="act-metric-val">${{s.distance_km}} km</div><div class="act-metric-lbl">Distancia</div></div>`:''}}
            ${{s.duration_s?`<div><div class="act-metric-val">${{hms(s.duration_s)}}</div><div class="act-metric-lbl">Duración</div></div>`:''}}
            ${{s.avg_hr_bpm?`<div><div class="act-metric-val">${{s.avg_hr_bpm}} bpm</div><div class="act-metric-lbl">FC prom.</div></div>`:''}}
            ${{s.avg_speed_kmh&&s.avg_speed_kmh>0?`<div><div class="act-metric-val">${{Number(s.avg_speed_kmh).toFixed(1)}} km/h</div><div class="act-metric-lbl">Velocidad</div></div>`:''}}
            ${{s.ascent_m?`<div><div class="act-metric-val">+${{s.ascent_m}} m</div><div class="act-metric-lbl">Ascenso</div></div>`:''}}
          </div>
        </div>
        <div class="act-right">
          <div class="act-sport-tag ${{tag}}">${{sportLabel(s.sport)}}</div>
          <a class="act-link" href="/charts/${{s.session_id}}" onclick="event.stopPropagation()">Ver gráficas →</a>
        </div>
      </div>
    </div>`;
  }}).join('');

  // Pagination
  const totalPages = Math.ceil(filtered.length/PAGE_SIZE);
  const pag = document.getElementById('pagination');
  pag.style.display = totalPages>1?'flex':'none';
  document.getElementById('pag-prev').disabled = currentPage===0;
  document.getElementById('pag-next').disabled = currentPage>=totalPages-1;
  document.getElementById('pag-info').textContent = 'Página '+(currentPage+1)+' de '+totalPages+' · '+filtered.length+' actividades';
}}

function changePage(delta){{
  const totalPages = Math.ceil(filtered.length/PAGE_SIZE);
  currentPage = Math.max(0,Math.min(totalPages-1,currentPage+delta));
  renderPage(); window.scrollTo(0,0);
}}

loadSessions();
</script>
</body>
</html>"""


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



# ─────────────────────────────────────────────────────────────────────────────
# /calendar  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

CALENDAR_HTML = ""  # keep variable for compatibility

@app.get("/calendar", response_class=HTMLResponse)
def calendar_page():
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js')}}</script>
<title>Bitácora — Calendario</title>
<style>{DARK_CSS}
.year-nav{{display:flex;align-items:center;gap:12px;margin-bottom:16px}}
.year-btn{{width:34px;height:34px;border-radius:10px;border:1px solid var(--stroke);
  background:var(--surface2);font-size:16px;cursor:pointer;color:var(--text);
  display:flex;align-items:center;justify-content:center;transition:all .15s}}
.year-btn:hover{{background:var(--bike);border-color:var(--bike);color:#08090b}}
.year-display{{font-size:22px;font-weight:900;letter-spacing:-.03em}}
.months-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}}
.month-card{{background:linear-gradient(180deg,var(--surface2),var(--surface));
  border:1px solid var(--stroke);border-radius:var(--radius-lg);padding:16px}}
.month-title{{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:12px;display:flex;justify-content:space-between}}
.month-title span{{color:var(--bike);font-size:12px}}
.dow-hdr{{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:4px}}
.dow-lbl{{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--muted2);text-align:center;padding:3px 0}}
.days-grid{{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}}
.day-cell{{aspect-ratio:1;border-radius:8px;display:flex;flex-direction:column;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;cursor:default;
  background:var(--surface3);color:var(--muted2);position:relative}}
.day-cell.empty{{background:transparent}}
.day-cell.today{{border:1px solid var(--bike);color:var(--bike)}}
.day-cell.has-c1{{background:rgba(232,89,60,.12);color:var(--text)}}
.day-cell.has-c2{{background:rgba(232,89,60,.28);color:var(--text)}}
.day-cell.has-c3{{background:rgba(232,89,60,.50);color:var(--text)}}
.day-cell.has-c4{{background:rgba(232,89,60,.75);color:#fff}}
.day-dot{{width:4px;height:4px;border-radius:50%;background:var(--bike);position:absolute;bottom:3px}}
.summary-strip{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}}
.hm-legend{{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted);flex-wrap:wrap}}
.hm-swatch{{width:16px;height:16px;border-radius:5px;flex-shrink:0}}
</style>
</head>
<body>
{DARK_NAV}
<div class="page">
  <div class="page-hdr">
    <div class="page-title">Calendario</div>
    <div class="page-sub">Heatmap de actividad anual</div>
  </div>

  <!-- Year summary KPIs -->
  <div class="kpi-grid-4" id="year-kpis">
    <div class="kpi-box"><div class="kpi-label">Días activos</div><div class="kpi-val" id="sk-days" style="color:var(--bike)">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Total km</div><div class="kpi-val" id="sk-km" style="font-size:22px">— <span style="font-size:11px;color:var(--muted)">km</span></div></div>
    <div class="kpi-box"><div class="kpi-label">Total sesiones</div><div class="kpi-val" id="sk-sessions">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Total ascenso</div><div class="kpi-val" style="font-size:20px" id="sk-ascent">— <span style="font-size:11px;color:var(--muted)">m</span></div></div>
  </div>

  <!-- Year nav -->
  <div class="year-nav">
    <button class="year-btn" onclick="changeYear(-1)">←</button>
    <div class="year-display" id="year-display">2026</div>
    <button class="year-btn" onclick="changeYear(1)">→</button>
  </div>

  <!-- Legend -->
  <div class="hm-legend" style="margin-bottom:16px">
    <span>Intensidad:</span>
    <span class="hm-swatch" style="background:var(--surface3)"></span><span>Sin actividad</span>
    <span class="hm-swatch" style="background:rgba(232,89,60,.12)"></span><span>Baja</span>
    <span class="hm-swatch" style="background:rgba(232,89,60,.35)"></span><span>Media</span>
    <span class="hm-swatch" style="background:rgba(232,89,60,.6)"></span><span>Alta</span>
    <span class="hm-swatch" style="background:rgba(232,89,60,.85)"></span><span>Intensa</span>
  </div>

  <div class="months-grid" id="months-grid"><div class="loading"><div class="spinner"></div>Cargando calendario...</div></div>
</div>

<script>
const API = window.location.origin;
let currentYear = new Date().getFullYear();

const MONTH_NAMES=['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
const DOW=['L','M','X','J','V','S','D'];
const today = new Date();

function changeYear(d){{
  currentYear += d;
  document.getElementById('year-display').textContent = currentYear;
  loadCalendar();
}}

async function loadCalendar(){{
  const el = document.getElementById('months-grid');
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Cargando...</div>';
  try{{
    const data = await fetch(API+'/gpt/calendar-heatmap?year='+currentYear).then(r=>r.json());
    const heatmap = data.heatmap||{{}};
    const sessionsByDate = data.sessions_by_date||heatmap;

    // Build summary
    const days = Object.keys(sessionsByDate);
    const totalDays = days.length;
    let totalKm=0, totalSessions=0, totalAscent=0;
    days.forEach(d=>{{
      const entry = sessionsByDate[d];
      if(typeof entry==='object'){{totalKm+=entry.km||0;totalSessions+=entry.count||1;totalAscent+=entry.ascent||0;}}
      else totalSessions++;
    }});
    document.getElementById('sk-days').textContent = totalDays;
    document.getElementById('sk-km').innerHTML = totalKm.toFixed(0)+' <span style="font-size:11px;color:var(--muted)">km</span>';
    document.getElementById('sk-sessions').textContent = totalSessions;
    document.getElementById('sk-ascent').innerHTML = totalAscent.toFixed(0)+' <span style="font-size:11px;color:var(--muted)">m</span>';

    // Compute max km for color scale
    const kmVals = days.map(d=>{{const e=sessionsByDate[d];return typeof e==='object'?e.km||0:0;}});
    const maxKm = Math.max(...kmVals, 1);

    // Render months
    el.innerHTML = '';
    for(let m=0;m<12;m++){{
      const firstDay = new Date(currentYear, m, 1);
      const daysInMonth = new Date(currentYear, m+1, 0).getDate();
      const startDow = (firstDay.getDay()+6)%7; // 0=Mon

      let html = '<div class="month-card">';
      const monthSessions = days.filter(d=>d.startsWith(currentYear+'-'+String(m+1).padStart(2,'0'))).length;
      html += `<div class="month-title">${{MONTH_NAMES[m]}}<span>${{monthSessions}} sesiones</span></div>`;
      html += '<div class="dow-hdr">'+DOW.map(d=>`<div class="dow-lbl">${{d}}</div>`).join('')+'</div>';
      html += '<div class="days-grid">';
      // Empty cells before first
      for(let i=0;i<startDow;i++) html+='<div class="day-cell empty"></div>';
      for(let day=1;day<=daysInMonth;day++){{
        const dateStr = currentYear+'-'+String(m+1).padStart(2,'0')+'-'+String(day).padStart(2,'0');
        const entry = sessionsByDate[dateStr];
        const isToday = today.getFullYear()===currentYear && today.getMonth()===m && today.getDate()===day;
        let cls = 'day-cell';
        if(isToday) cls+=' today';
        if(entry){{
          const km = typeof entry==='object'?entry.km||0:0;
          const pct = km/maxKm;
          if(pct<0.25) cls+=' has-c1';
          else if(pct<0.5) cls+=' has-c2';
          else if(pct<0.75) cls+=' has-c3';
          else cls+=' has-c4';
        }}
        const dot = entry?'<div class="day-dot"></div>':'';
        html+=`<div class="${{cls}}" title="${{dateStr}}">${{day}}${{dot}}</div>`;
      }}
      html += '</div></div>';
      el.innerHTML += html;
    }}
  }}catch(e){{
    el.innerHTML='<div style="color:var(--muted);padding:20px">'+e.message+'</div>';
  }}
}}

document.getElementById('year-display').textContent = currentYear;
loadCalendar();
</script>
</body>
</html>"""


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



# ─────────────────────────────────────────────────────────────────────────────
# /performance  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

PERFORMANCE_HTML = ""  # keep for compatibility

@app.get("/performance", response_class=HTMLResponse)
def performance_page():
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js')}}</script>
<title>Bitácora — Rendimiento</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{DARK_CSS}
.chart-wrap{{height:180px;margin:14px 0}}
.rec-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:14px}}
.rec-box{{background:var(--surface2);border:1px solid var(--stroke);border-radius:var(--radius-md);padding:14px;cursor:pointer;transition:border-color .15s}}
.rec-box:hover{{border-color:var(--bike)}}
.rec-ico{{font-size:20px;margin-bottom:6px}}
.rec-label{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;margin-bottom:4px}}
.rec-value{{font-size:20px;font-weight:900;letter-spacing:-.03em}}
.rec-unit{{font-size:10px;color:var(--muted)}}
.rec-date{{font-size:10px;color:var(--muted2);margin-top:3px}}
</style>
</head>
<body>
{DARK_NAV}
<div class="page">
  <div class="page-hdr">
    <div class="page-title">Rendimiento</div>
    <div class="page-sub">VO2Max estimado, carga de entrenamiento y marcas personales</div>
  </div>

  <!-- Top KPIs -->
  <div class="kpi-grid-4" id="perf-kpis">
    <div class="kpi-box"><div class="kpi-label">VO2Max est.</div><div class="kpi-val" id="pk-vo2" style="color:var(--stats)">—</div><div class="kpi-unit">ml/kg/min</div></div>
    <div class="kpi-box"><div class="kpi-label">Estado carga</div><div class="kpi-val" style="font-size:18px" id="pk-estado">—</div><div class="kpi-unit" id="pk-tsb">TSB —</div></div>
    <div class="kpi-box"><div class="kpi-label">Eficiencia 6m</div><div class="kpi-val" id="pk-eff" style="font-size:22px">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Cadencia</div><div class="kpi-val" id="pk-cad" style="font-size:22px">—</div></div>
  </div>

  <!-- Carga ATL/CTL/TSB chart -->
  <div class="card">
    <div class="card-hdr"><h3>Carga de entrenamiento</h3><span id="carga-estado-badge"></span></div>
    <div class="chart-wrap"><canvas id="carga-chart"></canvas></div>
    <div class="kpi-grid-3" style="margin:0">
      <div class="kpi-box"><div class="kpi-label">ATL (Forma aguda)</div><div class="kpi-val" style="font-size:20px" id="ck-atl">—</div></div>
      <div class="kpi-box"><div class="kpi-label">CTL (Fitness crónico)</div><div class="kpi-val" style="font-size:20px" id="ck-ctl">—</div></div>
      <div class="kpi-box"><div class="kpi-label">TSB (Balance)</div><div class="kpi-val" style="font-size:20px" id="ck-tsb">—</div></div>
    </div>
  </div>

  <!-- Zonas FC -->
  <div class="card">
    <div class="card-title">Distribución Zonas FC — 4 semanas</div>
    <div id="zones-chart">
      <div class="zone-row"><div class="zone-dot" style="background:#4a9eff"></div><div class="zone-lbl">Z1 — Recuperación (&lt;108 bpm)</div><div class="zone-bar-wrap"><div class="zone-bar-fill" id="z1-bar" style="background:#4a9eff;width:0%"></div></div><div class="zone-time" id="z1-pct">—</div></div>
      <div class="zone-row"><div class="zone-dot" style="background:#3dd68c"></div><div class="zone-lbl">Z2 — Aeróbico (134-150 bpm)</div><div class="zone-bar-wrap"><div class="zone-bar-fill" id="z2-bar" style="background:#3dd68c;width:0%"></div></div><div class="zone-time" id="z2-pct">—</div></div>
      <div class="zone-row"><div class="zone-dot" style="background:#f59e0b"></div><div class="zone-lbl">Z3 — Tempo (151-160 bpm)</div><div class="zone-bar-wrap"><div class="zone-bar-fill" id="z3-bar" style="background:#f59e0b;width:0%"></div></div><div class="zone-time" id="z3-pct">—</div></div>
      <div class="zone-row"><div class="zone-dot" style="background:#e8593c"></div><div class="zone-lbl">Z4 — Umbral (161-168 bpm)</div><div class="zone-bar-wrap"><div class="zone-bar-fill" id="z4-bar" style="background:#e8593c;width:0%"></div></div><div class="zone-time" id="z4-pct">—</div></div>
      <div class="zone-row"><div class="zone-dot" style="background:#c026d3"></div><div class="zone-lbl">Z5 — Máximo (&gt;168 bpm)</div><div class="zone-bar-wrap"><div class="zone-bar-fill" id="z5-bar" style="background:#c026d3;width:0%"></div></div><div class="zone-time" id="z5-pct">—</div></div>
    </div>
  </div>

  <!-- Eficiencia aeróbica -->
  <div class="card">
    <div class="card-hdr"><h3>Eficiencia aeróbica (vel/FC ratio)</h3></div>
    <div class="chart-wrap"><canvas id="eff-chart"></canvas></div>
    <div class="kpi-grid-3" style="margin-top:10px;margin-bottom:0">
      <div class="kpi-box"><div class="kpi-label">Ratio actual</div><div class="kpi-val" style="font-size:20px" id="ek-current">—</div></div>
      <div class="kpi-box"><div class="kpi-label">Línea base</div><div class="kpi-val" style="font-size:20px" id="ek-base">0.1483</div></div>
      <div class="kpi-box"><div class="kpi-label">Objetivo</div><div class="kpi-val" style="font-size:20px;color:var(--green)" id="ek-target">0.155+</div></div>
    </div>
  </div>

  <!-- Récords personales -->
  <div class="card">
    <div class="card-title">Récords personales</div>
    <div class="rec-grid" id="records-grid">
      <div class="loading"><div class="spinner"></div></div>
    </div>
  </div>

  <!-- Top 10 rides -->
  <div class="card">
    <div class="card-hdr"><h3>Top sesiones por distancia</h3><span>cycling</span></div>
    <div id="top-rides"><div class="loading"><div class="spinner"></div></div></div>
  </div>
</div>

<script>
const API = window.location.origin;
let cargaChart=null, effChart=null;

function fmtDate(s){{
  if(!s)return '—';
  const d=new Date((s+'').slice(0,10)+'T12:00:00');
  const M=['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
  return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
}}
function hms(s){{s=parseInt(s)||0;const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h?h+'h '+String(m).padStart(2,'0')+'m':m+'m';}}

async function loadPerformance(){{
  try{{
    const [perf, zones, eff] = await Promise.all([
      fetch(API+'/gpt/performance-profile?sport=cycling').then(r=>r.json()),
      fetch(API+'/gpt/zones-summary?sport=cycling&weeks=4').then(r=>r.json()).catch(()=>({{}})),
      fetch(API+'/gpt/efficiency-trend?sport=cycling').then(r=>r.json()).catch(()=>({{}})),
    ]);

    const c=perf.carga||{{}}, r=perf.records||{{}};
    const tsbColor=c.estado==='fresco'?'var(--green)':c.estado==='fatigado'?'var(--red)':'var(--wellness)';

    document.getElementById('pk-vo2').textContent = perf.vo2max_estimado||'—';
    document.getElementById('pk-estado').style.color = tsbColor;
    document.getElementById('pk-estado').textContent = c.estado||'—';
    document.getElementById('pk-tsb').textContent = 'TSB '+(c.tsb||0);
    const eff6 = perf.eficiencia_aerobica?.delta_pct_6_meses;
    document.getElementById('pk-eff').textContent = eff6!=null?(eff6>=0?'+':'')+Number(eff6).toFixed(1)+'%':'—';
    document.getElementById('pk-cad').textContent = perf.cadencia_trend||'—';

    document.getElementById('ck-atl').textContent = c.atl||'—';
    document.getElementById('ck-ctl').textContent = c.ctl||'—';
    document.getElementById('ck-tsb').style.color = tsbColor;
    document.getElementById('ck-tsb').textContent = c.tsb||'—';

    const badge = document.getElementById('carga-estado-badge');
    const badgeCls = c.estado==='fresco'?'badge-green':c.estado==='fatigado'?'badge-red':'badge-blue';
    badge.className = 'badge '+badgeCls;
    badge.textContent = c.estado||'—';

    // Carga chart
    if(c.history&&c.history.length){{
      if(cargaChart)cargaChart.destroy();
      cargaChart = new Chart(document.getElementById('carga-chart').getContext('2d'),{{
        type:'line',
        data:{{
          labels:c.history.map(h=>fmtDate(h.date)),
          datasets:[
            {{label:'CTL',data:c.history.map(h=>h.ctl),borderColor:'var(--wellness)',tension:.4,pointRadius:0}},
            {{label:'ATL',data:c.history.map(h=>h.atl),borderColor:'var(--bike)',tension:.4,pointRadius:0}},
            {{label:'TSB',data:c.history.map(h=>h.tsb),borderColor:'var(--green)',tension:.4,pointRadius:0,borderDash:[4,4]}},
          ]
        }},
        options:{{responsive:true,maintainAspectRatio:false,
          plugins:{{legend:{{labels:{{color:'#8e95a3',font:{{size:10}}}}}}}},
          scales:{{
            x:{{ticks:{{color:'#5f6673',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'rgba(255,255,255,.04)'}}}},
            y:{{ticks:{{color:'#5f6673',font:{{size:9}}}},grid:{{color:'rgba(255,255,255,.04)'}}}}
          }}}}
      }});
    }}

    // Zones
    const zd = zones.zonas||{{}};
    const zKeys=['z1','z2','z3','z4','z5'];
    const zPcts = zKeys.map(k=>Number(zd[k+'_pct']||zd[k]||0));
    const zMax = Math.max(...zPcts, 1);
    zKeys.forEach((k,i)=>{{
      const pct = zPcts[i];
      document.getElementById(k+'-bar').style.width = (pct/zMax*100)+'%';
      document.getElementById(k+'-pct').textContent = pct.toFixed(1)+'%';
    }});

    // Efficiency chart
    if(eff.trend&&eff.trend.length){{
      if(effChart)effChart.destroy();
      effChart = new Chart(document.getElementById('eff-chart').getContext('2d'),{{
        type:'line',
        data:{{
          labels:eff.trend.map(t=>fmtDate(t.date||t.month)),
          datasets:[
            {{label:'Eficiencia vel/FC',data:eff.trend.map(t=>t.ratio||t.efficiency),borderColor:'var(--stats)',backgroundColor:'rgba(167,139,250,.08)',tension:.4,pointRadius:3}},
          ]
        }},
        options:{{responsive:true,maintainAspectRatio:false,
          plugins:{{legend:{{labels:{{color:'#8e95a3',font:{{size:10}}}}}},annotation:{{annotations:{{line1:{{type:'line',yMin:0.155,yMax:0.155,borderColor:'var(--green)',borderWidth:1,borderDash:[4,4],label:{{content:'Objetivo',enabled:true,color:'var(--green)'}}}}}}}}}},
          scales:{{
            x:{{ticks:{{color:'#5f6673',font:{{size:9}},maxTicksLimit:8}},grid:{{color:'rgba(255,255,255,.04)'}}}},
            y:{{ticks:{{color:'#5f6673',font:{{size:9}}}},grid:{{color:'rgba(255,255,255,.04)'}}}}
          }}}}
      }});
      const currRatio = eff.trend.at(-1);
      if(currRatio) document.getElementById('ek-current').textContent = Number(currRatio.ratio||currRatio.efficiency||0).toFixed(4);
    }}

    // Records
    const REC_DEFS=[
      {{k:'max_distance',l:'Mayor distancia',ico:'🏁',col:'var(--bike)',fmt:v=>Number(v).toFixed(1)+' km'}},
      {{k:'max_ascent',l:'Mayor ascenso',ico:'⛰️',col:'var(--fuerza)',fmt:v=>'+'+parseInt(v)+' m'}},
      {{k:'max_duration',l:'Sesión más larga',ico:'⏱️',col:'var(--wellness)',fmt:v=>hms(v)}},
      {{k:'max_speed',l:'Mayor velocidad',ico:'⚡',col:'var(--stats)',fmt:v=>Number(v).toFixed(1)+' km/h'}},
      {{k:'min_avg_hr',l:'FC mínima',ico:'❤️',col:'var(--red)',fmt:v=>parseInt(v)+' bpm'}},
    ];
    document.getElementById('records-grid').innerHTML = REC_DEFS.map(rd=>{{
      const v=r[rd.k]; if(!v) return '';
      return `<div class="rec-box" onclick="location.href='/charts/${{v.session_id||''}}'">
        <div class="rec-ico">${{rd.ico}}</div>
        <div class="rec-label">${{rd.l}}</div>
        <div class="rec-value" style="color:${{rd.col}}">${{rd.fmt(v.value)}}</div>
        <div class="rec-date">${{fmtDate(v.date)}}</div>
      </div>`;
    }}).join('');

    // Top rides
    const top = (perf.top_sessions||[]).slice(0,10);
    if(top.length){{
      document.getElementById('top-rides').innerHTML = '<table class="data-table"><thead><tr><th>#</th><th>Fecha</th><th>Nombre</th><th>Distancia</th><th>Duración</th><th>FC</th></tr></thead><tbody>'
        +top.map((s,i)=>`<tr onclick="location.href='/charts/${{s.session_id}}'" style="cursor:pointer">
          <td style="color:var(--muted)">${{i+1}}</td>
          <td>${{fmtDate(s.start_time)}}</td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{s.workout_name||'Ciclismo'}}</td>
          <td style="color:var(--bike);font-weight:800">${{s.distance_km||'—'}} km</td>
          <td>${{hms(s.duration_s)}}</td>
          <td>${{s.avg_hr_bpm||'—'}} bpm</td>
        </tr>`).join('')+'</tbody></table>';
    }} else {{
      document.getElementById('top-rides').innerHTML='<div style="color:var(--muted);padding:20px;text-align:center">Sin datos</div>';
    }}

  }}catch(e){{
    document.getElementById('records-grid').innerHTML='<div style="color:var(--muted)">'+e.message+'</div>';
  }}
}}

loadPerformance();
</script>
</body>
</html>"""



# ── Missing Pydantic models ───────────────────────────────────────────────────

class WellnessIn(BaseModel):
    date: str
    category: str
    fatigue: int = None
    notes: str = None
    muscle_zone: list = []
    sleep_hours: float = None
    sleep_quality: str = None
    hr_rest: int = None
    garmin_sleep_score: int = None
    pain_zone: str = None
    pain_level: int = None
    ceragem_duration_min: int = None
    ceragem_sensation_before: int = None
    ceragem_sensation_after: int = None
    stress_level: int = None
    stress_cause: str = None
    duration_min: int = None
    compex_program: str = None

class WeightIn(BaseModel):
    date: str
    weight_kg: float
    waist_cm: float = None
    body_fat_pct: float = None
    notes: str = None

class GearServiceIn(BaseModel):
    gear_id: str = None
    gear_name: str = None
    service_type: str
    description: str = None
    date: str
    km_at_service: float = None
    cost_mxn: float = None
    shop: str = None
    notes: str = None

# ── Weight tracking ───────────────────────────────────────────────────────────

def _ensure_weight_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS weight_log (
            id SERIAL PRIMARY KEY, date DATE NOT NULL,
            weight_kg NUMERIC(5,2), waist_cm NUMERIC(5,1),
            body_fat_pct NUMERIC(4,1), notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

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

def _ensure_gear_service_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS gear_service (
            id SERIAL PRIMARY KEY, gear_id TEXT, gear_name TEXT,
            service_type TEXT NOT NULL, description TEXT, date DATE NOT NULL,
            km_at_service NUMERIC(10,1), cost_mxn NUMERIC(10,2),
            shop TEXT, notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

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
                FROM sessions WHERE sport='cycling'
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
.nav-links{display:flex;gap:4px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;max-width:calc(100vw - 160px)}
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
.nav-links{display:flex;gap:4px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;max-width:calc(100vw - 160px)}
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


# ─────────────────────────────────────────────────────────────────────────────
# /gear  —  dark mode redesign
# ─────────────────────────────────────────────────────────────────────────────

GEAR_HTML = ""  # keep for compatibility

@app.get("/gear", response_class=HTMLResponse)
def gear_page():
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#08090b">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<script>if('serviceWorker' in navigator){{navigator.serviceWorker.register('/sw.js')}}</script>
<title>Bitácora — Equipo</title>
<style>{DARK_CSS}
.gear-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:14px}}
.gear-card{{background:linear-gradient(180deg,var(--surface2),var(--surface));border:1px solid var(--stroke);
  border-radius:var(--radius-md);padding:16px;border-left:3px solid var(--stroke)}}
.gear-card.red{{border-left-color:var(--red)}}
.gear-card.yellow{{border-left-color:var(--yellow)}}
.gear-card.green{{border-left-color:var(--green)}}
.gear-name{{font-size:14px;font-weight:800;letter-spacing:-.02em;margin-bottom:2px}}
.gear-type{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;margin-bottom:10px}}
.gear-bar-wrap{{height:6px;background:var(--surface3);border-radius:3px;overflow:hidden;margin-bottom:6px}}
.gear-bar{{height:100%;border-radius:3px}}
.gear-bar.red{{background:var(--red)}}
.gear-bar.yellow{{background:var(--yellow)}}
.gear-bar.green{{background:var(--green)}}
.gear-km{{display:flex;justify-content:space-between;font-size:11px;margin-top:2px}}
.gear-current{{font-weight:800;color:var(--text)}}
.gear-limit{{color:var(--muted)}}
.pct-badge{{font-size:11px;font-weight:800;padding:2px 8px;border-radius:6px}}
/* Add form */
.form-section{{background:var(--surface2);border:1px solid var(--stroke);border-radius:var(--radius-md);padding:16px;margin-bottom:12px}}
.form-section-title{{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:12px}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.form-group{{display:flex;flex-direction:column;gap:4px}}
.form-group.full{{grid-column:1/-1}}
.form-label{{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
.form-input{{background:var(--surface3);border:1px solid var(--stroke);border-radius:10px;
  padding:10px 12px;font-size:13px;color:var(--text);outline:none;width:100%}}
.form-input:focus{{border-color:var(--bike)}}
.btn-add{{background:var(--bike);color:#08090b;border:none;border-radius:12px;
  padding:12px 20px;font-size:14px;font-weight:900;cursor:pointer;width:100%;margin-top:4px}}
/* Maintenance table */
.maint-table{{width:100%;border-collapse:collapse;font-size:12px}}
.maint-table th{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  font-weight:700;text-align:left;padding:0 8px 10px 0;border-bottom:1px solid var(--stroke)}}
.maint-table td{{padding:9px 8px 9px 0;border-bottom:1px solid var(--stroke)}}
.maint-table tr:last-child td{{border-bottom:none}}
</style>
</head>
<body>
{DARK_NAV}
<div class="page">
  <div class="page-hdr">
    <div class="page-title">Equipo</div>
    <div class="page-sub">Estado de componentes · Orbea Avant aluminio 2019</div>
  </div>

  <!-- Summary KPIs -->
  <div class="kpi-grid-4" id="gear-kpis">
    <div class="kpi-box"><div class="kpi-label">Componentes</div><div class="kpi-val" id="gk-total" style="color:var(--bike)">—</div></div>
    <div class="kpi-box"><div class="kpi-label">⚠️ Alerta</div><div class="kpi-val" id="gk-alert" style="color:var(--red)">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Próximo cambio</div><div class="kpi-val" style="font-size:14px;padding-top:4px" id="gk-next">—</div></div>
    <div class="kpi-box"><div class="kpi-label">Mantenimientos</div><div class="kpi-val" id="gk-maint">—</div></div>
  </div>

  <!-- Gear status -->
  <div class="card">
    <div class="card-title">Estado de componentes</div>
    <div class="gear-grid" id="gear-grid"><div class="loading"><div class="spinner"></div>Cargando...</div></div>
  </div>

  <!-- Alerts -->
  <div id="gear-alerts-section" style="display:none">
    <div class="card" style="border-color:rgba(232,89,60,.4)">
      <div class="card-title" style="color:var(--red)">⚠️ Alertas de mantenimiento</div>
      <div id="gear-alerts"></div>
    </div>
  </div>

  <!-- Maintenance log -->
  <div class="card">
    <div class="card-hdr"><h3>Historial de mantenimiento</h3></div>
    <div id="maint-log"><div class="loading"><div class="spinner"></div></div></div>
  </div>

  <!-- Add gear form -->
  <div class="card">
    <div class="card-title">Agregar componente</div>
    <div class="form-section">
      <div class="form-grid">
        <div class="form-group"><label class="form-label">Nombre</label><input type="text" class="form-input" id="fg-name" placeholder="Cadena Shimano"></div>
        <div class="form-group"><label class="form-label">Tipo</label>
          <select class="form-input" id="fg-type">
            <option value="chain">Cadena</option>
            <option value="cassette">Cassette</option>
            <option value="tire_front">Llanta delantera</option>
            <option value="tire_rear">Llanta trasera</option>
            <option value="brake_pad">Pastillas de freno</option>
            <option value="cable">Cables</option>
            <option value="shoe">Zapatillas</option>
            <option value="other">Otro</option>
          </select>
        </div>
        <div class="form-group"><label class="form-label">Km al instalar</label><input type="number" class="form-input" id="fg-km" placeholder="0"></div>
        <div class="form-group"><label class="form-label">Km límite</label><input type="number" class="form-input" id="fg-limit" placeholder="2500"></div>
        <div class="form-group full"><label class="form-label">Notas</label><input type="text" class="form-input" id="fg-notes" placeholder="Shimano HG601, comprado en..."></div>
      </div>
      <button class="btn-add" onclick="addGear()">+ Agregar componente</button>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;

function fmtDate(s){{if(!s)return '—';const d=new Date((s+'').slice(0,10)+'T12:00:00');const M=['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();}}

function warnClass(pct){{return pct>=90?'red':pct>=70?'yellow':'green';}}
function barClass(pct){{return pct>=90?'red':pct>=70?'yellow':'green';}}
function badgeClass(pct){{return pct>=90?'badge-red':pct>=70?'badge-yellow':'badge-green';}}

async function loadGear(){{
  try{{
    const [status, alerts, maint] = await Promise.all([
      fetch(API+'/gpt/gear-status').then(r=>r.json()),
      fetch(API+'/gear/alerts').then(r=>r.json()).catch(()=>{{return {{alerts:[]}}}}),
      fetch(API+'/maintenance').then(r=>r.json()).catch(()=>({{}})),
    ]);

    const components = status.components||[];
    document.getElementById('gk-total').textContent = components.length;
    const alertItems = alerts.alerts||[];
    document.getElementById('gk-alert').textContent = alertItems.length;
    document.getElementById('gk-maint').textContent = (maint.maintenance||[]).length;

    // Most urgent
    const urgent = components.filter(c=>c.pct_used>=70).sort((a,b)=>(b.pct_used||0)-(a.pct_used||0))[0];
    document.getElementById('gk-next').textContent = urgent?.name||'Sin alertas';

    // Render gear cards
    const grid = document.getElementById('gear-grid');
    if(!components.length){{
      grid.innerHTML='<div style="color:var(--muted);padding:20px;text-align:center;grid-column:1/-1">Sin componentes registrados</div>';
    }} else {{
      grid.innerHTML = components.map(c=>{{
        const pct = Math.round(c.pct_used||0);
        const cls = warnClass(pct);
        const remaining = (c.km_limit||0) - (c.km_used||0);
        return `<div class="gear-card ${{cls}}">
          <div class="gear-name">${{c.name||'Componente'}}</div>
          <div class="gear-type">${{c.type||'—'}}</div>
          <div class="gear-bar-wrap"><div class="gear-bar ${{cls}}" style="width:${{Math.min(pct,100)}}%"></div></div>
          <div class="gear-km">
            <span class="gear-current">${{c.km_used||0}} km</span>
            <span class="pct-badge ${{badgeClass(pct)}}">${{pct}}%</span>
          </div>
          <div class="gear-limit">Límite: ${{c.km_limit||'—'}} km · Restantes: ~${{remaining>0?remaining:'0'}} km</div>
        </div>`;
      }}).join('');
    }}

    // Alerts
    if(alertItems.length){{
      document.getElementById('gear-alerts-section').style.display='block';
      document.getElementById('gear-alerts').innerHTML = alertItems.map(a=>
        `<div class="row-item">
          <div class="row-ico" style="background:rgba(232,89,60,.15)">⚠️</div>
          <div class="row-main"><div class="row-title">${{a.name||a.component}}</div><div class="row-sub">${{a.message||'Requiere atención'}}</div></div>
          <div class="row-val">${{a.pct_used||''}}%</div>
        </div>`
      ).join('');
    }}

    // Maintenance log
    const maintList = maint.maintenance||[];
    document.getElementById('gk-maint').textContent = maintList.length;
    if(maintList.length){{
      document.getElementById('maint-log').innerHTML = '<table class="maint-table"><thead><tr><th>Fecha</th><th>Componente</th><th>Tipo</th><th>Km</th><th>Costo</th></tr></thead><tbody>'
        +maintList.slice(0,20).map(m=>`<tr>
          <td>${{fmtDate(m.date)}}</td>
          <td>${{m.gear_id||'—'}}</td>
          <td>${{m.type||'—'}}</td>
          <td>${{m.km_at_service||'—'}}</td>
          <td>${{m.cost_mxn?'$'+m.cost_mxn:'—'}}</td>
        </tr>`).join('')+'</tbody></table>';
    }} else {{
      document.getElementById('maint-log').innerHTML='<div style="color:var(--muted);padding:20px;text-align:center">Sin registros de mantenimiento</div>';
    }}

  }}catch(e){{
    document.getElementById('gear-grid').innerHTML='<div style="color:var(--muted);padding:20px">'+e.message+'</div>';
  }}
}}

async function addGear(){{
  const body={{
    name:document.getElementById('fg-name').value,
    type:document.getElementById('fg-type').value,
    bike_id:'orbea-avant-2019',
    km_at_install:parseInt(document.getElementById('fg-km').value)||0,
    km_limit:parseInt(document.getElementById('fg-limit').value)||null,
    notes:document.getElementById('fg-notes').value||null,
    installed_date:new Date().toISOString().slice(0,10),
    gear_id:'gear-'+Date.now(),
  }};
  try{{
    await fetch(API+'/gear',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
    loadGear();
  }}catch(e){{alert('Error: '+e.message);}}
}}

loadGear();
</script>
</body>
</html>"""


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
            cur.execute("SELECT COUNT(*) FROM sessions")
            sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM session_records")
            records = cur.fetchone()[0]
            cur.execute("SELECT MAX(start_time), MAX(created_at) FROM sessions")
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
                        FROM sessions WHERE sport=%s AND session_id != %s
                        AND avg_hr_bpm > 0
                    """, (sport, session_id))
                else:
                    cur.execute(f"""
                        SELECT MAX({col}) FROM sessions
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
                FROM sessions
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
                FROM sessions
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
                  "achievements", "athlete_snapshots"]
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
              "athlete_profile", "athlete_tests", "achievements", "athlete_snapshots"]
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
.nav-links{display:flex;gap:4px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;max-width:calc(100vw - 160px)}
.nav-links::-webkit-scrollbar{display:none}
.nav-link{padding:6px 12px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s;white-space:nowrap;flex-shrink:0}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
@media(max-width:600px){
  nav{padding:0 12px;height:52px}
  .nav-name{font-size:14px}
  .nav-sub{display:none}
  .nav-link{padding:5px 10px;font-size:11px}
  .page{padding:16px 14px}
}
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
.nav-links{display:flex;gap:4px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;max-width:calc(100vw - 160px)}
.nav-links::-webkit-scrollbar{display:none}
.nav-link{padding:6px 12px;border-radius:8px;font-size:12px;font-weight:500;color:var(--muted);text-decoration:none;transition:all .15s;white-space:nowrap;flex-shrink:0}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--text);color:white}
@media(max-width:600px){
  nav{padding:0 12px;height:52px}
  .nav-name{font-size:14px}
  .nav-sub{display:none}
  .nav-link{padding:5px 10px;font-size:11px}
  .page{padding:16px 14px}
}

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
                FROM sessions
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

def _ensure_nutrition_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nutrition_log (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                session_id TEXT,
                moment TEXT,           -- pre, durante, post
                gel_type TEXT,         -- agave_casero, miel_casero, comercial
                gel_count INTEGER,
                agua_ml INTEGER,
                carbos_g NUMERIC(6,1),
                notas TEXT,
                gi_response TEXT,
                energy_response TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()

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

MARS_PROFILE_DEFAULT = '{"zonas_ciclismo":{"lt_bpm":168,"max_hr":196,"z1":[0,109],"z2":[109,134],"z3":[134,150],"z4":[150,160],"z5":[160,168]},"zonas_running":{"lt_bpm":173,"max_hr":194,"z1":[0,112],"z2":[112,138],"z3":[138,154],"z4":[154,164],"z5":[164,173]},"athlete":{"nombre":"Miguel Angel Mars","edad":47,"peso_actual_kg":89.1,"peso_objetivo_kg":80.0,"elevacion_m":2300},"bici":{"nombre":"Rarotonga","marca":"Orbea Avant Aluminio 2019","km":716.6,"primer_uso":"2026-04-27","llantas":"Vittoria Corsa N.EXT 700C x26"},"objetivos":[{"o":"Mejorar motor aerobico Z2","p":1},{"o":"Bajar peso a 80 kg","p":2},{"o":"Cadencia 100 rpm","p":3},{"o":"Eficiencia vel/FC 0.155+","p":4}],"plan_garmin":{"nombre":"Garmin Coach Time Trial","fase":"Base aerobica","desc":"Construccion motor aerobico Z2 con salidas largas y cadencia"},"rutas":[{"nombre":"Atizapan base","km":21,"desc":"Ruta base Z2 local"},{"nombre":"Salida larga","km":45,"desc":"Ruta larga entrenamiento"}],"nutricion":{"gel":"60% apple juice Tree Top + 40% agave Kirkland + pizca sal","carbos_g":40,"timing":"Cada 45-60 min durante sesion","agua_ml_h":500},"compex":{"fuerza":["Strength","Explosive Strength"],"recovery":["Active Recovery","Massage"],"dolor":["TENS"]},"cadencia_obj":100,"eff_base":0.1483,"eff_obj":0.155}'

def _ensure_profile_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS athlete_profile_full (
            id SERIAL PRIMARY KEY, profile_key TEXT UNIQUE NOT NULL DEFAULT 'mars',
            data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _get_profile(conn):
    import json as _j
    _ensure_profile_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM athlete_profile_full WHERE profile_key='mars'")
        row = cur.fetchone()
    return row[0] if row and isinstance(row[0], dict) else (_j.loads(row[0]) if row else _j.loads(MARS_PROFILE_DEFAULT))

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
                    FROM sessions WHERE sport='cycling' AND avg_hr_bpm>0 AND avg_speed_kmh>0
                    AND start_time::timestamp>=NOW()-'4 weeks'::interval""")
                row = cur.fetchone()
            if row and row[0]: p["eff_actual"] = float(row[0])
        except: pass
        z = p.get("zonas_ciclismo",{})
        z2 = z.get("z2",[109,134])
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
                FROM sessions WHERE session_id=%s""",(session_id,))
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
                FROM sessions WHERE sport='cycling'
                AND start_time::timestamp>=NOW()-'90 days'::interval AND session_id!=%s""",(session_id,))
            hrow = cur.fetchone()
        hist = {"avg_hr":float(hrow[0]) if hrow[0] else None,"avg_spd":float(hrow[1]) if hrow[1] else None,
                "avg_cad":float(hrow[2]) if hrow[2] else None,"avg_dist":float(hrow[3]) if hrow[3] else None,"n":hrow[4]}
        z = p.get("zonas_ciclismo",{})
        hr = s.get("avg_hr_bpm") or 0
        spd = s.get("avg_speed_kmh") or 0
        cadval = s.get("avg_cadence") or 0
        z2lo,z2hi = z.get("z2",[109,134])
        z3hi = z.get("z3",[134,150])[1]
        if hr < z2lo: zone_eval="Z1 muy facil — sube intensidad"
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
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}html,body{height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",sans-serif;overflow:hidden}button,input,select,textarea{font-family:inherit}.app{height:100dvh;display:flex;flex-direction:column;background:radial-gradient(900px 430px at 50% -180px,color-mix(in srgb,var(--theme) 18%,transparent),transparent 62%),linear-gradient(180deg,#0c0e12,#07080a);transition:background .28s ease}.top{height:62px;display:flex;align-items:center;justify-content:space-between;padding:calc(env(safe-area-inset-top) + 10px) 16px 8px}.brand{display:flex;gap:10px;align-items:center}.logo{width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,var(--theme),color-mix(in srgb,var(--theme) 52%,#000));box-shadow:var(--glow);color:#08090b;font-size:19px;font-weight:950;display:flex;align-items:center;justify-content:center}.brand small{display:block;font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.03em}.brand strong{display:block;font-size:16px;letter-spacing:-.03em}.icon{width:38px;height:38px;border-radius:14px;border:1px solid var(--line);background:rgba(255,255,255,.045);color:var(--text);font-size:18px}.content{flex:1;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding:10px 14px 94px}.screen{display:none;animation:fade .22s ease}.screen.active{display:block}@keyframes fade{from{opacity:.35;transform:translateY(8px)}to{opacity:1;transform:none}}
.hero{border-radius:28px;padding:20px 20px 18px;margin-bottom:14px;position:relative;overflow:hidden;background:linear-gradient(135deg,color-mix(in srgb,var(--theme) 24%,#15171c),#14161b 55%,#0d0f13);border:1px solid color-mix(in srgb,var(--theme) 22%,var(--line));box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;right:-42px;top:-45px;width:142px;height:142px;border-radius:50%;background:color-mix(in srgb,var(--theme) 18%,transparent);filter:blur(8px)}.kicker{font-size:10px;font-weight:900;letter-spacing:.12em;text-transform:uppercase;color:color-mix(in srgb,var(--theme) 88%,#fff);margin-bottom:8px}.hero h1{font-size:30px;line-height:.98;letter-spacing:-.06em;margin-bottom:8px}.hero p{font-size:13px;line-height:1.45;color:#c8cbd2;max-width:330px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:22px;padding:16px;margin-bottom:12px}.mini{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:17px;padding:14px;min-height:88px}.label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:900;margin-bottom:7px}.value{font-size:28px;font-weight:950;letter-spacing:-.05em;color:var(--theme);line-height:1}.unit{font-size:11px;color:var(--muted);margin-top:3px}.head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.head h3{font-size:14px;letter-spacing:-.02em}.head span{font-size:11px;color:var(--muted)}.row{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:none}.r-ico{width:42px;height:42px;border-radius:15px;background:color-mix(in srgb,var(--theme) 14%,#111);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}.r-main{flex:1;min-width:0}.r-title{font-size:14px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.r-sub{font-size:11px;color:var(--muted);margin-top:3px}.r-val{text-align:right;font-size:14px;font-weight:950;color:var(--theme)}.pillbar{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;padding-bottom:8px;margin-bottom:10px}.pillbar::-webkit-scrollbar{display:none}.pill{padding:8px 14px;border-radius:999px;background:rgba(255,255,255,.045);border:1px solid var(--line);font-size:12px;font-weight:900;color:var(--muted);white-space:nowrap}.pill.on{background:var(--theme);border-color:var(--theme);color:#08090b}.upload{border:1.6px dashed color-mix(in srgb,var(--theme) 44%,var(--line));background:color-mix(in srgb,var(--theme) 7%,var(--card));border-radius:24px;padding:25px 18px;text-align:center;margin-bottom:12px;display:block}.upload input{display:none}.upload .big{font-size:40px;margin-bottom:8px}.upload h3{font-size:17px;margin-bottom:4px}.upload p{font-size:12px;color:var(--muted)}input,select,textarea{width:100%;background:#242832;border:1px solid var(--line);border-radius:14px;padding:12px 13px;color:var(--text);font-size:14px;outline:none}input:focus,select:focus,textarea:focus{border-color:var(--theme);box-shadow:0 0 0 3px color-mix(in srgb,var(--theme) 17%,transparent)}.field{margin-bottom:10px}.field label{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:900;margin:0 0 5px 2px}.btn{width:100%;border:none;border-radius:16px;padding:15px;background:var(--theme);color:#08090b;font-size:15px;font-weight:950}.btn2{background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--text)}.bnav{position:fixed;left:10px;right:10px;bottom:calc(env(safe-area-inset-bottom) + 10px);height:66px;background:rgba(17,19,24,.88);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,.14);border-radius:24px;display:flex;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;z-index:80;box-shadow:0 16px 40px rgba(0,0,0,.45)}.nav{flex:0 0 auto;min-width:52px;background:transparent;border:none;color:var(--muted);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:9px;font-weight:900;text-transform:uppercase}.nav svg{opacity:.5;transition:transform .2s,filter .2s,opacity .2s}.nav.active{color:var(--theme)}.nav.active svg{opacity:1;transform:translateY(-2px);filter:drop-shadow(0 0 5px var(--theme))}.tabs{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:0 -2px 12px;padding:0 2px}.tabs::-webkit-scrollbar{display:none}.tab{border:none;background:rgba(255,255,255,.045);border:1px solid var(--line);color:var(--muted);border-radius:999px;padding:9px 14px;font-size:12px;font-weight:900;white-space:nowrap}.tab.on{background:var(--theme);color:#08090b;border-color:var(--theme)}.calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}.day{aspect-ratio:1;border-radius:10px;background:#1d222c;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--muted);font-weight:800}.day.hot{background:color-mix(in srgb,var(--theme) 38%,#1d222c);color:#08090b;border-color:var(--theme)}.gearbar{height:7px;border-radius:4px;background:#303642;overflow:hidden;margin-top:8px}.gearfill{height:100%;background:var(--theme);border-radius:4px}.bodymap{height:240px;border-radius:24px;background:radial-gradient(circle at 50% 35%,color-mix(in srgb,var(--theme) 20%,transparent),transparent 35%),linear-gradient(180deg,#11141a,#0c0e12);border:1px solid var(--line);position:relative;margin-bottom:12px;overflow:hidden}.human{position:absolute;left:50%;top:22px;transform:translateX(-50%);width:100px;height:205px}.human div{position:absolute;background:#343943}.human .h{left:36px;top:0;width:28px;height:28px;border-radius:50%}.human .t{left:22px;top:34px;width:56px;height:72px;border-radius:28px 28px 18px 18px;background:linear-gradient(180deg,var(--theme),color-mix(in srgb,var(--theme) 45%,#111))}.human .a{top:46px;width:21px;height:88px;border-radius:15px}.human .al{left:0;transform:rotate(12deg)}.human .ar{right:0;transform:rotate(-12deg)}.human .l{top:112px;width:28px;height:92px;border-radius:18px;background:linear-gradient(180deg,var(--theme),#343943)}.human .ll{left:20px}.human .lr{right:20px}.mq{position:absolute;inset:14px;display:flex;flex-direction:column;justify-content:space-between;font-size:12px;color:#d8dde7}.mq div{display:flex;justify-content:space-between}.mq strong{font-size:19px;color:var(--theme)}.ring{width:94px;height:94px;border-radius:50%;background:conic-gradient(var(--theme) var(--p,75%),rgba(255,255,255,.08) 0);display:flex;align-items:center;justify-content:center;position:relative;margin:auto;box-shadow:var(--glow)}.ring:before{content:"";position:absolute;width:68px;height:68px;border-radius:50%;background:var(--card)}.ring span{position:relative;font-size:25px;font-weight:950;color:var(--theme)}.toast{position:fixed;top:calc(env(safe-area-inset-top) + 14px);left:50%;transform:translateX(-50%);background:var(--theme);color:#08090b;padding:10px 18px;border-radius:999px;z-index:999;font-size:13px;font-weight:950;display:none;white-space:nowrap}.loading{padding:24px;color:var(--muted);font-size:13px;text-align:center}.spin{display:inline-block;width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--theme);border-radius:50%;animation:spin .75s linear infinite;vertical-align:middle;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}
@media(min-width:900px){
  .app{max-width:100%}
  .top{max-width:1100px;margin:0 auto;width:100%;padding-left:28px;padding-right:28px;box-sizing:border-box}
  .content{max-width:1100px;margin:0 auto;padding-left:28px;padding-right:28px;padding-bottom:72px;display:grid;max-width:760px;box-sizing:border-box}
  .screen{padding:0 0 0 20px}
  .bnav{left:0;right:0;bottom:0;border-radius:0;border:none;border-top:1px solid rgba(255,255,255,.1);height:54px;background:rgba(10,11,14,.97)}
  .nav .lbl{font-size:8px}
}
@media(min-width:1200px){
  .top,.content{max-width:1280px}
}}
</style>
</head>
<body>
<div class="app">
  <div class="top"><div class="brand"><div class="logo">M</div><div><small id="kicker">Bitácora Mars</small><strong id="title">Home</strong></div></div><button class="icon" onclick="refresh()">↻</button></div>
  <main class="content">
    <section class="screen active" id="s-home"><div class="hero"><div class="kicker">Home / blanco neutro</div><h1 id="greeting-h1">Buenos días,<br>Mars.</h1><p>Resumen vivo de carga, recuperación, última sesión y accesos rápidos.</p></div><div id="home-data" class="loading"><span class="spin"></span>Cargando...</div></section>
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
  </main>
  <nav class="bnav"><button class="nav active" data-s="home" onclick="go('home')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg><span class="lbl">Inicio</span></button><button class="nav" data-s="activities" onclick="go('activities')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><path d="M15 6a1 1 0 1 0 0-2 1 1 0 0 0 0 2"/><path d="M10 7l-2.5 5H5.5m4.5-5l5 3 3.5 7.5M10 7l2-5"/></svg><span class="lbl">Bici</span></button><button class="nav" data-s="dashboard" onclick="go('dashboard')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg><span class="lbl">Stats</span></button><button class="nav" data-s="gear" onclick="go('gear')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><span class="lbl">Gear</span></button><button class="nav" data-s="calendar" onclick="go('calendar')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg><span class="lbl">Cal</span></button><button class="nav" data-s="fuerza" onclick="go('fuerza')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 6.5h11M6.5 17.5h11M5 12h14"/><circle cx="3.5" cy="12" r="1.5"/><circle cx="20.5" cy="12" r="1.5"/><circle cx="3.5" cy="6.5" r="1.5"/><circle cx="20.5" cy="6.5" r="1.5"/><circle cx="3.5" cy="17.5" r="1.5"/><circle cx="20.5" cy="17.5" r="1.5"/></svg><span class="lbl">Fuerza</span></button><button class="nav" data-s="wellness" onclick="go('wellness')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg><span class="lbl">Wellness</span></button><button class="nav" data-s="performance" onclick="go('performance')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg><span class="lbl">Récords</span></button><button class="nav" data-s="eficiencia" onclick="go('eficiencia')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg><span class="lbl">Carga</span></button><button class="nav" data-s="correlaciones" onclick="go('correlaciones')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2 20 6 12 10 16 14 10 18 14 22 4"/></svg><span class="lbl">Correl.</span></button><button class="nav" data-s="nutricion" onclick="go('nutricion')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 0 1 10 10c0 5.52-4.48 10-10 10S2 17.52 2 12"/><path d="M12 6v6l4 2"/></svg><span class="lbl">Nutri.</span></button><button class="nav" data-s="perfil" onclick="go('perfil')"><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg><span class="lbl">Perfil</span></button></nav>
</div><div class="toast" id="toast"></div>
<script>
const API=window.location.origin;
const THEME={home:'#ffffff',perfil:'#3dd68c',dashboard:'#e8593c',activities:'#e8593c',gear:'#f59e0b',calendar:'#22d3ee',performance:'#a78bfa',fuerza:'#c8f135',wellness:'#4a9eff',eficiencia:'#3dd68c',correlaciones:'#a78bfa',nutricion:'#f59e0b'};
const TITLE={home:['Bitácora Mars','Home'],perfil:['Perfil Mars','atleta'],dashboard:['Dashboard','stats'],activities:['Bici','sesiones'],gear:['Gear','mantenimiento'],calendar:['Calendario','heatmap'],performance:['Récords','personales'],fuerza:['Fuerza','Compex'],wellness:['Wellness','recuperación'],eficiencia:['Eficiencia','aeróbica'],correlaciones:['Correlaciones','FC · Peso'],nutricion:['Nutrición','geles']};
let current='home';
function $(id){return document.getElementById(id)}
function setTheme(s){document.documentElement.style.setProperty('--theme',THEME[s]||'#fff');$('kicker').textContent=TITLE[s][0];$('title').textContent=TITLE[s][1]}
function go(s){current=s;setTheme(s);document.querySelectorAll('.screen').forEach(x=>x.classList.toggle('active',x.id==='s-'+s));document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.s===s));load(s)}
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
async function load(s){if(s==='home')return loadHome();if(s==='dashboard')return loadDash();if(s==='activities')return loadActs();if(s==='gear')return loadGear();if(s==='calendar')return loadCal();if(s==='performance')return loadPerf();if(s==='fuerza')return loadFuerza();if(s==='wellness')return loadWell();if(s==='eficiencia')return loadEficiencia();if(s==='correlaciones')return loadCorrelaciones();if(s==='nutricion')return loadNutricion();if(s==='perfil')return loadPerfil();}
async function loadHome(){
  try{
    const [d,w,mp] = await Promise.all([
      fetch(API+'/gpt/dashboard').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/wellness-summary?weeks=1').then(r=>r.json()).catch(()=>({})),
      fetch(API+'/gpt/mars-context').then(r=>r.json()).catch(()=>({}))
    ]);
    const a=d.athlete||{}, s=d.semana_actual||{}, z=d.z2_check||{};
    const pains=(w.molestias_activas||[]).length;
    $('home-data').innerHTML=
      '<div class="grid2">'+
        metric('Km semana',Number(s.km||0).toFixed(0),'km')+
        metric('Horas',Number(s.horas||0).toFixed(1),'h')+
        metric('Sesiones',s.sesiones||0,'semana')+
        metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(0)+'%','4 semanas')+
      '</div>'+
      (mp&&mp.context_msg?'<div class="card" style="font-size:11px;color:var(--muted);line-height:1.6">'+mp.context_msg+'</div>':'')+
      '<div class="card"><div class="head"><h3>Estado del atleta</h3><span>'+(a.fitness||'—')+'</span></div>'+
        row('·','Fitness',d.recommendation||'Sin recomendacion',a.mars_index?Number(a.mars_index).toFixed(1):'—')+
        row('·','Fatiga',a.fatiga||'—','')+
        row('·','Molestias activas',pains?pains+' activa(s)':'Sin alertas','')+
      '</div>';
  }catch(e){
    $('home-data').innerHTML='<div class="card" style="color:var(--muted)">Error cargando: '+e.message+'</div>';
  }
}

async function loadDash(){try{const d=await fetch(API+'/gpt/dashboard').then(r=>r.json());const a=d.athlete||{},s=d.semana_actual||{},c=d.carga||{},z=d.z2_check||{};$('dash-data').innerHTML=`<div class="grid2">${metric('Fitness',a.fitness||'—','Mars Index '+(a.mars_index||'—'))}${metric('Fatiga',a.fatiga||'—','TSB '+(c.tsb||0))}${metric('Carga',c.estado||'—','actual')}${metric('Z2',Number(z.pct_z2_4_semanas||0).toFixed(1)+'%','4 semanas')}</div><div class="card"><div class="head"><h3>Semana actual</h3><span>${s.sesiones||0} sesiones</span></div>${row('','Distancia semanal','Acumulado Garmin',Number(s.km||0).toFixed(1)+' km')}${row('·','Tiempo semanal','Horas de carga',Number(s.horas||0).toFixed(1)+' h')}${row('🔥','Calorías','Estimado semanal',s.calorias||'—')}</div>`}catch(e){$('dash-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
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
          '<button onclick="deleteSession(\x27'+s.session_id+'\x27)" style="background:rgba(232,89,60,.1);border:none;border-radius:8px;padding:4px 8px;color:#e8593c;font-size:10px;font-weight:800;cursor:pointer">borrar</button>'+
        '</div>'+
      '</div>';
    }).join('');
  }catch(e){$('act-list').innerHTML='<div style="color:var(--muted)">'+e.message+'</div>'}
}

async function uploadFit(file){if(!file)return;$('upload-result').innerHTML='<div class="card">Procesando '+file.name+'...</div>';const fd=new FormData();fd.append('file',file);try{const d=await fetch(API+'/analyze-fit',{method:'POST',body:fd}).then(r=>r.json());const s=d.session||{};$('upload-result').innerHTML='';$('upload-result').innerHTML=`<div class="card"><div class="head"><h3>${d.duplicate?'Sesión existente':'Sesión guardada'}</h3><span>${d.session_id}</span></div><div class="grid2">${metric('Distancia',s.distance_km||'—','km')}${metric('FC prom.',s.avg_hr_bpm||'—','bpm')}${metric('Duración',s.duration_hms||'—','')}${metric('Ascenso',s.ascent_m||'—','m')}</div><button class="btn btn2" onclick="navigator.clipboard.writeText('${d.session_id}');toast('ID copiado')">Copiar session_id</button></div>`;loadActs()}catch(e){$('upload-result').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
async function loadGear(){loadGearHistory();try{const d=await fetch(API+'/gpt/gear-status').then(r=>r.json()).catch(()=>null);const a=await fetch(API+'/gpt/gear-alerts').then(r=>r.json()).catch(()=>({alerts:[]}));const items=(d&&d.components)||d?.gear||[];let html=`<div class="card"><div class="head"><h3>Alertas</h3><span>${(a.alerts||[]).length}</span></div>${(a.alerts||[]).length?(a.alerts||[]).map(x=>row('',x.name||x.type||'Alerta',x.message||x.detail||'',x.km_left?x.km_left+' km':'' )).join(''):'Sin alertas de equipo'}</div>`;html+=`<div class="card"><div class="head"><h3>Componentes</h3><span>${items.length||0}</span></div>${items.length?items.map(g=>{let pct=Math.min(100,Math.round(((g.km_current||g.current_km||0)/(g.km_limit||g.limit_km||4500))*100));return `<div class="row"><div class="r-ico">—</div><div class="r-main"><div class="r-title">${g.name||g.type||'Componente'}</div><div class="r-sub">${g.km_current||g.current_km||0} / ${g.km_limit||g.limit_km||'—'} km<div class="gearbar"><div class="gearfill" style="width:${pct}%"></div></div></div></div><div class="r-val">${pct}%</div></div>`}).join(''):'Sin componentes registrados'}</div>`;$('gear-data').innerHTML=html}catch(e){$('gear-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
async function loadCal(){try{const d=await fetch(API+'/gpt/calendar-heatmap?months=3').then(r=>r.json());const days=d.days||d.calendar||[];const recent=days.slice(-42);$('cal-data').innerHTML=`<div class="card"><div class="head"><h3>Últimas 6 semanas</h3><span>${recent.filter(x=>x.count||x.sessions).length} días activos</span></div><div class="calgrid">${recent.map(x=>`<div class="day ${(x.count||x.sessions||0)>0?'hot':''}">${(x.date||'').slice(8,10)}</div>`).join('')}</div></div><div class="grid2">${metric('Días activos',recent.filter(x=>x.count||x.sessions).length,'6 semanas')}${metric('Sesiones',recent.reduce((a,x)=>a+(x.count||x.sessions||0),0),'total')}</div>`}catch(e){$('cal-data').innerHTML='<div class="card">Error: '+e.message+'</div>'}}
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
  }catch(e){$('perf-data').innerHTML='<div class="card" style="color:var(--muted)">'+e.message+'</div>'}
}

async function loadFuerza(){
  try{
    const [d,hist]=await Promise.all([
      fetch(API+'/gpt/fuerza-summary?weeks=8').then(r=>r.json()),
      fetch(API+'/fuerza?limit=10').then(r=>r.json()).catch(()=>({}))
    ]);
    const items=hist.sesiones||hist.records||hist.data||[];
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
      fetch(API+'/gpt/wellness-summary?weeks=4').then(r=>r.json()),
      fetch(API+'/wellness?limit=10').then(r=>r.json()).catch(()=>({}))
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


async function deleteSession(id){
  if(!confirm('Borrar esta sesion?'))return;
  const d=await fetch(API+'/sessions/'+id,{method:'DELETE'}).then(r=>r.json()).catch(()=>({}));
  if(d.ok){toast('Sesion borrada');loadActs();}
  else toast('Error al borrar');
}
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

go('home');
</script>
</body>
</html>"""


def _full_app_response():
    return HTMLResponse(APP_FULL_HTML)

# Reemplaza las páginas viejas sin tocar los endpoints API/GPT.
_override_paths = {"/home", "/dashboard", "/activities", "/gear", "/calendar", "/performance", "/progress", "/fuerza", "/wellness"}
# Only remove GET routes — POST/PUT/DELETE API routes stay intact
app.router.routes = [
    r for r in app.router.routes
    if not (getattr(r, "path", None) in _override_paths and getattr(r, "methods", None) == {"GET"})
]
for _path in sorted(_override_paths):
    app.add_api_route(_path, _full_app_response, methods=["GET"], response_class=HTMLResponse)
