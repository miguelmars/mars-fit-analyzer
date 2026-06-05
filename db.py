"""
db.py — Database connection and table helpers for Bitácora Mars
"""
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def _load_local_env():
    """Load local .env values for desktop maintenance scripts."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as e:
        logger.warning(f"Could not load local .env: {e}")

_load_local_env()

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

def _ensure_gear_service_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS gear_service (
            id SERIAL PRIMARY KEY, gear_id TEXT, gear_name TEXT,
            service_type TEXT NOT NULL, description TEXT, date DATE NOT NULL,
            km_at_service NUMERIC(10,1), cost_mxn NUMERIC(10,2),
            shop TEXT, notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _ensure_gear_activity_links_table(conn):
    """Future model: link clean activities to gear/components for real component km."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gear_activity_links (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                source_activity_id TEXT,
                gear_id TEXT NOT NULL,
                gear_source TEXT DEFAULT 'manual',
                role TEXT,
                confidence TEXT DEFAULT 'manual',
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(session_id, source_activity_id, gear_id, role)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gear_activity_links_session ON gear_activity_links(session_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gear_activity_links_source_activity ON gear_activity_links(source_activity_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gear_activity_links_gear ON gear_activity_links(gear_id)")
    conn.commit()

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

def _ensure_profile_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS athlete_profile_full (
            id SERIAL PRIMARY KEY, profile_key TEXT UNIQUE NOT NULL DEFAULT 'mars',
            data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _ensure_accidents_table(conn):
    with conn.cursor() as cur:
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
    conn.commit()

def _ensure_garmin_staging_tables(conn):
    """Create non-destructive staging tables for Garmin account exports."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS garmin_export_activities (
                source_activity_id TEXT PRIMARY KEY,
                source             TEXT NOT NULL DEFAULT 'garmin_export',
                name               TEXT,
                sport              TEXT,
                sport_type         TEXT,
                start_time_utc     TIMESTAMPTZ,
                start_time_local   TIMESTAMPTZ,
                duration_s         INT,
                moving_duration_s  INT,
                elapsed_duration_s INT,
                distance_km        NUMERIC(9,3),
                ascent_m           INT,
                descent_m          INT,
                min_elevation_m    INT,
                max_elevation_m    INT,
                avg_speed_kmh      NUMERIC(6,2),
                max_speed_kmh      NUMERIC(6,2),
                computed_avg_speed_kmh NUMERIC(6,2),
                avg_hr_bpm         NUMERIC(5,1),
                max_hr_bpm         INT,
                avg_cadence        NUMERIC(5,1),
                max_cadence        INT,
                calories           INT,
                temperature_min_c  NUMERIC(4,1),
                temperature_max_c  NUMERIC(4,1),
                aerobic_training_effect NUMERIC(3,1),
                anaerobic_training_effect NUMERIC(3,1),
                start_lat          NUMERIC(10,7),
                start_lon          NUMERIC(10,7),
                end_lat            NUMERIC(10,7),
                end_lon            NUMERIC(10,7),
                device_id          TEXT,
                lap_count          INT,
                favorite           BOOLEAN,
                pr                 BOOLEAN,
                power_available    BOOLEAN DEFAULT FALSE,
                avg_power_w        INT,
                max_power_w        INT,
                normalized_power_w INT,
                efficiency_speed_hr NUMERIC(8,5),
                is_probable_real_activity BOOLEAN DEFAULT TRUE,
                raw_json           JSONB,
                imported_at        TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS garmin_export_gear (
                source_gear_id TEXT PRIMARY KEY,
                source         TEXT NOT NULL DEFAULT 'garmin_export',
                uuid           TEXT,
                name           TEXT,
                type           TEXT,
                status         TEXT,
                model          TEXT,
                date_begin     DATE,
                max_distance_km NUMERIC(10,1),
                raw_json       JSONB,
                imported_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS garmin_export_sleep (
                id               SERIAL PRIMARY KEY,
                source           TEXT NOT NULL DEFAULT 'garmin_export',
                calendar_date    DATE,
                sleep_start_gmt  TIMESTAMPTZ,
                sleep_end_gmt    TIMESTAMPTZ,
                sleep_start_local TIMESTAMPTZ,
                sleep_end_local  TIMESTAMPTZ,
                duration_s       INT,
                deep_sleep_s     INT,
                light_sleep_s    INT,
                rem_sleep_s      INT,
                awake_s          INT,
                sleep_score      INT,
                confidence       TEXT DEFAULT 'low',
                notes            TEXT,
                raw_json         JSONB,
                imported_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(calendar_date, sleep_start_gmt, sleep_end_gmt)
            )
        """)
        cur.execute("ALTER TABLE garmin_export_sleep ADD COLUMN IF NOT EXISTS confidence TEXT DEFAULT 'low'")
        cur.execute("ALTER TABLE garmin_export_sleep ADD COLUMN IF NOT EXISTS notes TEXT")
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_garmin_export_activities_start ON garmin_export_activities(start_time_local)",
            "CREATE INDEX IF NOT EXISTS idx_garmin_export_activities_sport ON garmin_export_activities(sport)",
            "CREATE INDEX IF NOT EXISTS idx_garmin_export_sleep_date ON garmin_export_sleep(calendar_date)",
            "CREATE INDEX IF NOT EXISTS idx_garmin_export_gear_status ON garmin_export_gear(status)",
        ]:
            cur.execute(idx_sql)
    conn.commit()

def _ensure_clean_sessions_table(conn):
    """Create the non-destructive clean activity layer used for Phase 1 cleanup."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clean_sessions (
                clean_session_id TEXT PRIMARY KEY,
                source           TEXT NOT NULL,
                source_activity_id TEXT,
                original_session_id TEXT,
                name             TEXT,
                sport            TEXT,
                sport_type       TEXT,
                start_time       TIMESTAMPTZ,
                start_date       DATE,
                duration_s       INT,
                moving_duration_s INT,
                elapsed_duration_s INT,
                distance_km      NUMERIC(9,3),
                ascent_m         INT,
                descent_m        INT,
                avg_speed_kmh    NUMERIC(6,2),
                max_speed_kmh    NUMERIC(6,2),
                avg_hr_bpm       NUMERIC(5,1),
                max_hr_bpm       INT,
                avg_cadence      NUMERIC(5,1),
                max_cadence      INT,
                calories         INT,
                start_lat        NUMERIC(10,7),
                start_lon        NUMERIC(10,7),
                end_lat          NUMERIC(10,7),
                end_lon          NUMERIC(10,7),
                route_id         TEXT,
                power_available  BOOLEAN DEFAULT FALSE,
                efficiency_speed_hr NUMERIC(8,5),
                quality          TEXT DEFAULT 'clean',
                quality_notes    TEXT,
                raw_json         JSONB,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_clean_sessions_start_time ON clean_sessions(start_time)",
            "CREATE INDEX IF NOT EXISTS idx_clean_sessions_sport ON clean_sessions(sport)",
            "CREATE INDEX IF NOT EXISTS idx_clean_sessions_route_id ON clean_sessions(route_id)",
            "CREATE INDEX IF NOT EXISTS idx_clean_sessions_source ON clean_sessions(source)",
            "CREATE INDEX IF NOT EXISTS idx_clean_sessions_start_date ON clean_sessions(start_date)",
        ]:
            cur.execute(idx_sql)
    conn.commit()

def _ensure_clean_sessions_compat_view(conn):
    """Expose clean_sessions with the legacy sessions column names for read endpoints."""
    _ensure_clean_sessions_table(conn)
    with conn.cursor() as cur:
        cur.execute("DROP VIEW IF EXISTS sessions_clean_compat")
        cur.execute("""
            CREATE VIEW sessions_clean_compat AS
            SELECT
                clean_session_id AS session_id,
                NULL::text AS filename,
                created_at AS uploaded_at,
                start_time::text AS start_time,
                sport,
                distance_km::double precision AS distance_km,
                duration_s,
                ascent_m,
                avg_hr_bpm::double precision AS avg_hr_bpm,
                avg_speed_kmh::double precision AS avg_speed_kmh,
                avg_cadence::double precision AS avg_cadence,
                COALESCE(name, '') AS workout_name,
                start_lat::double precision AS start_lat,
                start_lon::double precision AS start_lon,
                end_lat::double precision AS end_lat,
                end_lon::double precision AS end_lon,
                route_id,
                raw_json::text AS result_json,
                NULL::text AS file_hash,
                calories,
                source,
                quality
            FROM clean_sessions
        """)
    conn.commit()

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
    _ensure_gear_activity_links_table(conn)
    _ensure_clean_sessions_compat_view(conn)

def _ensure_weight_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS weight_log (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            weight_kg DECIMAL(5,2),
            waist_cm DECIMAL(5,1),
            body_fat_pct DECIMAL(4,1),
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _ensure_wellness_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS wellness (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            category TEXT,
            compex_program TEXT,
            muscle_zone TEXT[],
            duration_min SMALLINT,
            ceragem_duration_min SMALLINT,
            ceragem_sensation_before SMALLINT,
            ceragem_sensation_after SMALLINT,
            sleep_hours DECIMAL(4,2),
            sleep_quality TEXT,
            hr_rest SMALLINT,
            garmin_sleep_score SMALLINT,
            pain_zone TEXT,
            pain_level SMALLINT,
            pain_start DATE,
            pain_end DATE,
            pain_type TEXT,
            stress_level SMALLINT,
            stress_cause TEXT,
            notes TEXT,
            fatigue SMALLINT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _ensure_fuerza_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS fuerza (
            id SERIAL PRIMARY KEY,
            date DATE NOT NULL,
            category TEXT,
            subcategory TEXT,
            muscle_groups TEXT[],
            intensity SMALLINT,
            duration_min SMALLINT,
            sets SMALLINT,
            reps SMALLINT,
            weight_kg DECIMAL(6,2),
            exercise TEXT,
            notes TEXT,
            rpe SMALLINT,
            fatigue_before SMALLINT,
            fatigue_after SMALLINT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

# In-memory fallback
RESULTS_STORE: dict = {}
RESULTS_STORE_MAX = 5  # Máximo de sesiones en memoria
