"""
routers/data_entry.py — Endpoints de ingesta de datos del atleta
================================================================
TD-010A: extraídos de main.py.
Incluye:
  POST/GET /post-session/{session_id}
  PUT /gear/{gear_id}
  GET /gear/alerts
  GET/POST /maintenance
  POST/GET /recovery
  GET/POST/PUT/DELETE /gpt/goals
  GET /gpt/gear-alerts
  GET /gpt/athlete-status
  GET/POST /gpt/athlete-profile
  POST/GET /weight, /weight/history
  POST/GET /gear/service, /gear/service-history
  POST/GET /wellness, /gpt/wellness-summary
  POST/GET /fuerza, /gpt/fuerza-summary
  GET /gpt/gear-status
  POST /gear
  POST /accidents
  POST/GET /nutrition, /nutrition/summary
  DELETE /fuerza/{id}, /wellness/{id}, /nutrition/{id}, /gear/service/{id}, /gear/{id}
  GET /api/fuerza-records, /api/wellness-records
"""
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from db import (
    get_db,
    _ensure_weight_table,
    _ensure_gear_service_table,
    _ensure_wellness_table,
    _ensure_fuerza_table,
    _ensure_accidents_table,
    _ensure_gear_activity_links_table,
    _ensure_nutrition_table,
    _ensure_goals_table,
)
from shared.models import (
    PostSessionIn, GearUpdate, GearServiceIn, GearIn, MaintenanceIn,
    RecoveryIn, WellnessIn, FuerzaIn, NutritionIn, WeightIn,
    GoalCreate, GoalUpdate, AthleteProfileIn, AccidentIn,
)

logger = logging.getLogger("mars_fit")

router = APIRouter(tags=["data_entry"])


# ── POST /post-session/{session_id} ──────────────────────────────────────────

