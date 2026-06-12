# Epoch v6.5 — Data Loss Map & Workout Intelligence

## Data Loss Map (auditoría 2026-06-10)

| Data | FIT (Garmin) | Strava staging | clean_sessions | Estado |
|---|---|---|---|---|
| HR/vel/cadencia (avg/max) | ✅ | ✅ | ✅ | Conservada |
| moving vs elapsed (pausas) | ✅ | ✅ | ✅ ambos | Conservada |
| GPS start/end + route_id | ✅ | ✅ start | ✅ | Conservada (resumen) |
| Laps/intervalos | extrae, NO persiste | strava_laps_raw (poblándose) | session_laps ✅ | Parcial → en curso |
| Streams completos (HR/GPS/cad/watts/temp/grade/alt/moving) | session_records (FIT) | strava_streams_raw ✅ | — | **Existe y NADIE lo lee** |
| suffer_score (load Strava) | — | ✅ | ❌ transform lo tira | Recuperable de staging |
| max_watts, device, gear_id, elev_high/low | — | ✅ | ❌ | Recuperable de staging |
| temperature, grade, altitude perfil | — | streams ✅ | ❌ | Recuperable de staging |
| Training effect, workout steps, time-in-zone Garmin | FIT lo trae, parser no lo extrae | — | — | No disponible aún |

**Clave:** nada está perdido permanentemente — el transform pone `raw_json=None` pero
staging (Supabase) conserva todo. El mayor desperdicio: streams completos de ~3,000
actividades descargados y sin un solo consumidor.

## Top 10 datos por valor Epoch
1. Laps/intervalos → todo Workout Intelligence (en curso)
2. Streams HR → decoupling aeróbico, drift, time-in-zone real
3. stream_altitude/grade → calidad de subidas, VAM por bloque
4. suffer_score → proxy de carga percibida histórica (gratis: 1 campo)
5. stream_temp → esfuerzo ajustado por calor (CDMX)
6. stream_moving → pausas reales, endurance ajustada
7. max_watts → picos de potencia/sprint
8. gear_id → km por bici reales (Gear screen)
9. device → confianza de sensor por fuente
10. FIT laps → intervalos de uploads Garmin directos

## Qué preservar primero
1. **Laps** (en curso: backfill-laps → transform-laps)
2. **suffer_score + max_watts + device + gear_id**: ampliar SELECT del transform y
   guardarlos en clean_sessions.raw_json (JSONB ya existe, cero migración) — propuesta.
3. **Streams**: NO copiar; crear `session_streams_summary` (decoupling, time-in-zone,
   temp media, pausas) calculado bajo demanda por lotes — propuesta fase 2.

## 5 análisis que se desbloquean
| Análisis | Data | ¿Existe? | Dificultad |
|---|---|---|---|
| Interval Quality Score | session_laps | ✅ | **Implementado v6.5.1** |
| Cadence fade (fatiga neuromuscular) | session_laps | ✅ | **Implementado v6.5.1** |
| HR recovery entre intervalos | session_laps | ✅ | Implementado v6.5 |
| Decoupling aeróbico (drift 1ª vs 2ª mitad) | stream_heartrate+velocity | ✅ staging | Media — fase 2 |
| Heat-adjusted effort | stream_temp | ✅ staging | Media — fase 2 |

## Workout Intelligence v6.5.1 (implementado hoy)
`GET /gpt/session/{id}/workout-analysis` ahora incluye:
- `interval_quality`: score 0-100 por consistencia entre bloques work (CV de duración/FC/velocidad)
- `cadence_fade`: caída de rpm primer→último bloque work (umbral 8 rpm)
- `verdict`: `went_well[]` / `degraded[]` / `missing_context[]` — qué salió bien,
  qué se degradó, qué falta para leer mejor
- `explanation_text` enriquecido con calidad y cadencia

`GET /gpt/data-coverage` (nuevo): cobertura por campo en clean_sessions, cobertura de
laps, y lista honesta de campos en staging que el transform no lleva.

## Estado del backfill de laps
- Batch 1: 100 actividades → 612 laps → session_laps ✅ (24/36 sesiones 4w con laps)
- Batches siguientes: 429 persistente — el auto stream backfill de main.py consume la
  cuota Strava continuamente. Reanudar laps cuando streams termine, o correr de
  madrugada. Pending: 2,989.
