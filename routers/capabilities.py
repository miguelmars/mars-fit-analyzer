"""
routers/capabilities.py — Endpoints de capacidades, readiness y rendimiento
============================================================================
TD-010A: extraídos de main.py.
Incluye:
  GET /gpt/capacidades
  GET /gpt/capacidad/{nombre}/history
  GET /gpt/readiness
  GET /gpt/readiness/eventos
  GET /gpt/patron-historico
  GET /gpt/academia/{key}
  GET /gpt/lt-detect
  GET /gpt/capacidad/{nombre}/validation
  GET /gpt/capacidad/{nombre}
  GET /gpt/baseline-compare
  GET /gpt/performance-profile
"""
import json
import logging

from fastapi import APIRouter, HTTPException

from db import get_db
from shared.helpers import (
    _normalize_capability_name,
    _find_capability,
    _previous_capability_run,
)

logger = logging.getLogger("mars_fit")

router = APIRouter(tags=["capabilities"])


# ── GET /gpt/capacidades ──────────────────────────────────────────────────────

@router.get("/gpt/capacidades")
def gpt_capacidades():
    """Seis capacidades personales con score, confianza, anclas y limitantes."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    try:
        from capability_engine import calculate_capabilities
        return calculate_capabilities(conn)
    except Exception as e:
        logger.error(f"Capability engine error: {e}")
        raise HTTPException(500, str(e))


# ── GET /gpt/capacidad/{nombre}/history ──────────────────────────────────────

@router.get("/gpt/capacidad/{nombre}/history")
def gpt_capability_history(nombre: str):
    """Annual capability history built from sustainable 12-week blocks."""
    normalized = _normalize_capability_name(nombre)
    _HISTORY_SUPPORTED = {"motor_aerobico", "composicion_corporal", "escalada"}
    if normalized not in _HISTORY_SUPPORTED:
        raise HTTPException(
            501,
            "This capacity does not have an annual history implemented yet.",
        )
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    try:
        if normalized == "composicion_corporal":
            from capability_engine import calculate_body_composition_history
            return calculate_body_composition_history(conn)
        if normalized == "escalada":
            from capability_engine import calculate_climbing_history
            return calculate_climbing_history(conn)
        from capability_engine import calculate_aerobic_history
        return calculate_aerobic_history(conn)
    except Exception as e:
        logger.error(f"Capability history error: {e}")
        raise HTTPException(500, str(e))


# ── GET /gpt/readiness ────────────────────────────────────────────────────────

@router.get("/gpt/readiness")
def gpt_readiness(evento: str = "escalera_al_infierno"):
    """Readiness score for a specific event based on current capability scores."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    try:
        from capability_engine import calculate_readiness
        return calculate_readiness(conn, evento)
    except Exception as e:
        logger.error(f"Readiness error: {e}")
        raise HTTPException(500, str(e))


# ── GET /gpt/readiness/eventos ────────────────────────────────────────────────

@router.get("/gpt/readiness/eventos")
def gpt_readiness_eventos():
    """List all supported readiness events."""
    from capability_engine import READINESS_EVENTS
    return {
        "eventos": [
            {"key": k, "nombre": v["nombre"], "tipo": v["tipo"], "descripcion": v["descripcion"]}
            for k, v in READINESS_EVENTS.items()
        ]
    }


# ── GET /gpt/patron-historico ─────────────────────────────────────────────────

@router.get("/gpt/patron-historico")
def gpt_patron_historico(top_n: int = 5):
    """Find historical periods most similar to current fitness state and classify their trajectories."""
    if top_n < 1 or top_n > 10:
        raise HTTPException(400, "top_n must be between 1 and 10")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    from capability_engine import calculate_historical_similarity
    return calculate_historical_similarity(conn)


# ── GET /gpt/academia/{key} ───────────────────────────────────────────────────

@router.get("/gpt/academia/{key}")
def gpt_academia(key: str):
    """Return educational content for a capability key, glossary term, or 'glosario' for all terms."""
    from academia import academia
    return academia(key)


# ── GET /gpt/capability-levels (V10.4) ───────────────────────────────────────

