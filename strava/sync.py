"""
Sincronización Strava → Supabase staging tables.

Flujo:
  Strava API → strava_activities_raw (staging)
             → strava_streams_raw   (staging, opcional)
             → strava_laps_raw      (staging, opcional)

  Después: transform_to_mars_canonical() pasará staging → clean_sessions (existente)

Las tablas strava_*_raw son staging — nunca tocan clean_sessions directamente.
Esto garantiza ADR-012: Mars es la fuente maestra, Strava es ingestión.

El backfill usa strava_sync_state como cursor reanudable.
Rate limits Strava: 200 req/15min · 2,000/día.
"""
import logging
import asyncio
from typing import Optional
from datetime import datetime, timezone

from .auth import get_supabase
from .client import get_activity, get_activity_streams, get_activity_laps
from .models import StravaWebhookEvent

logger = logging.getLogger("bitacora.strava.sync")

# Tablas de staging — nombres canónicos v4.8
_TABLE_ACTIVITIES = "strava_activities_raw"
_TABLE_STREAMS    = "strava_streams_raw"
_TABLE_LAPS       = "strava_laps_raw"
_TABLE_SYNC_STATE = "strava_sync_state"


async def process_webhook_event(event: StravaWebhookEvent) -> Optional[dict]:
    """
    Procesa un evento de webhook recibido de Strava.
    Solo procesa actividades (no atletas ni otros tipos).
    """
    if event.object_type != "activity":
        logger.debug(f"Ignorando evento: {event.object_type}/{event.aspect_type}")
        return None

    if event.aspect_type == "create":
        return await ingest_activity(event.object_id)
    elif event.aspect_type == "update":
        return await ingest_activity(event.object_id, is_update=True)
    elif event.aspect_type == "delete":
        return await delete_activity_staging(event.object_id)

    return None


