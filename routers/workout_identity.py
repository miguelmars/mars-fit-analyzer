"""
V7.1 — Workout Identity & Progression.

La idea central del fundador: el mismo workout se repite dentro del plan y en
las mismas rutas, pero con INTENCIÓN distinta (Z2 relax vs intervalos). Strava
agrupa solo por GPS y castiga el día "lento". Epoch agrupa por
**ruta + intención + estructura** y mide la evolución entre repeticiones.

Endpoints (read-only, cero escrituras):
  GET /gpt/session/{id}/progression  → ¿mejoré en ESTE workout vs sus repeticiones?
  GET /gpt/workout-groups            → grupos de repeticiones detectados
"""
import logging

from fastapi import APIRouter, HTTPException, Query

from db import get_db
from routers.gpt_training_context import (_ensure_training_tables,
                                          _session_type_from_row, _zone_for_hr)

logger = logging.getLogger("epoch.workout_identity")
router = APIRouter()

_INTENT_LABEL = {"intervals": "intervals", "high_intensity": "high intensity",
                 "tempo": "tempo", "endurance": "endurance/Z2",
                 "recovery": "recovery", "climb": "climbing"}

# Modalidad: no es lo mismo calle (tráfico, topes) que rodillo (datos limpios)
_MODALITY = {"Ride": ("road/street", "traffic, bumps and lights cut the ride"),
             "VirtualRide": ("trainer", "clean data — no traffic, no terrain"),
             "MountainBikeRide": ("MTB", "technical terrain — speed not comparable to road"),
             "GravelRide": ("gravel", "loose surface — more effort at equal speed"),
             "EBikeRide": ("e-bike", "electric assist — effort not comparable"),
             "Run": ("running", None), "Walk": ("walking", None)}


def _structure_signature(n_laps, work_laps, med_work_s):
    """Firma de estructura: '3x~5min', 'steady', '2 bloques'. Agrupa workouts
    aunque cambie el nombre."""
    if not n_laps or n_laps <= 1:
        return "continua"
    if work_laps and work_laps >= 2 and med_work_s:
        mins = max(1, round(med_work_s / 60))
        return f"{work_laps}x~{mins}min"
    return f"{n_laps}_bloques"


def _load_session_identity(cur, clean_session_id):
    cur.execute("""
        SELECT cs.clean_session_id, cs.name, cs.sport_type, cs.start_date,
               cs.route_id, cs.distance_km, cs.duration_s, cs.avg_hr_bpm,
               cs.avg_speed_kmh, cs.efficiency_speed_hr, cs.ascent_m,
               cs.avg_cadence,
               COUNT(sl.lap_id) FILTER (WHERE sl.lap_index >= 0),
               COUNT(sl.lap_id) FILTER (WHERE sl.lap_type='work'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY sl.duration_s)
                   FILTER (WHERE sl.lap_type='work')
        FROM clean_sessions cs
        LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
        WHERE cs.clean_session_id = %s
        GROUP BY cs.clean_session_id
    """, (clean_session_id,))
    r = cur.fetchone()
    if not r:
        return None
    avg_hr = float(r[7]) if r[7] else None
    intent = _session_type_from_row(avg_hr, r[10], float(r[5]) if r[5] else 0,
                                    r[6], r[13], r[12])
    return {
        "clean_session_id": r[0], "name": r[1], "sport_type": r[2],
        "date": r[3].isoformat() if r[3] else None,
        "route_id": r[4],
        "distance_km": float(r[5]) if r[5] else None,
        "duration_s": r[6], "avg_hr_bpm": avg_hr,
        "avg_speed_kmh": float(r[8]) if r[8] else None,
        "efficiency": float(r[9]) if r[9] else None,
        "ascent_m": r[10],
        "avg_cadence": float(r[11]) if r[11] else None,
        "n_laps": r[12], "work_laps": r[13],
        "intent": intent,
        "signature": _structure_signature(r[12], r[13],
                                          float(r[14]) if r[14] else None),
    }


def _load_temps(cur, ids):
    """Contexto ambiental por sesión: temperatura y pausas (streams summary)."""
    if not ids:
        return {}
    cur.execute("""
        SELECT clean_session_id, temp_avg_c, pauses_count, paused_time_s, wind_kmh
        FROM session_streams_summary
        WHERE clean_session_id = ANY(%s)
    """, (list(ids),))
    return {r[0]: {"temp": float(r[1]) if r[1] is not None else None,
                   "pauses": r[2], "paused_s": r[3],
                   "wind": float(r[4]) if r[4] is not None else None} for r in cur.fetchall()}


def _load_repetitions(cur, ref, limit=12):
    """Repeticiones del mismo workout: misma ruta + misma intención.
    Si no hay route_id, cae a misma firma de estructura + mismo deporte."""
    cur.execute("""
        SELECT cs.clean_session_id, cs.start_date, cs.distance_km, cs.duration_s,
               cs.avg_hr_bpm, cs.avg_speed_kmh, cs.efficiency_speed_hr, cs.ascent_m,
               cs.avg_cadence,
               COUNT(sl.lap_id) FILTER (WHERE sl.lap_index >= 0),
               COUNT(sl.lap_id) FILTER (WHERE sl.lap_type='work'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY sl.duration_s)
                   FILTER (WHERE sl.lap_type='work'),
               AVG(sl.avg_hr_bpm) FILTER (WHERE sl.lap_type='work'),
               AVG(sl.avg_speed_kmh) FILTER (WHERE sl.lap_type='work')
        FROM clean_sessions cs
        LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
        WHERE cs.sport_type = %s
          AND cs.clean_session_id != %s
          AND (%s::text IS NOT NULL AND cs.route_id = %s)
        GROUP BY cs.clean_session_id
        ORDER BY cs.start_date DESC
        LIMIT 60
    """, (ref["sport_type"], ref["clean_session_id"],
          ref["route_id"], ref["route_id"]))
    reps = []
    for r in cur.fetchall():
        avg_hr = float(r[4]) if r[4] else None
        intent = _session_type_from_row(avg_hr, r[7], float(r[2]) if r[2] else 0,
                                        r[3], r[10], r[9])
        if intent != ref["intent"]:
            continue  # misma ruta pero OTRA intención — no comparar
        reps.append({
            "clean_session_id": r[0],
            "date": r[1].isoformat() if r[1] else None,
            "distance_km": float(r[2]) if r[2] else None,
            "duration_s": r[3], "avg_hr_bpm": avg_hr,
            "avg_speed_kmh": float(r[5]) if r[5] else None,
            "efficiency": float(r[6]) if r[6] else None,
            "avg_cadence": float(r[8]) if r[8] else None,
            "n_laps": r[9], "work_laps": r[10],
            "work_hr": round(float(r[12]), 1) if r[12] else None,
            "work_speed_kmh": round(float(r[13]), 2) if r[13] else None,
        })
        if len(reps) >= limit:
            break
    return reps


