"""
v6.5 — Training Context & Workout Intelligence (fase 1, sin riesgo).

Endpoints:
  GET  /gpt/training-context        → qué plan sigo, semana, qué toca, cómo voy (read-only)
  GET  /gpt/session/{id}/laps       → intervalos de una sesión (read-only)
  POST /api/strava/transform-laps   → staging strava_laps_raw → session_laps (manual, idempotente)

Las tablas (session_laps, training_plans, plan_sessions) son aditivas:
CREATE IF NOT EXISTS, nunca tocan datos existentes. DDL espejo en
migrations/v6_5_training_context.sql.
"""
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query

from db import get_db

logger = logging.getLogger("epoch.training_context")
router = APIRouter()

# Zonas FC ciclismo (mismas que mars_context default; v6.5: leer de zone_models)
_ZONES_CYCLING = [("z1", 0, 108), ("trans", 109, 133), ("z2", 134, 150),
                  ("z3", 151, 160), ("z4", 161, 168), ("z5", 169, 999)]


def _ensure_training_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS session_laps (
                lap_id           BIGSERIAL PRIMARY KEY,
                clean_session_id TEXT NOT NULL REFERENCES clean_sessions(clean_session_id) ON DELETE CASCADE,
                lap_index        INT NOT NULL,
                name             TEXT,
                duration_s       INT,
                moving_s         INT,
                distance_km      NUMERIC(9,3),
                avg_speed_kmh    NUMERIC(6,2),
                max_speed_kmh    NUMERIC(6,2),
                avg_hr_bpm       NUMERIC(5,1),
                max_hr_bpm       INT,
                avg_cadence      NUMERIC(5,1),
                avg_watts        NUMERIC(7,1),
                zone_label       TEXT,
                lap_type         TEXT,
                source           TEXT DEFAULT 'strava',
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (clean_session_id, lap_index)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_session_laps_session ON session_laps(clean_session_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS training_plans (
                plan_id     TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                source      TEXT DEFAULT 'manual',
                goal_id     INT,
                start_date  DATE,
                end_date    DATE,
                total_weeks INT,
                status      TEXT DEFAULT 'active',
                meta        JSONB,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plan_sessions (
                id              BIGSERIAL PRIMARY KEY,
                plan_id         TEXT NOT NULL REFERENCES training_plans(plan_id) ON DELETE CASCADE,
                week_number     INT NOT NULL,
                planned_date    DATE,
                session_type    TEXT,
                description     TEXT,
                target          JSONB,
                matched_clean_session_id TEXT,
                status          TEXT DEFAULT 'planned',
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_plan_sessions_plan_week ON plan_sessions(plan_id, week_number)")
    conn.commit()


def _zone_for_hr(hr):
    if hr is None:
        return None
    for label, lo, hi in _ZONES_CYCLING:
        if lo <= hr <= hi:
            return label
    return None


def _classify_lap(lap_hr, session_avg_hr):
    """Heurística simple v6.5: work/recovery/steady relativo al promedio de la sesión."""
    if lap_hr is None or not session_avg_hr:
        return None
    ratio = float(lap_hr) / float(session_avg_hr)
    if ratio >= 1.06:
        return "work"
    if ratio <= 0.94:
        return "recovery"
    return "steady"


# ── GET /gpt/training-context ─────────────────────────────────────────────────

@router.get("/gpt/training-context")
def training_context():
    """Estado del entrenamiento ACTUAL: plan, semana, meta, última sesión, gaps."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_training_tables(conn)
    gaps = []

    # 1. Meta activa
    goal = None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, event_name, event_type, event_date, distance_km, elevation_m
            FROM mars_goals WHERE status='active' ORDER BY priority ASC LIMIT 1
        """)
        g = cur.fetchone()
    if g:
        goal = {"id": g[0], "event_name": g[1], "event_type": g[2],
                "event_date": g[3].isoformat() if g[3] else None,
                "distance_km": float(g[4]) if g[4] else None,
                "elevation_m": float(g[5]) if g[5] else None}
        if g[3]:
            goal["weeks_to_event"] = max(0, (g[3] - date.today()).days // 7)
    else:
        gaps.append("sin_meta_activa")

    # 2. Plan estructurado (training_plans) con fallback al plan declarativo
    plan = None
    plan_week = None
    today_session = None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT plan_id, name, source, start_date, end_date, total_weeks, meta
            FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        p = cur.fetchone()
    if p:
        plan = {"plan_id": p[0], "name": p[1], "source": p[2],
                "start_date": p[3].isoformat() if p[3] else None,
                "end_date": p[4].isoformat() if p[4] else None,
                "total_weeks": p[5], "structured": True}
        if p[3]:
            wk = max(1, ((date.today() - p[3]).days // 7) + 1)
            plan_week = {"week_number": wk, "total_weeks": p[5]}
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_type, description, target, status, planned_date
                    FROM plan_sessions
                    WHERE plan_id=%s AND (planned_date=%s OR (planned_date IS NULL AND week_number=%s))
                    ORDER BY planned_date NULLS LAST LIMIT 3
                """, (p[0], date.today(), wk))
                rows = cur.fetchall()
            today_session = [{"session_type": r[0], "description": r[1],
                              "target": r[2], "status": r[3],
                              "planned_date": r[4].isoformat() if r[4] else None}
                             for r in rows] or None
            if not today_session:
                gaps.append("sin_sesiones_planificadas_esta_semana")
    else:
        gaps.append("sin_plan_estructurado")
        # Fallback: plan declarativo de mars_context (texto, sin semanas)
        try:
            from mars_context import _get_profile
            mc = _get_profile(conn)
            pg = (mc or {}).get("plan_garmin") or {}
            if pg.get("nombre"):
                plan = {"name": pg["nombre"], "fase": pg.get("fase"),
                        "desc": pg.get("desc"), "source": "mars_context_declarativo",
                        "structured": False}
        except Exception:
            pass

    # 3. Semana actual real (clean_sessions) + cobertura de laps
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(distance_km),0),
                   COALESCE(SUM(duration_s),0)/3600.0
            FROM clean_sessions
            WHERE start_date >= date_trunc('week', CURRENT_DATE)::date
        """)
        w = cur.fetchone()
        cur.execute("""
            SELECT cs.clean_session_id, cs.name, cs.sport_type, cs.start_date,
                   cs.distance_km, cs.duration_s, cs.avg_hr_bpm,
                   COUNT(sl.lap_id) AS laps
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.start_time IS NOT NULL
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_time DESC LIMIT 1
        """)
        ls = cur.fetchone()
        cur.execute("""
            SELECT COUNT(DISTINCT cs.clean_session_id) FILTER (WHERE sl.lap_id IS NOT NULL),
                   COUNT(DISTINCT cs.clean_session_id)
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.start_date > CURRENT_DATE - 28
        """)
        cov = cur.fetchone()

    last_session = None
    if ls:
        last_session = {"clean_session_id": ls[0], "name": ls[1], "sport_type": ls[2],
                        "date": ls[3].isoformat() if ls[3] else None,
                        "distance_km": float(ls[4]) if ls[4] else None,
                        "duration_s": ls[5], "avg_hr_bpm": float(ls[6]) if ls[6] else None,
                        "laps_count": ls[7]}
        if not ls[7]:
            gaps.append("ultima_sesion_sin_laps")
    laps_coverage = {"sessions_with_laps_4w": cov[0] or 0, "total_sessions_4w": cov[1] or 0}
    if (cov[1] or 0) > 0 and (cov[0] or 0) == 0:
        gaps.append("laps_no_transformados — correr POST /api/strava/transform-laps")

    return {
        "ok": True,
        "goal": goal,
        "plan": plan,
        "plan_week": plan_week,
        "today": today_session,
        "week_actual": {"sessions": w[0], "km": float(w[1]), "hours": round(float(w[2]), 1)},
        "last_session": last_session,
        "laps_coverage": laps_coverage,
        "data_gaps": gaps,
        "nota": "El pasado es contexto. Este endpoint describe el entrenamiento ACTUAL y declara qué falta.",
    }


# ── GET /gpt/session/{id}/laps ────────────────────────────────────────────────

@router.get("/gpt/session/{clean_session_id}/laps")
def session_laps(clean_session_id: str):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT lap_index, name, duration_s, distance_km, avg_speed_kmh,
                   avg_hr_bpm, max_hr_bpm, avg_cadence, avg_watts, zone_label, lap_type
            FROM session_laps WHERE clean_session_id=%s ORDER BY lap_index
        """, (clean_session_id,))
        rows = cur.fetchall()
    laps = [{"lap_index": r[0], "name": r[1], "duration_s": r[2],
             "distance_km": float(r[3]) if r[3] else None,
             "avg_speed_kmh": float(r[4]) if r[4] else None,
             "avg_hr_bpm": float(r[5]) if r[5] else None, "max_hr_bpm": r[6],
             "avg_cadence": float(r[7]) if r[7] else None,
             "avg_watts": float(r[8]) if r[8] else None,
             "zone_label": r[9], "lap_type": r[10]} for r in rows]
    work = [l for l in laps if l["lap_type"] == "work"]
    return {"ok": True, "clean_session_id": clean_session_id, "laps": laps,
            "summary": {"total_laps": len(laps), "work_laps": len(work),
                        "structure_detected": len(work) >= 2}}


# ── POST /api/strava/transform-laps ──────────────────────────────────────────

@router.post("/api/strava/transform-laps")
def transform_laps(batch: int = Query(200, le=500)):
    """
    Transforma strava_laps_raw (Supabase staging) → session_laps (Railway).
    Idempotente (ON CONFLICT DO NOTHING). Manual — no corre automático.
    """
    from strava.auth import get_supabase
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_training_tables(conn)
    sb = get_supabase()

    # Sesiones Strava ya transformadas que aún no tienen laps
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cs.clean_session_id, cs.source_activity_id, cs.avg_hr_bpm
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.source='strava' AND cs.source_activity_id IS NOT NULL
              AND sl.lap_id IS NULL
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_time DESC
            LIMIT %s
        """, (batch,))
        pending = cur.fetchall()

    if not pending:
        return {"ok": True, "transformed_sessions": 0, "laps_inserted": 0,
                "message": "Sin sesiones pendientes de laps"}

    by_activity = {str(r[1]): (r[0], r[2]) for r in pending}
    activity_ids = [int(a) for a in by_activity.keys()]

    inserted = 0
    sessions_done = set()
    # Supabase: traer laps por lotes de ids
    for i in range(0, len(activity_ids), 100):
        chunk = activity_ids[i:i + 100]
        resp = (sb.table("strava_laps_raw")
                .select("strava_activity_id,lap_index,name,distance_m,moving_time_s,"
                        "elapsed_time_s,avg_speed_ms,max_speed_ms,avg_hr,max_hr,"
                        "avg_cadence,avg_watts")
                .in_("strava_activity_id", chunk).execute())
        for lap in (resp.data or []):
            key = str(lap["strava_activity_id"])
            if key not in by_activity:
                continue
            cs_id, session_avg_hr = by_activity[key]
            avg_hr = lap.get("avg_hr")
            row = {
                "clean_session_id": cs_id,
                "lap_index": lap.get("lap_index") or 0,
                "name": lap.get("name"),
                "duration_s": lap.get("elapsed_time_s"),
                "moving_s": lap.get("moving_time_s"),
                "distance_km": round((lap.get("distance_m") or 0) / 1000, 3) or None,
                "avg_speed_kmh": round((lap.get("avg_speed_ms") or 0) * 3.6, 2) or None,
                "max_speed_kmh": round((lap.get("max_speed_ms") or 0) * 3.6, 2) or None,
                "avg_hr_bpm": avg_hr,
                "max_hr_bpm": int(lap["max_hr"]) if lap.get("max_hr") else None,
                "avg_cadence": lap.get("avg_cadence"),
                "avg_watts": lap.get("avg_watts"),
                "zone_label": _zone_for_hr(avg_hr),
                "lap_type": _classify_lap(avg_hr, session_avg_hr),
            }
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO session_laps (
                            clean_session_id, lap_index, name, duration_s, moving_s,
                            distance_km, avg_speed_kmh, max_speed_kmh, avg_hr_bpm,
                            max_hr_bpm, avg_cadence, avg_watts, zone_label, lap_type
                        ) VALUES (
                            %(clean_session_id)s, %(lap_index)s, %(name)s, %(duration_s)s,
                            %(moving_s)s, %(distance_km)s, %(avg_speed_kmh)s,
                            %(max_speed_kmh)s, %(avg_hr_bpm)s, %(max_hr_bpm)s,
                            %(avg_cadence)s, %(avg_watts)s, %(zone_label)s, %(lap_type)s
                        ) ON CONFLICT (clean_session_id, lap_index) DO NOTHING
                    """, row)
                conn.commit()
                inserted += 1
                sessions_done.add(cs_id)
            except Exception as e:
                conn.rollback()
                logger.warning(f"lap insert fail {cs_id}#{row['lap_index']}: {e}")

    return {"ok": True, "transformed_sessions": len(sessions_done),
            "laps_inserted": inserted, "pending_batch": len(pending),
            "nota": "Re-ejecutar hasta transformed_sessions=0 para completar historial."}
