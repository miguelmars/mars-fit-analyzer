# V7 — Epoch Experience (plan de acción, no implementación)

> Most athletes know what they did. Few understand what they built.
> V7 es donde la app deja de parecer un panel técnico y se convierte en Epoch.

## Dónde estamos (v6.5 cerró el motor)
Inteligencia lista: laps, workout-analysis, weekly-intelligence, training-context,
plan estructurado + matcher, countdown, capacidades humanas, formato
Observación→Interpretación→Capacidad→Evidencia→Confianza→Qué falta.
**El motor ya piensa Epoch. V7 hace que la experiencia lo muestre.**

---

## QUÉ FALTA DE ASOCIAR (cierres de data, en orden)

| # | Gap | Cómo cerrarlo | Esfuerzo |
|---|---|---|---|
| 1 | **Nombres reales Garmin del plan** ("5 Min. Tempo Intervals") | Corto plazo: captura semanal (screenshot→seed, 30 seg). Real: **Garmin API directa en V7** — trae nombre, steps y targets de cada workout automáticamente | captura: nada · API: 1-2 semanas |
| 2 | Meta del evento real | Registrar GF Izzi Kardias (11 oct) en Metas — 1 minuto, desbloquea countdown+readiness | 0 |
| 3 | Resto del calendario del plan (semanas 7-22) | Mismo que #1 — Garmin API lo resuelve completo | con #1 |
| 4 | Peso continuo (solo 1 semana) | Báscula → check matutino constante, o Garmin API (weigh-ins) | hábito |
| 5 | Laps históricos (quedan ~2,800) | `run_laps_backfill.py` 2-3 tiradas más | 2 días goteo |
| 6 | Streams sin consumidor (3,089 descargados) | `session_streams_summary`: decoupling, heat-effort, pausas reales | 1 semana |
| 7 | gear_id/suffer_score/max_watts tirados | Ampliar SELECT del transform → raw_json | 1 día |

**La decisión grande de V7: Garmin API directa.** Es el único camino a los nombres
reales del plan sin trabajo manual — y de paso trae sleep score, HRV, body battery,
training readiness. Todo lo que el Coach necesita para "¿cómo estoy hoy?" de verdad.

---

## EL CORAZÓN DE V7: CÓMO SE RELACIONA LA DATA

Este es el insight del fundador que ordena todo lo demás:

**El mismo workout se repite dentro del plan. Epoch debe reconocer cada
repetición y medir la evolución ENTRE repeticiones.**

### Workout Identity — agrupar repeticiones
Cada sesión obtiene una identidad por 3 llaves (en orden de confianza):
1. **Nombre Garmin** ("5 Min. Tempo Intervals", "Intervalos Zona 5") — vía API/captura
2. **Firma de estructura** (de session_laps: 5 works de ~5min con ~3min recovery = misma sesión aunque cambie el nombre)
3. **route_id** (ya existe en clean_sessions — mismas rutas, como Matched Rides de Strava pero explicado)

### Workout Progression — ¿mejoré en ESTE workout?
Agrupadas las repeticiones, comparar entre semanas del plan:
> "Semana 6: completaste 3 de 5 intervalos al target, el 4º se cayó (4:03, FC 171).
> Semana 8: los 5 completos, misma velocidad con 6 bpm menos.
> **Tu umbral está subiendo — esto es lo que Build estaba construyendo.**"
Métricas por repetición: intervalos completados, tiempo/velocidad por work,
FC al mismo esfuerzo, deriva entre el primero y el último, cadencia.
La data YA existe (session_laps); falta la llave de agrupación.

### Misma ruta ≠ misma sesión: la llave es ruta + INTENCIÓN
El atleta usa la misma ruta con propósitos distintos: un día Z2 relax, otro
intervalos Z3/Z4. **Strava agrupa solo por GPS y no sabe de qué fue la sesión —
solo le importa que "fuiste lento" y no te da medalla.** Ese es exactamente el
hueco de Epoch.

La llave de comparación correcta: **route_id + workout_type** (la intención ya
la detecta workout-analysis desde laps/zonas):
- Z2 en Atizapán se compara SOLO contra otros Z2 en Atizapán →
  la medalla es **menos FC a la misma velocidad** (aunque fueras "lento")
- Intervalos en Atizapán contra otros intervalos ahí →
  la medalla es **repeticiones completadas y velocidad en los works**
- Día lento intencional = ejecución correcta, no retroceso

> Strava tiene un solo leaderboard por ruta. Epoch tiene uno por intención —
> y te explica por qué tu día "lento" fue exactamente lo que tocaba construir.

