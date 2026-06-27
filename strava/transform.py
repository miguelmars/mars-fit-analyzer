"""
TD-014C — Transform strava_activities_raw → clean_sessions (modelo canónico Mars)

Flujo:
  1. Lee strava_activities_raw WHERE transform_status = 'pending'  (Supabase)
  2. Transforma al modelo canónico clean_sessions                   (Railway PostgreSQL)
  3. Actualiza transform_status = 'done' | 'error'                 (Supabase)

ADR-012: Strava es ingestión; Mars canonical es la fuente maestra.
          Garmin owns history through the active snapshot cutoff. Strava only
          creates clean sessions strictly after that cutoff.
ADR-011: Sin tocar goals ni prescripciones durante la transformación.

Notas de identidad:
  - Garmin:  clean_session_id = 'garmin:XXXXX'
  - Strava nuevo: clean_session_id = 'strava_XXXXX'
  - Strava histórico queda en staging con transform_status='skipped'.
"""
import json as _json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("bitacora.strava.transform")

# ── Mapeo sport_type Strava → sport canónico Mars ────────────────────────────
SPORT_MAP = {
    # Ciclismo
    "Ride":              "cycling",
    "VirtualRide":       "cycling",
    "EBikeRide":         "cycling",
    "MountainBikeRide":  "cycling",
    "GravelRide":        "cycling",
    "Velomobile":        "cycling",
    "Handcycle":         "cycling",
    # Running
    "Run":               "running",
    "TrailRun":          "running",
    "VirtualRun":        "running",
    # Caminata
    "Walk":              "walking",
    "Hike":              "hiking",
    # Nieve
    "BackcountrySkiing": "skiing",
    "AlpineSki":         "skiing",
    "NordicSki":         "skiing",
    # Agua
    "Swim":              "swimming",
    "Kayaking":          "paddle",
    "Canoeing":          "paddle",
    "Surfing":           "paddle",
    "Rowing":            "rowing",
    # Fuerza / movilidad
    "WeightTraining":    "strength",
    "Crossfit":          "strength",
    "Yoga":              "yoga",
    "Pilates":           "yoga",
    "Stretching":        "yoga",
    # Cardio indoor
    "Workout":           "workout",
    "Elliptical":        "cardio",
    "StairStepper":      "cardio",
    "StationaryBike":    "cycling",
    # Deportes de equipo
    "Soccer":            "sport",
    "Tennis":            "sport",
    "Basketball":        "sport",
    "Golf":              "sport",
}

BATCH_SIZE = 100  # actividades por lote de transformación


