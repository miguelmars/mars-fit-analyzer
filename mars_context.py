"""
mars_context.py — Mars zones, profile, helpers
"""
import json as _json

MARS_ZONES = {
    "cycling": {
        "lt_bpm": 168, "max_hr": 187,
        "z1": [0, 108], "transition": [109, 133],
        "z2": [134, 150], "z3": [151, 160],
        "z4": [161, 168], "z5": [169, 999]
    },
    "running": {
        "lt_bpm": 173, "max_hr": 194,
        "z1": [0, 112], "transition": [113, 137],
        "z2": [138, 154], "z3": [155, 164],
        "z4": [165, 173], "z5": [174, 999]
    }
}

MARS_PROFILE_DEFAULT = '{"zonas_ciclismo":{"lt_bpm":168,"max_hr":187,"z1":[0,108],"transition":[109,133],"z2":[134,150],"z3":[151,160],"z4":[161,168],"z5":[169,999]},"zonas_running":{"lt_bpm":173,"max_hr":194,"z1":[0,112],"transition":[113,137],"z2":[138,154],"z3":[155,164],"z4":[165,173],"z5":[174,999]},"athlete":{"nombre":"Miguel Angel Mars","edad":47,"peso_actual_kg":89.1,"peso_objetivo_kg":80.0,"peso_meta_final_kg":70.0,"elevacion_m":2300},"bici":{"nombre":"Rarotonga","marca":"Orbea Avant Aluminio 2019","km":716.6,"primer_uso":"2026-04-27","llantas":"Vittoria Corsa N.EXT 700C x26"},"objetivos":[{"o":"Improve Z2 aerobic engine","p":1},{"o":"Lower weight to 80 kg","p":2},{"o":"Cadence 100 rpm","p":3},{"o":"Speed/HR efficiency 0.155+","p":4}],"plan_garmin":{"nombre":"Garmin Coach Time Trial","fase":"Aerobic base","desc":"Building the Z2 aerobic engine with long rides and cadence"},"rutas":[{"nombre":"Atizapan base","km":21,"desc":"Local Z2 base route"},{"nombre":"Salida larga","km":45,"desc":"Long training route"}],"nutricion":{"gel":"60% apple juice Tree Top + 40% agave Kirkland + pizca sal","carbos_g":40,"timing":"Every 45-60 min during the session","agua_ml_h":500},"compex":{"fuerza":["Strength","Explosive Strength"],"recovery":["Active Recovery","Massage"],"dolor":["TENS"]},"cadencia_obj":100,"eff_base":0.1483,"eff_obj":0.155}'

def _ensure_profile_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS athlete_profile_full (
            id SERIAL PRIMARY KEY, profile_key TEXT UNIQUE NOT NULL DEFAULT 'mars',
            data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _get_profile(conn):
    _ensure_profile_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM athlete_profile_full WHERE profile_key='mars'")
        row = cur.fetchone()
    return row[0] if row and isinstance(row[0], dict) else (_json.loads(row[0]) if row else _json.loads(MARS_PROFILE_DEFAULT))

def get_zone_label(hr: int, sport: str = "cycling") -> str:
    key = "cycling" if sport in ("cycling","ciclismo","bike","cycling") else "running"
    z = MARS_ZONES[key]
    if hr <= z["z1"][1]: return "Z1 Recovery"
    if hr <= z["transition"][1]: return "Transition"
    if hr <= z["z2"][1]: return "Z2 Aerobic"
    if hr <= z["z3"][1]: return "Z3 Tempo"
    if hr <= z["z4"][1]: return "Z4 Threshold"
    return "Z5 Maximum"

def analyze_session_quick(session: dict, sport: str = "cycling") -> dict:
    hr = float(session.get("avg_hr_bpm") or 0)
    spd = float(session.get("avg_speed_kmh") or 0)
    cad = float(session.get("avg_cadence") or session.get("avg_cadence_rpm") or 0)
    eff = round(spd / hr, 4) if hr > 0 and spd > 0 else None
    return {
        "zone": get_zone_label(int(hr), sport) if hr > 0 else "—",
        "efficiency": eff,
        "efficiency_delta": round(eff - 0.1483, 4) if eff else None,
        "cadence": cad,
        "cadence_delta": round(cad - 100, 1) if cad > 0 else None,
        "cadence_ok": cad >= 100 if cad > 0 else None,
    }
