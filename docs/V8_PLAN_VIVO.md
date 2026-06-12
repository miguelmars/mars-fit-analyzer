# V8 — El Plan Vivo (propuesta)

> Garmin te da un plan estático: solo puedes mover sesiones de día, y si saltas
> una, sientes culpa. — el fundador, definiendo exactamente el problema.

## La idea en una frase
**V8 convierte el plan estático de Garmin en un plan que respira contigo:**
cada mañana, Epoch cruza cómo amaneciste con lo que toca construir, y adapta
la semana — sin culpa, explicando por qué, sin romper el plan.

## Por qué es LA mejora cabrona
- V6 entendió tu pasado. V7 entendió tu presente y tu plan. **V8 cierra el ciclo:
  el futuro inmediato.** Es la evolución natural, no una feature suelta.
- Nadie lo hace: Garmin no se adapta; TrainingPeaks te regaña; Strava ni se entera.
  **Adaptación encima de un plan existente, con explicación honesta = categoría propia.**
- Usa TODO lo ya construido (cero data nueva): wellness check, weekly-intelligence,
  plan_sessions + matcher, fases, readiness, workout identity, streams (deriva/calor).

## Las 3 piezas

### 1. La adaptación de hoy — GET /gpt/today-adaptation
Entradas: check matutino (FC reposo vs tu baseline, sueño, fatiga, estado) ·
sesión de ayer (carga, deriva cardiaca) · cumplimiento de la semana · fase y foco.
Salida (formato Epoch completo):
> "Hoy el plan dice **5 Min Tempo Intervals**. Amaneciste con FC reposo 8 arriba
> de tu base y 5h de sueño. SUGERENCIA PRUDENTE: muévelo — hoy Z2 suave 45 min
> sostiene el proceso; el tempo cabe el jueves. La fase Base no se rompe por
> escuchar al cuerpo: se construye mejor."
Guardas honestos: sin check matutino → "no puedo leer cómo amaneciste" + CTA de
20 segundos. Sin plan → solo lectura del día. **Siempre propone, nunca ordena.**

### 2. La semana que se reacomoda — POST /gpt/week-rebalance (propuesta, no auto)
Si saltaste/moviste una sesión: Epoch propone dónde cabe de forma realista
(días restantes, carga acumulada, no 2 días duros seguidos). Un tap = aceptar
(actualiza plan_sessions, status='moved', historial intacto). El mensaje clave:
**"no pasa nada — así queda la semana ahora"**. Anti-culpa por diseño.

### 3. La proyección al evento — GET /gpt/event-projection
Con la trayectoria de capacidades (snapshots semanales ya existen) + las fases
restantes del plan: "A este ritmo llegas al Time Trial con readiness ~78.
El Build de julio es donde más se juega — protege esas semanas."
Honesto: banda de incertidumbre + confianza declarada, no promesa.

## UI (sigue la guía v1.0)
- **Home escena mañana** se vuelve accionable: check → adaptación de hoy con
  un CTA ("mantener plan" / "ajustar como sugieres").