async def ingest_activity(
    activity_id: int,
    is_update: bool = False,
    fetch_streams: bool = True,
    fetch_laps: bool = True,
) -> dict:
    """
    Ingesta una actividad de Strava a las tablas staging.
    No transforma ni toca clean_sessions — eso es responsabilidad de
    transform_to_mars_canonical() (TD-014C).
    """
    sb = get_supabase()

    # ── Duplicado check ──────────────────────────────────
    if not is_update:
        existing = (
            sb.table(_TABLE_ACTIVITIES)
            .select("id")
            .eq("strava_id", activity_id)
            .execute()
        )
        if existing.data:
            logger.info(f"Activity {activity_id} ya en staging, saltando")
            return {"status": "duplicate", "strava_id": activity_id}

    # ── 1. Fetch detalle de actividad ────────────────────
    activity = await get_activity(activity_id)
    athlete_id = activity.get("athlete", {}).get("id")

    # Detectar Compex antes de guardar
    if _is_compex_artifact(activity):
        logger.info(f"Ignoring Compex session: {activity.get('name')}")
        return {"status": "filtered_compex", "strava_id": activity_id}

    # ── 2. Construir fila para staging ───────────────────
    started_at = activity.get("start_date")
    started_at_local = activity.get("start_date_local")

    row = {
        "strava_id":        activity_id,
        "athlete_id":       athlete_id,
        "name":             activity.get("name"),
        "sport_type":       activity.get("sport_type") or activity.get("type", "Unknown"),
        "started_at":       started_at,
        "started_at_local": started_at_local,
        "timezone":         activity.get("timezone"),
        "distance_m":       activity.get("distance", 0),
        "moving_time_s":    activity.get("moving_time", 0),
        "elapsed_time_s":   activity.get("elapsed_time", 0),
        "elevation_gain_m": activity.get("total_elevation_gain", 0),
        "elev_high_m":      activity.get("elev_high"),
        "elev_low_m":       activity.get("elev_low"),
        "avg_speed_ms":     activity.get("average_speed", 0),
        "max_speed_ms":     activity.get("max_speed", 0),
        "avg_hr":           activity.get("average_heartrate"),
        "max_hr":           activity.get("max_heartrate"),
        "has_hr":           bool(activity.get("has_heartrate")),
        "avg_cadence":      activity.get("average_cadence"),
        "avg_watts":        activity.get("average_watts"),
        "max_watts":        activity.get("max_watts"),
        "calories":         activity.get("calories"),
        "device":           activity.get("device_name"),
        "gear_id":          activity.get("gear_id"),
        "start_lat":        (activity.get("start_latlng") or [None, None])[0],
        "start_lng":        (activity.get("start_latlng") or [None, None])[1],
        "suffer_score":     activity.get("suffer_score"),
        "raw_json":         activity,
        "source_confidence": 0.85,
        "transform_status": "pending",
        "synced_at":        datetime.now(timezone.utc).isoformat(),
    }

    result = (
        sb.table(_TABLE_ACTIVITIES)
        .upsert(row, on_conflict="strava_id")
        .execute()
    )
    row_id = result.data[0]["id"] if result.data else None

    logger.info(
        f"{'Actualizada' if is_update else 'Nueva'} actividad staging: "
        f"{activity.get('name')} | {row['sport_type']} | "
        f"{row['distance_m']/1000:.1f}km | HR:{row['avg_hr'] or 'N/A'}"
    )

    # ── 3. Streams ───────────────────────────────────────
    streams_ok = False
    if fetch_streams:
        try:
            streams = await get_activity_streams(activity_id)
            if streams:
                # Calcular coverages y stream_quality (B3)
                n_time = len(streams.get("time") or [])
                def _cov(key: str) -> float:
                    data = streams.get(key)
                    if not data or not n_time:
                        return 0.0
                    valid = sum(1 for v in data if v is not None)
                    return round(valid / n_time, 4)

                gps_cov  = _cov("latlng")
                hr_cov   = _cov("heartrate")
                cad_cov  = _cov("cadence")
                pow_cov  = _cov("watts")

                coverages = [c for c in [gps_cov, hr_cov, cad_cov, pow_cov] if c > 0]
                stream_quality = round(sum(coverages) / len(coverages), 4) if coverages else 0.0

                stream_row = {
                    "strava_activity_id": activity_id,
                    "stream_time":        streams.get("time"),
                    "stream_distance":    streams.get("distance"),
                    "stream_heartrate":   streams.get("heartrate"),
                    "stream_cadence":     streams.get("cadence"),
                    "stream_altitude":    streams.get("altitude"),
                    "stream_velocity":    streams.get("velocity_smooth"),
                    "stream_grade":       streams.get("grade_smooth"),
                    "stream_latlng":      streams.get("latlng"),
                    # v5.0: streams adicionales (potencia, temperatura, movimiento)
                    "stream_watts":       streams.get("watts"),
                    "stream_temp":        streams.get("temp"),
                    "stream_moving":      streams.get("moving"),
                    "source_confidence":  0.85,
                }
                sb.table(_TABLE_STREAMS).upsert(
                    stream_row, on_conflict="strava_activity_id"
                ).execute()

                # Actualizar quality flags en strava_activities_raw (B3)
                sb.table(_TABLE_ACTIVITIES).update({
                    "has_gps":          gps_cov > 0,
                    "has_cadence":      cad_cov > 0,
                    "has_power":        pow_cov > 0,
                    "has_temp":         bool(streams.get("temp")),
                    "has_moving":       bool(streams.get("moving")),
                    "gps_coverage":     gps_cov,
                    "hr_coverage":      hr_cov,
                    "cadence_coverage": cad_cov,
                    "power_coverage":   pow_cov,
                    "stream_quality":   stream_quality,
                }).eq("strava_id", activity_id).execute()

                streams_ok = True
        except Exception as e:
            logger.warning(f"Error jalando streams para {activity_id}: {e}")

    # ── 4. Laps ──────────────────────────────────────────
    if fetch_laps:
        try:
            laps_raw = await get_activity_laps(activity_id)
            if laps_raw:
                sb.table(_TABLE_LAPS).delete().eq(
                    "strava_activity_id", activity_id
                ).execute()
                laps_rows = [
                    {
                        "strava_activity_id": activity_id,
                        "lap_index":       i,
                        "name":            lap.get("name"),
                        "distance_m":      lap.get("distance"),
                        "moving_time_s":   lap.get("moving_time"),
                        "elapsed_time_s":  lap.get("elapsed_time"),
                        "avg_speed_ms":    lap.get("average_speed"),
                        "max_speed_ms":    lap.get("max_speed"),
                        "avg_hr":          lap.get("average_heartrate"),
                        "max_hr":          lap.get("max_heartrate"),
                        "avg_cadence":     lap.get("average_cadence"),
                        "avg_watts":       lap.get("average_watts"),
                    }
                    for i, lap in enumerate(laps_raw)
                ]
                if laps_rows:
                    sb.table(_TABLE_LAPS).insert(laps_rows).execute()
        except Exception as e:
            logger.warning(f"Error jalando laps para {activity_id}: {e}")

    return {
        "status":       "ingested",
        "strava_id":    activity_id,
        "staging_id":   row_id,
        "sport_type":   row["sport_type"],
        "name":         row["name"],
        "distance_km":  round(row["distance_m"] / 1000, 2),
        "has_streams":  streams_ok,
        "next_step":    "transform_to_mars_canonical() — pendiente TD-014C",
    }


