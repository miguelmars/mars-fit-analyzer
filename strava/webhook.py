"""
FastAPI Router — Endpoints de Strava para Epoch (Mars Edition).

Endpoints:
  GET  /api/strava/authorize     → Redirige a Strava para autorizar (setup inicial)
  GET  /api/strava/callback      → Recibe authorization code y lo intercambia por tokens
  GET  /api/strava/webhook       → Validación de webhook (Strava verifica tu endpoint)
  POST /api/strava/webhook       → Recibe eventos push de Strava
  POST /api/strava/backfill      → Trigger manual de sincronización histórica
  GET  /api/strava/status             → Health check del módulo
  GET  /api/strava/stream-completeness → Reporte de cobertura hr/gps/cadence/power
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import STRAVA_VERIFY_TOKEN, STRAVA_CALLBACK_URL, STRAVA_ALLOWED_ATHLETE_ID
from .auth import get_authorization_url, exchange_code, get_valid_token
from .models import StravaWebhookEvent
from .sync import process_webhook_event, backfill_from_strava, ingest_activity_from_summary

logger = logging.getLogger("bitacora.strava.webhook")

router = APIRouter(prefix="/api/strava", tags=["strava"])


# ── OAuth: Autorización inicial (una sola vez) ──────────

@router.get("/authorize")
async def authorize():
    """
    Redirige al usuario a Strava para autorizar la app.
    Solo necesitas hacer esto UNA VEZ.
    """
    url = get_authorization_url(redirect_uri=STRAVA_CALLBACK_URL.replace("/webhook", "/callback"))
    return RedirectResponse(url)


@router.get("/callback")
async def oauth_callback(code: str = Query(...), scope: str = Query("")):
    """
    Recibe el authorization code de Strava después de autorizar.
    Lo intercambia por access_token + refresh_token.
    """
    try:
        tokens = await exchange_code(code)
        return JSONResponse({
            "status": "ok",
            "message": "Autorización exitosa. Tokens guardados.",
            "athlete_id": tokens.athlete_id,
            "scopes": scope,
            "expires_at": datetime.fromtimestamp(tokens.expires_at).isoformat(),
        })
    except Exception as e:
        logger.error(f"Error en OAuth callback: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=400
        )


# ── Webhook: Validación GET ──────────────────────────────

@router.get("/webhook")
async def webhook_validation(
    request: Request,
):
    """
    Strava envía un GET para validar tu callback URL.
    Debe responder en < 2 segundos con el hub.challenge.
    """
    params = request.query_params

    mode = params.get("hub.mode")
    challenge = params.get("hub.challenge")
    verify_token = params.get("hub.verify_token")

    if mode == "subscribe" and verify_token == STRAVA_VERIFY_TOKEN:
        logger.info(f"Webhook validado. Challenge: {challenge}")
        return JSONResponse({"hub.challenge": challenge})

    logger.warning(f"Webhook validation fallida. Token: {verify_token}")
    return JSONResponse({"error": "Forbidden"}, status_code=403)


# ── Webhook: Eventos POST ────────────────────────────────

@router.post("/webhook")
async def webhook_receive(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Recibe eventos push de Strava.
    DEBE responder 200 en < 2 segundos.
    El procesamiento real va en background task.
    """
    try:
        body = await request.json()
        event = StravaWebhookEvent(**body)
    except Exception as e:
        logger.error(f"Error parseando webhook: {e}")
        # Aún así responder 200 para que Strava no reintente
        return JSONResponse({"status": "parse_error"}, status_code=200)

    # Validar owner_id — ignorar webhooks de otros atletas (seguridad single-player)
    if STRAVA_ALLOWED_ATHLETE_ID and event.owner_id != STRAVA_ALLOWED_ATHLETE_ID:
        logger.warning(
            f"Webhook ignorado: owner_id {event.owner_id} != "
            f"atleta autorizado {STRAVA_ALLOWED_ATHLETE_ID}"
        )
        return JSONResponse({"status": "ignored"}, status_code=200)

    logger.info(
        f"Webhook: {event.object_type}/{event.aspect_type} "
        f"id={event.object_id} owner={event.owner_id}"
    )

    # Procesar en background — NO bloquear el response
    background_tasks.add_task(_process_event_safe, event)

    # Responder 200 inmediatamente
    return JSONResponse({"status": "ok"})


