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

## UI v2 — Sprint 1 (del blueprint UI VERSION 2)

| # | Feature | Detalle |
|---|---------|---------|
| U1 | Calidad de lectura (Lab vs Field) | `reading_quality` high/medium/low en streams-summary. Rodillo=controlada; exterior se grada por pausas/calor/viento. "Epoch no juzga una sesion como laboratorio cuando ocurrio en el mundo real" |
| U2 | Chips de contexto en hero de sesion | urban·stops / heat / wind / reading quality — context before judgment |
| U3 | Mi Semana adaptativa | Sin plan: "Free week" con weekly-intelligence (que construiste, capacidad dominante, barras) — JAMAS habla de cumplimiento. Con plan: Plan Vivo. Sin modos: la app se adapta a las capas disponibles |
| U8 | Navegacion | My Plan → My Week ("What am I building this week?") |

SW: epoch-v11. Smoke: +1 (60 lineas).
## UI v2 — Sprint 2

| # | Feature | Detalle |
|---|---------|---------|
| U4 | Plan Vivo v2 — semana dia por dia | Estructura Garmin (Mon-Sun, colores por intencion) + interpretacion Epoch: estados sin regano ("not done — the week does not break; it reduces one stimulus"), dias de descanso ("rest · absorbing load"), resumen semanal Epoch (hecho X de Y · construyo · confianza) + frase de la semana |
| U5 | Home por capas de contexto | Strip Data → Goal → Plan → Check today con "siguiente capa util". Valor primero, precision despues — nunca exige contexto completo |

## UI v2 — Sprint 3

| # | Feature | Detalle |
|---|---------|---------|
| U6 | Body Map | `/gpt/body-map`: molestias activas (rojo) + terapias 7d (verde) por zona. SVG frontal/posterior con zonas tocables (hombros, espalda alta/baja, cadera, cuads, rodillas, gemelos, pies) — tap pre-selecciona la zona para registrar. Usa las columnas existentes pain_zone/muscle_zone/compex |
| U7 | Variante de entorno en comparaciones | progression anota cuando hoy fue urbano y las repeticiones fueron camino abierto (o inverso): "part of the speed gain may be fewer stops, not just fitness" |

UI v2 del blueprint: COMPLETA (U1-U8). Smoke: +1 (61 lineas).

## Bloque A — Estabilizacion y seguridad (pre-multiusuario)

| Medida | Detalle |
|---|---|
| Auth en escrituras | Middleware: todo POST/PUT/DELETE/PATCH exige X-Epoch-Key == env EPOCH_API_KEY. Exento: webhook Strava. Sin la env var: permite con warning (migracion suave). La llave se guarda en Perfil (localStorage), viaja via patch de fetch en config.js — nunca vive en el codigo |
| Rate limit | 240 req/min por IP, ventana deslizante en memoria → 429 |
| Limite de subida | 30 MB → 413 |
| CORS restringido | Solo origenes propios (Railway, app.useepoch.app, localhost) |
| Anti-XSS | esc() en utils.js aplicado a todo texto libre en innerHTML (metas, notas, descripciones, nombres de sesion/plan/rutas) |
| 25 rutas duplicadas | Los 4 routers sombreados (gpt_dashboard/coaching/history/patterns) DESREGISTRADOS — gpt_analytics ya servia todo |
| Bug muscle_groups | /api/fuerza-records consultaba muscle_group (no existia); el except lo silenciaba devolviendo vacio |
| Tests endurecidos | 500=crash ya NO pasa (503 honesto si). Destapo 4 rutas que crasheaban sin staging → get_supabase ahora degrada a 503. Test de idioma corregido. 156/156 verdes |
| Dependencias fijadas | requirements.txt con versiones == |

PENDIENTE EN RAILWAY (Bloque B): configurar la variable EPOCH_API_KEY; despues pegar la misma llave en Perfil → Access key. El smoke la lee de la misma env var.