@router.post("/post-session/{session_id}")
def save_post_session(session_id: str, body: PostSessionIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")

    sweat_rate = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT duration_s FROM sessions_clean_compat WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
        if row and row[0] and body.weight_before and body.weight_after and body.water_liters:
            duration_h = row[0] / 3600
            if duration_h > 0:
                sweat_rate = round(
                    (body.weight_before - body.weight_after + body.water_liters) / duration_h, 2
                )
    except Exception:
        pass

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO post_session (
                    session_id, rpe, weight_before, weight_after, water_liters, sweat_rate,
                    caffeine_mg, food_before, gels, bars, electrolytes, digestion,
                    sleep_hours, sleep_quality, conditions, notes,
                    gel_type, gel_recipe, gel_carbs_g, gel_sodium_mg,
                    gel_timing, gi_response, energy_response, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    rpe=EXCLUDED.rpe, weight_before=EXCLUDED.weight_before,
                    weight_after=EXCLUDED.weight_after, water_liters=EXCLUDED.water_liters,
                    sweat_rate=EXCLUDED.sweat_rate, caffeine_mg=EXCLUDED.caffeine_mg,
                    food_before=EXCLUDED.food_before, gels=EXCLUDED.gels, bars=EXCLUDED.bars,
                    electrolytes=EXCLUDED.electrolytes, digestion=EXCLUDED.digestion,
                    sleep_hours=EXCLUDED.sleep_hours, sleep_quality=EXCLUDED.sleep_quality,
                    conditions=EXCLUDED.conditions, notes=EXCLUDED.notes,
                    gel_type=EXCLUDED.gel_type, gel_recipe=EXCLUDED.gel_recipe,
                    gel_carbs_g=EXCLUDED.gel_carbs_g, gel_sodium_mg=EXCLUDED.gel_sodium_mg,
                    gel_timing=EXCLUDED.gel_timing, gi_response=EXCLUDED.gi_response,
                    energy_response=EXCLUDED.energy_response
            """, (
                session_id, body.rpe, body.weight_before, body.weight_after,
                body.water_liters, sweat_rate, body.caffeine_mg,
                body.food_before, body.gels, body.bars, body.electrolytes,
                body.digestion, body.sleep_hours, body.sleep_quality,
                body.conditions, body.notes,
                body.gel_type, body.gel_recipe, body.gel_carbs_g, body.gel_sodium_mg,
                body.gel_timing, body.gi_response, body.energy_response
            ))
        return {"ok": True, "session_id": session_id, "sweat_rate": sweat_rate}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /post-session/{session_id} ───────────────────────────────────────────

@router.get("/post-session/{session_id}")
def get_post_session(session_id: str):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM post_session WHERE session_id=%s", (session_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"No hay registro post-sesión para {session_id}")
            cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── PUT /gear/{gear_id} ───────────────────────────────────────────────────────

@router.put("/gear/{gear_id}")
def update_gear(gear_id: str, body: GearUpdate):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nada que actualizar")
    try:
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gear SET {set_clause} WHERE gear_id=%s",
                list(updates.values()) + [gear_id]
            )
            if cur.rowcount == 0:
                raise HTTPException(404, f"gear_id '{gear_id}' no encontrado")
        return {"ok": True, "gear_id": gear_id, "updated": list(updates.keys())}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gear/alerts ──────────────────────────────────────────────────────────

@router.get("/gear/alerts")
def gear_alerts(bike_id: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT g.gear_id, g.name, g.type, g.bike_id,
                   g.km_at_install, g.km_limit, g.installed_date, g.retired_date,
                   COALESCE(
                       (SELECT MAX(m.km_at_service) FROM maintenance m
                        WHERE m.gear_id = g.gear_id),
                       g.km_at_install, 0
                   ) as last_km
            FROM gear g
            WHERE g.retired_date IS NULL
              AND g.km_limit IS NOT NULL
        """
        params = []
        if bike_id:
            query += " AND g.bike_id=%s"
            params.append(bike_id)

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        alerts = []
        for row in rows:
            item = dict(zip(cols, row))
            km_install = item.get("km_at_install") or 0
            last_km = item.get("last_km") or km_install
            km_used = max(0, last_km - km_install)
            km_limit = item["km_limit"]
            pct = round(km_used / km_limit * 100, 1) if km_limit else 0
            if pct >= 60:
                status = "red" if pct >= 85 else "yellow"
                label = "Cambiar ahora" if pct >= 85 else "Revisar pronto"
                alerts.append({**item, "km_used": km_used, "pct_used": pct,
                                "status": status, "label": label})

        alerts.sort(key=lambda x: x["pct_used"], reverse=True)
        return {"alerts": alerts, "total": len(alerts)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /maintenance ──────────────────────────────────────────────────────────

@router.get("/maintenance")
def list_maintenance(bike_id: Optional[str] = None, gear_id: Optional[str] = None, limit: int = 50):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM maintenance WHERE 1=1"
        params = []
        if bike_id:
            query += " AND bike_id=%s"
            params.append(bike_id)
        if gear_id:
            query += " AND gear_id=%s"
            params.append(gear_id)
        query += " ORDER BY date DESC NULLS LAST LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"maintenance": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /recovery ────────────────────────────────────────────────────────────

@router.post("/recovery")
def add_recovery(body: RecoveryIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO recovery (date, type, duration_min, muscle_zone,
                    compex_program, notes, fatigue, muscle_pain, mental_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                body.date, body.type, body.duration_min, body.muscle_zone,
                body.compex_program, body.notes, body.fatigue,
                body.muscle_pain, body.mental_state
            ))
            new_id = cur.fetchone()[0]
        return {"ok": True, "id": new_id}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /recovery ─────────────────────────────────────────────────────────────

@router.get("/recovery")
def list_recovery(limit: int = 30, type: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = "SELECT * FROM recovery WHERE 1=1"
        params = []
        if type:
            query += " AND type=%s"
            params.append(type)
        query += " ORDER BY date DESC NULLS LAST LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"recovery": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/goals ───────────────────────────────────────────────────────────

@router.get("/gpt/goals")
def get_goals(status: str = "active"):
    """Lista metas del atleta. ADR-011: solo las que el usuario creó."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, event_name, event_type, event_date,
                   distance_km, elevation_m, priority, status,
                   notes, scenario_key, created_at
            FROM mars_goals
            WHERE status = %s
            ORDER BY priority ASC, event_date ASC NULLS LAST
        """, (status,))
        rows = cur.fetchall()
    goals = []
    for r in rows:
        goals.append({
            "id":           r[0],
            "event_name":   r[1],
            "event_type":   r[2],
            "event_date":   r[3].isoformat() if r[3] else None,
            "distance_km":  r[4],
            "elevation_m":  r[5],
            "priority":     r[6],
            "status":       r[7],
            "notes":        r[8],
            "scenario_key": r[9],
            "created_at":   r[10].isoformat() if r[10] else None,
        })
    return {"goals": goals, "total": len(goals)}


# ── POST /gpt/goals ──────────────────────────────────────────────────────────

@router.post("/gpt/goals")
def create_goal(goal: GoalCreate):
    """Crea una meta activa. ADR-011: acción explícita del usuario."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mars_goals
                (event_name, event_type, event_date, distance_km,
                 elevation_m, priority, notes, scenario_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
        """, (
            goal.event_name, goal.event_type,
            goal.event_date, goal.distance_km,
            goal.elevation_m, goal.priority,
            goal.notes, goal.scenario_key,
        ))
        row = cur.fetchone()
    return {
        "status":     "created",
        "id":         row[0],
        "event_name": goal.event_name,
        "created_at": row[1].isoformat(),
    }


# ── PUT /gpt/goals/{goal_id} ─────────────────────────────────────────────────

@router.put("/gpt/goals/{goal_id}")
def update_goal(goal_id: int, update: GoalUpdate):
    """Actualiza una meta (status, fecha, notas, etc.)."""
    fields, values = [], []
    if update.event_name  is not None: fields.append("event_name=%s");  values.append(update.event_name)
    if update.event_date  is not None: fields.append("event_date=%s");  values.append(update.event_date)
    if update.distance_km is not None: fields.append("distance_km=%s"); values.append(update.distance_km)
    if update.elevation_m is not None: fields.append("elevation_m=%s"); values.append(update.elevation_m)
    if update.priority    is not None: fields.append("priority=%s");    values.append(update.priority)
    if update.status      is not None: fields.append("status=%s");      values.append(update.status)
    if update.notes       is not None: fields.append("notes=%s");       values.append(update.notes)
    if not fields:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    fields.append("updated_at=NOW()")
    values.append(goal_id)
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE mars_goals SET {', '.join(fields)} WHERE id=%s RETURNING id",
            values
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Meta no encontrada")
    return {"status": "updated", "id": goal_id}


# ── DELETE /gpt/goals/{goal_id} ──────────────────────────────────────────────

@router.delete("/gpt/goals/{goal_id}")
def delete_goal(goal_id: int):
    """Elimina una meta."""
    conn = get_db()
    if not conn:
        raise HTTPException(status_code=503, detail="DB no disponible")
    _ensure_goals_table(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM mars_goals WHERE id=%s RETURNING id", (goal_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Meta no encontrada")
        conn.commit()
    return {"status": "deleted", "id": goal_id}


# ── GET /gpt/gear-alerts ─────────────────────────────────────────────────────

@router.get("/gpt/gear-alerts")
def gpt_gear_alerts():
    """Alertas de componentes cerca del límite para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.gear_id, g.name, g.type, g.bike_id,
                       g.km_at_install, g.km_limit,
                       COALESCE(
                           (SELECT MAX(m.km_at_service) FROM maintenance m WHERE m.gear_id = g.gear_id),
                           g.km_at_install, 0
                       ) as last_km
                FROM gear g
                WHERE g.retired_date IS NULL AND g.km_limit IS NOT NULL
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        alerts = []
        for row in rows:
            item = dict(zip(cols, row))
            km_install = item.get("km_at_install") or 0
            last_km = item.get("last_km") or km_install
            km_used = max(0, last_km - km_install)
            km_limit = item["km_limit"]
            pct = round(km_used / km_limit * 100, 1) if km_limit else 0
            item["km_used"] = km_used
            item["pct_used"] = pct
            item["status"] = "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
            item["label"] = "Cambiar ahora" if pct >= 85 else "Revisar pronto" if pct >= 60 else "OK"
            alerts.append(item)

        alerts.sort(key=lambda x: x["pct_used"], reverse=True)
        critical = [a for a in alerts if a["status"] in ("red", "yellow")]
        return {
            "total_componentes": len(alerts),
            "alertas_criticas": len(critical),
            "alertas": critical,
            "todos": alerts
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/athlete-status ───────────────────────────────────────────────────

@router.get("/gpt/athlete-status")
def gpt_athlete_status(sport: str = "cycling"):
    """Estado actual del atleta — fitness, fatiga, eficiencia aeróbica, cadencia, recomendación."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric,1) as km_2w,
                    ROUND(SUM(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN COALESCE(distance_km,0) ELSE 0 END)::numeric/4.0,1) as km_4w_avg,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_hr_bpm END)::numeric,0) as fc_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_speed_kmh END)::numeric,1) as spd_base,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_reciente,
                    ROUND(AVG(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '6 weeks'
                                   AND start_time::timestamp < NOW()-INTERVAL '2 weeks'
                              THEN avg_cadence END)::numeric,1) as cad_base,
                    COUNT(CASE WHEN start_time::timestamp >= NOW()-INTERVAL '2 weeks' THEN 1 END) as sesiones_2w
                FROM sessions_clean_compat WHERE sport = %s AND start_time IS NOT NULL
            """, (sport,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        km_2w = float(d.get("km_2w") or 0)
        km_4w_avg = float(d.get("km_4w_avg") or 0)
        fc_rec = float(d.get("fc_reciente") or 0)
        fc_base = float(d.get("fc_base") or 0)
        spd_rec = float(d.get("spd_reciente") or 0)
        spd_base = float(d.get("spd_base") or 0)
        cad_rec = float(d.get("cad_reciente") or 0)
        cad_base = float(d.get("cad_base") or 0)
        fitness = "estable"
        if km_4w_avg > 0:
            p = (km_2w - km_4w_avg) / km_4w_avg * 100
            if p > 10: fitness = "subiendo"
            elif p < -15: fitness = "bajando"
        fatiga = "baja"
        if fc_base > 0 and spd_base > 0:
            eff_rec  = spd_rec  / fc_rec  if fc_rec  else 0
            eff_base = spd_base / fc_base
            if eff_base > 0:
                ep = (eff_rec - eff_base) / eff_base * 100
                if ep < -5: fatiga = "alta"
                elif ep < -2: fatiga = "moderada"
        eff_rv = round(spd_rec/fc_rec*100, 2) if fc_rec else None
        eff_bv = round(spd_base/fc_base*100, 2) if fc_base else None
        eff_str = None
        if eff_rv and eff_bv:
            delta = round(eff_rv - eff_bv, 2)
            eff_str = f"+{delta}%" if delta >= 0 else f"{delta}%"
        cad_str = None
        if cad_rec and cad_base:
            delta = round(cad_rec - cad_base, 1)
            cad_str = f"+{delta} rpm" if delta >= 0 else f"{delta} rpm"
        rec = "continuar Z2"
        if fatiga == "alta": rec = "reducir intensidad, sesión de recuperación"
        elif fatiga == "moderada": rec = "mantener Z2, evitar Z4-Z5 esta semana"
        elif fitness == "subiendo" and fatiga == "baja": rec = "buena forma, puedes agregar una sesión tempo Z3"
        return {"sport": sport, "fitness": fitness, "fatiga": fatiga,
                "aerobic_efficiency": eff_str, "eficiencia_reciente": eff_rv,
                "cadence_trend": cad_str, "cadencia_reciente_rpm": cad_rec,
                "km_ultimas_2_semanas": km_2w, "sesiones_2_semanas": int(d.get("sesiones_2w") or 0),
                "recommendation": rec}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/athlete-profile ─────────────────────────────────────────────────

@router.get("/gpt/athlete-profile")
def get_athlete_profile():
    """Perfil centralizado del atleta — edad, peso, FC, FTP, zonas, objetivos, lesiones."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM athlete_profile ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return {
                    "name": "Mars",
                    "age": 47,
                    "hr_lt": 168,
                    "zones": {
                        "Z1": "0-108 bpm",
                        "Z2": "134-150 bpm (Aeróbico)",
                        "Z3": "151-160 bpm (Tempo)",
                        "Z4": "161-168 bpm (Umbral)",
                        "Z5": "169+ bpm (Máximo)"
                    },
                    "active_bike": "Orbea Aluminum Road 2020 matte black",
                    "nota": "Perfil no guardado aún — usar POST /gpt/athlete-profile para inicializarlo"
                }
            cols = [d[0] for d in cur.description]
        profile = dict(zip(cols, row))
        for k, v in profile.items():
            if hasattr(v, "isoformat"):
                profile[k] = v.isoformat()
        lt = profile.get("hr_lt") or 168
        profile["zones"] = {
            "Z1": f"0-{lt-60} bpm (Recuperación)",
            "Z2": f"{lt-34}-{lt-18} bpm (Aeróbico)",
            "Z3": f"{lt-17}-{lt-8} bpm (Tempo)",
            "Z4": f"{lt-7}-{lt} bpm (Umbral)",
            "Z5": f"{lt+1}+ bpm (Máximo)"
        }
        return profile
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /gpt/athlete-profile ─────────────────────────────────────────────────

