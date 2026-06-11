# ADR — Insight First (Epoch Experience)

**Estado:** aceptado · 2026-06-10
**Fuente:** Epoch Brand Foundation v3.0, Product Philosophy, "epoch vision informacio"

## Regla

Epoch no abre con métricas. Abre con comprensión.

Toda pantalla responde una **pregunta humana** antes de mostrar evidencia:

| Pantalla | Pregunta | NO es |
|---|---|---|
| Home | ¿Cómo estoy hoy? | ¿Cuál es mi HRV? |
| Evolución | ¿Cómo he cambiado? | ¿Cuál es mi gráfica? |
| Coach | ¿Qué debería entender ahora? | ¿Qué recomienda la IA? |
| Capacidades/Readiness | ¿Qué me falta? | ¿Cuál es mi score? |
| Perfil | ¿Qué sabe Epoch sobre mí? | ¿Qué configuración tengo? |

**Jerarquía:** Comprensión primero → Evidencia segundo → Detalle tercero.
Las métricas crudas son información de soporte, nunca la experiencia principal.
El atleta debe entender *qué cambió* antes de ver *por qué cambió*.

## Principios derivados

1. **Epoch Principle #1: Knowledge should not be a barrier.** Ningún término
   técnico sin explicación en lenguaje humano (TSB, CV, 0.1483 → prohibidos
   como texto principal).
2. **Capacidades, no deportes.** El deporte cambia; el cuerpo que se adapta es
   el mismo. "Escalada" queda ELIMINADA como nombre de capacidad — display:
   **"Fuerza-resistencia"** (la clave backend `escalada` no se migra aún).
3. **Most athletes know what they did. Few understand what they built.**
   Cada análisis termina en capacidad construida, no en número.
4. **Think Epoch first, speak Epoch later.** Primero la app piensa Epoch
   (insight-first, lenguaje humano); la traducción a inglés viene después.
5. Wellness no es una función: Entrenamiento → Adaptación → Recuperación →
   Evolución.

## Implicación inmediata (v6.6)
- Home abre con UNA lectura humana (de weekly-intelligence/coach), no con grid de métricas.
- Las cards de métricas se vuelven evidencia colapsable.
- Pendiente: "Epoch Experience Vision v1.0" (escenas, no pantallas) — doc del fundador.