_NEXT_STEP = {
    "efficiency": "More honest Z2 time (134-150 bpm) — speed per heartbeat is what raises this.",
    "consistency": "At least one aerobic session every week — gaps reset adaptation.",
    "z2": "Keep intensity honest: more time inside 134-150 bpm, less drifting into Z3.",
    "long_endurance": "One longer ride — past your recent long-ride reference.",
    "weight_trend": "Hold the gentle downward trend: moderate deficit, enough protein.",
    "personal_range": "Keep weekly weigh-ins; the range moves slowly.",
    "measurement_adherence": "Weigh in once a week — without data there is no signal.",
    "weight_level": "The period minimum follows the trend — consistency over crash diets.",
    "resting_hr": "Log resting HR each morning — it takes 20 seconds.",
    "wellness": "Log fatigue and sleep 3+ times a week.",
    "rest_days": "Protect rest days — the muscle grows there.",
    "weekly_ascent": "Add climbing meters: one route with sustained gain this week.",
    "climbing_sessions": "One session with more than 500 m of gain.",
    "best_ascent": "A bigger single climb day than your recent best.",
    "sessions": "Two strength sessions per week, consistent.",
    "progression": "Log the load you lift so progression becomes visible.",
    "logged_uses": "Log each gel or fuel use in sessions over 1h.",
    "timing": "Note when you fuel (before/during/after).",
    "gi_tolerance": "Note how your gut responded — race day should hold no surprises.",
}


@router.get("/gpt/capability-levels")
def gpt_capability_levels():
    """Niveles 1-10 por capacidad + qué desbloquea el siguiente nivel.
    El siguiente paso sale del indicador con más terreno por ganar (gap × peso) —
    siempre una acción concreta, nunca un sermón."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    from capability_engine import calculate_capabilities
    data = calculate_capabilities(conn)
    levels = []
    for cap in data.get("capabilities", []):
        score = cap.get("score")
        if score is None:
            levels.append({"key": cap["key"], "nombre": cap.get("nombre"),
                           "level": None, "score": None,
                           "status": "to_calibrate",
                           "next_step": cap.get("recomendacion") or
                                        "Start logging — without data there is no level.",
                           "confidence": cap.get("confidence")})
            continue
        level = max(1, min(10, int(score // 10) + (1 if score % 10 else 0) or 1))
        nxt_threshold = min(100, level * 10)
        inds = [i for i in (cap.get("indicators") or []) if i.get("score") is not None]
        weakest = max(inds, key=lambda i: (100 - i["score"]) * (i.get("weight") or 0.1),
                      default=None)
        step = (_NEXT_STEP.get((weakest or {}).get("key"),
                               cap.get("recomendacion") or "Keep the consistency.")
                if weakest else cap.get("recomendacion") or "Keep the consistency.")
        levels.append({
            "key": cap["key"], "nombre": cap.get("nombre"),
            "level": level, "score": score,
            "points_to_next": round(max(0, nxt_threshold - score), 1) if level < 10 else 0,
            "status": cap.get("status"),
            "confidence": cap.get("confidence"),
            "weakest_indicator": (weakest or {}).get("label"),
            "next_step": step,
        })
    return {"ok": True, "levels": levels,
            "nota": ("Levels are your score in steps of 10 — personal history only. "
                     "The next step targets the indicator with the most ground to gain.")}


# ── GET /gpt/lt-detect ────────────────────────────────────────────────────────

@router.get("/gpt/lt-detect")
def gpt_lt_detect(sport: str = "cycling"):
    """Estimate lactate threshold from historical session data (diagnostic, read-only)."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport must be 'cycling' or 'running'")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    try:
        from capability_engine import lt_detect_from_records
        return lt_detect_from_records(conn, sport=sport)
    except Exception as e:
        logger.error(f"LT detect error: {e}")
        raise HTTPException(500, str(e))


# ── GET /gpt/capacidad/{nombre}/validation ────────────────────────────────────

