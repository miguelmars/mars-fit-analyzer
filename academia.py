"""
academia.py
===========
Educational content for the Mars system: glossary of terms and per-capacity
education. Split out of capability_engine per TD-010C / TD-011.

Import directly from here:
    from academia import GLOSSARY, CAPABILITY_EDUCATION, academia
"""

from __future__ import annotations

from typing import Any


# ── Glossary of terms ─────────────────────────────────────────────────────────

GLOSSARY: dict[str, dict] = {
    "score_100": {
        "término": "Score 100",
        "definición": "Your best recorded personal moment on that indicator. It does not compare you against other athletes — it compares you against yourself. An 89 means you are at 89% of the best you have ever achieved.",
        "ejemplo": "Aerobic Engine 89.4 = you are at 89.4% of your best historical cycling efficiency.",
    },
    "madurez": {
        "término": "Model maturity",
        "definición": "How well calibrated the scoring engine is with your personal history. High maturity = many weeks of prior data to compare against. Low maturity = the model is still learning your baseline.",
        "ejemplo": "Aerobic Engine 93% maturity = it has 8 years of data to calibrate with. Strength 65% = still few comparable records.",
    },
    "confianza": {
        "término": "Confidence",
        "definición": "How well supported the score is by quality data. High confidence = many sessions with complete telemetry. Low = estimates from partial data.",
        "ejemplo": "If you only have 3 weeks of recent data instead of 12, confidence drops even though the score is valid.",
    },
    "z2": {
        "término": "Zone 2 (Z2)",
        "definición": "Low-intensity aerobic range: 134–150 bpm on the bike, 138–154 bpm running. It is the zone where muscle burns fat as its main fuel and where the long-term endurance engine is built.",
        "ejemplo": "60 min in Z2 is like building 'aerobic infrastructure'. It is boring, but it is the work that lasts longest.",
    },
    "lt_bpm": {
        "término": "Lactate Threshold (LT)",
        "definición": "The heart rate where effort shifts from sustainable aerobic to accumulating anaerobic. Above this point, the body produces lactate faster than it can clear it.",
        "ejemplo": "Mars cycling LT: 168 bpm. Above 168 you are burning matches — you can hold it for minutes, not hours.",
    },
    "eficiencia_vel_fc": {
        "término": "Speed/HR efficiency",
        "definición": "Average speed divided by average HR in a session. It measures how many km/h you produce per heartbeat. Higher number = better aerobic movement economy.",
        "ejemplo": "0.148 speed/HR = 20 km/h at 135 bpm. Mars target: 0.155+ (more speed at the same HR).",
    },
    "bloque_12_semanas": {
        "término": "12-week block",
        "definición": "The period analyzed to build the annual history. For each year, every block of 12 consecutive weeks is evaluated and the best one is chosen. This prevents one exceptional month from distorting the whole year.",
        "ejemplo": "If in 2024 you had 3 epic months and then tapered off, the year's score reflects those 3 months, not the year's average.",
    },
    "mejor_epoca_historica": {
        "término": "Best historical era",
        "definición": "The 12-week block with the highest score in your entire history, as long as it has enough maturity and confidence to be comparable. Only one can exist.",
        "ejemplo": "Aerobic Engine best era: Nov 2019 – Feb 2020. Everything is compared against that period.",
    },
    "readiness": {
        "término": "Readiness",
        "definición": "Weighted sum of your current capacities against what each specific event demands. A 65 for Escalera al Infierno means you demonstrate 65% of what that goal requires, with the data available.",
        "ejemplo": "Escalera al Infierno weighs strength-endurance 30% and aerobic 35%. Your high aerobic engine helps a lot, but strength-endurance is the limiter.",
    },
}


# ── Per-capacity education ────────────────────────────────────────────────────

