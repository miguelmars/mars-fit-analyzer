"""
academia.py
===========
Contenido educativo del sistema Mars: glosario de términos y educación
por capacidad. Separado de capability_engine para cumplir TD-010C / TD-011.

Importar desde aquí directamente:
    from academia import GLOSSARY, CAPABILITY_EDUCATION, academia
"""

from __future__ import annotations

from typing import Any


# ── Glosario de términos ──────────────────────────────────────────────────────

GLOSSARY: dict[str, dict] = {
    "score_100": {
        "término": "Score 100",
        "definición": "Tu mejor momento personal registrado en ese indicador. No compara contra otros atletas — compara contra ti mismo. Un 89 significa que estás al 89% de lo mejor que has logrado.",
        "ejemplo": "Motor Aeróbico 89.4 = estás al 89.4% de tu mejor eficiencia histórica en ciclismo.",
    },
    "madurez": {
        "término": "Madurez del modelo",
        "definición": "Qué tan bien calibrado está el motor de cálculo con tu historial personal. Alta madurez = muchas semanas de datos previos para comparar. Baja madurez = el modelo todavía está aprendiendo tu línea base.",
        "ejemplo": "Motor Aeróbico 93% madurez = tiene 8 años de datos para calibrarse. Fuerza 65% = todavía pocos registros comparables.",
    },
    "confianza": {
        "término": "Confianza",
        "definición": "Qué tan bien respaldado está el score con datos de calidad. Confianza alta = muchas sesiones con telemetría completa. Baja = estimaciones con datos parciales.",
        "ejemplo": "Si solo tienes 3 semanas de datos recientes en lugar de 12, la confianza baja aunque el score sea válido.",
    },
    "z2": {
        "término": "Zona 2 (Z2)",
        "definición": "Rango aeróbico de baja intensidad: 134–150 bpm en bici, 138–154 bpm corriendo. Es la zona donde el músculo usa grasa como combustible principal y donde se construye el motor de resistencia a largo plazo.",
        "ejemplo": "60 min en Z2 equivale a construir 'infraestructura aeróbica'. Es aburrido pero es el trabajo que más dura.",
    },
    "lt_bpm": {
        "término": "Umbral Láctico (LT)",
        "definición": "La frecuencia cardíaca donde el esfuerzo pasa de aeróbico sostenible a anaeróbico acumulativo. Por encima de este punto, el cuerpo produce lactato más rápido de lo que puede eliminarlo.",
        "ejemplo": "Mars LT ciclismo: 168 bpm. Arriba de 168 estás quemando fósforos — puedes sostenerlo minutos, no horas.",
    },
    "eficiencia_vel_fc": {
        "término": "Eficiencia vel/FC",
        "definición": "Velocidad promedio dividida entre FC promedio en una sesión. Mide cuántos km/h produces por cada latido. Número más alto = mejor economía de movimiento aeróbica.",
        "ejemplo": "0.148 vel/FC = 20 km/h a 135 bpm. Objetivo Mars: 0.155+ (equivale a más velocidad al mismo HR).",
    },
    "bloque_12_semanas": {
        "término": "Bloque de 12 semanas",
        "definición": "El período analizado para construir la historia anual. Cada año se evalúan todos los bloques de 12 semanas consecutivas y se elige el mejor. Evita que un mes excepcional distorsione el año completo.",
        "ejemplo": "Si en 2024 tuviste 3 meses épicos y luego bajaste, el score del año refleja esos 3 meses, no el promedio del año.",
    },
    "mejor_epoca_historica": {
        "término": "Mejor época histórica",
        "definición": "El bloque de 12 semanas con el score más alto de toda tu historia, siempre que tenga madurez y confianza suficientes para ser comparable. Solo puede existir uno.",
        "ejemplo": "Motor Aeróbico mejor época: Nov 2019 – Feb 2020. Todo se compara contra ese período.",
    },
    "readiness": {
        "término": "Preparación (Readiness)",
        "definición": "Suma ponderada de tus capacidades actuales según lo que demanda cada evento específico. 65 en Escalera al Infierno significa que demuestras el 65% de lo necesario para ese objetivo, con los datos disponibles.",
        "ejemplo": "Escalera al Infierno pesa escalada 30% y aeróbico 35%. Tu motor aeróbico alto ayuda mucho, pero la escalada es el limitante.",
    },
}


