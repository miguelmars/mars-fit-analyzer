"""
FIT Analyzer API — Mars Edition
================================
Endpoint POST /analyze-fit
Recibe un .zip o .fit de Garmin y devuelve JSON compacto para GPT:
- resumen de sesión
- laps
- zonas oficiales Mars
- métricas de FC/cadencia/velocidad
- datos suficientes para análisis sin saturar la Action
"""

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile, os, zipfile, math, statistics
from typing import Optional

try:
    import fitparse
except ImportError:
    raise RuntimeError("Instala fitparse: pip install fitparse")

app = FastAPI(title="FIT Analyzer API — Mars Edition", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SEMICIRCLES_TO_DEG = 180 / 2**31

# ZONAS OFICIALES MARS — NO MODIFICAR SIN CONFIRMACIÓN
MARS_ZONES = [
    {"zone": 1, "name": "Z1 Recuperación", "bpm_low": 0,   "bpm_high": 108},
    {"zone": 2, "name": "Z2 Aeróbico",     "bpm_low": 134, "bpm_high": 150},
    {"zone": 3, "name": "Z3 Tempo",        "bpm_low": 151, "bpm_high": 160},
    {"zone": 4, "name": "Z4 Umbral",       "bpm_low": 161, "bpm_high": 168},
    {"zone": 5, "name": "Z5 Máximo",       "bpm_low": 169, "bpm_high": 999},
]
# Nota: bpm 109-133 queda como "entre Z1 y Z2" porque las zonas oficiales de Mars tienen hueco.


def extract_fit_from_zip(zip_bytes: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
        zpath = f.name
    try:
        with zipfile.ZipFile(zpath) as zf:
            fits = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise HTTPException(400, "El ZIP no contiene ningún .fit")
            return zf.read(fits[0])
    finally:
        os.unlink(zpath)


def percentile(values, p):
    values = sorted([v for v in values if v is not None])
    if not values:
        return None
    k = (len(values)-1) * (p/100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c-k) + values[c] * (k-f)


def zone_for_hr(hr):
    if hr is None:
        return None
    if 109 <= hr <= 133:
        return 0  # gap oficial entre Z1 y Z2
    for z in MARS_ZONES:
        if z["bpm_low"] <= hr <= z["bpm_high"]:
            return z["zone"]
    return None


def summarize_records(records):
    hrs = [r["heart_rate_bpm"] for r in records if r.get("heart_rate_bpm") is not None]
    cads = [r["cadence_rpm"] for r in records if r.get("cadence_rpm") is not None]
    speeds = [r["speed_kmh"] for r in records if r.get("speed_kmh") is not None]
    alts = [r["altitude_m"] for r in records if r.get("altitude_m") is not None]

    return {
        "records_count": len(records),
        "hr": {
            "min": min(hrs) if hrs else None,
            "max": max(hrs) if hrs else None,
            "avg": round(statistics.mean(hrs), 1) if hrs else None,
            "p90": round(percentile(hrs, 90), 1) if hrs else None,
        },
        "cadence": {
            "min": min(cads) if cads else None,
            "max": max(cads) if cads else None,
            "avg": round(statistics.mean(cads), 1) if cads else None,
            "p90": round(percentile(cads, 90), 1) if cads else None,
        },
        "speed": {
            "min_kmh": round(min(speeds), 1) if speeds else None,
            "max_kmh": round(max(speeds), 1) if speeds else None,
            "avg_kmh": round(statistics.mean(speeds), 1) if speeds else None,
        },
        "altitude": {
            "min_m": round(min(alts), 1) if alts else None,
            "max_m": round(max(alts), 1) if alts else None,
        }
    }


def compute_zones(records):
    counts = {z["zone"]: 0 for z in MARS_ZONES}
    gap_count = 0
    no_hr_count = 0

    for rec in records:
        hr = rec.get("heart_rate_bpm")
        if hr is None:
            no_hr_count += 1
            continue
        z = zone_for_hr(hr)
        if z == 0:
            gap_count += 1
        elif z is not None:
            counts[z] += 1

    total_with_hr = sum(counts.values()) + gap_count or 1
    zones = []
    for z in MARS_ZONES:
        secs = counts[z["zone"]]
        zones.append({
            "zone": z["zone"],
            "name": z["name"],
            "bpm_low": z["bpm_low"],
            "bpm_high": None if z["bpm_high"] == 999 else z["bpm_high"],
            "seconds": secs,
            "minutes": round(secs / 60, 1),
            "percent": round(secs / total_with_hr * 100, 1),
        })
    zones.append({
        "zone": 0,
        "name": "Entre Z1 y Z2 oficial",
        "bpm_low": 109,
        "bpm_high": 133,
        "seconds": gap_count,
        "minutes": round(gap_count / 60, 1),
        "percent": round(gap_count / total_with_hr * 100, 1),
    })
    return zones


def parse_fit(fit_bytes: bytes, include_records: bool = False) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as f:
        f.write(fit_bytes)
        fpath = f.name

    try:
        fit = fitparse.FitFile(fpath)

        session_raw = {}
        for msg in fit.get_messages("session"):
            for d in msg:
                if d.value is not None:
                    session_raw[d.name] = d.value

        et = session_raw.get("total_elapsed_time", 0) or 0
        avg_spd = session_raw.get("avg_speed", 0) or 0
        max_spd = session_raw.get("max_speed", 0) or 0

        session = {
            "start_time": str(session_raw.get("start_time", "")),
            "duration_seconds": round(et),
            "duration_hms": f"{int(et//3600):02d}h {int((et%3600)//60):02d}m {int(et%60):02d}s",
            "distance_km": round((session_raw.get("total_distance", 0) or 0) / 1000, 2),
            "calories_kcal": session_raw.get("total_calories"),
            "ascent_m": session_raw.get("total_ascent"),
            "descent_m": session_raw.get("total_descent"),
            "avg_hr_bpm": session_raw.get("avg_heart_rate"),
            "max_hr_bpm": session_raw.get("max_heart_rate"),
            "avg_speed_kmh": round(avg_spd * 3.6, 1),
            "max_speed_kmh": round(max_spd * 3.6, 1),
            "avg_cadence_rpm": session_raw.get("avg_cadence"),
            "max_cadence_rpm": session_raw.get("max_cadence"),
            "avg_temperature_c": session_raw.get("avg_temperature"),
            "max_temperature_c": session_raw.get("max_temperature"),
            "training_effect_aerobic": session_raw.get("total_training_effect"),
            "training_effect_anaerobic": session_raw.get("total_anaerobic_training_effect"),
            "sport": str(session_raw.get("sport", "")),
            "sub_sport": str(session_raw.get("sub_sport", "")),
        }

        laps = []
        for i, msg in enumerate(fit.get_messages("lap"), 1):
            r = {d.name: d.value for d in msg if d.value is not None}
            t = r.get("total_elapsed_time", 0) or 0
            sp = r.get("avg_speed", 0) or 0
            laps.append({
                "lap": i,
                "duration_s": round(t),
                "duration_mmss": f"{int(t//60)}m{int(t%60):02d}s",
                "distance_km": round((r.get("total_distance", 0) or 0) / 1000, 2),
                "avg_hr_bpm": r.get("avg_heart_rate"),
                "max_hr_bpm": r.get("max_heart_rate"),
                "avg_speed_kmh": round(sp * 3.6, 1),
                "avg_cadence_rpm": r.get("avg_cadence"),
                "calories_kcal": r.get("total_calories"),
            })

        records = []
        for msg in fit.get_messages("record"):
            rec = {d.name: d.value for d in msg if d.value is not None}
            lat = rec.get("position_lat")
            lon = rec.get("position_long")
            spd = rec.get("speed", rec.get("enhanced_speed", 0)) or 0
            records.append({
                "timestamp": str(rec.get("timestamp", "")),
                "heart_rate_bpm": rec.get("heart_rate"),
                "speed_kmh": round(spd * 3.6, 2),
                "cadence_rpm": rec.get("cadence"),
                "altitude_m": rec.get("enhanced_altitude", rec.get("altitude")),
                "distance_m": round(rec.get("distance", 0), 1),
                "temperature_c": rec.get("temperature"),
                "lat": round(lat * SEMICIRCLES_TO_DEG, 6) if lat else None,
                "lon": round(lon * SEMICIRCLES_TO_DEG, 6) if lon else None,
            })

        result = {
            "athlete": "Mars / Miguel Ángel Ramírez Sousa",
            "zone_model": "Zonas oficiales Mars por bpm",
            "zones_definition": MARS_ZONES,
            "session": session,
            "laps": laps,
            "zones": compute_zones(records),
            "record_summary": summarize_records(records),
            "analysis_guidance": {
                "use_as_primary_data": True,
                "do_not_invent": True,
                "notes": [
                    "Usar estos datos para analizar la sesión.",
                    "No asumir causa de picos de FC sin contexto del usuario.",
                    "Pedir al usuario sensación, ruta, tráfico y nutrición después del análisis."
                ]
            }
        }
        if include_records:
            # Ojo: puede ser pesado para GPT. Úsalo sólo para depuración o análisis externo.
            result["records"] = records
        return result
    finally:
        os.unlink(fpath)


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "FIT Analyzer API — Mars Edition",
        "version": "2.0",
        "zones": "Mars official HR zones"
    }


@app.post("/analyze-fit")
async def analyze_fit(
    file: UploadFile = File(...),
    include_records: bool = Form(False),
):
    """
    Recibe un archivo .fit o .zip de Garmin.
    Devuelve JSON compacto con session, laps, zonas Mars y resumen de registros.
    """
    content = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".zip"):
        fit_bytes = extract_fit_from_zip(content)
    elif filename.endswith(".fit"):
        fit_bytes = content
    else:
        fit_bytes = content

    return parse_fit(fit_bytes, include_records=include_records)
