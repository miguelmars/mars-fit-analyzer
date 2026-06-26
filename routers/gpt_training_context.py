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
import json
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from db import get_db, _ensure_zone_model_system
from mars_zones import zones_from_threshold, describe_zone_shift

logger = logging.getLogger("epoch.training_context")
router = APIRouter()

# Zonas FC ciclismo (mismas que mars_context default; v6.5: leer de zone_models)
_ZONES_CYCLING = [("z1", 0, 108), ("trans", 109, 133), ("z2", 134, 150),
                  ("z3", 151, 160), ("z4", 161, 168), ("z5", 169, 999)]


# ── Living zones anchored to threshold tests ─────────────────────────────────
# The founding EPOCH story made real: a test becomes the official zone anchor,
# zones move with it, and every anchor is kept as history (no more handbrake).

class ZoneAnchorIn(BaseModel):
    sport: str = "cycling"
    lt_bpm: int                       # threshold HR from the test (the anchor)
    max_hr: Optional[int] = None
    test_date: Optional[str] = None   # YYYY-MM-DD; defaults to today
    source: Optional[str] = None      # e.g. "ftp_test_2026-07-15"
    notes: Optional[str] = None


def _ensure_athlete_tests_table(conn):
    """Small local guard so zone anchors can also become athlete-history evidence."""
    with conn.cursor() as cur:
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
    conn.commit()


@router.post("/gpt/zone-anchor")
def gpt_zone_anchor(body: ZoneAnchorIn):
    """Anchor your HR zones to a threshold test. Builds a new versioned zone
    model, archives the previous one, and reports how your zones moved."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    eff_from = body.test_date or date.today().isoformat()
    model = zones_from_threshold(body.lt_bpm, body.sport, body.max_hr)
    model_id = f"{body.sport}-{eff_from}-lthr{body.lt_bpm}"
    try:
        _ensure_zone_model_system(conn)
        _ensure_athlete_tests_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lt_bpm FROM zone_models
                WHERE sport=%s AND status='active'
                ORDER BY effective_from DESC NULLS LAST LIMIT 1
            """, (body.sport,))
            row = cur.fetchone()
            prev_lt = row[0] if row else None
            # Archive the current active anchor.
            cur.execute("""
                UPDATE zone_models SET status='historical', effective_to=(%s::date - 1)
                WHERE sport=%s AND status='active'
            """, (eff_from, body.sport))
            # Insert the new active anchor (idempotent on same test/day).
            cur.execute("""
                INSERT INTO zone_models
                    (model_id, sport, lt_bpm, max_hr_bpm, method, zones_json,
                     effective_from, effective_to, status, source, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,'active',%s,%s)
                ON CONFLICT (model_id) DO UPDATE SET
                    lt_bpm=EXCLUDED.lt_bpm, max_hr_bpm=EXCLUDED.max_hr_bpm,
                    zones_json=EXCLUDED.zones_json, status='active',
                    effective_from=EXCLUDED.effective_from, effective_to=NULL,
                    source=EXCLUDED.source, notes=EXCLUDED.notes
            """, (model_id, body.sport, body.lt_bpm, body.max_hr, "field_test_lthr",
                  json.dumps(model["zones"]), eff_from, body.source or "field_test",
                  body.notes))
            cur.execute("""
                SELECT id FROM athlete_tests
                WHERE date=%s::date AND type='lthr_anchor'
                  AND result_value=%s AND result_unit='bpm'
                ORDER BY id DESC LIMIT 1
            """, (eff_from, body.lt_bpm))
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO athlete_tests
                        (date, type, result_value, result_unit, avg_hr_bpm,
                         conditions, notes, raw_data)
                    VALUES (%s, 'lthr_anchor', %s, 'bpm', %s, %s, %s, %s::jsonb)
                """, (
                    eff_from,
                    body.lt_bpm,
                    body.lt_bpm,
                    body.source or "field_test",
                    body.notes,
                    json.dumps({
                        "model_id": model_id,
                        "sport": body.sport,
                        "max_hr_bpm": body.max_hr,
                        "zones": model["zones"],
                    }),
                ))
        conn.commit()
        shift = describe_zone_shift(prev_lt, body.lt_bpm, body.sport) if prev_lt else None
        return {
            "ok": True,
            "model_id": model_id,
            "anchor": model,
            "previous_lt_bpm": prev_lt,
            "shift": shift,
            "message": "Zones re-anchored to your test — handbrake off.",
        }
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@router.get("/gpt/zone-history")
def gpt_zone_history(sport: str = "cycling"):
    """Your zone evolution: every anchor over time, oldest to newest."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT model_id, lt_bpm, max_hr_bpm, method, effective_from,
                       effective_to, status, source
                FROM zone_models WHERE sport=%s
                ORDER BY effective_from ASC NULLS FIRST
            """, (sport,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        history = []
        for r in rows:
            d = dict(zip(cols, r))
            for k in ("effective_from", "effective_to"):
                if d.get(k) and hasattr(d[k], "isoformat"):
                    d[k] = d[k].isoformat()
            history.append(d)
        return {"sport": sport, "count": len(history), "anchors": history}
    except Exception as e:
        raise HTTPException(500, str(e))


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
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    # Matcher lazy: enlaza sesiones planificadas con la ejecución real (idempotente)
    try:
        _match_plan_sessions(conn)
    except Exception:
        conn.rollback()
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
        # Fase actual según las fases reales del plan (meta.phases)
        try:
            _phases = (p[6] or {}).get("phases") or []
            _today = date.today().isoformat()
            for _ph in _phases:
                if _ph.get("start") <= _today <= _ph.get("end"):
                    plan["current_phase"] = _ph.get("name")
                    plan["phase_focus"] = _ph.get("focus")
                    break
        except Exception:
            pass
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
            FROM canonical_sessions
            WHERE start_date >= date_trunc('week', CURRENT_DATE)::date
        """)
        w = cur.fetchone()
        cur.execute("""
            SELECT cs.clean_session_id, cs.name, cs.sport_type, cs.start_date,
                   cs.distance_km, cs.duration_s, cs.avg_hr_bpm,
                   COUNT(sl.lap_id) AS laps
            FROM canonical_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.start_time IS NOT NULL
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_time DESC LIMIT 1
        """)
        ls = cur.fetchone()
        cur.execute("""
            SELECT COUNT(DISTINCT cs.clean_session_id) FILTER (WHERE sl.lap_id IS NOT NULL),
                   COUNT(DISTINCT cs.clean_session_id)
            FROM canonical_sessions cs
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
        gaps.append("laps_not_transformed — run POST /api/strava/transform-laps")

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
        "nota": "The past is context. This endpoint describes CURRENT training and declares what is missing.",
    }


# ── GET /gpt/session/{id}/laps ────────────────────────────────────────────────

@router.get("/gpt/session/{clean_session_id}/laps")
def session_laps(clean_session_id: str):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
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
def transform_laps(limit: int = Query(100, ge=1, le=500),
                   dry_run: bool = Query(False)):
    """
    Transforma strava_laps_raw (Supabase staging) → session_laps (Railway).
    Solo escribe en session_laps. Idempotente (ON CONFLICT DO NOTHING).
    dry_run=true: estima sin insertar. Si Supabase falla, devuelve resumen parcial.
    """
    from strava.auth import get_supabase
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    summary = {"ok": True, "dry_run": dry_run, "pending": 0, "processed": 0,
               "inserted": 0, "skipped": 0, "errors": 0}

    # Total pendiente (sesiones Strava sin laps) + batch de trabajo
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.source='strava' AND cs.source_activity_id IS NOT NULL
              AND sl.lap_id IS NULL
        """)
        summary["pending"] = cur.fetchone()[0]
        cur.execute("""
            SELECT cs.clean_session_id, cs.source_activity_id, cs.avg_hr_bpm
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.source='strava' AND cs.source_activity_id IS NOT NULL
              AND sl.lap_id IS NULL
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_time DESC
            LIMIT %s
        """, (limit,))
        batch_rows = cur.fetchall()

    if not batch_rows:
        summary["message"] = "No sessions pending laps"
        return summary

    by_activity = {str(r[1]): (r[0], r[2]) for r in batch_rows}
    activity_ids = [int(a) for a in by_activity.keys()]

    try:
        sb = get_supabase()
    except Exception as e:
        summary["ok"] = False
        summary["message"] = f"Supabase unavailable: {str(e)[:100]}"
        return summary

    sessions_seen = set()
    for i in range(0, len(activity_ids), 100):
        chunk = activity_ids[i:i + 100]
        try:
            resp = (sb.table("strava_laps_raw")
                    .select("strava_activity_id,lap_index,name,distance_m,moving_time_s,"
                            "elapsed_time_s,avg_speed_ms,max_speed_ms,avg_hr,max_hr,"
                            "avg_cadence,avg_watts")
                    .in_("strava_activity_id", chunk).execute())
        except Exception as e:
            # Rate limit o caída: detener y devolver resumen parcial
            summary["ok"] = False
            summary["message"] = f"Supabase stopped the batch: {str(e)[:120]}"
            break
        for lap in (resp.data or []):
            if (lap.get("lap_index") or 0) < 0:
                continue  # sentinel del backfill: actividad sin laps reales
            key = str(lap["strava_activity_id"])
            if key not in by_activity:
                continue
            cs_id, session_avg_hr = by_activity[key]
            sessions_seen.add(cs_id)
            summary["processed"] += 1
            if dry_run:
                continue
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
                    dup = cur.rowcount == 0
                conn.commit()
                if dup:
                    summary["skipped"] += 1
                else:
                    summary["inserted"] += 1
            except Exception as e:
                conn.rollback()
                summary["errors"] += 1
                logger.warning(f"lap insert fail {cs_id}#{row['lap_index']}: {e}")

    summary["sessions_in_batch"] = len(batch_rows)
    summary["sessions_with_laps_found"] = len(sessions_seen)
    summary["sessions_without_laps_in_staging"] = len(batch_rows) - len(sessions_seen)
    if dry_run:
        summary["message"] = (f"DRY RUN: {summary['processed']} laps ready to insert "
                              f"across {len(sessions_seen)} sessions. Nothing written.")
    elif summary["ok"]:
        summary["message"] = "Re-run until pending=0 to complete the history."
    return summary


