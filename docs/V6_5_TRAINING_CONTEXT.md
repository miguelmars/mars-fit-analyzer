# Epoch v6.5 — Training Context & Workout Intelligence

## Diagnóstico (auditoría 2026-06-10)

**Lo que un vistazo superficial no muestra:** los intervalos YA existen.
- `strava/sync.py` descarga laps con `fetch_laps=True` → `strava_laps_raw` (Supabase staging).
- El FIT parser (`decode_fit.py:extract_laps`) extrae laps en cada upload.
- **Pero nadie los transforma**: `clean_sessions` solo guarda agregados; la migración
  staging dice literalmente "strava_laps_raw → sin tabla Mars equivalente".
- El "plan" (`plan_garmin` en mars_context) es texto declarativo: sin semanas, sin
  sesiones, sin fechas. `adaptive-coach` deriva la fase solo de semanas-al-evento.

**Conclusión:** Epoch no entiende el entrenamiento actual porque (a) la estructura
intra-sesión se tira en el staging, y (b) el plan no es dato, es prosa.

## Arquitectura v6.5 (mínima, aditiva)

```
session_laps      ← strava_laps_raw (transform manual idempotente)  [HECHO]
training_plans    ← plan activo como dato: semanas, fechas, fuente  [TABLA LISTA]
plan_sessions     ← sesiones planificadas; matched_clean_session_id [TABLA LISTA]
```

### Endpoints (routers/gpt_training_context.py) — deployables ya
- `GET /gpt/training-context` — plan, semana, meta, qué toca hoy, última sesión,
  cobertura de laps y `data_gaps` explícitos. Read-only.
- `GET /gpt/session/{id}/laps` — intervalos con zone_label (z1–z5) y lap_type
  (work/recovery/steady, heurística vs FC promedio de la sesión).
- `POST /api/strava/transform-laps?batch=200` — staging → session_laps.
  Manual, idempotente (ON CONFLICT DO NOTHING). Re-ejecutar hasta 0.

### Flujo actual vs planificado (fase 2 — no implementado)
1. Registrar plan: INSERT en training_plans + plan_sessions (UI en Perfil o seed SQL).
2. Matcher: al transformar una sesión, buscar plan_session de esa fecha/semana
   → set matched_clean_session_id + status=completed.
3. Coach lee /gpt/training-context: si hay plan estructurado prescribe contra el plan;
   si no, mantiene "contexto incompleto" (v6.4).

## Orden de implementación
1. **Hoy (hecho):** tablas aditivas + transform laps + endpoint contexto.
2. Ejecutar transform-laps en prod (manual, lotes) → valida cobertura.
3. Seed del plan Garmin Coach actual como training_plan real (12 filas aprox).
4. Matcher sesión↔plan + Coach v2 leyendo training-context.
5. FIT upload: persistir laps de archivos FIT también (hoy solo respuesta API).

## Para después (no ahora)
Garmin API directa · workout steps target (FIT workout messages) · UI de plan en
Perfil · análisis de cumplimiento por bloque (% intervalos completados).

## Riesgos
- `transform-laps` consume cuota Supabase (lecturas paginadas) — correr en horas valle.
- Heurística work/recovery es v1: suficiente para "detectó estructura", no para
  validar targets de potencia.
- Ninguna tabla nueva toca datos existentes; rollback = DROP de 3 tablas vacías.

## Runner de backfill de laps (v6.5.2)

`scripts/run_laps_backfill.py` — completa el historial sin vigilarlo manualmente:

```bash
python3 scripts/run_laps_backfill.py --dry-run          # estimar, 0 escrituras
python3 scripts/run_laps_backfill.py --max-cycles 3     # default seguro
python3 scripts/run_laps_backfill.py --max-cycles 15 --sleep-minutes 16  # tirada larga
```

Comportamiento: batch de 100 → transform 500 → espera 16 min → repite.
En 429 espera y reintenta una vez; si persiste (límite diario) se detiene solo.
Se detiene si pending no baja en 2 ciclos o ante errores no-429.
Ctrl+C es seguro: todo es idempotente. Nunca toca clean_sessions.

Verificar cobertura: `GET /gpt/training-context` → laps_coverage, o `GET /gpt/data-coverage`.
