"""
Bloque A — pruebas del candado de seguridad.
El revisor tenía razón: 156 tests verdes no prueban seguridad si ninguno
ejercita el middleware. Estas pruebas guardan el contrato:
  · escrituras sin llave → 401 (cuando EPOCH_API_KEY está configurada)
  · webhook de Strava exento (Strava no puede mandar nuestra header)
  · GET peligroso (register-webhook) también exige llave
  · subida gigante → 413
  · ráfaga → 429
  · CORS: orígenes ajenos no reciben Access-Control-Allow-Origin
"""
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture
def armed_key(monkeypatch):
    """Activa la llave solo durante la prueba y limpia el rate limit."""
    monkeypatch.setattr(main, "_EPOCH_API_KEY", "test-key-123")
    main._RATE_BUCKETS.clear()
    yield "test-key-123"
    main._RATE_BUCKETS.clear()


def test_write_without_key_is_401(armed_key):
    r = client.post("/api/feedback?context=test&verdict=up")
    assert r.status_code == 401


def test_write_with_wrong_key_is_401(armed_key):
    r = client.post("/api/feedback?context=test&verdict=up",
                    headers={"X-Epoch-Key": "wrong"})
    assert r.status_code == 401


def test_write_with_key_passes_auth(armed_key):
    r = client.post("/api/feedback?context=test&verdict=up",
                    headers={"X-Epoch-Key": armed_key})
    # 200 con DB · 503 sin DB — lo que NO puede ser es 401
    assert r.status_code != 401


def test_reads_stay_public(armed_key):
    r = client.get("/health")
    assert r.status_code == 200


def test_strava_webhook_is_exempt(armed_key):
    # Strava no puede mandar X-Epoch-Key; el webhook valida por su cuenta.
    r = client.post("/api/strava/webhook", json={"aspect_type": "noop"})
    assert r.status_code != 401


def test_dangerous_get_requires_key(armed_key):
    r = client.get("/api/strava/register-webhook")
    assert r.status_code == 401


def test_dedup_diagnosis_get_requires_key(armed_key):
    # GET que escribe en strava_sync_state — debe exigir llave.
    r = client.get("/api/strava/dedup-diagnosis")
    assert r.status_code == 401


def test_real_upload_size_enforced(armed_key, monkeypatch):
    # El limite real se valida sobre los bytes leidos, no solo Content-Length.
    monkeypatch.setattr(main, "_UPLOAD_MAX_BYTES", 50)
    files = {"file": ("big.fit", b"x" * 5000, "application/octet-stream")}
    r = client.post("/analyze-fit", files=files,
                    headers={"X-Epoch-Key": armed_key})
    assert r.status_code == 413


def test_oversized_upload_is_413(armed_key, monkeypatch):
    monkeypatch.setattr(main, "_UPLOAD_MAX_BYTES", 10)
    r = client.post("/api/feedback?context=test&verdict=up",
                    headers={"X-Epoch-Key": armed_key},
                    content=b"x" * 100)
    assert r.status_code == 413


def test_rate_limit_429(monkeypatch):
    monkeypatch.setattr(main, "_RATE_MAX_PER_MIN", 5)
    main._RATE_BUCKETS.clear()
    codes = [client.get("/health").status_code for _ in range(8)]
    main._RATE_BUCKETS.clear()
    assert 429 in codes


def test_cors_blocks_foreign_origin():
    r = client.options("/gpt/dashboard",
                       headers={"Origin": "https://evil.example.com",
                                "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_allows_own_origin():
    own = "https://mars-fit-analyzer-production.up.railway.app"
    r = client.options("/gpt/dashboard",
                       headers={"Origin": own,
                                "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == own


def test_frontend_assets_are_versioned_and_have_critical_shell():
    r = client.get("/home")
    assert r.status_code == 200
    assert "/static/app.css?v=20260612.3" in r.text
    assert "/static/app.js?v=20260612.3" in r.text
    assert "Critical shell" in r.text


def test_service_worker_rejects_invalid_cached_assets():
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert r.headers["service-worker-allowed"] == "/"
    assert "if(!response || !response.ok)return response" in r.text
    assert "path.endsWith('.css') && !type.includes('text/css')" in r.text
    assert "url.pathname.startsWith('/api/')" in r.text


def test_responsive_css_contract():
    r = client.get("/static/app.css?v=20260612.3")
    assert r.status_code == 200
    assert "@media(max-width:350px)" in r.text
    assert "@media(min-width:600px)" in r.text
    assert "@media(min-width:900px)" in r.text
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in r.text