@router.get("/gpt/session/{clean_session_id}/progression")
def session_progression(clean_session_id: str):
    """¿Mejoré en ESTE workout? Compara contra repeticiones de la misma
    ruta + misma intención. El día Z2 'lento' se compara contra otros Z2."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        ref = _load_session_identity(cur, clean_session_id)
        if not ref:
            raise HTTPException(404, "Session not found")
        if not ref["route_id"]:
            return {"ok": True, "session": ref, "repetitions": [],
                    "comparison": None,
                    "explanation_text": ("OBSERVATION: This session has no recorded "
                                         "route. INTERPRETATION: Without a route there is no "
                                         "clean comparison group. MISSING: "
                                         "route_id — your usual routes do have it.")}
        reps = _load_repetitions(cur, ref)
        # Contexto ambiental: la misma ruta a 31°C no es la misma ruta a 20°C
        _ctx = _load_temps(cur, [ref["clean_session_id"]] + [r["clean_session_id"] for r in reps])
        _rc = _ctx.get(ref["clean_session_id"]) or {}
        ref["temp_avg_c"] = _rc.get("temp")
        ref["pauses"] = _rc.get("pauses")
        ref["paused_s"] = _rc.get("paused_s")
        ref["wind_kmh"] = _rc.get("wind")
        for r in reps:
            _c = _ctx.get(r["clean_session_id"]) or {}
            r["temp_avg_c"] = _c.get("temp")
            r["pauses"] = _c.get("pauses")

    intent_label = _INTENT_LABEL.get(ref["intent"], ref["intent"])
    if not reps:
        return {"ok": True, "session": ref, "repetitions": [], "comparison": None,
                "explanation_text": (f"OBSERVATION: First {intent_label} session "
                                     f"recorded on this route. INTERPRETATION: No repetitions "
                                     f"to compare yet — this one becomes the baseline.")}

    # Comparar contra el promedio de las últimas 3 repeticiones previas
    prev = reps[:3]
    def _avg(key):
        vals = [r[key] for r in prev if r.get(key)]
        return sum(vals) / len(vals) if vals else None
    base_speed, base_hr, base_eff = _avg("avg_speed_kmh"), _avg("avg_hr_bpm"), _avg("efficiency")

    deltas = {}
    if ref["avg_speed_kmh"] and base_speed:
        deltas["speed_kmh"] = round(ref["avg_speed_kmh"] - base_speed, 2)
    if ref["avg_hr_bpm"] and base_hr:
        deltas["hr_bpm"] = round(ref["avg_hr_bpm"] - base_hr, 1)
    if ref["efficiency"] and base_eff:
        deltas["efficiency"] = round(ref["efficiency"] - base_eff, 5)
        deltas["efficiency_pct"] = round((ref["efficiency"] - base_eff) / base_eff * 100, 1)

    # Veredicto según la INTENCIÓN (la medalla correcta para cada tipo de día)
    verdict = "estable"
    medal = None
    if ref["intent"] in ("endurance", "recovery"):
        # Medalla Z2: menos FC a velocidad similar, o más eficiencia
        if deltas.get("efficiency_pct", 0) > 2:
            verdict, medal = "mejorando", f"More speed per heartbeat than your last {len(prev)} {intent_label} here (+{deltas['efficiency_pct']}%)."
        elif deltas.get("hr_bpm", 0) < -3 and abs(deltas.get("speed_kmh", 0)) < 1.5:
            verdict, medal = "mejorando", f"Same speed at {abs(deltas['hr_bpm'])} bpm less — your aerobic engine is growing."
        elif deltas.get("efficiency_pct", 0) < -3:
            verdict = "por_debajo"
    else:
        # Medalla intervalos/tempo: más works completados, más velocidad en works
        cur_w, prev_w = ref.get("work_laps") or 0, _avg("work_laps")
        if prev_w and cur_w > prev_w:
            verdict, medal = "mejorando", f"You completed {cur_w} work blocks (previously ~{round(prev_w)})."
        elif deltas.get("speed_kmh", 0) > 0.8 and deltas.get("hr_bpm", 99) <= 2:
            verdict, medal = "mejorando", f"+{deltas['speed_kmh']} km/h at the same cardiac cost."
        elif deltas.get("speed_kmh", 0) < -1.5 and deltas.get("hr_bpm", 0) >= 0:
            verdict = "por_debajo"

    # ── Clima: contexto que explica, no que castiga (ni regala) ─────────────
    _rep_temps = [r["temp_avg_c"] for r in prev if r.get("temp_avg_c") is not None]
    base_temp = sum(_rep_temps) / len(_rep_temps) if _rep_temps else None
    climate_note = ""
    climate = None
    if ref.get("temp_avg_c") is not None and base_temp is not None:
        dt = ref["temp_avg_c"] - base_temp
        climate = {"session_c": round(ref["temp_avg_c"], 1),
                   "baseline_c": round(base_temp, 1), "delta_c": round(dt, 1)}
        if dt >= 5:
            climate_note = (f" CONTEXT: today was {climate['session_c']}°C vs ~{climate['baseline_c']}°C "
                            "across your repetitions — in heat, HR runs higher and the body spends energy cooling. "
                            "The day counts as a record, not a regression.")
            if verdict == "por_debajo":
                verdict = "condiciones_distintas"
        elif dt <= -5:
            climate_note = (f" CONTEXT: today was {climate['session_c']}°C vs ~{climate['baseline_c']}°C "
                            "— in cool air HR rises less and you ride faster. Part of the gain "
                            "may be the weather; the multi-repetition trend is what counts.")
    # Viento: en Irlanda (y en todos lados) el aire también pedalea
    wind_note = ""
    if ref.get("wind_kmh") is not None and ref["wind_kmh"] >= 25:
        wind_note = (f" WIND: {ref['wind_kmh']} km/h that day — speed is not "
                     "comparable against calm days; your effort still counts in full.")
        if verdict == "por_debajo":
            verdict = "condiciones_distintas"
    # Terreno/modalidad: la calle corta la rodada; el rodillo no
    _mod = _MODALITY.get(ref.get("sport_type") or "", (None, None))
    terrain_note = ""
    if ref.get("paused_s") and ref["paused_s"] >= 180:
        terrain_note = (f" TERRAIN: the street cut the ride {ref.get('pauses') or '?'} times "
                        f"({round(ref['paused_s']/60)} min stopped) — Epoch uses moving "
                        "speed, so the traffic light does not punish you.")
    elif _mod[0] == "rodillo":
        terrain_note = " TERRAIN: trainer — clean data, direct comparison."
    obs = (f"OBSERVATION: {intent_label.capitalize()} on a known route — "
           f"repetition #{len(reps)+1}. Today: {ref['avg_speed_kmh'] or '—'} km/h, "
           f"HR {round(ref['avg_hr_bpm']) if ref['avg_hr_bpm'] else '—'}"
           + (f", {climate['session_c']}°C" if climate else "")
           + (f" · {_mod[0]}" if _mod[0] else "") + ".")
    if verdict == "mejorando":
        interp = f"INTERPRETATION: {medal}"
    elif verdict == "condiciones_distintas":
        interp = ("INTERPRETATION: The numbers came out lower, but conditions were not "
                  "comparable — not a regression, just a different day.")
    elif verdict == "por_debajo":
        interp = ("INTERPRETATION: Below your recent repetitions — could be "
                  "fatigue, heat or an off day. One repetition is not a trend.")
    else:
        interp = "INTERPRETATION: In line with your recent repetitions — consistency builds too."
    nota = (" NOTE: This day was " + intent_label + " on purpose — it is compared only "
            "against equal days, never against your fast days.") if ref["intent"] in ("endurance", "recovery") else ""

    return {
        "ok": True,
        "session": ref,
        "group_key": {"route_id": ref["route_id"], "intent": ref["intent"],
                      "signature": ref["signature"]},
        "repetitions": reps,
        "comparison": {"baseline": f"last {len(prev)} repetitions",
                       "deltas": deltas, "verdict": verdict, "medal": medal},
        "climate": climate,
        "modality": _mod[0],
        "explanation_text": obs + " " + interp + nota + climate_note + wind_note + terrain_note,
    }


@router.get("/gpt/workout-groups")
def workout_groups(min_reps: int = Query(3, ge=2)):
    """Grupos de workouts repetidos (ruta + intención). Para la vista de
    progresión y para Epochs (V7.4)."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cs.route_id, cs.sport_type, COUNT(*) AS n,
                   MIN(cs.start_date), MAX(cs.start_date),
                   AVG(cs.avg_hr_bpm), AVG(cs.avg_speed_kmh),
                   AVG(cs.efficiency_speed_hr), MAX(rt.name)
            FROM clean_sessions cs
            LEFT JOIN routes rt ON rt.route_id = cs.route_id
            WHERE cs.route_id IS NOT NULL
            GROUP BY cs.route_id, cs.sport_type
            HAVING COUNT(*) >= %s
            ORDER BY n DESC
            LIMIT 30
        """, (min_reps,))
        rows = cur.fetchall()
    groups = [{"route_id": r[0], "route_name": r[8], "sport_type": r[1], "repetitions": r[2],
               "first": r[3].isoformat() if r[3] else None,
               "last": r[4].isoformat() if r[4] else None,
               "avg_hr": round(float(r[5]), 1) if r[5] else None,
               "avg_speed_kmh": round(float(r[6]), 2) if r[6] else None,
               "avg_efficiency": round(float(r[7]), 5) if r[7] else None}
              for r in rows]
    return {"ok": True, "groups": groups,
            "nota": ("v1 grouping by route. Intent is evaluated per session in "
                     "/gpt/session/{id}/progression — the same route group contains "
                     "Z2 days and interval days and they are NEVER compared to each other.")}


# ── Fase 2: tendencia de largo plazo por grupo (ruta + intención) ────────────

@router.get("/gpt/workout-group/trend")
def workout_group_trend(route_id: str = Query(...),
                        sport: str = Query("Ride"),
                        intent: str = Query(None,
                            description="endurance|intervals|tempo|recovery|climb|high_intensity. "
                                        "Empty = dominant intent of the group")):
    """
    Serie completa de repeticiones de un workout (ruta + intención) y su
    tendencia de largo plazo: ¿este workout mejora a lo largo de las semanas?
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT cs.clean_session_id, cs.start_date, cs.distance_km, cs.duration_s,
                   cs.avg_hr_bpm, cs.avg_speed_kmh, cs.efficiency_speed_hr, cs.ascent_m,
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_index >= 0),
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_type='work')
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.route_id = %s AND cs.sport_type = %s
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_date ASC
        """, (route_id, sport))
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(404, "No sessions for that route/sport")

    # Clasificar intención por sesión y agrupar
    by_intent = {}
    for r in rows:
        avg_hr = float(r[4]) if r[4] else None
        it = _session_type_from_row(avg_hr, r[7], float(r[2]) if r[2] else 0,
                                    r[3], r[9], r[8])
        by_intent.setdefault(it, []).append({
            "clean_session_id": r[0],
            "date": r[1].isoformat() if r[1] else None,
            "avg_hr_bpm": avg_hr,
            "avg_speed_kmh": float(r[5]) if r[5] else None,
            "efficiency": float(r[6]) if r[6] else None,
            "work_laps": r[9],
        })

    if not intent:
        intent = max(by_intent, key=lambda k: len(by_intent[k]))
    series = by_intent.get(intent, [])
    intent_label = _INTENT_LABEL.get(intent, intent)

    if len(series) < 4:
        return {"ok": True, "route_id": route_id, "intent": intent,
                "repetitions": len(series), "series": series,
                "available_intents": {k: len(v) for k, v in by_intent.items()},
                "trend": None,
                "explanation_text": (f"OBSERVATION: Only {len(series)} {intent_label} repetitions "
                                     f"on this route. INTERPRETATION: ~4 are needed "
                                     f"for an honest trend.")}

    # Tendencia: últimas 5 vs 5 anteriores (o mitades si hay pocas)
    n = len(series)
    k = min(5, n // 2)
    recent, earlier = series[-k:], series[-2*k:-k]

    def _avg(arr, key):
        vals = [s[key] for s in arr if s.get(key)]
        return sum(vals) / len(vals) if vals else None

    trend = {"window": f"last {k} vs previous {k}", "repetitions": n,
             "first_date": series[0]["date"], "last_date": series[-1]["date"]}
    e_r, e_e = _avg(recent, "efficiency"), _avg(earlier, "efficiency")
    h_r, h_e = _avg(recent, "avg_hr_bpm"), _avg(earlier, "avg_hr_bpm")
    s_r, s_e = _avg(recent, "avg_speed_kmh"), _avg(earlier, "avg_speed_kmh")
    if e_r and e_e:
        trend["efficiency_pct"] = round((e_r - e_e) / e_e * 100, 1)
    if h_r and h_e:
        trend["hr_delta_bpm"] = round(h_r - h_e, 1)
    if s_r and s_e:
        trend["speed_delta_kmh"] = round(s_r - s_e, 2)

    # Veredicto por intención
    verdict = "estable"
    reading = ""
    eff_pct = trend.get("efficiency_pct", 0) or 0
    hr_d = trend.get("hr_delta_bpm", 0) or 0
    sp_d = trend.get("speed_delta_kmh", 0) or 0
    if intent in ("endurance", "recovery"):
        if eff_pct > 2 or (hr_d < -3 and sp_d > -1):
            verdict = "mejorando"
            reading = (f"Your body produces more speed per heartbeat than {k} repetitions ago "
                       f"({'+' if eff_pct>0 else ''}{eff_pct}% eficiencia"
                       + (f", HR {hr_d} bpm" if hr_d else "") + "). The aerobic engine is growing.")
        elif eff_pct < -3:
            verdict = "retrocediendo"
            reading = (f"Efficiency dropped {abs(eff_pct)}% vs your earlier repetitions — "
                       "could be accumulated fatigue, heat or a recent pause. Worth checking recovery.")
        else:
            reading = "Efficiency steady across repetitions — the base is holding."
    else:
        w_r, w_e = _avg(recent, "work_laps"), _avg(earlier, "work_laps")
        if (w_r or 0) > (w_e or 0) or (sp_d > 0.5 and hr_d <= 1):
            verdict = "mejorando"
            reading = (f"You sustain more work at the same cost: "
                       + (f"{round(w_r,1)} vs {round(w_e,1)} blocks · " if w_r and w_e else "")
                       + f"{'+' if sp_d>0 else ''}{sp_d} km/h with HR {'+' if hr_d>0 else ''}{hr_d} bpm.")
        elif sp_d < -1 and hr_d >= 0:
            verdict = "retrocediendo"
            reading = "Less speed at the same cardiac cost in recent repetitions."
        else:
            reading = "Steady execution across repetitions."

    obs = (f"OBSERVATION: {n} {intent_label} repetitions on this route, "
           f"from {series[0]['date']} to {series[-1]['date']}.")
    return {
        "ok": True, "route_id": route_id, "sport": sport, "intent": intent,
        "available_intents": {kk: len(v) for kk, v in by_intent.items()},
        "series": series,
        "trend": {**trend, "verdict": verdict},
        "explanation_text": f"{obs} INTERPRETATION: {reading}",
    }


# ── Nombrar rutas (las habituales merecen nombre humano) ─────────────────────

@router.post("/api/route/{route_id}/rename")
def rename_route(route_id: str, name: str = Query(..., min_length=1, max_length=80)):
    """Pone nombre humano a una ruta ("Atizapán base"). Solo actualiza routes.name."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    with conn.cursor() as cur:
        cur.execute("UPDATE routes SET name=%s WHERE route_id=%s", (name, route_id))
        if cur.rowcount == 0:
            cur.execute("""
                INSERT INTO routes (route_id, name, created_at)
                VALUES (%s, %s, NOW()) ON CONFLICT (route_id) DO UPDATE SET name=EXCLUDED.name
            """, (route_id, name))
    conn.commit()
    return {"ok": True, "route_id": route_id, "name": name}


