"""
routers/gpt_analytics.py — Analytics & GPT endpoints
=====================================================
TD-010A: extraído de main.py
Endpoints: /gpt/month-summary, /gpt/efficiency-trend, /gpt/zones-summary,
           /gpt/cadence-trend, /gpt/weekly-report, /gpt/adaptive-coach,
           /gpt/fueling-log, /gpt/gel-tests, /gpt/weight-trend,
           /gpt/tests, /gpt/dashboard, /gpt/historical-progress,
           /gpt/month-compare, /gpt/fitness-timeline, /gpt/athletic-history,
           /gpt/calendar-heatmap, /gpt/trends, /gpt/rebuild-snapshots,
           /gpt/environment-summary, /gpt/athletic-status,
           /gpt/correlaciones, /gpt/correlations, /gpt/tendencia,
           /gpt/mars-context
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from db import (
    get_db,
    _ensure_weight_table,
    _ensure_session_environment_table,
    _ensure_goals_table,
)
from shared.models import AthleteTestIn
from mars_context import _get_profile

logger = logging.getLogger("mars_fit")
router = APIRouter(tags=["gpt_analytics"])


# ── GET /gpt/month-summary ────────────────────────────────────────────────────

@router.get("/gpt/month-summary")
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
                FROM sessions_clean_compat
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
                FROM sessions_clean_compat
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


# ── GET /gpt/efficiency-trend ─────────────────────────────────────────────────

@router.get("/gpt/efficiency-trend")
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
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                GROUP BY semana ORDER BY semana ASC
            """, (sport, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]
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

@router.get("/gpt/zones-summary")
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
                    SELECT result_json FROM sessions_clean_compat
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
                               "nota": "Optimal Z2 for an aerobic base: 70-80% of total time"}
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/cadence-trend ────────────────────────────────────────────────────

@router.get("/gpt/cadence-trend")
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
                FROM sessions_clean_compat
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
                JOIN sessions_clean_compat s ON sr.session_id IN (s.session_id, REPLACE(s.session_id, 'session:', ''))
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


# ── GET /gpt/weekly-report ────────────────────────────────────────────────────

@router.get("/gpt/weekly-report")
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
                    FROM sessions_clean_compat
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


# ── GET /gpt/adaptive-coach ───────────────────────────────────────────────────

@router.get("/gpt/adaptive-coach")
def adaptive_coach():
    """
    E29 — Adaptive Coaching Engine.
    Combina meta activa (E26B) + capacidades (CE) + carga reciente → plan semanal.
    """
    from datetime import date as _date
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")

    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, event_name, event_type, event_date,
                   distance_km, elevation_m, priority, notes
            FROM mars_goals
            WHERE status = 'active'
            ORDER BY priority ASC LIMIT 1
        """)
        gr = cur.fetchone()

    goal = None
    weeks_to_event = None
    phase = "base"

    if gr:
        goal = {
            "id": gr[0], "event_name": gr[1], "event_type": gr[2],
            "event_date": gr[3].isoformat() if gr[3] else None,
            "distance_km": gr[4], "elevation_m": gr[5], "notes": gr[7],
        }
        if gr[3]:
            days = (gr[3] - _date.today()).days
            weeks_to_event = max(0, days // 7)
            if weeks_to_event > 16:   phase = "base"
            elif weeks_to_event > 8:  phase = "build"
            elif weeks_to_event > 3:  phase = "peak"
            else:                     phase = "taper"

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                ROUND(SUM(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN COALESCE(distance_km,0) ELSE 0 END)::numeric/4, 1) as km_week,
                COUNT(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks' THEN 1 END)/4.0 as sess_week,
                ROUND(AVG(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN avg_hr_bpm END)::numeric, 0) as avg_hr,
                ROUND(AVG(CASE WHEN start_time::timestamp > NOW()-INTERVAL '4 weeks'
                          THEN avg_speed_kmh END)::numeric, 1) as avg_spd
            FROM sessions_clean_compat
            WHERE sport = 'cycling' AND start_time IS NOT NULL
        """)
        sr = cur.fetchone()

    km_week   = float(sr[0] or 0)
    sess_week = round(float(sr[1] or 0), 1)
    avg_hr    = float(sr[2] or 0)
    avg_spd   = float(sr[3] or 0)
    eff       = round(avg_spd / avg_hr * 100, 4) if avg_hr > 0 else 0

    _PHASES = {
        "base":  {"label": "Base — aerobic Z2 volume",
                  "km_mult": 1.10, "sess": 4, "z2_pct": 80,
                  "intensidad": "80% Z2 · 20% Z1",
                  "foco": "Aerobic volume, cadence 95-100 rpm, long rides"},
        "build": {"label": "Build — intensity on top of the base",
                  "km_mult": 1.05, "sess": 5, "z2_pct": 70,
                  "intensidad": "70% Z2 · 20% Z3 · 10% Z4",
                  "foco": "Tempo intervals, strength on climbs, 2h+ sessions"},
        "peak":  {"label": "Peak — sharpening form pre-event",
                  "km_mult": 0.85, "sess": 4, "z2_pct": 75,
                  "intensidad": "75% Z2 · 15% Z3 · 10% Z4-Z5",
                  "foco": "Simulate event pace, quality over volume"},
        "taper": {"label": "Taper — rest and activation",
                  "km_mult": 0.60, "sess": 3, "z2_pct": 80,
                  "intensidad": "80% Z1-Z2 · short activations",
                  "foco": "Fresh legs, mobility, hydration and nutrition"},
    }
    ph = _PHASES[phase]
    prescription = {
        "fase":       phase,
        "fase_label": ph["label"],
        "km_objetivo":    round(km_week * ph["km_mult"], 0) if km_week > 0 else None,
        "sesiones":       ph["sess"],
        "z2_pct_objetivo": ph["z2_pct"],
        "intensidad":     ph["intensidad"],
        "foco_semana":    ph["foco"],
    }

    limiting = None
    limiting_score = None
    try:
        from capability_engine import calculate_capabilities
        caps_data = calculate_capabilities(conn)
        caps_raw = caps_data.get("capabilities", [])
        caps = {c["key"]: c for c in caps_raw} if isinstance(caps_raw, list) else caps_raw
        goal_type = (goal or {}).get("event_type", "cycling")
        relevance = {
            "cycling": ["motor_aerobico", "composicion_corporal", "escalada"],
            "gravel":  ["motor_aerobico", "escalada", "fuerza"],
            "running": ["motor_aerobico", "composicion_corporal", "recuperacion"],
            "climbing":["escalada", "fuerza", "motor_aerobico"],
        }.get(goal_type, ["motor_aerobico", "composicion_corporal"])

        best_gap, best_cap = 0, None
        for cname in relevance:
            cap = caps.get(cname, {})
            score = cap.get("score")
            if score is None:
                continue
            gap = 100 - float(score)
            if gap > best_gap:
                best_gap, best_cap = gap, cname
        if best_cap:
            limiting = best_cap
            limiting_score = round(float(caps.get(best_cap, {}).get("score") or 0), 1)
    except Exception as _cap_err:
        logger.warning(f"E29 capability error: {_cap_err}", exc_info=True)
        limiting = f"error:{type(_cap_err).__name__}"

    if goal and weeks_to_event is not None:
        mensaje = (f"{goal['event_name']} in {weeks_to_event} weeks — {phase} phase. "
                   f"Target: {prescription['km_objetivo']} km/week, "
                   f"{ph['z2_pct']}% Z2.")
    elif goal:
        mensaje = f"Active goal: {goal['event_name']}. No date — base phase by default."
    else:
        mensaje = "No active goals. Add one in the Goals section for personalized coaching."

    return {
        "goal":             goal,
        "weeks_to_event":   weeks_to_event,
        "phase":            phase,
        "prescription":     prescription,
        "training_status":  {"km_semana": km_week, "sesiones_semana": sess_week,
                             "eficiencia": eff},
        "limiting_capability": limiting,
        "limiting_score":   limiting_score,
        "mensaje":          mensaje,
    }


# ── GET /gpt/fueling-log ──────────────────────────────────────────────────────

