"""
routers/gpt_coaching.py — Coaching, nutrition & context endpoints
TD-010A split de gpt_analytics.py
Endpoints: /gpt/adaptive-coach, /gpt/fueling-log, /gpt/gel-tests, /gpt/mars-context
"""
import logging
from fastapi import APIRouter, HTTPException
from db import get_db, _ensure_goals_table
from mars_context import _get_profile

logger = logging.getLogger("mars_fit")
router = APIRouter(tags=["gpt_coaching"])


@router.get("/gpt/adaptive-coach")
def adaptive_coach():
    """
    E29 — Adaptive Coaching Engine.
    Combina meta activa (E26B) + capacidades (CE) + carga reciente → plan semanal.
    """
    from datetime import date as _date
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")

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


@router.get("/gpt/fueling-log")
def gpt_fueling_log(limit: int = 20):
    """Historial de nutrición post-sesión — geles, barras, agua, cafeína, CHO/hora."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
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


@router.get("/gpt/gel-tests")
def gpt_gel_tests():
    """Comparativa de geles — qué tipo funcionó mejor por respuesta GI y energética."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
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


@router.get("/gpt/mars-context")
def gpt_mars_context():
    conn = get_db()
    if not conn: raise HTTPException(503, "DB unavailable")
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
        p["context_msg"] = (f"Plan {plan.get('nombre','Garmin Coach')} · {plan.get('fase','Base')} phase · "
            f"Z2 ciclismo: {z2[0]}-{z2[1]} bpm · Cadencia obj: {p.get('cadencia_obj',100)} rpm · "
            f"Weight: {a.get('peso_actual_kg',89.1)} kg · target {a.get('peso_objetivo_kg',80)} kg")
        return p
    except Exception as e: raise HTTPException(500, str(e))
