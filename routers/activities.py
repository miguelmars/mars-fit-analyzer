"""
routers/activities.py — Endpoints de sesiones, rutas y estadísticas
====================================================================
TD-010A: extraído de main.py (Step 6).

Contiene 19 endpoints:
  GET  /routes
  GET  /route/{route_id}
  GET  /route/{route_id}/matched   (HTML + JSON)
  GET  /sessions
  GET  /charts/{session_id}        (HTML)
  GET  /stats/yearly
  GET  /stats/records
  GET  /stats/monthly
  GET  /stats/efficiency
  GET  /sessions/recent
  GET  /gpt/latest-session
  GET  /gpt/session/{session_id}
  GET  /gpt/route-progress/{route_id}
  GET  /gpt/route-compare/{route_id}
  GET  /gpt/session-environment/{session_id}
  GET  /gpt/session-analysis/{session_id}
  GET  /gpt/matched-rides/{session_id}
  GET  /gpt/route-history
  DELETE /sessions/{session_id}
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from db import get_db
from shared.sql_helpers import (
    _telemetry_exists_sql,
    _telemetry_points_sql,
    _telemetry_map_sql,
    _sport_filter_sql,
    _enrich_session_dict,
    _load_old_telemetry_index,
    _telemetry_match_for_session,
    _extract_calories_from_payload,
)
from shared.results_store import RESULTS_STORE
from mars_context import _get_profile

logger = logging.getLogger("mars_fit")

router = APIRouter(tags=["activities"])


# ── GET /routes ───────────────────────────────────────────────────────────────

@router.get("/routes")
def list_routes():
    conn = get_db()
    if not conn:
        return {"error": "Base de datos no conectada. Configura DATABASE_URL."}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.route_id, r.name, r.distance_km, r.ascent_m,
                       COUNT(s.session_id) as times,
                       MIN(s.start_time) as first_ride,
                       MAX(s.start_time) as last_ride,
                       AVG(s.avg_hr_bpm) as avg_hr,
                       AVG(s.avg_speed_kmh) as avg_spd,
                       MODE() WITHIN GROUP (ORDER BY s.sport) as sport
                FROM routes r
                LEFT JOIN sessions_clean_compat s ON s.route_id = r.route_id
                GROUP BY r.route_id, r.name, r.distance_km, r.ascent_m
                ORDER BY times DESC
            """)
            rows = cur.fetchall()
            return [{"route_id":r[0],"name":r[1],"distance_km":r[2],"ascent_m":r[3],
                     "times_ridden":r[4],"first_ride":str(r[5])[:10] if r[5] else None,
                     "last_ride":str(r[6])[:10] if r[6] else None,
                     "avg_hr_bpm":round(r[7],1) if r[7] else None,
                     "avg_speed_kmh":round(r[8],1) if r[8] else None,
                     "sport":r[9]}
                    for r in rows]
    except Exception as e:
        return {"error": str(e)}


# ── GET /route/{route_id} ─────────────────────────────────────────────────────

@router.get("/route/{route_id}")
def get_route(route_id: str):
    conn = get_db()
    if not conn:
        return {"error": "Base de datos no conectada."}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, distance_km, ascent_m FROM routes WHERE route_id=%s", (route_id,))
            route = cur.fetchone()
            if not route:
                raise HTTPException(404, "Ruta no encontrada")
            cur.execute("""
                SELECT session_id, start_time, avg_hr_bpm, avg_speed_kmh,
                       avg_cadence, duration_s, workout_name
                FROM sessions_clean_compat WHERE route_id=%s
                ORDER BY start_time ASC
            """, (route_id,))
            rides = cur.fetchall()

        sessions_list = [{"session_id":r[0],"date":str(r[1])[:10] if r[1] else None,
                          "avg_hr_bpm":r[2],"avg_speed_kmh":r[3],
                          "avg_cadence":r[4],"duration_s":r[5],"workout_name":r[6]}
                         for r in rides]

        # Compute progress
        progress = {}
        if len(sessions_list) >= 2:
            first = sessions_list[0]; last = sessions_list[-1]
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                hr_delta = last["avg_hr_bpm"] - first["avg_hr_bpm"]
                progress["hr_change_bpm"] = hr_delta
                progress["hr_note"] = (
                    f"FC bajó {abs(hr_delta)} bpm → mejora aeróbica real" if hr_delta < -3
                    else f"FC subió {hr_delta} bpm" if hr_delta > 3
                    else "FC estable entre primera y última ejecución"
                )
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                spd_delta = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 1)
                progress["speed_change_kmh"] = spd_delta
                progress["speed_note"] = (
                    f"Velocidad aumentó {spd_delta} km/h en {len(sessions_list)} ejecuciones"
                    if spd_delta > 0.5 else "Velocidad estable"
                )

        return {"route_id":route_id,"name":route[0],"distance_km":route[1],
                "ascent_m":route[2],"times_ridden":len(sessions_list),
                "rides":sessions_list,"progress":progress}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


# ── HTML helper para /route/{route_id}/matched ────────────────────────────────