# ── GET /gpt/session/{id}/workout-analysis — Workout Intelligence v1 ─────────

@router.get("/gpt/session/{clean_session_id}/workout-analysis")
def workout_analysis(clean_session_id: str):
    """
    Analiza una sesión por BLOQUES (laps), no por promedio total.
    Solo lee session_laps + clean_sessions. Sin targets inventados:
    si no hay plan_session enlazada → "sin objetivo programado".
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, sport_type, start_date, distance_km, duration_s,
                   avg_hr_bpm, max_hr_bpm, ascent_m, avg_cadence
            FROM canonical_sessions WHERE clean_session_id=%s
        """, (clean_session_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, "Session not found")
        cur.execute("""
            SELECT lap_index, duration_s, distance_km, avg_speed_kmh,
                   avg_hr_bpm, max_hr_bpm, avg_cadence, avg_watts,
                   zone_label, lap_type
            FROM session_laps WHERE clean_session_id=%s ORDER BY lap_index
        """, (clean_session_id,))
        lap_rows = cur.fetchall()
        cur.execute("""
            SELECT session_type, description, target FROM plan_sessions
            WHERE matched_clean_session_id=%s LIMIT 1
        """, (clean_session_id,))
        planned = cur.fetchone()

    session = {"name": s[0], "sport_type": s[1],
               "date": s[2].isoformat() if s[2] else None,
               "distance_km": float(s[3]) if s[3] else None,
               "duration_s": s[4],
               "avg_hr_bpm": float(s[5]) if s[5] else None,
               "max_hr_bpm": s[6], "ascent_m": s[7],
               "avg_cadence": float(s[8]) if s[8] else None}

    laps = [{"lap_index": r[0], "duration_s": r[1] or 0,
             "distance_km": float(r[2]) if r[2] else 0,
             "avg_speed_kmh": float(r[3]) if r[3] else None,
             "avg_hr_bpm": float(r[4]) if r[4] else None,
             "max_hr_bpm": r[5],
             "avg_cadence": float(r[6]) if r[6] else None,
             "avg_watts": float(r[7]) if r[7] else None,
             "zone_label": r[8], "lap_type": r[9]} for r in lap_rows]

    warnings = []
    if not laps:
        return {"ok": True, "session": session, "blocks": [],
                "workout_type": "desconocido", "structured": False,
                "confidence_score": 0.0,
                "warnings": ["No laps recorded — run transform-laps, or the activity has no structure."],
                "planned_target": "no programmed target",
                "explanation_text": "No blocks recorded for this session; only the overall average exists."}

    # Refinar etiquetas: primer lap work-largo = warmup heurístico, último = cooldown
    n = len(laps)
    work = [l for l in laps if l["lap_type"] == "work"]
    rec = [l for l in laps if l["lap_type"] == "recovery"]
    steady = [l for l in laps if l["lap_type"] == "steady"]
    if n >= 3 and laps[0]["lap_type"] in ("steady", "recovery"):
        laps[0]["block_role"] = "warmup"
    if n >= 3 and laps[-1]["lap_type"] in ("steady", "recovery"):
        laps[-1]["block_role"] = "cooldown"
    for l in laps:
        l.setdefault("block_role", l["lap_type"] or "steady")

    work_time = sum(l["duration_s"] for l in work)
    rec_time = sum(l["duration_s"] for l in rec)
    total_time = sum(l["duration_s"] for l in laps) or 1

    # work:recovery ratio
    wr_ratio = round(work_time / rec_time, 2) if rec_time > 0 else None

    # Degradación: último bloque work vs primero.
    # v6.5.2: velocidad↓ + FC igual/↑ = fatiga real; velocidad↓ + FC↓ = terreno
    # o intensidad menor (caso real strava_18758175501: 33→14 km/h con 169→153 bpm).
    degradation = None
    if len(work) >= 2:
        f, l_ = work[0], work[-1]
        if f["avg_speed_kmh"] and l_["avg_speed_kmh"]:
            speed_drop = round((f["avg_speed_kmh"] - l_["avg_speed_kmh"]) / f["avg_speed_kmh"] * 100, 1)
            hr_change = None
            if f["avg_hr_bpm"] and l_["avg_hr_bpm"]:
                hr_change = round(l_["avg_hr_bpm"] - f["avg_hr_bpm"], 1)
            if speed_drop <= 8:
                deg_reading = "sin_degradacion"
            elif hr_change is not None and hr_change <= -5:
                deg_reading = "terreno_o_intensidad_menor"   # velocidad y FC cayeron juntas
            elif hr_change is not None and hr_change >= -2:
                deg_reading = "fatiga_real"                  # más lento al mismo/mayor costo cardíaco
            else:
                deg_reading = "ambigua"
            degradation = {
                "first_work": {"speed_kmh": f["avg_speed_kmh"], "hr": f["avg_hr_bpm"], "cadence": f["avg_cadence"]},
                "last_work": {"speed_kmh": l_["avg_speed_kmh"], "hr": l_["avg_hr_bpm"], "cadence": l_["avg_cadence"]},
                "speed_drop_pct": speed_drop,
                "hr_change_bpm": hr_change,
                "reading": deg_reading,
            }
            if f["avg_cadence"] and l_["avg_cadence"]:
                degradation["cadence_drop_rpm"] = round(f["avg_cadence"] - l_["avg_cadence"], 1)

    # Recuperación de FC entre intervalos: HR de recovery vs work previo
    hr_recovery = None
    pairs = []
    for i in range(1, n):
        if laps[i]["lap_type"] == "recovery" and laps[i-1]["lap_type"] == "work":
            if laps[i]["avg_hr_bpm"] and laps[i-1]["avg_hr_bpm"]:
                pairs.append(laps[i-1]["avg_hr_bpm"] - laps[i]["avg_hr_bpm"])
    if pairs:
        avg_drop = round(sum(pairs) / len(pairs), 1)
        hr_recovery = {"avg_hr_drop_bpm": avg_drop, "samples": len(pairs),
                       "reads_well": avg_drop >= 15}

    # ── v6.5.1: Interval Quality Score — consistencia entre bloques work ─────
    # Intervalos bien ejecutados se parecen entre sí (duración, FC, velocidad).
    interval_quality = None
    if len(work) >= 2:
        import statistics as _st

        def _cv(vals):
            vals = [float(v) for v in vals if v]
            if len(vals) < 2:
                return None
            m = _st.mean(vals)
            return round(_st.pstdev(vals) / m, 3) if m else None

        cv_dur = _cv([l["duration_s"] for l in work])
        cv_hr = _cv([l["avg_hr_bpm"] for l in work])
        cv_spd = _cv([l["avg_speed_kmh"] for l in work])
        comps = [c for c in (cv_dur, cv_hr, cv_spd) if c is not None]
        if comps:
            avg_cv = sum(comps) / len(comps)
            iq_score = max(0, min(100, round(100 * (1 - avg_cv * 2))))
            interval_quality = {
                "score": iq_score,
                "label": "high" if iq_score >= 75 else "medium" if iq_score >= 50 else "low",
                "cv_duration": cv_dur, "cv_hr": cv_hr, "cv_speed": cv_spd,
                "nota": "100 = work blocks identical to each other; <50 = irregular execution or variable terrain.",
            }

    # Cadence fade — fatiga neuromuscular/coordinación
    cadence_fade = None
    if len(work) >= 2 and work[0]["avg_cadence"] and work[-1]["avg_cadence"]:
        cf_drop = round(float(work[0]["avg_cadence"]) - float(work[-1]["avg_cadence"]), 1)
        cadence_fade = {"first_rpm": work[0]["avg_cadence"], "last_rpm": work[-1]["avg_cadence"],
                        "drop_rpm": cf_drop, "significant": cf_drop >= 8}

    # Distribución de zonas por tiempo
    zone_time = {}
    for l in laps:
        z = l["zone_label"] or "sin_fc"
        zone_time[z] = zone_time.get(z, 0) + l["duration_s"]
    dominant_zone = max(zone_time, key=zone_time.get) if zone_time else None

    # workout_type heurístico
    structured = len(work) >= 2 and len(rec) >= 1
    hi_time = sum(v for k, v in zone_time.items() if k in ("z4", "z5"))
    tempo_time = zone_time.get("z3", 0)
    ascent = session["ascent_m"] or 0
    dist = session["distance_km"] or 0
    if structured:
        workout_type = "intervals"
    elif ascent > 0 and dist > 0 and (ascent / dist) > 15:
        workout_type = "climb"
    elif hi_time / total_time > 0.3:
        workout_type = "high_intensity"
    elif tempo_time / total_time > 0.4:
        workout_type = "tempo"
    elif dominant_zone in ("z1", "trans") and (session["avg_hr_bpm"] or 999) < 120:
        workout_type = "recovery"
    else:
        workout_type = "endurance"

    # Confianza: cuántos laps tienen FC + cuántos laps hay — con razón explícita
    laps_with_hr = sum(1 for l in laps if l["avg_hr_bpm"])
    confidence = round(min(1.0, (laps_with_hr / n) * (0.5 + min(0.5, n / 10))), 2)
    _reasons = []
    _reasons.append(f"{laps_with_hr}/{n} blocks with HR" + (" (complete)" if laps_with_hr == n else ""))
    _reasons.append(f"{n} blocks recorded" + (" — rich structure" if n >= 10 else " — limited structure" if n < 4 else ""))
    if n == 1:
        warnings.append("Only 1 lap — no intra-session structure; analysis limited to the average.")
        confidence = min(confidence, 0.3)
        _reasons.append("a single lap caps confidence at 0.3")
    if laps_with_hr < n:
        warnings.append(f"{n - laps_with_hr} laps without HR — partial classification.")
    if not structured and n >= 4:
        warnings.append("Several laps but no clear work/recovery pattern — possibly a free session with auto-laps.")
    confidence_reason = ("High: " if confidence >= 0.75 else "Medium: " if confidence >= 0.5 else "Low: ") + " · ".join(_reasons)

    # Capacidad construida → capacidades humanas (marco Epoch)
    cap_map = {"intervals": "The ability to repeat efforts above threshold (VO2/threshold)",
               "tempo": "Threshold resistance (sustained Z3)",
               "high_intensity": "Power and lactate tolerance",
               "climb": "Climbing performance (strength-endurance)",
               "endurance": "Aerobic engine (Z2 base)",
               "recovery": "Active recovery — load absorption"}
    capacity_built = cap_map.get(workout_type, "Aerobic base")
    _human_caps = {"intervals": ["power", "aerobic_fitness"],
                   "high_intensity": ["power", "aerobic_fitness"],
                   "endurance": ["aerobic_fitness", "endurance"],
                   "climb": ["strength_endurance", "power"],
                   "recovery": ["recovery"],
                   "tempo": ["endurance", "aerobic_fitness"]}
    capacities_built = _human_caps.get(workout_type, ["aerobic_fitness"])

    # ── v6.5.1: veredicto — qué salió bien / qué se degradó / qué falta ──────
    went_well, degraded, missing_context = [], [], []
    if interval_quality and interval_quality["score"] >= 75:
        went_well.append(f"Consistent work blocks (quality {interval_quality['score']}/100).")
    if hr_recovery and hr_recovery["reads_well"]:
        went_well.append(f"HR recovered {hr_recovery['avg_hr_drop_bpm']} bpm between intervals.")
    if degradation and degradation["speed_drop_pct"] <= 5:
        went_well.append("Speed held from first block to last.")
    if interval_quality and interval_quality["score"] < 50:
        degraded.append(f"Irregular execution across blocks (quality {interval_quality['score']}/100) — or the terrain varied a lot.")
    if degradation and degradation["reading"] == "fatiga_real":
        degraded.append(f"Speed fell {degradation['speed_drop_pct']}% at the end with sustained HR — real fatigue.")
    elif degradation and degradation["reading"] == "terreno_o_intensidad_menor":
        missing_context.append(f"Last block {degradation['speed_drop_pct']}% slower but with HR {abs(degradation['hr_change_bpm'])} bpm lower — looks like terrain or an easy close, not fatigue.")
    if cadence_fade and cadence_fade["significant"]:
        degraded.append(f"Cadence fell {cadence_fade['drop_rpm']} rpm in the hard blocks — neuromuscular fatigue.")
    if hr_recovery and not hr_recovery["reads_well"]:
        degraded.append(f"HR only dropped {hr_recovery['avg_hr_drop_bpm']} bpm between intervals — short or incomplete recovery.")
    if not planned:
        missing_context.append("No programmed target — compliance cannot be judged.")
    if hr_recovery is None and structured:
        missing_context.append("No work→recovery pairs with HR — recovery between intervals was not measured.")
    if laps_with_hr < n:
        missing_context.append(f"{n - laps_with_hr} blocks without HR.")

    # explanation_text — marco Epoch:
    # Observación → Interpretación → Capacidad construida → Sugerencia prudente
    _wt_label = {"intervals": "structured intervals", "tempo": "sustained tempo",
                 "high_intensity": "high intensity", "climb": "climbing work",
                 "endurance": "endurance ride", "recovery": "active recovery"}.get(workout_type, workout_type)
    obs = (f"OBSERVATION: {round(total_time/60)} min session, {n} blocks "
           f"({len(work)} work, {len(rec)} recovery, {len(steady)} steady), "
           f"dominant zone {dominant_zone or '—'}.")
    interp = f"INTERPRETATION: This was a {_wt_label} session."
    if interval_quality:
        interp += f" Interval quality {interval_quality['label']} ({interval_quality['score']}/100)."
    if degradation:
        if degradation["reading"] == "fatiga_real":
            interp += f" The last block was {degradation['speed_drop_pct']}% slower with sustained HR — real fatigue at the close."
        elif degradation["reading"] == "terreno_o_intensidad_menor":
            interp += f" The last block was {degradation['speed_drop_pct']}% slower but with lower HR — different terrain or an easy close, not necessarily fatigue."
        elif degradation["reading"] == "sin_degradacion":
            interp += " Speed held from first block to last."
    if cadence_fade and cadence_fade["significant"]:
        interp += f" Cadence dropped {cadence_fade['drop_rpm']} rpm — the legs lost their spark before the engine did."
    if hr_recovery and hr_recovery["reads_well"]:
        interp += f" HR dropped {hr_recovery['avg_hr_drop_bpm']} bpm between intervals — good recovery."
    cap_txt = f"CAPACITY BUILT: {capacity_built} ({' + '.join(capacities_built)})."
    sug = ("SUGGESTION: No programmed target; analysis based on the real lap structure. "
           "Registering the plan would allow judging compliance, not just execution."
           if not planned else
           f"SUGGESTION: Compare against what was planned: {planned[0] or ''} {planned[1] or ''}".strip() + ".")

    return {
        "ok": True,
        "session": session,
        "workout_type": workout_type,
        "structured": structured,
        "blocks": laps,
        "summary": {
            "total_blocks": n, "work_intervals": len(work), "recoveries": len(rec),
            "work_time_s": work_time, "recovery_time_s": rec_time,
            "work_recovery_ratio": wr_ratio,
            "zone_time_s": zone_time, "dominant_zone": dominant_zone,
        },
        "interval_quality": interval_quality,
        "cadence_fade": cadence_fade,
        "degradation": degradation,
        "hr_recovery": hr_recovery,
        "verdict": {"went_well": went_well, "degraded": degraded,
                    "missing_context": missing_context},
        "capacity_built": capacity_built,
        "capacities_built": capacities_built,
        "planned_target": ({"session_type": planned[0], "description": planned[1], "target": planned[2]}
                           if planned else "no programmed target; analysis based on real lap structure"),
        "confidence_score": confidence,
        "confidence_reason": confidence_reason,
        "warnings": warnings,
        "explanation_text": f"{obs} {interp} {cap_txt} {sug}",
    }


