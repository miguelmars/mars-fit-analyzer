#!/usr/bin/env python3
"""
tools/garmin_sleep_to_wellness.py — Fase D1 + D2
================================================
Importa datos de sueño de Garmin (garmin_export_sleep staging)
hacia la tabla canonical wellness.

Reglas de importación (NO NEGOCIABLES):
  ✓  1 registro por día — upsert por date + source
  ✓  NO sobrescribe wellness manual existente
  ✓  NO sobrescribe fatiga manual
  ✓  source = 'garmin_sleep_export'
  ✓  source_confidence = 0.90 · is_subjective = false
  ✓  source_batch = 'garmin_export_2026_06_08'

Uso:
  python tools/garmin_sleep_to_wellness.py --dry-run      # solo reporte
  python tools/garmin_sleep_to_wellness.py --import       # importar
  python tools/garmin_sleep_to_wellness.py --coverage     # reporte por año
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_db  # noqa: E402


SOURCE_BATCH      = "garmin_export_2026_06_08"
SOURCE_CONFIDENCE = 0.90


# ── Helpers ──────────────────────────────────────────────────────────────────

def _s_to_hours(seconds: int | None) -> float | None:
    if seconds is None or seconds <= 0:
        return None
    return round(seconds / 3600, 2)


def _sleep_quality(score: int | None) -> str | None:
    """Convierte sleep_score de Garmin a etiqueta cualitativa."""
    if score is None:
        return None
    if score >= 80:
        return "excelente"
    if score >= 60:
        return "buena"
    if score >= 40:
        return "regular"
    return "mala"


# ── Función principal de importación ─────────────────────────────────────────

def import_sleep_to_wellness(dry_run: bool = True) -> dict[str, Any]:
    """
    Lee garmin_export_sleep y hace upsert en wellness.

    Args:
        dry_run: si True, solo reporta sin escribir nada.

    Returns:
        dict con conteos y reporte detallado.
    """
    conn = get_db()
    cur  = conn.cursor()

    # ── 1. Leer staging ──────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            calendar_date,
            duration_s,
            sleep_score,
            deep_sleep_s,
            light_sleep_s,
            rem_sleep_s,
            awake_s
        FROM garmin_export_sleep
        WHERE calendar_date IS NOT NULL
          AND duration_s > 0
        ORDER BY calendar_date
    """)
    staging_rows = cur.fetchall()

    # ── 2. Leer días con wellness manual ya registrado ───────────────────────
    # Para NO sobrescribirlos — protección de datos manuales
    cur.execute("""
        SELECT DISTINCT date
        FROM wellness
        WHERE source IN ('manual_morning_check', 'manual_fatigue', 'manual_sleep_hours')
           OR source IS NULL
    """)
    manual_dates = {row[0] for row in cur.fetchall()}

    # ── 3. Leer días que ya tienen garmin_sleep_export ───────────────────────
    cur.execute("""
        SELECT DISTINCT date
        FROM wellness
        WHERE source = 'garmin_sleep_export'
    """)
    already_imported = {row[0] for row in cur.fetchall()}

    # ── 4. Procesar cada registro ─────────────────────────────────────────────
    results = {
        "total_sleep_records": len(staging_rows),
        "imported_days":    0,
        "updated_days":     0,
        "skipped_manual":   0,
        "skipped_duplicate": 0,
        "errors":           0,
        "years_covered":    set(),
        "coverage_by_year": {},
        "dry_run":          dry_run,
    }

    for row in staging_rows:
        cal_date, dur_s, sleep_score, deep_s, light_s, rem_s, awake_s = row

        if isinstance(cal_date, str):
            from datetime import date as dt_date
            cal_date = dt_date.fromisoformat(cal_date)

        year = cal_date.year
        results["years_covered"].add(year)

        # No sobrescribir datos manuales
        if cal_date in manual_dates:
            results["skipped_manual"] += 1
            continue

        sleep_h = _s_to_hours(dur_s)
        quality  = _sleep_quality(sleep_score)

        if dry_run:
            if cal_date in already_imported:
                results["updated_days"] += 1
            else:
                results["imported_days"] += 1
            continue

        # ── Upsert en wellness ────────────────────────────────────────────────
        try:
            if cal_date in already_imported:
                cur.execute("""
                    UPDATE wellness SET
                        sleep_hours          = %s,
                        sleep_quality        = %s,
                        garmin_sleep_score   = %s,
                        source_confidence    = %s,
                        source_batch         = %s
                    WHERE date = %s AND source = 'garmin_sleep_export'
                """, (sleep_h, quality, sleep_score, SOURCE_CONFIDENCE, SOURCE_BATCH, cal_date))
                results["updated_days"] += 1
            else:
                cur.execute("""
                    INSERT INTO wellness (
                        date, category,
                        sleep_hours, sleep_quality, garmin_sleep_score,
                        source, source_confidence, source_batch, is_subjective
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    cal_date, "sleep_garmin",
                    sleep_h, quality, sleep_score,
                    "garmin_sleep_export", SOURCE_CONFIDENCE, SOURCE_BATCH, False,
                ))
                results["imported_days"] += 1
        except Exception as e:
            results["errors"] += 1
            print(f"  ERR {cal_date}: {e}")

    if not dry_run:
        conn.commit()

    cur.close()

    results["years_covered"] = sorted(results["years_covered"])
    return results


# ── Reporte de cobertura por año (D2) ────────────────────────────────────────

def coverage_report() -> dict[str, Any]:
    """
    D2: Reporte de cobertura de sueño Garmin por año.
    Determina qué años son confiables para E25C Recuperación History.

    Criterio:
      >= 80% → confiable para history
      40-79% → parcial (etiquetar madurez baja)
      < 40%  → data_insufficient
    """
    conn = get_db()
    cur  = conn.cursor()

    # Días con dato de sueño Garmin en staging
    cur.execute("""
        SELECT
            EXTRACT(YEAR FROM calendar_date)::INT AS year,
            COUNT(*) AS days_with_data
        FROM garmin_export_sleep
        WHERE calendar_date IS NOT NULL
          AND duration_s > 0
        GROUP BY 1
        ORDER BY 1
    """)
    sleep_by_year = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()

    if not sleep_by_year:
        return {"error": "No hay datos en garmin_export_sleep. Corre garmin_export_normalize.py primero."}

    min_year = min(sleep_by_year.keys())
    max_year = max(sleep_by_year.keys())

    report = {
        "generated_at": str(date.today()),
        "total_sleep_records_in_staging": sum(sleep_by_year.values()),
        "year_range": f"{min_year}–{max_year}",
        "by_year": [],
        "reliable_years": [],
        "partial_years": [],
        "insufficient_years": [],
        "e25c_ready": False,
    }

    for year in range(min_year, max_year + 1):
        days_in_year = 366 if year % 4 == 0 else 365
        # Si el año está en curso, días hasta hoy
        if year == date.today().year:
            start_of_year = date(year, 1, 1)
            days_in_year = (date.today() - start_of_year).days + 1

        days_with_data = sleep_by_year.get(year, 0)
        coverage_pct   = round(days_with_data / days_in_year * 100, 1)

        if coverage_pct >= 80:
            status = "confiable"
            tag    = "✅"
            report["reliable_years"].append(year)
        elif coverage_pct >= 40:
            status = "parcial"
            tag    = "⚠️"
            report["partial_years"].append(year)
        else:
            status = "data_insufficient"
            tag    = "❌"
            report["insufficient_years"].append(year)

        report["by_year"].append({
            "year":          year,
            "days_with_data": days_with_data,
            "days_in_year":  days_in_year,
            "coverage_pct":  coverage_pct,
            "status":        status,
            "tag":           tag,
            "use_for_e25c":  coverage_pct >= 80,
        })

    # E25C listo si al menos 3 años confiables
    report["e25c_ready"] = len(report["reliable_years"]) >= 3
    report["e25c_note"] = (
        f"E25C habilitado para años: {report['reliable_years']}. "
        f"Años parciales con etiqueta PRELIMINAR: {report['partial_years']}. "
        f"Años insuficientes excluidos: {report['insufficient_years']}."
        if report["e25c_ready"]
        else "E25C bloqueado — menos de 3 años con cobertura >=80%."
    )

    return report


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Garmin sleep → wellness import (D1 + D2)")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run",  action="store_true", help="Reporte sin escribir nada")
    group.add_argument("--import",   dest="do_import", action="store_true", help="Importar datos")
    group.add_argument("--coverage", action="store_true", help="Reporte de cobertura por año (D2)")
    args = parser.parse_args()

    if args.coverage:
        report = coverage_report()
        print("\n── Cobertura Garmin Sleep por año (D2) ──")
        for row in report.get("by_year", []):
            print(
                f"  {row['tag']} {row['year']}: "
                f"{row['days_with_data']} días / {row['days_in_year']} "
                f"({row['coverage_pct']}%) → {row['status']}"
            )
        print(f"\n  {report.get('e25c_note')}")
        return

    dry = not args.do_import
    print(f"\n── Garmin Sleep → Wellness {'(DRY RUN)' if dry else '(IMPORT)'} ──")
    results = import_sleep_to_wellness(dry_run=dry)

    print(f"  Total registros staging : {results['total_sleep_records']}")
    print(f"  Días a importar (nuevos): {results['imported_days']}")
    print(f"  Días a actualizar       : {results['updated_days']}")
    print(f"  Saltados (manual)       : {results['skipped_manual']}")
    print(f"  Errores                 : {results['errors']}")
    print(f"  Años cubiertos          : {results['years_covered']}")

    if dry:
        print("\n  DRY RUN — ningún dato escrito. Corre con --import para ejecutar.")


if __name__ == "__main__":
    main()