@router.get("/gpt/fueling-log")
def gpt_fueling_log(limit: int = 20):
    """Historial de nutrición post-sesión — geles, barras, agua, cafeína, CHO/hora."""
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
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
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

@router.get("/gpt/gel-tests")
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
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
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
                "gi": gi, "energia": en, "rpe": t.get("rpe")
            })
        resumen = []
        for gt, data in by_type.items():
            uses = data["uses"]
            gi_score = round(data["gi_ok"] / uses * 100) if uses else 0
            en_score = round(data["energy_good"] / uses * 100) if uses else 0
            score = round((gi_score + en_score) / 2)
            resumen.append({
                "gel_type": gt, "usos": uses, "gi_ok_pct": gi_score,
                "energia_ok_pct": en_score, "score": score,
                "recomendacion": "✅ Seguir usando" if score >= 70 else ("⚠️ Revisar" if score >= 40 else "❌ Evitar"),
                "historial": data["sessions"]
            })
        resumen.sort(key=lambda x: x["score"], reverse=True)
        return {"total_pruebas": len(tests), "tipos_probados": len(by_type), "ranking": resumen}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/weight-trend ─────────────────────────────────────────────────────

@router.get("/gpt/weight-trend")
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
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
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
        entries = list(reversed(entries))
        tendencia = "sin datos"
        delta_kg = None
        if len(entries) >= 2:
            w_first = float(entries[0].get("weight_before") or 0)
            w_last  = float(entries[-1].get("weight_before") or 0)
            if w_first and w_last:
                delta_kg = round(w_last - w_first, 2)
                tendencia = f"{'+' if delta_kg > 0 else ''}{delta_kg} kg since first measurement"
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ROUND(AVG(ps.weight_before)::numeric, 2)
                FROM post_session ps
                JOIN sessions_clean_compat s ON ps.session_id = s.session_id
                WHERE ps.weight_before IS NOT NULL
                  AND s.start_time::timestamp >= NOW() - INTERVAL '4 weeks'
            """)
            avg_row = cur.fetchone()
        return {
            "registros": len(entries), "tendencia": tendencia,
            "delta_kg_total": delta_kg,
            "peso_promedio_4_semanas": float(avg_row[0]) if avg_row and avg_row[0] else None,
            "historial": entries
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST/GET /gpt/tests ───────────────────────────────────────────────────────

@router.post("/gpt/tests")
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


@router.get("/gpt/tests")
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
        by_type = {}
        for t in reversed(tests):
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

@router.get("/gpt/dashboard")
def gpt_dashboard(sport: str = "cycling"):
    """Dashboard completo — una sola llamada para el estado actual de Mars."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        result = {}
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
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '7 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric, 1) as atl_7d,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '42 days'
                              THEN COALESCE(duration_s,0)/3600.0 * COALESCE(avg_hr_bpm,130)/130.0 ELSE 0 END)::numeric / 6.0, 1) as ctl_42d,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '1 week' THEN 1 END) as ses_7d,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks' THEN 1 END) as ses_2w
                FROM sessions_clean_compat WHERE sport=%s AND start_time IS NOT NULL
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
        fitness = "steady"
        if km_4w > 0:
            p = (km_2w - km_4w) / km_4w * 100
            fitness = "rising" if p > 10 else ("declining" if p < -15 else "steady")
        fatiga = "low"
        if fc_base > 0 and spd_base > 0:
            er = spd_rec / fc_rec if fc_rec else 0
            eb = spd_base / fc_base
            if eb > 0:
                ep = (er - eb) / eb * 100
                fatiga = "high" if ep < -5 else ("moderate" if ep < -2 else "low")
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
        mars_index = None
        if eff_rec:
            mars_index = round(eff_rec * (1 + int(d.get("ses_2w") or 0) * 0.02), 2)
        atl = float(d.get("atl_7d") or 0)
        ctl = float(d.get("ctl_42d") or 0)
        tsb = round(ctl - atl, 1)
        carga_estado = "fresh" if tsb > 5 else ("recover" if tsb < -15 else "loading")
        result["athlete"] = {
            "fitness": fitness, "fatiga": fatiga,
            "eficiencia": eff_delta, "cadencia": cad_delta,
            "km_2_semanas": km_2w,
            "horas_2_semanas": float(d.get("hrs_2w") or 0),
            "sesiones_7_dias": int(d.get("ses_7d") or 0),
            "mars_index": mars_index
        }
        result["carga"] = {
            "atl": atl, "ctl": ctl, "tsb": tsb, "estado": carga_estado,
            "nota": "Estimated from duration and HR; it gets sharper once we have power data."
        }
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ps.weight_before, s.start_time
                FROM post_session ps JOIN sessions_clean_compat s ON ps.session_id=s.session_id
                WHERE ps.weight_before IS NOT NULL
                ORDER BY s.start_time DESC LIMIT 1
            """)
            w_row = cur.fetchone()
        result["peso_reciente"] = {
            "kg": float(w_row[0]) if w_row else None,
            "fecha": w_row[1].isoformat() if w_row and hasattr(w_row[1], "isoformat") else None
        }
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) as ses,
                       ROUND(SUM(distance_km)::numeric,1) as km,
                       ROUND(SUM(COALESCE(duration_s,0))::numeric/3600,2) as hrs,
                       ROUND(SUM(ascent_m)::numeric,0) as asc_m
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL
                  AND start_time::timestamp >= DATE_TRUNC('week', NOW())
            """, (sport,))
            wk = cur.fetchone()
        weekly_calories = 0
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(calories),0)
                    FROM clean_sessions
                    WHERE sport=%s AND start_time IS NOT NULL
                      AND start_time >= DATE_TRUNC('week', NOW())
                """, (sport,))
                weekly_calories = int(cur.fetchone()[0] or 0)
        except Exception:
            weekly_calories = 0
        result["semana_actual"] = {
            "sesiones": int(wk[0] or 0), "km": float(wk[1] or 0),
            "horas": float(wk[2] or 0), "ascenso_m": int(wk[3] or 0),
            "calorias": weekly_calories
        }
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE calories IS NOT NULL) AS with_calories,
                        COALESCE(SUM(calories),0) AS total_calories,
                        ROUND(AVG(CASE WHEN distance_km > 0 AND calories IS NOT NULL THEN calories / distance_km END)::numeric,1) AS kcal_per_km,
                        MAX(calories) AS max_session_calories
                    FROM clean_sessions
                    WHERE sport=%s AND start_time IS NOT NULL
                """, (sport,))
                cal_row = cur.fetchone()
            kcal_per_km = float(cal_row[2] or 0)
            result["calorias_audit"] = {
                "sesiones_con_calorias": int(cal_row[0] or 0),
                "calorias_historicas": int(cal_row[1] or 0),
                "kcal_por_km_promedio": kcal_per_km,
                "max_calorias_sesion": int(cal_row[3] or 0),
                "validacion": "ok" if kcal_per_km >= 12 else "revisar_posible_division_por_10",
                "nota": "En ciclismo, un promedio muy bajo de kcal/km seria senal de calorias divididas por 10."
            }
        except Exception:
            result["calorias_audit"] = {"validacion": "no_disponible"}
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
                "componente": gear_row[0], "pct_usado": pct,
                "km_restantes": max(0, gear_row[1] - km_used),
                "status": "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            }
        else:
            result["proximo_mantenimiento"] = None
        with conn.cursor() as cur:
            cur.execute("""
                SELECT result_json FROM sessions_clean_compat
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
            "nota": "Optimal: 70-80%"
        }
        rec = "continue Z2"
        if fatiga == "high":
            rec = "reduce intensity — active recovery session"
        elif fatiga == "moderate":
            rec = "hold Z2, avoid Z4-Z5"
        elif fitness == "rising" and fatiga == "low":
            rec = "good form — you can add a Z3 tempo session"
        elif result["semana_actual"]["sesiones"] == 0:
            rec = "no sessions this week — resume training"
        result["recommendation"] = rec
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/historical-progress ─────────────────────────────────────────────

@router.get("/gpt/historical-progress")
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
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 month')
                GROUP BY mes ORDER BY mes ASC
            """, (sport, months))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]
        baseline = next((m for m in data if (m.get("sesiones") or 0) >= 3), None)
        baseline_month = baseline["mes"] if baseline else None
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