@router.get("/gpt/capacidad/{nombre}/validation")
def gpt_capability_validation(nombre: str):
    """Audit arithmetic, anchors and indicator changes without writing data."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    try:
        from capability_engine import calculate_capabilities, validate_capability

        result = calculate_capabilities(conn)
        capability = _find_capability(result, nombre)
        previous = _previous_capability_run(conn, capability["key"])
        return {
            "ok": True,
            "capability": capability,
            "validation": validate_capability(capability, previous),
            "recorded": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Capability validation error: {e}")
        raise HTTPException(500, str(e))


# ── GET /gpt/capacidad/{nombre} ───────────────────────────────────────────────

@router.get("/gpt/capacidad/{nombre}")
def gpt_capacidad(nombre: str):
    """Detalle de una capacidad por key estable."""
    result = gpt_capacidades()
    capability = _find_capability(result, nombre)
    return {
        "ok": True,
        "version": result["version"],
        "generated_from": result["generated_from"],
        "rules": result["rules"],
        "capability": capability,
    }


# ── GET /gpt/baseline-compare ─────────────────────────────────────────────────

@router.get("/gpt/baseline-compare")
def gpt_baseline_compare(sport: str = "cycling"):
    """Compara métricas actuales vs línea base mayo 2026 con señal aeróbica clave."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
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
                FROM sessions_clean_compat
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
                FROM sessions_clean_compat
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
            senal = "✅ Aerobic engine improving — %.1f km/h at %.0f bpm" % (vel, fc)
            estado = "mejorando"
        elif vel >= 19 and fc < 145:
            senal = "🔄 In progress — %.1f km/h at %.0f bpm" % (vel, fc)
            estado = "en_progreso"
        elif actual["sesiones_4_semanas"] < 2:
            senal = "⚠️ Insufficient data (fewer than 2 sessions in 4 weeks)"
            estado = "datos_insuficientes"
        else:
            senal = "📊 Keep stacking Z2 — %.1f km/h at %.0f bpm" % (vel, fc)
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


# ── GET /gpt/performance-profile ─────────────────────────────────────────────

@router.get("/gpt/performance-profile")
def gpt_performance_profile(sport: str = "cycling"):
    """Perfil de rendimiento completo — VO2Max, carga, desacople, eficiencia aeróbica, cadencia, ranking."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
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
                    FROM sessions_clean_compat WHERE sport=%s AND {col} IS NOT NULL
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
                ("mejor_eficiencia", "avg_speed_kmh/NULLIF(avg_hr_bpm,0)", "DESC", "Best HR/speed efficiency"),
                ("mayor_distancia", "distance_km", "DESC", "Longest distance"),
                ("mayor_ascenso", "ascent_m", "DESC", "Most climbing"),
                ("mayor_velocidad", "avg_speed_kmh", "DESC", "Highest speed"),
            ]:
                cur.execute(f"""
                    SELECT session_id, start_time, workout_name, distance_km,
                           avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence
                    FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
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
                        if hasattr(v, "isoformat"):
                            s[k] = v.isoformat()
                    if s.get("avg_hr_bpm") and s.get("avg_speed_kmh"):
                        s["eficiencia_ratio"] = round(float(s["avg_speed_kmh"]) / float(s["avg_hr_bpm"]), 4)

        # 3. VO2Max estimado
        vo2max_est = None
        with conn.cursor() as cur:
            cur.execute("""
                SELECT avg_speed_kmh, avg_hr_bpm FROM sessions_clean_compat
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
                SELECT session_id, start_time, result_json FROM sessions_clean_compat
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
                FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
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
                FROM sessions_clean_compat
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
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW()-INTERVAL '8 weeks'
                GROUP BY semana ORDER BY semana ASC
            """, (sport,))
            rows = cur.fetchall()
            cols4 = [d[0] for d in cur.description]
        weekly_load = [dict(zip(cols4, r)) for r in rows]

        # FC/vel ratio tendencia
        fc_vel_trend = "no data"
        if len(weekly_load) >= 2:
            f = weekly_load[0]
            l = weekly_load[-1]
            fc_f = float(f.get("fc") or 0)
            vel_f = float(f.get("vel") or 0)
            fc_l = float(l.get("fc") or 0)
            vel_l = float(l.get("vel") or 0)
            if fc_f and vel_f and fc_l and vel_l:
                r_f = vel_f / fc_f
                r_l = vel_l / fc_l
                delta = round((r_l - r_f) / r_f * 100, 1)
                fc_vel_trend = f"+{delta}%" if delta >= 0 else f"{delta}%"

        # 8. Evolución cadencia mensual
        cad_trend = "no data"
        cad_months = [(m["mes"], float(m.get("cad") or 0)) for m in monthly_eff if m.get("cad")]
        if len(cad_months) >= 2:
            delta_cad = round(cad_months[-1][1] - cad_months[0][1], 1)
            cad_trend = f"+{delta_cad} rpm" if delta_cad >= 0 else f"{delta_cad} rpm"

        return {
            "sport": sport,
            "vo2max_estimado": vo2max_est,
            "vo2max_nota": "Firstbeat estimate (speed/HR). Does not replace a real test.",
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
