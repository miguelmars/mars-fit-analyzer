"""
routers/gpt_dashboard.py — Dashboard, status, correlations & trends
TD-010A split de gpt_analytics.py
Endpoints: /gpt/dashboard, /gpt/calendar-heatmap, POST /gpt/rebuild-snapshots,
           /gpt/athletic-status, /gpt/correlaciones, /gpt/correlations, /gpt/tendencia
"""
import logging
from fastapi import APIRouter, HTTPException
from db import get_db

logger = logging.getLogger("mars_fit")
router = APIRouter(tags=["gpt_dashboard"])


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
    "cycling": "ciclismo", "indoor_cycling": "bici interior",
    "running": "correr", "trail_running": "trail running",
    "treadmill_running": "caminadora", "lap_swimming": "natacion",
    "open_water_swimming": "aguas abiertas", "indoor_cardio": "cardio interior",
    "strength_training": "fuerza", "walking": "caminata",
    "yoga": "movilidad", "multi_sport": "multideporte",
    "bikeToRunTransition_v2": "transicion bici-correr",
}


def _athletic_load_hours(sport_breakdown):
    breakdown = sport_breakdown or {}
    return sum(
        float((values or {}).get("hours") or 0) * ATHLETIC_LOAD_WEIGHTS.get(sport, 0.65)
        for sport, values in breakdown.items()
    )


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
        fitness = "estable"
        if km_4w > 0:
            p = (km_2w - km_4w) / km_4w * 100
            fitness = "subiendo" if p > 10 else ("bajando" if p < -15 else "estable")
        fatiga = "baja"
        if fc_base > 0 and spd_base > 0:
            er = spd_rec / fc_rec if fc_rec else 0
            eb = spd_base / fc_base
            if eb > 0:
                ep = (er - eb) / eb * 100
                fatiga = "alta" if ep < -5 else ("moderada" if ep < -2 else "baja")
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
        carga_estado = "fresco" if tsb > 5 else ("recuperar" if tsb < -15 else "en carga")
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
            "nota": "Estimación por duración y FC; se refina cuando tengamos potencia."
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
            "nota": "Óptimo: 70-80%"
        }
        rec = "continuar Z2"
        if fatiga == "alta":
            rec = "reducir intensidad — sesión de recuperación activa"
        elif fatiga == "moderada":
            rec = "mantener Z2, evitar Z4-Z5"
        elif fitness == "subiendo" and fatiga == "baja":
            rec = "buena forma — puedes agregar una sesión tempo Z3"
        elif result["semana_actual"]["sesiones"] == 0:
            rec = "sin sesiones esta semana — retomar entrenamiento"
        result["recommendation"] = rec
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


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
            return {"ok": False, "message": "Sin sesiones en clean_sessions todavía", "count": 0}
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
            direction = "ascendente_con_cautela"
            explanation = (
                "Volviste a entrenar con regularidad despues de un bloque de carga baja. "
                "La prioridad es sostener esta frecuencia antes de volver a subir."
            )
            recommendation = "Mantener una semana similar, sin aumentar volumen ni intensidad."
        elif load_delta is not None and load_delta > 60 and discipline_change:
            state = "transicion_de_disciplina"
            direction = "ascendente_con_cautela"
            explanation = (
                "La carga subio al pasar de "
                f"{ATHLETIC_SPORT_NAMES.get(previous_summary['disciplina_dominante'], previous_summary['disciplina_dominante'])} a "
                f"{ATHLETIC_SPORT_NAMES.get(recent_summary['disciplina_dominante'], recent_summary['disciplina_dominante'])}. "
                "El porcentaje refleja un cambio de disciplina, no solo mas entrenamiento."
            )
            recommendation = "Consolidar la nueva disciplina antes de aumentar otra vez la carga."
        elif load_delta is not None and load_delta > 60:
            state = "pico_de_carga"
            direction = "en_observacion"
            explanation = "La carga total crecio rapidamente frente al bloque anterior."
            recommendation = "Consolidar la carga y priorizar recuperacion antes de otro aumento."
        elif load_delta is not None and load_delta < -40:
            state = "descarga_o_pausa"
            direction = "descendente"
            explanation = "La carga total bajo claramente durante las ultimas cuatro semanas."
            recommendation = "Distinguir si es una descarga planeada o una perdida de continuidad."
        elif active_streak >= 4 and active_days_delta >= 0:
            state = "continuidad_solida"
            direction = "estable_positiva"
            explanation = "La frecuencia se mantiene y ya existe una base continua de varias semanas."
            recommendation = "Mantener la constancia y cambiar solo una variable a la vez."
        else:
            state = "construyendo_continuidad"
            direction = "estable"
            explanation = "La actividad esta presente, pero la continuidad todavia puede consolidarse."
            recommendation = "Priorizar dias activos regulares antes de perseguir mas volumen."

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
            state = "sin_actividad_reciente"; direction = "pausa"
            explanation = f"No hay sesiones recientes de {sport_label}. La historia sigue disponible, pero no existe una tendencia actual que interpretar."
        elif previous_sport_hours <= 0:
            state = "regreso_tras_pausa"; direction = "ascendente_con_cautela"
            explanation = f"Las sesiones recientes de {sport_label} representan un regreso tras un bloque sin actividad."
        elif overload:
            state = "posible_sobrecarga"; direction = "descendente"
            explanation = "La FC subio mientras la eficiencia bajo; conviene reducir carga y observar recuperacion."
        elif extreme_load and load_context == "regreso_tras_pausa":
            state = "regreso_tras_pausa"; direction = "ascendente_con_cautela"
            explanation = "El aumento porcentual es grande porque el bloque anterior tuvo muy poco volumen. Es un regreso tras pausa: la respuesta aerobica es positiva, pero conviene consolidar."
        elif extreme_load:
            state = "pico_atipico"; direction = "en_observacion"
            explanation = "El volumen reciente es un pico atipico frente al bloque anterior. No conviene usar el porcentaje aislado ni volver a aumentar la carga de inmediato."
        elif abrupt_load and efficiency_delta is not None and efficiency_delta >= 2:
            state = "respuesta_positiva_carga_alta"; direction = "ascendente_con_cautela"
            explanation = "La eficiencia mejoro, pero el volumen subio bruscamente. La respuesta es positiva; conviene consolidar la carga antes de aumentarla otra vez."
        elif abrupt_load:
            state = "carga_en_observacion"; direction = "incierta"
            explanation = "El volumen subio bruscamente y todavia no hay una mejora aerobica clara. Conviene observar recuperacion y no aumentar carga esta semana."
        elif score >= 2:
            state = "mejorando"; direction = "ascendente"
            explanation = "La respuesta aerobica reciente mejora frente al bloque anterior."
        elif score <= -2:
            state = "retrocediendo"; direction = "descendente"
            explanation = "Dos o mas senales empeoraron frente a las cuatro semanas anteriores."
        else:
            state = "estable"; direction = "lateral"
            explanation = "Los cambios recientes son mixtos o pequenos; no hay una direccion fuerte."

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