@router.get("/gpt/month-compare")
def gpt_month_compare(sport: str = "cycling", month_a: str = None, month_b: str = None):
    """Compara dos meses específicos en todas las métricas."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    today = date.today()
    if not month_a:
        month_a = "2026-05"
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
                    FROM sessions_clean_compat
                    WHERE sport=%s AND LEFT(start_time,7)=%s AND start_time IS NOT NULL
                """, (sport, month_str))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        stats_a = get_month_stats(month_a)
        stats_b = get_month_stats(month_b)
        deltas = {}
        for k in ("fc_promedio", "vel_promedio", "cadencia_promedio", "km_total", "horas_total", "eficiencia_ratio"):
            va = float(stats_a.get(k) or 0)
            vb = float(stats_b.get(k) or 0)
            if va:
                deltas[k + "_delta"] = round(vb - va, 2)
                deltas[k + "_pct"] = round((vb - va) / va * 100, 1)
        aerobic_signal = None
        if stats_b.get("vel_promedio") and stats_b.get("fc_promedio"):
            v = float(stats_b["vel_promedio"])
            fc = float(stats_b["fc_promedio"])
            if 20 <= v <= 22 and fc < 140:
                aerobic_signal = "✅ Aerobic engine improving: %.1f km/h at %.0f bpm" % (v, fc)
            elif v >= 20:
                aerobic_signal = "⚠️ %.1f km/h but HR still at %.0f bpm — stay in Z2" % (v, fc)
        return {
            "sport": sport, "mes_base": month_a, "mes_actual": month_b,
            "stats_base": stats_a, "stats_actual": stats_b,
            "deltas": deltas, "senal_aerobica": aerobic_signal
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/fitness-timeline ─────────────────────────────────────────────────

@router.get("/gpt/fitness-timeline")
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
                FROM sessions_clean_compat
                WHERE sport=%s AND start_time IS NOT NULL AND avg_hr_bpm > 0
                GROUP BY mes ORDER BY mes ASC
            """, (sport,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, r)) for r in rows]
        ratios = [float(m["eff_ratio"]) for m in data if m.get("eff_ratio")]
        if ratios:
            min_r, max_r = min(ratios), max(ratios)
            rng = max_r - min_r or 1
            for m in data:
                if m.get("eff_ratio"):
                    m["fitness_score"] = round((float(m["eff_ratio"]) - min_r) / rng * 100, 1)
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


# ── GET /gpt/athletic-history ─────────────────────────────────────────────────

@router.get("/gpt/athletic-history")
def gpt_athletic_history():
    """Historia atlética completa desde clean_sessions: todos los deportes vivos."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS sesiones,
                    COUNT(*) FILTER (WHERE avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0) AS con_fc,
                    COUNT(*) FILTER (WHERE distance_km IS NOT NULL AND distance_km > 0) AS con_distancia,
                    COUNT(*) FILTER (WHERE calories IS NOT NULL AND calories > 0) AS con_calorias,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(SUM(COALESCE(calories,0))::numeric, 0) AS calorias,
                    MIN(start_time) AS desde,
                    MAX(start_time) AS hasta
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
            """)
            totals = dict(zip([d[0] for d in cur.description], cur.fetchone()))
            cur.execute("""
                SELECT
                    CASE
                        WHEN sport IN ('running','trail_running','treadmill_running') THEN 'running'
                        WHEN sport IN ('cycling','indoor_cycling') THEN 'cycling'
                        WHEN sport IN ('indoor_cardio') THEN 'indoor_cardio'
                        WHEN sport IN ('strength_training') THEN 'strength'
                        WHEN sport IN ('lap_swimming','open_water_swimming') THEN 'swimming'
                        WHEN sport IN ('walking') THEN 'walking'
                        WHEN sport IN ('yoga') THEN 'mobility'
                        WHEN sport IN ('multi_sport','bikeToRunTransition_v2') THEN 'multi_sport'
                        ELSE 'other'
                    END AS grupo,
                    COUNT(*) AS sesiones,
                    COUNT(*) FILTER (WHERE avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 0) AS con_fc,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(AVG(NULLIF(avg_hr_bpm,0))::numeric, 1) AS fc_promedio,
                    ROUND(AVG(NULLIF(avg_speed_kmh,0))::numeric, 2) AS velocidad_promedio,
                    ROUND((AVG(NULLIF(avg_speed_kmh,0))/NULLIF(AVG(NULLIF(avg_hr_bpm,0)),0))::numeric, 4) AS eficiencia,
                    MIN(start_time) AS desde,
                    MAX(start_time) AS hasta
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY grupo
                ORDER BY sesiones DESC
            """)
            by_group = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT
                    EXTRACT(YEAR FROM start_time::timestamp)::int AS year,
                    CASE
                        WHEN sport IN ('running','trail_running','treadmill_running') THEN 'running'
                        WHEN sport IN ('cycling','indoor_cycling') THEN 'cycling'
                        WHEN sport IN ('indoor_cardio') THEN 'indoor_cardio'
                        WHEN sport IN ('strength_training') THEN 'strength'
                        WHEN sport IN ('lap_swimming','open_water_swimming') THEN 'swimming'
                        WHEN sport IN ('walking') THEN 'walking'
                        WHEN sport IN ('yoga') THEN 'mobility'
                        WHEN sport IN ('multi_sport','bikeToRunTransition_v2') THEN 'multi_sport'
                        ELSE 'other'
                    END AS grupo,
                    COUNT(*) AS sesiones,
                    ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) AS km,
                    ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) AS horas,
                    ROUND(AVG(NULLIF(avg_hr_bpm,0))::numeric, 1) AS fc_promedio,
                    ROUND(AVG(NULLIF(avg_speed_kmh,0))::numeric, 2) AS velocidad_promedio,
                    ROUND((AVG(NULLIF(avg_speed_kmh,0))/NULLIF(AVG(NULLIF(avg_hr_bpm,0)),0))::numeric, 4) AS eficiencia
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY year, grupo
                ORDER BY year ASC, grupo ASC
            """)
            yearly = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT sport, COUNT(*) AS sesiones
                FROM sessions_clean_compat
                WHERE start_time IS NOT NULL
                GROUP BY sport
                ORDER BY sesiones DESC
            """)
            raw_sports = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

        def clean_value(v):
            if hasattr(v, "isoformat"):
                return v.isoformat()
            try:
                import decimal
                if isinstance(v, decimal.Decimal):
                    return float(v)
            except Exception:
                pass
            return v

        def clean_obj(obj):
            return {k: clean_value(v) for k, v in obj.items()}

        by_group = [clean_obj(x) for x in by_group]
        yearly = [clean_obj(x) for x in yearly]
        raw_sports = [clean_obj(x) for x in raw_sports]
        totals = clean_obj(totals)
        running = next((x for x in by_group if x.get("grupo") == "running"), {})
        cycling = next((x for x in by_group if x.get("grupo") == "cycling"), {})
        swimming = next((x for x in by_group if x.get("grupo") == "swimming"), {})
        strength = next((x for x in by_group if x.get("grupo") == "strength"), {})
        story = [
            "2018-2020: base amplia con running, caminatas, ciclismo, cardio indoor y primeras transiciones.",
            "2021-2022: swimming and mobility/yoga blocks appear, with less running volume.",
            "2023-2026: cycling and strength take more weight, with clean_sessions as the living master index.",
        ]
        return {
            "ok": True, "totals": totals, "by_group": by_group,
            "raw_sports": raw_sports, "yearly": yearly,
            "running": running, "cycling": cycling,
            "swimming": swimming, "strength": strength, "story": story,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/calendar-heatmap ─────────────────────────────────────────────────

@router.get("/gpt/calendar-heatmap")
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
            FROM sessions_clean_compat
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
            km = float(rd.get("km") or 0)
            if km == 0: intensity = 1
            elif km < 20: intensity = 1
            elif km < 40: intensity = 2
            elif km < 60: intensity = 3
            else: intensity = 4
            rd["intensity"] = intensity
            days.append(rd)
        total_km = sum(float(d.get("km") or 0) for d in days)
        total_h = sum(float(d.get("horas") or 0) for d in days)
        total_ses = sum(int(d.get("sesiones") or 0) for d in days)
        return {
            "year": year, "sport": sport or "all",
            "dias_activos": len(days), "total_km": round(total_km, 1),
            "total_horas": round(total_h, 1), "total_sesiones": total_ses,
            "days": days
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/trends ───────────────────────────────────────────────────────────

@router.get("/gpt/trends")
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
                FROM sessions_clean_compat WHERE sport='cycling'
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


# ── POST /gpt/rebuild-snapshots ───────────────────────────────────────────────

@router.post("/gpt/rebuild-snapshots")
def rebuild_snapshots():
    """
    1. Calcula efficiency_speed_hr en sesiones que tienen speed + HR pero no efficiency.
    2. Genera todos los snapshots semanales históricos desde clean_sessions.
    """
    conn = get_db()
    if not conn:
        return {"ok": False, "message": "DB no disponible"}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE clean_sessions
                SET efficiency_speed_hr = ROUND((avg_speed_kmh / avg_hr_bpm)::numeric, 5)
                WHERE avg_speed_kmh IS NOT NULL
                  AND avg_hr_bpm    IS NOT NULL
                  AND avg_hr_bpm    > 40
                  AND avg_speed_kmh > 0
                  AND efficiency_speed_hr IS NULL
            """)
            efficiency_updated = cur.rowcount
        conn.commit()
        logger.info(f"rebuild_snapshots: {efficiency_updated} sesiones con efficiency calculada")
        from tools.backfill_athlete_snapshots import build_snapshots, write_snapshots
        snapshots = build_snapshots(conn)
        if not snapshots:
            return {"ok": False, "message": "No sessions in clean_sessions yet", "count": 0}
        write_snapshots(conn, snapshots)
        return {
            "ok": True, "count": len(snapshots),
            "efficiency_updated": efficiency_updated,
            "first_week": str(snapshots[0]["week_start"]),
            "last_week":  str(snapshots[-1]["week_start"]),
        }
    except Exception as e:
        logger.error(f"rebuild_snapshots error: {e}", exc_info=True)
        return {"ok": False, "message": str(e)}


