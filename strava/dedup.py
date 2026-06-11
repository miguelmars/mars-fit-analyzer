"""
strava/dedup.py — Deduplicación segura Strava ↔ clean_sessions (Fase C)

Compara strava_activities_raw (Supabase) vs clean_sessions (Railway PostgreSQL).
Categoriza cada actividad de Strava en:

  exact_match    → fecha ±5 min · distancia ±2% · duración ±3%
                   Acción: solo enlazar strava_id (link_only)
  probable_match → fecha ±30 min · distancia ±5% · duración ±10%
                   Acción: requiere revisión antes de enlazar
  conflict       → fecha ±60 min pero distancia/duración difieren >10%
                   Acción: bloquear — investigar manualmente
  new_session    → sin equivalente en Garmin dentro de 60 min
                   Acción: crear sesión nueva en clean_sessions

REGLA OPERATIVA: La transformación está BLOQUEADA hasta que el atleta
revise muestras y apruebe en dedup_approval_log.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("bitacora.strava.dedup")

# ── Umbrales de matching ─────────────────────────────────────────────────────
_EXACT_DATE_MIN     = 5    # ±5 minutos
_EXACT_DIST_PCT     = 0.02  # ±2%
_EXACT_DUR_PCT      = 0.03  # ±3%

_PROB_DATE_MIN      = 30   # ±30 minutos
_PROB_DIST_PCT      = 0.05  # ±5%
_PROB_DUR_PCT       = 0.10  # ±10%

_CONFLICT_DATE_MIN  = 60   # ±60 min para considerar "cerca"

_SAMPLES_PER_CAT    = 10   # Ejemplos por categoría en /dedup-samples


# ── Funciones de comparación ─────────────────────────────────────────────────

def _date_diff_minutes(dt_a: str, dt_b: str) -> float:
    """Diferencia absoluta en minutos entre dos timestamps ISO."""
    try:
        def _parse(s: str) -> datetime:
            s = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except Exception:
                return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        a = _parse(dt_a)
        b = _parse(dt_b)
        return abs((a - b).total_seconds()) / 60
    except Exception:
        return 9999.0


def _pct_diff(a: float, b: float) -> float:
    """Diferencia porcentual entre dos valores. 0 si alguno es None/0."""
    if not a or not b:
        return 0.0
    return abs(a - b) / max(a, b)


def _classify(
    date_diff_min: float,
    dist_diff_pct: float,
    dur_diff_pct: float,
) -> str:
    """Clasifica el par (Strava, Garmin) según umbrales."""
    # Exact match
    if (date_diff_min <= _EXACT_DATE_MIN and
            dist_diff_pct <= _EXACT_DIST_PCT and
            dur_diff_pct <= _EXACT_DUR_PCT):
        return "exact_match"

    # Probable match
    if (date_diff_min <= _PROB_DATE_MIN and
            dist_diff_pct <= _PROB_DIST_PCT and
            dur_diff_pct <= _PROB_DUR_PCT):
        return "probable_match"

    # Conflict: fechas cerca pero datos muy diferentes
    if date_diff_min <= _CONFLICT_DATE_MIN:
        return "conflict"

    return "new_session"


# ── Función principal de diagnóstico ─────────────────────────────────────────

def run_dedup_diagnosis(sport_type: Optional[str] = None) -> dict:
    """
    Compara strava_activities_raw vs clean_sessions y categoriza cada actividad.
    Guarda el diagnóstico para uso posterior por /dedup-samples y /approve-samples.

    Args:
        sport_type: filtrar por tipo de deporte (opcional)

    Returns:
        dict con conteos por categoría y diagnosis_id para tracking.
    """
    from .auth import get_supabase
    from db import get_db  # Railway PostgreSQL

    sb = get_supabase()

    # ── 1. Leer actividades de Strava staging (paginado) ─────────────────────
    logger.info("Dedup: leyendo strava_activities_raw...")
    strava_rows: list = []
    page = 0
    while True:
        q = (
            sb.table("strava_activities_raw")
            .select("strava_id,started_at,distance_m,moving_time_s,sport_type,name,started_at_local")
            .neq("transform_status", "done")
            .range(page * 1000, (page + 1) * 1000 - 1)
        )
        if sport_type:
            q = q.ilike("sport_type", f"%{sport_type}%")
        chunk = q.execute().data or []
        strava_rows.extend(chunk)
        if len(chunk) < 1000:
            break
        page += 1
    logger.info(f"Dedup: {len(strava_rows)} actividades Strava para analizar")

    # ── 2. Leer clean_sessions de Railway ────────────────────────────────────
    logger.info("Dedup: leyendo clean_sessions...")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            clean_session_id,
            start_time,
            distance_km,
            duration_s,
            sport,
            source_activity_id
        FROM clean_sessions
        ORDER BY start_time DESC
    """)
    garmin_rows = cur.fetchall()
    cur.close()

    # Convertir a lista de dicts para comparación
    garmin = [
        {
            "session_id":         r[0],
            "start_time":         r[1].isoformat() if hasattr(r[1], 'isoformat') else str(r[1]),
            "distance_m":         (r[2] or 0) * 1000,  # km → m
            "duration_s":         r[3] or 0,
            "activity_type":      r[4],  # maps to clean_sessions.sport
            "source_activity_id": r[5],
        }
        for r in garmin_rows
    ]
    logger.info(f"Dedup: {len(garmin)} sesiones en clean_sessions")

    # ── 3. Clasificar cada actividad Strava ──────────────────────────────────
    results = {
        "exact_match":    [],  # → link_only
        "probable_match": [],
        "conflict":       [],
        "new_session":    [],
        "already_linked": [],  # strava_id ya en source_activity_id
    }

    already_linked_ids = {
        g["source_activity_id"]
        for g in garmin
        if g.get("source_activity_id")
    }

    # Índice por fecha para bajar de O(n*m) a O(n * pocos) — crítico con 3k × 3k
    from collections import defaultdict
    garmin_by_date: dict = defaultdict(list)
    for g in garmin:
        day = (g["start_time"] or "")[:10]  # YYYY-MM-DD
        if day:
            garmin_by_date[day].append(g)

    for s in strava_rows:
        strava_id = str(s["strava_id"])

        # Ya enlazado anteriormente
        if strava_id in already_linked_ids:
            results["already_linked"].append({
                "strava_id":   s["strava_id"],
                "name":        s.get("name"),
                "started_at":  s.get("started_at"),
            })
            continue

        best_category = "new_session"
        best_match    = None
        best_date_diff = 9999.0

        s_dist = s.get("distance_m") or 0
        s_dur  = s.get("moving_time_s") or 0
        s_date = s.get("started_at") or ""
        s_day  = s_date[:10]  # YYYY-MM-DD

        # Solo comparar contra sesiones Garmin del mismo día ±1 día
        from datetime import timedelta
        try:
            s_dt = datetime.fromisoformat(s_day)
            candidate_days = [
                (s_dt - timedelta(days=1)).strftime("%Y-%m-%d"),
                s_day,
                (s_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
            ]
        except Exception:
            candidate_days = [s_day]

        candidates = []
        for d in candidate_days:
            candidates.extend(garmin_by_date.get(d, []))

        for g in candidates:
            date_diff = _date_diff_minutes(s_date, g["start_time"])
            if date_diff > _CONFLICT_DATE_MIN:
                continue  # Demasiado lejos en tiempo — saltar

            dist_diff = _pct_diff(s_dist, g["distance_m"])
            dur_diff  = _pct_diff(s_dur,  g["duration_s"])
            category  = _classify(date_diff, dist_diff, dur_diff)

            # Prioridad: exact > probable > conflict > new
            prio = {"exact_match": 0, "probable_match": 1, "conflict": 2, "new_session": 3}
            if prio.get(category, 9) < prio.get(best_category, 9) or (
                prio.get(category, 9) == prio.get(best_category, 9) and date_diff < best_date_diff
            ):
                best_category = category
                best_date_diff = date_diff
                best_match = {
                    "garmin_session_id": g["session_id"],
                    "garmin_date":       g["start_time"],
                    "garmin_dist_m":     g["distance_m"],
                    "garmin_dur_s":      g["duration_s"],
                    "delta_min":         round(date_diff, 1),
                    "dist_diff_pct":     round(dist_diff * 100, 1),
                    "dur_diff_pct":      round(dur_diff * 100, 1),
                }

        entry = {
            "strava_id":      s["strava_id"],
            "name":           s.get("name"),
            "sport_type":     s.get("sport_type"),
            "strava_date":    s.get("started_at"),
            "strava_dist_m":  s_dist,
            "strava_dur_s":   s_dur,
            "category":       best_category,
            "best_match":     best_match,
        }
        results[best_category].append(entry)

    # ── 4. Construir resumen ─────────────────────────────────────────────────
    diagnosis_id = str(uuid.uuid4())[:8]
    total = len(strava_rows)
    safe  = len(results["exact_match"]) + len(results["already_linked"])

    summary = {
        "diagnosis_id":       diagnosis_id,
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "total_strava_raw":   total,
        "exact_matches":      len(results["exact_match"]),
        "probable_matches":   len(results["probable_match"]),
        "conflicts":          len(results["conflict"]),
        "new_sessions":       len(results["new_session"]),
        "already_linked":     len(results["already_linked"]),
        "link_only":          len(results["exact_match"]),
        "safe_to_transform":  len(results["conflict"]) == 0,
        "semaforo": (
            "🟢 VERDE"   if len(results["conflict"]) == 0 and total > 0 else
            "🟡 AMARILLO" if len(results["conflict"]) > 0 else
            "🔴 ROJO"
        ),
        "nota": (
            "Revisión de muestras requerida antes de transformar."
            if len(results["conflict"]) > 0
            else "Sin conflictos — revisar muestras y aprobar para habilitar transformación."
        ),
    }

    # ── 5. Guardar para /dedup-samples ───────────────────────────────────────
    # Guardamos en strava_sync_state como JSONB por simplicidad
    # (evita una tabla extra en Supabase)
    try:
        from .auth import get_supabase as _sb
        _sb().table("strava_sync_state").update({
            "last_diagnosis_id":      diagnosis_id,
            "last_diagnosis_summary": summary,
            "last_diagnosis_detail":  {
                "exact_match":    results["exact_match"][:50],
                "probable_match": results["probable_match"][:50],
                "conflict":       results["conflict"][:50],
                "new_session":    results["new_session"][:50],
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()
    except Exception as e:
        logger.warning(f"No se pudo guardar diagnóstico en sync_state: {e}")

    logger.info(f"Dedup diagnosis {diagnosis_id}: {summary}")
    return summary


# ── Muestras para revisión humana ─────────────────────────────────────────────

def get_dedup_samples(diagnosis_id: str) -> dict:
    """
    Devuelve 10 ejemplos de cada categoría para revisión humana.
    Requiere que run_dedup_diagnosis() haya corrido antes.
    """
    from .auth import get_supabase

    sb = get_supabase()
    state = sb.table("strava_sync_state").select(
        "last_diagnosis_id,last_diagnosis_detail,last_diagnosis_summary"
    ).eq("id", 1).execute().data

    if not state:
        return {"error": "No hay diagnóstico guardado. Corre /dedup-diagnosis primero."}

    row = state[0]
    saved_id = row.get("last_diagnosis_id")

    if saved_id != diagnosis_id:
        return {
            "error": f"diagnosis_id '{diagnosis_id}' no coincide con el guardado '{saved_id}'. "
                     "Corre /dedup-diagnosis de nuevo.",
            "current_diagnosis_id": saved_id,
        }

    detail  = row.get("last_diagnosis_detail") or {}
    summary = row.get("last_diagnosis_summary") or {}

    samples = {}
    for cat in ("exact_match", "probable_match", "conflict", "new_session"):
        samples[cat] = (detail.get(cat) or [])[:_SAMPLES_PER_CAT]

    return {
        "diagnosis_id": diagnosis_id,
        "summary":      summary,
        "samples":      samples,
        "instrucciones": (
            "Revisa los ejemplos. Si todo se ve correcto, llama a "
            "POST /api/strava/approve-samples con este diagnosis_id. "
            "El botón de transformar aparece solo después de aprobar."
        ),
    }


# ── Registro de aprobación ────────────────────────────────────────────────────

def record_approval(diagnosis_id: str, approved_by: str, notes: str = "") -> dict:
    """
    Guarda la aprobación humana en dedup_approval_log (Railway PostgreSQL).
    Habilita el endpoint POST /api/strava/transform.
    """
    from .auth import get_supabase
    from db import get_db

    # Leer el summary del diagnóstico
    sb = get_supabase()
    state = sb.table("strava_sync_state").select(
        "last_diagnosis_id,last_diagnosis_summary"
    ).eq("id", 1).execute().data

    if not state or state[0].get("last_diagnosis_id") != diagnosis_id:
        return {
            "ok": False,
            "error": f"diagnosis_id '{diagnosis_id}' no encontrado. "
                     "Corre /dedup-diagnosis primero.",
        }

    summary = state[0].get("last_diagnosis_summary") or {}

    # Insertar en dedup_approval_log (Railway)
    conn = get_db()
    cur  = conn.cursor()

    sample_set_id = str(uuid.uuid4())[:8]
    cur.execute("""
        INSERT INTO dedup_approval_log
            (diagnosis_id, sample_set_id, approved_by, notes,
             total_strava, exact_matches, probable_matches, new_sessions, conflicts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, approved_at
    """, (
        diagnosis_id,
        sample_set_id,
        approved_by,
        notes,
        summary.get("total_strava_raw"),
        summary.get("exact_matches"),
        summary.get("probable_matches"),
        summary.get("new_sessions"),
        summary.get("conflicts"),
    ))
    row = cur.fetchone()
    conn.commit()
    cur.close()

    return {
        "ok":            True,
        "approval_id":   row[0],
        "approved_at":   row[1].isoformat() if hasattr(row[1], 'isoformat') else str(row[1]),
        "diagnosis_id":  diagnosis_id,
        "sample_set_id": sample_set_id,
        "siguiente_paso": "Semáforo VERDE. Puedes ejecutar POST /api/strava/transform ahora.",
    }