# ── GET /gpt/data-coverage — auditoría read-only de preservación de datos ────

# Campos que existen en staging (Supabase) pero el transform NO lleva a
# clean_sessions. Recuperables: siguen en strava_activities_raw/streams_raw.
_STAGING_FIELDS_NOT_CARRIED = [
    {"field": "suffer_score", "donde": "strava_activities_raw", "valor": "carga percibida Strava — proxy de training load"},
    {"field": "max_watts", "donde": "strava_activities_raw", "valor": "pico de potencia — sprint/fuerza"},
    {"field": "device", "donde": "strava_activities_raw", "valor": "confianza del sensor por dispositivo"},
    {"field": "gear_id", "donde": "strava_activities_raw", "valor": "km reales por bici — mantenimiento"},
    {"field": "elev_high_m/elev_low_m", "donde": "strava_activities_raw", "valor": "altitude range of the session"},
    {"field": "stream_temp", "donde": "strava_streams_raw", "valor": "esfuerzo ajustado por calor"},
    {"field": "stream_grade", "donde": "strava_streams_raw", "valor": "pendiente — calidad de subidas"},
    {"field": "stream_altitude", "donde": "strava_streams_raw", "valor": "elevation profile — VAM per block"},
    {"field": "stream_moving", "donde": "strava_streams_raw", "valor": "pausas reales — endurance ajustada"},
    {"field": "FIT laps", "donde": "decode_fit (solo respuesta API)", "valor": "intervalos de uploads Garmin — no persisten a session_laps"},
]