async def ingest_activity_from_summary(
    summary,  # StravaActivitySummary
    athlete_id: Optional[int] = None,
) -> dict:
    """
    Ingesta una actividad a staging usando el summary ya descargado.
    NO hace request adicional a Strava — usa los datos del listing.
    Esto es clave para el backfill masivo: 1 request/actividad (el listing),
    no 2,900 requests individuales.
    """
    sb = get_supabase()

    strava_id = summary.id if hasattr(summary, 'id') else summary.get('id')

    # Duplicado check
    existing = (
        sb.table(_TABLE_ACTIVITIES)
        .select("id")
        .eq("strava_id", strava_id)
        .execute()
    )
    if existing.data:
        return {"status": "duplicate", "strava_id": strava_id}

    # Detectar Compex por nombre
    name = summary.name if hasattr(summary, 'name') else summary.get('name', '')
    if any(s in (name or '').lower() for s in ["compex", "electrostim", "ems", "electroestim"]):
        return {"status": "filtered_compex", "strava_id": strava_id}

    # Construir fila desde el summary (sin request adicional)
    if hasattr(summary, 'id'):
        # Es un objeto StravaActivitySummary
        started_at    = summary.start_date.isoformat()
        started_local = summary.start_date_local.isoformat()
        sport_type    = summary.sport_type or summary.type
        start_lat     = summary.start_latlng[0] if summary.start_latlng else None
        start_lng     = summary.start_latlng[1] if summary.start_latlng else None
        row = {
            "strava_id":         strava_id,
            "athlete_id":        athlete_id,
            "name":              summary.name,
            "sport_type":        sport_type,
            "started_at":        started_at,
            "started_at_local":  started_local,
            "timezone":          summary.timezone,
            "distance_m":        summary.distance,
            "moving_time_s":     summary.moving_time,
            "elapsed_time_s":    summary.elapsed_time,
            "elevation_gain_m":  summary.total_elevation_gain,
            "elev_high_m":       summary.elev_high,
            "elev_low_m":        summary.elev_low,
            "avg_speed_ms":      summary.average_speed,
            "max_speed_ms":      summary.max_speed,
            "avg_hr":            summary.average_heartrate,
            "max_hr":            summary.max_heartrate,
            "has_hr":            summary.has_heartrate,
            "avg_cadence":       summary.average_cadence,
            "avg_watts":         summary.average_watts,
            "max_watts":         summary.max_watts,
            "calories":          summary.calories,
            "device":            summary.device_name,
            "gear_id":           summary.gear_id,
            "start_lat":         start_lat,
            "start_lng":         start_lng,
            "suffer_score":      summary.suffer_score,
            "raw_json":          None,  # no hay raw_json en el summary listing
            "source_confidence": 0.82,  # listing summary = un poco menos que detalle
            "transform_status":  "pending",
            "synced_at":         datetime.now(timezone.utc).isoformat(),
        }
    else:
        # Es un dict (fallback)
        row = summary
        row["transform_status"] = "pending"

    sb.table(_TABLE_ACTIVITIES).upsert(row, on_conflict="strava_id").execute()

    logger.info(
        f"Staging (summary): {name} | {row.get('sport_type')} | "
        f"{row.get('distance_m', 0)/1000:.1f}km | HR:{row.get('avg_hr') or 'N/A'}"
    )

    return {"status": "ingested", "strava_id": strava_id, "name": name}


async def delete_activity_staging(activity_id: int) -> dict:
    """Elimina una actividad del staging cuando se borra en Strava."""
    sb = get_supabase()
    result = (
        sb.table(_TABLE_ACTIVITIES)
        .delete()
        .eq("strava_id", activity_id)
        .execute()
    )
    deleted = len(result.data) if result.data else 0
    logger.info(f"Eliminado del staging: activity {activity_id} ({deleted} rows)")
    return {"status": "deleted", "strava_id": activity_id}


