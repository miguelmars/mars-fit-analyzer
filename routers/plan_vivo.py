"""
V8 — El Plan Vivo: el plan estático de Garmin se convierte en un plan que
respira con el atleta. Adaptación diaria, semana que se reacomoda y proyección
al evento — sin culpa, con reglas transparentes, proponiendo (nunca ordenando).

  GET  /gpt/today-adaptation      → ¿qué toca hoy, considerando cómo amanecí?
  GET  /gpt/week-rebalance        → propuesta para reacomodar lo saltado
  POST /api/plan-session/{id}/move → aceptar un movimiento (historial intacto)
  GET  /gpt/event-projection      → ¿cómo llego al evento si sigo así?
  POST /api/feedback              → 👍/👎 sobre cualquier lectura (afina heurísticas)
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from db import get_db
from routers.gpt_training_context import _ensure_training_tables

logger = logging.getLogger("epoch.plan_vivo")
router = APIRouter()

# Umbrales conservadores y TRANSPARENTES (se muestran al usuario en /legal §4)
_HR_DELTA_FLAG = 7      # FC reposo ≥7 bpm sobre tu base de 21 días
_SLEEP_FLAG = 6.0       # menos de 6 h de sueño
_FATIGUE_FLAG = 7       # fatiga reportada ≥7/10
_ESTADO_FLAG = 4        # estado general ≤4/10


def _ensure_feedback_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reading_feedback (
                id          BIGSERIAL PRIMARY KEY,
                context     TEXT NOT NULL,
                ref_id      TEXT,
                verdict     TEXT NOT NULL CHECK (verdict IN ('up','down')),
                note        TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            ALTER TABLE plan_sessions ADD COLUMN IF NOT EXISTS moved_from DATE
        """)
        cur.execute("""
            ALTER TABLE plan_sessions ADD COLUMN IF NOT EXISTS move_reason TEXT
        """)
    conn.commit()


# ── GET /gpt/today-adaptation ─────────────────────────────────────────────────