@router.post("/gpt/athlete-profile")
def save_athlete_profile(body: AthleteProfileIn):
    """Guarda o actualiza el perfil del atleta."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM athlete_profile LIMIT 1")
            exists = cur.fetchone()
            if exists:
                fields = {k: v for k, v in body.dict().items() if v is not None}
                if not fields:
                    raise HTTPException(400, "Sin campos para actualizar")
                set_clause = ", ".join(f"{k}=%s" for k in fields)
                set_clause += ", updated_at=NOW()"
                cur.execute(f"UPDATE athlete_profile SET {set_clause} WHERE id=%s",
                            list(fields.values()) + [exists[0]])
                return {"ok": True, "action": "updated", "fields": list(fields.keys())}
            else:
                cur.execute("""
                    INSERT INTO athlete_profile
                        (name, age, weight_kg, height_cm, hr_rest, hr_max, hr_lt,
                         ftp_watts, vo2max, active_bike_id, goals, injuries, notes, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """, ("Mars", body.age, body.weight_kg, body.height_cm,
                      body.hr_rest, body.hr_max, body.hr_lt or 168,
                      body.ftp_watts, body.vo2max, body.active_bike_id,
                      body.goals, body.injuries, body.notes))
                return {"ok": True, "action": "created"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /weight ──────────────────────────────────────────────────────────────

@router.post("/weight")
def add_weight(body: WeightIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_weight_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO weight_log (date,weight_kg,waist_cm,body_fat_pct,notes) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (body.date, body.weight_kg, body.waist_cm, body.body_fat_pct, body.notes)
            )
            wid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": wid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ── GET /weight/history ───────────────────────────────────────────────────────

@router.get("/weight/history")
def get_weight_history(limit: int = 60):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_weight_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date,weight_kg,waist_cm,body_fat_pct,notes FROM weight_log ORDER BY date DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        entries = [dict(zip(cols, r)) for r in rows]
        for e in entries:
            for k, v in e.items():
                if hasattr(v, "isoformat"): e[k] = v.isoformat()
                if hasattr(v, "__float__") and v is not None:
                    try: e[k] = float(v)
                    except Exception: pass
        asc = list(reversed(entries))
        delta = None
        if len(asc) >= 2:
            w0 = float(asc[0].get("weight_kg") or 0)
            w1 = float(asc[-1].get("weight_kg") or 0)
            if w0 and w1: delta = round(w1 - w0, 2)
        current = float(entries[0]["weight_kg"]) if entries and entries[0].get("weight_kg") else None
        return {"current_kg": current, "delta_kg": delta, "registros": len(entries), "historial": asc}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /gear/service ────────────────────────────────────────────────────────

@router.post("/gear/service")
def add_gear_service(body: GearServiceIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_gear_service_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gear_service (gear_id,gear_name,service_type,description,date,km_at_service,cost_mxn,shop,notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.gear_id, body.gear_name, body.service_type, body.description,
                  body.date, body.km_at_service, body.cost_mxn, body.shop, body.notes))
            sid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": sid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ── GET /gear/service-history ─────────────────────────────────────────────────

@router.get("/gear/service-history")
def get_gear_service_history(limit: int = 50):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_gear_service_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT gs.*, (CURRENT_DATE - gs.date) as days_since
                FROM gear_service gs ORDER BY gs.date DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        entries = []
        for r in rows:
            d = dict(zip(cols, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"): d[k] = v.isoformat()
                if hasattr(v, "__float__") and v is not None:
                    try: d[k] = float(v)
                    except Exception: pass
            entries.append(d)
        return {"registros": len(entries), "historial": entries}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /wellness ────────────────────────────────────────────────────────────

@router.post("/wellness")
def save_wellness(body: WellnessIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_wellness_table(conn)
    try:
        with conn.cursor() as cur:
            # Prevenir duplicado de morning check (hr_rest en misma fecha)
            if body.hr_rest:
                cur.execute(
                    "SELECT id FROM wellness WHERE date=%s AND hr_rest IS NOT NULL LIMIT 1",
                    (body.date,)
                )
                existing = cur.fetchone()
                if existing:
                    # Actualizar en lugar de insertar
                    cur.execute("""
                        UPDATE wellness SET hr_rest=%s, fatigue=%s, sleep_hours=%s,
                            notes=%s, source='manual_app', source_confidence=1.0,
                            is_subjective=TRUE
                        WHERE id=%s
                    """, (body.hr_rest, body.fatigue, body.sleep_hours, body.notes, existing[0]))
                    conn.commit()
                    return {"ok": True, "id": existing[0], "updated": True}
            cur.execute("""
                INSERT INTO wellness (date, category, compex_program, muscle_zone,
                    duration_min, ceragem_duration_min, ceragem_sensation_before,
                    ceragem_sensation_after, sleep_hours, sleep_quality, hr_rest,
                    garmin_sleep_score, pain_zone, pain_level, pain_start, pain_end,
                    pain_type, stress_level, stress_cause, notes, fatigue,
                    source, source_confidence, is_subjective)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'manual_app', 1.0, TRUE)
                RETURNING id
            """, (body.date, body.category, body.compex_program, body.muscle_zone,
                  body.duration_min, body.ceragem_duration_min,
                  body.ceragem_sensation_before, body.ceragem_sensation_after,
                  body.sleep_hours, body.sleep_quality, body.hr_rest,
                  body.garmin_sleep_score, body.pain_zone, body.pain_level,
                  body.pain_start, body.pain_end, body.pain_type,
                  body.stress_level, body.stress_cause, body.notes, body.fatigue))
            wid = cur.fetchone()[0]
        return {"ok": True, "id": wid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/wellness-summary ─────────────────────────────────────────────────

@router.get("/gpt/wellness-summary")
def gpt_wellness_summary(weeks: int = 4):
    """Resumen de recuperación — sueño, Compex, Ceragem, molestias activas, fatiga."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_wellness_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM wellness
                WHERE date >= CURRENT_DATE - (%s * INTERVAL '1 week')
                ORDER BY date DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        entries = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = str(v)
            entries.append(rd)

        sleep = [e for e in entries if e.get("sleep_hours")]
        sleep_avg = round(sum(float(e["sleep_hours"]) for e in sleep) / len(sleep), 1) if sleep else None
        hr_rest_avg = None
        hr_rows = [e for e in entries if e.get("hr_rest")]
        if hr_rows:
            hr_rest_avg = round(sum(int(e["hr_rest"]) for e in hr_rows) / len(hr_rows), 0)

        by_cat = {}
        for e in entries:
            cat = e.get("category", "otro")
            by_cat[cat] = by_cat.get(cat, 0) + 1

        with conn.cursor() as cur:
            cur.execute("""
                SELECT pain_zone, pain_level, pain_type, pain_start, pain_end, notes
                FROM wellness
                WHERE category='pain' AND pain_zone IS NOT NULL
                  AND (pain_end IS NULL OR pain_end >= CURRENT_DATE)
                ORDER BY pain_start DESC
            """)
            pain_rows = cur.fetchall()
            pain_cols = [d[0] for d in cur.description]
        active_pain = [dict(zip(pain_cols, r)) for r in pain_rows]
        for p in active_pain:
            for k, v in p.items():
                if hasattr(v, "isoformat"): p[k] = str(v)

        fatigue_entries = [e for e in entries if e.get("fatigue")]
        fatigue_avg = round(sum(int(e["fatigue"]) for e in fatigue_entries) / len(fatigue_entries), 1) if fatigue_entries else None

        ceragem = [e for e in entries if e.get("category") == "ceragem"]
        ceragem_delta = None
        if ceragem:
            antes = [float(e["ceragem_sensation_before"]) for e in ceragem if e.get("ceragem_sensation_before")]
            despues = [float(e["ceragem_sensation_after"]) for e in ceragem if e.get("ceragem_sensation_after")]
            if antes and despues:
                ceragem_delta = round(sum(despues)/len(despues) - sum(antes)/len(antes), 1)

        return {
            "semanas": weeks,
            "total_sesiones": len(entries),
            "por_categoria": by_cat,
            "sueno_promedio_horas": sleep_avg,
            "fc_reposo_promedio": hr_rest_avg,
            "fatiga_promedio": fatigue_avg,
            "ceragem_delta_sensacion": ceragem_delta,
            "molestias_activas": active_pain,
            "historial_reciente": entries[:15]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /fuerza ──────────────────────────────────────────────────────────────

@router.post("/fuerza")
def save_fuerza(body: FuerzaIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_fuerza_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fuerza (date, category, subcategory, muscle_groups,
                    intensity, duration_min, sets, reps, weight_kg, exercise,
                    notes, rpe, fatigue_before, fatigue_after)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.date, body.category, body.subcategory, body.muscle_groups,
                  body.intensity, body.duration_min, body.sets, body.reps,
                  body.weight_kg, body.exercise, body.notes, body.rpe,
                  body.fatigue_before, body.fatigue_after))
            fid = cur.fetchone()[0]
        return {"ok": True, "id": fid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/fuerza-summary ───────────────────────────────────────────────────

@router.get("/gpt/fuerza-summary")
def gpt_fuerza_summary(weeks: int = 8):
    """Resumen de fuerza — sesiones, progresión por músculo, Compex intensidad."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_fuerza_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, date, category, subcategory, muscle_groups,
                       intensity, duration_min, sets, reps, weight_kg,
                       exercise, rpe, notes
                FROM fuerza
                WHERE date >= CURRENT_DATE - (%s * INTERVAL '1 week')
                ORDER BY date DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        sessions = []
        for r in rows:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = str(v)
            sessions.append(rd)

        by_cat = {}
        for s in sessions:
            cat = s.get("category", "otro")
            if cat not in by_cat:
                by_cat[cat] = {"sesiones": 0, "minutos": 0}
            by_cat[cat]["sesiones"] += 1
            by_cat[cat]["minutos"] += int(s.get("duration_min") or 0)

        compex_progress = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('month', date), 'YYYY-MM') as mes,
                    UNNEST(muscle_groups) as musculo,
                    MAX(intensity) as max_intensity,
                    COUNT(*) as sesiones
                FROM fuerza
                WHERE category = 'compex' AND intensity IS NOT NULL
                GROUP BY mes, musculo ORDER BY mes ASC
            """)
            cp_rows = cur.fetchall()
        for mes, musculo, max_int, ses in cp_rows:
            if musculo not in compex_progress:
                compex_progress[musculo] = []
            compex_progress[musculo].append({
                "mes": mes,
                "max_intensity": int(max_int or 0),
                "sesiones": int(ses or 0)
            })

        total_ses = len(sessions)
        total_min = sum(int(s.get("duration_min") or 0) for s in sessions)

        return {
            "semanas": weeks,
            "total_sesiones": total_ses,
            "total_horas": round(total_min / 60, 1),
            "por_categoria": by_cat,
            "compex_progresion": compex_progress,
            "sesiones_recientes": sessions[:10]
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/gear-status ─────────────────────────────────────────────────────

@router.get("/gpt/gear-status")
def gpt_gear_status():
    """Estado completo del gear — odómetro por componente, alertas y costo/km."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    _ensure_gear_activity_links_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(distance_km),0) FROM sessions_clean_compat
                WHERE sport='cycling' AND start_time IS NOT NULL
            """)
            total_km_db = float(cur.fetchone()[0] or 0)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT g.gear_id, g.name, g.type, g.bike_id, g.brand, g.model,
                       g.installed_date, g.km_at_install, g.km_limit,
                       g.retired_date, g.notes,
                       COALESCE((SELECT MAX(km_at_service) FROM maintenance m
                                  WHERE m.gear_id=g.gear_id), g.km_at_install, 0) as last_service_km
                FROM gear g WHERE g.retired_date IS NULL
                ORDER BY g.type, g.name
            """)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        components = []
        for r in rows:
            d = dict(zip(cols, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"): d[k] = str(v)
            km_install = int(d.get("km_at_install") or 0)
            km_used = max(0, round(total_km_db) - km_install)
            km_limit = d.get("km_limit")
            pct = round(km_used / km_limit * 100, 1) if km_limit else None
            status = "green"
            if pct is not None:
                status = "red" if pct >= 85 else ("yellow" if pct >= 60 else "green")
            d["km_used"] = km_used
            d["km_used_source"] = "bike_odometer_estimate"
            d["km_used_note"] = "Aproximado por km total de bici menos km de instalación; será exacto cuando existan enlaces actividad-pieza."
            d["km_remaining"] = max(0, km_limit - km_used) if km_limit else None
            d["pct_used"] = pct
            d["status"] = status
            d["status_emoji"] = "🔴" if status == "red" else ("🟡" if status == "yellow" else "🟢")
            components.append(d)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_gear_id, name, type, model, status, date_begin, max_distance_km
                FROM garmin_export_gear
                ORDER BY type, name
            """)
            garmin_rows = cur.fetchall()
            garmin_cols = [d[0] for d in cur.description]
        garmin_catalog = []
        garmin_active = []
        garmin_retired = []
        existing_names = {str(c.get("name") or "").strip().lower() for c in components}
        for r in garmin_rows:
            d = dict(zip(garmin_cols, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"): d[k] = str(v)
            status_raw = str(d.get("status") or "").lower()
            is_retired = "retired" in status_raw or "inactive" in status_raw
            d["source"] = "garmin_export"
            d["gear_id"] = f"garmin:{d.get('source_gear_id')}"
            d["km_used"] = None
            d["km_remaining"] = None
            d["pct_used"] = None
            d["status_emoji"] = "retirado" if is_retired else "activo"
            d["km_limit"] = float(d["max_distance_km"]) if d.get("max_distance_km") is not None else None
            garmin_catalog.append(d)
            if is_retired:
                garmin_retired.append(d)
            else:
                garmin_active.append(d)
            if not is_retired and str(d.get("name") or "").strip().lower() not in existing_names:
                components.append(d)

        with conn.cursor() as cur:
            cur.execute("""
                SELECT type, description, date, km_at_service, cost_mxn, shop
                FROM maintenance ORDER BY date DESC LIMIT 10
            """)
            maint_rows = cur.fetchall()
            maint_cols = [d[0] for d in cur.description]
        maintenance = [dict(zip(maint_cols, r)) for r in maint_rows]
        for m in maintenance:
            for k, v in m.items():
                if hasattr(v, "isoformat"): m[k] = str(v)

        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(cost_mxn),0) FROM maintenance")
            total_cost = float(cur.fetchone()[0] or 0)
        cost_per_km = round(total_cost / total_km_db, 2) if total_km_db > 0 else 0

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM accidents ORDER BY date DESC")
            acc_rows = cur.fetchall()
            acc_cols = [d[0] for d in cur.description]
        accidents = [dict(zip(acc_cols, r)) for r in acc_rows]
        for a in accidents:
            for k, v in a.items():
                if hasattr(v, "isoformat"): a[k] = str(v)

        alerts = [c for c in components if c["status"] in ("red", "yellow")]
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gear_activity_links")
            gear_links_count = int(cur.fetchone()[0] or 0)

        return {
            "total_km_bici": round(total_km_db),
            "componentes": components,
            "garmin_catalog": garmin_catalog,
            "garmin_active": garmin_active,
            "garmin_retired": garmin_retired,
            "gear_model_note": "Garmin export trae catalogo de piezas, pero no siempre liga actividad a pieza.",
            "future_component_km_model": {
                "table_ready": True,
                "existing_links": gear_links_count,
                "activity_gear_links": ["session_id", "source_activity_id", "gear_id", "role"],
                "component_distance_rollup": "sumar distance_km de clean_sessions enlazadas a cada gear_id"
            },
            "alertas": alerts,
            "mantenimiento_reciente": maintenance,
            "costo_total_mxn": total_cost,
            "costo_por_km_mxn": cost_per_km,
            "accidentes": accidents
        }
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /gear ────────────────────────────────────────────────────────────────

@router.post("/gear")
def add_gear(body: GearIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    gear_id = body.gear_id or str(uuid.uuid4())[:8]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO gear (gear_id, name, type, bike_id, installed_date,
                    km_at_install, km_limit, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (gear_id) DO UPDATE SET
                    name=EXCLUDED.name, type=EXCLUDED.type,
                    installed_date=EXCLUDED.installed_date,
                    km_at_install=EXCLUDED.km_at_install,
                    km_limit=EXCLUDED.km_limit, notes=EXCLUDED.notes
            """, (gear_id, body.name, body.type, body.bike_id or "orbea-avant-2019",
                  body.installed_date, body.km_at_install or 0, body.km_limit, body.notes))
        return {"ok": True, "gear_id": gear_id}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /maintenance ─────────────────────────────────────────────────────────

@router.post("/maintenance")
def add_maintenance(body: MaintenanceIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO maintenance (bike_id, gear_id, type, description, date,
                    km_at_service, cost_mxn, shop, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (body.bike_id, body.gear_id, body.type, body.description,
                  body.date, body.km_at_service, body.cost_mxn, body.shop, body.notes))
            mid = cur.fetchone()[0]
        return {"ok": True, "id": mid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /accidents ───────────────────────────────────────────────────────────

@router.post("/accidents")
def add_accident(body: AccidentIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_accidents_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO accidents (date, description, damage, repair,
                    cost_mxn, km_at_accident, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.date, body.description, body.damage, body.repair,
                  body.cost_mxn, body.km_at_accident, body.notes))
            aid = cur.fetchone()[0]
        return {"ok": True, "id": aid}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── POST /nutrition ───────────────────────────────────────────────────────────

@router.post("/nutrition")
def add_nutrition(body: NutritionIn):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_nutrition_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO nutrition_log
                    (date, session_id, moment, gel_type, gel_count, agua_ml, carbos_g, notas, gi_response, energy_response)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (body.date, body.session_id, body.moment, body.gel_type,
                  body.gel_count, body.agua_ml, body.carbos_g, body.notas,
                  body.gi_response, body.energy_response))
            nid = cur.fetchone()[0]
        conn.commit()
        return {"ok": True, "id": nid}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ── GET /nutrition/summary ────────────────────────────────────────────────────

@router.get("/nutrition/summary")
def get_nutrition_summary(weeks: int = 8):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    _ensure_nutrition_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT gel_type, COUNT(*) as usos,
                       AVG(gel_count) as avg_gels,
                       AVG(carbos_g) as avg_carbos,
                       COUNT(CASE WHEN gi_response IN ('nausea','inflamacion') THEN 1 END) as gi_issues
                FROM nutrition_log
                WHERE date >= CURRENT_DATE - (%s || ' weeks')::interval::interval
                  AND gel_type IS NOT NULL
                GROUP BY gel_type ORDER BY usos DESC
            """, (weeks,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        gels = [dict(zip(cols, r)) for r in rows]
        for g in gels:
            for k, v in g.items():
                if hasattr(v, "__float__") and v is not None:
                    try: g[k] = float(v)
                    except Exception: pass
        return {"semanas": weeks, "por_tipo": gels}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── DELETE endpoints ──────────────────────────────────────────────────────────

@router.delete("/fuerza/{record_id}")
def delete_fuerza(record_id: int):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fuerza WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@router.delete("/wellness/{record_id}")
def delete_wellness_record(record_id: int):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wellness WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@router.delete("/nutrition/{record_id}")
def delete_nutrition_record(record_id: int):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM nutrition_log WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@router.delete("/gear/service/{record_id}")
def delete_gear_service(record_id: int):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gear_service WHERE id=%s", (record_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


@router.delete("/gear/{gear_id}")
def delete_gear_component(gear_id: str):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gear WHERE gear_id=%s", (gear_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))


# ── GET /api/fuerza-records, /api/wellness-records ────────────────────────────

@router.get("/api/fuerza-records")
def api_fuerza_records(limit: int = 10):
    conn = get_db()
    if not conn:
        return {"records": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, date::text, category, subcategory,
                       muscle_group, intensity, duration_min, notes
                FROM fuerza ORDER BY date DESC, id DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"records": [dict(zip(cols, r)) for r in rows]}
    except Exception:
        return {"records": []}


@router.get("/api/wellness-records")
def api_wellness_records(limit: int = 10):
    conn = get_db()
    if not conn:
        return {"records": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, date::text, category, sleep_hours,
                       hr_rest, fatigue, pain_zone, pain_level, notes
                FROM wellness ORDER BY date DESC, id DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"records": [dict(zip(cols, r)) for r in rows]}
    except Exception:
        return {"records": []}
