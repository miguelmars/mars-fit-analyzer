"""
decode_fit.py
=============
Decodifica archivos .fit (o .zip que contengan un .fit) de Garmin
y exporta:
  - CSV con todos los registros segundo a segundo
  - Resumen de sesión en consola
  - Distribución de zonas de FC (configurable)

USO:
  python decode_fit.py archivo.fit
  python decode_fit.py archivo.zip
  python decode_fit.py archivo.fit --lt 168
  python decode_fit.py archivo.fit --output mi_salida.csv

REQUISITOS:
  pip install fitparse

AUTOR: generado para Mars / @thecallehabla
"""

import sys
import os
import csv
import zipfile
import argparse
import tempfile
from datetime import datetime

try:
    import fitparse
except ImportError:
    print("ERROR: Falta la librería fitparse.")
    print("Instálala con:  pip install fitparse")
    sys.exit(1)


# ── Constantes de conversión ────────────────────────────────────────────────
SEMICIRCLES_TO_DEG = 180 / 2**31   # factor para coordenadas Garmin


# ── Funciones principales ────────────────────────────────────────────────────

def find_fit_in_zip(zip_path):
    """Extrae el primer .fit que encuentre dentro de un .zip."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        fit_files = [f for f in zf.namelist() if f.lower().endswith('.fit')]
        if not fit_files:
            print("ERROR: El ZIP no contiene ningún archivo .fit")
            sys.exit(1)
        target = fit_files[0]
        print(f"  → Encontrado dentro del ZIP: {target}")
        tmp = tempfile.NamedTemporaryFile(suffix='.fit', delete=False)
        tmp.write(zf.read(target))
        tmp.close()
        return tmp.name


def load_fit(path):
    """Carga el FitFile desde .fit o .zip."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.zip':
        print(f"Descomprimiendo {path}...")
        path = find_fit_in_zip(path)
    elif ext != '.fit':
        print(f"ADVERTENCIA: extensión '{ext}' no reconocida, intentando leer como .fit")
    return fitparse.FitFile(path)


def extract_records(fit):
    """Extrae los registros segundo a segundo."""
    rows = []
    for msg in fit.get_messages('record'):
        rec = {d.name: d.value for d in msg if d.value is not None}
        lat = rec.get('position_lat')
        lon = rec.get('position_long')
        speed = rec.get('speed', rec.get('enhanced_speed', 0)) or 0
        rows.append({
            'timestamp':      rec.get('timestamp', ''),
            'heart_rate_bpm': rec.get('heart_rate', ''),
            'speed_kmh':      round(speed * 3.6, 2),
            'cadence_rpm':    rec.get('cadence', ''),
            'altitude_m':     rec.get('enhanced_altitude', rec.get('altitude', '')),
            'distance_m':     round(rec.get('distance', 0), 1),
            'temperature_c':  rec.get('temperature', ''),
            'lat':            round(lat * SEMICIRCLES_TO_DEG, 6) if lat else '',
            'lon':            round(lon * SEMICIRCLES_TO_DEG, 6) if lon else '',
        })
    return rows


def extract_session(fit):
    """Extrae el resumen de la sesión."""
    for msg in fit.get_messages('session'):
        return {d.name: d.value for d in msg if d.value is not None}
    return {}


def extract_laps(fit):
    """Extrae los laps."""
    laps = []
    for msg in fit.get_messages('lap'):
        laps.append({d.name: d.value for d in msg if d.value is not None})
    return laps


def hr_zones(records, lt_bpm):
    """
    Calcula distribución de zonas de FC basadas en % del umbral de lactato.
    Zonas Garmin estilo % LT:
      Z1 < 68%  Z2 68-83%  Z3 83-94%  Z4 94-105%  Z5 > 105%
    """
    bounds = [
        (1, 0,   68,  'Recuperación activa'),
        (2, 68,  83,  'Resistencia aeróbica'),
        (3, 83,  94,  'Tempo / Umbral bajo'),
        (4, 94,  105, 'Umbral / VO2max'),
        (5, 105, 999, 'Anaeróbico / Sprint'),
    ]
    counts = {z: 0 for z, *_ in bounds}
    for r in records:
        hr = r['heart_rate_bpm']
        if hr == '':
            continue
        pct = hr / lt_bpm * 100
        for z, lo, hi, _ in bounds:
            if lo <= pct < hi:
                counts[z] += 1
                break
    return counts, bounds


