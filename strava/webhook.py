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
from datetime import datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import STRAVA_VERIFY_TOKEN, STRAVA_CALLBACK_URL, STRAVA_ALLOWED_ATHLETE_ID
from .auth import get_authorization_url, exchange_code, get_valid_token
from .models import StravaWebhookEvent
from .sync import process_webhook_event, backfill_from_strava

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


# ── TD-014C: Transform staging → clean_sessions ──────────

@router.post("/transform")
async def trigger_transform(
    background_tasks: BackgroundTasks,
    batch_size: int = Query(
        default=100,
        description="Actividades por lote. Reduce si hay timeouts."
    ),
):
    """
    Transforma strava_activities_raw (pending) → clean_sessions (modelo Mars).
    TD-014C — La pieza final del pipeline de ingesta Strava.

    Seguro para correr múltiples veces — ON CONFLICT DO UPDATE.
    """
    from .transform import transform_all_pending
    background_tasks.add_task(transform_all_pending, batch_size)
    return JSONResponse({
        "status":       "transform_started",
        "batch_size":   batch_size,
        "message":      "Transformando Strava → Mars canonical en background. Revisa /transform-status.",
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
