"""
routers/admin.py — Endpoints /admin/* de Bitácora Mars
=======================================================
TD-010A: extraídos de main.py para reducir el monolito.

Incluir en main.py:
    from routers.admin import router as admin_router
    app.include_router(admin_router)
"""
import os
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from db import (
    get_db,
    _ensure_garmin_staging_tables,
    _ensure_clean_sessions_table,
    _ensure_capability_runs_table,
    _ensure_session_environment_table,
    _ensure_gear_activity_links_table,
)
from shared.helpers import (
    generate_weekly_snapshot,
    _find_capability,
    _previous_capability_run,
)

logger = logging.getLogger("mars_fit")

router = APIRouter(tags=["admin"])


# ── Helper: validación de token ───────────────────────────────────────────────

def _check_token(token: str):
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token or token != admin_token:
        raise HTTPException(401, "Token requerido")


# ── /admin/health ─────────────────────────────────────────────────────────────

@router.get("/admin/health")
def admin_health(token: str = None):
    """Estado de salud del sistema — tamaño DB, última sesión, tiempos de respuesta."""
    _check_token(token)
    import time
    conn = get_db()
    if not conn:
        return {"api": "ok", "db": "error"}
    try:
        t0 = time.time()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clean_sessions")
            sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM session_records")
            records = cur.fetchone()[0]
            cur.execute("SELECT MAX(start_time), MAX(created_at) FROM clean_sessions")
            row = cur.fetchone()
            last_session = str(row[0])[:10] if row[0] else None
            last_upload = str(row[1])[:19] if row[1] else None
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            db_size = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM achievements")
            achievements = cur.fetchone()[0]
        db_ms = round((time.time() - t0) * 1000, 1)
        return {
            "api": "ok", "db": "ok",
            "db_size": db_size,
            "sessions": sessions,
            "session_records": records,
            "achievements": achievements,
            "last_session_date": last_session,
            "last_upload": last_upload,
            "db_response_ms": db_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Health error: {e}")
        return {"api": "ok", "db": "error", "detail": str(e)}


# ── /admin/achievements ───────────────────────────────────────────────────────

@router.get("/admin/achievements")
def get_achievements(limit: int = 20):
    """Últimos hitos detectados automáticamente."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.id, a.session_id, a.date, a.type, a.metric,
                       a.value, a.prev_best, a.description
                FROM achievements a
                ORDER BY a.date DESC, a.id DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        result = [dict(zip(cols, r)) for r in rows]
        for item in result:
            for k, v in item.items():
                if hasattr(v, "isoformat"):
                    item[k] = str(v)
        return {"achievements": result, "total": len(result)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── /admin/generate-snapshot ─────────────────────────────────────────────────

@router.get("/admin/generate-snapshot")
def admin_generate_snapshot(token: str = None):
    """Genera manualmente el snapshot semanal del atleta."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        generate_weekly_snapshot(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM athlete_snapshots ORDER BY week_start DESC LIMIT 1")
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        snap = dict(zip(cols, row)) if row else {}
        for k, v in snap.items():
            if hasattr(v, "isoformat"):
                snap[k] = str(v)
        return {"ok": True, "snapshot": snap}
    except Exception as e:
        logger.error(f"Generate snapshot error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/backfill-snapshots ─────────────────────────────────────────────────

@router.get("/admin/backfill-snapshots")
def admin_backfill_snapshots(token: str = None):
    """
    Genera TODOS los snapshots semanales históricos desde clean_sessions.
    Necesario una sola vez si athlete_snapshots está vacío.
    Sin esto las capacidades muestran score=None.
    """
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.backfill_athlete_snapshots import build_snapshots, write_snapshots
        snapshots = build_snapshots(conn)
        if not snapshots:
            return {"ok": True, "message": "Sin sesiones en clean_sessions — backfill de Strava pendiente", "count": 0}
        write_snapshots(conn, snapshots)
        return {
            "ok": True,
            "count": len(snapshots),
            "first_week": str(snapshots[0]["week_start"]),
            "last_week": str(snapshots[-1]["week_start"]),
            "message": f"{len(snapshots)} semanas de snapshots generadas. Las capacidades ahora tienen datos."
        }
    except Exception as e:
        logger.error(f"Backfill snapshots error: {e}", exc_info=True)
        raise HTTPException(500, str(e))


# ── /admin/diagnostics ────────────────────────────────────────────────────────

@router.get("/admin/diagnostics")
def admin_diagnostics(token: str = None):
    """Diagnóstico rápido de la API y conteos de todas las tablas."""
    _check_token(token)
    conn = get_db()
    if not conn:
        return {"api": "ok", "db": "error", "detail": "DB no disponible"}
    try:
        counts = {}
        tables = ["sessions", "session_records", "post_session", "gear",
                  "maintenance", "fuerza", "wellness", "accidents",
                  "athlete_profile", "athlete_tests", "recovery",
                  "achievements", "athlete_snapshots",
                  "garmin_export_activities", "garmin_export_gear",
                  "garmin_export_sleep", "clean_sessions", "zone_models",
                  "session_environment", "capability_runs"]
        with conn.cursor() as cur:
            for table in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cur.fetchone()[0]
                except Exception:
                    counts[table] = "tabla no existe"
        return {
            "api": "ok",
            "db": "ok",
            **counts,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Diagnostics error: {e}")
        return {"api": "ok", "db": "error", "detail": str(e)}


# ── /admin/garmin-staging ─────────────────────────────────────────────────────

@router.get("/admin/garmin-staging")
def admin_garmin_staging(token: str = None):
    """Conteos de staging Garmin sin mezclar con sesiones actuales."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_garmin_staging_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities")
            activities_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities WHERE is_probable_real_activity IS TRUE")
            real_candidates = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_gear")
            gear_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_sleep")
            sleep_count = cur.fetchone()[0]
            cur.execute("""
                SELECT sport, COUNT(*)
                FROM garmin_export_activities
                GROUP BY sport
                ORDER BY COUNT(*) DESC
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT MIN(start_time_local)::text, MAX(start_time_local)::text
                FROM garmin_export_activities
            """)
            date_min, date_max = cur.fetchone()
            cur.execute("""
                SELECT confidence, COUNT(*)
                FROM garmin_export_sleep
                GROUP BY confidence
                ORDER BY COUNT(*) DESC
            """)
            sleep_confidence = [{"confidence": r[0], "count": r[1]} for r in cur.fetchall()]
        return {
            "ok": True,
            "activities": activities_count,
            "real_activity_candidates": real_candidates,
            "gear": gear_count,
            "sleep": sleep_count,
            "date_min": date_min,
            "date_max": date_max,
            "by_sport": by_sport,
            "sleep_confidence": sleep_confidence,
        }
    except Exception as e:
        logger.error(f"Garmin staging diagnostics error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/garmin-compare ─────────────────────────────────────────────────────

@router.get("/admin/garmin-compare")
def admin_garmin_compare(token: str = None):
    """Compara staging Garmin contra sessions actuales para decidir limpieza."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_garmin_staging_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sessions")
            current_sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM garmin_export_activities")
            staging_activities = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*)
                FROM garmin_export_activities g
                JOIN sessions s
                  ON s.sport = g.sport
                 AND ABS(COALESCE(s.distance_km, 0) - COALESCE(g.distance_km::float, 0)) < 0.05
                 AND ABS(COALESCE(s.duration_s, 0) - COALESCE(g.duration_s, 0)) <= 5
                 AND LEFT(COALESCE(s.start_time, ''), 10) = LEFT(g.start_time_local::text, 10)
            """)
            fuzzy_matches = cur.fetchone()[0]
            cur.execute("""
                SELECT LEFT(COALESCE(start_time, ''), 10) AS date,
                       sport,
                       ROUND(COALESCE(distance_km, 0)::numeric, 2) AS distance_km,
                       COALESCE(duration_s, 0) AS duration_s,
                       COUNT(*) AS duplicates
                FROM sessions
                GROUP BY 1, 2, 3, 4
                HAVING COUNT(*) > 1
                ORDER BY duplicates DESC
                LIMIT 20
            """)
            duplicate_groups = [
                {
                    "date": r[0], "sport": r[1],
                    "distance_km": float(r[2]) if r[2] is not None else None,
                    "duration_s": r[3], "duplicates": r[4],
                }
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT sport, COUNT(*) FROM sessions GROUP BY sport ORDER BY COUNT(*) DESC
            """)
            current_by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT sport, COUNT(*) FROM garmin_export_activities GROUP BY sport ORDER BY COUNT(*) DESC
            """)
            staging_by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
        return {
            "ok": True,
            "current_sessions": current_sessions,
            "garmin_staging_activities": staging_activities,
            "fuzzy_matches_staging_to_current": fuzzy_matches,
            "current_by_sport": current_by_sport,
            "staging_by_sport": staging_by_sport,
            "top_duplicate_groups_current_sessions": duplicate_groups,
            "verdict": "Usar staging Garmin como indice limpio y limpiar sessions despues de revisar duplicados.",
        }
    except Exception as e:
        logger.error(f"Garmin compare error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/clean-sessions ─────────────────────────────────────────────────────

@router.get("/admin/clean-sessions")
def admin_clean_sessions(token: str = None):
    """Estado de la capa limpia de sesiones."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_clean_sessions_table(conn)
        _ensure_gear_activity_links_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM clean_sessions")
            total = cur.fetchone()[0]
            cur.execute("""
                SELECT source, quality, COUNT(*)
                FROM clean_sessions
                GROUP BY source, quality
                ORDER BY source, quality
            """)
            by_source_quality = [
                {"source": r[0], "quality": r[1], "count": r[2]}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT sport, COUNT(*) FROM clean_sessions
                GROUP BY sport ORDER BY COUNT(*) DESC
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("SELECT MIN(start_time)::text, MAX(start_time)::text FROM clean_sessions")
            date_min, date_max = cur.fetchone()
            cur.execute("""
                SELECT COUNT(*) FROM sessions
                WHERE COALESCE(start_time,'')='' AND COALESCE(sport,'')=''
                  AND COALESCE(distance_km,0)=0 AND COALESCE(duration_s,0)=0
            """)
            current_junk_zero_empty = cur.fetchone()[0]
        return {
            "ok": True,
            "clean_sessions": total,
            "date_min": date_min,
            "date_max": date_max,
            "by_source_quality": by_source_quality,
            "by_sport": by_sport,
            "current_junk_zero_empty": current_junk_zero_empty,
            "verdict": "clean_sessions es la capa recomendada para fase 1/2; sessions queda como tabla historica cruda.",
        }
    except Exception as e:
        logger.error(f"Clean sessions diagnostics error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/zone-models ────────────────────────────────────────────────────────

@router.get("/admin/zone-models")
def admin_zone_models(token: str = None):
    """List versioned zone models and recalculation coverage."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.recalculate_mars_zones import ensure_zone_models
        ensure_zone_models(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT model_id, sport, lt_bpm, max_hr_bpm, method, zones_json,
                       effective_from, effective_to, status, source, notes, created_at
                FROM zone_models
                ORDER BY sport, effective_from, model_id
            """)
            columns = [description[0] for description in cur.description]
            models = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE zone_model_used IS NOT NULL) AS calculated,
                    COUNT(*) FILTER (WHERE zone_confidence LIKE 'telemetry_%') AS telemetry,
                    COUNT(*) FILTER (WHERE zone_confidence = 'session_average_estimate') AS estimated,
                    COUNT(*) FILTER (WHERE zone_confidence = 'no_heart_rate') AS no_hr
                FROM clean_sessions
                WHERE sport IN ('cycling','indoor_cycling','running','trail_running','treadmill_running')
            """)
            calculated, telemetry, estimated, no_hr = cur.fetchone()
        for model in models:
            for key, value in list(model.items()):
                if hasattr(value, "isoformat"):
                    model[key] = value.isoformat()
        return {
            "ok": True,
            "models": models,
            "coverage": {
                "calculated": calculated,
                "telemetry": telemetry,
                "session_average_estimate": estimated,
                "no_heart_rate": no_hr,
            },
            "rules": {
                "original_is_immutable": True,
                "score_and_confidence_are_separate": True,
                "models_are_append_only": True,
            },
        }
    except Exception as e:
        logger.error(f"Zone model diagnostics error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/recalculate-zones ─────────────────────────────────────────────────

@router.post("/admin/recalculate-zones")
def admin_recalculate_zones(execute: bool = False, token: str = None):
    """Preview or execute versioned Mars zone recalculation."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.recalculate_mars_zones import recalculate_zones
        result = recalculate_zones(conn, execute=execute)
        result["next_step"] = (
            "Validate /admin/zone-models and snapshot coverage before Capability Engine."
            if execute
            else "Re-run with execute=true only after reviewing this dry-run summary."
        )
        return result
    except Exception as e:
        logger.error(f"Zone recalculation error: {e}")
        raise HTTPException(500, str(e))