# ── GET /gpt/environment-summary ──────────────────────────────────────────────

@router.get("/gpt/environment-summary")
def gpt_environment_summary():
    """Altitude exposure and travel context from session_environment."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_session_environment_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS sessions,
                    COUNT(avg_altitude_m) AS with_altitude,
                    ROUND(AVG(habitual_altitude_m)::numeric, 0) AS habitual_altitude_m,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'above_habitual', 'well_above_habitual'
                    )) AS above_habitual,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'below_habitual', 'well_below_habitual'
                    )) AS below_habitual
                FROM session_environment
            """)
            sessions, with_altitude, habitual, above, below = cur.fetchone()
            cur.execute("""
                SELECT altitude_band, COUNT(*)
                FROM session_environment
                GROUP BY altitude_band
                ORDER BY COUNT(*) DESC
            """)
            bands = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("""
                SELECT country_code, region_label, COUNT(*)
                FROM session_environment
                WHERE country_code IS NOT NULL
                GROUP BY country_code, region_label
                ORDER BY COUNT(*) DESC
            """)
            countries = [
                {"code": row[0], "label": row[1], "sessions": row[2]}
                for row in cur.fetchall()
            ]
            cur.execute("""
                SELECT se.clean_session_id, cs.start_time, cs.name, cs.sport,
                       se.avg_altitude_m, se.max_altitude_m, se.ascent_m,
                       se.country_code, se.region_label, se.relative_altitude_band,
                       se.prior_21d_exposure_days, se.acclimatization_status,
                       se.altitude_confidence
                FROM session_environment se
                JOIN clean_sessions cs USING (clean_session_id)
                WHERE se.avg_altitude_m IS NOT NULL
                ORDER BY se.avg_altitude_m DESC
                LIMIT 12
            """)
            columns = [description[0] for description in cur.description]
            highest = [dict(zip(columns, row)) for row in cur.fetchall()]
        for item in highest:
            if item.get("start_time"):
                item["start_time"] = item["start_time"].isoformat()
            for key in ("avg_altitude_m", "max_altitude_m", "altitude_confidence"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
        return {
            "ok": True, "sessions": sessions, "with_altitude": with_altitude,
            "coverage_pct": round(with_altitude / sessions * 100, 1) if sessions else 0,
            "habitual_altitude_m": float(habitual) if habitual is not None else None,
            "above_habitual": above, "below_habitual": below,
            "altitude_bands": bands, "countries": countries,
            "highest_sessions": highest,
            "interpretation": {
                "absolute_altitude": "Elevation above sea level.",
                "ascent": "Accumulated climbing inside the session.",
                "acclimatization": "Comparable-altitude training days in the prior 21 days.",
                "performance_use": "Context for comparison; it does not automatically add fitness points.",
            },
        }
    except Exception as e:
        logger.error(f"Environment summary error: {e}")
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS MATEMÁTICOS — correlaciones, tendencia, athletic-status
# ═══════════════════════════════════════════════════════════════════════════════

def _pearson(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xvals = [x for x, _ in pairs]
    yvals = [y for _, y in pairs]
    mx = sum(xvals) / len(xvals)
    my = sum(yvals) / len(yvals)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = (
        sum((x - mx) ** 2 for x in xvals) *
        sum((y - my) ** 2 for y in yvals)
    ) ** 0.5
    return round(numerator / denominator, 4) if denominator else None


def _linear_regression(xs, ys):
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    denominator = sum((x - mx) ** 2 for x, _ in pairs)
    if not denominator:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / denominator
    return {"slope": slope, "intercept": my - slope * mx}


def _solve_3x3(matrix, vector):
    augmented = [list(map(float, row)) + [float(value)] for row, value in zip(matrix, vector)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][3] for row in range(3)]


def _multiple_regression_hr_speed_temperature(rows):
    if len(rows) < 12:
        return None
    n = float(len(rows))
    speeds = [float(row["speed"]) for row in rows]
    temperatures = [float(row["temperature"]) for row in rows]
    hrs = [float(row["hr"]) for row in rows]
    matrix = [
        [n, sum(speeds), sum(temperatures)],
        [sum(speeds), sum(x * x for x in speeds), sum(x * t for x, t in zip(speeds, temperatures))],
        [sum(temperatures), sum(x * t for x, t in zip(speeds, temperatures)), sum(t * t for t in temperatures)],
    ]
    vector = [
        sum(hrs),
        sum(x * y for x, y in zip(speeds, hrs)),
        sum(t * y for t, y in zip(temperatures, hrs)),
    ]
    coefficients = _solve_3x3(matrix, vector)
    if not coefficients:
        return None
    predicted = [
        coefficients[0] + coefficients[1] * speed + coefficients[2] * temperature
        for speed, temperature in zip(speeds, temperatures)
    ]
    mean_hr = sum(hrs) / len(hrs)
    total_variance = sum((hr - mean_hr) ** 2 for hr in hrs)
    residual_variance = sum((hr - estimate) ** 2 for hr, estimate in zip(hrs, predicted))
    r2 = 1 - residual_variance / total_variance if total_variance else None
    return {
        "intercept": coefficients[0],
        "bpm_per_kmh": coefficients[1],
        "bpm_per_celsius": coefficients[2],
        "r2": r2,
    }


def _evidence_level(sample_size, correlation=None):
    if sample_size < 8:
        return "insuficiente"
    if sample_size < 20:
        return "baja"
    if sample_size < 50:
        return "media"
    if correlation is not None and abs(correlation) < 0.15:
        return "media"
    return "alta"


def _model_evidence_level(sample_size, r2):
    if sample_size < 12 or r2 is None:
        return "insuficiente"
    if r2 < 0.15:
        return "baja"
    if r2 < 0.35:
        return "media"
    return "alta"


def _average(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else None


ATHLETIC_LOAD_WEIGHTS = {
    "cycling": 1.0, "indoor_cycling": 0.9, "running": 1.15,
    "trail_running": 1.2, "treadmill_running": 1.1,
    "lap_swimming": 1.0, "open_water_swimming": 1.1,
    "indoor_cardio": 0.8, "strength_training": 0.8,
    "walking": 0.5, "yoga": 0.4,
    "multi_sport": 1.05, "bikeToRunTransition_v2": 0.7,
}

ATHLETIC_SPORT_NAMES = {
    "cycling": "cycling", "indoor_cycling": "indoor cycling",
    "running": "running", "trail_running": "trail running",
    "treadmill_running": "treadmill", "lap_swimming": "swimming",
    "open_water_swimming": "open water", "indoor_cardio": "indoor cardio",
    "strength_training": "strength", "walking": "walking",
    "yoga": "mobility", "multi_sport": "multi-sport",
    "bikeToRunTransition_v2": "bike-run transition",
}


def _athletic_load_hours(sport_breakdown):
    breakdown = sport_breakdown or {}
    return sum(
        float((values or {}).get("hours") or 0) * ATHLETIC_LOAD_WEIGHTS.get(sport, 0.65)
        for sport, values in breakdown.items()
    )


# ── GET /gpt/athletic-status ──────────────────────────────────────────────────

@router.get("/gpt/athletic-status")
def gpt_athletic_status():
    """Lectura semanal de carga, continuidad y diversidad de toda la actividad."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT week_start, week_end, hours_week, sessions, active_days,
                       calories_week, sport_breakdown
                FROM athlete_snapshots
                WHERE week_end < CURRENT_DATE
                ORDER BY week_start DESC
                LIMIT 16
            """)
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT week_start, week_end, hours_week, sessions, active_days,
                       calories_week, sport_breakdown
                FROM athlete_snapshots
                ORDER BY week_start DESC
                LIMIT 1
            """)
            current_row = cur.fetchone()
            current = dict(zip([d[0] for d in cur.description], current_row)) if current_row else None

        if len(rows) < 8:
            return {"ok": False, "estado": "datos_insuficientes", "semanas_disponibles": len(rows)}

        recent = list(reversed(rows[:4]))
        previous = list(reversed(rows[4:8]))

        def summarize(block):
            discipline_hours = {}
            for row in block:
                for sport, values in (row.get("sport_breakdown") or {}).items():
                    discipline_hours[sport] = (
                        discipline_hours.get(sport, 0)
                        + float((values or {}).get("hours") or 0)
                    )
            dominant_sport = max(discipline_hours, key=discipline_hours.get) if discipline_hours else None
            return {
                "desde": block[0]["week_start"].isoformat(),
                "hasta": block[-1]["week_end"].isoformat(),
                "carga_equivalente_h_promedio": round(
                    _average([_athletic_load_hours(row["sport_breakdown"]) for row in block]), 2
                ),
                "horas_promedio": round(_average([row["hours_week"] for row in block]), 2),
                "sesiones_promedio": round(_average([row["sessions"] for row in block]), 1),
                "dias_activos_promedio": round(_average([row["active_days"] for row in block]), 1),
                "calorias_promedio": round(_average([row["calories_week"] for row in block]), 0),
                "disciplina_dominante": dominant_sport,
                "horas_por_disciplina": {
                    sport: round(hours, 2)
                    for sport, hours in sorted(discipline_hours.items())
                },
            }

        recent_summary = summarize(recent)
        previous_summary = summarize(previous)

        def pct_change(current_value, previous_value):
            if current_value is None or previous_value in (None, 0):
                return None
            return (current_value - previous_value) / abs(previous_value) * 100

        load_delta = pct_change(
            recent_summary["carga_equivalente_h_promedio"],
            previous_summary["carga_equivalente_h_promedio"],
        )
        active_days_delta = (
            recent_summary["dias_activos_promedio"] - previous_summary["dias_activos_promedio"]
        )
        session_delta = pct_change(
            recent_summary["sesiones_promedio"], previous_summary["sesiones_promedio"],
        )

        chronological = list(reversed(rows))
        active_streak = 0
        for row in reversed(chronological):
            if int(row.get("sessions") or 0) <= 0:
                break
            active_streak += 1
        recent_empty_weeks = sum(1 for row in rows[:8] if int(row.get("sessions") or 0) == 0)

        current_breakdown = (current or {}).get("sport_breakdown") or {}
        current_disciplines = [
            sport for sport, values in current_breakdown.items()
            if int((values or {}).get("sessions") or 0) > 0
        ]
        current_load = _athletic_load_hours(current_breakdown)
        previous_load = previous_summary["carga_equivalente_h_promedio"] or 0
        recent_load = recent_summary["carga_equivalente_h_promedio"] or 0
        discipline_change = bool(
            recent_summary["disciplina_dominante"]
            and previous_summary["disciplina_dominante"]
            and recent_summary["disciplina_dominante"] != previous_summary["disciplina_dominante"]
        )

        if load_delta is not None and load_delta > 200 and (
            previous_load < 2 or previous_load < recent_load * 0.35
        ):
            state = "regreso_tras_pausa"
            direction = "upward, with caution"
            explanation = (
                "You came back to regular training after a low-load block. "
                "The priority is holding this frequency before raising load again."
            )
            recommendation = "Keep a similar week, without raising volume or intensity."
        elif load_delta is not None and load_delta > 60 and discipline_change:
            state = "transicion_de_disciplina"
            direction = "upward, with caution"
            explanation = (
                "Load rose when switching from "
                f"{ATHLETIC_SPORT_NAMES.get(previous_summary['disciplina_dominante'], previous_summary['disciplina_dominante'])} a "
                f"{ATHLETIC_SPORT_NAMES.get(recent_summary['disciplina_dominante'], recent_summary['disciplina_dominante'])}. "
                "The percentage reflects a discipline change, not just more training."
            )
            recommendation = "Consolidate the new discipline before raising load again."
        elif load_delta is not None and load_delta > 60:
            state = "pico_de_carga"
            direction = "under watch"
            explanation = "Total load grew quickly compared with the previous block."
            recommendation = "Consolidate the load and prioritize recovery before another increase."
        elif load_delta is not None and load_delta < -40:
            state = "descarga_o_pausa"
            direction = "downward"
            explanation = "Total load clearly dropped over the last four weeks."
            recommendation = "Distinguish whether this is a planned deload or a loss of continuity."
        elif active_streak >= 4 and active_days_delta >= 0:
            state = "continuidad_solida"
            direction = "steady, positive"
            explanation = "Frequency is holding and a continuous base of several weeks already exists."
            recommendation = "Keep the consistency and change only one variable at a time."
        else:
            state = "construyendo_continuidad"
            direction = "steady"
            explanation = "Activity is there, but continuity can still be consolidated."
            recommendation = "Prioritize regular active days before chasing more volume."

        def serialize_week(row):
            if not row:
                return None
            return {
                "week_start": row["week_start"].isoformat(),
                "week_end": row["week_end"].isoformat(),
                "hours": float(row.get("hours_week") or 0),
                "load_equivalent_hours": round(_athletic_load_hours(row.get("sport_breakdown")), 2),
                "sessions": int(row.get("sessions") or 0),
                "active_days": int(row.get("active_days") or 0),
                "sport_breakdown": row.get("sport_breakdown") or {},
            }

        return {
            "ok": True, "estado": state, "direccion": direction,
            "explicacion": explanation, "recomendacion": recommendation,
            "ultimas_4": recent_summary, "anteriores_4": previous_summary,
            "cambios": {
                "carga_pct": round(load_delta, 1) if load_delta is not None else None,
                "dias_activos": round(active_days_delta, 1),
                "sesiones_pct": round(session_delta, 1) if session_delta is not None else None,
            },
            "continuidad": {
                "racha_semanas_activas": active_streak,
                "semanas_vacias_ultimas_8": recent_empty_weeks,
            },
            "transicion_disciplina": {
                "activa": discipline_change,
                "anterior": previous_summary["disciplina_dominante"],
                "actual": recent_summary["disciplina_dominante"],
            },
            "semana_actual": serialize_week(current),
            "carga_actual_equivalente_h": round(current_load, 2),
            "disciplinas_actuales": current_disciplines,
            "metodo_carga": {
                "unidad": "horas_equivalentes",
                "pesos": ATHLETIC_LOAD_WEIGHTS,
                "nota": "Proxy transparente para comparar disciplinas; no es una medida medica.",
            },
            "historial": [serialize_week(row) for row in chronological[-12:]],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/correlaciones + /gpt/correlations ───────────────────────────────

@router.get("/gpt/correlaciones")
@router.get("/gpt/correlations")
def gpt_correlations(sport: str = "cycling"):
    """Cuatro relaciones personales basadas en snapshots y sesiones limpias."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport debe ser cycling o running")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        efficiency_column = "cycling_efficiency" if sport == "cycling" else "running_efficiency"
        with conn.cursor() as cur:
            cur.execute("""
                SELECT week_start, km_week, hours_week, sessions, active_days,
                    weight_kg, avg_hr, cycling_efficiency, running_efficiency, sport_breakdown
                FROM athlete_snapshots ORDER BY week_start
            """)
            snapshot_columns = [d[0] for d in cur.description]
            snapshots = [dict(zip(snapshot_columns, row)) for row in cur.fetchall()]

            sport_values = (
                ("cycling", "indoor_cycling")
                if sport == "cycling"
                else ("running", "trail_running", "treadmill_running")
            )
            cur.execute("""
                WITH ordered AS (
                    SELECT start_time, sport,
                        avg_hr_bpm::float AS hr,
                        avg_speed_kmh::float AS speed,
                        efficiency_speed_hr::float AS efficiency,
                        EXTRACT(EPOCH FROM (
                            start_time - LAG(start_time) OVER (ORDER BY start_time)
                        )) / 86400.0 AS gap_days
                    FROM clean_sessions
                    WHERE sport IN %s
                      AND start_time IS NOT NULL
                      AND avg_hr_bpm BETWEEN 60 AND 210
                      AND avg_speed_kmh > 0
                )
                SELECT gap_days, hr, speed, efficiency
                FROM ordered
                WHERE gap_days IS NOT NULL AND gap_days BETWEEN 0 AND 14
                ORDER BY start_time
            """, (sport_values,))
            rest_rows = [
                {"gap_days": float(row[0]), "hr": float(row[1]), "speed": float(row[2]),
                 "efficiency": float(row[3]) if row[3] is not None else float(row[2]) / float(row[1])}
                for row in cur.fetchall()
            ]

            heat_rows = []
            if sport == "cycling":
                cur.execute("""
                    SELECT avg_speed_kmh::float AS speed, avg_hr_bpm::float AS hr,
                        COALESCE(
                            (NULLIF(raw_json->>'minTemperature','')::numeric +
                             NULLIF(raw_json->>'maxTemperature','')::numeric) / 2,
                            NULLIF(raw_json->>'minTemperature','')::numeric,
                            NULLIF(raw_json->>'maxTemperature','')::numeric
                        )::float AS temperature
                    FROM clean_sessions
                    WHERE sport IN ('cycling','indoor_cycling')
                      AND source='garmin_export'
                      AND avg_speed_kmh BETWEEN 8 AND 55
                      AND avg_hr_bpm BETWEEN 70 AND 200
                      AND (raw_json ? 'minTemperature' OR raw_json ? 'maxTemperature')
                """)
                heat_rows = [
                    {"speed": float(row[0]), "hr": float(row[1]), "temperature": float(row[2])}
                    for row in cur.fetchall()
                    if row[2] is not None and -10 <= float(row[2]) <= 50
                ]

        rest_buckets = {
            "menos_de_1_dia": (0, 1), "1_dia": (1, 2), "2_dias": (2, 3),
            "3_dias": (3, 4), "4_a_7_dias": (4, 8), "8_a_14_dias": (8, 15),
        }
        rest_summary = []
        for label, (low, high) in rest_buckets.items():
            rows = [row for row in rest_rows if low <= row["gap_days"] < high]
            if not rows:
                continue
            rest_summary.append({
                "rango": label, "sesiones": len(rows),
                "fc_promedio": round(_average([row["hr"] for row in rows]), 1),
                "eficiencia_promedio": round(_average([row["efficiency"] for row in rows]), 5),
            })
        eligible_rest = [row for row in rest_summary if row["sesiones"] >= 8]
        optimal_rest = max(eligible_rest, key=lambda row: row["eficiencia_promedio"]) if eligible_rest else None

        def sport_volume(snapshot, metric):
            breakdown = snapshot.get("sport_breakdown") or {}
            sport_names = (
                ("cycling", "indoor_cycling") if sport == "cycling"
                else ("running", "trail_running", "treadmill_running")
            )
            return sum(float((breakdown.get(name) or {}).get(metric) or 0) for name in sport_names)

        volume_pairs = []
        for previous, current in zip(snapshots, snapshots[1:]):
            efficiency = current.get(efficiency_column)
            if efficiency is not None:
                volume_pairs.append((sport_volume(previous, "km"), float(efficiency)))

        volume_corr = _pearson(
            [pair[0] for pair in volume_pairs],
            [pair[1] for pair in volume_pairs],
        )
        volume_buckets = [(0, 25), (25, 50), (50, 100), (100, 150), (150, 200), (200, 100000)]
        volume_summary = []
        for low, high in volume_buckets:
            rows = [eff for km, eff in volume_pairs if low <= km < high]
            if rows:
                volume_summary.append({
                    "km_semana_anterior": f"{low}-{high if high < 100000 else '+'}",
                    "semanas": len(rows),
                    "eficiencia_promedio": round(_average(rows), 5),
                })
        eligible_volume = [row for row in volume_summary if row["semanas"] >= 6]
        optimal_volume = max(eligible_volume, key=lambda row: row["eficiencia_promedio"]) if eligible_volume else None

        weight_rows = [
            {
                "year": row["week_start"].year,
                "week_start": row["week_start"],
                "weight": float(row["weight_kg"]),
                "efficiency": float(row[efficiency_column]),
            }
            for row in snapshots
            if row.get("weight_kg") is not None and row.get(efficiency_column) is not None
        ]
        weight_by_year = {}
        for row in weight_rows:
            weight_by_year.setdefault(row["year"], []).append(row)
        weight_year_summary = []
        adjusted_weights = []
        adjusted_efficiencies = []
        for year, rows in sorted(weight_by_year.items()):
            weights = [row["weight"] for row in rows]
            efficiencies = [row["efficiency"] for row in rows]
            year_corr = _pearson(weights, efficiencies)
            year_regression = _linear_regression(weights, efficiencies)
            weight_year_summary.append({
                "year": year, "weeks": len(rows),
                "correlation": round(year_corr, 4) if year_corr is not None else None,
                "change_efficiency_per_kg": (
                    round(year_regression["slope"], 6) if year_regression else None
                ),
            })
            if len(rows) < 6:
                continue
            mean_weight = _average(weights)
            mean_efficiency = _average(efficiencies)
            adjusted_weights.extend(weight - mean_weight for weight in weights)
            adjusted_efficiencies.extend(efficiency - mean_efficiency for efficiency in efficiencies)
        weight_corr = _pearson(adjusted_weights, adjusted_efficiencies)
        weight_regression = _linear_regression(adjusted_weights, adjusted_efficiencies)

        heat_model = _multiple_regression_hr_speed_temperature(heat_rows)
        heat_confidence = _model_evidence_level(
            len(heat_rows), heat_model.get("r2") if heat_model else None,
        )
        weight_slope = weight_regression["slope"] if weight_regression else None
        weight_direction = (
            "menor_peso_mejor_eficiencia" if weight_slope is not None and weight_slope < 0
            else "menor_peso_peor_eficiencia" if weight_slope is not None and weight_slope > 0
            else "sin_direccion"
        )
        weight_confounded = weight_direction == "menor_peso_peor_eficiencia"
        weight_usable_years = [
            row for row in weight_year_summary
            if row["weeks"] >= 6 and row["change_efficiency_per_kg"] is not None
        ]
        weight_evidence = (
            "insuficiente"
            if len(adjusted_weights) < 20 or len(weight_usable_years) < 2
            else "baja"
            if weight_corr is None or abs(weight_corr) < 0.2
            else "media"
        )
        weight_recommendable = bool(
            weight_regression and not weight_confounded and weight_evidence == "media"
        )
        return {
            "ok": True, "sport": sport,
            "metodo": "historia personal; asociaciones observacionales, no causalidad",
            "descanso_optimo": {
                "muestra": len(rest_rows), "por_rango": rest_summary,
                "mejor_rango": optimal_rest,
                "confianza": _evidence_level(len(rest_rows)),
                "lectura": (
                    f"El mejor rendimiento observado aparece tras {optimal_rest['rango'].replace('_',' ')}."
                    if optimal_rest else "Todavia no hay suficientes sesiones por rango."
                ),
            },
            "volumen_optimo": {
                "muestra_semanas": len(volume_pairs),
                "correlacion_km_previos_eficiencia": volume_corr,
                "por_rango": volume_summary, "mejor_rango": optimal_volume,
                "confianza": _evidence_level(len(volume_pairs), volume_corr),
                "lectura": (
                    f"El rango con mejor eficiencia posterior fue {optimal_volume['km_semana_anterior']} km."
                    if optimal_volume else "Todavia no hay un rango dominante con muestra suficiente."
                ),
            },
            "costo_calor": {
                "muestra_sesiones": len(heat_rows),
                "bpm_por_10c_a_misma_velocidad": (
                    round(heat_model["bpm_per_celsius"] * 10, 2) if heat_model else None
                ),
                "bpm_por_kmh": round(heat_model["bpm_per_kmh"], 3) if heat_model else None,
                "r2": round(heat_model["r2"], 3) if heat_model and heat_model["r2"] is not None else None,
                "confianza": heat_confidence,
                "usable_para_recomendacion": heat_confidence in ("media", "alta"),
                "lectura": (
                    f"Diez grados adicionales se asocian con "
                    f"{heat_model['bpm_per_celsius'] * 10:+.1f} bpm a velocidad comparable; "
                    f"el modelo explica {heat_model['r2'] * 100:.1f}% de la variacion."
                    if heat_model else "No hay muestra suficiente para aislar temperatura y velocidad."
                ),
            },
            "peso_rendimiento": {
                "muestra_semanas": len(weight_rows),
                "muestra_ajustada_semanas": len(adjusted_weights),
                "metodo": "efectos_fijos_por_anio",
                "por_anio": weight_year_summary,
                "correlacion": weight_corr,
                "cambio_eficiencia_por_kg": (
                    round(weight_regression["slope"], 6) if weight_regression else None
                ),
                "cambio_estimado_al_bajar_1kg": (
                    round(-weight_regression["slope"], 6) if weight_regression else None
                ),
                "direccion_observada": weight_direction,
                "posibles_confusores": (
                    ["ruta", "volumen", "estado_de_forma"] if weight_confounded else []
                ),
                "usable_para_recomendacion": weight_recommendable,
                "confianza": (
                    "baja_por_confusores" if weight_confounded else weight_evidence
                ),
                "lectura": (
                    "Aun controlando por año, la asociacion sale en direccion contraintuitiva; "
                    "no debe usarse para recomendar peso hasta controlar ruta, volumen y estado de forma."
                    if weight_confounded else
                    f"Comparando semanas dentro del mismo año, bajar 1 kg se asocia con "
                    f"{-weight_regression['slope']:+.5f} de eficiencia {sport}."
                    if weight_regression else "Faltan semanas con peso y eficiencia coincidentes."
                ),
            },
            "notas": [
                "Descanso compara el intervalo previo con la eficiencia de la siguiente sesion.",
                "Volumen usa km de la semana anterior contra eficiencia de la semana siguiente.",
                "Calor controla estadisticamente la velocidad, pero no ruta, viento ni altimetria.",
                "Peso no se interpola y se compara dentro de cada año para evitar mezclar epocas.",
            ],
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/tendencia ────────────────────────────────────────────────────────

@router.get("/gpt/tendencia")
def gpt_tendencia(sport: str = "cycling"):
    """Compara las ultimas cuatro semanas completas contra las cuatro anteriores."""
    if sport not in ("cycling", "running"):
        raise HTTPException(400, "sport debe ser cycling o running")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    efficiency_column = "cycling_efficiency" if sport == "cycling" else "running_efficiency"
    sport_values = (
        ("cycling", "indoor_cycling") if sport == "cycling"
        else ("running", "trail_running", "treadmill_running")
    )
    sport_label = "ciclismo" if sport == "cycling" else "correr"
    comparable_best_sports = ("cycling",) if sport == "cycling" else sport_values
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT a.week_start, a.week_end, a.km_week, a.hours_week, a.sessions,
                    a.sport_breakdown,
                    (
                        SELECT SUM(cs.avg_hr_bpm * cs.duration_s) / NULLIF(SUM(cs.duration_s),0)
                        FROM clean_sessions cs
                        WHERE cs.start_date BETWEEN a.week_start AND a.week_end
                          AND cs.sport IN %s AND cs.avg_hr_bpm > 0 AND cs.duration_s > 0
                    ) AS avg_hr,
                    a.{efficiency_column} AS efficiency
                FROM athlete_snapshots a
                WHERE a.week_end < CURRENT_DATE
                ORDER BY a.week_start DESC
                LIMIT 8
            """, (sport_values,))
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute(f"""
                SELECT a.week_start, a.week_end, a.km_week, a.hours_week, a.sessions,
                    a.sport_breakdown,
                    (
                        SELECT SUM(cs.avg_hr_bpm * cs.duration_s) / NULLIF(SUM(cs.duration_s),0)
                        FROM clean_sessions cs
                        WHERE cs.start_date BETWEEN a.week_start AND a.week_end
                          AND cs.sport IN %s AND cs.avg_hr_bpm > 0 AND cs.duration_s > 0
                    ) AS avg_hr,
                    a.{efficiency_column} AS efficiency
                FROM athlete_snapshots a
                ORDER BY a.week_start DESC
                LIMIT 1
            """, (sport_values,))
            current_row = cur.fetchone()
            current_week = dict(zip([d[0] for d in cur.description], current_row)) if current_row else None
            cur.execute("""
                WITH weekly AS (
                    SELECT DATE_TRUNC('week', start_time)::date AS week_start,
                        (SUM((avg_speed_kmh / avg_hr_bpm) * duration_s) /
                         NULLIF(SUM(duration_s), 0))::float AS efficiency
                    FROM clean_sessions
                    WHERE sport IN %s AND start_time IS NOT NULL
                      AND avg_speed_kmh > 0 AND avg_hr_bpm BETWEEN 60 AND 210 AND duration_s > 0
                    GROUP BY DATE_TRUNC('week', start_time)::date
                    HAVING COUNT(*) >= 2 AND SUM(duration_s) >= 7200
                ),
                benchmark AS (
                    SELECT PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY efficiency) AS efficiency
                    FROM weekly
                )
                SELECT weekly.week_start, weekly.efficiency
                FROM weekly, benchmark
                ORDER BY ABS(weekly.efficiency - benchmark.efficiency), weekly.week_start DESC
                LIMIT 1
            """, (comparable_best_sports,))
            best_row = cur.fetchone()
            best_week = (
                {"week_start": best_row[0], "efficiency": float(best_row[1])}
                if best_row else None
            )

        if len(rows) < 8:
            return {
                "ok": False, "estado": "datos_insuficientes",
                "semanas_disponibles": len(rows), "necesarias": 8,
            }
        recent = list(reversed(rows[:4]))
        previous = list(reversed(rows[4:8]))

        def block_sport_hours(row):
            breakdown = row.get("sport_breakdown") or {}
            return sum(
                float((breakdown.get(name) or {}).get("hours") or 0)
                for name in sport_values
            )

        def block_sport_km(row):
            breakdown = row.get("sport_breakdown") or {}
            return sum(
                float((breakdown.get(name) or {}).get("km") or 0)
                for name in sport_values
            )

        def block_summary(block):
            return {
                "desde": block[0]["week_start"].isoformat(),
                "hasta": block[-1]["week_end"].isoformat(),
                "km_promedio": round(_average([block_sport_km(row) for row in block]), 1),
                "horas_promedio": round(_average([block_sport_hours(row) for row in block]), 2),
                "sesiones_promedio": round(_average([row["sessions"] for row in block]), 1),
                "fc_promedio": (
                    round(_average([row["avg_hr"] for row in block]), 1)
                    if _average([row["avg_hr"] for row in block]) is not None else None
                ),
                "eficiencia_promedio": (
                    round(_average([row["efficiency"] for row in block]), 5)
                    if _average([row["efficiency"] for row in block]) is not None else None
                ),
            }

        recent_summary = block_summary(recent)
        previous_summary = block_summary(previous)
        recent_sport_hours = sum(block_sport_hours(row) for row in recent)
        previous_sport_hours = sum(block_sport_hours(row) for row in previous)

        def pct_change(current, prior):
            if current is None or prior in (None, 0):
                return None
            return (current - prior) / abs(prior) * 100

        efficiency_delta = pct_change(recent_summary["eficiencia_promedio"], previous_summary["eficiencia_promedio"])
        hr_delta = (
            recent_summary["fc_promedio"] - previous_summary["fc_promedio"]
            if recent_summary["fc_promedio"] is not None and previous_summary["fc_promedio"] is not None
            else None
        )
        volume_delta = pct_change(recent_summary["horas_promedio"], previous_summary["horas_promedio"])
        extreme_load = volume_delta is not None and volume_delta > 200
        load_context = None
        if extreme_load:
            previous_hours = previous_summary["horas_promedio"] or 0
            recent_hours = recent_summary["horas_promedio"] or 0
            load_context = (
                "regreso_tras_pausa"
                if previous_hours < 2 or previous_hours < recent_hours * 0.35
                else "pico_atipico"
            )

        signals = []
        score = 0
        if efficiency_delta is not None:
            if efficiency_delta >= 2: signals.append("eficiencia_mejora"); score += 1
            elif efficiency_delta <= -2: signals.append("eficiencia_baja"); score -= 1
        if hr_delta is not None:
            if hr_delta <= -2: signals.append("fc_baja"); score += 1
            elif hr_delta >= 2: signals.append("fc_sube"); score -= 1
        if volume_delta is not None:
            if 5 <= volume_delta <= 30: signals.append("volumen_sube_controlado"); score += 1
            elif volume_delta < -20: signals.append("volumen_baja"); score -= 1
            elif volume_delta > 30: signals.append("volumen_sube_fuerte")

        overload = (hr_delta is not None and hr_delta >= 3 and efficiency_delta is not None and efficiency_delta <= -2)
        abrupt_load = volume_delta is not None and volume_delta > 50

        if recent_sport_hours <= 0:
            state = "sin_actividad_reciente"; direction = "paused"
            explanation = f"No recent {sport_label} sessions. The history remains available, but there is no current trend to interpret."
        elif previous_sport_hours <= 0:
            state = "regreso_tras_pausa"; direction = "upward, with caution"
            explanation = f"The recent {sport_label} sessions represent a return after a block without activity."
        elif overload:
            state = "posible_sobrecarga"; direction = "downward"
            explanation = "HR rose while efficiency dropped; better to reduce load and watch recovery."
        elif extreme_load and load_context == "regreso_tras_pausa":
            state = "regreso_tras_pausa"; direction = "upward, with caution"
            explanation = "The percentage jump is large because the previous block had very little volume. It is a return after a pause: the aerobic response is positive, but consolidating comes first."
        elif extreme_load:
            state = "pico_atipico"; direction = "under watch"
            explanation = "Recent volume is an atypical peak versus the previous block. Do not read the percentage in isolation or raise load again right away."
        elif abrupt_load and efficiency_delta is not None and efficiency_delta >= 2:
            state = "respuesta_positiva_carga_alta"; direction = "upward, with caution"
            explanation = "Efficiency improved, but volume jumped sharply. The response is positive; consolidate the load before raising it again."
        elif abrupt_load:
            state = "carga_en_observacion"; direction = "uncertain"
            explanation = "Volume jumped sharply and there is no clear aerobic improvement yet. Watch recovery and do not raise load this week."
        elif score >= 2:
            state = "mejorando"; direction = "upward"
            explanation = "The recent aerobic response improves on the previous block."
        elif score <= -2:
            state = "retrocediendo"; direction = "downward"
            explanation = "Two or more signals worsened versus the previous four weeks."
        else:
            state = "estable"; direction = "sideways"
            explanation = "Recent changes are mixed or small; there is no strong direction."

        def serialize_week(row):
            if not row:
                return None
            return {
                key: value.isoformat() if hasattr(value, "isoformat") else float(value)
                if hasattr(value, "__float__") and value is not None else value
                for key, value in row.items()
            }

        current_efficiency = (
            float(current_week["efficiency"])
            if current_week and current_week.get("efficiency") is not None else None
        )
        best_efficiency = best_week["efficiency"] if best_week else None
        best_form_pct = (
            current_efficiency / best_efficiency * 100
            if current_efficiency is not None and best_efficiency else None
        )
        return {
            "ok": True, "sport": sport, "estado": state, "direccion": direction,
            "score": score, "explicacion": explanation, "senales": signals,
            "actividad_reciente": recent_sport_hours > 0,
            "ultimas_4": recent_summary, "anteriores_4": previous_summary,
            "cambios": {
                "eficiencia_pct": round(efficiency_delta, 2) if efficiency_delta is not None else None,
                "fc_bpm": round(hr_delta, 1) if hr_delta is not None else None,
                "volumen_horas_pct": round(volume_delta, 2) if volume_delta is not None else None,
            },
            "alerta_carga": {
                "activa": abrupt_load, "umbral_pct": 50, "contexto": load_context,
                "lectura": (
                    "Regreso tras pausa: el porcentaje se amplifica por el bajo volumen previo."
                    if load_context == "regreso_tras_pausa"
                    else "Pico atipico frente al bloque anterior; no sumar mas carga hasta consolidar."
                    if load_context == "pico_atipico"
                    else "Aumento brusco frente al bloque anterior; no sumar mas carga hasta consolidar."
                    if abrupt_load else "Cambio de carga dentro del rango de observacion."
                ),
            },
            "mejor_version": {
                "eficiencia_historica": round(best_efficiency, 5) if best_efficiency is not None else None,
                "semana": best_week["week_start"].isoformat() if best_week else None,
                "eficiencia_actual": round(current_efficiency, 5) if current_efficiency is not None else None,
                "porcentaje_mejor_forma": round(best_form_pct, 1) if best_form_pct is not None else None,
                "criterio": "percentil 90 de semanas con al menos 2 sesiones y 2 horas comparables",
            },
            "semana_actual": serialize_week(current_week),
            "nota": "Indicador observacional; no sustituye evaluacion medica ni mide por si solo fatiga.",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/mars-context ─────────────────────────────────────────────────────

@router.get("/gpt/mars-context")
def gpt_mars_context():
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
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
                    FROM sessions_clean_compat WHERE sport='cycling' AND avg_hr_bpm>0 AND avg_speed_kmh>0
                    AND start_time::timestamp>=NOW()-'4 weeks'::interval""")
                row = cur.fetchone()
            if row and row[0]: p["eff_actual"] = float(row[0])
        except: pass
        z = p.get("zonas_ciclismo",{})
        z2 = z.get("z2",[134,150])
        a = p.get("athlete",{})
        plan = p.get("plan_garmin",{})
        p["context_msg"] = (f"Plan {plan.get('nombre','Garmin Coach')} · fase {plan.get('fase','Base')} · "
            f"Z2 ciclismo: {z2[0]}-{z2[1]} bpm · Cadencia obj: {p.get('cadencia_obj',100)} rpm · "
            f"Peso: {a.get('peso_actual_kg',89.1)} kg · objetivo {a.get('peso_objetivo_kg',80)} kg")
        return p
    except Exception as e: raise HTTPException(500, str(e))


