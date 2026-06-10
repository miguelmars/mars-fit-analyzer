#!/usr/bin/env python3
"""Runner seguro v6.5 — completa backfill-laps + transform-laps por lotes.

Respeta rate limits de Strava (espera entre batches, reintenta en 429).
Solo escribe en strava_laps_raw (staging) y session_laps. Nunca toca clean_sessions.

Uso:
    python3 scripts/run_laps_backfill.py --dry-run
    python3 scripts/run_laps_backfill.py --max-cycles 3
    python3 scripts/run_laps_backfill.py --max-cycles 10 --batch 100 --sleep-minutes 16
Detener: Ctrl+C (seguro en cualquier momento — todo es idempotente).
"""
import argparse
import json
import sys
import time
import urllib.request

BASE = "https://mars-fit-analyzer-production.up.railway.app"


def post(path, timeout=300):
    req = urllib.request.Request(BASE + path, method="POST",
                                 headers={"User-Agent": "epoch-laps-runner/6.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="estimar sin insertar nada")
    ap.add_argument("--max-cycles", type=int, default=3, help="ciclos máximos (default 3)")
    ap.add_argument("--batch", type=int, default=100, help="actividades por batch (default 100)")
    ap.add_argument("--sleep-minutes", type=float, default=16, help="espera entre batches (default 16)")
    args = ap.parse_args()

    if args.dry_run:
        r = post(f"/api/strava/backfill-laps?batch={args.batch}&dry_run=true")
        print(f"DRY RUN → pending={r.get('pending')} · {r.get('message','')}")
        t = post(f"/api/strava/transform-laps?limit={args.batch}&dry_run=true")
        print(f"DRY RUN transform → {t.get('processed')} laps en staging listos para "
              f"{t.get('sessions_with_laps_found')} sesiones · "
              f"{t.get('sessions_without_laps_in_staging')} sesiones sin laps en staging")
        return

    prev_pending = None
    stale_cycles = 0
    for cycle in range(1, args.max_cycles + 1):
        # ── Backfill (consume cuota Strava) ──────────────────────────────────
        try:
            b = post(f"/api/strava/backfill-laps?batch={args.batch}")
        except Exception as e:
            print(f"[{cycle}] ERROR de red en backfill: {e} — detenido.")
            sys.exit(1)

        if b.get("rate_limited"):
            print(f"[{cycle}] 429 rate limit — esperando {args.sleep_minutes} min y reintentando…")
            time.sleep(args.sleep_minutes * 60)
            try:
                b = post(f"/api/strava/backfill-laps?batch={args.batch}")
            except Exception as e:
                print(f"[{cycle}] ERROR de red en reintento: {e} — detenido.")
                sys.exit(1)
            if b.get("rate_limited"):
                print(f"[{cycle}] 429 persistente tras espera — probable límite diario. Detenido.")
                print(f"    pending={b.get('pending')} — reintentar mañana.")
                sys.exit(0)

        if (b.get("errors") or 0) > 0 and not b.get("ok", True):
            print(f"[{cycle}] Errores no-429 en backfill: {json.dumps(b, ensure_ascii=False)} — detenido.")
            sys.exit(1)

        pending = b.get("pending")
        inserted_staging = b.get("inserted", 0)

        # ── Transform (no consume cuota Strava) ──────────────────────────────
        t = {"inserted": 0, "skipped": 0, "errors": 0}
        if inserted_staging > 0:
            try:
                t = post("/api/strava/transform-laps?limit=500")
            except Exception as e:
                print(f"[{cycle}] ERROR transform: {e} — detenido.")
                sys.exit(1)
            if (t.get("errors") or 0) > 0:
                print(f"[{cycle}] Transform con errores: {json.dumps(t, ensure_ascii=False)} — detenido.")
                sys.exit(1)

        remaining = b.get("remaining", "?")
        print(f"[{cycle}/{args.max_cycles}] pending={pending} · backfill processed={b.get('processed',0)} "
              f"staging+{inserted_staging} · transform+{t.get('inserted',0)} "
              f"skipped={t.get('skipped',0)} errors={t.get('errors',0)} · remaining={remaining}")

        if inserted_staging == 0 and (t.get("skipped") or 0) > 0:
            print("    Nota: inserted=0 con skipped alto — esas sesiones no tienen laps "
                  "en staging (actividades sin laps reales o sentinel). No es error.")

        if remaining == 0:
            print("✅ Historial de laps completo.")
            return

        # ── Stale check: pending no baja en 2 ciclos ──────────────────────────
        if prev_pending is not None and pending is not None and pending >= prev_pending:
            stale_cycles += 1
            if stale_cycles >= 2:
                print("⚠️  pending no baja en 2 ciclos — detenido. Revisa staging/cuota.")
                sys.exit(1)
        else:
            stale_cycles = 0
        prev_pending = pending

        if cycle < args.max_cycles:
            print(f"    Esperando {args.sleep_minutes} min antes del siguiente batch…")
            time.sleep(args.sleep_minutes * 60)

    print(f"Fin: {args.max_cycles} ciclos completados. Re-ejecutar para continuar.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido por usuario (seguro — todo es idempotente).")
