# V9 — Competitive Layer (post análisis TrainerRoad)

Origen: docs/COMPETENCIA_TRAINERROAD.md. Todo propone, nada ordena.

| # | Feature | Endpoint | UI |
|---|---------|----------|-----|
| V9.1 | Laboratorios generalizados — fallback declarado: route lab (high) → effort class (medium: intención + modalidad + distancia ±25% + terreno flat/rolling/climbing) → intent baseline (low, sin veredictos negativos) | `/gpt/session/{id}/progression` (`comparison.level`) + `effort_classes` en `/gpt/workout-groups` | chip de nivel en Sesión |
| V9.2 | Session Demand — demanda 0-100 relativa a TUS últimos 60 días. Componentes: duración (percentil) 35% · intensidad (FC vs LT) 35% · desnivel 15% · estructura 15%. Modificadores: decoupling >7%, calor ≥28°C | `/gpt/session/{id}/demand` | card con barras en Sesión |
| V9.3 | Survey post-sesión — fresh/normal/tough/very_tough/emptied (1 tap, RPE implícito). Calibra percibido vs medido (gap ≥25 se reporta) | `POST/GET /api/session/{id}/survey` | botones en Sesión |
| V9.4 | Today Options — 2-3 opciones (plan / Z2 / recovery) con razón y demanda estimada. Reglas: ≥2 flags→recovery · 1 flag→suavizar · 0 flags→plan · ≥2 días duros en 7→Z2 | `/gpt/today-options` | card en Home |
| V9.5 | Training Alignment — completed/moved/skipped por semana. "Mover no es fallar" | `/gpt/training-alignment` | card en My Plan |

SW cache: epoch-v9. Smoke: +6 checks (total esperado 53 + redirect = 54 lineas).
Tablas nuevas (lazy): session_surveys.

## V10 (continuación)

| # | Feature | Endpoint | UI |
|---|---------|----------|-----|
| V10.1 | Fitness Signal — lt-detect expuesto: señal de umbral vs zonas actuales, confianza declarada, "a signal, never the center" | `/gpt/lt-detect` (ya existía) | card en Evolution (modo bici/correr) |
| V10.2 | Calendario completo del plan — todas las semanas con fase (color), sesiones con estado (done/moved/skipped), semana actual resaltada. `focus` de fases se traduce al leer (el meta en DB quedó en español) | `sessions` agregado a `/gpt/training-plan` | evd colapsado en My Plan |

Smoke: +2 checks (total 56 lineas).
Pendientes V10: cierre del loop Today Options (V10.3) · Capability Levels (V10.4) · Plan Builder (V10.5, decide multiusuario) · Life events (V10.6).

| V10.3 | Cierre del loop — lo sugerido se registra (today_options_log) y al dia siguiente se compara con lo que el cuerpo hizo. followed / chose_different / rested — todo calibra, nada culpa | `/gpt/options-outcome` | linea en card de opciones (Home) |
| V10.4 | Capability Levels — nivel 1-10 por capacidad + "next level (X pts away)" desde el indicador con mas terreno por ganar (gap x peso) | `/gpt/capability-levels` | chip Lv + next step en Capacidades |

SW: epoch-v10. Smoke: +2 (57 checks).

| V10.5 | Plan Builder multiusuario — plan desde la EVIDENCIA del atleta (volumen real 6 sem → arranque · mejor bloque 12 sem → techo). Escribe el mismo esquema que lee todo el Plan Vivo. athlete_id agregado. Preview por defecto, confirmar explicito. base 45/build 30/peak 15/taper 10, progresion 3+1 | `POST /api/plan-builder` (dry_run=true default, replace para retirar el activo conservando historia) | form en My Plan (rama sin plan) |
| V10.6 | Life events — travel/illness/work/family. El plan propone movimientos ANTES de que las sesiones se vuelvan skipped. today-options respeta el evento activo | `POST/DELETE /api/life-event` · `/gpt/life-events` · `/gpt/life-event-impact` | evd "Life happens" en My Plan |

Smoke: +2 (59 lineas). Router nuevo: routers/plan_builder.py (registrado en main.py).
