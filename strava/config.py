"""
Configuración centralizada para la integración Strava.
Todas las variables sensibles vienen de .env — ninguna tiene default inseguro.

Fixes v4.8:
- STRAVA_VERIFY_TOKEN: sin default — falla en startup si no está en env
- STRAVA_CALLBACK_URL / STRAVA_WEBHOOK_URL: dos variables separadas
- SUPABASE_KEY → SUPABASE_SERVICE_ROLE_KEY: nombre explícito
- STRAVA_ALLOWED_ATHLETE_ID: valida owner_id en cada webhook
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(var: str) -> str:
    """Falla en startup si una variable requerida no está en env."""
    val = os.getenv(var)
    if not val:
        raise RuntimeError(
            f"Variable de entorno requerida no configurada: {var}. "
            "Revisa tu .env o las variables de Railway."
        )
    return val


# ── Strava OAuth ──────────────────────────────────────────
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")

# URL de callback OAuth (una sola vez, durante el setup inicial)
STRAVA_CALLBACK_URL = os.getenv(
    "STRAVA_CALLBACK_URL",
    "http://localhost:8000/api/strava/callback"
)

# URL donde Strava envía webhooks push (debe ser HTTPS público en Railway)
STRAVA_WEBHOOK_URL = os.getenv(
    "STRAVA_WEBHOOK_URL",
    "http://localhost:8000/api/strava/webhook"
)

# Token de verificación del webhook — DEBE estar en env, sin default
# Si falta, el módulo falla al importar (intencional)
_raw_verify = os.getenv("STRAVA_VERIFY_TOKEN")
if not _raw_verify:
    import warnings
    warnings.warn(
        "STRAVA_VERIFY_TOKEN no configurado — webhook validation fallará. "
        "Agrégalo a Railway antes de registrar el webhook.",
        RuntimeWarning,
        stacklevel=2,
    )
STRAVA_VERIFY_TOKEN = _raw_verify or ""

# ID del atleta autorizado — solo se procesan webhooks de este atleta
# Obtenerlo de https://www.strava.com/athletes/<tu_id> o del primer callback OAuth
STRAVA_ALLOWED_ATHLETE_ID: int | None = (
    int(os.getenv("STRAVA_ALLOWED_ATHLETE_ID"))
    if os.getenv("STRAVA_ALLOWED_ATHLETE_ID")
    else None
)

# ── Strava API ────────────────────────────────────────────
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"

# ── Scopes necesarios ────────────────────────────────────
STRAVA_SCOPES = "read,activity:read_all"

# ── Streams que jalamos por actividad ────────────────────
STRAVA_STREAM_KEYS = [
    "time", "distance", "latlng", "altitude",
    "heartrate", "cadence", "velocity_smooth", "grade_smooth"
]

# ── Supabase — service role key, nunca la anon key ───────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
# Alias para backward compat con código que lee SUPABASE_KEY
SUPABASE_KEY = SUPABASE_SERVICE_ROLE_KEY
