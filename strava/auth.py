"""
OAuth 2.0 para Strava.
- Flujo de autorización inicial (una sola vez)
- Refresh automático de tokens expirados
- Almacenamiento de tokens en Supabase
"""
import time
import logging
from typing import Optional

import httpx

from .config import (
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
    STRAVA_AUTH_URL, STRAVA_TOKEN_URL, STRAVA_SCOPES,
    SUPABASE_URL, SUPABASE_KEY,
)
from .models import StravaTokens

logger = logging.getLogger("bitacora.strava.auth")

# ── Supabase client singleton ────────────────────────────
# Lazy import: supabase se importa solo cuando se necesita (no al cargar el módulo).
# Esto permite que el router se registre aunque supabase no esté disponible.
_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client  # lazy — no falla en startup
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


# ── Tabla en Supabase: strava_tokens ─────────────────────
# CREATE TABLE strava_tokens (
#   athlete_id   BIGINT PRIMARY KEY,
#   access_token TEXT NOT NULL,
#   refresh_token TEXT NOT NULL,
#   expires_at   BIGINT NOT NULL,
#   updated_at   TIMESTAMPTZ DEFAULT now()
# );


def get_authorization_url(redirect_uri: str) -> str:
    """
    Genera la URL para que el usuario autorice la app.
    Solo se usa una vez en el setup inicial.
    """
    return (
        f"{STRAVA_AUTH_URL}"
        f"?client_id={STRAVA_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&approval_prompt=force"
        f"&scope={STRAVA_SCOPES}"
    )


async def exchange_code(code: str) -> StravaTokens:
    """
    Intercambia el authorization code por tokens.
    Se llama una sola vez después de que autorizas en el navegador.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        data = resp.json()

    tokens = StravaTokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=data["expires_at"],
        athlete_id=data["athlete"]["id"],
    )

    # Guardar en Supabase
    await _save_tokens(tokens)
    logger.info(f"Tokens guardados para athlete {tokens.athlete_id}")
    return tokens


async def get_valid_token(athlete_id: Optional[int] = None) -> str:
    """
    Devuelve un access_token válido.
    Si está expirado, lo refresca automáticamente.
    """
    tokens = await _load_tokens(athlete_id)
    if tokens is None:
        raise RuntimeError(
            "No hay tokens almacenados. Ejecuta el flujo de autorización primero: "
            "GET /api/strava/authorize"
        )

    # Refrescar si expira en menos de 5 minutos
    if tokens.expires_at - time.time() < 300:
        logger.info("Token expirado o por expirar, refrescando...")
        tokens = await _refresh_token(tokens)

    return tokens.access_token


async def _refresh_token(tokens: StravaTokens) -> StravaTokens:
    """Refresca el access token usando el refresh token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(STRAVA_TOKEN_URL, data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        data = resp.json()

    new_tokens = StravaTokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=data["expires_at"],
        athlete_id=tokens.athlete_id,
    )

    await _save_tokens(new_tokens)
    logger.info(f"Token refrescado para athlete {new_tokens.athlete_id}")
    return new_tokens


async def _save_tokens(tokens: StravaTokens):
    """Upsert tokens en Supabase."""
    sb = get_supabase()
    sb.table("strava_tokens").upsert({
        "athlete_id": tokens.athlete_id,
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "updated_at": tokens.updated_at.isoformat(),
    }).execute()


async def _load_tokens(athlete_id: Optional[int] = None) -> Optional[StravaTokens]:
    """
    Carga tokens de Supabase.
    Si no se pasa athlete_id, toma el primero (single player mode).
    """
    sb = get_supabase()
    query = sb.table("strava_tokens").select("*")

    if athlete_id:
        query = query.eq("athlete_id", athlete_id)

    result = query.limit(1).execute()

    if not result.data:
        return None

    row = result.data[0]
    return StravaTokens(
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        athlete_id=row["athlete_id"],
    )
