"""
shared/helpers.py — Funciones helper compartidas entre routers y main.py
=========================================================================
TD-010A: extraídas de main.py para evitar duplicación.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

logger = logging.getLogger("mars_fit")


# ── Capacidades ───────────────────────────────────────────────────────────────

def _normalize_capability_name(nombre: str) -> str:
    """Normaliza alias en español/inglés al key canónico del motor."""
    normalized = nombre.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "aerobico": "motor_aerobico",
        "motor": "motor_aerobico",
        "composicion": "composicion_corporal",
        "nutricion": "nutricion_deportiva",
    }
    return aliases.get(normalized, normalized)


def _find_capability(result: dict, nombre: str) -> dict:
    """Busca una capacidad por nombre (normalizado) en el resultado del motor."""
    normalized = _normalize_capability_name(nombre)
    capability = next(
        (item for item in result["capabilities"] if item["key"] == normalized),
        None,
    )
    if not capability:
        raise HTTPException(404, "Capacidad no encontrada")
    return capability


def _previous_capability_run(conn, capability_key: str) -> Optional[dict]:
    """Retorna la última ejecución oficial de una capacidad para calcular delta."""
    from db import _ensure_capability_runs_table
    _ensure_capability_runs_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT capability_json
            FROM capability_runs
            WHERE capability_key = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (capability_key,))
        row = cur.fetchone()
    return row[0] if row else None


def detect_and_save_achievements(conn, session_id, result):
    """Detecta récords automáticamente al subir un FIT y los guarda en achievements."""
    s = result.get("session", {})
    if not s.get("start_time"):
        return []
    new_achievements = []
    checks = [
        ("max_distance", "distance_km", s.get("distance_km"), "Mayor distancia"),
        ("max_ascent", "ascent_m", s.get("ascent_m"), "Mayor ascenso"),
        ("max_speed", "avg_speed_kmh", s.get("avg_speed_kmh"), "Mayor velocidad promedio"),
        ("best_efficiency", "efficiency",
         round(float(s.get("avg_speed_kmh", 0)) / float(s.get("avg_hr_bpm", 1)), 4)
         if s.get("avg_hr_bpm") else None,
         "Mejor eficiencia aeróbica"),
    ]
    sport = s.get("sport", "cycling")
    try:
        with conn.cursor() as cur:
            for ach_type, col, value, label in checks:
                if not value:
                    continue
                fval = float(value)
                if col == "efficiency":
                    cur.execute("""
                        SELECT MAX(avg_speed_kmh/NULLIF(avg_hr_bpm,0))
                        FROM sessions_clean_compat WHERE sport=%s AND session_id != %s
                        AND avg_hr_bpm > 0
                    """, (sport, session_id))
                else:
                    cur.execute(f"""
                        SELECT MAX({col}) FROM sessions_clean_compat
                        WHERE sport=%s AND session_id != %s
                    """, (sport, session_id))
                row = cur.fetchone()
                prev_best = float(row[0]) if row and row[0] else None
                if prev_best is None or fval > prev_best:
                    date_val = s.get("start_time", "")[:10]
                    fmt_val = f"{fval:.2f}" if col not in ("ascent_m",) else f"{int(fval)} m"
                    desc = f"🏆 {label}: {fmt_val}"
                    if prev_best:
                        diff = fval - prev_best
                        desc += f" (+{diff:.2f} vs anterior)"
                    cur.execute("""
                        INSERT INTO achievements (session_id, date, type, metric, value, prev_best, description)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """, (session_id, date_val, ach_type, col, fval, prev_best, desc))
                    new_achievements.append(desc)
                    logger.info(f"Achievement: {desc}")
    except Exception as e:
        logger.error(f"Achievement detection error: {e}")
    return new_achievements


def generate_weekly_snapshot(conn):
    """Genera o actualiza la semana más reciente usando la columna limpia."""
    try:
        from tools.backfill_athlete_snapshots import build_snapshots, write_snapshots

        snapshots = build_snapshots(conn)
        if not snapshots:
            return
        latest = snapshots[-1]
        write_snapshots(conn, [latest])
        logger.info(
            "Weekly snapshot saved: %s sessions=%s km=%s",
            latest["week_start"],
            latest["sessions"],
            latest["km_week"],
        )
    except Exception as e:
        logger.error(f"Weekly snapshot error: {e}")
