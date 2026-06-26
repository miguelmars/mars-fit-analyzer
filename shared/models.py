"""
shared/models.py — Pydantic models de Bitácora Mars
=====================================================
Extraídos de main.py (TD-010A) para compartir entre todos los routers.
Importar así:
    from shared.models import WellnessIn, GoalCreate, ...
"""

from typing import Optional, List
from pydantic import BaseModel, Field


# ── Gear / Bicicleta ─────────────────────────────────────────────────────────

class GearServiceIn(BaseModel):
    service_type: str
    gear_id: str = None
    gear_name: str = None
    description: str = None
    shop: str = None
    date: str = None
    km_at_service: float = None
    cost_mxn: float = None
    notes: str = None


class AccidentIn(BaseModel):
    date: str
    description: str = None
    damage: str = None
    repair: str = None
    cost_mxn: float = None
    km_at_accident: int = None
    notes: str = None


class GearIn(BaseModel):
    gear_id: str
    name: Optional[str] = None
    type: Optional[str] = None
    bike_id: Optional[str] = None
    installed_date: Optional[str] = None
    km_at_install: Optional[int] = None
    km_limit: Optional[int] = None
    notes: Optional[str] = None


class GearUpdate(BaseModel):
    retired_date: Optional[str] = None
    retired_reason: Optional[str] = None
    km_limit: Optional[int] = None
    notes: Optional[str] = None


class MaintenanceIn(BaseModel):
    bike_id: Optional[str] = None
    gear_id: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    date: Optional[str] = None
    km_at_service: Optional[int] = None
    cost_mxn: Optional[float] = None
    shop: Optional[str] = None
    notes: Optional[str] = None


# ── Nutrición ─────────────────────────────────────────────────────────────────

class NutritionIn(BaseModel):
    date: str
    session_id: str = None
    moment: str = "durante"
    gel_type: str = None
    gel_count: int = None
    agua_ml: int = None
    carbos_g: float = None
    notas: str = None
    gi_response: str = None
    energy_response: str = None


# ── Peso corporal ─────────────────────────────────────────────────────────────

class WeightIn(BaseModel):
    date: str
    weight_kg: float
    waist_cm: float = None
    body_fat_pct: float = None
    notes: str = None


# ── Wellness / Recuperación ──────────────────────────────────────────────────

class WellnessIn(BaseModel):
    date: str
    category: str = None
    compex_program: str = None
    muscle_zone: list = None
    duration_min: int = None
    ceragem_duration_min: int = None
    ceragem_sensation_before: int = None
    ceragem_sensation_after: int = None
    sleep_hours: float = None
    sleep_quality: str = None
    hr_rest: int = None
    garmin_sleep_score: int = None
    pain_zone: str = None
    pain_level: int = None
    pain_start: str = None
    pain_end: str = None
    pain_type: str = None
    stress_level: int = None
    stress_cause: str = None
    notes: str = None
    fatigue: int = None


class RecoveryIn(BaseModel):
    date: Optional[str] = None
    type: Optional[str] = None
    duration_min: Optional[int] = None
    muscle_zone: Optional[List[str]] = None
    compex_program: Optional[str] = None
    notes: Optional[str] = None
    fatigue: Optional[int] = None
    muscle_pain: Optional[int] = None
    mental_state: Optional[int] = None


# ── Fuerza ────────────────────────────────────────────────────────────────────

class FuerzaIn(BaseModel):
    date: str
    category: str = None
    subcategory: str = None
    muscle_groups: list = None
    intensity: int = None
    duration_min: int = None
    sets: int = None
    reps: int = None
    weight_kg: float = None
    exercise: str = None
    notes: str = None
    rpe: int = None
    fatigue_before: int = None
    fatigue_after: int = None


# ── Post-sesión ───────────────────────────────────────────────────────────────

class PostSessionIn(BaseModel):
    rpe: Optional[int] = Field(None, ge=1, le=10)
    weight_before: Optional[float] = Field(None, gt=0, lt=200)
    weight_after: Optional[float] = Field(None, gt=0, lt=200)
    water_liters: Optional[float] = Field(None, ge=0, le=10)
    caffeine_mg: Optional[int] = Field(None, ge=0, le=1000)
    food_before: Optional[List[str]] = None
    gels: Optional[int] = Field(None, ge=0, le=20)
    bars: Optional[int] = Field(None, ge=0, le=20)
    electrolytes: Optional[bool] = None
    digestion: Optional[str] = None
    sleep_hours: Optional[float] = Field(None, ge=0, le=16)
    sleep_quality: Optional[str] = None
    conditions: Optional[List[str]] = None
    notes: Optional[str] = None
    gel_type: Optional[str] = None
    gel_recipe: Optional[str] = None
    gel_carbs_g: Optional[int] = Field(None, ge=0, le=150)
    gel_sodium_mg: Optional[int] = Field(None, ge=0, le=3000)
    gel_timing: Optional[str] = None
    gi_response: Optional[str] = None
    energy_response: Optional[str] = None


# ── Metas (Goals) — ADR-011: el usuario define sus metas ─────────────────────

class GoalCreate(BaseModel):
    event_name: str
    event_type: str = "cycling"
    event_date: Optional[str] = None     # ISO date "YYYY-MM-DD"
    distance_km: Optional[float] = None
    elevation_m: Optional[float] = None
    priority: int = 1
    notes: Optional[str] = None
    scenario_key: Optional[str] = None  # link a Readiness Scenario


class GoalUpdate(BaseModel):
    event_name: Optional[str] = None
    event_date: Optional[str] = None
    distance_km: Optional[float] = None
    elevation_m: Optional[float] = None
    priority: Optional[int] = None
    status: Optional[str] = None         # active | completed | cancelled
    notes: Optional[str] = None


# ── Perfil del atleta ─────────────────────────────────────────────────────────

class AthleteProfileIn(BaseModel):
    age: Optional[int] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[int] = None
    hr_rest: Optional[int] = None
    hr_max: Optional[int] = None
    hr_lt: Optional[int] = None
    ftp_watts: Optional[int] = None
    vo2max: Optional[float] = None
    active_bike_id: Optional[str] = None
    goals: Optional[List[str]] = None
    injuries: Optional[List[str]] = None
    notes: Optional[str] = None


class AthleteTestIn(BaseModel):
    date: str
    type: str   # hr_drift, ftp, subida_referencia, cadencia, vo2max
    result_value: Optional[float] = None
    result_unit: Optional[str] = None
    route_id: Optional[str] = None
    duration_s: Optional[int] = None
    avg_hr_bpm: Optional[int] = None
    avg_speed_kmh: Optional[float] = None
    avg_cadence: Optional[int] = None
    conditions: Optional[str] = None
    notes: Optional[str] = None
    raw_data: Optional[dict] = None
