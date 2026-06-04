"""
db.py — Database connection and table helpers for Bitácora Mars
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

def _ensure_gear_service_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS gear_service (
            id SERIAL PRIMARY KEY, gear_id TEXT, gear_name TEXT,
            service_type TEXT NOT NULL, description TEXT, date DATE NOT NULL,
            km_at_service NUMERIC(10,1), cost_mxn NUMERIC(10,2),
            shop TEXT, notes TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""")
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