def _parse_utc(value) -> Optional[datetime]:
    """Parse an ISO timestamp and normalize it to timezone-aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _is_newer_than_garmin_cutoff(started_at, garmin_cutoff) -> bool:
    """True only when Strava starts strictly after the active Garmin snapshot."""
    start = _parse_utc(started_at)
    cutoff = _parse_utc(garmin_cutoff)
    if cutoff is None:
        return True
    if start is None:
        return False
    return start > cutoff


def _active_garmin_cutoff(conn):
    """Return the exact UTC cutoff owned by the active Garmin snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(start_time_utc)
            FROM garmin_export_activities
            WHERE is_active_snapshot IS TRUE
            """
        )
        row = cur.fetchone()
    return row[0] if row else None


# ── Función de transformación individual ─────────────────────────────────────

def _strava_to_clean_session(row: dict) -> dict:
    """
    Convierte un row de strava_activities_raw al esquema clean_sessions.
    Nunca lanza excepción (errores quedan como None en los campos).
    """
    strava_id = row.get("strava_id")
    sport_type_raw = row.get("sport_type") or "Unknown"
    sport_canonical = SPORT_MAP.get(sport_type_raw, "other")

    # ── Conversión de unidades ───────────────────────────
    distance_m   = float(row.get("distance_m")   or 0)
    avg_speed_ms = float(row.get("avg_speed_ms") or 0)
    max_speed_ms = float(row.get("max_speed_ms") or 0)
    elev_gain_m  = float(row.get("elevation_gain_m") or 0)

    avg_hr   = row.get("avg_hr")
    max_hr   = row.get("max_hr")
    avg_watts = row.get("avg_watts")
    max_watts = row.get("max_watts")

    # ── Timestamps ───────────────────────────────────────
    # Supabase devuelve ISO 8601; puede tener 'Z' o '+HH:MM'
    started_at       = row.get("started_at")
    started_at_local = row.get("started_at_local") or started_at

    start_date = None
    if started_at_local:
        try:
            dt = datetime.fromisoformat(
                str(started_at_local).replace("Z", "+00:00")
            )
            start_date = dt.date().isoformat()
        except Exception:
            pass

    # ── Velocidades kmh ──────────────────────────────────
    avg_speed_kmh = round(avg_speed_ms * 3.6, 2) if avg_speed_ms else None
    max_speed_kmh = round(max_speed_ms * 3.6, 2) if max_speed_ms else None

    # ── Efficiency (velocidad / FC) ──────────────────────
    efficiency = None
    if avg_hr and float(avg_hr) > 0 and avg_speed_kmh and avg_speed_kmh > 0:
        efficiency = round(avg_speed_kmh / float(avg_hr), 5)

    return {
        "clean_session_id":    f"strava_{strava_id}",
        "source":              "strava",
        "source_activity_id":  str(strava_id),
        "original_session_id": None,
        "name":                row.get("name"),
        "sport":               sport_canonical,
        "sport_type":          sport_type_raw,
        "start_time":          started_at,
        "start_date":          start_date,
        "duration_s":          int(row.get("moving_time_s")  or 0) or None,
        "moving_duration_s":   int(row.get("moving_time_s")  or 0) or None,
        "elapsed_duration_s":  int(row.get("elapsed_time_s") or 0) or None,
        "distance_km":         round(distance_m / 1000, 3) if distance_m else None,
        "ascent_m":            int(elev_gain_m) if elev_gain_m else None,
        "descent_m":           None,   # Strava summary no siempre da descent
        "avg_speed_kmh":       avg_speed_kmh,
        "max_speed_kmh":       max_speed_kmh,
        "avg_hr_bpm":          float(avg_hr)  if avg_hr  else None,
        "max_hr_bpm":          int(max_hr)    if max_hr  else None,
        "avg_cadence":         float(row.get("avg_cadence")) if row.get("avg_cadence") else None,
        "max_cadence":         None,
        "calories":            int(row.get("calories")) if row.get("calories") else None,
        "start_lat":           float(row.get("start_lat")) if row.get("start_lat") else None,
        "start_lon":           float(row.get("start_lng")) if row.get("start_lng") else None,
        "end_lat":             None,
        "end_lon":             None,
        "route_id":            None,
        "power_available":     bool(avg_watts),
        "avg_power_w":         int(float(avg_watts)) if avg_watts else None,
        "max_power_w":         int(float(max_watts)) if max_watts else None,
        "normalized_power_w":  None,
        "efficiency_speed_hr": efficiency,
        # Zones — NULL por ahora, el motor de zonas las calcula después
        "z2_pct_original":     None,
        "z2_pct_mars":         None,
        "zone_model_used":     None,
        "zone_confidence":     None,
        "z2_confidence_score": None,
        "quality":             "strava",
        "quality_notes":       f"confidence={row.get('source_confidence', 0.85):.2f}",
        # v7.5: preservar campos que antes se tiraban (consultables sin ir a staging)
        "raw_json":            (lambda _x: _json.dumps(_x) if _x else None)(
                                   {k: row.get(k) for k in
                                    ("max_watts", "device", "gear_id",
                                     "suffer_score", "elev_high_m", "elev_low_m")
                                    if row.get(k) is not None})
    }


# ── Transformación de un batch ───────────────────────────────────────────────

def _is_device_artifact(row: dict, cs: dict) -> bool:
    """True for fake 'rides' the speed sensor logs while washing or moving the bike.

    Signature: the recording device is a speed sensor (not the watch) AND there is
    no heart rate. Real workouts are recorded by the Forerunner with HR; a bare
    spinning wheel is not training. These must never enter clean_sessions.
    """
    device = (row.get("device") or "").lower()
    return "speed sensor" in device and not cs.get("avg_hr_bpm")


def transform_pending_batch(batch_size: int = BATCH_SIZE) -> dict:
    """
    Transforma hasta batch_size actividades pending → clean_sessions.
    Síncrono — para uso en endpoints o background tasks.

    Returns dict: {"transformed": n, "errors": n, "skipped": n, "message": str}
    """
    from .auth import get_supabase
    from db import get_db

    sb   = get_supabase()
    conn = get_db()

    if not conn:
        return {"error": "No DB connection (Railway PostgreSQL)", "transformed": 0, "errors": 0}

    results = {"transformed": 0, "errors": 0, "skipped": 0}
    garmin_cutoff = _active_garmin_cutoff(conn)

    # ── 1. Fetch pending de Supabase ─────────────────────
    resp = (
        sb.table("strava_activities_raw")
        .select(
            "id,strava_id,name,sport_type,"
            "started_at,started_at_local,"
            "distance_m,moving_time_s,elapsed_time_s,elevation_gain_m,"
            "avg_speed_ms,max_speed_ms,avg_hr,max_hr,"
            "avg_cadence,avg_watts,calories,"
            "start_lat,start_lng,source_confidence,"
            "max_watts,device,gear_id,suffer_score,elev_high_m,elev_low_m"
        )
        .eq("transform_status", "pending")
        .order("started_at", desc=False)
        .limit(batch_size)
        .execute()
    )

    activities = resp.data or []
    if not activities:
        logger.info("TD-014C: sin actividades pending")
        return {**results, "message": "No pending activities"}

    logger.info(f"TD-014C: transformando {len(activities)} actividades → clean_sessions")

    # ── 2. Transformar e insertar en Railway PostgreSQL ──
    for row in activities:
        strava_id = row["strava_id"]
        try:
            if not _is_newer_than_garmin_cutoff(row.get("started_at"), garmin_cutoff):
                reason = (
                    "covered_by_active_garmin_snapshot"
                    f"_through_{garmin_cutoff.isoformat() if hasattr(garmin_cutoff, 'isoformat') else garmin_cutoff}"
                )
                sb.table("strava_activities_raw").update({
                    "transform_status": "skipped",
                    "transformed_at": datetime.now(timezone.utc).isoformat(),
                    "transform_error": reason[:500],
                }).eq("strava_id", strava_id).execute()
                results["skipped"] += 1
                continue

            cs = _strava_to_clean_session(row)

            # Skip device artifacts: fake "rides" the speed sensor logs while the
            # bike is washed or moved (no HR, generic name). Not real workouts.
            if _is_device_artifact(row, cs):
                sb.table("strava_activities_raw").update({
                    "transform_status": "skipped",
                    "transformed_at": datetime.now(timezone.utc).isoformat(),
                    "transform_error": "device_artifact_speed_sensor_no_hr",
                }).eq("strava_id", strava_id).execute()
                results["skipped"] += 1
                continue

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO clean_sessions (
                        clean_session_id, source, source_activity_id,
                        original_session_id, name, sport, sport_type,
                        start_time, start_date, duration_s, moving_duration_s,
                        elapsed_duration_s, distance_km, ascent_m, descent_m,
                        avg_speed_kmh, max_speed_kmh, avg_hr_bpm, max_hr_bpm,
                        avg_cadence, max_cadence, calories,
                        start_lat, start_lon, end_lat, end_lon,
                        route_id, power_available, avg_power_w, max_power_w,
                        normalized_power_w, efficiency_speed_hr,
                        quality, quality_notes, raw_json
                    ) VALUES (
                        %(clean_session_id)s, %(source)s, %(source_activity_id)s,
                        %(original_session_id)s, %(name)s, %(sport)s, %(sport_type)s,
                        %(start_time)s, %(start_date)s, %(duration_s)s, %(moving_duration_s)s,
                        %(elapsed_duration_s)s, %(distance_km)s, %(ascent_m)s, %(descent_m)s,
                        %(avg_speed_kmh)s, %(max_speed_kmh)s, %(avg_hr_bpm)s, %(max_hr_bpm)s,
                        %(avg_cadence)s, %(max_cadence)s, %(calories)s,
                        %(start_lat)s, %(start_lon)s, %(end_lat)s, %(end_lon)s,
                        %(route_id)s, %(power_available)s, %(avg_power_w)s, %(max_power_w)s,
                        %(normalized_power_w)s, %(efficiency_speed_hr)s,
                        %(quality)s, %(quality_notes)s, %(raw_json)s
                    )
                    ON CONFLICT (clean_session_id) DO UPDATE SET
                        name              = EXCLUDED.name,
                        avg_hr_bpm        = EXCLUDED.avg_hr_bpm,
                        avg_speed_kmh     = EXCLUDED.avg_speed_kmh,
                        distance_km       = EXCLUDED.distance_km,
                        duration_s        = EXCLUDED.duration_s,
                        ascent_m          = EXCLUDED.ascent_m,
                        calories          = EXCLUDED.calories,
                        power_available   = EXCLUDED.power_available,
                        avg_power_w       = EXCLUDED.avg_power_w,
                        max_power_w       = EXCLUDED.max_power_w,
                        normalized_power_w = EXCLUDED.normalized_power_w,
                        quality_notes     = EXCLUDED.quality_notes
                """, cs)

            conn.commit()

            # ── 3. Marcar done en Supabase ────────────────
            sb.table("strava_activities_raw").update({
                "transform_status": "done",
                "transformed_at":   datetime.now(timezone.utc).isoformat(),
                "transform_error":  None,
            }).eq("strava_id", strava_id).execute()

            results["transformed"] += 1

        except Exception as e:
            logger.error(f"TD-014C error strava_id={strava_id}: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass
            # Marcar error en Supabase para no reintentar infinitamente
            try:
                sb.table("strava_activities_raw").update({
                    "transform_status": "error",
                    "transform_error":  str(e)[:500],
                }).eq("strava_id", strava_id).execute()
            except Exception:
                pass
            results["errors"] += 1

    logger.info(f"TD-014C batch completado: {results}")
    return results


# ── Transformación masiva de todas las pending ────────────────────────────────

async def transform_all_pending(batch_size: int = BATCH_SIZE) -> dict:
    """
    Transforma TODAS las actividades pending en batches sucesivos.
    Para background tasks — no bloquea el event loop entre batches.
    """
    import asyncio

    total = {"transformed": 0, "errors": 0, "skipped": 0, "batches": 0}

    while True:
        result = transform_pending_batch(batch_size)

        if "error" in result:
            return {**total, "error": result["error"]}

        batch_transformed = result.get("transformed", 0)
        batch_errors      = result.get("errors",      0)

        total["transformed"] += batch_transformed
        total["errors"]      += batch_errors
        total["skipped"]     += result.get("skipped", 0)
        total["batches"]     += 1

        # Sin actividades en el batch → terminamos
        if (
            batch_transformed == 0
            and batch_errors == 0
            and result.get("skipped", 0) == 0
        ):
            break

        logger.info(
            f"TD-014C batch {total['batches']} OK. "
            f"Acumulado: {total['transformed']} transformadas, "
            f"{total['errors']} errores"
        )

        # Pausa entre batches para no saturar Railway DB
        await asyncio.sleep(0.5)

    logger.info(f"TD-014C COMPLETO: {total}")
    return total


# ── Status helper ─────────────────────────────────────────────────────────────

def get_transform_status() -> dict:
    """
    Retorna conteo de actividades por transform_status en Supabase.
    Para el endpoint /api/strava/transform-status.
    """
    from .auth import get_supabase
    sb = get_supabase()

    counts = {}
    for status in ("pending", "done", "error", "skipped"):
        try:
            resp = (
                sb.table("strava_activities_raw")
                .select("id", count="exact")
                .eq("transform_status", status)
                .execute()
            )
            counts[status] = resp.count or 0
        except Exception:
            counts[status] = -1

    counts["total"] = sum(v for v in counts.values() if v >= 0)
    return counts