@router.get("/gpt/data-coverage")
def data_coverage():
    """Qué data tiene Epoch, por sesión y por campo. Read-only."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(avg_hr_bpm), COUNT(avg_cadence),
                   COUNT(*) FILTER (WHERE power_available),
                   COUNT(ascent_m), COUNT(start_lat), COUNT(calories),
                   COUNT(*) FILTER (WHERE moving_duration_s IS NOT NULL
                                    AND elapsed_duration_s IS NOT NULL
                                    AND elapsed_duration_s > moving_duration_s)
            FROM canonical_sessions
        """)
        t = cur.fetchone()
        cur.execute("""
            SELECT source, COUNT(*) FROM canonical_sessions GROUP BY source ORDER BY 2 DESC
        """)
        by_source = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(DISTINCT clean_session_id), COUNT(*) FROM session_laps WHERE lap_index >= 0")
        laps = cur.fetchone()
        cur.execute("""
            SELECT COUNT(DISTINCT sl.clean_session_id)
            FROM session_laps sl JOIN canonical_sessions cs USING (clean_session_id)
            WHERE cs.start_date > CURRENT_DATE - 28 AND sl.lap_index >= 0
        """)
        laps_4w = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM canonical_sessions WHERE start_date > CURRENT_DATE - 28")
        total_4w = cur.fetchone()[0]

    total = t[0] or 1
    pct = lambda v: round((v or 0) / total * 100, 1)
    return {
        "ok": True,
        "total_sessions": t[0],
        "by_source": by_source,
        "field_coverage_pct": {
            "hr": pct(t[1]), "cadence": pct(t[2]), "power": pct(t[3]),
            "ascent": pct(t[4]), "gps_start": pct(t[5]), "calories": pct(t[6]),
            "pauses_detectable": pct(t[7]),
        },
        "laps": {"sessions_with_laps": laps[0], "total_laps": laps[1],
                 "coverage_4w": f"{laps_4w}/{total_4w}"},
        "staging_not_carried": _STAGING_FIELDS_NOT_CARRIED,
        "nota": ("Nothing is permanently lost: what was not carried over remains in staging "
                 "(Supabase). Full streams exist for ~all activities."),
    }


