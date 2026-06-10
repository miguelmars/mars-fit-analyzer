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
    ("/manifest.json", "/home"),
    ("/sw.js", "epoch-v6.4"),
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

    print(f"\n{len(CHECKS) + 1 - len(failed)}/{len(CHECKS) + 1} OK")
    if failed:
        print("FALLAS:", ", ".join(failed))
        sys.exit(1)
    print("Todo verde 🟢")


if __name__ == "__main__":
    main()