CAPABILITY_EDUCATION: dict[str, dict] = {
    "motor_aerobico": {
        "qué_es": "The aerobic engine measures your capacity to hold sustained effort in zone 2 — the range where you build endurance without accumulating metabolic debt. It is the foundation of all endurance performance.",
        "qué_significa_score": "Score 100 = your best historical cycling efficiency, in your best 12-week block. Score 89 = you are very close to that peak.",
        "cómo_mejorar": "Long Z2 rides (134–150 bpm), consistent, 3–4 times per week, minimum 1.5h. Efficiency climbs week by week when you train in the right zone.",
        "indicadores": {
            "efficiency": "Speed / average HR in cycling. It rises when your heart moves blood more efficiently or when your pedaling improves. Target: 0.155+ km/h/bpm.",
            "consistency": "Weeks with at least one aerobic session in the last 3 months. Gaps kill adaptation — muscle needs a regular stimulus.",
            "z2": "Percentage of time in Mars Z2 (134–150 bpm). If you train in Z3-Z4 believing it is Z2, the engine does not get built no matter the hours.",
            "long_endurance": "Distance of your recent long ride. Endurance beyond 60 km requires specific adaptations that only come from doing those distances.",
        },
    },
    "composicion_corporal": {
        "qué_es": "Measures your current weight position relative to your personal history and the recent trend. It does not judge general health — it judges specific performance: less weight at the same power = more speed.",
        "qué_significa_score": "Score 100 = you are at your historical minimum weight of the last 8 years (69 kg in 2019). Score 91 = current weight (89 kg) sits well within your range, with a favorable trend.",
        "cómo_mejorar": "Moderate caloric deficit + enough protein to preserve muscle mass. In endurance cycling, every kg lost = ~10 sec/km gained on long climbs.",
        "indicadores": {
            "weight_trend": "Comparison of the last 4 weeks' average vs the previous 4. Downward trend = progress.",
            "personal_range": "Where your current weight sits within your personal historical range (69–95 kg). It is not a comparison against external tables.",
            "measurement_adherence": "Consistency of weekly measurement. Without data there is no signal — weighing in once a week is enough.",
        },
    },
    "recuperacion": {
        "qué_es": "Measures the quality of your recovery between sessions: resting HR, rest days and monitoring consistency. Poor recovery = training stimuli never turn into adaptation.",
        "qué_significa_score": "Currently no score — no wellness data recorded. The model cannot invent recovery.",
        "cómo_mejorar": "Log resting HR every morning and wellness (fatigue, sleep) 3+ times per week. With 4 weeks of data the score activates.",
        "indicadores": {
            "resting_hr": "HR on waking, before getting up. If it rises 5+ bpm above your average, it signals accumulated fatigue or the start of illness.",
            "wellness": "Entries in the last week (fatigue, sleep, pain). More entries = better signal for detecting overtraining.",
            "rest_days": "Days without a session in the last 4 weeks. Muscle grows during rest, not during training.",
        },
    },
    "escalada": {
        "qué_es": "Measures the capacity to generate and sustain accumulated elevation gain in cycling. Critical for events like the Escalera al Infierno. It develops specifically — flat hours do not substitute for meters of climbing.",
        "qué_significa_score": "Score 100 = the best strength-endurance block of your history (reference: p90 of your previous climbing weeks). Currently 67.9 = good level but below your historical ceiling.",
        "cómo_mejorar": "Include at least 1 session with >500m of elevation gain per week. Stack consecutive active weeks — cardiovascular adaptation to climbing is specific and takes 6–8 weeks.",
        "indicadores": {
            "weekly_ascent": "Total elevation gain in the last 27 days vs. your historical p75 of climbing weeks × 4. 4540m recent vs a reference of 7474m.",
            "climbing_sessions": "Sessions with >500m of gain in the last 55 days. The minimum stimulus to adapt the legs to real climbing efforts.",
            "best_ascent": "Largest gain in a single session (last 55 days) vs. your historical p90 of sessions with elevation. Measures peak capacity, not just volume.",
        },
    },
    "fuerza": {
        "qué_es": "Measures consistency and progression in strength training. For endurance cycling, specific strength improves pedaling economy and reduces injury risk on long rides.",
        "qué_significa_score": "Score 13.8 = very sporadic strength training in the last 55 days. The engine does not distinguish between gym, Compex and functional work — it all counts.",
        "cómo_mejorar": "2 strength sessions per week, consistent. Log them in the app so the model can trace progression.",
        "indicadores": {
            "sessions": "Average strength sessions per week over the last 55 days. Reference: 2 sessions/week.",
            "consistency": "Weeks with at least one strength session. Continuity matters more than intensity for building the habit.",
            "progression": "Entries with load recorded. Without weights logged, the model cannot tell whether you are progressing.",
        },
    },
    "nutricion_deportiva": {
        "qué_es": "Measures consistency in using and logging nutrition during sessions. Nutrition does not boost performance directly — it removes the ceiling that energy deficiency imposes.",
        "qué_significa_score": "No score — the nutrition_log table is empty. Start logging to activate the model.",
        "cómo_mejorar": "Log every gel used (40g carbs, every 45–60 min in sessions >1h). With 8 logged sessions the score activates.",
        "indicadores": {
            "logged_uses": "Sessions with nutrition logged in the last 55 days. Reference: 8 sessions.",
            "timing": "Entries with intake timing documented (before/during/after). Timing matters as much as quantity.",
            "gi_tolerance": "Entries with GI response noted. Knowing what you tolerate and what you do not avoids surprises on race day.",
        },
    },
}


# ── Main function ─────────────────────────────────────────────────────────────

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
    return {"ok": False, "error": f"Key '{key}' not found.", "disponibles": available}