# ── GET /gpt/weekly-intelligence — qué construyó la semana ───────────────────

_CAP_LABEL = {"aerobic_fitness": "aerobic engine", "endurance": "endurance",
              "power": "power", "strength_endurance": "strength-endurance",
              "recovery": "recovery", "tempo": "threshold"}


def _session_type_from_row(avg_hr, ascent_m, dist_km, dur_s, work_laps, total_laps):
    """Clasifica una sesión: con laps usa estructura; sin laps usa FC/ascenso."""
    if total_laps and total_laps > 1 and work_laps and work_laps >= 2:
        return "intervals"
    if ascent_m and dist_km and dist_km > 0 and (ascent_m / dist_km) > 15:
        return "climb"
    z = _zone_for_hr(avg_hr)
    if z in ("z4", "z5"):
        return "high_intensity"
    if z == "z3":
        return "tempo"
    if z == "z1" and (avg_hr or 999) < 120:
        return "recovery"
    return "endurance"


@router.get("/gpt/weekly-intelligence")
def weekly_intelligence(week_offset: int = Query(0, ge=0, le=52,
                        description="0=semana actual, 1=anterior, etc.")):
    """
    Responde: ¿qué construyó esta semana? Formato Epoch completo.
    Read-only: clean_sessions + session_laps + wellness. Sin targets inventados.
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        # Sesiones de la semana objetivo + laps agregados
        cur.execute("""
            WITH wk AS (
                SELECT (date_trunc('week', CURRENT_DATE) - (%s * INTERVAL '7 days'))::date AS start
            )
            SELECT cs.clean_session_id, cs.name, cs.sport_type, cs.start_date,
                   cs.distance_km, cs.duration_s, cs.avg_hr_bpm, cs.ascent_m,
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_index >= 0) AS laps,
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_type='work') AS work_laps
            FROM canonical_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            CROSS JOIN wk
            WHERE cs.start_date >= wk.start AND cs.start_date < wk.start + 7
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_date
        """, (week_offset,))
        rows = cur.fetchall()
        # Semana anterior (contexto, no instrucción)
        cur.execute("""
            WITH wk AS (
                SELECT (date_trunc('week', CURRENT_DATE) - (%s * INTERVAL '7 days'))::date AS start
            )
            SELECT COUNT(*), COALESCE(SUM(distance_km),0), COALESCE(SUM(duration_s),0)/3600.0
            FROM canonical_sessions CROSS JOIN wk
            WHERE start_date >= wk.start - 7 AND start_date < wk.start
        """, (week_offset,))
        prev = cur.fetchone()
        # Wellness checks de la semana (tabla: wellness)
        try:
            cur.execute("""
                WITH wk AS (
                    SELECT (date_trunc('week', CURRENT_DATE) - (%s * INTERVAL '7 days'))::date AS start
                )
                SELECT COUNT(DISTINCT date) FROM wellness CROSS JOIN wk
                WHERE date >= wk.start AND date < wk.start + 7 AND hr_rest IS NOT NULL
            """, (week_offset,))
            wellness_checks = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            wellness_checks = None
        # Meta/plan para contexto
        cur.execute("SELECT COUNT(*) FROM mars_goals WHERE status='active'")
        has_goal = cur.fetchone()[0] > 0
        cur.execute("SELECT COUNT(*) FROM training_plans WHERE status='active'")
        has_plan = cur.fetchone()[0] > 0
        # Cumplimiento plan vs ejecución de la semana (si hay plan_sessions)
        plan_compliance = None
        if has_plan:
            try:
                _match_plan_sessions(conn)
            except Exception:
                conn.rollback()
            cur.execute("""
                WITH wk AS (
                    SELECT (date_trunc('week', CURRENT_DATE) - (%s * INTERVAL '7 days'))::date AS start
                )
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE status='completed'),
                       COUNT(*) FILTER (WHERE status='skipped')
                FROM plan_sessions CROSS JOIN wk
                WHERE planned_date >= wk.start AND planned_date < wk.start + 7
            """, (week_offset,))
            pc = cur.fetchone()
            if pc and pc[0] > 0:
                plan_compliance = {"planned": pc[0], "completed": pc[1], "skipped": pc[2],
                                   "pending": pc[0] - pc[1] - pc[2]}

    sessions = []
    cap_time = {}
    for r in rows:
        avg_hr = float(r[6]) if r[6] else None
        stype = _session_type_from_row(avg_hr, r[7], float(r[4]) if r[4] else 0,
                                       r[5], r[9], r[8])
        caps = {"intervals": ["power", "aerobic_fitness"],
                "high_intensity": ["power", "aerobic_fitness"],
                "endurance": ["aerobic_fitness", "endurance"],
                "climb": ["strength_endurance", "power"],
                "recovery": ["recovery"],
                "tempo": ["endurance", "aerobic_fitness"]}.get(stype, ["aerobic_fitness"])
        dur = r[5] or 0
        for c in caps:
            cap_time[c] = cap_time.get(c, 0) + dur // len(caps)
        sessions.append({"clean_session_id": r[0], "name": r[1], "sport_type": r[2],
                         "date": r[3].isoformat() if r[3] else None,
                         "distance_km": float(r[4]) if r[4] else None,
                         "duration_s": r[5], "avg_hr_bpm": avg_hr,
                         "ascent_m": r[7], "laps": r[8], "structured": (r[9] or 0) >= 2,
                         "session_type": stype, "capacities": caps})

    n_ses = len(sessions)
    km = round(sum(s["distance_km"] or 0 for s in sessions), 1)
    hours = round(sum(s["duration_s"] or 0 for s in sessions) / 3600, 1)
    prev_ctx = {"sessions": prev[0], "km": round(float(prev[1]), 1),
                "hours": round(float(prev[2]), 1)}
    structured_n = sum(1 for s in sessions if s["structured"])
    with_hr = sum(1 for s in sessions if s["avg_hr_bpm"])
    with_laps = sum(1 for s in sessions if s["laps"])

    cap_ranked = sorted(cap_time.items(), key=lambda x: -x[1])
    dominant = cap_ranked[0][0] if cap_ranked else None
    cap_pct = {c: round(t / max(1, sum(cap_time.values())) * 100) for c, t in cap_ranked}

    # ── Formato Epoch ─────────────────────────────────────────────────────────
    types_count = {}
    for s in sessions:
        types_count[s["session_type"]] = types_count.get(s["session_type"], 0) + 1
    obs = (f"OBSERVATION: {n_ses} sessions, {km} km, {hours} h. "
           + " · ".join(f"{v}× {k}" for k, v in types_count.items())
           + f". Previous week: {prev_ctx['sessions']} sessions, {prev_ctx['km']} km.")
    if not sessions:
        interp = "INTERPRETATION: A week with no recorded sessions."
        cap_txt = "CAPACITY BUILT: none this week."
    else:
        interp = (f"INTERPRETATION: The week's time concentrated on "
                  f"{_CAP_LABEL.get(dominant, dominant)} ({cap_pct.get(dominant, 0)}%). "
                  f"{structured_n} of {n_ses} sessions had block structure.")
        cap_txt = ("CAPACITY BUILT: " +
                   ", ".join(f"{_CAP_LABEL.get(c, c)} {p}%" for c, p in cap_pct.items()) + ".")
    evid = (f"EVIDENCE: {with_hr}/{n_ses} sessions with HR, {with_laps}/{n_ses} with laps, "
            f"{structured_n} structured." if sessions else "EVIDENCE: no sessions.")
    conf_pct = round((with_hr / n_ses) * 100) if n_ses else 0
    conf = (f"CONFIDENCE: {'high' if conf_pct >= 80 else 'medium' if conf_pct >= 50 else 'low'} "
            f"({conf_pct}% of sessions with HR).")
    missing = []
    if not has_goal:
        missing.append("active goal")
    if not has_plan:
        missing.append("structured plan")
    if n_ses and with_laps < n_ses:
        missing.append(f"laps in {n_ses - with_laps} sessions")
    if wellness_checks is not None and wellness_checks < 4:
        missing.append(f"morning checks ({wellness_checks or 0}/7)")
    falta = "MISSING: " + (", ".join(missing) + "." if missing else "nothing critical.")
    if not sessions:
        sug = "PRUDENT SUGGESTION: no data this week — log or sync activities."
    elif not (has_goal or has_plan):
        sug = ("PRUDENT SUGGESTION: the week explains itself, but without a goal or plan there is no "
               "way to say whether it builds in the right direction. Registering a goal would give it one.")
    else:
        _phase_now = None
        try:
            with conn.cursor() as cur2:
                cur2.execute("SELECT meta FROM training_plans WHERE status='active' ORDER BY created_at DESC LIMIT 1")
                _pm = cur2.fetchone()
            _today2 = date.today().isoformat()
            for _ph in ((_pm[0] or {}).get("phases") or []) if _pm else []:
                if _ph.get("start") <= _today2 <= _ph.get("end"):
                    _phase_now = _ph
                    break
        except Exception:
            conn.rollback()
        if _phase_now:
            sug = (f"PRUDENT SUGGESTION: volume concentrated on {_CAP_LABEL.get(dominant, dominant)}. "
                   f"The current phase ({_phase_now.get('name')}) targets {_phase_now.get('focus')} — "
                   + ("they are aligned." if dominant in ("aerobic_fitness", "endurance") and _phase_now.get('name') == 'base'
                      else "worth checking they point the same way."))
        else:
            sug = (f"PRUDENT SUGGESTION: volume concentrated on {_CAP_LABEL.get(dominant, dominant)}; "
                   "check it against what the current plan phase asks for.")

    return {
        "ok": True,
        "week_offset": week_offset,
        "totals": {"sessions": n_ses, "km": km, "hours": hours},
        "previous_week": prev_ctx,
        "sessions": sessions,
        "capacities_built_pct": cap_pct,
        "dominant_capacity": dominant,
        "wellness_checks": wellness_checks,
        "has_goal": has_goal, "has_plan": has_plan,
        "plan_compliance": plan_compliance,
        "explanation_text": f"{obs} {interp} {cap_txt} {evid} {conf} {falta} {sug}",
    }


# ── POST /api/admin/seed-garmin-plan — siembra el plan REAL (una vez) ────────

@router.post("/api/admin/seed-garmin-plan")
def seed_garmin_plan():
    """
    Siembra el plan Garmin Coach real como dato estructurado (fuente: app
    Garmin del atleta, 2026-06-10). Idempotente: si ya existe, no duplica.
    Fases reales: Base may4–jun27 · Build jun28–ago22 · Peak ago23–oct3.
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    plan_id = "garmin_tt_2026"
    plan_meta = {
        "phases": [
            {"name": "base", "start": "2026-05-04", "end": "2026-06-27",
             "focus": "aerobic fitness and volume"},
            {"name": "build", "start": "2026-06-28", "end": "2026-08-22",
             "focus": "intensity and aerobic capacity"},
            {"name": "peak", "start": "2026-08-23", "end": "2026-10-03",
             "focus": "sharpening fitness with long, intense rides"},
        ],
        "event": "Time Trial",
        "source": "screenshots Garmin 2026-06-10",
    }
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM training_plans WHERE plan_id=%s", (plan_id,))
        if cur.fetchone():
            cur.execute("""
                UPDATE training_plans
                SET name=%s, source='garmin_coach', start_date='2026-05-04',
                    end_date='2026-10-03', total_weeks=22, status='active',
                    meta=%s::jsonb
                WHERE plan_id=%s
            """, ("Garmin Coach — Time Trial Plan", json.dumps(plan_meta), plan_id))
            conn.commit()
            return {"ok": True, "seeded": False, "updated": True,
                    "plan_id": plan_id,
                    "message": "Plan already existed; dates and phases were refreshed."}
        cur.execute("""
            INSERT INTO training_plans
                (plan_id, name, source, start_date, end_date, total_weeks, status, meta)
            VALUES (%s, %s, 'garmin_coach', '2026-05-04', '2026-10-03', 22, 'active',
                    %s::jsonb)
        """, (plan_id, "Garmin Coach — Time Trial Plan", json.dumps(plan_meta)))
        # Solo sesiones confirmadas por el atleta (semana jun 8-14). No inventar más.
        cur.execute("""
            INSERT INTO plan_sessions (plan_id, week_number, planned_date, session_type, description, target)
            VALUES
              (%s, 6, '2026-06-10', 'tempo_intervals', '5 Min. Tempo Intervals (plan Garmin)',
               '{"intervals":{"work_min":5,"zone":"tempo"}}'::jsonb),
              (%s, 6, '2026-06-11', 'ride', 'Garmin plan session (type to confirm)', NULL),
              (%s, 6, '2026-06-13', 'ride', 'Garmin plan session (type to confirm)', NULL)
        """, (plan_id, plan_id, plan_id))
    conn.commit()
    return {"ok": True, "seeded": True, "plan_id": plan_id,
            "message": "22-week Time Trial plan seeded with 3 sessions from week 6. "
                       "GET /gpt/training-context now has a structured plan."}


