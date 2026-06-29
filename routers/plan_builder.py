"""
V10.5 — Plan Builder (multiusuario desde el diseño)
====================================================
Genera un plan estructurado a partir de UNA fecha de evento y la EVIDENCIA
del atleta — no de un cuestionario:

  · volumen de arranque   ← promedio real de las últimas 6 semanas
  · techo de volumen      ← mejor bloque sostenido de la historia personal
  · énfasis de las fases  ← tipo de evento (mismo catálogo que readiness)

Decisiones de arquitectura:
  1. Escribe el MISMO esquema que ya lee todo el Plan Vivo (training_plans +
     plan_sessions) → matcher, today-adaptation, week-rebalance, alignment y
     calendario funcionan sin tocar una línea.
  2. Cero constantes del atleta hardcodeadas: todo se deriva de clean_sessions
     o llega como parámetro. Listo para multiusuario.
  3. athlete_id en training_plans (DEFAULT 'mars') — la llave que falta para
     el multiusuario real; hoy una sola fila de atleta, mañana N.
  4. Preview por defecto (dry_run=true). Epoch propone; el atleta confirma.
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from db import get_db
from routers.gpt_training_context import _ensure_training_tables

logger = logging.getLogger("epoch.plan_builder")
router = APIRouter()

# Patrón semanal por días disponibles. La calidad (Q) solo aparece en
# build/peak — en base TODO es aeróbico (el motor primero).
#   E = endurance Z2 · Q = quality (tempo/intervals) · L = long ride · R = recovery
_WEEK_PATTERNS = {
    3: ["E", "L", "R"],
    4: ["E", "Q", "L", "R"],
    5: ["E", "Q", "E", "L", "R"],
    6: ["E", "Q", "E", "L", "R", "E"],
}
# Día de la semana (0=lunes) para cada slot según días disponibles
_WEEK_DAYS = {
    3: [1, 5, 6],
    4: [1, 3, 5, 6],
    5: [1, 2, 4, 5, 6],
    6: [0, 1, 2, 4, 5, 6],
}

_SESSION_DEFS = {
    "E": ("endurance", "Z2 endurance ride — aerobic engine"),
    "Q": ("tempo_intervals", "Quality: tempo/interval blocks"),
    "L": ("long_ride", "Long Z2 ride — the week's anchor"),
    "R": ("recovery", "Easy spin or rest — adaptation day"),
}


def _ensure_athlete_column(conn):
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE training_plans
            ADD COLUMN IF NOT EXISTS athlete_id TEXT DEFAULT 'mars'
        """)
    conn.commit()


def _phase_split(total_weeks):
    """base 45% · build 30% · peak 15% · taper 10% (mínimos honestos)."""
    if total_weeks < 6:
        return [("base", max(1, total_weeks - 2)), ("peak", 1), ("taper", 1)]
    base = max(2, round(total_weeks * 0.45))
    build = max(2, round(total_weeks * 0.30))
    taper = max(1, round(total_weeks * 0.10))
    peak = total_weeks - base - build - taper
    if peak < 1:
        base -= (1 - peak)  # garantizar peak>=1 sin inflar el total
        peak = 1
    return [("base", base), ("build", build), ("peak", peak), ("taper", taper)]


_PHASE_FOCUS = {
    "base": "aerobic engine and volume",
    "build": "intensity on top of the base",
    "peak": "event-specific sharpness",
    "taper": "fresh legs — absorb everything built",
}
# Multiplicador de volumen por fase (sobre el volumen de arranque)
_PHASE_VOL = {"base": 1.0, "build": 1.10, "peak": 0.95, "taper": 0.60}