# ══ V7.3 — Event Readiness Gap · Capability Evidence · Epoch Tests ═══════════

_CAP_DISPLAY = {"motor_aerobico": "Aerobic engine", "escalada": "Strength-endurance",
                "composicion_corporal": "Body composition",
                "nutricion_deportiva": "Sports nutrition",
                "recuperacion": "Recovery", "fuerza": "Strength",
                "consistencia": "Consistency"}

# Qué fase del plan ataca cada capacidad (lectura, no dogma)
_PHASE_ATTACKS = {
    "base": ["motor_aerobico", "composicion_corporal", "consistencia"],
    "build": ["escalada", "motor_aerobico", "fuerza"],
    "peak": ["escalada", "recuperacion", "nutricion_deportiva"],
}


def _evento_for_goal(goal_name, goal_type):
    n = (goal_name or "").lower()
    if "time trial" in n or "crono" in n:
        return "time_trial"
    return {"cycling": "gran_fondo_150", "gravel": "gran_fondo_150",
            "running": "medio_maraton",
            "climbing": "escalera_al_infierno"}.get(goal_type, "gran_fondo_150")


@router.get("/gpt/event-readiness-gap")
def event_readiness_gap():
    """El evento exige X; hoy tienes Y; la fase Z ataca ese gap. Read-only."""
    from datetime import date as _date
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_name, event_type, event_date FROM mars_goals
            WHERE status='active' ORDER BY priority ASC LIMIT 1
        """)
        g = cur.fetchone()
        if not g:
            return {"ok": True, "goal": None,
                    "explanation_text": "No active goal — register one to measure the gap to your event."}
        cur.execute("""
            SELECT meta FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        pm = cur.fetchone()

    evento = _evento_for_goal(g[0], g[1])
    weeks_to_event = max(0, (g[2] - _date.today()).days // 7) if g[2] else None

    try:
        from capability_engine import calculate_readiness, READINESS_EVENTS
        rr = calculate_readiness(conn, evento)
    except Exception as e:
        logger.warning(f"readiness fail: {e}")
        return {"ok": False, "message": f"Readiness unavailable: {str(e)[:80]}"}

    comps = [c for c in (rr.get("components") or []) if c.get("score") is not None]
    comps_sorted = sorted(comps, key=lambda c: c.get("weighted_contribution", 0))
    gap = comps_sorted[0] if comps_sorted else None
    demands = [{"capability": k, "display": _CAP_DISPLAY.get(k, k), "weight": w}
               for k, w in sorted((READINESS_EVENTS.get(evento, {}).get("weights") or {}).items(),
                                  key=lambda x: -x[1])]

    # ¿Qué fase ataca el gap?
    phases = ((pm[0] or {}).get("phases") or []) if pm else []
    today_s = _date.today().isoformat()
    current_phase = next((p for p in phases if p.get("start", "") <= today_s <= p.get("end", "")), None)
    attacking_phase = None
    if gap:
        gap_key = gap.get("capability") or gap.get("key")
        for p in phases:
            if gap_key in _PHASE_ATTACKS.get(p.get("name", ""), []):
                attacking_phase = p
                break

    score = rr.get("readiness_score")
    gap_name = _CAP_DISPLAY.get((gap or {}).get("capability") or (gap or {}).get("key"),
                                (gap or {}).get("nombre", "—")) if gap else None
    obs = (f"OBSERVATION: {g[0]}"
           + (f" in {weeks_to_event} weeks" if weeks_to_event is not None else "")
           + f". Readiness today: {round(score) if score is not None else '—'}/100.")
    interp = (f"INTERPRETATION: Your piece with the most room is {gap_name} "
              f"({round(gap['score']) if gap and gap.get('score') is not None else '—'} pts)."
              if gap else "INTERPRETATION: No components calculated yet.")
    plan_line = ""
    if attacking_phase:
        if current_phase and attacking_phase.get("name") == current_phase.get("name"):
            plan_line = f" The current phase ({attacking_phase['name']}) is attacking exactly that gap."
        else:
            plan_line = (f" The {attacking_phase['name']} phase "
                         f"(from {attacking_phase.get('start')}) attacks that gap — the plan already covers it.")

    return {
        "ok": True,
        "goal": {"event_name": g[0], "event_type": g[1],
                 "event_date": g[2].isoformat() if g[2] else None,
                 "weeks_to_event": weeks_to_event},
        "evento_model": evento,
        "readiness_score": score,
        "demands": demands,
        "components": comps,
        "gap": gap,
        "current_phase": (current_phase or {}).get("name"),
        "attacking_phase": (attacking_phase or {}).get("name"),
        "confidence": rr.get("confidence"),
        "data_gaps": rr.get("data_gaps"),
        "explanation_text": f"{obs} {interp}{plan_line}",
    }


@router.get("/gpt/capability-evidence")
def capability_evidence(weeks: int = Query(4, ge=1, le=12)):
    """Evidencia: qué sesiones construyeron cada capacidad humana (últimas N semanas)."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT cs.clean_session_id, cs.name, cs.start_date, cs.duration_s,
                   cs.distance_km, cs.avg_hr_bpm, cs.ascent_m,
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_index >= 0),
                   COUNT(sl.lap_id) FILTER (WHERE sl.lap_type='work')
            FROM clean_sessions cs
            LEFT JOIN session_laps sl ON sl.clean_session_id = cs.clean_session_id
            WHERE cs.start_date > CURRENT_DATE - %s * 7
            GROUP BY cs.clean_session_id
            ORDER BY cs.start_date DESC
        """, (weeks,))
        rows = cur.fetchall()

    caps_map = {"intervals": ["power", "aerobic_fitness"],
                "high_intensity": ["power", "aerobic_fitness"],
                "endurance": ["aerobic_fitness", "endurance"],
                "climb": ["strength_endurance", "power"],
                "recovery": ["recovery"], "tempo": ["endurance", "aerobic_fitness"]}
    evidence = {}
    for r in rows:
        avg_hr = float(r[5]) if r[5] else None
        intent = _session_type_from_row(avg_hr, r[6], float(r[4]) if r[4] else 0,
                                        r[3], r[8], r[7])
        for cap in caps_map.get(intent, ["aerobic_fitness"]):
            e = evidence.setdefault(cap, {"sessions": [], "time_s": 0, "count": 0})
            e["count"] += 1
            e["time_s"] += r[3] or 0
            if len(e["sessions"]) < 5:
                e["sessions"].append({"clean_session_id": r[0], "name": r[1],
                                      "date": r[2].isoformat() if r[2] else None,
                                      "intent": intent})
    out = [{"capability": k, "sessions_count": v["count"],
            "hours": round(v["time_s"] / 3600, 1), "evidence": v["sessions"]}
           for k, v in sorted(evidence.items(), key=lambda x: -x[1]["time_s"])]
    return {"ok": True, "weeks": weeks, "capacities": out,
            "nota": "Each capacity cites the sessions that built it — a score without evidence is not Epoch."}


