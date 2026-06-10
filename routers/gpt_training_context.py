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

    # Degradación: último bloque work vs primero (velocidad a FC comparable)
    degradation = None
    if len(work) >= 2:
        f, l_ = work[0], work[-1]
        if f["avg_speed_kmh"] and l_["avg_speed_kmh"]:
            degradation = {
                "first_work": {"speed_kmh": f["avg_speed_kmh"], "hr": f["avg_hr_bpm"], "cadence": f["avg_cadence"]},
                "last_work": {"speed_kmh": l_["avg_speed_kmh"], "hr": l_["avg_hr_bpm"], "cadence": l_["avg_cadence"]},
                "speed_drop_pct": round((f["avg_speed_kmh"] - l_["avg_speed_kmh"]) / f["avg_speed_kmh"] * 100, 1),
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

    # Confianza: cuántos laps tienen FC + cuántos laps hay
    laps_with_hr = sum(1 for l in laps if l["avg_hr_bpm"])
    confidence = round(min(1.0, (laps_with_hr / n) * (0.5 + min(0.5, n / 10))), 2)
    if n == 1:
        warnings.append("Solo 1 lap — sin estructura intra-sesión; análisis limitado al promedio.")
        confidence = min(confidence, 0.3)
    if laps_with_hr < n:
        warnings.append(f"{n - laps_with_hr} laps sin FC — clasificación parcial.")
    if not structured and n >= 4:
        warnings.append("Varios laps pero sin patrón work/recovery claro — posible sesión libre con auto-laps.")

    # Capacidad construida
    cap_map = {"intervals": "Capacidad de repetir esfuerzos sobre umbral (VO2/umbral)",
               "tempo": "Resistencia al umbral (Z3 sostenido)",
               "high_intensity": "Potencia y tolerancia al lactato",
               "climb": "Rendimiento en subidas (fuerza-resistencia)",
               "endurance": "Motor aeróbico (base Z2)",
               "recovery": "Recuperación activa — asimilación de carga"}
    capacity_built = cap_map.get(workout_type, "Base aeróbica")

    # explanation_text estilo Epoch: observación → interpretación → sugerencia
    obs = (f"Sesión de {round(total_time/60)} min con {n} bloques: {len(work)} de trabajo, "
           f"{len(rec)} de recuperación, {len(steady)} estables. Zona dominante: {dominant_zone or '—'}.")
    interp = f"Estructura {'clara de intervalos' if structured else 'libre/continua'}. Construyó: {capacity_built.lower()}."
    if degradation and degradation["speed_drop_pct"] > 8:
        interp += f" El último bloque cayó {degradation['speed_drop_pct']}% en velocidad vs el primero — fatiga visible al final."
    if hr_recovery and hr_recovery["reads_well"]:
        interp += f" La FC bajó en promedio {hr_recovery['avg_hr_drop_bpm']} bpm entre intervalos — buena recuperación."
    sug = ("Sin objetivo programado para comparar — registra el plan para evaluar cumplimiento."
           if not planned else f"Comparar contra lo planificado: {planned[0] or ''} {planned[1] or ''}".strip())

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
        "degradation": degradation,
        "hr_recovery": hr_recovery,
        "capacity_built": capacity_built,
        "planned_target": ({"session_type": planned[0], "description": planned[1], "target": planned[2]}
                           if planned else "sin objetivo programado"),
        "confidence_score": confidence,
        "warnings": warnings,
        "explanation_text": f"{obs} {interp} {sug}",
    }
