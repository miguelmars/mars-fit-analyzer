"""
FastAPI Router — Endpoints de Strava para Bitácora Mars.

Endpoints:
  GET  /api/strava/authorize     → Redirige a Strava para autorizar (setup inicial)
  GET  /api/strava/callback      → Recibe authorization code y lo intercambia por tokens
  GET  /api/strava/webhook       → Validación de webhook (Strava verifica tu endpoint)
  POST /api/strava/webhook       → Recibe eventos push de Strava
  POST /api/strava/backfill      → Trigger manual de sincronización histórica
  GET  /api/strava/status        → Health check del módulo
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

    # Buscar actividades sin streams todavía
    query = (
        sb.table("strava_activities_raw")
        .select("strava_id, name, sport_type")
        .is_("has_gps", "null")  # columna nueva = nunca procesada
        .limit(batch)
    )
    if sport:
        query = query.ilike("sport_type", f"%{sport}%")

    rows = query.execute().data or []

    if not rows:
        # Intentar con has_gps = false (ya procesadas pero sin GPS)
        query2 = (
            sb.table("strava_activities_raw")
            .select("strava_id, name, sport_type")
            .eq("has_gps", False)
            .eq("hr_coverage", 0.0)
            .limit(batch)
        )
        if sport:
            query2 = query2.ilike("sport_type", f"%{sport}%")
        rows = query2.execute().data or []

    if not rows:
        # Contar cuántas quedan
        total = sb.table("strava_streams_raw").select("id", count="exact").execute()
        return JSONResponse({
            "status":  "completed",
            "message": "Todas las actividades tienen streams o no hay GPS disponible.",
            "streams_in_db": total.count or 0,
        })

    results = {"ok": 0, "empty": 0, "error": 0, "processed": len(rows)}
    for row in rows:
        activity_id = row["strava_id"]
        try:
            result = await ingest_activity(
                activity_id,
                is_update=False,
                fetch_streams=True,
                fetch_laps=False,
            )
            if result.get("has_streams"):
                results["ok"] += 1
            else:
                results["empty"] += 1
        except Exception as e:
            logger.warning(f"Stream error activity {activity_id}: {e}")
            results["error"] += 1

    # Contar cuántas quedan sin streams
    remaining = (
        sb.table("strava_activities_raw")
        .select("id", count="exact")
        .is_("has_gps", "null")
        .execute()
    )

    return JSONResponse({
        "status":    "ok",
        "processed": results["processed"],
        "with_streams": results["ok"],
        "no_gps":    results["empty"],
        "errors":    results["error"],
        "remaining_without_streams": remaining.count or 0,
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

    # Estimar ETA
    rate_per_day = 2000
    remaining    = activities_pending
    eta_days     = ceil(remaining / rate_per_day) if remaining > 0 else 0
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
