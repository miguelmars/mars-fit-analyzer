"""
db.py — Database connection for Bitácora Mars
Imported by main.py
"""
import os
import logging

logger = logging.getLogger(__name__)

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