@router.get("/gpt/today-adaptation")
def today_adaptation():
    """Cruza cómo amaneciste con lo que el plan dice que toca. Propone, no ordena."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_feedback_table(conn)
    today = date.today()

    with conn.cursor() as cur:
        # Check matutino de hoy
        cur.execute("""
            SELECT hr_rest, sleep_hours, fatigue, stress_level
            FROM wellness WHERE date=%s AND hr_rest IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (today,))
        chk = cur.fetchone()
        # Baseline FC reposo (21 días)
        cur.execute("""
            SELECT AVG(hr_rest) FROM wellness
            WHERE date >= %s AND date < %s AND hr_rest IS NOT NULL
        """, (today - timedelta(days=21), today))
        baseline = cur.fetchone()[0]
        # Sesión de ayer (la más larga) + su deriva
        cur.execute("""
            SELECT cs.duration_s, ss.decoupling_pct,
                   (SELECT COUNT(*) FROM session_laps sl
                    WHERE sl.clean_session_id=cs.clean_session_id AND sl.lap_type='work')
            FROM clean_sessions cs
            LEFT JOIN session_streams_summary ss USING (clean_session_id)
            WHERE cs.start_date = %s
            ORDER BY cs.duration_s DESC NULLS LAST LIMIT 1
        """, (today - timedelta(days=1),))
        yest = cur.fetchone()
        # Lo que el plan dice que toca hoy
        cur.execute("""
            SELECT ps.id, ps.description, ps.session_type, ps.status
            FROM plan_sessions ps JOIN training_plans tp USING (plan_id)
            WHERE tp.status='active' AND ps.planned_date = %s
            ORDER BY ps.id LIMIT 1
        """, (today,))
        planned = cur.fetchone()

    planned_out = ({"id": planned[0], "description": planned[1],
                    "session_type": planned[2], "status": planned[3]}
                   if planned else None)

    # Sin check matutino → honesto: no puedo leer cómo amaneciste
    if not chk:
        return {"ok": True, "status": "sin_lectura", "planned": planned_out,
                "explanation_text": ("OBSERVATION: No morning check today. "
                                     "INTERPRETATION: Without resting HR or sleep I cannot "
                                     "read how you woke up — adapting blind would be making things up. "
                                     + (f"The plan says: {planned[1]}. " if planned else "")
                                     + "SUGGESTION: the check takes 20 seconds and unlocks the read."),
                "cta": "morning_check"}

    hr_rest, sleep_h, fatigue, estado = chk
    flags = []
    hr_delta = None
    if baseline and hr_rest:
        hr_delta = round(float(hr_rest) - float(baseline), 1)
        if hr_delta >= _HR_DELTA_FLAG:
            flags.append(f"resting HR {hr_delta:+.0f} bpm above your 21-day base")
    if sleep_h is not None and float(sleep_h) < _SLEEP_FLAG:
        flags.append(f"only {sleep_h}h of sleep")
    if fatigue is not None and int(fatigue) >= _FATIGUE_FLAG:
        flags.append(f"reported fatigue {fatigue}/10")
    if estado is not None and int(estado) <= _ESTADO_FLAG:
        flags.append(f"overall state {estado}/10")
    yesterday_hard = bool(yest and ((yest[0] or 0) >= 5400 or
                                    (yest[1] is not None and float(yest[1]) > 7) or
                                    (yest[2] or 0) >= 3))
    if yesterday_hard and flags:
        flags.append("yesterday was a demanding session")

    # Decisión conservadora
    if len(flags) >= 2 or (hr_delta is not None and hr_delta >= _HR_DELTA_FLAG and flags):
        adaptation = "mover_o_suavizar"
    elif len(flags) == 1:
        adaptation = "precaucion"
    else:
        adaptation = "mantener"

    plan_txt = planned[1] if planned else None
    obs = ("OBSERVATION: " +
           (f"resting HR {int(hr_rest)} (base ~{round(float(baseline))})" if baseline else f"resting HR {int(hr_rest)}") +
           (f", sleep {sleep_h}h" if sleep_h is not None else "") +
           (f", fatigue {fatigue}/10" if fatigue is not None else "") + ".")
    if adaptation == "mantener":
        interp = "INTERPRETATION: You woke up inside your range — the body is ready for what is planned."
        sug = (f"SUGGESTION: The plan says {plan_txt} — green light." if plan_txt
               else "SUGGESTION: No session registered today in the plan; if you ride, your read is green.")
    elif adaptation == "precaucion":
        interp = f"INTERPRETATION: One signal to watch: {flags[0]}. It does not stop the day, but listen to yourself."
        sug = (f"SUGGESTION: {plan_txt} can stand — if mid-session the body says otherwise, cut it short without guilt."
               if plan_txt else "SUGGESTION: A usable day, in moderation.")
    else:
        interp = ("INTERPRETATION: " + "; ".join(flags).capitalize() +
                  ". Pushing today adds little — adaptation happens when you rest.")
        sug = ((f"SUGGESTION: Move {plan_txt} to another day this week — a short easy Z2 or rest today "
                "keeps the process going. The plan does not break by listening to the body: it builds better.")
               if plan_txt else
               "SUGGESTION: Easy or rest today. It is not losing a day: it is winning the adaptation.")

    return {"ok": True, "status": adaptation,
            "check": {"hr_rest": hr_rest, "baseline": round(float(baseline), 1) if baseline else None,
                      "hr_delta": hr_delta, "sleep_h": float(sleep_h) if sleep_h is not None else None,
                      "fatigue": fatigue, "estado": estado},
            "yesterday_hard": yesterday_hard,
            "flags": flags, "planned": planned_out,
            "thresholds": {"hr_delta": _HR_DELTA_FLAG, "sleep_h": _SLEEP_FLAG,
                           "fatigue": _FATIGUE_FLAG, "estado": _ESTADO_FLAG},
            "explanation_text": f"{obs} {interp} {sug}"}


# ── Feedback loop: 👍/👎 sobre cualquier lectura ─────────────────────────────

@router.post("/api/feedback")
def post_feedback(context: str = Query(..., max_length=40),
                  verdict: str = Query(..., pattern="^(up|down)$"),
                  ref: str = Query(None, max_length=80),
                  note: str = Query(None, max_length=300)):
    """Registra si una lectura te sonó cierta. Es el insumo para afinar heurísticas."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_feedback_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO reading_feedback (context, ref_id, verdict, note)
            VALUES (%s, %s, %s, %s)
        """, (context, ref, verdict, note))
    conn.commit()
    return {"ok": True, "message": "Thanks — this sharpens the reads."}