### Phase Report Card — sin culpa, con evidencia
Garmin es estático: pasas de fase y solo ves "pasaste la fase 2". Epoch al cerrar
cada fase entrega el reporte: **"Base construyó esto en ti"** (volumen sostenido,
FC bajando a misma velocidad, X workouts progresados). Y si moviste o saltaste
sesiones: **"no pasa nada — esto es lo que importa esta semana"**. La culpa no
construye nada; la comprensión sí.

### Epoch Tests — ¿dónde estoy realmente?
Cuando cambia la fase o la confianza de los datos es baja, Epoch propone (no ordena)
una sesión de evaluación: "¿Quieres saber dónde está tu umbral? 20 min sostenidos
en esta ruta te lo dicen." Resultados alimentan zonas y capacidades con evidencia
fresca en vez de fórmulas viejas.

---

## V7 — LAS 6 PIEZAS (para que se vea cabrón)

### 1. My Plan — el plan vivo con nombres reales
Lo que Garmin muestra pero con alma Epoch:
- Anillo "Semana 6 de 22" + fases Base/Build/Peak con fechas y foco
- **Hoy: "5 Min. Tempo Intervals"** (nombre real) + *qué construye* (umbral + potencia)
- Semana con palomitas ✓ = matcher real plan-vs-ejecución (ya funciona)
- Al completar: "Cumpliste la sesión. Esto fue lo que construyó." → workout-analysis
**Diferencia vs Garmin:** Garmin te dice qué hacer; Epoch te dice qué estás construyendo.

### 2. Home por escenas (no pantalla única)
- **Mañana:** "¿Cómo estoy hoy?" + qué toca según el plan (nombre real del workout)
- **Post-entreno** (auto-sync detecta sesión nueva): "Tu rodada ya está. Construyó X." ← el momento wow
- **Noche:** recuperación + check + qué viene mañana
Misma pantalla, contenido cambia por hora del día y por si ya entrenaste. 0 endpoints nuevos.

### 3. Capacidades = columna vertebral (no pantalla aparte)
Las 8 capacidades humanas como perfil central: cada una con score, tendencia,
evidencia (qué sesiones la construyeron — capability_evidence) y qué la mejora.
El deporte cambia; el cuerpo que se adapta es el mismo.

### 4. Epochs — la línea de eras (el nombre de la marca, cobrado)
Con 2017–2026 ya transformado: detectar y nombrar tus épocas ("La era del maratón",
"El regreso", "La base 2026"). Cada era: qué construiste, qué perdiste, qué aprendiste.
**Nadie tiene esto. Strava te da un feed; Epoch te da tu historia.**

### 5. Event Readiness con gap real
"17 semanas para Izzi Kardias. El evento exige resistencia (160km) y
fuerza-resistencia (2,800m). Hoy: resistencia 72, fuerza-resistencia 58.
El plan Build de julio ataca exactamente ese gap." — countdown ya existe;
falta conectar demandas del evento ↔ scores ↔ fases del plan.

### 6. Speak Epoch (inglés) — al final, no al inicio
Cuando la app ya piense Epoch (1-5 listos), un pase de traducción de UI +
explanation_texts con `lang=en`. Hacerlo antes sería traducir un panel técnico.

---

## ORDEN PROPUESTO (4 sprints)

| Sprint | Entrega | Por qué primero |
|---|---|---|
| V7.0 | Garmin API directa (OAuth + workouts + wellness) | Desbloquea nombres reales, plan completo, sleep/HRV — todo lo demás lo consume |
| V7.1 ✅ | **Workout Identity + Progression** — progression, groups, trend, Laboratorios, nombrar rutas | HECHO |
| V7.2 ✅ | My Plan (/plan) + Phase Report Card + hoy-según-plan en Home | HECHO |
| V7.3 ✅ | Event Readiness gap (evento time_trial) + Capability Evidence + Epoch Tests | HECHO |
| V7.4 🟡 | Epochs (eras) HECHO · EN pospuesto (Think Epoch first, por decisión del fundador) · dominio = acción manual | parcial |

**Regla de todos los sprints:** ADR Insight-First — comprensión → evidencia → detalle.
Cada pantalla abre respondiendo su pregunta humana, nunca con un grid de métricas.

## Riesgos honestos
- Garmin API: requiere aprobación de developer program (semanas de espera — solicitar YA aunque V7.0 no empiece)
- Epochs necesita laps backfill completo + bloques de 12 semanas ya calculados (existen)
- No empezar V7.1 sin cerrar gaps #1-2 — sin plan completo, My Plan se ve vacío
