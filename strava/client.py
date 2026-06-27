"""
Cliente HTTP para la API de Strava v3.
Jala actividades, streams, laps, y datos del atleta.
"""
import logging
from typing import Optional

import httpx

from .config import STRAVA_API_BASE, STRAVA_STREAM_KEYS
from .auth import get_valid_token
from .models import StravaActivitySummary

logger = logging.getLogger("bitacora.strava.client")

# Timeout generoso — streams pesados pueden tardar
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def _headers() -> dict:
    """Headers con token válido (auto-refresh)."""
    token = await get_valid_token()
    return {"Authorization": f"Bearer {token}"}


# ── Actividad individual ─────────────────────────────────

async def get_activity(activity_id: int) -> StravaActivitySummary:
    """
    GET /activities/{id}
    Devuelve el detalle completo de una actividad.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers=await _headers(),
        )
        resp.raise_for_status()
        data = resp.json()

    logger.info(f"Activity {activity_id}: {data.get('name')} ({data.get('type')})")
    return StravaActivitySummary(**data)


async def update_activity_name(activity_id: int, name: str) -> dict:
    """
    PUT /activities/{id}
    Updates the Strava activity title. Requires the activity:write scope.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.put(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers=await _headers(),
            data={"name": name},
        )
        if resp.status_code in (401, 403):
            raise PermissionError(
                "Strava refused activity rename. Re-authorize at "
                "/api/strava/authorize after deploying activity:write scope."
            )
        resp.raise_for_status()
        data = resp.json()

    logger.info("Renamed Strava activity %s → %s", activity_id, name)
    return data


# ── Streams (time-series: HR, GPS, cadencia, etc.) ───────

async def get_activity_streams(activity_id: int) -> dict:
    """
    GET /activities/{id}/streams
    Devuelve streams como dict keyed por tipo.

    Ejemplo de retorno:
    {
        "time": [0, 1, 2, ...],
        "heartrate": [120, 122, 125, ...],
        "distance": [0.0, 3.2, 6.8, ...],
        ...
    }
    """
    keys_param = ",".join(STRAVA_STREAM_KEYS)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}/streams",
            headers=await _headers(),
            params={"keys": keys_param, "key_by_type": "true"},
        )

        # 404 = actividad sin streams (manual entry, por ejemplo)
        if resp.status_code == 404:
            logger.warning(f"No streams para activity {activity_id}")
            return {}

        resp.raise_for_status()
        data = resp.json()

    # Extraer solo los arrays de data
    streams = {}
    for key in STRAVA_STREAM_KEYS:
        if key in data and "data" in data[key]:
            streams[key] = data[key]["data"]

    logger.info(
        f"Streams para {activity_id}: "
        f"{', '.join(f'{k}({len(v)} pts)' for k, v in streams.items())}"
    )
    return streams


# ── Laps ─────────────────────────────────────────────────

async def get_activity_laps(activity_id: int) -> list[dict]:
    """
    GET /activities/{id}/laps
    Importante para análisis de intervalos.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}/laps",
            headers=await _headers(),
        )
        resp.raise_for_status()
        return resp.json()


# ── Lista de actividades (para backfill inicial) ─────────

async def get_recent_activities(
    per_page: int = 30,
    page: int = 1,
    after: Optional[int] = None,
    before: Optional[int] = None,
) -> list[StravaActivitySummary]:
    """
    GET /athlete/activities
    Para sincronización inicial / backfill.
    after/before son Unix timestamps.
    """
    params = {"per_page": per_page, "page": page}
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=await _headers(),
            params=params,
        )

        if resp.status_code == 429:
            # Rate limit — incluir info de cuándo resetea
            retry_after = resp.headers.get("X-RateLimit-Reset") or "900"
            raise httpx.HTTPStatusError(
                f"RATE_LIMIT:{retry_after}",
                request=resp.request,
                response=resp,
            )

        resp.raise_for_status()
        data = resp.json()

    activities = [StravaActivitySummary(**a) for a in data]
    logger.info(f"Fetched {len(activities)} actividades (page {page})")
    return activities


# ── Backfill completo ────────────────────────────────────

async def backfill_all_activities(
    after: Optional[int] = None,
) -> list[StravaActivitySummary]:
    """
    Jala TODAS las actividades paginando.
    Útil para la carga inicial de las 2,901 sesiones.
    Respeta rate limits con paginación de 200/page.
    """
    all_activities = []
    page = 1

    while True:
        batch = await get_recent_activities(
            per_page=200, page=page, after=after
        )
        if not batch:
            break
        all_activities.extend(batch)
        logger.info(f"Backfill: {len(all_activities)} actividades hasta ahora...")
        page += 1

    logger.info(f"Backfill completo: {len(all_activities)} actividades totales")
    return all_activities


# ── Datos del atleta ─────────────────────────────────────

async def get_athlete() -> dict:
    """GET /athlete — perfil del atleta autenticado."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/athlete",
            headers=await _headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def get_athlete_stats(athlete_id: int) -> dict:
    """GET /athletes/{id}/stats — totales acumulados."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{STRAVA_API_BASE}/athletes/{athlete_id}/stats",
            headers=await _headers(),
        )
        resp.raise_for_status()
        return resp.json()
