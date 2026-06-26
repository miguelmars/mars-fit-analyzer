#!/usr/bin/env python3
"""Audit a Garmin account export without importing it into the app database.

The export contains several data sources. For the Mars app, the safest first
pass is to trust Garmin's summarized activity JSON as the activity index, then
use FIT files only after they are classified as real activities.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import os
import re
import statistics
import zipfile
from pathlib import Path
from typing import Any


def _read_json(zf: zipfile.ZipFile, name: str) -> Any:
    return json.loads(zf.read(name).decode("utf-8-sig"))


def _date_from_garmin_ms(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000
        return dt.datetime.utcfromtimestamp(ts).date().isoformat()
    except Exception:
        return None


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _garmin_summary_km(row: dict[str, Any]) -> float:
    # Garmin account exports store summarized activity distance in centimeters.
    return _as_float(row.get("distance")) / 100_000.0


def _garmin_summary_minutes(row: dict[str, Any]) -> float:
    # Garmin account exports store summarized activity duration in milliseconds.
    return _as_float(row.get("duration")) / 60_000.0


def _has_any(row: dict[str, Any], *keys: str) -> bool:
    return any(row.get(key) not in (None, "", 0) for key in keys)


def _load_summarized_activities(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    for name in zf.namelist():
        if "summarizedActivities" not in name or not name.endswith(".json"):
            continue
        data = _read_json(zf, name)
        if isinstance(data, list):
            for item in data:
                rows = item.get("summarizedActivitiesExport") if isinstance(item, dict) else None
                if isinstance(rows, list):
                    activities.extend(row for row in rows if isinstance(row, dict))
        elif isinstance(data, dict):
            rows = data.get("summarizedActivitiesExport")
            if isinstance(rows, list):
                activities.extend(row for row in rows if isinstance(row, dict))
    return activities


def _activity_stats(activities: list[dict[str, Any]]) -> dict[str, Any]:
    dates = [
        _date_from_garmin_ms(_pick(a, "startTimeLocal", "beginTimestamp", "startTimeGmt"))
        for a in activities
    ]
    dates = [d for d in dates if d]
    by_sport = collections.Counter(str(a.get("activityType") or "unknown") for a in activities)
    by_year = collections.Counter(d[:4] for d in dates)
    ids = [str(a.get("activityId") or "") for a in activities]

    field_sets = {
        "gps_start_end": ("startLatitude", "startLongitude", "endLatitude", "endLongitude"),
        "heart_rate": ("avgHr", "maxHr"),
        "cadence": ("avgBikeCadence", "avgRunCadence", "avgCadence", "avgFractionalCadence"),
        "elevation": ("elevationGain", "elevationLoss", "minElevation", "maxElevation"),
        "calories": ("calories",),
        "training_effect": ("aerobicTrainingEffect", "anaerobicTrainingEffect"),
        "temperature": ("minTemperature", "maxTemperature"),
        "power": ("avgPower", "maxPower", "normalizedPower"),
    }

    coverage = {
        label: sum(1 for row in activities if _has_any(row, *keys))
        for label, keys in field_sets.items()
    }

    sport_details: dict[str, Any] = {}
    for sport, _count in by_sport.most_common():
        rows = [a for a in activities if str(a.get("activityType") or "unknown") == sport]
        kms = [_garmin_summary_km(a) for a in rows]
        mins = [_garmin_summary_minutes(a) for a in rows]
        sport_details[sport] = {
            "count": len(rows),
            "km_total": round(sum(kms), 2),
            "km_max": round(max(kms), 2) if kms else 0,
            "duration_min_max": round(max(mins), 1) if mins else 0,
            "short_lt_2_min": sum(1 for m in mins if m < 2),
            "zero_distance": sum(1 for k in kms if k == 0),
            "coverage": {
                label: sum(1 for row in rows if _has_any(row, *keys))
                for label, keys in field_sets.items()
            },
        }

    return {
        "count": len(activities),
        "unique_activity_ids": len({i for i in ids if i}),
        "blank_activity_ids": sum(1 for i in ids if not i),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "by_year": dict(sorted(by_year.items())),
        "by_sport": dict(by_sport.most_common()),
        "coverage": coverage,
        "sport_details": sport_details,
    }


def _nested_fit_stats(zf: zipfile.ZipFile) -> dict[str, Any]:
    nested = [
        info for info in zf.infolist()
        if info.filename.endswith(".zip") and "UploadedFiles" in info.filename
    ]
    parts = []
    total_fit = 0
    for info in nested:
        with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
            fit_infos = [i for i in inner.infolist() if i.filename.lower().endswith(".fit")]
            total_fit += len(fit_infos)
            parts.append({
                "name": info.filename,
                "compressed_bytes": info.file_size,
                "fit_files": len(fit_infos),
                "largest_fit_bytes": max((i.file_size for i in fit_infos), default=0),
                "smallest_fit_bytes": min((i.file_size for i in fit_infos), default=0),
                "sample_files": [Path(i.filename).name for i in fit_infos[:5]],
            })
    return {"nested_zip_count": len(nested), "fit_files_total": total_fit, "parts": parts}


def _gear_stats(zf: zipfile.ZipFile) -> dict[str, Any]:
    gear_files = [n for n in zf.namelist() if n.endswith("_gear.json")]
    if not gear_files:
        return {"files": [], "gear_count": 0, "gear": []}

    gear_rows: list[dict[str, Any]] = []
    activity_links = 0
    default_links = 0
    for name in gear_files:
        data = _read_json(zf, name)
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                rows = item.get("gearDTOS")
                if isinstance(rows, list):
                    gear_rows.extend(row for row in rows if isinstance(row, dict))
                links = item.get("gearActivityDTOs")
                if isinstance(links, list):
                    activity_links += len(links)
                defaults = item.get("gearActivityTypeDTOS")
                if isinstance(defaults, list):
                    default_links += len(defaults)

    gear = []
    for row in gear_rows:
        max_meters = _as_float(row.get("maximumMeters"), 0)
        gear.append({
            "gear_pk": row.get("gearPk"),
            "type": row.get("gearTypeName"),
            "status": row.get("gearStatusName"),
            "name": row.get("displayName"),
            "model": row.get("customMakeModel"),
            "date_begin": row.get("dateBegin"),
            "maximum_km": round(max_meters / 1000, 1) if max_meters else None,
        })

    return {
        "files": gear_files,
        "gear_count": len(gear),
        "activity_links": activity_links,
        "default_links": default_links,
        "gear": gear,
    }


def _sleep_stats(zf: zipfile.ZipFile) -> dict[str, Any]:
    files = [n for n in zf.namelist() if n.endswith("_sleepData.json")]
    records = 0
    dates: list[str] = []
    sample_keys: list[str] = []
    for name in files:
        data = _read_json(zf, name)
        rows: list[Any] = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    rows = value
                    break
        records += len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not sample_keys:
                sample_keys = list(row.keys())[:30]
            date_value = row.get("calendarDate")
            if date_value:
                dates.append(str(date_value)[:10])
    return {
        "files": len(files),
        "records": records,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "sample_keys": sample_keys,
    }


def _biometrics_stats(zf: zipfile.ZipFile) -> dict[str, Any]:
    name = next((n for n in zf.namelist() if n.endswith("userBioMetrics.json")), None)
    latest_name = next((n for n in zf.namelist() if n.endswith("bioMetrics_latest.json")), None)
    if not name:
        return {"records": 0}

    rows = _read_json(zf, name)
    if not isinstance(rows, list):
        rows = []

    fields = collections.Counter()
    dates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if value not in (None, ""):
                fields[key] += 1
        for key in ("calendarDate", "startDate", "updateDate", "measurementDate"):
            value = row.get(key)
            if value:
                dates.append(str(value)[:10])
                break

    latest = _read_json(zf, latest_name) if latest_name else None
    return {
        "records": len(rows),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "top_fields": dict(fields.most_common(30)),
        "latest": latest,
    }


def _top_level_stats(zf: zipfile.ZipFile) -> dict[str, Any]:
    infos = zf.infolist()
    top_dirs = collections.Counter(info.filename.split("/")[0] for info in infos if info.filename)
    exts = collections.Counter(
        (Path(info.filename).suffix.lower() or "[none]")
        for info in infos
        if not info.is_dir()
    )
    return {
        "files": len(infos),
        "uncompressed_bytes": sum(info.file_size for info in infos),
        "top_dirs": dict(top_dirs.most_common()),
        "extensions": dict(exts.most_common()),
    }


def audit_export(zip_path: str) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        activities = _load_summarized_activities(zf)
        return {
            "source_zip": str(zip_path),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "archive": _top_level_stats(zf),
            "activities": _activity_stats(activities),
            "nested_fits": _nested_fit_stats(zf),
            "gear": _gear_stats(zf),
            "sleep": _sleep_stats(zf),
            "biometrics": _biometrics_stats(zf),
            "recommendation": {
                "activity_index_source": "summarizedActivitiesExport",
                "fit_strategy": "classify FIT files before import; discard monitoring/stress/internal FITs as sessions",
                "safe_next_step": "import to staging tables, then deduplicate and compare with production",
            },
        }


def _format_table(rows: list[tuple[Any, ...]], headers: tuple[str, ...]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    act = report["activities"]
    gear = report["gear"]
    sleep = report["sleep"]
    nested = report["nested_fits"]
    cycling = act["sport_details"].get("cycling", {})

    sports = [(k, v) for k, v in act["by_sport"].items()]
    coverage = [(k, v, f"{(v / act['count'] * 100):.1f}%" if act["count"] else "0%") for k, v in act["coverage"].items()]
    cycling_cov = [
        (k, v, f"{(v / cycling.get('count', 1) * 100):.1f}%")
        for k, v in cycling.get("coverage", {}).items()
    ]

    gear_rows = [
        (
            row.get("name") or "",
            row.get("type") or "",
            row.get("status") or "",
            row.get("model") or "",
            row.get("maximum_km") or "",
        )
        for row in gear.get("gear", [])
    ]

    md = f"""# Garmin Export Audit