# ── Matcher plan ↔ ejecución (v6.5.4) ─────────────────────────────────────────

def _match_plan_sessions(conn):
    """
    Enlaza plan_sessions sin match con la clean_session real del mismo día.
    - planned_date == hoy y aún sin sesión → queda 'planned' (puede llegar más tarde)
    - planned_date < hoy sin sesión → 'skipped' (honesto, no castigo)
    Idempotente; solo escribe en plan_sessions.
    """
    matched = skipped = 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ps.id, ps.planned_date
            FROM plan_sessions ps
            WHERE ps.matched_clean_session_id IS NULL
              AND ps.status = 'planned'
              AND ps.planned_date IS NOT NULL
              AND ps.planned_date <= CURRENT_DATE
        """)
        pending = cur.fetchall()
        for ps_id, pdate in pending:
            cur.execute("""
                SELECT clean_session_id FROM canonical_sessions
                WHERE start_date = %s
                  AND sport_type IN ('Ride','VirtualRide','Run')
                ORDER BY duration_s DESC NULLS LAST LIMIT 1
            """, (pdate,))
            m = cur.fetchone()
            if m:
                cur.execute("""
                    UPDATE plan_sessions
                    SET matched_clean_session_id=%s, status='completed'
                    WHERE id=%s
                """, (m[0], ps_id))
                matched += 1
            elif pdate < date.today():
                cur.execute("UPDATE plan_sessions SET status='skipped' WHERE id=%s", (ps_id,))
                skipped += 1
    conn.commit()
    return {"matched": matched, "skipped": skipped, "checked": len(pending)}


@router.post("/api/admin/match-plan-sessions")
def match_plan_sessions_endpoint():
    """Corre el matcher manualmente. También corre solo en /gpt/training-context."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    result = _match_plan_sessions(conn)
    return {"ok": True, **result}