- **Mi Plan**: la semana muestra sesiones movidas con su razón ("movida el mar 11:
  FC reposo alta") — la historia de adaptación queda registrada.
- **Coach**: la proyección al evento como card mensual, no diaria.

## Mejoras menores detectadas en la revisión (quick wins, no son V8)
1. **Feedback loop de confianza**: 👍/👎 en cada lectura ("¿te sonó cierta?") →
   tabla feedback → afinar heurísticas con TU criterio. Barato y oro puro.
2. Completar backfills (laps ~2,800 · streams) — los motores leen mejor con todo.
3. Nombrar rutas habituales (✏️ ya existe) — mejora todos los textos.
4. Coach aún calcula su lectura con reglas v6.4 (Z2%, eficiencia) — debería
   consumir weekly-intelligence + today-adaptation como fuente única (V8 lo hace).
5. gpt_training_context.py ya pesa mucho — separar en módulos al entrar V8.

## Qué NO es V8
No es IA generativa improvisando planes. No toca el plan de Garmin en Garmin.
No prescribe sin contexto. No es comunidad ni multiusuario (eso sigue siendo V9+).

## Riesgo principal y mitigación
Adaptar mal = perder confianza. Mitigación: reglas conservadoras y transparentes
(umbral de FC reposo +7, sueño <6h, fatiga ≥7), siempre con el porqué visible,
y el feedback loop (quick win #1) midiendo si las sugerencias te suenan ciertas.

## Orden propuesto
| Fase | Entrega |
|---|---|
| V8.0 ✅ | today-adaptation + Home mañana accionable + feedback 👍/👎 |
| V8.1 ✅ | week-rebalance (propuesta + aceptar con un tap) |
| V8.2 ✅ | event-projection + card en Mi Plan |
| V8.3 ✅ | Coach unificado: la adaptación de hoy es la lectura cuando existe |

## Anexo — Contexto Ambiental Completo (historia del fundador, 2026-06-10)

> "Aunque hagas la misma ruta, con calor tu FC no es la misma... y cuando hace
> fresco vas más rápido y me marcas que fui mejor — pero no es justo. Garmin
> tiene una métrica de temperatura que nunca explicó. Yo quiero darle sentido.
> Y en Irlanda lo que afecta es el viento."

### Ya implementado (con esta historia)
- **Progression justa por clima**: si la sesión fue ≥5°C más caliente que tus
  repeticiones, un mal número se lee como "condiciones_distintas", no retroceso.
  Si fue ≥5°C más fresca, la mejora se matiza ("parte puede ser el clima").
  El clima NUNCA mancha la tabla — queda como antecedente que explica.
- **Temperatura con sentido** en Sesión detalle: qué es (sensor del Garmin),
  cómo afecta (calor → FC alta + gasto en enfriarse; fresco → FC cuesta subir,
  más velocidad) y para qué la usa Epoch (contexto, no castigo).
- Coach ya lee calor de la última sesión ("no todo es fatiga").

### V8.x — Clima completo + ubicación (propuesta)
1. **Enriquecimiento meteorológico**: con start_lat/lon + fecha (ya existen en
   cada sesión), la API gratuita de Open-Meteo (histórico, sin key) da viento,
   ráfagas, humedad y condición (sunny/cloudy/rain). Batch idempotente tipo
   summarize-streams → columnas nuevas en session_streams_summary.
   **Esto desbloquea Irlanda**: "ese día 38 km/h de viento en contra — tu
   velocidad baja era el viento, no tú."
2. **Ubicación como contexto**: país/región desde lat/lon (geocoding offline
   por bounding boxes — MX/IE/US no necesita API). Las rutas se agrupan por
   lugar; el historial dice "época de Irlanda" y las comparaciones no cruzan
   geografías sin avisarlo.
3. **Heat-adjusted reading** (no score nuevo): la eficiencia se reporta con su
   temperatura al lado; las tendencias de Laboratorios marcan qué repeticiones
   fueron con calor/fresco/viento para leer la curva con honestidad.
Principio rector: **el clima explica, jamás califica.**

### Tercera pata del contexto: MODALIDAD Y TERRENO (fundador, 2026-06-10)
> "En la calle influye tráfico, topes... no es lo mismo que rodillo, ahí los
> datos son limpios. Y ruta no es MTB ni gravel — solo decir 'ciclismo' no basta."

Implementado:
- **Modalidades separadas siempre**: Ride (calle), VirtualRide (rodillo),
  MountainBikeRide, GravelRide nunca se comparan entre sí (la llave de
  progresión ya incluye sport_type) — y ahora la lectura LO DICE.
- **Pausas reales = tráfico/topes**: del stream `moving`. La lectura explica:
  "la calle cortó la rodada N veces — Epoch usa velocidad en movimiento,
  el semáforo no te castiga".
- **Rodillo etiquetado "datos limpios"** — comparación directa.
- Sesión detalle: chip de modalidad + explicación de pausas.

La evaluación justa completa = **misma ruta + misma intención + misma modalidad,
con clima y desnivel como contexto declarado.** HECHO V8.x: viento (Open-Meteo, gratis) y país/región (geocoding offline MX/IE/GB/US/ES).