Generated: `{report['generated_at']}`

## Veredicto

Este ZIP sirve como fuente maestra para reconstruir la columna de datos, pero no debe importarse ciegamente. El indice confiable de actividades es `summarizedActivitiesExport`; los FIT internos deben clasificarse antes de tratarlos como sesiones.

## Archivo

- Archivos en el ZIP principal: **{report['archive']['files']}**
- Tamano sin comprimir: **{round(report['archive']['uncompressed_bytes'] / 1024 / 1024, 1)} MB**
- ZIPs internos de uploaded files: **{nested['nested_zip_count']}**
- FITs internos detectados: **{nested['fit_files_total']}**

## Actividades Reales Garmin

- Total resumidas: **{act['count']}**
- IDs unicos: **{act['unique_activity_ids']}**
- Rango: **{act['date_min']}** a **{act['date_max']}**

### Por Deporte

{_format_table(sports, ('Deporte', 'Actividades'))}

### Cobertura De Campos

{_format_table(coverage, ('Campo', 'Actividades', '%'))}

## Cycling

- Actividades: **{cycling.get('count', 0)}**
- Km totales aprox: **{cycling.get('km_total', 0)}**
- Actividad mas larga: **{cycling.get('km_max', 0)} km**
- Duracion maxima: **{cycling.get('duration_min_max', 0)} min**
- Menores a 2 min: **{cycling.get('short_lt_2_min', 0)}**
- Sin distancia: **{cycling.get('zero_distance', 0)}**

