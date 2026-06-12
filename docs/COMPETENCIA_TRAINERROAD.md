# Competencia — TrainerRoad (análisis clean-room)

> Ingeniería inversa **funcional**, solo con información pública (sitio, help center,
> blog, reviews). Sin código propietario, sin APIs privadas, sin decompilación.
> Fecha: junio 2026.

---

## 1. Mapa funcional de TrainerRoad

**Promesa central: "Get faster."** Rendimiento medible en ciclismo, con el FTP como moneda.

| Pieza | Qué hace (público) |
|---|---|
| **Plan Builder** | Usuario mete fecha de inicio, eventos/metas, disponibilidad → genera plan en calendario |
| **Adaptive Training** | Tras cada workout analiza ejecución (potencia, FC, survey) y ajusta los workouts FUTUROS del plan. No modifica el workout en sí: cambia cuál te recomienda |
| **Progression Levels** | Tu capacidad actual por zona (Endurance, Tempo, Sweet Spot, Threshold, VO2, Anaerobic, Sprint), escala 1–10. Sube/baja según qué workouts completas y qué tan bien |
| **Workout Levels** | Dificultad de cada workout (ML): repetición, longitud y formato de intervalos, recuperación. Etiquetas relativas a TU nivel: Recovery / Achievable / Productive / Stretch / Breakthrough / Not Recommended |
| **Post-Workout Survey** | Inmediatamente al terminar: "¿cómo se sintió?" Easy → Moderate → Hard → Very Hard → All Out. Calibra las adaptaciones |
| **TrainNow** | Sin plan: 3 opciones diarias (Endurance / Climbing / Attacking) con una marcada "Recommended" según el estrés reciente |
| **Outside Workouts** | Manda el workout al Garmin/Wahoo; rides de Strava entran al calendario |
| **Pricing** | ~$209.99 USD/año (~$17.45/mes). Producto maduro de suscripción |

### Flujo completo
`objetivo → plan → workout → ejecución → survey → adaptación`

## 2. Arquitectura conceptual (deducida, sin código)

- athlete profile (FTP, zonas, historial)
- goal/event → training plan → planned workout
- completed workout + survey response
- workout difficulty (ML) ↔ athlete progression level (por zona)
- adaptation decision → calendar state

## 3. Equivalentes Epoch

| TrainerRoad | Epoch | Estado |
|---|---|---|
| Progression Levels | **Capability Levels** (capacidades con score, confianza, evidencia) | ✅ existe (`/gpt/capacidades`) — falta framing "level" en UI |
| Workout Levels | **Session Demand** (qué tan demandante fue/será una sesión vs TU capacidad reciente) | 🔨 F2 |
| Adaptive Training | **Plan Vivo** (today-adaptation + week-rebalance + move) | ✅ existe |
| Post-workout survey | **Feedback Calibration** (👍/👎 + RPE post-sesión) | ◐ parcial (falta RPE 1-tap) → F3 |
| TrainNow | **Today Options** (2–3 opciones del día con razón) | 🔨 F4 (base: today-adaptation) |
| Calendar compliance | **Training Alignment** (matcher + cumplimiento sin culpa) | ✅ backend, falta UI → F5 |
| FTP Detection | **Fitness Signal** (lt-detect + eficiencia — señal, nunca centro) | ✅ existe (`/gpt/lt-detect`) — falta exponer |

## 4. Diferencias estratégicas (dónde gana Epoch)

TrainerRoad es **prescripción**: te dice qué hacer. Es fuerte indoor/potencia y débil en
contexto del mundo real. Las quejas públicas recurrentes: planes con demasiada intensidad,
sensación de sistema cerrado ("el algoritmo lo decidió"), experiencia centrada en el rodillo.

Epoch es **comprensión**:

1. **Contexto justo** — calor, viento, pausas, tráfico, modalidad, país. *El contexto explica; jamás califica.* TrainerRoad necesita potencia y condiciones limpias; Epoch es honesto en la calle real.
2. **No castigar la intención** — "Hoy no tocaba romper récord. Hoy tocaba construir motor aeróbico. La sesión fue buena porque respetaste la intención."
3. **Lectura con confianza declarada** — evidencia + confidence_reason + qué falta. Anti caja-negra.
4. **Laboratorios personales** — Atizapán Z2 contra Atizapán Z2; Irlanda con viento contra Irlanda con viento. Y con fallback para quien no repite rutas (clase de esfuerzo → línea base por intención).
5. **Plan vivo sin culpa** — "no fallaste; reacomodamos; tu cuerpo cuenta; el proceso sigue."

### Posicionamiento
> **TrainerRoad tells you the workout. Epoch explains the process.**

- TrainerRoad: *Structured training engine for cyclists.*
- Epoch: *Training understanding and adaptive context layer for athletes.*

### Ciclo propio de Epoch
`objetivo → plan vivo → cuerpo de hoy → contexto externo → ejecución → explicación → adaptación prudente`

## 5. Qué NO copiar (anti-metas)

- FTP como centro absoluto de todo
- Indoor-first
- Caja negra ("haz esto porque sí")
- Prescripción autoritaria
- Gamificación sin explicación
- Obsesión por "faster" como única promesa
- UI densa de entrenamiento técnico

## 6. Primera implementación recomendada (orden)

1. **F1 — Laboratorios generalizados**: jerarquía de comparación con fallback declarado
   (ruta exacta → clase de esfuerzo → línea base por intención). Resuelve al atleta que no repite rutas.
2. **F2 — Session Demand**: score de demanda por sesión desde laps/streams/desnivel/decoupling,
   relativo a la capacidad reciente del atleta. Formato Epoch completo (observación → … → sugerencia).
3. **F3 — Survey post-sesión**: RPE 1–10 + sensación en 2 taps. Alimenta calibración.
4. **F4 — Today Options**: 2–3 opciones del día (lo planeado / versión suave / descanso) con razón.
5. **F5 — Training Alignment**: cumplimiento visible, sin culpa.

### Archivos a modificar
- `routers/workout_identity.py` (F1: fallback en progression/groups)
- `routers/workout_identity.py` o nuevo `routers/session_demand.py` (F2)
- `routers/plan_vivo.py` + `static/screens/sesion.js` (F3 survey)
- `routers/plan_vivo.py` + `static/screens/home.js` (F4 Today Options)
- `static/screens/plan.js` (F5 alignment UI)
- `scripts/smoke_test.py` (needles nuevos por fase)

### Riesgos
- Sonar autoritario al dar "opciones del día" → siempre proponer, nunca ordenar
- Session Demand sin potencia es estimación → declarar confianza y método
- Fallback de laboratorios puede comparar peras con manzanas → bandas estrechas + confianza decreciente + declarar el nivel usado
- Más cards en UI ya cargada → cada feature entra colapsada (evd) y con una conclusión arriba

### Cómo probar
- Smoke: needles `comparison_level`, `demand`, `survey`, `options`
- Caso real: sesión strava_18758175501 (15 bloques) → demand alto esperado
- Caso fallback: cualquier sesión con ruta no repetida → debe leer en nivel 2 o 3 y declararlo

---

*Fuentes públicas: trainerroad.com (Adaptive Training, Workout Levels, Progression Levels,
TrainNow, Plan Builder, pricing), support.trainerroad.com, blog y reviews de terceros
(cyclistshub, road.cc, Cyclingnews).*