@router.get("/admin/recalculate-zones/preview")
def admin_recalculate_zones_preview(token: str = None):
    """Safe read-only preview of the Mars zone recalculation population."""
    return admin_recalculate_zones(execute=False, token=token)


# ── /admin/session-environment ────────────────────────────────────────────────

@router.post("/admin/session-environment")
def admin_session_environment(execute: bool = False, token: str = None):
    """Preview or build altitude/location context without changing source sessions."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from tools.backfill_session_environment import backfill_session_environment
        return backfill_session_environment(conn, execute=execute)
    except Exception as e:
        logger.error(f"Session environment error: {e}")
        raise HTTPException(500, str(e))


@router.get("/admin/session-environment/preview")
def admin_session_environment_preview(token: str = None):
    """Read-only coverage preview for session_environment."""
    return admin_session_environment(execute=False, token=token)


# ── /admin/phase1-audit ───────────────────────────────────────────────────────

@router.get("/admin/phase1-audit")
def admin_phase1_audit(token: str = None):
    """Auditoría compacta de la columna Fase 1: data, gear, rutas y telemetría."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_clean_sessions_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE calories IS NOT NULL AND calories > 0) AS with_calories,
                    COALESCE(SUM(calories), 0) AS calories_total,
                    COUNT(*) FILTER (WHERE start_lat IS NOT NULL AND start_lon IS NOT NULL) AS with_start_gps,
                    COUNT(*) FILTER (WHERE end_lat IS NOT NULL AND end_lon IS NOT NULL) AS with_end_gps,
                    COUNT(*) FILTER (WHERE route_id IS NOT NULL) AS with_route_id,
                    MIN(start_time)::text AS date_min,
                    MAX(start_time)::text AS date_max
                FROM clean_sessions
            """)
            total, with_cal, cal_total, start_gps, end_gps, with_route, date_min, date_max = cur.fetchone()
            cur.execute("""
                SELECT sport, COUNT(*) FROM clean_sessions
                GROUP BY sport ORDER BY COUNT(*) DESC LIMIT 12
            """)
            by_sport = [{"sport": r[0], "count": r[1]} for r in cur.fetchall()]
            cur.execute("""
                SELECT COUNT(*) AS gear_total,
                    COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) NOT LIKE '%retired%') AS active_or_unknown,
                    COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) LIKE '%retired%') AS retired
                FROM garmin_export_gear
            """)
            gear_total, gear_active, gear_retired = cur.fetchone()
            cur.execute("""
                SELECT name, type, status, max_distance_km
                FROM garmin_export_gear
                ORDER BY CASE WHEN LOWER(COALESCE(status,'')) LIKE '%retired%' THEN 1 ELSE 0 END, name
                LIMIT 12
            """)
            gear_sample = [
                {"name": r[0], "type": r[1], "status": r[2],
                 "max_distance_km": float(r[3]) if r[3] is not None else None}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT COUNT(*), COUNT(DISTINCT gear_id),
                       COUNT(DISTINCT COALESCE(session_id, source_activity_id))
                FROM gear_activity_links
            """)
            gear_links_total, gear_links_items, gear_links_activities = cur.fetchone()
            cur.execute("""
                WITH grouped AS (
                    SELECT route_id, MAX(sport) AS sport, COUNT(*) AS efforts,
                           ROUND(AVG(distance_km)::numeric,2) AS distance_km,
                           ROUND(AVG(ascent_m)::numeric,0) AS ascent_m,
                           MIN(start_time)::date AS first_ride,
                           MAX(start_time)::date AS last_ride
                    FROM clean_sessions WHERE route_id IS NOT NULL
                    GROUP BY route_id
                )
                SELECT COUNT(*) AS routes_total,
                    COUNT(*) FILTER (WHERE efforts>=2) AS matched_routes,
                    COALESCE(SUM(efforts) FILTER (WHERE efforts>=2),0) AS matched_efforts
                FROM grouped
            """)
            routes_total, matched_routes, matched_efforts = cur.fetchone()
            cur.execute("""
                WITH grouped AS (
                    SELECT route_id, MAX(sport) AS sport, COUNT(*) AS efforts,
                           ROUND(AVG(distance_km)::numeric,2) AS distance_km,
                           ROUND(AVG(ascent_m)::numeric,0) AS ascent_m,
                           MIN(start_time)::date AS first_ride,
                           MAX(start_time)::date AS last_ride
                    FROM clean_sessions WHERE route_id IS NOT NULL
                    GROUP BY route_id
                )
                SELECT route_id, sport, efforts, distance_km, ascent_m,
                       first_ride::text, last_ride::text
                FROM grouped WHERE efforts>=2
                ORDER BY efforts DESC, last_ride DESC LIMIT 10
            """)
            matched_sample = [
                {"route_id": r[0], "sport": r[1], "efforts": r[2],
                 "distance_km": float(r[3]) if r[3] is not None else None,
                 "ascent_m": int(r[4]) if r[4] is not None else None,
                 "first_ride": r[5], "last_ride": r[6]}
                for r in cur.fetchall()
            ]
            cur.execute("""
                SELECT COUNT(*) AS records,
                    COUNT(DISTINCT session_id) AS sessions_with_records,
                    COUNT(DISTINCT session_id) FILTER (WHERE lat IS NOT NULL AND lon IS NOT NULL) AS sessions_with_map_points,
                    COUNT(DISTINCT session_id) FILTER (WHERE altitude IS NOT NULL) AS sessions_with_altimetry,
                    COUNT(DISTINCT session_id) FILTER (WHERE power IS NOT NULL) AS sessions_with_power
                FROM session_records
            """)
            records, sessions_records, sessions_map, sessions_alt, sessions_power = cur.fetchone()
        return {
            "ok": True,
            "clean_sessions": {"total": total, "date_min": date_min, "date_max": date_max, "by_sport": by_sport},
            "calories": {
                "sessions_with_calories": with_cal,
                "coverage_pct": round((with_cal / total * 100), 1) if total else 0,
                "total_kcal": int(cal_total or 0),
            },
            "gear": {
                "garmin_items": gear_total,
                "active_or_unknown": gear_active,
                "retired": gear_retired,
                "activity_links_total": gear_links_total,
                "linked_gear_items": gear_links_items,
                "linked_activities": gear_links_activities,
                "sample": gear_sample,
            },
            "matched_rides": {
                "sessions_with_route_id": with_route,
                "routes_total": routes_total,
                "routes_with_2_or_more_efforts": matched_routes,
                "matched_efforts": matched_efforts,
                "sample": matched_sample,
            },
            "maps_and_charts": {
                "sessions_with_start_gps": start_gps,
                "sessions_with_end_gps": end_gps,
                "session_record_points": records,
                "sessions_with_time_series": sessions_records,
                "sessions_with_map_points": sessions_map,
                "sessions_with_altimetry_points": sessions_alt,
                "sessions_with_power_points": sessions_power,
            },
        }
    except Exception as e:
        logger.error(f"Phase 1 audit error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/backup ─────────────────────────────────────────────────────────────

@router.get("/admin/backup")
def admin_backup(token: str = ""):
    """Exporta todas las tablas en JSON. Requiere token de entorno ADMIN_TOKEN."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if token != admin_token:
        raise HTTPException(403, "Token inválido. Usa ?token=TU_TOKEN")
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    tables = ["sessions", "routes", "post_session", "gear", "maintenance",
              "recovery", "fuerza", "wellness", "accidents",
              "athlete_profile", "athlete_tests", "achievements", "athlete_snapshots",
              "garmin_export_activities", "garmin_export_gear", "garmin_export_sleep",
              "clean_sessions", "gear_activity_links", "zone_models",
              "session_environment", "capability_runs"]
    backup = {}
    try:
        with conn.cursor() as cur:
            for table in tables:
                try:
                    cur.execute(f"SELECT * FROM {table}")
                    rows = cur.fetchall()
                    cols = [d[0] for d in cur.description]
                    clean = []
                    for row in rows:
                        item = dict(zip(cols, row))
                        for k, v in item.items():
                            if hasattr(v, "isoformat"):
                                item[k] = v.isoformat()
                        clean.append(item)
                    backup[table] = clean
                except Exception as te:
                    backup[table] = {"error": str(te)}
        logger.info(f"Backup: {sum(len(v) if isinstance(v,list) else 0 for v in backup.values())} rows")
        return {"created_at": datetime.now(timezone.utc).isoformat(), "tables": backup}
    except Exception as e:
        logger.error(f"Backup error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/capability-validation/{nombre} ────────────────────────────────────

@router.post("/admin/capability-validation/{nombre}")
def admin_capability_validation(
    nombre: str,
    execute: bool = False,
    token: str = None,
):
    """Validate a capability and optionally record the official execution."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        from capability_engine import calculate_capabilities, validate_capability
        from psycopg2.extras import Json

        _ensure_capability_runs_table(conn)
        result = calculate_capabilities(conn)
        capability = _find_capability(result, nombre)
        previous = _previous_capability_run(conn, capability["key"])
        validation = validate_capability(capability, previous)
        recorded = False
        run_id, created_at = None, None
        if execute:
            if validation["blockers"]:
                raise HTTPException(
                    409,
                    "La validación tiene bloqueos y no puede registrarse como oficial.",
                )
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO capability_runs (
                        capability_key, capability_version, score, confidence,
                        maturity, validation_status, capability_json, validation_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, created_at
                """, (
                    capability["key"],
                    result["version"],
                    capability.get("score"),
                    capability.get("confidence"),
                    capability.get("maturity"),
                    validation["status"],
                    Json(capability),
                    Json(validation),
                ))
                run_id, created_at = cur.fetchone()
            conn.commit()
            recorded = True
        return {
            "ok": True,
            "recorded": recorded,
            "run_id": run_id,
            "created_at": created_at.isoformat() if created_at else None,
            "capability": capability,
            "validation": validation,
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"Admin capability validation error: {e}")
        raise HTTPException(500, str(e))


# ── /admin/import-mars-profile ────────────────────────────────────────────────

@router.post("/admin/import-mars-profile")
def import_mars_profile(token: str = None):
    """Importa el perfil Mars por defecto a la tabla athlete_profile_full."""
    _check_token(token)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from mars_context import MARS_PROFILE_DEFAULT, _ensure_profile_table
    _ensure_profile_table(conn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO athlete_profile_full (profile_key, data)
                VALUES ('mars', %s::jsonb)
                ON CONFLICT (profile_key) DO UPDATE
                    SET data = %s::jsonb, updated_at = NOW()
            """, (MARS_PROFILE_DEFAULT, MARS_PROFILE_DEFAULT))
        conn.commit()
        return {"ok": True, "msg": "Perfil Mars importado"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