@router.get("/gpt/test-recommendation")
def test_recommendation():
    """
    Epoch Tests: propone (no ordena) una sesión de evaluación cuando hay razón:
    cambio de fase cercano, o sin esfuerzo sostenido medible en 6+ semanas.
    """
    from datetime import date as _date, timedelta as _td
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    with conn.cursor() as cur:
        # Último esfuerzo sostenido tipo test: lap work >= 15 min en z3+
        cur.execute("""
            SELECT MAX(cs.start_date)
            FROM session_laps sl JOIN clean_sessions cs USING (clean_session_id)
            WHERE sl.lap_type='work' AND sl.duration_s >= 900
              AND sl.zone_label IN ('z3','z4','z5')
        """)
        last_test_like = cur.fetchone()[0]
        cur.execute("""
            SELECT meta FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        pm = cur.fetchone()
        # Ruta más repetida (el laboratorio) para proponer el test ahí
        cur.execute("""
            SELECT cs.route_id, MAX(rt.name), COUNT(*)
            FROM clean_sessions cs LEFT JOIN routes rt ON rt.route_id=cs.route_id
            WHERE cs.route_id IS NOT NULL AND cs.sport_type IN ('Ride','VirtualRide')
            GROUP BY cs.route_id ORDER BY COUNT(*) DESC LIMIT 1
        """)
        lab = cur.fetchone()

    today = _date.today()
    reasons = []
    phases = ((pm[0] or {}).get("phases") or []) if pm else []
    for p in phases:
        try:
            end = _date.fromisoformat(p.get("end", ""))
            if 0 <= (end - today).days <= 7:
                reasons.append(f"the {p.get('name')} phase ends on {p.get('end')} — a good moment to measure what it built")
        except Exception:
            pass
    weeks_since = None
    if last_test_like:
        weeks_since = (today - last_test_like).days // 7
        if weeks_since >= 6:
            reasons.append(f"no measurable sustained effort in {weeks_since} weeks — your zones may be outdated")
    else:
        reasons.append("no measurable sustained effort on record — your zones are estimated, not measured")

    if not reasons:
        return {"ok": True, "recommended": False,
                "explanation_text": ("No reason for a test right now: there is recent sustained effort "
                                     "and no phase change coming. Keep building.")}

    lab_name = (lab[1] or f"your usual route ({lab[0][:8]})") if lab else "a familiar route"
    test = {
        "name": "Sustained effort test (20 min)",
        "protocol": ("Warm up 15 min easy → 20 sustained minutes at the highest effort "
                     "you can hold steady → cool down 10 min."),
        "where": lab_name,
        "updates": "Average HR of those 20 min ≈ your real threshold → recalibrates Z2-Z4 zones with fresh evidence.",
    }
    return {
        "ok": True, "recommended": True, "reasons": reasons, "test": test,
        "last_sustained_effort": last_test_like.isoformat() if last_test_like else None,
        "explanation_text": ("OBSERVATION: " + "; ".join(reasons).capitalize() + ". "
                             "PRUDENT SUGGESTION: if you feel fresh one day this week, "
                             f"a 20-minute sustained test on {lab_name} would tell you where your threshold "
                             "really is — and every zone recalibrates from it. A proposal, not an order."),
    }


# ══ V7.4 — EPOCHS: las eras de tu vida atlética ══════════════════════════════

_SPORT_ERA = {"Ride": "bici", "VirtualRide": "bici", "Run": "correr",
              "Walk": "caminar", "Swim": "nadar", "WeightTraining": "fuerza",
              "Workout": "entrenamiento", "Hike": "senderismo"}


@router.get("/gpt/epochs")
def epochs():
    """
    Detecta las ÉPOCAS de la historia atlética: bloques continuos de actividad
    separados por pausas (>=8 semanas con casi nada) o cambios sostenidos de
    disciplina dominante. Cada época: qué fue, cuánto duró, qué construyó.
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT date_trunc('month', start_date)::date AS mes,
                   sport_type, COUNT(*),
                   COALESCE(SUM(distance_km),0), COALESCE(SUM(duration_s),0)/3600.0
            FROM clean_sessions
            WHERE start_date IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1
        """)
        rows = cur.fetchall()
    if not rows:
        return {"ok": True, "epochs": [],
                "explanation_text": "Not enough history yet."}

    # Agregar por mes: total + deporte dominante (por horas)
    months = {}
    for mes, sport, n, km, hrs in rows:
        m = months.setdefault(mes.isoformat()[:7], {"sessions": 0, "km": 0.0,
                                                    "hours": 0.0, "sports": {}})
        m["sessions"] += n
        m["km"] += float(km)
        m["hours"] += float(hrs)
        m["sports"][sport] = m["sports"].get(sport, 0) + float(hrs)

    keys = sorted(months.keys())
    # Rellenar meses vacíos del rango (un mes sin filas = pausa total)
    from datetime import date as _date
    def _next_month(ym):
        y, mo = int(ym[:4]), int(ym[5:7])
        return f"{y + (mo == 12):04d}-{(mo % 12) + 1:02d}"
    full = []
    cur_m = keys[0]
    while cur_m <= keys[-1]:
        full.append(cur_m)
        cur_m = _next_month(cur_m)

    def _dominant(m):
        sp = months.get(m, {}).get("sports") or {}
        if not sp:
            return None
        best = max(sp, key=sp.get)
        return _SPORT_ERA.get(best, best)

    def _active(m):
        return months.get(m, {}).get("sessions", 0) >= 3  # >=3 sesiones/mes = mes activo

    # Segmentar: pausa = 2+ meses inactivos · cambio de era = dominante distinto 3+ meses
    eras = []
    seg = None
    inactive_run = 0
    for m in full:
        if _active(m):
            dom = _dominant(m)
            if seg is None:
                seg = {"start": m, "end": m, "months": [m]}
            elif inactive_run >= 2:
                eras.append(seg)
                seg = {"start": m, "end": m, "months": [m]}
            else:
                seg["end"] = m
                seg["months"].append(m)
            inactive_run = 0
        else:
            inactive_run += 1

    if seg:
        eras.append(seg)

    # Subdividir por cambio sostenido de disciplina dominante
    final = []
    for e in eras:
        sub = None
        for m in e["months"]:
            dom = _dominant(m) or "mixto"
            if sub is None:
                sub = {"start": m, "end": m, "doms": [dom]}
            elif dom != max(set(sub["doms"][-3:]), key=sub["doms"][-3:].count) and \
                 len(sub["doms"]) >= 3 and sub["doms"][-1] != dom and sub["doms"][-2:] == [sub["doms"][-1]] * 2:
                final.append(sub)
                sub = {"start": m, "end": m, "doms": [dom]}
            else:
                sub["end"] = m
                sub["doms"].append(dom)
        if sub:
            final.append(sub)

    # Construir las épocas con stats y nombre honesto
    out = []
    today_ym = _date.today().isoformat()[:7]
    for e in final:
        ms = [m for m in full if e["start"] <= m <= e["end"]]
        sess = sum(months.get(m, {}).get("sessions", 0) for m in ms)
        km = round(sum(months.get(m, {}).get("km", 0) for m in ms))
        hrs = round(sum(months.get(m, {}).get("hours", 0) for m in ms))
        if sess < 10:
            continue  # ruido, no época
        dom_counts = {}
        for m in ms:
            d = _dominant(m)
            if d:
                dom_counts[d] = dom_counts.get(d, 0) + 1
        dom = max(dom_counts, key=dom_counts.get) if dom_counts else "mixto"
        n_months = len(ms)
        is_current = e["end"] >= today_ym
        _dom_en={"bici":"cycling","correr":"running","fuerza":"strength","caminar":"walking","nadar":"swimming"}.get(dom,dom)
        name = (f"The {_dom_en} era" if not is_current else f"The current era ({_dom_en})")
        caps = {"bici": ["aerobic_fitness", "endurance"], "correr": ["aerobic_fitness", "endurance"],
                "fuerza": ["strength"], "caminar": ["consistency"], "nadar": ["aerobic_fitness"]}.get(dom, ["consistency"])
        out.append({"name": name, "start": e["start"], "end": e["end"],
                    "months": n_months, "is_current": is_current,
                    "dominant": dom, "sessions": sess, "km": km, "hours": hrs,
                    "built": caps})

    # Pausas entre épocas (también son parte de la historia, sin culpa)
    story = []
    for i, ep in enumerate(out):
        story.append(ep)
        if i + 1 < len(out):
            gap_start, gap_end = ep["end"], out[i + 1]["start"]
            story.append({"name": "Pause", "start": gap_start, "end": gap_end,
                          "is_pause": True,
                          "nota": "Pauses are part of the story too — what matters is that you came back."})

    n_eras = len(out)
    span = f"{out[0]['start']} → {out[-1]['end']}" if out else "—"
    expl = (f"OBSERVATION: {n_eras} epochs detected between {span}. "
            "INTERPRETATION: The sport kept changing; the adapting body is the same. "
            "Each era built something the next ones inherited"
            + (" — and every comeback after a pause counts more than the pause itself." if any(s.get('is_pause') for s in story) else "."))
    return {"ok": True, "epochs": story, "explanation_text": expl}


# ══ V7.5 — Stream Intelligence: los streams pagados, por fin consumidos ══════
# 3,089 actividades tienen streams completos en Supabase sin un solo lector.
# Esto los destila a un resumen por sesión: decoupling aeróbico, calor,
# pausas reales y tiempo en zona REAL (no estimado por promedio).


def _ensure_streams_summary_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS session_streams_summary (
                clean_session_id  TEXT PRIMARY KEY REFERENCES clean_sessions(clean_session_id) ON DELETE CASCADE,
                decoupling_pct    NUMERIC(6,2),
                temp_avg_c        NUMERIC(5,1),
                temp_max_c        NUMERIC(5,1),
                pauses_count      INT,
                paused_time_s     INT,
                time_in_zone_s    JSONB,
                hr_points         INT,
                computed_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def _summarize_stream(time_s, hr, vel, temp, moving):
    """Destila un stream a métricas con significado. Todas opcionales."""
    out = {"decoupling_pct": None, "temp_avg_c": None, "temp_max_c": None,
           "pauses_count": None, "paused_time_s": None,
           "time_in_zone_s": None, "hr_points": 0}
    n = len(time_s or [])
    if not n:
        return out
    # Tiempo en zona REAL desde el stream de FC (1 muestra ≈ delta t)
    if hr:
        out["hr_points"] = sum(1 for h in hr if h)
        tz = {}
        for i in range(1, min(n, len(hr))):
            h = hr[i]
            if not h:
                continue
            dt = max(0, min(60, (time_s[i] or 0) - (time_s[i-1] or 0)))
            z = _zone_for_hr(h) or "sin_zona"
            tz[z] = tz.get(z, 0) + dt
        out["time_in_zone_s"] = tz or None
    # Decoupling aeróbico: eficiencia (vel/FC) 1ª mitad vs 2ª mitad.
    # >5% = deriva cardiaca: el mismo ritmo costó más al final.
    if hr and vel and n >= 600:
        mid = n // 2
        def _eff(a, b):
            pairs = [(vel[i], hr[i]) for i in range(a, min(b, len(vel), len(hr)))
                     if vel[i] and hr[i] and vel[i] > 1.0]
            if len(pairs) < 60:
                return None
            return (sum(p[0] for p in pairs) / len(pairs)) / (sum(p[1] for p in pairs) / len(pairs))
        e1, e2 = _eff(0, mid), _eff(mid, n)
        if e1 and e2:
            out["decoupling_pct"] = round((e1 - e2) / e1 * 100, 2)
    # Temperatura
    if temp:
        vals = [t for t in temp if t is not None]
        if vals:
            out["temp_avg_c"] = round(sum(vals) / len(vals), 1)
            out["temp_max_c"] = round(max(vals), 1)
    # Pausas reales (stream moving: False = detenido)
    if moving:
        pauses, paused_s, run_start = 0, 0, None
        for i in range(1, min(n, len(moving))):
            if moving[i] is False:
                if run_start is None:
                    run_start = time_s[i-1] or 0
            else:
                if run_start is not None:
                    dur = (time_s[i] or 0) - run_start
                    if dur >= 15:
                        pauses += 1
                        paused_s += dur
                    run_start = None
        out["pauses_count"] = pauses
        out["paused_time_s"] = paused_s
    return out


@router.post("/api/strava/summarize-streams")
def summarize_streams(limit: int = Query(50, ge=1, le=200),
                      dry_run: bool = Query(False)):
    """
    Destila strava_streams_raw (Supabase) → session_streams_summary (Railway).
    Manual, idempotente, solo escribe en la tabla nueva. dry_run estima.
    No consume cuota Strava — solo lecturas Supabase.
    """
    from strava.auth import get_supabase
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_streams_summary_table(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM clean_sessions cs
            LEFT JOIN session_streams_summary ss USING (clean_session_id)
            WHERE cs.source='strava' AND cs.source_activity_id IS NOT NULL
              AND ss.clean_session_id IS NULL
        """)
        pending = cur.fetchone()[0]
        cur.execute("""
            SELECT cs.clean_session_id, cs.source_activity_id
            FROM clean_sessions cs
            LEFT JOIN session_streams_summary ss USING (clean_session_id)
            WHERE cs.source='strava' AND cs.source_activity_id IS NOT NULL
              AND ss.clean_session_id IS NULL
            ORDER BY cs.start_time DESC
            LIMIT %s
        """, (limit,))
        batch = cur.fetchall()

    summary = {"ok": True, "dry_run": dry_run, "pending": pending,
               "processed": 0, "inserted": 0, "no_streams": 0, "errors": 0}
    if dry_run or not batch:
        summary["message"] = (f"DRY RUN: {pending} sessions without a streams summary."
                              if dry_run else "Nothing pending.")
        return summary

    try:
        sb = get_supabase()
    except Exception as e:
        summary["ok"] = False
        summary["message"] = f"Supabase no disponible: {str(e)[:80]}"
        return summary

    by_act = {str(r[1]): r[0] for r in batch}
    ids = [int(a) for a in by_act.keys()]
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        try:
            resp = (sb.table("strava_streams_raw")
                    .select("strava_activity_id,stream_time,stream_heartrate,"
                            "stream_velocity,stream_temp,stream_moving")
                    .in_("strava_activity_id", chunk).execute())
        except Exception as e:
            summary["ok"] = False
            summary["message"] = f"Supabase detuvo el batch: {str(e)[:100]}"
            break
        got = {str(row["strava_activity_id"]): row for row in (resp.data or [])}
        for act_id in chunk:
            cs_id = by_act[str(act_id)]
            summary["processed"] += 1
            row = got.get(str(act_id))
            if not row or not row.get("stream_time"):
                summary["no_streams"] += 1
                # sentinel: fila vacía para no re-pedir
                vals = {"clean_session_id": cs_id, "hr_points": 0}
            else:
                try:
                    m = _summarize_stream(row.get("stream_time") or [],
                                          row.get("stream_heartrate") or [],
                                          row.get("stream_velocity") or [],
                                          row.get("stream_temp") or [],
                                          row.get("stream_moving") or [])
                    vals = {"clean_session_id": cs_id, **m}
                except Exception as e:
                    summary["errors"] += 1
                    logger.warning(f"summarize fail {cs_id}: {e}")
                    continue
            try:
                import json as _json
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO session_streams_summary
                            (clean_session_id, decoupling_pct, temp_avg_c, temp_max_c,
                             pauses_count, paused_time_s, time_in_zone_s, hr_points)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (clean_session_id) DO NOTHING
                    """, (cs_id, vals.get("decoupling_pct"), vals.get("temp_avg_c"),
                          vals.get("temp_max_c"), vals.get("pauses_count"),
                          vals.get("paused_time_s"),
                          _json.dumps(vals.get("time_in_zone_s")) if vals.get("time_in_zone_s") else None,
                          vals.get("hr_points", 0)))
                conn.commit()
                summary["inserted"] += 1
            except Exception as e:
                conn.rollback()
                summary["errors"] += 1
                logger.warning(f"insert summary fail {cs_id}: {e}")

    summary["remaining"] = max(0, pending - summary["processed"])
    if "message" not in summary:
        summary["message"] = "Re-run until remaining=0. Uses no Strava quota."
    return summary


@router.get("/gpt/session/{clean_session_id}/streams-summary")
def get_streams_summary(clean_session_id: str):
    """Lectura del resumen de streams: decoupling, calor, pausas, zona real."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_streams_summary_table(conn)
    _ensure_weather_columns(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT decoupling_pct, temp_avg_c, temp_max_c, pauses_count,
                   paused_time_s, time_in_zone_s, hr_points,
                   wind_kmh, wind_gust_kmh, weather_label, country, humidity_pct
            FROM session_streams_summary WHERE clean_session_id=%s
        """, (clean_session_id,))
        r = cur.fetchone()
    if not r:
        return {"ok": True, "available": False,
                "message": "No summary yet — run POST /api/strava/summarize-streams."}
    dec = float(r[0]) if r[0] is not None else None
    reading = None
    if dec is not None:
        if dec <= 3:
            reading = "No cardiac drift — the aerobic engine held the full effort."
        elif dec <= 7:
            reading = f"Moderate drift ({dec}%): the same pace cost a bit more at the end — normal in long or hot sessions."
        else:
            reading = f"High drift ({dec}%): the second half clearly cost more — fatigue, heat, or a pace above your current base."
    wind = float(r[7]) if r[7] is not None else None
    wind_reading = None
    if wind is not None:
        if wind >= 30:
            wind_reading = f"Strong wind ({wind} km/h) — that day's speed is not comparable; your effort (HR) is."
        elif wind >= 18:
            wind_reading = f"Notable wind ({wind} km/h) — the air took part of your speed."
    return {"ok": True, "available": True,
            "decoupling_pct": dec, "decoupling_reading": reading,
            "temp_avg_c": float(r[1]) if r[1] is not None else None,
            "temp_max_c": float(r[2]) if r[2] is not None else None,
            "pauses_count": r[3], "paused_time_s": r[4],
            "time_in_zone_s": r[5], "hr_points": r[6],
            "wind_kmh": wind,
            "wind_gust_kmh": float(r[8]) if r[8] is not None else None,
            "wind_reading": wind_reading,
            "weather_label": r[9], "country": r[10],
            "humidity_pct": float(r[11]) if r[11] is not None else None}


# ══ V8.x — Clima completo (viento, condición) + país/región ══════════════════
# Open-Meteo Archive: gratuito, sin API key, histórico por lat/lon + fecha.
# "En Irlanda lo que afecta es el viento" — ahora Epoch lo sabe.

_WMO_LABEL = {0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado",
              3: "nublado", 45: "niebla", 48: "niebla", 51: "llovizna", 53: "llovizna",
              55: "llovizna", 61: "lluvia ligera", 63: "lluvia", 65: "lluvia fuerte",
              71: "nieve", 73: "nieve", 75: "nieve", 80: "chubascos", 81: "chubascos",
              82: "chubascos fuertes", 95: "tormenta", 96: "tormenta", 99: "tormenta"}


def _country_from_latlon(lat, lon):
    """Geocoding offline por cajas — suficiente para MX/IE/GB/US/ES."""
    if lat is None or lon is None:
        return None
    if 51.3 <= lat <= 55.5 and -10.7 <= lon <= -5.9:
        return "IE"
    if 49.8 <= lat <= 60.9 and -8.7 <= lon <= 1.8:
        return "GB"
    if 14.5 <= lat <= 32.8 and -118.5 <= lon <= -86.6:
        return "MX"
    if 24.4 <= lat <= 49.5 and -125.0 <= lon <= -66.9:
        return "US"
    if 35.9 <= lat <= 43.9 and -9.4 <= lon <= 3.4:
        return "ES"
    return "otro"


def _ensure_weather_columns(conn):
    with conn.cursor() as cur:
        for col, typ in [("wind_kmh", "NUMERIC(5,1)"), ("wind_gust_kmh", "NUMERIC(5,1)"),
                         ("humidity_pct", "NUMERIC(5,1)"), ("weather_code", "INT"),
                         ("weather_label", "TEXT"), ("country", "TEXT"),
                         ("weather_fetched_at", "TIMESTAMPTZ")]:
            cur.execute(f"ALTER TABLE session_streams_summary ADD COLUMN IF NOT EXISTS {col} {typ}")
    conn.commit()


@router.post("/api/enrich-weather")
def enrich_weather(limit: int = Query(50, ge=1, le=200),
                   dry_run: bool = Query(False)):
    """
    Enriquece sesiones con clima histórico (Open-Meteo) + país por lat/lon.
    Manual, idempotente (weather_fetched_at evita repetir). Gratis, sin key.
    Solo escribe en session_streams_summary.
    """
    import httpx
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_weather_columns(conn)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)
            FROM clean_sessions cs
            LEFT JOIN session_streams_summary ss USING (clean_session_id)
            WHERE cs.start_lat IS NOT NULL AND cs.start_time IS NOT NULL
              AND (ss.weather_fetched_at IS NULL)
        """)
        pending = cur.fetchone()[0]
        cur.execute("""
            SELECT cs.clean_session_id, cs.start_lat, cs.start_lon,
                   cs.start_date, EXTRACT(HOUR FROM cs.start_time)::int,
                   COALESCE(cs.duration_s, 3600)
            FROM clean_sessions cs
            LEFT JOIN session_streams_summary ss USING (clean_session_id)
            WHERE cs.start_lat IS NOT NULL AND cs.start_time IS NOT NULL
              AND (ss.weather_fetched_at IS NULL)
            ORDER BY cs.start_time DESC
            LIMIT %s
        """, (limit,))
        batch = cur.fetchall()

    summary = {"ok": True, "dry_run": dry_run, "pending": pending,
               "processed": 0, "enriched": 0, "no_data": 0, "errors": 0}
    if dry_run or not batch:
        summary["message"] = (f"DRY RUN: {pending} sessions without weather."
                              if dry_run else "Nothing pending.")
        return summary

    client = httpx.Client(timeout=20)
    for cs_id, lat, lon, sdate, shour, dur_s in batch:
        summary["processed"] += 1
        country = _country_from_latlon(float(lat), float(lon) if lon else None)
        wind = gust = hum = None
        wcode = None
        try:
            r = client.get("https://archive-api.open-meteo.com/v1/archive", params={
                "latitude": float(lat), "longitude": float(lon),
                "start_date": sdate.isoformat(), "end_date": sdate.isoformat(),
                "hourly": "windspeed_10m,windgusts_10m,relativehumidity_2m,weathercode",
                "timezone": "UTC",
            })
            if r.status_code == 200:
                h = (r.json().get("hourly") or {})
                hrs = max(1, min(3, round((dur_s or 3600) / 3600)))
                idxs = [min(23, (shour or 12) + k) for k in range(hrs)]
                def _avg_of(key):
                    vals = [(h.get(key) or [None]*24)[i] for i in idxs]
                    vals = [v for v in vals if v is not None]
                    return round(sum(vals) / len(vals), 1) if vals else None
                wind = _avg_of("windspeed_10m")
                gust = _avg_of("windgusts_10m")
                hum = _avg_of("relativehumidity_2m")
                wcs = [(h.get("weathercode") or [None]*24)[i] for i in idxs]
                wcs = [w for w in wcs if w is not None]
                wcode = max(wcs) if wcs else None  # el peor código de la ventana
            else:
                summary["no_data"] += 1
        except Exception as e:
            summary["errors"] += 1
            logger.warning(f"weather fail {cs_id}: {e}")
            if summary["errors"] >= 5:
                summary["ok"] = False
                summary["message"] = "Too many errors — stopped (partial summary)."
                break

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO session_streams_summary
                        (clean_session_id, wind_kmh, wind_gust_kmh, humidity_pct,
                         weather_code, weather_label, country, weather_fetched_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (clean_session_id) DO UPDATE SET
                        wind_kmh=EXCLUDED.wind_kmh, wind_gust_kmh=EXCLUDED.wind_gust_kmh,
                        humidity_pct=EXCLUDED.humidity_pct, weather_code=EXCLUDED.weather_code,
                        weather_label=EXCLUDED.weather_label, country=EXCLUDED.country,
                        weather_fetched_at=NOW()
                """, (cs_id, wind, gust, hum, wcode,
                      _WMO_LABEL.get(wcode) if wcode is not None else None, country))
            conn.commit()
            summary["enriched"] += 1
        except Exception as e:
            conn.rollback()
            summary["errors"] += 1
            logger.warning(f"weather insert fail {cs_id}: {e}")
    client.close()

    summary["remaining"] = max(0, pending - summary["processed"])
    if "message" not in summary:
        summary["message"] = "Re-run until remaining=0. Free (Open-Meteo, no key)."
    return summary
