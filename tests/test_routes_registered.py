"""
tests/test_routes_registered.py
================================
Verifica que cada endpoint existe y el router cargó correctamente.
404 = router NO registrado → test FALLA.
200 / 4xx / 5xx = router existe → test PASA.

raise_server_exceptions=False es crítico:
permite distinguir 404 (router no cargó) de 500 (router cargó pero falló lógica).
"""

import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app, raise_server_exceptions=False)

ROUTE_PATTERN_OVERRIDES = {
    # This endpoint correctly returns 404 when the session id does not exist.
    # For route registration, validate the FastAPI path pattern instead of a
    # fake record.
    "/gpt/session/strava_0/workout-analysis": "/gpt/session/{clean_session_id}/workout-analysis",
    # These endpoints correctly return 404 when the timeline/planned id does not exist.
    "/timeline/_probe/plan-intent": "/timeline/{event_id}/plan-intent",
    "/planned-workouts/_probe/match": "/planned-workouts/{planned_workout_id}/match",
}

CRITICAL_ROUTES = [
    # ── Core ──────────────────────────────────────────────────────────────────
    "/health",
    "/api",
    # ── Admin ─────────────────────────────────────────────────────────────────
    "/admin/health",
    "/admin/diagnostics",
    # ── Capabilities ──────────────────────────────────────────────────────────
    "/gpt/capacidades",
    "/gpt/capacidad/motor_aerobico",
    "/gpt/readiness",
    "/gpt/readiness/eventos",
    "/gpt/patron-historico",
    "/gpt/lt-detect",
    "/gpt/starting-point",
    "/gpt/testing-calibration",
    "/gpt/threshold-evidence",
    "/gpt/notifications",
    "/gpt/phase-review",
    "/gpt/baseline-compare",
    "/gpt/performance-profile",
    "/gpt/academia/glosario",
    # ── GPT Dashboard ─────────────────────────────────────────────────────────
    "/gpt/dashboard",
    "/gpt/tendencia",
    "/gpt/correlaciones",
    "/gpt/correlations",
    "/gpt/athletic-status",
    "/gpt/calendar-heatmap",
    # ── GPT History ───────────────────────────────────────────────────────────
    "/gpt/month-summary",
    "/gpt/historical-progress",
    "/gpt/month-compare",
    "/gpt/fitness-timeline",
    "/gpt/athletic-history",
    "/gpt/tests",
    # ── GPT Coaching ──────────────────────────────────────────────────────────
    "/gpt/adaptive-coach",
    "/gpt/fueling-log",
    "/gpt/gel-tests",
    "/gpt/mars-context",
    "/gpt/wellness-summary",
    "/gpt/fuerza-summary",
    "/gpt/goals",
    "/gpt/gear-status",
    "/gpt/gear-alerts",
    "/gpt/athlete-profile",
    "/gpt/session/strava_0/workout-analysis",
    # ── GPT Patterns ──────────────────────────────────────────────────────────
    "/gpt/efficiency-trend",
    "/gpt/zones-summary",
    "/gpt/cadence-trend",
    "/gpt/weekly-report",
    "/gpt/trends",
    "/gpt/weight-trend",
    # ── GPT Environment ───────────────────────────────────────────────────────
    "/gpt/environment-summary",
    # ── Activities ────────────────────────────────────────────────────────────
    "/sessions",
    "/sessions/recent",
    "/routes",
    "/gpt/latest-session",
    "/gpt/route-history",
    "/stats/yearly",
    "/stats/records",
    "/stats/monthly",
    "/stats/efficiency",
    # ── Data entry ────────────────────────────────────────────────────────────
    "/recovery",
    "/maintenance",
    "/weight/history",
    "/nutrition/summary",
    "/api/fuerza-records",
    "/api/wellness-records",
    # ── Strava integration ────────────────────────────────────────────────────
    "/api/strava/status",
    "/api/strava/transform-status",
    "/api/strava/backfill-status",
    "/api/strava/stream-completeness",
    "/api/strava/dedup-diagnosis",
    "/api/strava/dedup-samples",
    "/api/strava/approve-samples",
    "/api/strava/activities",
    "/api/strava/rename-from-garmin",
    "/api/canonical-status",
    # ── Canonical Athlete Timeline ───────────────────────────────────────────────
    "/timeline",
    "/timeline/import",
    "/timeline/import-logs",
    # ── P0 intelligence read endpoints (#2 audit · #3 debrief · #4 readiness) ────
    # NOTE: /timeline/{event_id}/audit and /debrief correctly return 404 for a bogus id,
    # so they cannot be probed here (this test treats 404 = unregistered). They are
    # covered by tests/test_intelligence_endpoints.py with a real ingested event.
    "/data-health",
    "/goal-readiness",
    "/capability-matrix",
    "/recovery-context",
    "/timeline/_probe/plan-intent",
    "/planned-workouts/_probe/match",
    # ── Admin Garmin sleep (D1 + D2) ─────────────────────────────────────────
    "/admin/garmin-sleep-coverage",
    "/admin/import-garmin-sleep",
    # ── Admin extras ─────────────────────────────────────────────────────────
    "/admin/backfill-snapshots",
    "/admin/backup",
    "/admin/clean-sessions",
    "/admin/garmin-compare",
    "/admin/garmin-staging",
    "/admin/generate-snapshot",
    "/admin/import-mars-profile",
    "/admin/phase1-audit",
    "/admin/recalculate-zones",
    "/api/admin/seed-garmin-plan",
    "/admin/zone-models",
    # ── Data entry extras ─────────────────────────────────────────────────────
    "/accidents",
    "/fuerza",
    "/gear/service",
    "/gear/service-history",
    "/gear/alerts",
    "/nutrition",
    "/weight",
    # ── Frontend SPA routes ───────────────────────────────────────────────────
    "/metas",
    "/wellness",
    "/capacidades",
    "/dashboard",
    "/activities",
    "/gear",
    "/coach",
]


@pytest.mark.parametrize("path", CRITICAL_ROUTES)
def test_route_registered(path):
    """
    Verifica que el endpoint existe y el router cargó.
    404 = router no registrado (falla el test).
    Cualquier otro código = router existe (pasa el test).
    """
    expected_pattern = ROUTE_PATTERN_OVERRIDES.get(path)
    if expected_pattern:
        registered_patterns = {getattr(route, "path", "") for route in app.routes}
        assert expected_pattern in registered_patterns, (
            f"Ruta no registrada: {expected_pattern}\n"
            f"Posible causa: router no incluido en main.py "
            f"o import circular en el router."
        )
        return

    response = client.get(path)
    assert response.status_code != 404, (
        f"Ruta no registrada: {path}\n"
        f"Posible causa: router no incluido en main.py "
        f"o import circular en el router."
    )
    # Bloque A: un 500 es un crash, no una ruta válida. 503 (sin DB) sí es
    # un estado degradado honesto y aceptable en CI.
    assert response.status_code != 500, (
        f"Crash (500) en {path} — el router cargó pero la lógica truena."
    )