# ── Ciclo de vida del plan (v6.5.5): dar de baja sin borrar ──────────────────

@router.post("/api/training-plan/{plan_id}/deactivate")
def deactivate_plan(plan_id: str, reason: str = Query("otro", max_length=200),
                    outcome: str = Query("abandoned", pattern="^(abandoned|completed)$")):
    """
    Da de baja un plan SIN borrar nada: status → abandoned|completed,
    razón y fecha quedan en meta. plan_sessions, matches y cumplimiento
    se conservan como registro histórico.
    Razones típicas: lesion · nuevo_objetivo · ya_no_quiero · otro
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM training_plans WHERE plan_id=%s", (plan_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Plan not found")
        if row[0] != "active":
            return {"ok": True, "changed": False,
                    "message": f"Plan was already '{row[0]}' — no changes."}
        cur.execute("""
            UPDATE training_plans
            SET status=%s,
                meta = COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                    'ended_at', CURRENT_DATE::text,
                    'end_reason', %s::text)
            WHERE plan_id=%s
        """, (outcome, reason, plan_id))
    conn.commit()
    return {"ok": True, "changed": True, "plan_id": plan_id, "status": outcome,
            "reason": reason,
            "message": "Plan retired. The full historical record is preserved — "
                       "sessions, matches and compliance remain queryable."}


@router.get("/gpt/training-plans/history")
def plans_history():
    """Todos los planes (activos e históricos) con su razón de cierre."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT tp.plan_id, tp.name, tp.status, tp.start_date, tp.end_date,
                   tp.total_weeks, tp.meta,
                   COUNT(ps.id), COUNT(ps.id) FILTER (WHERE ps.status='completed')
            FROM training_plans tp
            LEFT JOIN plan_sessions ps ON ps.plan_id = tp.plan_id
            GROUP BY tp.plan_id ORDER BY tp.created_at DESC
        """)
        rows = cur.fetchall()
    plans = []
    for r in rows:
        meta = r[6] or {}
        plans.append({"plan_id": r[0], "name": r[1], "status": r[2],
                      "start_date": r[3].isoformat() if r[3] else None,
                      "end_date": r[4].isoformat() if r[4] else None,
                      "total_weeks": r[5],
                      "ended_at": meta.get("ended_at"),
                      "end_reason": meta.get("end_reason"),
                      "sessions_registered": r[7], "sessions_completed": r[8]})
    return {"ok": True, "plans": plans}


# ── V7.2: vista completa del plan + reporte de fase ──────────────────────────

@router.get("/gpt/training-plan")
def training_plan_full():
    """Todo lo que necesita la pantalla My Plan: plan, fases, semana actual,
    sesiones de la semana con estado real (matcher) y cumplimiento acumulado."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    try:
        _match_plan_sessions(conn)
    except Exception:
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT plan_id, name, source, start_date, end_date, total_weeks, meta
            FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        p = cur.fetchone()
        if not p:
            return {"ok": True, "plan": None,
                    "message": "No active plan. Past plans at /gpt/training-plans/history."}
        meta = p[6] or {}
        wk = max(1, ((date.today() - p[3]).days // 7) + 1) if p[3] else None
        cur.execute("""
            SELECT week_number, planned_date, session_type, description, status,
                   matched_clean_session_id
            FROM plan_sessions WHERE plan_id=%s
            ORDER BY planned_date NULLS LAST, week_number
        """, (p[0],))
        sess = cur.fetchall()

    phases = meta.get("phases") or []
    today_s = date.today().isoformat()
    current_phase = None
    # El meta del plan se sembró en español en la DB — traducir al leer
    _FOCUS_EN = {"aerobic fitness y volumen": "aerobic fitness and volume",
                 "intensidad y capacidad aerobica": "intensity and aerobic capacity",
                 "afinar fitness con rodadas largas e intensas":
                     "sharpening fitness with long, intense rides"}
    for ph in phases:
        ph["is_current"] = ph.get("start", "") <= today_s <= ph.get("end", "")
        ph["focus"] = _FOCUS_EN.get(ph.get("focus"), ph.get("focus"))
        if ph["is_current"]:
            current_phase = ph.get("name")

    sessions = [{"week_number": r[0],
                 "planned_date": r[1].isoformat() if r[1] else None,
                 "session_type": r[2], "description": r[3], "status": r[4],
                 "matched_clean_session_id": r[5]} for r in sess]
    this_week = [s for s in sessions if s["week_number"] == wk]
    done = sum(1 for s in sessions if s["status"] == "completed")
    skipped = sum(1 for s in sessions if s["status"] == "skipped")

    return {
        "ok": True,
        "plan": {"plan_id": p[0], "name": p[1], "source": p[2],
                 "start_date": p[3].isoformat() if p[3] else None,
                 "end_date": p[4].isoformat() if p[4] else None,
                 "total_weeks": p[5], "current_week": wk,
                 "current_phase": current_phase, "phases": phases,
                 "event": meta.get("event")},
        "this_week": this_week,
        "sessions": sessions,
        "compliance": {"registered": len(sessions), "completed": done,
                       "skipped": skipped,
                       "pending": len(sessions) - done - skipped},
        "nota": ("Only registered sessions count here — the rest of the "
                 "Garmin calendar comes in via weekly capture or the API (V7.0)."),
    }


@router.get("/gpt/phase-report")
def phase_report(phase: str = Query(None, description="base|build|peak. Empty = current phase")):
    """
    Phase Report Card: qué construyó una fase del plan. Sin culpa, con evidencia.
    Agrega clean_sessions del rango de fechas de la fase.
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT meta, name FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, "Sin plan activo")
        phases = (p[0] or {}).get("phases") or []
        today_s = date.today().isoformat()
        target = None
        for ph in phases:
            if phase and ph.get("name") == phase:
                target = ph
            elif not phase and ph.get("start", "") <= today_s <= ph.get("end", ""):
                target = ph
        if not target:
            raise HTTPException(404, f"Fase no encontrada. Disponibles: {[x.get('name') for x in phases]}")

        ph_start, ph_end = target["start"], min(target["end"], today_s)
        in_progress = target["end"] > today_s

        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(distance_km),0),
                   COALESCE(SUM(duration_s),0)/3600.0,
                   COALESCE(SUM(ascent_m),0),
                   AVG(avg_hr_bpm), AVG(efficiency_speed_hr),
                   COUNT(DISTINCT start_date)
            FROM canonical_sessions
            WHERE start_date >= %s::date AND start_date <= %s::date
        """, (ph_start, ph_end))
        t = cur.fetchone()
        # Eficiencia: primeras 2 semanas de la fase vs últimas 2
        cur.execute("""
            SELECT AVG(efficiency_speed_hr) FILTER (WHERE start_date < %s::date + 14),
                   AVG(efficiency_speed_hr) FILTER (WHERE start_date > %s::date - 14)
            FROM canonical_sessions
            WHERE start_date >= %s::date AND start_date <= %s::date
              AND efficiency_speed_hr IS NOT NULL
        """, (ph_start, ph_end, ph_start, ph_end))
        eff = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE status='completed')
            FROM plan_sessions ps JOIN training_plans tp USING (plan_id)
            WHERE tp.status='active' AND ps.planned_date >= %s::date
              AND ps.planned_date <= %s::date
        """, (ph_start, ph_end))
        pc = cur.fetchone()

    weeks_elapsed = max(1, (date.fromisoformat(ph_end) - date.fromisoformat(ph_start)).days // 7)
    eff_delta_pct = None
    if eff[0] and eff[1]:
        eff_delta_pct = round((float(eff[1]) - float(eff[0])) / float(eff[0]) * 100, 1)

    obs = (f"OBSERVATION: {target['name']} phase"
           + (" (in progress)" if in_progress else " (completed)")
           + f": {t[0]} sessions, {round(float(t[1]))} km, {round(float(t[2]),1)} h, "
           + f"{round(float(t[3]))} m of climbing in {weeks_elapsed} weeks.")
    built = []
    if float(t[1]) / weeks_elapsed >= 40:
        built.append(f"sustained volume (~{round(float(t[1])/weeks_elapsed)} km/week)")
    if eff_delta_pct is not None and eff_delta_pct > 1:
        built.append(f"aerobic efficiency +{eff_delta_pct}% within the phase")
    if t[6] and t[6] / (weeks_elapsed * 7) >= 0.4:
        built.append(f"consistency ({t[6]} active days)")
    interp = ("INTERPRETATION: " + ("This phase built: " + ", ".join(built) + "."
              if built else "The phase is advancing; no strong adaptation signal yet — normal in the first weeks."))
    foco = target.get("focus")
    cap_txt = f"CAPACITY BUILT: {foco}." if foco else ""
    falta = ""
    if pc[0]:
        falta = f"MISSING: of the registered plan, {pc[1]}/{pc[0]} sessions completed in this phase."
    sin_culpa = ("If you moved or skipped sessions, no guilt — a phase is judged by what "
                 "it built, not by a perfect checklist.")

    return {
        "ok": True, "phase": target["name"], "in_progress": in_progress,
        "range": {"start": ph_start, "end": target["end"], "evaluated_until": ph_end},
        "totals": {"sessions": t[0], "km": round(float(t[1]), 1),
                   "hours": round(float(t[2]), 1), "ascent_m": int(t[3] or 0),
                   "active_days": t[6], "avg_hr": round(float(t[4]), 1) if t[4] else None},
        "efficiency_delta_pct": eff_delta_pct,
        "plan_compliance": {"registered": pc[0], "completed": pc[1]} if pc[0] else None,
        "explanation_text": f"{obs} {interp} {cap_txt} {falta} {sin_culpa}".strip(),
    }