def _route_matched_html(route_id: str):
    rid = json.dumps(route_id)
    return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Comparativa de ruta</title>
  <style>
    body{{margin:0;background:#08090b;color:#f7f7f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
    main{{max-width:980px;margin:0 auto;padding:18px 14px 90px}}
    a,button{{color:#e8593c;text-decoration:none;font-weight:850}}
    button{{background:#15171c;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:9px 12px;cursor:pointer}}
    .top{{display:flex;align-items:center;gap:10px;justify-content:space-between;margin-bottom:14px}}
    .card{{background:#15171c;border:1px solid rgba(255,255,255,.09);border-radius:16px;padding:16px;margin:12px 0}}
    .muted{{color:#8e95a3}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
    .metric{{--mc:#8e95a3;background:linear-gradient(180deg,color-mix(in srgb,var(--mc) 10%,#1c1f26),#1c1f26);border:1px solid color-mix(in srgb,var(--mc) 24%,rgba(255,255,255,.09));border-radius:12px;padding:13px;position:relative;overflow:hidden}}
    .metric:before{{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--mc)}}
    .metric.speed{{--mc:#3dd68c}} .metric.time{{--mc:#4a9eff}} .metric.hr{{--mc:#e8593c}} .metric.efficiency{{--mc:#a78bfa}} .metric.overall{{--mc:#f5c542}}
    .metric .value{{color:var(--mc)}}
    .label{{font-size:11px;color:#8e95a3;font-weight:850;text-transform:uppercase;letter-spacing:.06em}}
    .value{{font-size:22px;font-weight:950;margin-top:4px}}
    table{{width:100%;border-collapse:collapse;font-size:13px}}
    th{{text-align:left;color:#8e95a3;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:10px 8px;border-bottom:1px solid rgba(255,255,255,.1)}}
    td{{padding:11px 8px;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap}}
    td:first-child,th:first-child{{white-space:normal}}
    .right{{text-align:right}}
    .good{{color:#3dd68c}} .warn{{color:#f59e0b}}
    .badge{{--bc:#f59e0b;display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:3px 7px;font-size:10px;font-weight:950;margin-left:7px;vertical-align:middle;background:color-mix(in srgb,var(--bc) 14%,transparent);color:var(--bc);border:1px solid color-mix(in srgb,var(--bc) 32%,transparent)}}
    .badge-dot{{width:6px;height:6px;border-radius:50%;background:var(--bc);box-shadow:0 0 8px color-mix(in srgb,var(--bc) 65%,transparent)}}
    .badge.speed{{--bc:#3dd68c}} .badge.time{{--bc:#4a9eff}} .badge.hr{{--bc:#e8593c}} .badge.efficiency{{--bc:#a78bfa}} .badge.overall{{--bc:#f5c542}}
    .mark{{--mc:#f59e0b;display:block;margin-top:3px;font-size:10px;font-weight:900;color:var(--mc)}}
    .mark.speed{{--mc:#3dd68c}} .mark.time{{--mc:#4a9eff}} .mark.hr{{--mc:#e8593c}} .mark.efficiency{{--mc:#a78bfa}} .mark.overall{{--mc:#f5c542}}
    tr.pr-row{{background:linear-gradient(90deg,rgba(255,255,255,.035),transparent 48%)}}
    tr.overall-row{{background:linear-gradient(90deg,rgba(245,197,66,.13),transparent 50%);box-shadow:inset 3px 0 0 #f5c542}}
    .scroll{{overflow-x:auto}}
  </style>
</head>
<body>
<main>
  <div class="top">
    <button onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
    <a href="/activities">Actividades</a>
  </div>
  <section class="card">
    <div class="label">Matched rides</div>
    <h1 id="title">Cargando comparativa...</h1>
    <p id="note" class="muted"></p>
  </section>
  <section class="grid" id="leaders"></section>
  <section class="card">
    <div class="scroll">
      <table>
        <thead><tr><th>Fecha</th><th class="right">Vel.</th><th class="right">Tiempo</th><th class="right">FC</th><th class="right">Cad.</th><th class="right">Efic.</th></tr></thead>
        <tbody id="rows"><tr><td colspan="6" class="muted">Cargando...</td></tr></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const rid = {rid};
const hms = s => {{
  s = Number(s || 0);
  return String(Math.floor(s/3600)).padStart(2,'0') + ':' + String(Math.floor((s%3600)/60)).padStart(2,'0');
}};
const fmtDate = v => {{
  if(!v) return '—';
  try {{
    const d = new Date(String(v).slice(0,10) + 'T12:00:00');
    const months = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear();
  }} catch(e) {{
    return String(v);
  }}
}};
const metric = (k,v,u='',cls='',extra='') => `<div class="metric ${{cls}}"><div class="label">${{k}}${{extra}}</div><div class="value">${{v}}</div><div class="muted">${{u}}</div></div>`;
const badge = (txt, cls='') => `<span class="badge ${{cls}}"><span class="badge-dot"></span>${{txt}}</span>`;
fetch('/route/' + encodeURIComponent(rid) + '/matched?format=json').then(async r => {{
  if(!r.ok) throw new Error(await r.text());
  return r.json();
}}).then(d => {{
  const route = d.route || {{}};
  document.getElementById('title').textContent = route.name || 'Ruta Garmin';
  document.getElementById('note').textContent = d.message || ((route.distance_km || '—') + ' km · ' + (d.counts?.unique_efforts || 0) + ' esfuerzos comparables' + (d.counts?.duplicates_hidden ? ' · ' + d.counts.duplicates_hidden + ' duplicados ocultos' : ''));
  const l = d.leaders || {{}};
  const efforts = d.efforts || [];
  const leaderIds = {{
    speed: l.best_speed?.session_id,
    time: l.best_time?.session_id,
    hr: l.lowest_hr?.session_id,
    efficiency: l.best_efficiency?.session_id
  }};
  const rankScore = (arr, id, key, dir='desc') => {{
    const sorted = arr.filter(x => x[key] !== null && x[key] !== undefined && Number(x[key]) > 0)
      .slice()
      .sort((a,b) => dir === 'asc' ? Number(a[key]) - Number(b[key]) : Number(b[key]) - Number(a[key]));
    const idx = sorted.findIndex(x => x.session_id === id);
    return idx >= 0 ? idx + 1 : sorted.length + 1;
  }};
  const scored = efforts.map(e => ({{
    id:e.session_id,
    score:
      rankScore(efforts,e.session_id,'avg_speed_kmh','desc') +
      rankScore(efforts,e.session_id,'duration_s','asc') +
      rankScore(efforts,e.session_id,'avg_hr_bpm','asc') +
      rankScore(efforts,e.session_id,'efficiency','desc')
  }})).sort((a,b)=>a.score-b.score);
  leaderIds.overall = scored[0]?.id;
  const cardClass = id => id && id === leaderIds.overall ? 'overall' : '';
  const leaderBadge = id => id && id === leaderIds.overall ? badge('MEJOR','overall') : '';
  document.getElementById('leaders').innerHTML = [
    metric('Mejor velocidad', l.best_speed?.avg_speed_kmh ? l.best_speed.avg_speed_kmh + ' km/h' : '—', fmtDate(l.best_speed?.date), cardClass(l.best_speed?.session_id) || 'speed', leaderBadge(l.best_speed?.session_id)),
    metric('Mejor tiempo', l.best_time?.duration_s ? hms(l.best_time.duration_s) : '—', fmtDate(l.best_time?.date), cardClass(l.best_time?.session_id) || 'time', leaderBadge(l.best_time?.session_id)),
    metric('FC mas baja', l.lowest_hr?.avg_hr_bpm ? l.lowest_hr.avg_hr_bpm + ' bpm' : '—', fmtDate(l.lowest_hr?.date), cardClass(l.lowest_hr?.session_id) || 'hr', leaderBadge(l.lowest_hr?.session_id)),
    metric('Mejor eficiencia', l.best_efficiency?.efficiency || '—', fmtDate(l.best_efficiency?.date), cardClass(l.best_efficiency?.session_id) || 'efficiency', leaderBadge(l.best_efficiency?.session_id))
  ].join('');
  const marksFor = e => {{
    const marks = [];
    if(e.session_id === leaderIds.overall) marks.push('mejor general');
    if(e.session_id === leaderIds.speed) marks.push('velocidad');
    if(e.session_id === leaderIds.time) marks.push('tiempo');
    if(e.session_id === leaderIds.hr) marks.push('FC');
    if(e.session_id === leaderIds.efficiency) marks.push('eficiencia');
    return marks;
  }};
  const badgesFor = e => {{
    const out = [];
    if(e.session_id === leaderIds.overall) out.push(badge('MEJOR','overall'));
    if(e.session_id === leaderIds.speed) out.push(badge('VEL','speed'));
    if(e.session_id === leaderIds.time) out.push(badge('TIEMPO','time'));
    if(e.session_id === leaderIds.hr) out.push(badge('FC','hr'));
    if(e.session_id === leaderIds.efficiency) out.push(badge('EFIC','efficiency'));
    return out.join('');
  }};
  const mark = (e, key, label) => e.session_id === leaderIds[key] ? `<span class="mark ${{key}}">${{label}}</span>` : '';
  document.getElementById('rows').innerHTML = efforts.length ? efforts.map(e => `
    <tr class="${{e.session_id===leaderIds.overall ? 'overall-row' : (marksFor(e).length ? 'pr-row' : '')}}" onclick="location.href='/session/${{e.session_id}}'" style="cursor:pointer">
      <td><strong>${{fmtDate(e.date)}}</strong>${{badgesFor(e)}}<div class="muted">${{e.workout_name || ''}}</div><div class="muted">${{marksFor(e).join(' · ')}}</div></td>
      <td class="right good">${{e.avg_speed_kmh || '—'}} km/h${{mark(e,'speed','mejor')}}</td>
      <td class="right">${{e.duration_hms || hms(e.duration_s)}}${{mark(e,'time','mejor')}}</td>
      <td class="right">${{e.avg_hr_bpm || '—'}}${{mark(e,'hr','mas baja')}}</td>
      <td class="right">${{e.avg_cadence || '—'}}</td>
      <td class="right warn">${{e.efficiency || '—'}}${{mark(e,'efficiency','mejor')}}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="muted">No hay esfuerzos comparables para esta ruta con el filtro actual.</td></tr>';
}}).catch(err => {{
  document.getElementById('title').textContent = 'No se pudo cargar';
  document.getElementById('note').textContent = err.message || String(err);
  document.getElementById('rows').innerHTML = '<tr><td colspan="6" class="muted">Sin datos.</td></tr>';
}});
</script>
</body>
</html>""")


# ── GET /route/{route_id}/matched ─────────────────────────────────────────────

@router.get("/route/{route_id}/matched")
def get_route_matched(route_id: str, request: Request, limit: int = 40, min_distance_km: float = 15.0, format: str = "auto"):
    """Matched rides by route_id, with duplicate rows hidden for comparison only."""
    wants_html = "text/html" in request.headers.get("accept", "") and format != "json"
    if wants_html:
        return _route_matched_html(route_id)
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    limit = max(1, min(limit, 200))
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, distance_km, ascent_m, sport
                FROM routes WHERE route_id=%s
            """, (route_id,))
            route = cur.fetchone()
            if not route:
                cur.execute("""
                    SELECT
                        COALESCE(MAX(workout_name), 'Ruta Garmin') AS name,
                        ROUND(AVG(distance_km)::numeric, 2)::float AS distance_km,
                        ROUND(AVG(ascent_m)::numeric, 0)::int AS ascent_m,
                        MAX(sport) AS sport
                    FROM sessions_clean_compat
                    WHERE route_id=%s
                """, (route_id,))
                route = cur.fetchone()
                if not route or route[1] is None:
                    raise HTTPException(404, "Ruta no encontrada")

            route_distance = float(route[1] or 0)
            route_sport = str(route[3] or "").lower()
            if route_sport in ("cycling", "ciclismo", "biking") and route_distance < min_distance_km:
                return {
                    "route": {
                        "route_id": route_id,
                        "name": route[0],
                        "distance_km": route_distance,
                        "ascent_m": int(route[2]) if route[2] is not None else None,
                        "sport": route[3],
                    },
                    "counts": {"raw_records": 0, "unique_efforts": 0, "duplicates_hidden": 0},
                    "leaders": {},
                    "progress": {},
                    "efforts": [],
                    "status": "filtered_short_route",
                    "message": f"Ruta filtrada: para bici se priorizan rutas de {min_distance_km:g} km o mas. Puedes bajar min_distance_km si quieres revisar esta ruta.",
                }

            cur.execute("""
                SELECT session_id, start_time::text, sport, distance_km::float,
                       duration_s::int, ascent_m::int, avg_hr_bpm::float,
                       avg_speed_kmh::float, avg_cadence::float, workout_name
                FROM sessions_clean_compat
                WHERE route_id=%s
                  AND start_time IS NOT NULL
                ORDER BY start_time ASC, session_id ASC
            """, (route_id,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        efforts_by_key = {}
        duplicate_count = 0
        for row in rows:
            s = dict(zip(cols, row))
            date = str(s.get("start_time") or "")[:10]
            duration_s = int(s.get("duration_s") or 0)
            distance_km = float(s.get("distance_km") or 0)
            ascent_m = int(s.get("ascent_m") or 0)
            avg_hr = float(s.get("avg_hr_bpm") or 0)
            avg_speed = float(s.get("avg_speed_kmh") or 0)
            avg_cadence = s.get("avg_cadence")
            efficiency = round(avg_speed / avg_hr, 4) if avg_speed > 0 and avg_hr > 0 else None

            effort = {
                "session_id": s.get("session_id"),
                "date": date,
                "start_time": s.get("start_time"),
                "sport": s.get("sport"),
                "distance_km": round(distance_km, 2),
                "duration_s": duration_s,
                "duration_hms": f"{int(duration_s//3600):02d}h {int((duration_s%3600)//60):02d}m",
                "ascent_m": ascent_m,
                "avg_hr_bpm": round(avg_hr, 1) if avg_hr else None,
                "avg_speed_kmh": round(avg_speed, 2) if avg_speed else None,
                "avg_cadence": round(float(avg_cadence), 1) if avg_cadence is not None else None,
                "efficiency": efficiency,
                "workout_name": s.get("workout_name") or "",
                "duplicate_session_ids": [],
                "duplicate_count": 0,
            }

            # Same-day identical FIT imports collapse into one display effort.
            key = (
                date,
                round(distance_km, 2),
                round(duration_s / 5) * 5,
                round(avg_hr, 0),
                round(avg_speed, 1),
                round(ascent_m / 5) * 5,
            )
            if key in efforts_by_key:
                efforts_by_key[key]["duplicate_session_ids"].append(effort["session_id"])
                efforts_by_key[key]["duplicate_count"] += 1
                duplicate_count += 1
            else:
                efforts_by_key[key] = effort

        efforts = list(efforts_by_key.values())
        efforts.sort(key=lambda x: x.get("start_time") or "")

        valid_speed = [e for e in efforts if e.get("avg_speed_kmh")]
        valid_hr = [e for e in efforts if e.get("avg_hr_bpm")]
        valid_duration = [e for e in efforts if e.get("duration_s")]
        valid_eff = [e for e in efforts if e.get("efficiency")]

        best_speed = max(valid_speed, key=lambda e: e["avg_speed_kmh"]) if valid_speed else None
        best_time = min(valid_duration, key=lambda e: e["duration_s"]) if valid_duration else None
        lowest_hr = min(valid_hr, key=lambda e: e["avg_hr_bpm"]) if valid_hr else None
        best_efficiency = max(valid_eff, key=lambda e: e["efficiency"]) if valid_eff else None

        progress = {}
        if len(efforts) >= 2:
            first, last = efforts[0], efforts[-1]
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                progress["speed_delta_kmh"] = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 2)
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                progress["hr_delta_bpm"] = round(last["avg_hr_bpm"] - first["avg_hr_bpm"], 1)
            if first.get("efficiency") and last.get("efficiency"):
                progress["efficiency_delta"] = round(last["efficiency"] - first["efficiency"], 4)
            progress["first_session_id"] = first.get("session_id")
            progress["last_session_id"] = last.get("session_id")

        recent_efforts = list(reversed(efforts))[:limit]

        return {
            "route": {
                "route_id": route_id,
                "name": route[0],
                "distance_km": float(route[1]) if route[1] is not None else None,
                "ascent_m": int(route[2]) if route[2] is not None else None,
                "sport": route[3],
            },
            "counts": {
                "raw_records": len(rows),
                "unique_efforts": len(efforts),
                "duplicates_hidden": duplicate_count,
            },
            "leaders": {
                "best_speed": best_speed,
                "best_time": best_time,
                "lowest_hr": lowest_hr,
                "best_efficiency": best_efficiency,
            },
            "progress": progress,
            "efforts": recent_efforts,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Helper para /sessions con telemetría ──────────────────────────────────────

def _list_sessions_with_telemetry(conn, sport=None, month=None, limit=20, offset=0, sort="recent"):
    query = """
        SELECT session_id, start_time, sport, distance_km, duration_s,
               avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence, workout_name,
               route_id, calories, source, quality, result_json
        FROM sessions_clean_compat
        WHERE start_time IS NOT NULL
    """
    params = []
    sport_sql, sport_params = _sport_filter_sql(sport)
    query += sport_sql
    params.extend(sport_params)
    if month:
        query += " AND LEFT(start_time, 7) = %s"
        params.append(month)
    if sort == "distance":
        query += " ORDER BY distance_km DESC NULLS LAST"
    elif sort == "duration":
        query += " ORDER BY duration_s DESC NULLS LAST"
    else:
        query += " ORDER BY start_time DESC"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    old_by_id, old_by_day = _load_old_telemetry_index(conn)
    matched = []
    seen = set()
    for row in rows:
        s = dict(zip(cols, row))
        match = _telemetry_match_for_session(s, old_by_id, old_by_day)
        if not match or s.get("session_id") in seen:
            continue
        seen.add(s.get("session_id"))
        s["has_telemetry"] = True
        s["telemetry_points"] = match.get("telemetry_points") or 0
        s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)
        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        s.pop("result_json", None)
        matched.append(s)

    total = len(matched)
    return {"sessions": matched[offset:offset+limit], "total": total, "limit": limit, "offset": offset, "telemetry": True}


# ── GET /sessions ─────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions(
    sport: Optional[str] = None,
    month: Optional[str] = None,
    telemetry: Optional[bool] = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "recent"
):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        if telemetry is True:
            return _list_sessions_with_telemetry(conn, sport=sport, month=month, limit=limit, offset=offset, sort=sort)
        has_telemetry_sql = _telemetry_exists_sql()
        telemetry_points_sql = _telemetry_points_sql()
        has_map_sql = _telemetry_map_sql()
        query = f"""
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence, workout_name,
                   route_id, calories, source, quality, result_json,
                   {has_telemetry_sql} AS has_telemetry,
                   {telemetry_points_sql} AS telemetry_points,
                   {has_map_sql} AS has_map
            FROM sessions_clean_compat
            WHERE start_time IS NOT NULL
        """
        params = []
        sport_sql, sport_params = _sport_filter_sql(sport)
        query += sport_sql
        params.extend(sport_params)
        if month:
            query += " AND LEFT(start_time, 7) = %s"
            params.append(month)
        if telemetry is True:
            query += f" AND {has_telemetry_sql}"
        elif telemetry is False:
            query += f" AND NOT {has_telemetry_sql}"
        if sort == "recent":
            query += " ORDER BY start_time DESC"
        elif sort == "distance":
            query += " ORDER BY distance_km DESC NULLS LAST"
        elif sort == "duration":
            query += " ORDER BY duration_s DESC NULLS LAST"
        else:
            query += " ORDER BY start_time DESC"
        query += " LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        sessions_list = []
        for row in rows:
            s = dict(zip(cols, row))
            dur = s.get("duration_s") or 0
            s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            _enrich_session_dict(s)
            for k, v in s.items():
                if hasattr(v, "isoformat"):
                    s[k] = v.isoformat()
            s.pop("result_json", None)
            sessions_list.append(s)

        # Total count
        count_query = "SELECT COUNT(*) FROM sessions_clean_compat WHERE start_time IS NOT NULL"
        count_params = []
        count_sport_sql, count_sport_params = _sport_filter_sql(sport)
        count_query += count_sport_sql
        count_params.extend(count_sport_params)
        if month:
            count_query += " AND LEFT(start_time, 7) = %s"
            count_params.append(month)
        if telemetry is True:
            count_query += f" AND {has_telemetry_sql}"
        elif telemetry is False:
            count_query += f" AND NOT {has_telemetry_sql}"
        with conn.cursor() as cur:
            cur.execute(count_query, count_params)
            total = cur.fetchone()[0]

        return {"sessions": sessions_list, "total": total, "limit": limit, "offset": offset, "telemetry": telemetry}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /charts/{session_id} (HTML) ──────────────────────────────────────────

@router.get("/charts/{session_id}", response_class=HTMLResponse)
def get_charts(session_id: str):
    entry = RESULTS_STORE.get(session_id)
    if not entry:
        conn = get_db()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT result_json, start_time, sport, distance_km, duration_s,
                               ascent_m, avg_hr_bpm, avg_speed_kmh, avg_cadence,
                               workout_name, calories, source, quality
                        FROM sessions_clean_compat WHERE session_id=%s
                    """,(session_id,))
                    row = cur.fetchone()
                    if row:
                        result = json.loads(row[0]) if row[0] else {}
                        if "session" not in result:
                            result["session"] = {
                                "start_time": row[1],
                                "sport": row[2],
                                "distance_km": row[3],
                                "duration_s": row[4],
                                "ascent_m": row[5],
                                "avg_hr_bpm": row[6],
                                "avg_speed_kmh": row[7],
                                "avg_cadence_rpm": row[8],
                                "workout_name": row[9],
                                "calories_kcal": row[10],
                                "source": row[11],
                                "quality": row[12],
                            }
                        entry = {"result": result}
            except Exception as _e: logger.error(f'Silent error: {_e}')
    if not entry:
        raise HTTPException(404, f"session_id '{session_id}' no encontrado.")

    result  = entry["result"]
    session = result["session"]
    if not session.get("calories_kcal") and not session.get("calories"):
        recovered_cal = _extract_calories_from_payload(result)
        if recovered_cal:
            session["calories_kcal"] = recovered_cal
    records = result.get("records", [])
    insights = result.get("derived_insights", {})

    if not records:
        # Try loading from session_records table
        conn2 = get_db()
        if conn2:
            try:
                with conn2.cursor() as cur:
                    cur.execute("""
                        SELECT t, hr, speed, cadence, altitude, lat, lon
                        FROM session_records sr
                        WHERE sr.session_id IN (%s, REPLACE(%s, 'session:', ''))
                           OR sr.session_id IN (
                                SELECT old_s.session_id
                                FROM sessions_clean_compat c
                                JOIN sessions old_s
                                 ON NULLIF(old_s.start_time, '') IS NOT NULL
                                 AND NULLIF(c.start_time, '') IS NOT NULL
                                 AND (
                                      ABS(EXTRACT(EPOCH FROM (c.start_time::timestamp - old_s.start_time::timestamp))) <= 5
                                      OR ABS(ABS(EXTRACT(EPOCH FROM (c.start_time::timestamp - old_s.start_time::timestamp))) - 21600) <= 5
                                 )
                                 AND ABS(COALESCE(c.distance_km, 0) - COALESCE(old_s.distance_km, 0)) <= 0.02
                                 AND ABS(COALESCE(c.duration_s, 0) - COALESCE(old_s.duration_s, 0)) <= 180
                                WHERE c.session_id = %s
                           )
                        ORDER BY t ASC
                    """, (session_id, session_id, session_id))
                    db_records = cur.fetchall()
                if db_records:
                    def db_num(v):
                        try:
                            return float(v) if v is not None else None
                        except Exception:
                            return None
                    records = [
                        {"heart_rate_bpm": db_num(r[1]), "speed_kmh": db_num(r[2]),
                         "cadence_rpm": db_num(r[3]), "altitude_m": db_num(r[4]),
                         "lat": db_num(r[5]), "lon": db_num(r[6]), "_t": db_num(r[0])}
                        for r in db_records
                    ]
                    times = [db_num(r[0]) for r in db_records]
                    hr = [db_num(r[1]) for r in db_records]
                    cad = [db_num(r[3]) for r in db_records]
                    spd = [db_num(r[2]) for r in db_records]
                    alt = [db_num(r[4]) for r in db_records]
                    lat = [db_num(r[5]) for r in db_records]
                    lon = [db_num(r[6]) for r in db_records]
                    temp = [None] * len(db_records)
            except Exception as e:
                logger.info(f"session_records load error: {e}")
    if not records:
        src = session.get("source", "")
        is_garmin_export = str(src).startswith("garmin")
        msg = (
            "Esta sesión fue importada desde Garmin Connect. "
            "Las gráficas segundo a segundo solo están disponibles para actividades subidas como archivo FIT."
            if is_garmin_export else
            "No hay datos de telemetría disponibles para esta sesión."
        )

        def _fmt_dur(s):
            if not s: return "—"
            s = int(s)
            h, m = s // 3600, (s % 3600) // 60
            return f"{h}h {m:02d}m" if h else f"{m}m"

        def _stat(label, val, unit=""):
            v = f"{val} {unit}".strip() if val else "—"
            return f'<div class="stat"><div class="stat-l">{label}</div><div class="stat-v">{v}</div></div>'

        stats_html = (
            _stat("Fecha", str(session.get("start_time", ""))[:10]) +
            _stat("Deporte", session.get("sport", "").replace("_", " ").title()) +
            _stat("Distancia", f"{session.get('distance_km', '')}", "km") +
            _stat("Duración", _fmt_dur(session.get("duration_s"))) +
            _stat("FC prom.", session.get("avg_hr_bpm", ""), "bpm") +
            _stat("Vel. prom.", session.get("avg_speed_kmh", ""), "km/h") +
            _stat("Cadencia", session.get("avg_cadence_rpm") or session.get("avg_cadence", ""), "rpm") +
            _stat("Desnivel", session.get("ascent_m", ""), "m")
        )
        sid_js = json.dumps(session_id)
        return HTMLResponse(f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Resumen de sesión</title>
  <style>
    body{{margin:0;background:#08090b;color:#f7f7f4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
    main{{max-width:860px;margin:0 auto;padding:22px 16px 96px}}
    .top{{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:18px}}
    a,button{{color:#e8593c;text-decoration:none;font-weight:850}}
    button,.btn{{background:#15171c;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:10px 12px;cursor:pointer;display:inline-block}}
    .card{{background:#15171c;border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:22px;margin:12px 0}}
    h2{{font-size:22px;font-weight:850;margin:0 0 14px}}
    .notice{{color:#8d8d96;font-size:14px;line-height:1.5;margin-bottom:16px;padding:10px 14px;background:rgba(255,255,255,.04);border-radius:8px;border-left:3px solid #34343a}}
    .stats{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px}}
    .stat{{background:#0d0f13;border-radius:10px;padding:12px 14px}}
    .stat-l{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8d8d96;margin-bottom:4px}}
    .stat-v{{font-size:20px;font-weight:750}}
    .actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}}
    @media(max-width:480px){{.stats{{grid-template-columns:1fr 1fr}}}}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <button onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
      <a href="/activities">Actividades</a>
    </div>
    <section class="card">
      <h2>{session.get('workout_name') or 'Resumen de sesión'}</h2>
      <div class="notice">{msg}</div>
      <div class="stats">{stats_html}</div>
      <div class="actions">
        <a class="btn" href="/activities">Actividades</a>
        <a class="btn" href="/activities?telemetry=1">Ver sesiones con curvas</a>
      </div>
    </section>
  </main>
  <script>const sid={sid_js};</script>
</body>
</html>""")

    # Si los records ya tienen _t (vienen de session_records), usar esos arrays directamente
    from_db = records and "_t" in records[0]
    if not from_db:
        times, hr, cad, spd, alt, temp, lat, lon = [], [], [], [], [], [], [], []
        start_ts = None
        for r in records:
            try:
                from datetime import datetime as dt
                t = dt.fromisoformat(r.get("timestamp",""))
                if start_ts is None: start_ts = t
                elapsed = int((t-start_ts).total_seconds())
            except:
                elapsed = len(times)
            times.append(elapsed)
            hr.append(r.get("heart_rate_bpm"))
            cad.append(r.get("cadence_rpm"))
            spd.append(r.get("speed_kmh"))
            alt.append(r.get("altitude_m"))
            temp.append(r.get("temperature_c"))
            lat.append(r.get("lat"))
            lon.append(r.get("lon"))
    # else: times, hr, cad, spd, alt, temp already set from session_records

    data_js = f"""
const times={json.dumps(times)};
const hrData={json.dumps(hr)};
const cadData={json.dumps(cad)};
const spdData={json.dumps(spd)};
const altData={json.dumps(alt)};
const tempData={json.dumps(temp)};
const latData={json.dumps(lat)};
const lonData={json.dumps(lon)};
const zonesData={json.dumps(result.get('zones',[]))};
const sessionInfo={json.dumps(session)};
const insights={json.dumps(insights)};
"""

    return f"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Graficas Mars</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{{--bg:#000;--s:#050505;--s2:#0d0d0f;--b:#242428;--line:#34343a;--a:#e8593c;--blue:#2f9bff;--green:#37d46f;--red:#ff6670;--orange:#ff8a1f;--gray:#7c7c83;--t:#f6f6f7;--m:#8d8d96}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--t);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",sans-serif;padding:0}}
  main{{max-width:920px;margin:0 auto;padding:14px 16px 80px}}
  .top{{position:sticky;top:0;z-index:5;background:rgba(0,0,0,.88);backdrop-filter:blur(18px);border-bottom:1px solid var(--b);padding:12px 16px;display:flex;align-items:center;gap:12px;justify-content:space-between}}
  .top-left{{display:flex;align-items:center;gap:10px}}
  .back{{background:#0d0d0f;border:1px solid var(--line);color:#ff7058;border-radius:12px;padding:9px 13px;font-size:14px;font-weight:850;cursor:pointer}}
  a{{color:#ff7058;text-decoration:none;font-weight:850}}
  .hero{{padding:20px 0 18px;border-bottom:10px solid #0d0d0f;margin-bottom:18px}}
  .ttl{{font-size:34px;font-weight:850;line-height:1.05;margin-bottom:8px}}
  .sub{{font-size:14px;color:var(--m);line-height:1.35}}
  .mode{{display:grid;grid-template-columns:1fr 1fr;gap:0;background:#151519;border:1px solid var(--b);border-radius:12px;padding:3px;margin:16px 0 4px;max-width:560px}}
  .mode button{{border:none;border-radius:9px;padding:9px;background:transparent;color:#d7d7dc;font-size:15px;font-weight:750}}
  .mode button.on{{background:#5e5e66;color:white}}
  .summary{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
  .mini{{background:#09090b;border:1px solid var(--b);border-radius:14px;padding:12px}}
  .mini-k{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--m);font-weight:850}}
  .mini-v{{font-size:22px;font-weight:850;margin-top:5px}}
  .section{{border-top:1px solid var(--b);padding:30px 0 26px}}
  .section:first-of-type{{border-top:0}}
  .section-title{{font-size:28px;font-weight:850;margin-bottom:18px;display:flex;align-items:center;gap:8px}}
  .metric-pair{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:18px}}
  .big-stat{{border-top:1px solid var(--line);padding-top:12px}}
  .big-val{{font-size:39px;font-weight:350;letter-spacing:0;line-height:1}}
  .big-unit{{font-size:18px;color:#d7d7dc;margin-left:4px}}
  .big-label{{font-size:17px;color:var(--m);margin-top:6px}}
  .chart-wrap{{height:260px;position:relative}}
  .chart-wrap.compact{{height:220px}}
  canvas{{width:100%!important;height:100%!important}}
  .mapbox{{height:240px;border-radius:16px;background:#090b0f;border:1px solid var(--b);overflow:hidden;position:relative;margin-top:10px}}
  .mapbox svg{{width:100%;height:100%;display:block}}
  .mapnote{{position:absolute;left:12px;bottom:10px;color:#d7d7dc;font-size:12px;background:rgba(0,0,0,.72);padding:6px 10px;border-radius:999px}}
  .te-grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:22px}}
  .te-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle}}
  .effect-row{{margin:24px 0}}
  .effect-label{{font-size:25px;margin-bottom:10px}}
  .effect-bar{{height:11px;border-radius:8px;display:grid;grid-template-columns:1.1fr 1.1fr 1.1fr 1.1fr 1.1fr .12fr;gap:8px;position:relative}}
  .effect-bar span{{border-radius:3px;background:#777}}
  .effect-bar .b{{background:#399cff}} .effect-bar .g{{background:#56d878}} .effect-bar .o{{background:#ff9b38}} .effect-bar .r{{background:#ff4545}}
  .knob{{position:absolute;top:50%;width:33px;height:33px;border-radius:50%;background:#56d878;border:4px solid #101010;transform:translate(-50%,-50%);box-shadow:0 0 0 1px rgba(255,255,255,.25)}}
  .zones-list{{display:flex;flex-direction:column;gap:18px;margin-top:4px}}
  .zone-row{{display:grid;grid-template-columns:minmax(160px,1fr) minmax(150px,2fr) 64px 52px;gap:12px;align-items:center}}
  .zone-name{{font-size:22px}}
  .zone-sub{{font-size:16px;color:var(--m);margin-left:4px}}
  .zone-track{{height:10px;border-radius:99px;background:#626267;overflow:hidden}}
  .zone-fill{{height:100%;border-radius:99px;background:var(--zc)}}
  .zone-time,.zone-pct{{font-size:20px;text-align:right;color:#e7e7eb}}
  .insight{{margin-top:8px;padding:12px 14px;background:#0d0d0f;border-radius:10px;border-left:3px solid var(--orange);font-size:14px;color:#c8c8cf;line-height:1.5}}
  @media(max-width:640px){{main{{padding:10px 14px 70px}}.ttl{{font-size:30px}}.summary{{grid-template-columns:1fr 1fr}}.metric-pair,.te-grid{{grid-template-columns:1fr 1fr;gap:16px}}.big-val{{font-size:34px}}.chart-wrap{{height:230px}}.zone-row{{grid-template-columns:1fr;gap:7px}}.zone-time,.zone-pct{{text-align:left;font-size:17px}}}}
</style></head><body>
<div class="top">
  <div class="top-left">
    <button class="back" onclick="if(history.length>1){{history.back()}}else{{location.href='/activities'}}">Volver</button>
  </div>
  <a href="/activities">Actividades</a>
</div>
<main>
<section class="hero">
  <div class="ttl" id="ttl">—</div>
  <div class="sub" id="subline">Gráficas por tiempo</div>
  <div class="mode"><button class="on">Tiempo</button><button>Distancia</button></div>
  <div class="summary">
    <div class="mini"><div class="mini-k">Distancia</div><div class="mini-v" id="sd">—</div></div>
    <div class="mini"><div class="mini-k">Duración</div><div class="mini-v" id="sdur">—</div></div>
    <div class="mini"><div class="mini-k">FC prom.</div><div class="mini-v" id="shr">—</div></div>
    <div class="mini"><div class="mini-k">Vel prom.</div><div class="mini-v" id="sspd">—</div></div>
  </div>
</section>
<section class="section"><div class="section-title">Mapa</div><div class="mapbox" id="mapBox"><div class="mapnote">Cargando mapa...</div></div></section>
<section class="section"><div class="section-title">Elevation</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="altMin">—</span><span class="big-unit">m</span></div><div class="big-label">Minimum</div></div><div class="big-stat"><div><span class="big-val" id="altMax">—</span><span class="big-unit">m</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cAlt"></canvas></div></section>
<section class="section"><div class="section-title">Speed</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="spdAvg">—</span><span class="big-unit">km/h</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="spdMax">—</span><span class="big-unit">km/h</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cSpd"></canvas></div></section>
<section class="section"><div class="section-title">Heart Rate</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="hrAvg">—</span><span class="big-unit">bpm</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="hrMax">—</span><span class="big-unit">bpm</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cHR"></canvas></div></section>
<section class="section"><div class="section-title">Training Effect</div><div class="te-grid"><div class="big-stat"><div><span class="big-val" id="teAer">—</span></div><div class="big-label"><span class="te-dot" style="background:#56d878"></span>Aerobic</div></div><div class="big-stat"><div><span class="big-val" id="teAna">—</span></div><div class="big-label"><span class="te-dot" style="background:#777"></span>Anaerobic</div></div></div><div class="effect-row"><div class="effect-label">Aerobic</div><div class="effect-bar" id="barAer"><span></span><span></span><span class="b"></span><span class="g"></span><span class="o"></span><span class="r"></span><i class="knob"></i></div></div><div class="effect-row"><div class="effect-label">Anaerobic</div><div class="effect-bar" id="barAna"><span></span><span></span><span class="b"></span><span class="g"></span><span class="o"></span><span class="r"></span><i class="knob"></i></div></div></section>
<section class="section"><div class="section-title">Cadence</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="cadAvg">—</span><span class="big-unit">rpm</span></div><div class="big-label">Average</div></div><div class="big-stat"><div><span class="big-val" id="cadMax">—</span><span class="big-unit">rpm</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap"><canvas id="cCad"></canvas></div></section>
<section class="section"><div class="section-title">Temperature</div><div class="metric-pair"><div class="big-stat"><div><span class="big-val" id="tmpMin">—</span><span class="big-unit">°C</span></div><div class="big-label">Minimum</div></div><div class="big-stat"><div><span class="big-val" id="tmpMax">—</span><span class="big-unit">°C</span></div><div class="big-label">Maximum</div></div></div><div class="chart-wrap compact"><canvas id="cTmp"></canvas></div></section>
<section class="section"><div class="section-title">Time in Heart Rate Zones</div><div class="zones-list" id="zrow"></div></section>
<section class="section"><div class="section-title">Insights</div><div id="insightsBox"></div></section>
<section class="section"><div class="section-title">Análisis Perfil Mars</div><div id="marsAnalysis" style="color:#c8c8cf;font-size:15px">Cargando análisis...</div></section>
</main>
<script>
{data_js}
function fmtDate(v){{
  if(!v)return '—';
  try{{
    const d=new Date(String(v).slice(0,10)+'T12:00:00');
    const M=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
    return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
  }}catch(e){{return String(v);}}
}}
document.getElementById('ttl').textContent=(sessionInfo.workout_name||fmtDate(sessionInfo.start_time)||'Sesión').slice(0,40)||'Sesión';
document.getElementById('sd').textContent=sessionInfo.distance_km?sessionInfo.distance_km+' km':'—';
(async function(){{
  try{{
    var sid=location.pathname.split('/').pop();
    var resp=await fetch('/gpt/session-analysis/'+sid);
    var a=await resp.json();
    if(a&&a.analysis){{
      var an=a.analysis;
      document.getElementById('marsAnalysis').innerHTML=
        '<div style="display:flex;flex-direction:column;gap:8px">'+
        '<div style="padding:8px 12px;background:rgba(61,214,140,.08);border-left:3px solid #3dd68c;border-radius:6px"><b style="color:#3dd68c">Zona:</b> '+an.zone+'</div>'+
        '<div style="padding:8px 12px;background:rgba(74,158,255,.08);border-left:3px solid #4a9eff;border-radius:6px"><b style="color:#4a9eff">Cadencia:</b> '+an.cadence+'</div>'+
        '<div style="padding:8px 12px;background:rgba(167,139,250,.08);border-left:3px solid #a78bfa;border-radius:6px"><b style="color:#a78bfa">Eficiencia:</b> '+an.efficiency+'</div>'+
        '<div style="padding:8px 12px;background:rgba(232,89,60,.08);border-left:3px solid #e8593c;border-radius:6px"><b style="color:#e8593c">vs Historico:</b> '+an.comparison+'</div>'+
        '</div>';
    }}else{{document.getElementById('marsAnalysis').textContent='Sin datos de analisis';}}
  }}catch(e){{document.getElementById('marsAnalysis').textContent='Analisis no disponible';}}
}})();
document.getElementById('sdur').textContent=sessionInfo.duration_hms||'—';
document.getElementById('shr').textContent=sessionInfo.avg_hr_bpm?sessionInfo.avg_hr_bpm+' bpm':'—';
document.getElementById('sspd').textContent=sessionInfo.avg_speed_kmh?sessionInfo.avg_speed_kmh+' km/h':'—';
const clean = a => (a||[]).map(Number).filter(Number.isFinite);
const avg = a => {{const v=clean(a);return v.length?v.reduce((x,y)=>x+y,0)/v.length:null;}};
const minv = a => {{const v=clean(a);return v.length?Math.min(...v):null;}};
const maxv = a => {{const v=clean(a);return v.length?Math.max(...v):null;}};
const setText = (id,val,dec=0) => {{
  const el=document.getElementById(id);
  if(!el)return;
  el.textContent=val==null?'—':Number(val).toFixed(dec).replace(/\\.0$/,'');
}};
setText('altMin',minv(altData),0); setText('altMax',maxv(altData),0);
setText('spdAvg',sessionInfo.avg_speed_kmh || avg(spdData),1); setText('spdMax',sessionInfo.max_speed_kmh || maxv(spdData),1);
setText('hrAvg',sessionInfo.avg_hr_bpm || avg(hrData),0); setText('hrMax',sessionInfo.max_hr_bpm || maxv(hrData),0);
setText('cadAvg',sessionInfo.avg_cadence_rpm || sessionInfo.avg_cadence || avg(cadData),0); setText('cadMax',sessionInfo.max_cadence_rpm || maxv(cadData),0);
setText('tmpMin',minv(tempData),0); setText('tmpMax',maxv(tempData),0);
const teAer = sessionInfo.training_effect_aerobic ?? sessionInfo.total_training_effect ?? null;
const teAna = sessionInfo.training_effect_anaerobic ?? sessionInfo.total_anaerobic_training_effect ?? null;
setText('teAer',teAer,1); setText('teAna',teAna,1);
function setKnob(id,val){{
  const bar=document.getElementById(id), k=bar&&bar.querySelector('.knob');
  if(!k)return;
  const pct=Math.max(3,Math.min(97,((Number(val)||0)/5)*100));
  k.style.left=pct+'%';
  if(!val) k.style.background='#777';
}}
setKnob('barAer',teAer); setKnob('barAna',teAna);
function drawMap(){{
  const pts=[];
  for(let i=0;i<latData.length;i++){{
    const la=Number(latData[i]), lo=Number(lonData[i]);
    if(Number.isFinite(la)&&Number.isFinite(lo)) pts.push([la,lo]);
  }}
  const box=document.getElementById('mapBox');
  if(pts.length<2){{
    box.innerHTML='<div class="mapnote">Sin puntos GPS suficientes para mapa.</div>';
    return;
  }}
  const lats=pts.map(p=>p[0]), lons=pts.map(p=>p[1]);
  const minLat=Math.min(...lats), maxLat=Math.max(...lats), minLon=Math.min(...lons), maxLon=Math.max(...lons);
  const pad=18, w=720, h=220;
  const dx=maxLon-minLon || 0.00001, dy=maxLat-minLat || 0.00001;
  const path=pts.map((p,i)=>{{
    const x=pad+((p[1]-minLon)/dx)*(w-pad*2);
    const y=h-pad-((p[0]-minLat)/dy)*(h-pad*2);
    return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);
  }}).join(' ');
  const start=pts[0], end=pts[pts.length-1];
  const px=p=>pad+((p[1]-minLon)/dx)*(w-pad*2);
  const py=p=>h-pad-((p[0]-minLat)/dy)*(h-pad*2);
  box.innerHTML=`<svg viewBox="0 0 ${{w}} ${{h}}" preserveAspectRatio="none">
    <path d="${{path}}" fill="none" stroke="#e8593c" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${{px(start)}}" cy="${{py(start)}}" r="6" fill="#3dd68c"/>
    <circle cx="${{px(end)}}" cy="${{py(end)}}" r="6" fill="#f59e0b"/>
  </svg><div class="mapnote">${{pts.length.toLocaleString('es-MX')}} puntos GPS</div>`;
}}
drawMap();
function ds(a,n){{if(a.length<=n)return a;const s=a.length/n;return Array.from({{length:n}},(_,i)=>a[Math.floor(i*s)]);}}
const N=400;
const lbl=ds(times,N).map(s=>{{const m=Math.floor(s/60),sc=s%60;return m+':'+String(sc).padStart(2,'0');}});
const avgLinePlugin={{
  id:'avgLine',
  afterDatasetsDraw(chart,args,opts){{
    if(opts.value==null)return;
    const y=chart.scales.y.getPixelForValue(opts.value);
    const ctx=chart.ctx;
    ctx.save();
    ctx.strokeStyle='rgba(255,255,255,.7)';
    ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(chart.chartArea.left,y);ctx.lineTo(chart.chartArea.right,y);ctx.stroke();
    ctx.restore();
  }}
}};
Chart.register(avgLinePlugin);
function chartOpts(unit,min,max,avgValue){{
  return {{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{display:false}},avgLine:{{value:avgValue}},tooltip:{{backgroundColor:'#101014',borderColor:'#363640',borderWidth:1,titleColor:'#9a9aa3',bodyColor:'#fff',callbacks:{{label:ctx=>ctx.parsed.y!=null?(Math.round(ctx.parsed.y*10)/10)+(unit||''):''}}}}}},
    scales:{{x:{{ticks:{{color:'#8d8d96',font:{{size:11}},maxTicksLimit:6}},grid:{{display:false}}}},y:{{min:min,max:max,ticks:{{color:'#8d8d96',font:{{size:11}}}},grid:{{color:'rgba(255,255,255,.08)'}}}}}}}};
}}
function areaChart(id,data,color,unit,min=null,max=null,avgValue=null,stepped=false){{
  const el=document.getElementById(id); if(!el)return;
  const ctx=el.getContext('2d');
  const grad=ctx.createLinearGradient(0,0,0,260);
  grad.addColorStop(0,color+'cc'); grad.addColorStop(.55,color+'66'); grad.addColorStop(1,color+'10');
  new Chart(el,{{type:'line',data:{{labels:lbl,datasets:[{{data:ds(data,N),borderColor:color,borderWidth:1.8,pointRadius:0,fill:true,backgroundColor:grad,tension:stepped?0:.22,stepped:stepped}}]}},options:chartOpts(unit,min,max,avgValue)}});}}
function barChart(id,data,color,unit,min=null,max=null,avgValue=null){{
  const el=document.getElementById(id); if(!el)return;
  new Chart(el,{{type:'bar',data:{{labels:lbl,datasets:[{{data:ds(data,N),backgroundColor:color+'dd',borderWidth:0,barPercentage:1,categoryPercentage:1}}]}},options:chartOpts(unit,min,max,avgValue)}});}}
areaChart('cAlt',altData,'#37d46f',' m',null,null,avg(altData));
areaChart('cSpd',spdData,'#2f9bff',' km/h',0,null,avg(spdData));
areaChart('cHR',hrData,'#ff6670',' bpm',60,null,avg(hrData));
barChart('cCad',cadData,'#ff8a1f',' rpm',0,null,avg(cadData));
areaChart('cTmp',tempData,'#777',' °C',null,null,avg(tempData),true);
const zc={{0:'#8d8d96',1:'#f6f6f7',2:'#2f9bff',3:'#1fb95f',4:'#ff8a1f',5:'#ff4545'}};
const zrow=document.getElementById('zrow');
zonesData.forEach((z,i)=>{{
  const zone=z.zone||i;
  const secs=Math.round((Number(z.minutes)||0)*60);
  const mm=Math.floor(secs/60), ss=String(secs%60).padStart(2,'0');
  const pct=Math.round(Number(z.percent)||0);
  zrow.innerHTML+=`<div class="zone-row" style="--zc:${{zc[zone]||'#777'}}"><div><span class="zone-name">Zone ${{zone}}</span><span class="zone-sub">${{z.bpm_low||''}}${{z.bpm_high?' - '+z.bpm_high:''}} bpm · ${{(z.name||'').replace('Z'+zone,'').trim()}}</span></div><div class="zone-track"><div class="zone-fill" style="width:${{Math.max(pct,1)}}%"></div></div><div class="zone-time">${{mm}}:${{ss}}</div><div class="zone-pct">${{pct}}%</div></div>`;
}});
const ib=document.getElementById('insightsBox');
const keys=['hr_drift_note','best_aerobic_window','traffic_note','altitude_note'];
keys.forEach(k=>{{if(insights[k]){{
  const val=typeof insights[k]==='object'?insights[k].note:insights[k];
  ib.innerHTML+=`<div class="insight">${{val}}</div>`;
}}}});
if(!ib.innerHTML)ib.innerHTML='<div class="insight" style="color:#444">Sin insights adicionales.</div>';
</script></body></html>"""


# ── GET /stats/yearly ─────────────────────────────────────────────────────────

@router.get("/stats/yearly")
def stats_yearly(sport: Optional[str] = None, limit: int = 20):
    """Volumen por año — km, horas, sesiones, FC promedio."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT
                EXTRACT(YEAR FROM start_time::timestamp)::int as year,
                COUNT(*) as sesiones,
                ROUND(SUM(COALESCE(distance_km,0))::numeric, 1) as km_total,
                ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas_total,
                ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                ROUND(SUM(COALESCE(ascent_m,0))::numeric, 0) as ascenso_total
            FROM sessions_clean_compat
            WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " GROUP BY year ORDER BY year ASC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        years = [dict(zip(cols, r)) for r in rows]
        return {"sport": sport or "all", "years": years}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /stats/records ────────────────────────────────────────────────────────

@router.get("/stats/records")
def stats_records(sport: Optional[str] = None):
    """Récords personales históricos."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        where = "WHERE start_time IS NOT NULL"
        params = []
        if sport:
            where += " AND sport = %s"
            params.append(sport)

        records = {}
        with conn.cursor() as cur:
            cur.execute(f"SELECT distance_km, start_time, session_id FROM sessions_clean_compat {where} AND distance_km IS NOT NULL ORDER BY distance_km DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_distance"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            cur.execute(f"SELECT duration_s, start_time, session_id FROM sessions_clean_compat {where} AND duration_s IS NOT NULL ORDER BY duration_s DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_duration"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            cur.execute(f"SELECT ascent_m, start_time, session_id FROM sessions_clean_compat {where} AND ascent_m IS NOT NULL ORDER BY ascent_m DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_ascent"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            cur.execute(f"SELECT avg_speed_kmh, start_time, session_id FROM sessions_clean_compat {where} AND avg_speed_kmh IS NOT NULL ORDER BY avg_speed_kmh DESC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["max_speed"] = {"value": float(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

            cur.execute(f"SELECT avg_hr_bpm, start_time, session_id FROM sessions_clean_compat {where} AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 60 AND distance_km > 20 ORDER BY avg_hr_bpm ASC LIMIT 1", params)
            r = cur.fetchone()
            if r: records["min_hr"] = {"value": int(r[0]), "date": str(r[1])[:10], "session_id": r[2]}

        return {"sport": sport or "all", "records": records}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /stats/monthly ────────────────────────────────────────────────────────

@router.get("/stats/monthly")
def stats_monthly(year: int = None, month: int = None, sport: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    from datetime import date
    today = date.today()
    if not year: year = today.year
    if not month: month = today.month
    month_str = f"{year}-{month:02d}"
    try:
        query = """
            SELECT COUNT(*) as sesiones,
                   ROUND(SUM(distance_km)::numeric, 1) as km_total,
                   ROUND(SUM(COALESCE(duration_s,0))::numeric / 3600, 1) as horas_total,
                   ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                   ROUND(SUM(ascent_m)::numeric, 0) as ascenso_total
            FROM sessions_clean_compat
            WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
        """
        params = [month_str]
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        result = dict(zip(cols, row))
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sport, COUNT(*) as sesiones,
                       ROUND(SUM(distance_km)::numeric, 1) as km
                FROM sessions_clean_compat
                WHERE LEFT(start_time, 7) = %s AND start_time IS NOT NULL
                  AND sport IS NOT NULL AND sport != ''
                GROUP BY sport ORDER BY sesiones DESC
            """, [month_str])
            rows = cur.fetchall()
            cols2 = [d[0] for d in cur.description]
        result["by_sport"] = [dict(zip(cols2, r)) for r in rows]
        result["year"] = year
        result["month"] = month
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /stats/efficiency ─────────────────────────────────────────────────────

@router.get("/stats/efficiency")
def stats_efficiency(weeks: int = 8, sport: str = "cycling"):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    TO_CHAR(DATE_TRUNC('week', start_time::timestamp), 'YYYY-MM-DD') as semana,
                    COUNT(*) as sesiones,
                    ROUND(AVG(avg_speed_kmh)::numeric, 1) as vel_promedio,
                    ROUND(AVG(avg_hr_bpm)::numeric, 0) as fc_promedio,
                    ROUND(SUM(distance_km)::numeric, 1) as km_total,
                    ROUND((CASE WHEN AVG(avg_hr_bpm) > 0
                        THEN AVG(avg_speed_kmh) / AVG(avg_hr_bpm) * 100
                        ELSE 0 END)::numeric, 2) as eficiencia
                FROM sessions_clean_compat
                WHERE sport = %s AND start_time IS NOT NULL
                  AND avg_speed_kmh IS NOT NULL AND avg_hr_bpm IS NOT NULL
                  AND start_time::timestamp >= NOW() - (%s * INTERVAL '1 week')
                GROUP BY semana ORDER BY semana ASC
            """, (sport, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        return {"sport": sport, "weeks": weeks, "data": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /sessions/recent ──────────────────────────────────────────────────────

@router.get("/sessions/recent")
def sessions_recent(limit: int = 1, sport: Optional[str] = None):
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = f"""
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                   workout_name, route_id, calories, source, quality, result_json,
                   {_telemetry_exists_sql()} AS has_telemetry,
                   {_telemetry_points_sql()} AS telemetry_points,
                   {_telemetry_map_sql()} AS has_map
            FROM sessions_clean_compat WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " ORDER BY start_time DESC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        sessions_list = []
        for row in rows:
            s = dict(zip(cols, row))
            dur = s.get("duration_s") or 0
            s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            for k, v in s.items():
                if hasattr(v, 'isoformat'):
                    s[k] = v.isoformat()
            sessions_list.append(s)
        return {"sessions": sessions_list, "total": len(sessions_list)}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/latest-session ───────────────────────────────────────────────────

@router.get("/gpt/latest-session")
def gpt_latest_session(sport: Optional[str] = None):
    """Última sesión para GPT — métricas completas de la actividad más reciente."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        query = """
            SELECT session_id, start_time, sport, distance_km, duration_s,
                   avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                   workout_name, route_id
            FROM sessions_clean_compat WHERE start_time IS NOT NULL
        """
        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)
        query += " ORDER BY start_time DESC LIMIT 1"
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Sin sesiones")
            cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        if not s.get("has_telemetry"):
            old_by_id, old_by_day = _load_old_telemetry_index(conn)
            match = _telemetry_match_for_session(s, old_by_id, old_by_day)
            if match:
                s["has_telemetry"] = True
                s["telemetry_points"] = match.get("telemetry_points") or 0
                s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)
        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        s.pop("result_json", None)
        return s
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/session/{session_id} ────────────────────────────────────────────

@router.get("/gpt/session/{session_id}")
def gpt_session(session_id: str):
    """Detalle de una sesión específica para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT session_id, start_time, sport, distance_km, duration_s,
                       avg_hr_bpm, avg_speed_kmh, ascent_m, avg_cadence,
                       workout_name, route_id, result_json, calories, source, quality,
                       {_telemetry_exists_sql()} AS has_telemetry,
                       {_telemetry_points_sql()} AS telemetry_points,
                       {_telemetry_map_sql()} AS has_map
                FROM sessions_clean_compat WHERE session_id = %s
            """, [session_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"session_id '{session_id}' no encontrado")
            cols = [d[0] for d in cur.description]
        s = dict(zip(cols, row))
        if not s.get("has_telemetry"):
            old_by_id, old_by_day = _load_old_telemetry_index(conn)
            match = _telemetry_match_for_session(s, old_by_id, old_by_day)
            if match:
                s["has_telemetry"] = True
                s["telemetry_points"] = match.get("telemetry_points") or 0
                s["has_map"] = bool(match.get("has_map"))
        dur = s.get("duration_s") or 0
        s["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
        _enrich_session_dict(s)

        # Extraer zonas e insights del result_json
        if s.get("result_json"):
            try:
                rj = json.loads(s["result_json"])
                s["zones"] = rj.get("zones", [])
                s["insights"] = rj.get("derived_insights", {})
                s["laps"] = rj.get("laps", [])
            except:
                pass
        del s["result_json"]

        for k, v in s.items():
            if hasattr(v, "isoformat"):
                s[k] = v.isoformat()
        return s
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/route-progress/{route_id} ───────────────────────────────────────

@router.get("/gpt/route-progress/{route_id}")
def gpt_route_progress(route_id: str):
    """Progreso en una ruta específica para GPT."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT route_id, name, distance_km, ascent_m FROM routes WHERE route_id = %s", [route_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"route_id '{route_id}' no encontrado")
            route = {"route_id": row[0], "name": row[1], "distance_km": row[2], "ascent_m": row[3]}

            cur.execute("""
                SELECT start_time, avg_hr_bpm, avg_speed_kmh, avg_cadence, duration_s
                FROM sessions_clean_compat
                WHERE route_id = %s AND start_time IS NOT NULL
                ORDER BY start_time ASC
            """, [route_id])
            rides = cur.fetchall()
            ride_cols = [d[0] for d in cur.description]

        ride_list = []
        for r in rides:
            rd = dict(zip(ride_cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"):
                    rd[k] = v.isoformat()
            ride_list.append(rd)

        if len(ride_list) >= 2:
            first = ride_list[0]
            last = ride_list[-1]
            hr_delta = None
            spd_delta = None
            if first.get("avg_hr_bpm") and last.get("avg_hr_bpm"):
                hr_delta = round(last["avg_hr_bpm"] - first["avg_hr_bpm"], 0)
            if first.get("avg_speed_kmh") and last.get("avg_speed_kmh"):
                spd_delta = round(last["avg_speed_kmh"] - first["avg_speed_kmh"], 1)
            route["progreso"] = {
                "veces_rodada": len(ride_list),
                "primera_fecha": ride_list[0]["start_time"],
                "ultima_fecha": ride_list[-1]["start_time"],
                "delta_fc_bpm": hr_delta,
                "delta_velocidad_kmh": spd_delta,
            }
        else:
            route["progreso"] = {"veces_rodada": len(ride_list)}

        route["historial"] = ride_list[-10:]  # últimas 10
        return route
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/route-compare/{route_id} ────────────────────────────────────────

@router.get("/gpt/route-compare/{route_id}")
def gpt_route_compare(route_id: str):
    """Comparador de ruta — primera vez, mejor tiempo, mejor FC, última vez, evolución %."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT route_id, name, distance_km, ascent_m FROM routes WHERE route_id = %s", [route_id])
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"route_id '{route_id}' no encontrado")
            route = {"route_id": row[0], "name": row[1], "distance_km": row[2], "ascent_m": row[3]}
            cur.execute("""
                SELECT session_id, start_time, duration_s, avg_hr_bpm, avg_speed_kmh, avg_cadence
                FROM sessions_clean_compat WHERE route_id = %s AND start_time IS NOT NULL
                ORDER BY start_time ASC
            """, [route_id])
            rides = cur.fetchall()
            cols = [d[0] for d in cur.description]
        if not rides:
            raise HTTPException(404, f"Sin sesiones para route_id '{route_id}'")
        ride_list = []
        for r in rides:
            rd = dict(zip(cols, r))
            for k, v in rd.items():
                if hasattr(v, "isoformat"): rd[k] = v.isoformat()
            ride_list.append(rd)
        valid_dur = [r for r in ride_list if r.get("duration_s")]
        valid_hr  = [r for r in ride_list if r.get("avg_hr_bpm")]
        valid_spd = [r for r in ride_list if r.get("avg_speed_kmh")]
        mejor_tiempo = min(valid_dur, key=lambda x: x["duration_s"]) if valid_dur else None
        mejor_fc     = min(valid_hr,  key=lambda x: x["avg_hr_bpm"]) if valid_hr  else None
        mejor_vel    = max(valid_spd, key=lambda x: x["avg_speed_kmh"]) if valid_spd else None
        primera = ride_list[0]
        ultima  = ride_list[-1]
        evolucion = {}
        if primera.get("avg_hr_bpm") and ultima.get("avg_hr_bpm"):
            d = ultima["avg_hr_bpm"] - primera["avg_hr_bpm"]
            evolucion["fc_delta_bpm"] = round(d, 0)
            evolucion["fc_pct"] = round(d / primera["avg_hr_bpm"] * 100, 1)
        if primera.get("avg_speed_kmh") and ultima.get("avg_speed_kmh"):
            d = ultima["avg_speed_kmh"] - primera["avg_speed_kmh"]
            evolucion["vel_delta_kmh"] = round(d, 1)
            evolucion["vel_pct"] = round(d / primera["avg_speed_kmh"] * 100, 1)
        if primera.get("duration_s") and ultima.get("duration_s"):
            d = ultima["duration_s"] - primera["duration_s"]
            evolucion["tiempo_delta_s"] = d
            evolucion["tiempo_pct"] = round(d / primera["duration_s"] * 100, 1)
        return {"ruta": route, "total_veces": len(ride_list), "primera_vez": primera,
                "ultima_vez": ultima, "mejor_tiempo": mejor_tiempo,
                "mejor_fc": mejor_fc, "mejor_velocidad": mejor_vel, "evolucion": evolucion}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/session-environment/{session_id} ─────────────────────────────────

@router.get("/gpt/session-environment/{session_id}")
def gpt_session_environment(session_id: str):
    """Environmental context for one clean session."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT se.*, cs.start_time, cs.name, cs.sport, cs.distance_km
            FROM session_environment se
            JOIN clean_sessions cs USING (clean_session_id)
            WHERE se.clean_session_id = %s
               OR cs.original_session_id = %s
               OR cs.source_activity_id = %s
            ORDER BY CASE WHEN se.clean_session_id = %s THEN 0 ELSE 1 END
            LIMIT 1
        """, (session_id, session_id, session_id, session_id))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Contexto ambiental no encontrado")
        columns = [description[0] for description in cur.description]
    result = dict(zip(columns, row))
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif hasattr(value, "as_tuple"):
            result[key] = float(value)
    return {
        "ok": True,
        "environment": result,
        "interpretation": {
            "altitude_band": "Absolute elevation above sea level.",
            "relative_altitude_band": "Difference versus this athlete's habitual altitude.",
            "acclimatization_status": "Estimated only from recent comparable training exposure.",
        },
    }


# ── DELETE /sessions/{session_id} ────────────────────────────────────────────

@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id=%s", (session_id,))
            cur.execute("DELETE FROM session_records WHERE session_id=%s", (session_id,))
        conn.commit()
        return {"ok": True}
    except Exception as e:
        conn.rollback(); raise HTTPException(500, str(e))


# ── GET /gpt/session-analysis/{session_id} ───────────────────────────────────

@router.get("/gpt/session-analysis/{session_id}")
def analyze_session_vs_profile(session_id: str):
    conn = get_db()
    if not conn: raise HTTPException(503,"DB no disponible")
    try:
        p = _get_profile(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT session_id,start_time,distance_km,duration_s,
                avg_hr_bpm,avg_speed_kmh,avg_cadence,ascent_m,workout_name
                FROM sessions_clean_compat WHERE session_id=%s""",(session_id,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        if not row: raise HTTPException(404,"Sesion no encontrada")
        s = dict(zip(cols,row))
        for k,v in s.items():
            if hasattr(v,'isoformat'): s[k]=v.isoformat()
            elif hasattr(v,'__float__') and v is not None:
                try: s[k]=float(v)
                except: pass
        with conn.cursor() as cur:
            cur.execute("""SELECT ROUND(AVG(avg_hr_bpm)::numeric,1),ROUND(AVG(avg_speed_kmh)::numeric,2),
                ROUND(AVG(avg_cadence)::numeric,1),ROUND(AVG(distance_km)::numeric,1),COUNT(*)
                FROM sessions_clean_compat WHERE sport='cycling'
                AND start_time::timestamp>=NOW()-'90 days'::interval AND session_id!=%s""",(session_id,))
            hrow = cur.fetchone()
        hist = {"avg_hr":float(hrow[0]) if hrow[0] else None,"avg_spd":float(hrow[1]) if hrow[1] else None,
                "avg_cad":float(hrow[2]) if hrow[2] else None,"avg_dist":float(hrow[3]) if hrow[3] else None,"n":hrow[4]}
        z = p.get("zonas_ciclismo",{})
        hr = s.get("avg_hr_bpm") or 0
        spd = s.get("avg_speed_kmh") or 0
        cadval = s.get("avg_cadence") or 0
        z2lo,z2hi = z.get("z2",[134,150])
        z3hi = z.get("z3",[151,160])[1]
        trans = z.get("transition",[109,133])
        if hr < trans[0]: zone_eval="Z1 recuperacion — muy facil"
        elif hr <= trans[1]: zone_eval="Transicion — calentamiento/enfriamiento"
        elif hr < z2lo: zone_eval="Z1 muy facil — sube intensidad"
        elif hr <= z2hi: zone_eval="Z2 perfecto — motor aerobico"
        elif hr <= z3hi: zone_eval="Z3 tempo — buen umbral"
        else: zone_eval="Z4/Z5 — alta intensidad, recupera"
        cad_obj = p.get("cadencia_obj",100)
        cad_eval = f"Cadencia {cadval:.0f} rpm — {'+' if cadval>cad_obj else ''}{cadval-cad_obj:.0f} vs obj {cad_obj}"
        eff = round(spd/hr,4) if hr>0 and spd>0 else None
        eff_base = p.get("eff_base",0.1483)
        eff_eval = f"Eficiencia {eff:.4f} ({'+' if eff and eff>eff_base else ''}{(eff-eff_base):.4f} vs base)" if eff else "Sin datos eficiencia"
        comp = []
        if hist["avg_hr"] and hr: comp.append(f"FC {'+' if hr-hist['avg_hr']>0 else ''}{hr-hist['avg_hr']:.1f} bpm vs hist")
        if hist["avg_spd"] and spd: comp.append(f"vel {'+' if spd-hist['avg_spd']>0 else ''}{spd-hist['avg_spd']:.1f} km/h vs hist")
        if hist["avg_cad"] and cadval: comp.append(f"cad {'+' if cadval-hist['avg_cad']>0 else ''}{cadval-hist['avg_cad']:.1f} rpm vs hist")
        return {"session":s,"analysis":{"zone":zone_eval,"cadence":cad_eval,"efficiency":eff_eval,
                "comparison":" · ".join(comp) or "Sin historial suficiente"},"historical":hist}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))


# ── GET /gpt/matched-rides/{session_id} ──────────────────────────────────────

@router.get("/gpt/matched-rides/{session_id}")
def get_matched_rides(session_id: str, min_distance_km: float = 15.0):
    """Encuentra sesiones históricas con distancia ±15% y ascenso ±20%."""
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT distance_km, ascent_m, avg_hr_bpm, avg_speed_kmh,
                       avg_cadence, duration_s, start_time::text, workout_name
                FROM sessions_clean_compat WHERE session_id=%s
            """, (session_id,))
            ref = cur.fetchone()
        if not ref:
            raise HTTPException(404, "Sesion no encontrada")

        dist, asc, hr_ref, spd_ref, cad_ref, dur_ref, dt_ref, name_ref = ref
        dist = float(dist or 0)
        asc = float(asc or 0)

        if dist < min_distance_km:
            return {
                "matched": [],
                "route_name": "Sesion corta filtrada",
                "route_label": f"Ruta corta ~{round(dist)} km",
                "count": 0,
                "status": "filtered_short_route",
                "message": f"Para bici se priorizan comparativas de {min_distance_km:g} km o mas. Puedes bajar min_distance_km si quieres revisar esta sesion."
            }

        dist_lo, dist_hi = dist * 0.85, dist * 1.15
        asc_lo = max(0, asc * 0.75)
        asc_hi = asc * 1.25 + 50

        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, start_time::text as dt,
                       distance_km::float, ascent_m::float,
                       avg_hr_bpm::float, avg_speed_kmh::float,
                       avg_cadence::float, duration_s::int,
                       workout_name
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND distance_km BETWEEN %s AND %s
                  AND (ascent_m IS NULL OR ascent_m BETWEEN %s AND %s)
                  AND session_id != %s
                ORDER BY start_time DESC
                LIMIT 20
            """, (dist_lo, dist_hi, asc_lo, asc_hi, session_id))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        matches = []
        for row in rows:
            m = dict(zip(cols, row))
            if m.get('avg_hr_bpm') and m.get('avg_speed_kmh') and m['avg_hr_bpm'] > 0:
                m['efficiency'] = round(float(m['avg_speed_kmh']) / float(m['avg_hr_bpm']), 4)
            else:
                m['efficiency'] = None
            dur = int(m.get("duration_s") or 0)
            m["duration_hms"] = f"{int(dur//3600):02d}h {int((dur%3600)//60):02d}m"
            matches.append(m)

        ref_eff = round(float(spd_ref) / float(hr_ref), 4) if hr_ref and spd_ref and float(hr_ref) > 0 else None

        route_label = f"~{round(dist)} km"
        if dist < 25: route_label = f"Ruta corta ~{round(dist)} km"
        elif dist < 40: route_label = f"Ruta media ~{round(dist)} km"
        else: route_label = f"Ruta larga ~{round(dist)} km"

        if matches:
            avg_spd_hist = sum(float(m['avg_speed_kmh'] or 0) for m in matches) / len(matches)
            avg_hr_hist = sum(float(m['avg_hr_bpm'] or 0) for m in matches if m['avg_hr_bpm']) / max(1, len([m for m in matches if m['avg_hr_bpm']]))
            avg_eff_hist = sum(m['efficiency'] for m in matches if m['efficiency']) / max(1, len([m for m in matches if m['efficiency']]))
            spd_delta = round(float(spd_ref or 0) - avg_spd_hist, 2) if spd_ref else None
            eff_delta = round(ref_eff - avg_eff_hist, 4) if ref_eff and avg_eff_hist else None
            verdict = "mejoraste" if (spd_delta and spd_delta > 0.3) else "similar" if (spd_delta and abs(spd_delta) <= 0.3) else "bajaste"
        else:
            avg_spd_hist = avg_hr_hist = avg_eff_hist = spd_delta = eff_delta = None
            verdict = "primera vez en esta ruta"

        return {
            "session_id": session_id,
            "reference": {
                "date": dt_ref, "distance_km": dist, "ascent_m": asc,
                "avg_speed_kmh": float(spd_ref or 0), "avg_hr_bpm": float(hr_ref or 0),
                "avg_cadence": float(cad_ref or 0), "efficiency": ref_eff,
                "workout_name": name_ref
            },
            "route_label": route_label,
            "matched": matches,
            "count": len(matches),
            "vs_historical": {
                "avg_speed_hist": round(avg_spd_hist, 2) if avg_spd_hist else None,
                "avg_hr_hist": round(avg_hr_hist, 1) if avg_hr_hist else None,
                "avg_eff_hist": round(avg_eff_hist, 4) if avg_eff_hist else None,
                "speed_delta": spd_delta,
                "efficiency_delta": eff_delta,
                "verdict": verdict
            }
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── GET /gpt/route-history ────────────────────────────────────────────────────

@router.get("/gpt/route-history")
def get_route_history(min_km: float = 5, weeks: int = 26):
    """Lista rutas agrupadas por distancia con progresión histórica."""
    conn = get_db()
    if not conn: raise HTTPException(503, "DB no disponible")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ROUND(distance_km::numeric) as dist_bucket,
                    COUNT(*) as n,
                    MIN(start_time::text) as first_date,
                    MAX(start_time::text) as last_date,
                    ROUND(AVG(avg_speed_kmh)::numeric, 2) as avg_speed,
                    ROUND(MAX(avg_speed_kmh)::numeric, 2) as best_speed,
                    ROUND(AVG(avg_hr_bpm)::numeric, 1) as avg_hr,
                    ROUND(MIN(avg_hr_bpm)::numeric, 1) as best_hr,
                    ROUND(AVG(avg_cadence)::numeric, 1) as avg_cad,
                    ROUND(AVG(ascent_m)::numeric, 0) as avg_asc
                FROM sessions_clean_compat
                WHERE sport='cycling'
                  AND distance_km >= %s
                  AND start_time::timestamp >= NOW() - (%s || ' weeks')::interval
                GROUP BY dist_bucket
                HAVING COUNT(*) >= 2
                ORDER BY n DESC, dist_bucket
            """, (min_km, weeks))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]

        routes = []
        for row in rows:
            d = dict(zip(cols, row))
            for k, v in d.items():
                if hasattr(v, '__float__') and v is not None:
                    try: d[k] = float(v)
                    except: pass
            km = d.get('dist_bucket', 0)
            if km < 25: d['label'] = f"Ruta corta ~{int(km)} km"
            elif km < 40: d['label'] = f"Ruta media ~{int(km)} km"
            else: d['label'] = f"Ruta larga ~{int(km)} km"
            routes.append(d)

        return {"routes": routes, "total": len(routes)}
    except Exception as e:
        raise HTTPException(500, str(e))