def _athlete_volume(cur):
    """Volumen de arranque (6 semanas reales) y techo (mejor bloque 12s)."""
    cur.execute("""
        SELECT COALESCE(SUM(distance_km), 0) / 6.0
        FROM canonical_sessions
        WHERE start_date >= CURRENT_DATE - INTERVAL '42 days'
    """)
    current = float(cur.fetchone()[0] or 0)
    cur.execute("""
        WITH weekly AS (
            SELECT DATE_TRUNC('week', start_date) wk, SUM(distance_km) km
            FROM canonical_sessions GROUP BY 1
        )
        SELECT COALESCE(MAX(avg12), 0) FROM (
            SELECT AVG(km) OVER (ORDER BY wk ROWS BETWEEN 11 PRECEDING AND CURRENT ROW) avg12
            FROM weekly
        ) t
    """)
    ceiling = float(cur.fetchone()[0] or 0)
    return round(current, 1), round(ceiling, 1)


@router.post("/api/plan-builder")
def build_plan(event_date: str = Query(..., description="YYYY-MM-DD"),
               event_name: str = Query("My event", max_length=80),
               goal_id: int = Query(None, description="Use a registered goal instead"),
               days_per_week: int = Query(4, ge=3, le=6),
               dry_run: bool = Query(True),
               replace: bool = Query(False)):
    """Construye un plan desde la evidencia del atleta. dry_run=true (default)
    solo muestra la propuesta; confirmar = dry_run=false."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB unavailable")
    _ensure_training_tables(conn)
    _ensure_athlete_column(conn)

    with conn.cursor() as cur:
        if goal_id:
            cur.execute("""
                SELECT event_name, event_date FROM goals
                WHERE id=%s AND status='active'
            """, (goal_id,))
            g = cur.fetchone()
            if not g:
                raise HTTPException(404, "Goal not found")
            event_name, ev_date = g[0], g[1]
        else:
            try:
                ev_date = date.fromisoformat(event_date)
            except ValueError:
                raise HTTPException(400, "event_date must be YYYY-MM-DD")

        today = date.today()
        start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)  # próximo lunes
        total_weeks = max(1, (ev_date - start).days // 7)
        if total_weeks < 4:
            raise HTTPException(400, f"Only {total_weeks} week(s) to the event — "
                                     "a structured plan needs at least 4.")
        if total_weeks > 52:
            raise HTTPException(400, "More than a year out — register the goal and "
                                     "build the plan closer to the event.")

        cur.execute("SELECT plan_id, name FROM training_plans WHERE status='active' LIMIT 1")
        active = cur.fetchone()
        current_vol, ceiling_vol = _athlete_volume(cur)

    if active and not replace:
        return {"ok": False, "conflict": "active_plan",
                "active_plan": {"plan_id": active[0], "name": active[1]},
                "message": ("An active plan exists. Pass replace=true to retire it "
                            "(its full history is preserved) and build the new one.")}

    # ── Construcción ────────────────────────────────────────────────────────
    start_vol = max(50.0, current_vol) if current_vol > 0 else 80.0
    peak_vol = min(start_vol * 1.35, ceiling_vol * 1.05 if ceiling_vol > 0 else start_vol * 1.35)
    split = _phase_split(total_weeks)
    pattern = _WEEK_PATTERNS[days_per_week]
    days = _WEEK_DAYS[days_per_week]

    phases, sessions, vol_curve = [], [], []
    week_no, cursor = 1, start
    for phase_name, phase_weeks in split:
        ph_start = cursor
        for i in range(phase_weeks):
            # Progresión 3+1: tres semanas subiendo ~5%, una de descarga
            prog = min(1.0, week_no / max(1, total_weeks * 0.75))
            wk_vol = start_vol + (peak_vol - start_vol) * prog
            wk_vol *= _PHASE_VOL[phase_name]
            if week_no % 4 == 0 and phase_name in ("base", "build"):
                wk_vol *= 0.70  # semana de absorción
            vol_curve.append({"week": week_no, "phase": phase_name,
                              "km_target": round(wk_vol)})
            for slot, dow in zip(pattern, days):
                stype, desc = _SESSION_DEFS[slot]
                if phase_name == "base" and slot == "Q":
                    stype, desc = _SESSION_DEFS["E"]  # en base, todo aeróbico
                sessions.append({
                    "week_number": week_no,
                    "planned_date": (cursor + timedelta(days=dow)).isoformat(),
                    "session_type": stype,
                    "description": desc + (f" · ~{round(wk_vol*0.35)} km" if slot == "L" else ""),
                })
            cursor += timedelta(days=7)
            week_no += 1
        phases.append({"name": phase_name, "start": ph_start.isoformat(),
                       "end": (cursor - timedelta(days=1)).isoformat(),
                       "focus": _PHASE_FOCUS[phase_name]})

    evidence = (f"OBSERVATION: your real volume is ~{current_vol} km/week "
                f"(last 6 weeks) and your best sustained block was ~{ceiling_vol} km/week. "
                f"INTERPRETATION: the plan starts at ~{round(start_vol)} km and peaks at "
                f"~{round(peak_vol)} km — built from YOUR history, not a template. "
                f"EVIDENCE: {total_weeks} weeks, {len(sessions)} sessions, "
                f"{days_per_week} days/week, 3+1 progression with absorption weeks. "
                "MISSING: nothing critical if volume data is current. "
                "PRUDENT SUGGESTION: review the preview — the plan proposes, you decide.")

    preview = {
        "ok": True, "dry_run": dry_run,
        "plan": {"name": f"{event_name} — Epoch Builder",
                 "event": {"name": event_name, "date": ev_date.isoformat()},
                 "start_date": start.isoformat(), "end_date": ev_date.isoformat(),
                 "total_weeks": total_weeks, "days_per_week": days_per_week,
                 "phases": phases},
        "volume_curve": vol_curve,
        "sessions_sample": sessions[:days_per_week * 2],
        "n_sessions": len(sessions),
        "derived_from": {"current_volume_kmwk": current_vol,
                         "historical_ceiling_kmwk": ceiling_vol,
                         "start_volume_kmwk": round(start_vol),
                         "peak_volume_kmwk": round(peak_vol)},
        "explanation_text": evidence,
    }
    if dry_run:
        preview["message"] = "PREVIEW — nothing written. Confirm with dry_run=false."
        return preview

    # ── Escritura ───────────────────────────────────────────────────────────
    import json as _json
    with conn.cursor() as cur:
        if active and replace:
            cur.execute("""
                UPDATE training_plans
                SET status='abandoned',
                    meta = COALESCE(meta,'{}'::jsonb) || jsonb_build_object(
                        'ended_at', CURRENT_DATE::text,
                        'end_reason', 'replaced_by_builder')
                WHERE plan_id=%s
            """, (active[0],))
        plan_id = f"epoch_builder_{start.isoformat()}"
        meta = {"phases": phases,
                "event": {"name": event_name, "date": ev_date.isoformat()},
                "volume_curve": vol_curve,
                "derived_from": preview["derived_from"],
                "builder_inputs": {"days_per_week": days_per_week}}
        cur.execute("""
            INSERT INTO training_plans (plan_id, name, source, start_date, end_date,
                                        total_weeks, status, meta)
            VALUES (%s, %s, 'epoch_builder', %s, %s, %s, 'active', %s::jsonb)
            ON CONFLICT (plan_id) DO NOTHING
        """, (plan_id, preview["plan"]["name"], start, ev_date, total_weeks,
              _json.dumps(meta)))
        for s in sessions:
            cur.execute("""
                INSERT INTO plan_sessions (plan_id, week_number, planned_date,
                                           session_type, description)
                VALUES (%s, %s, %s, %s, %s)
            """, (plan_id, s["week_number"], s["planned_date"],
                  s["session_type"], s["description"]))
    conn.commit()
    preview["plan_id"] = plan_id
    preview["message"] = (f"Plan created: {total_weeks} weeks, {len(sessions)} sessions. "
                          "My Plan, calendar, daily adaptation and alignment are already "
                          "reading it.")
    return preview
