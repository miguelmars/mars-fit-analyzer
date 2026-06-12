# EPOCH — Documento de Proyecto
### Estado, diferenciación, roadmap y necesidades · Junio 2026

> **Most athletes know what they did. Few understand what they built.**
> La mayoría de los atletas saben qué hicieron. Pocos entienden qué construyeron.

---

## 1. Resumen ejecutivo

**Epoch es una plataforma de inteligencia de entrenamiento que traduce datos
deportivos en comprensión.** No compite mostrando más métricas — gana explicando
qué significa cada entrenamiento, qué capacidad construyó, con qué evidencia y
qué tan confiable es la lectura.

Estado: **producto funcional en producción** (Railway), usado diariamente por su
fundador con 9 años de historia real (3,089 actividades, 2017–2026). Motor de
inteligencia completo, UI insight-first, plan adaptativo operando.

## 2. El problema

Garmin, Strava y TrainingPeaks generan datos, no comprensión:
- **Strava** compara tus rodadas por GPS sin saber la intención: castiga tu día
  suave de Z2 como si fuera un retroceso.
- **Garmin** da planes estáticos: si saltas una sesión, sientes culpa; pasas de
  fase y nadie te dice qué construiste. Tiene métricas (temperatura, training
  load) que jamás explica.
- Ninguno responde las preguntas reales del atleta: *¿Cómo estoy hoy? ¿Mejoré
  en ESTE entrenamiento? ¿Voy alineado al plan? ¿Llegaré listo a mi evento?*

El conocimiento deportivo existe, pero es una barrera: vive en papers, coaches
caros y dashboards crípticos. **Knowledge should not be a barrier** es el
principio fundacional de Epoch.

## 3. La solución — una categoría propia

Epoch no organiza el mundo por deportes sino por **capacidades humanas** (motor
aeróbico, resistencia, potencia, fuerza-resistencia, recuperación, consistencia).
El deporte cambia; el cuerpo que se adapta es el mismo.

Toda lectura sigue el formato Epoch:
**Observación → Interpretación → Capacidad construida → Evidencia → Confianza → Qué falta → Sugerencia prudente.**
Si faltan datos, Epoch lo declara en lugar de inventar. La confianza es parte del producto.

## 4. Qué está construido (verificable hoy en producción)

| Capa | Capacidades |
|---|---|
| **Datos** | Pipeline Strava completo (3,089 actividades), laps/intervalos, streams (FC/GPS/cadencia/potencia/temperatura), archivos FIT de Garmin, clima histórico (Open-Meteo) + país por ruta |
| **Workout Intelligence** | Análisis por bloques: intervalos, calidad de ejecución (0-100), deriva cardiaca, caída de cadencia, degradación honesta (distingue fatiga real de terreno/clima) |
| **Workout Identity** | Comparación justa: misma ruta + misma intención + misma modalidad. El Z2 se compara con Z2; el día con viento de 38 km/h no "mancha" la tabla — se explica |
| **Plan Vivo** | Plan Garmin como dato (22 semanas, fases), matcher automático plan↔ejecución, **adaptación diaria según cómo amaneció el atleta** (FC reposo, sueño, fatiga), semana que se reacomoda sin culpa, proyección al evento con banda de incertidumbre |
| **Coach** | Explica, no ordena. Countdown al evento, qué construyó la semana, gap de capacidades vs demandas del evento, tests de auto-evaluación propuestos |
| **Épocas** | La historia atlética como eras ("la era del maratón", "el regreso") — 9 años narrados, con pausas incluidas sin culpa |
| **UX** | Insight-first (cada pantalla abre con una respuesta humana), móvil/PWA, el color de la interfaz sigue la fase de entrenamiento del usuario, feedback 👍/👎 que calibra las heurísticas |
| **Confianza** | Sección legal (privacidad, descargo, métodos declarados), datos auto-reparables (backfills idempotentes), smoke test de 51 puntos |

## 5. Diferenciadores defendibles

1. **Comparación por intención** — nadie más distingue el día Z2 a propósito del día malo.
2. **Contexto justo** — calor, viento, tráfico, modalidad: explican, jamás califican.
3. **Plan adaptativo sobre planes existentes** — no reemplaza a Garmin Coach: lo hace respirar. Sin culpa por diseño.
4. **Honestidad como feature** — confianza declarada y "qué falta" en cada lectura. Anti-IA-que-opina.
5. **Capacidades humanas como modelo** — extensible a cualquier deporte sin rehacer el producto.

## 6. Arquitectura técnica (resumen)

FastAPI (Python) + PostgreSQL (Railway) + Supabase (staging de datos crudos) +
PWA vanilla JS (sin frameworks pesados — carga instantánea en móvil).
Integraciones: Strava API (webhook + polling de respaldo), FIT parser propio,
Open-Meteo. Todo backfill es idempotente, con dry-run y límites de cuota.
Costo de infraestructura actual: **< $20 USD/mes.**

## 7. Estado honesto (lo que un inversionista debe saber)

- **Hoy es single-user**: el fundador es usuario cero con datos reales. Auth,
  multiusuario y onboarding NO existen aún — es el trabajo de la siguiente etapa.
- Las heurísticas son v1: conservadoras, transparentes, en calibración activa
  mediante feedback del usuario real.
- La app opera en español; la marca está definida en inglés (manifiesto, voz,
  filosofía documentados). La traducción es un sprint planificado.
- Garmin API directa: solicitada (desbloquea nombres reales de workouts, sueño,
  HRV automáticos).

## 8. Roadmap

| Horizonte | Entrega |
|---|---|
| **Inmediato** | Speak Epoch (app en inglés) · Garmin API directa · calibración de heurísticas |
| **3-6 meses** | Multiusuario + onboarding ("usuario no-fundador puede entrar") · beta cerrada con ciclistas/corredores · revisión legal formal (privacidad/GDPR) |
| **6-12 meses** | Más deportes vía capacidades (running ya parcial, fuerza, natación) · narrativa Epoch (tu historia contada) · modelo de suscripción |

## 9. Qué se necesita

1. **Capital semilla** para: desarrollo full-time (multiusuario, mobile-polish,
   Garmin API), revisión legal/privacidad, y 6-12 meses de pista.
2. **Beta testers** atletas amateur comprometidos (el usuario definido: quien
   pregunta *por qué*).
3. **Advisor** en ciencias del deporte para validar/evolucionar heurísticas.

Uso de fondos estimado: 70% producto/ingeniería · 15% legal y cumplimiento ·
15% comunidad beta y validación.

## 10. Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| Dependencia de APIs (Strava/Garmin) | Datos propios en staging + FIT directo del dispositivo |
| Heurísticas incorrectas erosionan confianza | Confianza declarada + feedback loop + advisor científico |
| Gigantes copian la idea | La voz honesta y el modelo de capacidades son cultura de producto, no un feature copiable |
| Single-founder | Documentación exhaustiva (ADRs, guías de UI, roadmaps versionados en el repo) |

## 11. La tesis en una línea

Los wearables ganaron: todos tienen datos. **Nadie tiene comprensión.**
Epoch es la capa de comprensión — y empieza por la pregunta más humana del
deporte: *¿qué está construyendo todo este esfuerzo?*

---
*Documento generado del estado real del repositorio · v6.4→V8 completas ·
producción: Railway · contacto: Miguel Ángel Mars*