async def _process_event_safe(event: StravaWebhookEvent):
    """Wrapper seguro para procesamiento en background."""
    try:
        result = await process_webhook_event(event)
        if result:
            logger.info(f"Procesado: {result}")
    except Exception as e:
        logger.error(
            f"Error procesando webhook event "
            f"(type={event.object_type}, id={event.object_id}): {e}",
            exc_info=True
        )


# ── Backfill manual ──────────────────────────────────────

@router.post("/backfill")
async def trigger_backfill(
    background_tasks: BackgroundTasks,
    after: int = Query(
        default=None,
        description="Unix timestamp. Solo actividades después de esta fecha."
    ),
    with_streams: bool = Query(
        default=False,
        description="Jalar streams por actividad. Lento, cuidado con rate limits."
    ),
    force: bool = Query(
        default=False,
        description="Ignorar estado 'running' — útil si Railway mató el proceso anterior."
    ),
):
    """
    Trigger manual de backfill.
    Para la carga inicial de tus ~2,900 sesiones históricas.
    Usa force=true si el cursor quedó bloqueado en 'running'.
    """
    if force:
        # Resetear cursor para poder relanzar
        try:
            from .auth import get_supabase
            sb = get_supabase()
            sb.table("strava_sync_state").update({
                "status": "idle",
                "error_message": None,
            }).eq("id", 1).execute()
            logger.info("Cursor reseteado a idle (force=true)")
        except Exception as e:
            logger.warning(f"No se pudo resetear cursor: {e}")

    background_tasks.add_task(backfill_from_strava, after, with_streams)
    return JSONResponse({
        "status": "backfill_started",
        "after": after,
        "with_streams": with_streams,
        "force": force,
        "message": "Backfill corriendo en background. Revisa logs para progreso."
    })


# ── Backfill de streams (síncrono, por lotes) ────────────

