#!/usr/bin/env python3
"""Smoke test v6.4 — pantallas y endpoints críticos de Epoch.

Uso:
    python scripts/smoke_test.py [BASE_URL]
    (default: https://mars-fit-analyzer-production.up.railway.app)
"""
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else
        "https://mars-fit-analyzer-production.up.railway.app").rstrip("/")

# (ruta, debe_contener | None)
CHECKS = [
    # Páginas
    ("/home", "static/app.js"),
    ("/upload", None),
    ("/coach", "static/app.js"),
    ("/perfil", "static/app.js"),
    ("/metas", "static/app.js"),
    ("/capacidades", "static/app.js"),
    ("/progress", "static/app.js"),
    # Assets estáticos
    ("/static/app.css", ":root"),
    ("/static/app.js", "loadCapacidades"),
    ("/static/config.js", "CONFIG"),
    ("/static/utils.js", "fmtDate"),
    ("/static/api.js", "api"),
    ("/static/screens/home.js", "loadHome"),
    ("/static/screens/coach.js", "loadCoach"),
    ("/static/screens/goals.js", "loadMetas"),
    ("/static/screens/profile.js", "loadPerfil"),
    ("/static/screens/evolution.js", "loadProgress"),
    # API críticos
    ("/health", "api"),
    ("/admin/healthcheck", "status"),
    ("/gpt/dashboard", None),
    ("/gpt/capacidades", None),
    ("/gpt/goals", None),
    ("/gpt/training-context", "data_gaps"),
    ("/gpt/data-coverage", "staging_not_carried"),
    ("/gpt/weekly-intelligence", "CAPACITY BUILT"),
    ("/gpt/workout-groups", "groups"),
    # V9.1 laboratorios generalizados + V9.2 session demand
    ("/gpt/workout-groups", "effort_classes"),
    ("/gpt/session/strava_18758175501/progression", "comparison_level"),
    ("/gpt/session/strava_18758175501/demand", "demand_score"),
    # V9.3 survey · V9.4 today options · V9.5 alignment
    ("/api/session/strava_18758175501/survey", None),
    ("/gpt/today-options", "options"),
    ("/gpt/training-alignment", "alignment_pct"),
    # V10.1 fitness signal · V10.2 calendario (sessions en training-plan)
    ("/gpt/lt-detect", "consensus_lt_bpm"),
    ("/gpt/training-plan", "sessions"),
    # V10.3 loop · V10.4 capability levels
    ("/gpt/options-outcome", "outcome"),
    ("/gpt/capability-levels", "levels"),
    # V10.5 builder · V10.6 life events
    ("/gpt/life-events", "events"),
    ("/gpt/life-event-impact", "impacts"),
    ("/plan", "static/app.js"),
    ("/sesion", "static/app.js"),
    ("/legal", "Aviso de Privacidad"),
    ("/static/screens/sesion.js", "loadSesion"),
    ("/static/screens/plan.js", "loadPlan"),
    ("/gpt/training-plan", "plan"),
    ("/gpt/event-readiness-gap", None),
    ("/gpt/capability-evidence", "capacities"),
    ("/gpt/test-recommendation", "recommended"),
    ("/gpt/epochs", "epochs"),
    ("/gpt/today-adaptation", "explanation_text"),
    ("/gpt/week-rebalance", "proposals"),
    ("/gpt/event-projection", None),
    ("/api/feedback/summary", "summary"),



    ("/manifest.json", "/home"),
    ("/sw.js", "epoch-v10"),
]


def check(path, must_contain):
    url = BASE + path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "epoch-smoke/6.4"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
            if r.status != 200:
                return False, f"HTTP {r.status}"
            if must_contain and must_contain not in body:
                return False, f"falta '{must_contain}'"
            return True, "ok"
    except Exception as e:
        return False, str(e)[:80]


def check_laps_dry_run():
    """POST transform-laps en dry_run: nunca escribe, valida el pipeline."""
    url = BASE + "/api/strava/transform-laps?limit=5&dry_run=true"
    try:
        req = urllib.request.Request(url, method="POST",
                                     headers={"User-Agent": "epoch-smoke/6.5"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            if r.status != 200:
                return False, f"HTTP {r.status}"
            if '"dry_run": true' not in body and '"dry_run":true' not in body:
                return False, "respuesta sin dry_run=true"
            return True, "ok (sin escrituras)"
    except Exception as e:
        return False, str(e)[:80]


def main():
    print(f"Smoke test → {BASE}\n")
    failed = []
    # / debe redirigir a /home
    try:
        req = urllib.request.Request(BASE + "/", headers={"User-Agent": "epoch-smoke/6.4"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.url.rstrip("/").endswith("/home")
        print(f"{'✅' if ok else '❌'} / → /home redirect")
        if not ok:
            failed.append("/")
    except Exception as e:
        print(f"❌ / redirect: {e}")
        failed.append("/")

    for path, needle in CHECKS:
        ok, detail = check(path, needle)
        print(f"{'✅' if ok else '❌'} {path} — {detail}")
        if not ok:
            failed.append(path)

    ok, detail = check_laps_dry_run()
    print(f"{'✅' if ok else '❌'} POST /api/strava/transform-laps (dry_run) — {detail}")
    if not ok:
        failed.append("transform-laps dry_run")

    # summarize-streams dry_run: 0 escrituras, 0 cuota Strava
    url = BASE + "/api/strava/summarize-streams?dry_run=true"
    try:
        req = urllib.request.Request(url, method="POST",
                                     headers={"User-Agent": "epoch-smoke/7.5"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            ok = r.status == 200 and "pending" in body
    except Exception as e:
        ok, body = False, str(e)[:80]
    print(f"{'✅' if ok else '❌'} POST /api/strava/summarize-streams (dry_run) — {'ok' if ok else body}")
    if not ok:
        failed.append("summarize-streams dry_run")

    # enrich-weather dry_run: 0 escrituras, 0 llamadas externas
    url = BASE + "/api/enrich-weather?dry_run=true"
    try:
        req = urllib.request.Request(url, method="POST",
                                     headers={"User-Agent": "epoch-smoke/8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            ok = r.status == 200 and "pending" in body
    except Exception as e:
        ok, body = False, str(e)[:80]
    print(f"{'✅' if ok else '❌'} POST /api/enrich-weather (dry_run) — {'ok' if ok else body}")
    if not ok:
        failed.append("enrich-weather dry_run")

    # backfill-laps dry_run: estima pendientes en staging, 0 requests a Strava
    url = BASE + "/api/strava/backfill-laps?dry_run=true"
    try:
        req = urllib.request.Request(url, method="POST",
                                     headers={"User-Agent": "epoch-smoke/6.5"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            ok = r.status == 200 and "pending" in body
    except Exception as e:
        ok, body = False, str(e)[:80]
    print(f"{'✅' if ok else '❌'} POST /api/strava/backfill-laps (dry_run) — {'ok' if ok else body}")
    if not ok:
        failed.append("backfill-laps dry_run")

    total = len(CHECKS) + 5
    print(f"\n{total - len(failed)}/{total} OK")
    if failed:
        print("FALLAS:", ", ".join(failed))
        sys.exit(1)
    print("Todo verde 🟢")


if __name__ == "__main__":
    main()
