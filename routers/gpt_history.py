"""
routers/gpt_history.py — Historical progress & performance tests
TD-010A split de gpt_analytics.py
Endpoints: /gpt/month-summary, /gpt/historical-progress, /gpt/month-compare,
           /gpt/fitness-timeline, /gpt/athletic-history, POST+GET /gpt/tests
"""
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException
from db import get_db
from shared.models import AthleteTestIn

logger = logging.getLogger("mars_fit")
router = APIRouter(tags=["gpt_history"])


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
                aerobic_signal = "✅ Motor aeróbico mejorando: %.1f km/h con %.0f bpm" % (v, fc)
            elif v >= 20:
                aerobic_signal = "⚠️ %.1f km/h pero FC aún en %.0f bpm — seguir en Z2" % (v, fc)
        return {
            "sport": sport, "mes_base": month_a, "mes_actual": month_b,
            "stats_base": stats_a, "stats_actual": stats_b,
            "deltas": deltas, "senal_aerobica": aerobic_signal
        }
    except Exception as e:
        raise HTTPException(500, str(e))


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
            "2021-2022: aparecen bloques de natación, movilidad/yoga y menor volumen de carrera.",
            "2023-2026: ciclismo y fuerza toman más peso, con clean_sessions como índice maestro vivo.",
        ]
        return {
            "ok": True, "totals": totals, "by_group": by_group,
            "raw_sports": raw_sports, "yearly": yearly,
            "running": running, "cycling": cycling,
            "swimming": swimming, "strength": strength, "story": story,
        }
    except Exception as e:
        raise HTTPException(500, str(e))


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
