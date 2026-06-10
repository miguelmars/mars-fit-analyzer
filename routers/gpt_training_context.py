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
        raise HTTPException(503, "DB no disponible")
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
        summary["message"] = "Sin sesiones pendientes de laps"
        return summary

    by_activity = {str(r[1]): (r[0], r[2]) for r in batch_rows}
    activity_ids = [int(a) for a in by_activity.keys()]

    try:
        sb = get_supabase()
    except Exception as e:
        summary["ok"] = False
        summary["message"] = f"Supabase no disponible: {str(e)[:100]}"
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
            summary["message"] = f"Supabase detuvo el batch: {str(e)[:120]}"
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
        summary["message"] = (f"DRY RUN: {summary['processed']} laps listos para insertar "
                              f"en {len(sessions_seen)} sesiones. Nada escrito.")
    elif summary["ok"]:
        summary["message"] = "Re-ejecutar hasta pending=0 para completar historial."
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
        raise HTTPException(503, "DB no disponible")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, sport_type, start_date, distance_km, duration_s,
                   avg_hr_bpm, max_hr_bpm, ascent_m, avg_cadence
            FROM clean_sessions WHERE clean_session_id=%s
        """, (clean_session_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, "Sesión no encontrada")
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
                "warnings": ["Sin laps registrados — correr transform-laps o la actividad no tiene estructura."],
                "planned_target": "sin objetivo programado",
                "explanation_text": "No hay bloques registrados para esta sesión; solo existe el promedio total."}

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
                "label": "alta" if iq_score >= 75 else "media" if iq_score >= 50 else "baja",
                "cv_duration": cv_dur, "cv_hr": cv_hr, "cv_speed": cv_spd,
                "nota": "100 = bloques de trabajo idénticos entre sí; <50 = ejecución irregular o terreno variable.",
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
    _reasons.append(f"{laps_with_hr}/{n} bloques con FC" + (" (completo)" if laps_with_hr == n else ""))
    _reasons.append(f"{n} bloques registrados" + (" — estructura rica" if n >= 10 else " — estructura limitada" if n < 4 else ""))
    if n == 1:
        warnings.append("Solo 1 lap — sin estructura intra-sesión; análisis limitado al promedio.")
        confidence = min(confidence, 0.3)
        _reasons.append("1 solo lap limita la confianza a 0.3")
    if laps_with_hr < n:
        warnings.append(f"{n - laps_with_hr} laps sin FC — clasificación parcial.")
    if not structured and n >= 4:
        warnings.append("Varios laps pero sin patrón work/recovery claro — posible sesión libre con auto-laps.")
    confidence_reason = ("Alta: " if confidence >= 0.75 else "Media: " if confidence >= 0.5 else "Baja: ") + " · ".join(_reasons)

    # Capacidad construida → capacidades humanas (marco Epoch)
    cap_map = {"intervals": "Capacidad de repetir esfuerzos sobre umbral (VO2/umbral)",
               "tempo": "Resistencia al umbral (Z3 sostenido)",
               "high_intensity": "Potencia y tolerancia al lactato",
               "climb": "Rendimiento en subidas (fuerza-resistencia)",
               "endurance": "Motor aeróbico (base Z2)",
               "recovery": "Recuperación activa — asimilación de carga"}
    capacity_built = cap_map.get(workout_type, "Base aeróbica")
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
        went_well.append(f"Bloques de trabajo consistentes (calidad {interval_quality['score']}/100).")
    if hr_recovery and hr_recovery["reads_well"]:
        went_well.append(f"FC recuperó {hr_recovery['avg_hr_drop_bpm']} bpm entre intervalos.")
    if degradation and degradation["speed_drop_pct"] <= 5:
        went_well.append("Velocidad sostenida del primer al último bloque.")
    if interval_quality and interval_quality["score"] < 50:
        degraded.append(f"Ejecución irregular entre bloques (calidad {interval_quality['score']}/100) — o el terreno varió mucho.")
    if degradation and degradation["reading"] == "fatiga_real":
        degraded.append(f"Velocidad cayó {degradation['speed_drop_pct']}% al final con FC sostenida — fatiga real.")
    elif degradation and degradation["reading"] == "terreno_o_intensidad_menor":
        missing_context.append(f"Último bloque {degradation['speed_drop_pct']}% más lento pero con FC {abs(degradation['hr_change_bpm'])} bpm menor — parece terreno o cierre suave, no fatiga.")
    if cadence_fade and cadence_fade["significant"]:
        degraded.append(f"Cadencia cayó {cadence_fade['drop_rpm']} rpm en los bloques fuertes — fatiga neuromuscular.")
    if hr_recovery and not hr_recovery["reads_well"]:
        degraded.append(f"FC solo bajó {hr_recovery['avg_hr_drop_bpm']} bpm entre intervalos — recuperación corta o incompleta.")
    if not planned:
        missing_context.append("Sin objetivo programado — no se puede evaluar cumplimiento.")
    if hr_recovery is None and structured:
        missing_context.append("Sin pares work→recovery con FC — no se midió recuperación entre intervalos.")
    if laps_with_hr < n:
        missing_context.append(f"{n - laps_with_hr} bloques sin FC.")

    # explanation_text — marco Epoch:
    # Observación → Interpretación → Capacidad construida → Sugerencia prudente
    _wt_label = {"intervals": "intervalos estructurados", "tempo": "tempo sostenido",
                 "high_intensity": "alta intensidad", "climb": "trabajo de subidas",
                 "endurance": "rodada de resistencia", "recovery": "recuperación activa"}.get(workout_type, workout_type)
    obs = (f"OBSERVACIÓN: Sesión de {round(total_time/60)} min, {n} bloques "
           f"({len(work)} trabajo, {len(rec)} recuperación, {len(steady)} estables), "
           f"zona dominante {dominant_zone or '—'}.")
    interp = f"INTERPRETACIÓN: Fue una sesión de {_wt_label}."
    if interval_quality:
        interp += f" Calidad de intervalos {interval_quality['label']} ({interval_quality['score']}/100)."
    if degradation:
        if degradation["reading"] == "fatiga_real":
            interp += f" El último bloque fue {degradation['speed_drop_pct']}% más lento con FC sostenida — fatiga real al cierre."
        elif degradation["reading"] == "terreno_o_intensidad_menor":
            interp += f" El último bloque fue {degradation['speed_drop_pct']}% más lento pero con FC más baja — terreno distinto o cierre suave, no necesariamente fatiga."
        elif degradation["reading"] == "sin_degradacion":
            interp += " Velocidad sostenida del primer al último bloque."
    if cadence_fade and cadence_fade["significant"]:
        interp += f" La cadencia bajó {cadence_fade['drop_rpm']} rpm — las piernas perdieron chispa antes que el motor."
    if hr_recovery and hr_recovery["reads_well"]:
        interp += f" La FC bajó {hr_recovery['avg_hr_drop_bpm']} bpm entre intervalos — buena recuperación."
    cap_txt = f"CAPACIDAD CONSTRUIDA: {capacity_built} ({' + '.join(capacities_built)})."
    sug = ("SUGERENCIA: Sin objetivo programado; análisis basado en estructura real de laps. "
           "Registrar el plan permitiría evaluar cumplimiento, no solo ejecución."
           if not planned else
           f"SUGERENCIA: Comparar contra lo planificado: {planned[0] or ''} {planned[1] or ''}".strip() + ".")

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
                           if planned else "sin objetivo programado; análisis basado en estructura real de laps"),
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
    {"field": "elev_high_m/elev_low_m", "donde": "strava_activities_raw", "valor": "rango de altitud de la sesión"},
    {"field": "stream_temp", "donde": "strava_streams_raw", "valor": "esfuerzo ajustado por calor"},
    {"field": "stream_grade", "donde": "strava_streams_raw", "valor": "pendiente — calidad de subidas"},
    {"field": "stream_altitude", "donde": "strava_streams_raw", "valor": "perfil de elevación — VAM por bloque"},
    {"field": "stream_moving", "donde": "strava_streams_raw", "valor": "pausas reales — endurance ajustada"},
    {"field": "FIT laps", "donde": "decode_fit (solo respuesta API)", "valor": "intervalos de uploads Garmin — no persisten a session_laps"},
]


@router.get("/gpt/data-coverage")
def data_coverage():
    """Qué data tiene Epoch, por sesión y por campo. Read-only."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
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
            FROM clean_sessions
        """)
        t = cur.fetchone()
        cur.execute("""
            SELECT source, COUNT(*) FROM clean_sessions GROUP BY source ORDER BY 2 DESC
        """)
        by_source = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(DISTINCT clean_session_id), COUNT(*) FROM session_laps WHERE lap_index >= 0")
        laps = cur.fetchone()
        cur.execute("""
            SELECT COUNT(DISTINCT sl.clean_session_id)
            FROM session_laps sl JOIN clean_sessions cs USING (clean_session_id)
            WHERE cs.start_date > CURRENT_DATE - 28 AND sl.lap_index >= 0
        """)
        laps_4w = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM clean_sessions WHERE start_date > CURRENT_DATE - 28")
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
        "nota": ("Nada está perdido de forma permanente: lo no llevado sigue en staging "
                 "(Supabase). 'streams' completos existen para ~todas las actividades y "
                 "hoy ningún endpoint los consume."),
    }