def _is_compex_artifact(activity: dict) -> bool:
    """
    Detecta sesiones de electroestimulación Compex.
    Generan spikes de FC artificiales — no son entrenamiento real.
    """
    name = (activity.get("name") or "").lower()
    if any(s in name for s in ["compex", "electrostim", "ems", "electroestim"]):
        return True
    # Sesión corta sin movimiento con HR alto = probable Compex
    if (
        activity.get("distance", 0) < 50
        and activity.get("moving_time", 0) < 300
        and activity.get("has_heartrate")
        and (activity.get("max_heartrate") or 0) > 150
    ):
        return True
    return False


# ── Backfill reanudable con cursor ───────────────────────

async def backfill_from_strava(
    after_timestamp: Optional[int] = None,
    with_streams: bool = False,
) -> dict:
    """
    Sincronización masiva de actividades históricas a staging.

    Rate limits Strava: 200 req/15min · 2,000/día.
    Con 2,900 actividades mínimo 2 días a tasa máxima.

    El cursor strava_sync_state permite reanudar si se interrumpe.
    Si with_streams=False (default), solo jala actividad summary (seguro).
    Los streams se pueden jalar después en batches separados.
    """
    from .client import backfill_all_activities
    sb = get_supabase()

    # ── Leer cursor actual ───────────────────────────────
    state_row = sb.table(_TABLE_SYNC_STATE).select("*").eq("id", 1).execute()
    state = state_row.data[0] if state_row.data else {}

    if state.get("status") == "running":
        logger.warning("Backfill ya en progreso. Abortando para evitar duplicados.")
        return {"status": "already_running"}

    # ── Marcar como running ──────────────────────────────
    sb.table(_TABLE_SYNC_STATE).update({
        "status": "running",
        "with_streams": with_streams,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", 1).execute()

    # Continuar desde el último cursor si existe
    last_id = state.get("last_activity_id")
    if last_id and not after_timestamp:
        logger.info(f"Reanudando backfill desde last_activity_id={last_id}")

    results = {"ingested": 0, "duplicates": 0, "filtered": 0, "errors": 0}
    last_synced_id = last_id

    try:
        activities = await backfill_all_activities(after=after_timestamp)

        # Detectar athlete_id del primer elemento
        bulk_athlete_id = None
        if activities:
            first = activities[0]
            # StravaActivitySummary no tiene athlete_id en el listing — usar el del token
            from .auth import get_valid_token
            from .config import STRAVA_ALLOWED_ATHLETE_ID
            bulk_athlete_id = STRAVA_ALLOWED_ATHLETE_ID

        for activity in activities:
            activity_id = activity.id if hasattr(activity, 'id') else activity.get("id")
            try:
                # Usar summary directamente — NO llamar get_activity() (ahorra 2,900 requests)
                result = await ingest_activity_from_summary(
                    activity,
                    athlete_id=bulk_athlete_id,
                )
                status = result.get("status", "error")
                if status == "ingested":
                    results["ingested"] += 1
                elif status == "duplicate":
                    results["duplicates"] += 1
                elif status.startswith("filtered"):
                    results["filtered"] += 1

                last_synced_id = activity_id

                # Actualizar cursor cada 100 actividades
                if (results["ingested"] + results["duplicates"]) % 100 == 0:
                    sb.table(_TABLE_SYNC_STATE).update({
                        "last_activity_id": last_synced_id,
                        "total_synced": results["ingested"],
                        "last_synced_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", 1).execute()
                    logger.info(f"Backfill progreso: {results}")

                # Pausa mínima — no necesita rate limit porque no hacemos requests individuales
                await asyncio.sleep(0.05)

            except Exception as e:
                logger.error(f"Error ingesting activity {activity_id}: {e}")
                results["errors"] += 1

        # ── Marcar completado ────────────────────────────
        sb.table(_TABLE_SYNC_STATE).update({
            "status": "completed",
            "last_activity_id": last_synced_id,
            "total_synced": results["ingested"],
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()

    except Exception as e:
        sb.table(_TABLE_SYNC_STATE).update({
            "status": "error",
            "error_message": str(e),
            "last_activity_id": last_synced_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()
        raise

    logger.info(f"Backfill staging completado: {results}")
    return results
