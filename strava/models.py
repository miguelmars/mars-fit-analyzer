"""
Modelos Pydantic para validar data de Strava.
Mapeados al esquema canónico de Bitácora Mars.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── Webhook Event (lo que Strava pushea) ─────────────────
class StravaWebhookEvent(BaseModel):
    """Evento que Strava envía al webhook callback."""
    object_type: str          # "activity" o "athlete"
    object_id: int            # ID de la actividad o atleta
    aspect_type: str          # "create", "update", "delete"
    updates: dict = Field(default_factory=dict)
    owner_id: int             # ID del atleta
    subscription_id: int
    event_time: int           # Unix timestamp


class StravaWebhookValidation(BaseModel):
    """Query params del GET de validación de Strava."""
    hub_mode: str = Field(alias="hub.mode")
    hub_challenge: str = Field(alias="hub.challenge")
    hub_verify_token: str = Field(alias="hub.verify_token")

    class Config:
        populate_by_name = True


# ── Token Storage ────────────────────────────────────────
class StravaTokens(BaseModel):
    """Tokens OAuth almacenados en Supabase."""
    access_token: str
    refresh_token: str
    expires_at: int           # Unix timestamp
    athlete_id: int
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow().timestamp() >= self.expires_at


# ── Activity Summary (respuesta de Strava) ───────────────
class StravaActivitySummary(BaseModel):
    """Campos relevantes de GET /activities/{id}."""
    id: int
    name: str
    type: str                              # "Ride", "Run", etc.
    sport_type: Optional[str] = None       # "MountainBikeRide", etc.
    start_date: datetime
    start_date_local: datetime
    timezone: Optional[str] = None
    distance: float                        # metros
    moving_time: int                       # segundos
    elapsed_time: int                      # segundos
    total_elevation_gain: float            # metros
    elev_high: Optional[float] = None
    elev_low: Optional[float] = None
    average_speed: float                   # m/s
    max_speed: float                       # m/s
    average_heartrate: Optional[float] = None
    max_heartrate: Optional[float] = None
    has_heartrate: bool = False
    average_cadence: Optional[float] = None
    average_watts: Optional[float] = None
    max_watts: Optional[int] = None
    kilojoules: Optional[float] = None
    calories: Optional[float] = None
    device_name: Optional[str] = None
    gear_id: Optional[str] = None
    start_latlng: Optional[list[float]] = None
    end_latlng: Optional[list[float]] = None
    suffer_score: Optional[float] = None
    pr_count: Optional[int] = None

    class Config:
        extra = "ignore"  # Ignora campos que no nos interesan


# ── Modelo canónico para Supabase ────────────────────────
class BitacoraSession(BaseModel):
    """
    Modelo canónico de Bitácora Mars.
    Una sesión = una actividad procesada, lista para Supabase.
    """
    strava_id: int
    name: str
    sport: str                  # Normalizado: "cycling", "running", etc.
    sport_type_raw: str         # Valor original de Strava
    started_at: datetime
    started_at_local: datetime
    timezone: Optional[str] = None
    distance_m: float
    moving_time_s: int
    elapsed_time_s: int
    elevation_gain_m: float
    elev_high_m: Optional[float] = None
    elev_low_m: Optional[float] = None
    avg_speed_ms: float
    max_speed_ms: float
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    has_hr: bool = False
    avg_cadence: Optional[float] = None
    avg_watts: Optional[float] = None
    max_watts: Optional[int] = None
    calories: Optional[float] = None
    device: Optional[str] = None
    gear_id: Optional[str] = None
    start_lat: Optional[float] = None
    start_lng: Optional[float] = None
    suffer_score: Optional[float] = None
    source: str = "strava"
    synced_at: datetime = Field(default_factory=datetime.utcnow)

    # Streams (se populan aparte, pueden ser null si no hay data)
    stream_time: Optional[list[int]] = None
    stream_distance: Optional[list[float]] = None
    stream_heartrate: Optional[list[int]] = None
    stream_cadence: Optional[list[int]] = None
    stream_altitude: Optional[list[float]] = None
    stream_velocity: Optional[list[float]] = None
    stream_grade: Optional[list[float]] = None
    stream_latlng: Optional[list[list[float]]] = None

    @classmethod
    def from_strava(cls, activity: StravaActivitySummary) -> BitacoraSession:
        """Convierte actividad de Strava al modelo canónico."""
        sport_map = {
            "Ride": "cycling",
            "VirtualRide": "cycling",
            "MountainBikeRide": "cycling",
            "GravelRide": "cycling",
            "EBikeRide": "cycling",
            "Run": "running",
            "VirtualRun": "running",
            "TrailRun": "running",
            "Walk": "walking",
            "Hike": "hiking",
            "Swim": "swimming",
            "WeightTraining": "strength",
            "Workout": "workout",
        }
        sport = sport_map.get(activity.sport_type or activity.type, "other")

        return cls(
            strava_id=activity.id,
            name=activity.name,
            sport=sport,
            sport_type_raw=activity.sport_type or activity.type,
            started_at=activity.start_date,
            started_at_local=activity.start_date_local,
            timezone=activity.timezone,
            distance_m=activity.distance,
            moving_time_s=activity.moving_time,
            elapsed_time_s=activity.elapsed_time,
            elevation_gain_m=activity.total_elevation_gain,
            elev_high_m=activity.elev_high,
            elev_low_m=activity.elev_low,
            avg_speed_ms=activity.average_speed,
            max_speed_ms=activity.max_speed,
            avg_hr=activity.average_heartrate,
            max_hr=activity.max_heartrate,
            has_hr=activity.has_heartrate,
            avg_cadence=activity.average_cadence,
            avg_watts=activity.average_watts,
            max_watts=activity.max_watts,
            calories=activity.calories,
            device=activity.device_name,
            gear_id=activity.gear_id,
            start_lat=activity.start_latlng[0] if activity.start_latlng else None,
            start_lng=activity.start_latlng[1] if activity.start_latlng else None,
            suffer_score=activity.suffer_score,
        )
