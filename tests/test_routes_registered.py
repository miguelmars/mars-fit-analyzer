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
    response = client.get(path)
    assert response.status_code != 404, (
        f"Ruta no registrada: {path}\n"
        f"Posible causa: router no incluido en main.py "
        f"o import circular en el router."
    )