@router.get("/api/feedback/summary")
def feedback_summary():
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_feedback_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT context, verdict, COUNT(*) FROM reading_feedback
            GROUP BY context, verdict ORDER BY context
        """)
        rows = cur.fetchall()
    out = {}
    for ctx, v, n in rows:
        out.setdefault(ctx, {"up": 0, "down": 0})[v] = n
    return {"ok": True, "summary": out}


# ── V8.1 — La semana que se reacomoda (propuesta + aceptar con un tap) ───────

_HARD_TYPES = ("intervals", "tempo_intervals", "tempo", "high_intensity", "climb")


@router.get("/gpt/week-rebalance")
def week_rebalance():
    """Si saltaste o se te pasó una sesión, propone dónde cabe de forma realista.
    Solo propone — aceptar es POST /api/plan-session/{id}/move."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_feedback_table(conn)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT ps.id, ps.description, ps.session_type, ps.planned_date, ps.status
            FROM plan_sessions ps JOIN training_plans tp USING (plan_id)
            WHERE tp.status='active'
              AND ps.planned_date >= %s AND ps.planned_date <= %s
            ORDER BY ps.planned_date
        """, (week_start, week_end))
        week = cur.fetchall()

    pendientes_pasadas = [r for r in week
                          if r[4] in ("planned", "skipped") and r[3] < today]
    if not pendientes_pasadas:
        return {"ok": True, "proposals": [],
                "explanation_text": ("The week is on track — nothing to rearrange. "
                                     "If you move a session yourself, the proposal will appear here.")}

    # Días ocupados y días duros existentes
    occupied = {r[3] for r in week if r[4] in ("planned", "completed") and r[3] >= today}
    hard_days = {r[3] for r in week
                 if r[4] in ("planned", "completed") and (r[2] or "") in _HARD_TYPES}

    proposals = []
    for ps_id, desc, stype, pdate, status in pendientes_pasadas:
        is_hard = (stype or "") in _HARD_TYPES
        candidate = None
        d = today
        while d <= week_end:
            ok_day = d not in occupied
            # No dos días duros seguidos
            if ok_day and is_hard and ((d - timedelta(days=1)) in hard_days or
                                       (d + timedelta(days=1)) in hard_days):
                ok_day = False
            if ok_day:
                candidate = d
                break
            d += timedelta(days=1)
        proposals.append({
            "plan_session_id": ps_id, "description": desc,
            "session_type": stype, "original_date": pdate.isoformat(),
            "proposed_date": candidate.isoformat() if candidate else None,
            "reason": ("fits without stacking two hard days" if candidate and is_hard
                       else "free day available" if candidate
                       else "no realistic slot left this week — no guilt, a phase is not defined by one session"),
        })
        if candidate:
            occupied.add(candidate)
            if is_hard:
                hard_days.add(candidate)

    movibles = [p for p in proposals if p["proposed_date"]]
    expl = ("OBSERVATION: " + str(len(pendientes_pasadas)) +
            " session(s) this week fell behind. INTERPRETATION: No guilt — "
            "guilt builds nothing; rearranging does. " +
            ("SUGGESTION: " + "; ".join(
                f"{p['description']} → {p['proposed_date']}" for p in movibles) + ". One tap and it is set."
             if movibles else
             "No realistic slot left this week — let it go and protect the next one."))
    return {"ok": True, "proposals": proposals, "explanation_text": expl}


@router.post("/api/plan-session/{ps_id}/move")
def move_plan_session(ps_id: int, new_date: str = Query(...),
                      reason: str = Query("reacomodo", max_length=120)):
    """Acepta un movimiento: cambia la fecha, conserva el historial (moved_from)."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_feedback_table(conn)
    try:
        nd = date.fromisoformat(new_date)
    except ValueError:
        raise HTTPException(422, "new_date must be YYYY-MM-DD")
    with conn.cursor() as cur:
        cur.execute("SELECT planned_date, status FROM plan_sessions WHERE id=%s", (ps_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Plan session not found")
        cur.execute("""
            UPDATE plan_sessions
            SET moved_from = COALESCE(moved_from, planned_date),
                planned_date = %s, status = 'planned', move_reason = %s
            WHERE id = %s
        """, (nd, reason, ps_id))
    conn.commit()
    return {"ok": True, "plan_session_id": ps_id,
            "moved_from": row[0].isoformat() if row[0] else None,
            "new_date": new_date,
            "message": "Moved. This is the week now — no guilt, on record."}


# ── V8.2 — Proyección al evento: ¿cómo llego si sigo así? ────────────────────

@router.get("/gpt/event-projection")
def event_projection():
    """
    Proyección simple y declarada: readiness de hoy + pendiente de las últimas
    8 semanas de eficiencia (proxy del motor aeróbico) extendida a la fecha del
    evento, con banda de incertidumbre. Estimación honesta, no promesa.
    """
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    today = date.today()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_name, event_type, event_date FROM mars_goals
            WHERE status='active' ORDER BY priority ASC LIMIT 1
        """)
        g = cur.fetchone()
        if not g or not g[2]:
            return {"ok": True, "available": False,
                    "explanation_text": "No dated goal — a projection needs a destination."}
        # Eficiencia promedio por semana, últimas 8 semanas (pendiente del motor)
        cur.execute("""
            SELECT date_trunc('week', start_date)::date AS wk,
                   AVG(efficiency_speed_hr)
            FROM clean_sessions
            WHERE start_date > %s AND efficiency_speed_hr IS NOT NULL
              AND sport_type IN ('Ride','VirtualRide')
            GROUP BY 1 ORDER BY 1
        """, (today - timedelta(weeks=8),))
        weeks = [(r[0], float(r[1])) for r in cur.fetchall() if r[1]]
        cur.execute("""
            SELECT meta FROM training_plans WHERE status='active'
            ORDER BY created_at DESC LIMIT 1
        """)
        pm = cur.fetchone()

    weeks_to_event = max(0, (g[2] - today).days // 7)

    # Readiness actual
    try:
        from capability_engine import calculate_readiness
        from routers.workout_identity import _evento_for_goal
        rr = calculate_readiness(conn, _evento_for_goal(g[0], g[1]))
        readiness_now = rr.get("readiness_score")
    except Exception as e:
        logger.warning(f"projection readiness fail: {e}")
        readiness_now = None

    if readiness_now is None or len(weeks) < 4:
        return {"ok": True, "available": False,
                "readiness_now": readiness_now,
                "explanation_text": ("OBSERVATION: Not enough data weeks yet "
                                     "to project honestly (minimum 4). "
                                     "INTERPRETATION: Better no number than an invented one.")}

    # Pendiente semanal de eficiencia (regresión lineal simple) → % por semana
    n = len(weeks)
    xs = list(range(n))
    ys = [w[1] for w in weeks]
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs) or 1
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    slope_pct_week = (slope / my * 100) if my else 0.0

    # El motor aeróbico pesa ~45% del readiness del evento; traducción conservadora:
    # cada +1%/sem de eficiencia ≈ +0.45 pts de readiness/sem (cap ±1.2/sem)
    weekly_gain = max(-1.2, min(1.2, slope_pct_week * 0.45))
    projected = max(0, min(100, readiness_now + weekly_gain * weeks_to_event))
    band = min(15, 3 + weeks_to_event * 0.6)  # incertidumbre crece con el horizonte

    # ¿Qué fase restante pesa más?
    key_phase = None
    today_s = today.isoformat()
    for ph in ((pm[0] or {}).get("phases") or []) if pm else []:
        if ph.get("name") == "build" and ph.get("end", "") >= today_s:
            key_phase = ph
            break

    trend_word = ("rising" if weekly_gain > 0.15 else
                  "falling" if weekly_gain < -0.15 else "steady")
    obs = (f"OBSERVATION: Readiness today {round(readiness_now)}/100, trend {trend_word} "
           f"({slope_pct_week:+.1f}%/week efficiency over 8 weeks).")
    interp = (f"INTERPRETATION: At this rate you arrive at {g[0]} with readiness "
              f"~{round(projected)} (band {round(max(0,projected-band))}–{round(min(100,projected+band))}).")
    plan_line = (f" The {key_phase['name']} phase (from {key_phase.get('start')}) is where the most "
                 "is at stake — protect those weeks." if key_phase else "")
    caveat = (" CONFIDENCE: simple linear estimate — assumes you keep training like these "
              "8 weeks; an injury, pause or phase change shifts it.")
    return {"ok": True, "available": True,
            "goal": {"event_name": g[0], "event_date": g[2].isoformat(),
                     "weeks_to_event": weeks_to_event},
            "readiness_now": round(readiness_now, 1),
            "projected_readiness": round(projected, 1),
            "band": round(band, 1),
            "weekly_gain_pts": round(weekly_gain, 2),
            "efficiency_slope_pct_week": round(slope_pct_week, 2),
            "weeks_sampled": n,
            "key_phase": (key_phase or {}).get("name"),
            "explanation_text": obs + " " + interp + plan_line + caveat}