def print_summary(session, laps, records, lt_bpm):
    """Imprime el resumen en consola."""
    SEP = "─" * 55

    print(f"\n{'═'*55}")
    print("  RESUMEN DE ACTIVIDAD GARMIN")
    print(f"{'═'*55}")

    st = session.get('start_time', '')
    et = session.get('total_elapsed_time', 0)
    h, m, s = int(et // 3600), int((et % 3600) // 60), int(et % 60)
    print(f"  Fecha inicio : {st}")
    print(f"  Duración     : {h:02d}h {m:02d}m {s:02d}s")
    print(f"  Distancia    : {session.get('total_distance', 0)/1000:.2f} km")
    print(f"  Calorías     : {session.get('total_calories', '?')} kcal")
    print(f"  Ascenso/Desc : +{session.get('total_ascent','?')} m / -{session.get('total_descent','?')} m")
    print(f"  Deporte      : {session.get('sport','?')} / {session.get('sub_sport','?')}")

    print(f"\n  FRECUENCIA CARDÍACA")
    print(f"  Promedio : {session.get('avg_heart_rate','?')} bpm")
    print(f"  Máxima   : {session.get('max_heart_rate','?')} bpm")

    avg_spd = session.get('avg_speed', 0)
    max_spd = session.get('max_speed', 0)
    print(f"\n  VELOCIDAD")
    print(f"  Promedio : {avg_spd*3.6:.1f} km/h")
    print(f"  Máxima   : {max_spd*3.6:.1f} km/h")

    print(f"\n  CADENCIA")
    print(f"  Promedio : {session.get('avg_cadence','?')} rpm")
    print(f"  Máxima   : {session.get('max_cadence','?')} rpm")

    print(f"\n  TEMPERATURA")
    print(f"  Promedio : {session.get('avg_temperature','?')} °C")
    print(f"  Máxima   : {session.get('max_temperature','?')} °C")

    te = session.get('total_training_effect', '?')
    ate = session.get('total_anaerobic_training_effect', '?')
    print(f"\n  TRAINING EFFECT  Aeróbico: {te}  /  Anaeróbico: {ate}")

    # Laps
    print(f"\n  LAPS ({len(laps)})")
    print(f"  {'#':>3}  {'Tiempo':>8}  {'Dist km':>7}  {'FC avg':>6}  {'FC max':>6}  {'Vel km/h':>8}  {'Cal':>5}")
    print(f"  {SEP}")
    for i, lap in enumerate(laps, 1):
        t  = lap.get('total_elapsed_time', 0)
        mm, ss = int(t // 60), int(t % 60)
        dist = lap.get('total_distance', 0) / 1000
        fca  = lap.get('avg_heart_rate', '-')
        fcm  = lap.get('max_heart_rate', '-')
        vel  = (lap.get('avg_speed', 0) or 0) * 3.6
        cal  = lap.get('total_calories', '-')
        print(f"  {i:>3}  {mm:>4}m{ss:02d}s  {dist:>7.2f}  {str(fca):>6}  {str(fcm):>6}  {vel:>8.1f}  {str(cal):>5}")

    # Zonas FC
    print(f"\n  ZONAS DE FC  (LT = {lt_bpm} bpm)")
    counts, bounds = hr_zones(records, lt_bpm)
    total = sum(counts.values()) or 1
    print(f"  {'Zona':<6} {'Rango %LT':<14} {'bpm':<12} {'Tiempo':>8}  {'%':>5}")
    print(f"  {SEP}")
    for z, lo, hi, nombre in bounds:
        secs  = counts[z]
        mm, ss = secs // 60, secs % 60
        blo   = int(lt_bpm * lo / 100)
        bhi   = int(lt_bpm * hi / 100) if hi < 999 else '∞'
        pct   = secs / total * 100
        bar   = '█' * int(pct / 5)
        print(f"  Z{z}     {lo:>3}-{hi if hi<999 else '∞':>3}%     {blo:>3}-{bhi!s:<6}  {mm:>4}m{ss:02d}s  {pct:>5.1f}%  {bar}")

    print(f"{'═'*55}\n")


def save_csv(records, output_path):
    """Guarda los registros en CSV."""
    if not records:
        print("ADVERTENCIA: no hay registros para guardar.")
        return
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"  CSV guardado: {output_path}  ({len(records)} registros)")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Decodifica archivos .fit de Garmin y exporta CSV + resumen.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python decode_fit.py actividad.fit
  python decode_fit.py actividad.zip --lt 168
  python decode_fit.py actividad.fit --lt 168 --output entrenamiento.csv
        """
    )
    parser.add_argument('input',  help='Archivo .fit o .zip a procesar')
    parser.add_argument('--lt',   type=int, default=168,
                        help='Umbral de lactato en bpm (default: 168)')
    parser.add_argument('--output', default=None,
                        help='Nombre del CSV de salida (default: mismo nombre que el input)')
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: No se encontró el archivo '{args.input}'")
        sys.exit(1)

    # Nombre de salida
    if args.output:
        csv_path = args.output
    else:
        base = os.path.splitext(os.path.basename(args.input))[0]
        csv_path = base + '.csv'

    print(f"\nProcesando: {args.input}")
    fit     = load_fit(args.input)
    records = extract_records(fit)
    session = extract_session(fit)
    laps    = extract_laps(fit)

    print_summary(session, laps, records, args.lt)
    save_csv(records, csv_path)


if __name__ == '__main__':
    main()