{_format_table(cycling_cov, ('Campo cycling', 'Actividades', '%'))}

## Gear

- Elementos gear: **{gear.get('gear_count', 0)}**
- Links actividad-gear en export: **{gear.get('activity_links', 0)}**
- Defaults por deporte: **{gear.get('default_links', 0)}**

{_format_table(gear_rows, ('Nombre', 'Tipo', 'Estado', 'Modelo', 'Max km'))}

## Sueno Y Wellness

- Archivos de sueno: **{sleep.get('files', 0)}**
- Registros de sueno aprox: **{sleep.get('records', 0)}**
- Rango sueno: **{sleep.get('date_min')}** a **{sleep.get('date_max')}**
- Registros biometricos: **{report['biometrics'].get('records', 0)}**

## Recomendacion Tecnica

1. Crear staging de Garmin export.
2. Importar primero actividades resumidas como indice maestro.
3. Clasificar FITs antes de importar detalle.
4. Descartar FITs de monitoring/stress como sesiones deportivas.
5. Cargar gear desde `gear.json`.
6. Cargar sleep/wellness desde `sleepData.json` y `userBioMetrics.json`.
7. Comparar staging contra produccion antes de reemplazar o limpiar datos.
"""
    path.write_text(md, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a Garmin account export ZIP.")
    parser.add_argument("zip_path", help="Path to the Garmin export ZIP")
    parser.add_argument("--out-dir", default="reports", help="Directory for audit output")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = audit_export(args.zip_path)
    json_path = out_dir / "garmin_export_audit.json"
    md_path = out_dir / "garmin_export_audit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Activities: {report['activities']['count']}")
    print(f"FIT files inside uploaded archives: {report['nested_fits']['fit_files_total']}")


if __name__ == "__main__":
    main()