@router.post("/backfill-streams")
async def backfill_streams(
    batch: int = Query(
        default=50,
        description="Actividades con streams por llamada. Max recomendado: 50 (2 req/actividad)."
    ),
    sport: Optional[str] = Query(
        default=None,
        description="Filtrar por deporte, ej. 'Ride'. None = todos."
    ),
):
    """
    Descarga streams (GPS, HR, cadencia, watts, temp) para actividades
    que ya están en strava_activities_raw pero sin streams.

    Síncrono — llama múltiples veces hasta que streams_pending = 0.
    Cada llamada consume ~2 requests Strava por actividad.
    Con batch=50: 100 requests por llamada → seguro dentro del límite de 200/15min.

    Rate limits Strava: 200 req/15min · 2,000/día.
    Con 3,089 actividades → ~62 llamadas necesarias → ~3 días a ritmo normal.
    """
    from .auth import get_supabase
    from .sync import ingest_activity

    sb = get_supabase()

    # Fuente de verdad: strava_streams_raw.
    # IMPORTANTE: Supabase limita a 1,000 filas por query — paginar para obtener todos los IDs.
    streamed_ids: set = set()
    s_page = 0
    while True:
        s_chunk = (
            sb.table("strava_streams_raw")
            .select("strava_activity_id")
            .range(s_page * 1000, (s_page + 1) * 1000 - 1)
            .execute()
        )
        chunk_data = s_chunk.data or []
        streamed_ids.update(r["strava_activity_id"] for r in chunk_data)
        if len(chunk_data) < 1000:
            break
        s_page += 1

    # Paginar strava_activities_raw para encontrar el siguiente lote sin streams
    rows = []
    page = 0
    page_size = 200
    while len(rows) < batch:
        all_q = sb.table("strava_activities_raw").select("strava_id, name, sport_type")
        if sport:
            all_q = all_q.ilike("sport_type", f"%{sport}%")
        page_rows = all_q.range(page * page_size, (page + 1) * page_size - 1).execute().data or []
        if not page_rows:
            break
        for r in page_rows:
            if r["strava_id"] not in streamed_ids:
                rows.append(r)
                if len(rows) >= batch:
                    break
        page += 1
        if len(page_rows) < page_size:
            break

    if not rows:
        total_act = sb.table("strava_activities_raw").select("id", count="exact").execute()
        total = total_act.count or 0
        return JSONResponse({
            "status":                    "completed",
            "message":                   "Todas las actividades tienen streams.",
            "total_activities":          total,
            "streams_in_db":             len(streamed_ids),
            "remaining_without_streams": max(0, total - len(streamed_ids)),
        })

    from .client import get_activity_streams
    from datetime import timezone

    results = {"ok": 0, "empty": 0, "error": 0, "processed": len(rows)}
    for row in rows:
        activity_id = row["strava_id"]
        try:
            streams = await get_activity_streams(activity_id)

            # Calcular coverages
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

            if streams:
                # Guardar en strava_streams_raw
                stream_row = {
                    "strava_activity_id": activity_id,
                    "stream_time":     streams.get("time"),
                    "stream_distance": streams.get("distance"),
                    "stream_heartrate":streams.get("heartrate"),
                    "stream_cadence":  streams.get("cadence"),
                    "stream_altitude": streams.get("altitude"),
                    "stream_velocity": streams.get("velocity_smooth"),
                    "stream_grade":    streams.get("grade_smooth"),
                    "stream_latlng":   streams.get("latlng"),
                    "stream_watts":    streams.get("watts"),
                    "stream_temp":     streams.get("temp"),
                    "stream_moving":   streams.get("moving"),
                    "source_confidence": 0.85,
                }
                sb.table("strava_streams_raw").upsert(
                    stream_row, on_conflict="strava_activity_id"
                ).execute()
                results["ok"] += 1
            else:
                results["empty"] += 1

            # Actualizar quality flags en strava_activities_raw siempre
            sb.table("strava_activities_raw").update({
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

        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Too Many Requests" in err_str:
                # Rate limit de Strava — abortar lote completo, NO marcar actividades como error
                # Se reintentarán en el próximo ciclo cuando el rate limit resetee
                logger.warning(f"Rate limit Strava (429) en activity {activity_id} — abortando lote")
                results["error"] += 1
                break
            logger.warning(f"Stream error activity {activity_id}: {e}")
            results["error"] += 1

    # Contar cuántas quedan sin streams (fuente de verdad: strava_streams_raw)
    streamed_after = sb.table("strava_streams_raw").select("id", count="exact").execute()
    total_act = sb.table("strava_activities_raw").select("id", count="exact").execute()
    remaining_count = (total_act.count or 0) - (streamed_after.count or 0)

    return JSONResponse({
        "status":    "ok",
        "processed": results["processed"],
        "with_streams": results["ok"],
        "no_gps":    results["empty"],
        "errors":    results["error"],
        "remaining_without_streams": max(0, remaining_count),
        "total_with_streams": streamed_after.count or 0,
        "message": (
            f"Lote de {results['processed']} actividades procesado. "
            f"Llama de nuevo hasta que remaining_without_streams = 0."
        ),
    })


# ── Registro de webhook en Strava (una sola vez) ─────────

@router.get("/register-webhook")
async def register_webhook():
    """
    Registra el webhook en Strava. Solo se necesita hacer UNA VEZ.
    Strava verificará tu callback URL automáticamente.
    """
    from .config import (
        STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
        STRAVA_WEBHOOK_URL, STRAVA_VERIFY_TOKEN,
    )

    if not STRAVA_CLIENT_SECRET:
        return JSONResponse(
            {"status": "error", "message": "STRAVA_CLIENT_SECRET no configurado"},
            status_code=400,
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://www.strava.com/api/v3/push_subscriptions",
            data={
                "client_id":    STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "callback_url": STRAVA_WEBHOOK_URL,
                "verify_token": STRAVA_VERIFY_TOKEN,
            },
        )

    data = resp.json()

    if resp.status_code == 201:
        logger.info(f"Webhook registrado: subscription_id={data.get('id')}")
        return JSONResponse({
            "status":          "ok",
            "message":         "Webhook registrado exitosamente en Strava 🎉",
            "subscription_id": data.get("id"),
            "callback_url":    STRAVA_WEBHOOK_URL,
        })

    # 422 = ya existe una suscripción activa
    if resp.status_code == 422:
        return JSONResponse({
            "status":  "already_registered",
            "message": "Ya existe un webhook activo para esta app.",
            "details": data,
        })

    return JSONResponse({
        "status":          "error",
        "strava_response": data,
        "status_code":     resp.status_code,
    }, status_code=400)


# ── Sync síncrono (sin background task — más confiable en Railway) ───────────

@router.post("/sync-now")
async def sync_now(
    pages: int = Query(
        default=5,
        description="Cuántas páginas de 200 actividades jalar (5 = 1,000 actividades)."
    ),
    force: bool = Query(
        default=True,
        description="Resetear cursor si estaba bloqueado."
    ),
):
    """
    Sincronización síncrona de actividades Strava → staging.
    A diferencia del backfill (background), este endpoint espera a terminar
    y retorna el resultado. Ideal para Railway donde los background tasks mueren.

    Llama múltiples veces para procesar todo el historial.
    pages=5 → ~1,000 actividades por llamada.
    """
    from .client import get_recent_activities
    from .auth import get_supabase

    sb = get_supabase()

    # Resetear cursor bloqueado
    if force:
        try:
            sb.table("strava_sync_state").update({
                "status": "idle",
                "error_message": None,
            }).eq("id", 1).execute()
        except Exception:
            pass

    # Leer cursor para saber desde dónde continuar
    try:
        state_row = sb.table("strava_sync_state").select("*").eq("id", 1).execute()
        state = state_row.data[0] if state_row.data else {}
    except Exception:
        state = {}

    # Marcar running
    try:
        sb.table("strava_sync_state").update({
            "status": "running",
        }).eq("id", 1).execute()
    except Exception:
        pass

    results = {"ingested": 0, "duplicates": 0, "filtered": 0, "errors": 0, "pages": 0}
    last_id = None

    try:
        for page in range(1, pages + 1):
            batch = await get_recent_activities(per_page=200, page=page)
            if not batch:
                break
            results["pages"] += 1

            for summary in batch:
                try:
                    result = await ingest_activity_from_summary(
                        summary,
                        athlete_id=STRAVA_ALLOWED_ATHLETE_ID,
                    )
                    status = result.get("status", "error")
                    if status == "ingested":
                        results["ingested"] += 1
                        last_id = summary.id
                    elif status == "duplicate":
                        results["duplicates"] += 1
                    elif status.startswith("filtered"):
                        results["filtered"] += 1
                except Exception as e:
                    logger.error(f"Error ingesting {summary.id}: {e}")
                    results["errors"] += 1

        sb.table("strava_sync_state").update({
            "status": "idle",
            "last_activity_id": last_id,
            "total_synced": results["ingested"],
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()

    except Exception as e:
        err_str = str(e)
        # Detectar rate limit de Strava
        if "RATE_LIMIT" in err_str or "429" in err_str or "Too Many Requests" in err_str:
            try:
                sb.table("strava_sync_state").update({"status": "idle"}).eq("id", 1).execute()
            except Exception:
                pass
            return JSONResponse({
                "status": "rate_limited",
                "message": "Strava rate limit (200 req/15min). Espera 15 minutos y reintenta.",
                "retry_after_seconds": 900,
            }, status_code=429)
        try:
            sb.table("strava_sync_state").update({
                "status": "error",
                "error_message": err_str[:300],
            }).eq("id", 1).execute()
        except Exception:
            pass
        return JSONResponse({"status": "error", "message": err_str}, status_code=500)

    return JSONResponse({
        "status": "ok",
        **results,
        "message": f"Listo. {results['ingested']} nuevas, {results['duplicates']} ya existían.",
    })


# ── TD-014C: Transform staging → clean_sessions ──────────

@router.post("/transform")
async def trigger_transform(
    batches: int = Query(
        default=5,
        description="Lotes de 100 actividades por llamada (5 = 500 actividades)."
    ),
):
    """
    Transforma strava_activities_raw (pending) → clean_sessions (modelo Mars).
    TD-014C — Síncrono para evitar que Railway mate el proceso.

    Llama múltiples veces hasta que pending=0.
    Seguro para correr múltiples veces — ON CONFLICT DO UPDATE.
    """
    from .transform import transform_pending_batch

    total = {"transformed": 0, "errors": 0, "batches_run": 0}

    for _ in range(batches):
        result = transform_pending_batch(100)
        if "error" in result:
            return JSONResponse({"status": "error", "message": result["error"]}, status_code=500)

        total["transformed"] += result.get("transformed", 0)
        total["errors"]      += result.get("errors", 0)
        total["batches_run"] += 1

        # Sin actividades en el batch → terminamos antes
        if result.get("transformed", 0) == 0 and result.get("errors", 0) == 0:
            break

    return JSONResponse({
        "status":    "ok",
        **total,
        "message":   f"{total['transformed']} sesiones → clean_sessions. "
                     f"{total['errors']} errores.",
    })


@router.get("/transform-status")
async def get_transform_status_endpoint():
    """
    Estado actual de la transformación (pending / done / error).
    Útil para monitorear el progreso del backfill.
    """
    from .transform import get_transform_status
    counts = get_transform_status()
    pct_done = 0
    if counts.get("total", 0) > 0:
        pct_done = round(counts.get("done", 0) / counts["total"] * 100, 1)
    return JSONResponse({
        **counts,
        "pct_done": pct_done,
    })


# ── A1: Backfill status detallado ────────────────────────

@router.get("/backfill-status")
async def backfill_status():
    """
    Estado detallado del backfill — visible en /admin o /progreso.
    Fase A1: sustituye tener que revisar logs de Railway.
    """
    from .auth import get_supabase
    from math import ceil

    sb = get_supabase()

    # Leer cursor de sync
    try:
        state_row = sb.table("strava_sync_state").select("*").eq("id", 1).execute()
        state = state_row.data[0] if state_row.data else {}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # Contar actividades por estado de transformación
    try:
        total_res    = sb.table("strava_activities_raw").select("id", count="exact").execute()
        pending_res  = sb.table("strava_activities_raw").select("id", count="exact").eq("transform_status", "pending").execute()
        done_res     = sb.table("strava_activities_raw").select("id", count="exact").eq("transform_status", "done").execute()
        error_res    = sb.table("strava_activities_raw").select("id", count="exact").eq("transform_status", "error").execute()
        streams_res  = sb.table("strava_streams_raw").select("id", count="exact").execute()

        total_activities       = total_res.count or 0
        activities_pending     = pending_res.count or 0
        activities_done        = done_res.count or 0
        activities_error       = error_res.count or 0
        streams_downloaded     = streams_res.count or 0
    except Exception:
        total_activities = activities_pending = activities_done = 0
        activities_error = streams_downloaded = 0

    # ETA: basado en streams pendientes (lo que está corriendo ahora)
    # Rate real: ~2 lotes de 50 cada 20min = ~100 streams/20min = ~7,200/día
    # Con rate limit Strava (2,000/día conservador): 7,200 pero limitado a ~2,000
    remaining_streams = max(0, total_activities - streams_downloaded)
    rate_streams_day  = 2000  # conservador (Strava 2,000 req/day)
    eta_days     = ceil(remaining_streams / rate_streams_day) if remaining_streams > 0 else 0
    eta_ts       = None
    if eta_days > 0:
        from datetime import timedelta
        eta_ts = (datetime.now(timezone.utc) + timedelta(days=eta_days)).isoformat()

    # Leer rate limit restante del último header guardado (best-effort)
    rate_limit_remaining = state.get("rate_limit_remaining")

    return JSONResponse({
        "status":                state.get("status", "unknown"),
        "semaforo": (
            "🟢" if state.get("status") == "completed" else
            "🟡" if state.get("status") == "running" else
            "🔴"
        ),
        "total_activities":      total_activities,
        "activities_downloaded": total_activities,
        "activities_pending":    activities_pending,
        "activities_done":       activities_done,
        "activities_error":      activities_error,
        "streams_downloaded":    streams_downloaded,
        "remaining_streams":     remaining_streams,
        "last_activity_id":      state.get("last_activity_id"),
        "last_processed_at":     state.get("last_synced_at"),
        "rate_limit_remaining":  rate_limit_remaining,
        "estimated_completion":  eta_ts,
        "eta_days":              eta_days,
        "error_message":         state.get("error_message"),
        "pct_streams": round(streams_downloaded / total_activities * 100, 1) if total_activities else 0,
    })


# ── C1: Dedup diagnosis ───────────────────────────────────

@router.get("/dedup-diagnosis")
async def dedup_diagnosis(
    sport_type: Optional[str] = Query(
        default=None,
        description="Filtrar por tipo de deporte, ej. 'Ride', 'Run'."
    ),
):
    """
    Compara strava_activities_raw vs clean_sessions y categoriza cada actividad.

    Categorías:
      exact_match    → fecha ±5 min, dist ±2%, dur ±3% → solo enlazar strava_id
      probable_match → fecha ±30 min, dist ±5%, dur ±10% → requiere revisión
      conflict       → fecha cerca pero métricas muy distintas → bloquear
      new_session    → sin equivalente en Garmin → crear nueva sesión

    Fase C1. Prerequisito para /dedup-samples y /approve-samples.
    """
    import asyncio
    from .dedup import run_dedup_diagnosis

    # Correr en executor para no bloquear el event loop (es I/O sync intensivo)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, run_dedup_diagnosis, sport_type)
    except Exception as e:
        logger.error(f"Error en dedup diagnosis: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    return JSONResponse(result)


# ── C2: Dedup samples ─────────────────────────────────────

@router.get("/dedup-samples")
async def dedup_samples(
    diagnosis_id: str = Query(..., description="UUID del diagnóstico de /dedup-diagnosis"),
):
    """
    Devuelve 10 ejemplos de cada categoría para revisión humana antes de transformar.
    El atleta debe revisar estos ejemplos y confirmar que la clasificación es correcta.
    Fase C2.
    """
    from .dedup import get_dedup_samples

    try:
        result = get_dedup_samples(diagnosis_id)
    except Exception as e:
        logger.error(f"Error en dedup samples: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    if result.get("error"):
        return JSONResponse(result, status_code=404)

    return JSONResponse(result)


# ── C3: Approve samples ───────────────────────────────────

@router.post("/approve-samples")
async def approve_samples(request: Request):
    """
    Registra la aprobación humana de las muestras de dedup.
    Habilita el botón de transformar Strava → Mars.

    Body JSON:
      { "diagnosis_id": "XXXXXXXX", "approved_by": "mars", "notes": "todo ok" }

    Fase C3. Sin esta aprobación, POST /transform está bloqueado.
    """
    from .dedup import record_approval

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON requerido"}, status_code=400)

    diagnosis_id = body.get("diagnosis_id")
    approved_by  = body.get("approved_by", "mars")
    notes        = body.get("notes", "")

    if not diagnosis_id:
        return JSONResponse({"error": "diagnosis_id requerido"}, status_code=400)

    try:
        result = record_approval(diagnosis_id, approved_by, notes)
    except Exception as e:
        logger.error(f"Error registrando aprobación: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    if not result.get("ok"):
        return JSONResponse(result, status_code=400)

    return JSONResponse(result)


# ── Stream completeness report ───────────────────────────

@router.get("/stream-completeness")
async def stream_completeness():
    """
    Reporte de cobertura de streams por actividad.
    Muestra hr_coverage · gps_coverage · cadence_coverage · power_coverage
    con distribución por rangos y desglose por deporte.
    """
    from .auth import get_supabase
    sb = get_supabase()

    # Total actividades
    total_r = sb.table("strava_activities_raw").select("strava_id", count="exact").execute()
    total = total_r.count or 0

    # Actividades con streams — strava_streams_raw tiene 1 fila por actividad (upsert)
    streamed_r = (
        sb.table("strava_streams_raw")
        .select("strava_activity_id", count="exact")
        .execute()
    )
    with_streams = streamed_r.count or 0

    # Métricas de cobertura — paginar para superar límite de 1000 filas de Supabase
    rows = []
    page, page_size = 0, 1000
    while True:
        cov_r = (
            sb.table("strava_activities_raw")
            .select("sport_type,gps_coverage,hr_coverage,cadence_coverage,power_coverage,stream_quality")
            .not_.is_("stream_quality", "null")
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        chunk = cov_r.data or []
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1

    def _avg(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    def _dist(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return {
            "high":   sum(1 for v in vals if v >= 0.80),
            "medium": sum(1 for v in vals if 0.40 <= v < 0.80),
            "low":    sum(1 for v in vals if 0.0 < v < 0.40),
            "none":   sum(1 for v in vals if v == 0.0),
        }

    # Desglose por deporte
    by_sport: dict = {}
    for r in rows:
        sport = r.get("sport_type") or "Unknown"
        if sport not in by_sport:
            by_sport[sport] = {"count": 0, "hr": [], "gps": [], "cad": [], "pow": []}
        bs = by_sport[sport]
        bs["count"] += 1
        for key, lst in [("hr_coverage","hr"),("gps_coverage","gps"),
                          ("cadence_coverage","cad"),("power_coverage","pow")]:
            if r.get(key) is not None:
                bs[lst].append(r[key])

    sport_summary = {}
    for sport, d in by_sport.items():
        sport_summary[sport] = {
            "count": d["count"],
            "avg_hr_coverage":      round(sum(d["hr"])/len(d["hr"]),4) if d["hr"] else 0.0,
            "avg_gps_coverage":     round(sum(d["gps"])/len(d["gps"]),4) if d["gps"] else 0.0,
            "avg_cadence_coverage": round(sum(d["cad"])/len(d["cad"]),4) if d["cad"] else 0.0,
            "avg_power_coverage":   round(sum(d["pow"])/len(d["pow"]),4) if d["pow"] else 0.0,
        }

    pct = round(with_streams / total * 100, 1) if total else 0.0

    return JSONResponse({
        "total_activities":      total,
        "with_streams":          with_streams,
        "without_streams":       total - with_streams,
        "pct_complete":          pct,
        "activities_with_quality_data": len(rows),
        "avg_stream_quality":    _avg("stream_quality"),
        "avg_hr_coverage":       _avg("hr_coverage"),
        "avg_gps_coverage":      _avg("gps_coverage"),
        "avg_cadence_coverage":  _avg("cadence_coverage"),
        "avg_power_coverage":    _avg("power_coverage"),
        "distribution": {
            "hr_coverage":       _dist("hr_coverage"),
            "gps_coverage":      _dist("gps_coverage"),
            "cadence_coverage":  _dist("cadence_coverage"),
            "power_coverage":    _dist("power_coverage"),
        },
        "by_sport": sport_summary,
        "backfill_status": "complete" if pct >= 100.0 else "in_progress",
        "timestamp": datetime.utcnow().isoformat(),
    })


# ── Health check ─────────────────────────────────────────

@router.get("/status")
async def strava_status():
    """Verifica que el módulo Strava está funcionando."""
    try:
        token = await get_valid_token()
        has_token = bool(token)
    except Exception:
        has_token = False

    return JSONResponse({
        "module": "strava",
        "status": "ok" if has_token else "no_auth",
        "has_valid_token": has_token,
        "webhook_url": STRAVA_CALLBACK_URL,
        "timestamp": datetime.utcnow().isoformat(),
    })


# ── Strava activities browser ─────────────────────────────

@router.get("/activities")
async def strava_activities(
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    sport: Optional[str] = Query(None),
):
    """
    Navegar actividades de strava_activities_raw con indicadores de calidad de streams.
    Permite explorar el historial completo de Strava (3,089+ actividades) con filtro por deporte.
    """
    from .auth import get_supabase
    sb = get_supabase()

    q = sb.table("strava_activities_raw").select(
        "strava_id,name,sport_type,distance_m,moving_time_s,elapsed_time_s,"
        "started_at,avg_hr,elevation_gain_m,avg_speed_ms,"
        "has_gps,has_cadence,has_power,"
        "hr_coverage,gps_coverage,cadence_coverage,power_coverage,stream_quality"
    )
    if sport:
        q = q.ilike("sport_type", f"%{sport}%")
    q = q.order("started_at", desc=True).range(offset, offset + limit - 1)

    res = q.execute()
    rows = res.data or []

    # IDs con streams — usar count es suficiente aquí, pero para el flag por actividad
    # necesitamos los IDs de la página actual. Filtramos con strava_streams_raw.
    page_ids = [r["strava_id"] for r in rows]
    streamed_ids: set = set()
    if page_ids:
        sr = (
            sb.table("strava_streams_raw")
            .select("strava_activity_id")
            .in_("strava_activity_id", page_ids)
            .execute()
        )
        streamed_ids = {r["strava_activity_id"] for r in (sr.data or [])}

    activities = []
    for r in rows:
        dist_km = round(r.get("distance_m", 0) / 1000, 2) if r.get("distance_m") else None
        moving_s = r.get("moving_time_s") or 0
        h, m, s = moving_s // 3600, (moving_s % 3600) // 60, moving_s % 60
        duration_hms = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        has_streams = r["strava_id"] in streamed_ids
        activities.append({
            "strava_id":         r["strava_id"],
            "name":              r.get("name") or r.get("sport_type") or "Actividad",
            "sport_type":        r.get("sport_type"),
            "distance_km":       dist_km,
            "duration_hms":      duration_hms,
            "moving_time_s":     moving_s,
            "start_date":        (r.get("started_at") or "")[:10],
            "avg_hr":            r.get("avg_hr"),
            "elevation_m":       r.get("elevation_gain_m"),
            "avg_speed_kmh":     round(r.get("avg_speed_ms", 0) * 3.6, 1) if r.get("avg_speed_ms") else None,
            "has_streams":       has_streams,
            "stream_quality":    r.get("stream_quality"),
            "hr_coverage":       r.get("hr_coverage"),
            "gps_coverage":      r.get("gps_coverage"),
            "cadence_coverage":  r.get("cadence_coverage"),
            "power_coverage":    r.get("power_coverage"),
        })

    total_r = sb.table("strava_activities_raw").select("id", count="exact")
    if sport:
        total_r = total_r.ilike("sport_type", f"%{sport}%")
    total = total_r.execute().count or 0

    return JSONResponse({
        "activities": activities,
        "total":      total,
        "limit":      limit,
        "offset":     offset,
    })


@router.get("/activity/{strava_id}/stream-preview")
async def strava_stream_preview(strava_id: int):
    """
    Retorna muestra de streams (máx 120 puntos) para visualizar en el UI.
    Ideal para sparklines de HR, cadencia, velocidad, potencia.
    """
    import json
    from .auth import get_supabase
    sb = get_supabase()
    res = sb.table("strava_streams_raw").select(
        "stream_time,stream_heartrate,stream_cadence,stream_velocity_smooth,stream_watts,stream_altitude"
    ).eq("strava_activity_id", strava_id).limit(1).execute()

    if not res.data:
        return JSONResponse({"error": "no_streams"}, status_code=404)

    row = res.data[0]

    def _sample(raw, n=120):
        """Toma hasta n puntos equidistantes del stream."""
        if not raw:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return []
        if not data:
            return []
        step = max(1, len(data) // n)
        return [round(v, 1) if v is not None else None for v in data[::step][:n]]

    time_raw  = row.get("stream_time") or []
    hr        = _sample(row.get("stream_heartrate"))
    cadence   = _sample(row.get("stream_cadence"))
    speed     = _sample(row.get("stream_velocity_smooth"))
    watts     = _sample(row.get("stream_watts"))
    altitude  = _sample(row.get("stream_altitude"))

    try:
        total_s = (json.loads(time_raw) if isinstance(time_raw, str) else time_raw)[-1] if time_raw else 0
    except Exception:
        total_s = 0

    return JSONResponse({
        "strava_id":  strava_id,
        "duration_s": total_s,
        "n_points":   len(json.loads(time_raw) if isinstance(time_raw, str) else (time_raw or [])),
        "hr":         hr,
        "cadence":    cadence,
        "speed_ms":   speed,
        "watts":      watts,
        "altitude":   altitude,
    })