# ── Educación por capacidad ───────────────────────────────────────────────────

CAPABILITY_EDUCATION: dict[str, dict] = {
    "motor_aerobico": {
        "qué_es": "El motor aeróbico mide tu capacidad de mantener esfuerzo sostenido en zona 2 — el rango donde construyes resistencia sin acumular deuda metabólica. Es la base de todo rendimiento de resistencia.",
        "qué_significa_score": "Score 100 = tu mejor eficiencia histórica en ciclismo, en el mejor bloque de 12 semanas. Score 89 = estás muy cerca de ese pico.",
        "cómo_mejorar": "Salidas largas en Z2 (134–150 bpm), consistentes, 3–4 veces por semana mínimo 1.5h. La eficiencia sube semana a semana si entrenas en zona correcta.",
        "indicadores": {
            "efficiency": "Velocidad / FC promedio en ciclismo. Sube cuando tu corazón mueve más sangre eficientemente o cuando tu pedaleo mejora. Objetivo: 0.155+ km/h/bpm.",
            "consistency": "Semanas con al menos una sesión aeróbica en los últimos 3 meses. Discontinuidad mata la adaptación — el músculo necesita estímulo regular.",
            "z2": "Porcentaje del tiempo en Z2 Mars (134–150 bpm). Si entrenas en Z3-Z4 creyendo que es Z2, el motor no se construye aunque hagas horas.",
            "long_endurance": "Distancia de tu salida larga reciente. La resistencia a distancias >60 km requiere adaptaciones específicas que solo se logran haciéndolas.",
        },
    },
    "composicion_corporal": {
        "qué_es": "Mide tu posición de peso actual en relación a tu historial personal y la tendencia reciente. No evalúa salud general — evalúa rendimiento específico: menos peso con misma potencia = más velocidad.",
        "qué_significa_score": "Score 100 = estás en tu peso mínimo histórico de los últimos 8 años (69 kg en 2019). Score 91 = peso actual (89 kg) está bien posicionado en tu rango, con tendencia favorable.",
        "cómo_mejorar": "Déficit calórico moderado + proteína suficiente para preservar masa muscular. En ciclismo de resistencia, cada kg menos = ~10 seg/km de mejora en subidas largas.",
        "indicadores": {
            "weight_trend": "Comparación entre el promedio de las últimas 4 semanas vs las 4 anteriores. Tendencia a la baja = progreso.",
            "personal_range": "Dónde está tu peso actual dentro de tu rango histórico personal (69–95 kg). No es comparación con tablas externas.",
            "measurement_adherence": "Consistencia de medición semanal. Sin datos no hay señal — pesarte 1 vez por semana es suficiente.",
        },
    },
    "recuperacion": {
        "qué_es": "Mide la calidad de tu recuperación entre sesiones: FC de reposo, días de descanso y consistencia de monitoreo. Recuperación deficiente = estímulos de entrenamiento no se convierten en adaptación.",
        "qué_significa_score": "Actualmente sin score — no hay datos de wellness registrados. El modelo no puede inventar recuperación.",
        "cómo_mejorar": "Registrar FC de reposo cada mañana y wellness (fatiga, sueño) 3+ veces por semana. Con 4 semanas de datos el score se activa.",
        "indicadores": {
            "resting_hr": "FC al despertar, antes de levantarse. Si sube 5+ bpm sobre tu promedio, señal de fatiga acumulada o inicio de enfermedad.",
            "wellness": "Registros en la última semana (fatiga, sueño, dolor). Más registros = mejor señal para detectar sobreentrenamiento.",
            "rest_days": "Días sin sesión en las últimas 4 semanas. El músculo crece en el descanso, no en el entrenamiento.",
        },
    },
    "escalada": {
        "qué_es": "Mide la capacidad de generar y sostener desnivel acumulado en ciclismo. Crítico para eventos como la Escalera al Infierno. Se desarrolla específicamente — horas en llano no sustituyen metros de subida.",
        "qué_significa_score": "Score 100 = mejor bloque de escalada de tu historia (referencia: p90 de tus semanas de escalada previa). Actualmente 67.9 = buen nivel pero por debajo de tu techo histórico.",
        "cómo_mejorar": "Incluir al menos 1 sesión con >500m desnivel por semana. Acumular semanas activas consecutivas — la adaptación cardiovascular a subidas es específica y tarda 6–8 semanas.",
        "indicadores": {
            "weekly_ascent": "Desnivel total en los últimos 27 días vs. tu p75 histórico de semanas con escalada × 4. 4540m reciente vs referencia de 7474m.",
            "climbing_sessions": "Sesiones con >500m desnivel en los últimos 55 días. El estímulo mínimo para adaptar las piernas a esfuerzos de ascenso real.",
            "best_ascent": "Mayor desnivel en una sola sesión (últimos 55 días) vs. tu p90 histórico de sesiones con desnivel. Mide la capacidad de pico, no solo el volumen.",
        },
    },
    "fuerza": {
        "qué_es": "Mide la consistencia y progresión en entrenamiento de fuerza. Para ciclismo de resistencia, la fuerza específica mejora la economía de pedaleo y reduce riesgo de lesión en salidas largas.",
        "qué_significa_score": "Score 13.8 = entrenamiento de fuerza muy esporádico en los últimos 55 días. El motor no distingue entre gym, Compex y ejercicio funcional — todo suma.",
        "cómo_mejorar": "2 sesiones de fuerza por semana, consistentes. Registrar en la app para que el modelo pueda trazar progresión.",
        "indicadores": {
            "sessions": "Promedio de sesiones de fuerza por semana en los últimos 55 días. Referencia: 2 sesiones/semana.",
            "consistency": "Semanas con al menos una sesión de fuerza. Continuidad es más importante que intensidad para construir el hábito.",
            "progression": "Registros con carga anotada. Sin peso registrado el modelo no puede detectar si estás progresando.",
        },
    },
    "nutricion_deportiva": {
        "qué_es": "Mide la consistencia en el uso y registro de nutrición durante sesiones. La nutrición no mejora el rendimiento directo — elimina el techo que pone la deficiencia energética.",
        "qué_significa_score": "Sin score — la tabla de nutrition_log está vacía. Empezar a registrar activa el modelo.",
        "cómo_mejorar": "Registrar cada uso de gel (40g carbos, cada 45–60 min en sesiones >1h). Con 8 sesiones registradas el score se activa.",
        "indicadores": {
            "logged_uses": "Sesiones con nutrición registrada en los últimos 55 días. Referencia: 8 sesiones.",
            "timing": "Registros con momento de ingesta documentado (antes/durante/después). El timing importa tanto como la cantidad.",
            "gi_tolerance": "Registros con respuesta GI anotada. Entender qué toleras y qué no en carrera evita sorpresas en eventos.",
        },
    },
}


# ── Función principal ─────────────────────────────────────────────────────────

def academia(key: str) -> dict[str, Any]:
    """Return educational content for a capability or glossary term.

    Args:
        key: Capability key (e.g. 'motor_aerobico'), glossary term key
             (e.g. 'z2'), or 'glosario' to return all glossary terms.

    Returns:
        dict with 'ok' bool and 'educacion', 'termino', or 'glosario' payload.
    """
    if key == "glosario":
        return {"ok": True, "glosario": GLOSSARY}
    if key in CAPABILITY_EDUCATION:
        return {"ok": True, "key": key, "educacion": CAPABILITY_EDUCATION[key]}
    if key in GLOSSARY:
        return {"ok": True, "key": key, "termino": GLOSSARY[key]}
    available = list(CAPABILITY_EDUCATION.keys()) + list(GLOSSARY.keys()) + ["glosario"]
    return {"ok": False, "error": f"Clave '{key}' no encontrada.", "disponibles": available}
