"""
mars_context.py — Mars athlete profile: zones, goals, helpers
Imported by main.py
"""
import json as _json
import os

MARS_PROFILE_DEFAULT = '{"zonas_ciclismo":{"lt_bpm":168,"max_hr":187,"z1":[0,108],"transition":[109,133],"z2":[134,150],"z3":[151,160],"z4":[161,168],"z5":[169,999]},"zonas_running":{"lt_bpm":173,"max_hr":194,"z1":[0,112],"z2":[112,138],"z3":[138,154],"z4":[154,164],"z5":[164,173]},"athlete":{"nombre":"Miguel Angel Mars","edad":47,"peso_actual_kg":89.1,"peso_objetivo_kg":80.0,"peso_meta_final_kg":70.0,"elevacion_m":2300},"bici":{"nombre":"Rarotonga","marca":"Orbea Avant Aluminio 2019","km":716.6,"primer_uso":"2026-04-27","llantas":"Vittoria Corsa N.EXT 700C x26"},"objetivos":[{"o":"Mejorar motor aerobico Z2","p":1},{"o":"Bajar peso a 80 kg","p":2},{"o":"Cadencia 100 rpm","p":3},{"o":"Eficiencia vel/FC 0.155+","p":4}],"plan_garmin":{"nombre":"Garmin Coach Time Trial","fase":"Base aerobica","desc":"Construccion motor aerobico Z2 con salidas largas y cadencia"},"rutas":[{"nombre":"Atizapan base","km":21,"desc":"Ruta base Z2 local"},{"nombre":"Salida larga","km":45,"desc":"Ruta larga entrenamiento"}],"nutricion":{"gel":"60% apple juice Tree Top + 40% agave Kirkland + pizca sal","carbos_g":40,"timing":"Cada 45-60 min durante sesion","agua_ml_h":500},"compex":{"fuerza":["Strength","Explosive Strength"],"recovery":["Active Recovery","Massage"],"dolor":["TENS"]},"cadencia_obj":100,"eff_base":0.1483,"eff_obj":0.155}'

def _ensure_profile_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS athlete_profile_full (
            id SERIAL PRIMARY KEY, profile_key TEXT UNIQUE NOT NULL DEFAULT 'mars',
            data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
    conn.commit()

def _get_profile(conn):
    import json as _j
    _ensure_profile_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM athlete_profile_full WHERE profile_key='mars'")
        row = cur.fetchone()
    return row[0] if row and isinstance(row[0], dict) else (_j.loads(row[0]) if row else _j.loads(MARS_PROFILE_DEFAULT))

def get_zone_label(hr: int, sport: str = "cycling") -> str:
    """Returns zone label for given heart rate using Mars zones."""
    if hr <= 108: return "Z1"
    if hr <= 133: return "Transicion"
    if hr <= 150: return "Z2"
    if hr <= 160: return "Z3"
    if hr <= 168: return "Z4"
    return "Z5"

def analyze_session_quick(session: dict) -> dict:
    """Quick analysis: zone, efficiency, cadence delta."""
    hr = float(session.get("avg_hr_bpm") or 0)
    spd = float(session.get("avg_speed_kmh") or 0)
    cad = float(session.get("avg_cadence") or 0)
    eff = round(spd/hr, 4) if hr > 0 and spd > 0 else None
    return {
        "zone": get_zone_label(int(hr)) if hr > 0 else "—",
        "efficiency": eff,
        "efficiency_delta": round(eff - 0.1483, 4) if eff else None,
        "cadence_delta": round(cad - 100, 1) if cad > 0 else None,
    }
