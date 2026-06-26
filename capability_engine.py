"""Personal capability scores built only from Mars historical data."""

from __future__ import annotations

import math
from datetime import date
from statistics import median
from typing import Any

# TD-011 / TD-010C: GLOSSARY, CAPABILITY_EDUCATION y academia() viven en academia.py
# Se re-exportan aquí para backward compatibility con imports existentes.
from academia import GLOSSARY, CAPABILITY_EDUCATION, academia  # noqa: F401

CAPABILITY_VERSION = "capability_v1_personal_history"
CAPABILITY_VALIDATION_VERSION = "capability_validation_v1"
CAPABILITY_HISTORY_VERSION = "capability_history_v1"

READINESS_EVENTS: dict[str, dict] = {
    "escalera_al_infierno": {
        "nombre": "Escalera al Infierno",
        "tipo": "climbing_race",
        "descripcion": "Mountain hill-climb race",
        "weights": {
            "motor_aerobico": 0.35,
            "escalada": 0.30,
            "composicion_corporal": 0.15,
            "nutricion_deportiva": 0.10,
            "recuperacion": 0.10,
        },
    },
    "gran_fondo_150": {
        "nombre": "Gran Fondo 150 km",
        "tipo": "endurance_cycling",
        "descripcion": "Long-distance endurance ride",
        "weights": {
            "motor_aerobico": 0.45,
            "composicion_corporal": 0.20,
            "nutricion_deportiva": 0.15,
            "recuperacion": 0.15,
            "escalada": 0.05,
        },
    },
    "medio_maraton": {
        "nombre": "Half Marathon",
        "tipo": "running_endurance",
        "descripcion": "Middle-distance running race",
        "weights": {
            "motor_aerobico": 0.40,
            "composicion_corporal": 0.25,
            "fuerza": 0.20,
            "recuperacion": 0.15,
        },
    },
    # V7.3: el objetivo real del atleta — plan Garmin Time Trial (cierra 2026-10-03)
    "time_trial": {
        "nombre": "Time Trial",
        "tipo": "sustained_cycling",
        "descripcion": "Sustained effort against the clock on the bike",
        "weights": {
            "motor_aerobico": 0.45,
            "escalada": 0.20,          # display: Fuerza-resistencia (esfuerzo sostenido)
            "composicion_corporal": 0.15,
            "recuperacion": 0.10,
            "nutricion_deportiva": 0.10,
        },
    },
}
CYCLING_SPORTS = ("cycling", "indoor_cycling")
RUNNING_SPORTS = ("running", "trail_running", "treadmill_running")


def score_ratio(actual: float | None, reference: float | None) -> float | None:
    if actual is None or reference is None or reference <= 0:
        return None
    return round(min(max(actual / reference, 0.0), 1.0) * 100, 1)


def inverse_score_ratio(actual: float | None, reference: float | None) -> float | None:
    if actual is None or actual <= 0 or reference is None:
        return None
    return round(min(max(reference / actual, 0.0), 1.0) * 100, 1)


def weighted_score(indicators: list[dict[str, Any]]) -> float | None:
    if not indicators or any(item.get("score") is None for item in indicators):
        return None
    weight = sum(float(item["weight"]) for item in indicators)
    if not math.isclose(weight, 1.0, abs_tol=0.001):
        raise ValueError("Capability indicator weights must total 1.0")
    return round(sum(float(item["score"]) * float(item["weight"]) for item in indicators), 1)


def weighted_confidence(indicators: list[dict[str, Any]]) -> float:
    weight = sum(float(item["weight"]) for item in indicators)
    if not math.isclose(weight, 1.0, abs_tol=0.001):
        raise ValueError("Capability confidence weights must total 1.0")
    return round(sum(float(item["confidence"]) * float(item["weight"]) for item in indicators), 2)


def validate_capability(
    capability: dict[str, Any],
    previous: dict[str, Any] | None = None,
    alert_threshold: float = 15.0,
) -> dict[str, Any]:
    """Audit capability arithmetic, anchors and changes from the prior run."""
    indicators = capability.get("indicators") or []
    weights_total = round(sum(float(item.get("weight") or 0) for item in indicators), 4)
    weights_valid = math.isclose(weights_total, 1.0, abs_tol=0.001)
    calculable = bool(indicators) and all(item.get("score") is not None for item in indicators)
    recomputed_score = weighted_score(indicators) if calculable and weights_valid else None
    reported_score = _as_float(capability.get("score"))
    arithmetic_delta = (
        round(recomputed_score - reported_score, 4)
        if recomputed_score is not None and reported_score is not None
        else None
    )
    arithmetic_valid = (
        arithmetic_delta is not None
        and math.isclose(arithmetic_delta, 0.0, abs_tol=0.05)
        and weights_valid
    )

    previous_indicators = {
        item.get("key"): item
        for item in (previous or {}).get("indicators", [])
        if item.get("key")
    }
    indicator_report = []
    alerts = []
    for item in indicators:
        prior = previous_indicators.get(item.get("key"))
        prior_score = _as_float(prior.get("score")) if prior else None
        score = _as_float(item.get("score"))
        delta = (
            round(score - prior_score, 1)
            if score is not None and prior_score is not None
            else None
        )
        contribution = (
            round(score * float(item.get("weight") or 0), 2)
            if score is not None
            else None
        )
        changed = delta is not None and abs(delta) > alert_threshold
        if changed:
            alerts.append({
                "type": "indicator_change",
                "indicator": item.get("key"),
                "delta_points": delta,
                "threshold_points": alert_threshold,
            })
        indicator_report.append({
            **item,
            "weighted_contribution": contribution,
            "previous_score": prior_score,
            "delta_points": delta,
            "requires_review": changed,
        })

    previous_score = _as_float((previous or {}).get("score"))
    score_delta = (
        round(reported_score - previous_score, 1)
        if reported_score is not None and previous_score is not None
        else None
    )
    if score_delta is not None and abs(score_delta) > alert_threshold:
        alerts.append({
            "type": "capability_score_change",
            "delta_points": score_delta,
            "threshold_points": alert_threshold,
        })

    anchors = capability.get("anchors") or {}
    historical_anchor = anchors.get("historical") or {}
    similar_anchor = anchors.get("similar_era") or {}
    anchor_validation = {
        "historical_available": historical_anchor.get("status") == "available",
        "similar_era_available": similar_anchor.get("status") == "available",
    }
    anchors_valid = all(anchor_validation.values())
    blockers = []
    if not arithmetic_valid:
        blockers.append("arithmetic")
    if not anchors_valid:
        blockers.append("double_anchor")
    if not calculable:
        blockers.append("missing_indicator")

    status = (
        "invalid" if blockers
        else "review_required" if alerts
        else "accepted"
    )
    return {
        "version": CAPABILITY_VALIDATION_VERSION,
        "capability_key": capability.get("key"),
        "status": status,
        "reported_score": reported_score,
        "recomputed_score": recomputed_score,
        "arithmetic_delta": arithmetic_delta,
        "weights_total": weights_total,
        "arithmetic_valid": arithmetic_valid,
        "anchors": anchor_validation,
        "anchors_valid": anchors_valid,
        "previous_score": previous_score,
        "score_delta_points": score_delta,
        "alert_threshold_points": alert_threshold,
        "alerts": alerts,
        "blockers": blockers,
        "indicators": indicator_report,
        "first_record": previous is None,
        "note": (
            "First official baseline; changes will be evaluated from the next run onward."
            if previous is None
            else "Comparison against the previous official run."
        ),
    }


def percentile(values: list[float], pct: float) -> float | None:
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    index = (len(values) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return values[lower]
    fraction = index - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _indicator(
    key: str,
    label: str,
    actual: float | None,
    reference: float | None,
    weight: float,
    confidence: float,
    inverse: bool = False,
    unit: str = "",
) -> dict[str, Any]:
    score = (
        inverse_score_ratio(actual, reference)
        if inverse
        else score_ratio(actual, reference)
    )
    return {
        "key": key,
        "label": label,
        "actual": round(actual, 3) if actual is not None else None,
        "reference": round(reference, 3) if reference is not None else None,
        "unit": unit,
        "score": score,
        "weight": weight,
        "confidence": round(min(max(confidence, 0.0), 1.0), 2),
    }


def _capability(
    key: str,
    label: str,
    indicators: list[dict[str, Any]],
    maturity: float,
    recommendation: str,
    anchor_week: str | None = None,
    similar_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = weighted_score(indicators)
    confidence = weighted_confidence(indicators)
    available = score is not None
    valid = [item for item in indicators if item.get("score") is not None]
    limiting = min(valid, key=lambda item: item["score"]) if valid else None
    driving = max(valid, key=lambda item: item["score"]) if valid else None
    similar_score = similar_anchor.get("score") if similar_anchor else None
    return {
        "key": key,
        "nombre": label,
        "score": score,
        "confidence": confidence,
        "status": "calculated" if available else "insufficient_data",
        "maturity": round(maturity, 1),
        "limitante_principal": limiting["label"] if limiting else "Insufficient data",
        "impulsor_principal": driving["label"] if driving else "Insufficient data",
        "score_vs_historico": score,
        "score_vs_epoca_similar": (
            score_ratio(score, similar_score)
            if score is not None and similar_score is not None
            else None
        ),
        "anchor_week": anchor_week,
        "similar_anchor": similar_anchor,
        "anchors": {
            "historical": {
                "status": "available" if anchor_week else "insufficient_data",
                "week": anchor_week,
            },
            "similar_era": {
                "status": "available" if similar_anchor else "insufficient_data",
                **(similar_anchor or {}),
            },
        },
        "recomendacion": recommendation,
        "indicators": indicators,
        "history": [],
    }


def _load_snapshots(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT week_start, week_end, weight_kg, km_week, hours_week, sessions,
                   active_days, ascent_m_week, cycling_efficiency,
                   running_efficiency, z2_pct_mars, z2_confidence_score,
                   sport_breakdown
            FROM athlete_snapshots
            ORDER BY week_start
        """)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _dominant_aerobic_sport(recent: list[dict[str, Any]]) -> str:
    hours = {"cycling": 0.0, "running": 0.0}
    for row in recent:
        breakdown = row.get("sport_breakdown") or {}
        for sport, group in (
            ("cycling", ("cycling", "indoor_cycling")),
            ("running", ("running", "trail_running", "treadmill_running")),
        ):
            hours[sport] += sum(
                _as_float((breakdown.get(name) or {}).get("hours")) or 0
                for name in group
            )
    return max(hours, key=hours.get)


def _sport_names(sport: str) -> tuple[str, ...]:
    return CYCLING_SPORTS if sport == "cycling" else RUNNING_SPORTS


def _sport_hours(snapshot: dict[str, Any], sport: str) -> float:
    breakdown = snapshot.get("sport_breakdown") or {}
    return sum(
        _as_float((breakdown.get(name) or {}).get("hours")) or 0
        for name in _sport_names(sport)
    )


def _weighted_session_average(
    rows: list[dict[str, Any]],
    value_key: str,
) -> float | None:
    pairs = [
        (_as_float(row.get(value_key)), _as_float(row.get("duration_s")) or 0)
        for row in rows
    ]
    pairs = [(value, weight) for value, weight in pairs if value is not None and weight > 0]
    if not pairs:
        return None
    total = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / total


def _load_aerobic_sessions(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                cs.start_date,
                cs.sport,
                cs.distance_km,
                cs.duration_s,
                cs.efficiency_speed_hr,
                se.avg_altitude_m,
                se.habitual_altitude_m,
                se.altitude_confidence
            FROM clean_sessions cs
            LEFT JOIN session_environment se USING (clean_session_id)
            WHERE cs.start_date IS NOT NULL
              AND cs.sport = ANY(%s)
            ORDER BY cs.start_date, cs.start_time
        """, (list(CYCLING_SPORTS + RUNNING_SPORTS),))
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _historical_aerobic_block(
    block: list[dict[str, Any]],
    prior_snapshots: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate one 12-week aerobic block using only earlier references."""
    block_start = block[0]["week_start"]
    block_end = block[-1].get("week_end") or block[-1]["week_start"]
    sport = _dominant_aerobic_sport(block)
    sport_names = _sport_names(sport)
    efficiency_key = f"{sport}_efficiency"
    block_sessions = [
        row for row in sessions
        if row.get("sport") in sport_names
        and block_start <= row["start_date"] <= block_end
    ]
    prior_sessions = [
        row for row in sessions
        if row.get("sport") in sport_names and row["start_date"] < block_start
    ]
    efficiency_sessions = [
        row for row in block_sessions
        if _as_float(row.get("efficiency_speed_hr")) is not None
    ]
    current_efficiency = _weighted_session_average(
        efficiency_sessions,
        "efficiency_speed_hr",
    )
    current_altitude = _weighted_session_average(
        [
            row for row in efficiency_sessions
            if _as_float(row.get("avg_altitude_m")) is not None
        ],
        "avg_altitude_m",
    )
    altitude_confidence = _weighted_session_average(
        [
            row for row in efficiency_sessions
            if _as_float(row.get("altitude_confidence")) is not None
        ],
        "altitude_confidence",
    )
    comparable_sessions = []
    if current_altitude is not None:
        comparable_sessions = [
            row for row in prior_sessions
            if _as_float(row.get("efficiency_speed_hr")) is not None
            and _as_float(row.get("avg_altitude_m")) is not None
            and abs(float(row["avg_altitude_m"]) - current_altitude) <= 300
        ]
    fallback_sessions = [
        row for row in prior_sessions
        if _as_float(row.get("efficiency_speed_hr")) is not None
    ]
    reference_sessions = (
        comparable_sessions if len(comparable_sessions) >= 10 else fallback_sessions
    )
    reference_efficiency = (
        percentile(
            [_as_float(row.get("efficiency_speed_hr")) for row in reference_sessions],
            0.90,
        )
        if len(reference_sessions) >= 10 else None
    )
    comparison_method = (
        "similar_altitude_history"
        if reference_sessions is comparable_sessions and len(comparable_sessions) >= 10
        else "prior_history_fallback"
        if len(reference_sessions) >= 10
        else "insufficient_prior_history"
    )

    current_z2_values = [
        _as_float(row.get("z2_pct_mars")) for row in block
        if _as_float(row.get("z2_pct_mars")) is not None
        and _sport_hours(row, sport) > 0
    ]
    prior_z2_values = [
        _as_float(row.get("z2_pct_mars")) for row in prior_snapshots
        if _as_float(row.get("z2_pct_mars")) is not None
        and _sport_hours(row, sport) > 0
    ]
    current_z2 = (
        sum(current_z2_values) / len(current_z2_values)
        if current_z2_values else None
    )
    reference_z2 = percentile(prior_z2_values, 0.75) if len(prior_z2_values) >= 4 else None
    z2_confidences = [
        _as_float(row.get("z2_confidence_score")) for row in block
        if _as_float(row.get("z2_confidence_score")) is not None
        and _sport_hours(row, sport) > 0
    ]
    z2_confidence = (
        sum(z2_confidences) / len(z2_confidences) if z2_confidences else 0.0
    )

    block_distances = [
        _as_float(row.get("distance_km")) for row in block_sessions
        if (_as_float(row.get("distance_km")) or 0) > 0
    ]
    prior_distances = [
        _as_float(row.get("distance_km")) for row in prior_sessions
        if (_as_float(row.get("distance_km")) or 0) > 0
    ]
    current_long = max(block_distances) if block_distances else None
    reference_long = percentile(prior_distances, 0.90) if len(prior_distances) >= 10 else None
    active_weeks = sum(1 for row in block if _sport_hours(row, sport) > 0)
    indicators = [
        _indicator(
            "efficiency",
            f"Efficiency {'cycling' if sport == 'cycling' else 'running'}",
            current_efficiency,
            reference_efficiency,
            0.40,
            min(len(efficiency_sessions) / 4, 1.0) * (altitude_confidence or 0.7),
            unit="vel/FC",
        ),
        _indicator(
            "consistency",
            "12-week consistency",
            float(active_weeks),
            12.0,
            0.25,
            1.0,
            unit="weeks",
        ),
        _indicator(
            "z2",
            "Time in Mars Z2",
            current_z2,
            reference_z2,
            0.20,
            z2_confidence,
            unit="%",
        ),
        _indicator(
            "long_endurance",
            "Long ride",
            current_long,
            reference_long,
            0.15,
            1.0 if current_long is not None else 0.0,
            unit="km",
        ),
    ]
    score = weighted_score(indicators)
    confidence = weighted_confidence(indicators)
    valid = score is not None and active_weeks >= 6 and len(efficiency_sessions) >= 3
    limiting = min(
        (item for item in indicators if item.get("score") is not None),
        key=lambda item: item["score"],
        default=None,
    )
    return {
        "period_start": block_start.isoformat(),
        "period_end": block_end.isoformat(),
        "year": block[len(block) // 2]["week_start"].year,
        "sport_context": sport,
        "score": score if valid else None,
        "raw_score": score,
        "confidence": confidence,
        "maturity": round(min(95, 35 + len(prior_snapshots) / 6), 1),
        "status": "calculated" if valid else "insufficient_data",
        "reason": (
            None if valid
            else "Se requieren 6 semanas activas, 3 sesiones con eficiencia y referencias previas."
        ),
        "active_weeks": active_weeks,
        "session_sample": len(block_sessions),
        "efficiency_sample": len(efficiency_sessions),
        "limitante_principal": limiting["label"] if limiting else "Insufficient data",
        "indicators": indicators,
        "environment_context": {
            "current_altitude_m": round(current_altitude, 1) if current_altitude is not None else None,
            "altitude_confidence": round(altitude_confidence or 0, 2),
            "comparison_window_m": 300,
            "comparison_method": comparison_method,
            "reference_sample": len(reference_sessions),
            "future_data_used": False,
        },
    }


def build_aerobic_history(
    snapshots: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    """Build annual best sustainable 12-week blocks without future leakage."""
    today = today or date.today()
    complete = [
        row for row in snapshots
        if (row.get("week_end") or row["week_start"]) < today
    ]
    blocks = []
    for end_index in range(11, len(complete)):
        block = complete[end_index - 11:end_index + 1]
        prior = complete[:end_index - 11]
        result = _historical_aerobic_block(block, prior, sessions)
        blocks.append(result)

    annual = []
    for year in sorted({row["year"] for row in blocks}):
        candidates = [
            row for row in blocks
            if row["year"] == year and row.get("score") is not None
        ]
        if candidates:
            best = max(
                candidates,
                key=lambda row: (row["score"], row["confidence"], row["period_end"]),
            )
            annual.append({**best, "candidate_blocks": len(candidates)})
        else:
            year_blocks = [row for row in blocks if row["year"] == year]
            annual.append({
                "year": year,
                "score": None,
                "status": "insufficient_data",
                "reason": "No hubo un bloque de 12 semanas con evidencia suficiente.",
                "candidate_blocks": 0,
                "evaluated_blocks": len(year_blocks),
                "best_period_tag": "sin_datos_suficientes",
            })

    valid_years = [row for row in annual if row.get("score") is not None]
    comparable_years = [
        row for row in valid_years
        if float(row.get("maturity") or 0) >= 60
        and float(row.get("confidence") or 0) >= 0.70
    ]
    best_year = (
        max(comparable_years, key=lambda row: row["score"])
        if comparable_years else None
    )
    for row in annual:
        row["eligible_for_best_period"] = row in comparable_years
        if row.get("score") is None:
            row.setdefault("best_period_tag", "sin_datos_suficientes")
        elif row not in comparable_years:
            row["best_period_tag"] = "historia_preliminar"
        elif best_year and row["year"] == best_year["year"]:
            row["best_period_tag"] = "mejor_epoca_historica"
        elif best_year and row["score"] >= best_year["score"] * 0.95:
            row["best_period_tag"] = "epoca_destacada"
        elif row["score"] >= 75:
            row["best_period_tag"] = "forma_solida"
        else:
            row["best_period_tag"] = "base_en_desarrollo"

    return {
        "ok": True,
        "version": CAPABILITY_HISTORY_VERSION,
        "capability_key": "motor_aerobico",
        "period_contract": {
            "annual_value": "best_sustainable_12_week_block",
            "minimum_active_weeks": 6,
            "minimum_efficiency_sessions": 3,
            "best_period_minimum_maturity": 60,
            "best_period_minimum_confidence": 0.70,
            "future_data_used": False,
            "altitude_comparison_window_m": 300,
        },
        "years": annual,
        "best_period": best_year,
        "evaluated_blocks": len(blocks),
    }


def calculate_aerobic_history(conn) -> dict[str, Any]:
    history = build_aerobic_history(
        _load_snapshots(conn),
        _load_aerobic_sessions(conn),
    )
    with conn.cursor() as cur:
        cur.execute("""
            SELECT score, confidence, maturity, validation_status, created_at
            FROM capability_runs
            WHERE capability_key = 'motor_aerobico'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    history["current_official"] = (
        {
            "score": _as_float(row[0]),
            "confidence": _as_float(row[1]),
            "maturity": _as_float(row[2]),
            "validation_status": row[3],
            "recorded_at": row[4].isoformat(),
        }
        if row else None
    )
    return history


def _long_distance_stats(conn, sport: str, recent_start: date) -> tuple[float | None, float | None]:
    sports = (
        ("cycling", "indoor_cycling")
        if sport == "cycling"
        else ("running", "trail_running", "treadmill_running")
    )
    with conn.cursor() as cur:
        cur.execute("""
            SELECT start_date, distance_km
            FROM clean_sessions
            WHERE sport = ANY(%s) AND distance_km > 0
            ORDER BY start_date
        """, (list(sports),))
        rows = [(row[0], float(row[1])) for row in cur.fetchall()]
    current = max((km for day, km in rows if day >= recent_start), default=None)
    reference = percentile([km for _, km in rows], 0.90)
    return current, reference


def _altitude_adjusted_efficiency(conn, sport: str) -> dict[str, Any]:
    sports = (
        ("cycling", "indoor_cycling")
        if sport == "cycling"
        else ("running", "trail_running", "treadmill_running")
    )
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MAX(start_date)
            FROM clean_sessions
            WHERE sport = ANY(%s)
        """, (list(sports),))
        latest_date = cur.fetchone()[0]
        if not latest_date:
            return {"usable": False}
        recent_start = latest_date.fromordinal(latest_date.toordinal() - 27)
        cur.execute("""
            SELECT
                SUM(cs.efficiency_speed_hr * cs.duration_s)
                    / NULLIF(SUM(cs.duration_s), 0) AS efficiency,
                SUM(se.avg_altitude_m * cs.duration_s)
                    / NULLIF(SUM(cs.duration_s), 0) AS avg_altitude_m,
                AVG(se.habitual_altitude_m) AS habitual_altitude_m,
                AVG(se.altitude_confidence) AS altitude_confidence,
                MAX(se.prior_21d_exposure_days) AS exposure_days,
                COUNT(*) AS sample
            FROM clean_sessions cs
            JOIN session_environment se USING (clean_session_id)
            WHERE cs.sport = ANY(%s)
              AND cs.start_date >= %s
              AND cs.efficiency_speed_hr IS NOT NULL
              AND se.avg_altitude_m IS NOT NULL
        """, (list(sports), recent_start))
        current = cur.fetchone()
        current_eff, current_altitude, habitual, confidence, exposure_days, sample = current
        if current_altitude is None:
            return {"usable": False}
        cur.execute("""
            SELECT PERCENTILE_CONT(0.90)
                WITHIN GROUP (ORDER BY cs.efficiency_speed_hr),
                   COUNT(*)
            FROM clean_sessions cs
            JOIN session_environment se USING (clean_session_id)
            WHERE cs.sport = ANY(%s)
              AND cs.start_date < %s
              AND cs.efficiency_speed_hr IS NOT NULL
              AND se.avg_altitude_m BETWEEN %s AND %s
        """, (
            list(sports),
            recent_start,
            float(current_altitude) - 300,
            float(current_altitude) + 300,
        ))
        reference, reference_sample = cur.fetchone()
    usable = int(sample or 0) >= 3 and int(reference_sample or 0) >= 10
    return {
        "usable": usable,
        "current_efficiency": _as_float(current_eff),
        "reference_efficiency": _as_float(reference),
        "current_altitude_m": round(float(current_altitude), 1),
        "habitual_altitude_m": round(float(habitual), 1) if habitual is not None else None,
        "altitude_delta_m": (
            round(float(current_altitude) - float(habitual), 1)
            if habitual is not None else None
        ),
        "altitude_confidence": round(float(confidence or 0), 2),
        "prior_21d_exposure_days": int(exposure_days or 0),
        "current_sample": int(sample or 0),
        "reference_sample": int(reference_sample or 0),
        "comparison_window_m": 300,
        "comparison_method": (
            "similar_altitude_history" if usable else "all_history_fallback"
        ),
    }


def _aerobic_capability(conn, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    active = [row for row in snapshots if int(row.get("sessions") or 0) > 0]
    recent12 = snapshots[-12:]
    recent4 = snapshots[-4:]
    sport = _dominant_aerobic_sport(recent12)
    efficiency_key = f"{sport}_efficiency"
    current_eff_values = [_as_float(row.get(efficiency_key)) for row in recent4]
    current_eff_values = [value for value in current_eff_values if value is not None]
    historical_eff = [
        _as_float(row.get(efficiency_key)) for row in snapshots[:-4]
        if _as_float(row.get(efficiency_key)) is not None
    ]
    current_eff = sum(current_eff_values) / len(current_eff_values) if current_eff_values else None
    reference_eff = percentile(historical_eff, 0.90)
    environment_context = _altitude_adjusted_efficiency(conn, sport)
    if environment_context.get("usable"):
        current_eff = environment_context["current_efficiency"]
        reference_eff = environment_context["reference_efficiency"]

    current_z2_values = [_as_float(row.get("z2_pct_mars")) for row in recent4]
    current_z2_values = [value for value in current_z2_values if value is not None]
    historical_z2 = [
        _as_float(row.get("z2_pct_mars")) for row in snapshots[:-4]
        if _as_float(row.get("z2_pct_mars")) is not None
    ]
    current_z2 = sum(current_z2_values) / len(current_z2_values) if current_z2_values else None
    reference_z2 = percentile(historical_z2, 0.75)
    z2_confidence_values = [
        _as_float(row.get("z2_confidence_score")) for row in recent4
        if _as_float(row.get("z2_confidence_score")) is not None
    ]
    z2_confidence = (
        sum(z2_confidence_values) / len(z2_confidence_values)
        if z2_confidence_values else 0.0
    )
    latest_week = active[-1]["week_start"]
    recent_start = latest_week.fromordinal(latest_week.toordinal() - 55)
    current_long, reference_long = _long_distance_stats(conn, sport, recent_start)
    indicators = [
        _indicator(
            "efficiency",
            f"Efficiency {'cycling' if sport == 'cycling' else 'running'}",
            current_eff,
            reference_eff,
            0.40, len(current_eff_values) / 4, unit="vel/FC",
        ),
        _indicator(
            "consistency", "12-week consistency",
            float(sum(1 for row in recent12 if int(row.get("sessions") or 0) > 0)),
            12.0,
            0.25, 1.0, unit="weeks",
        ),
        _indicator(
            "z2", "Time in Mars Z2", current_z2, reference_z2,
            0.20, z2_confidence, unit="%",
        ),
        _indicator(
            "long_endurance", "Long ride", current_long, reference_long,
            0.15, 1.0 if current_long is not None else 0.0, unit="km",
        ),
    ]
    historical_best = max(
        (
            (_as_float(row.get(efficiency_key)), row["week_start"])
            for row in active
            if _as_float(row.get(efficiency_key)) is not None
        ),
        default=(None, None),
    )
    comparable = _similar_aerobic_anchor(snapshots, recent4, efficiency_key)
    capability = _capability(
        "motor_aerobico",
        "Aerobic Engine",
        indicators,
        maturity=min(95, 70 + len(historical_eff) / 10),
        recommendation=(
            "Consolidate four active weeks and lift the limiting indicator first."
        ),
        anchor_week=historical_best[1].isoformat() if historical_best[1] else None,
        similar_anchor=comparable,
    )
    capability["sport_context"] = sport
    capability["environment_context"] = environment_context
    return capability


def _similar_aerobic_anchor(
    active: list[dict[str, Any]],
    current: list[dict[str, Any]],
    efficiency_key: str,
) -> dict[str, Any] | None:
    if len(active) < 20 or len(current) < 4:
        return None

    def features(rows: list[dict[str, Any]]) -> tuple[float, float, float]:
        hours = sum(_as_float(row.get("hours_week")) or 0 for row in rows) / len(rows)
        efficiency = [
            _as_float(row.get(efficiency_key)) for row in rows
            if _as_float(row.get(efficiency_key)) is not None
        ]
        z2 = [
            _as_float(row.get("z2_pct_mars")) for row in rows
            if _as_float(row.get("z2_pct_mars")) is not None
        ]
        return (
            hours,
            sum(efficiency) / len(efficiency) if efficiency else 0.0,
            sum(z2) / len(z2) if z2 else 0.0,
        )

    current_features = features(current)
    ranges = [
        max([features(active[index:index + 4])[position] for index in range(len(active) - 15)] + [1])
        for position in range(3)
    ]
    candidates = []
    for index in range(0, len(active) - 15):
        block = active[index:index + 4]
        block_features = features(block)
        distance = sum(
            abs(current_features[position] - block_features[position]) / ranges[position]
            for position in range(3)
        ) / 3
        proxy_score = score_ratio(block_features[1], percentile([
            _as_float(row.get(efficiency_key)) for row in active
            if _as_float(row.get(efficiency_key)) is not None
        ], 0.90))
        candidates.append((distance, block[-1]["week_start"], proxy_score))
    if not candidates:
        return None
    distance, week, proxy_score = min(candidates, key=lambda item: item[0])
    return {
        "week": week.isoformat(),
        "similarity": round(max(0.0, 1 - distance), 3),
        "score": proxy_score,
    }


def _weight_capability(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    weighted = [row for row in snapshots if _as_float(row.get("weight_kg")) is not None]
    recent = weighted[-8:]
    latest = _as_float(weighted[-1]["weight_kg"]) if weighted else None
    previous = _as_float(recent[0]["weight_kg"]) if len(recent) >= 2 else None
    all_weights = [_as_float(row["weight_kg"]) for row in weighted]
    personal_low = percentile(all_weights, 0.10)
    trend_score_reference = previous if latest is not None and previous is not None else None
    adherence = len(recent)
    indicators = [
        _indicator(
            "weight_trend", "Recent weight trend", latest,
            trend_score_reference, 0.40, min(len(recent) / 8, 1), inverse=True, unit="kg",
        ),
        _indicator(
            "personal_range", "Position in personal range", latest,
            personal_low, 0.35, min(len(weighted) / 52, 1), inverse=True, unit="kg",
        ),
        _indicator(
            "measurement_adherence", "Measurement continuity", float(adherence),
            8.0, 0.25, 1.0, unit="weeks",
        ),
    ]
    return _capability(
        "composicion_corporal",
        "Body Composition",
        indicators,
        maturity=min(85, len(weighted) / 83 * 80) if weighted else 0,
        recommendation="Keep weekly measurements and register a structured goal in the profile.",
        anchor_week=weighted[all_weights.index(min(all_weights))]["week_start"].isoformat()
        if all_weights else None,
    )


def _simple_capability(
    key: str,
    label: str,
    indicators: list[dict[str, Any]],
    maturity: float,
    recommendation: str,
) -> dict[str, Any]:
    return _capability(key, label, indicators, maturity, recommendation)


def _support_capabilities(conn, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date >= CURRENT_DATE - 27) AS wellness_28,
                COUNT(hr_rest) FILTER (WHERE date >= CURRENT_DATE - 27) AS hr_rest_28,
                AVG(hr_rest) FILTER (WHERE date >= CURRENT_DATE - 27) AS avg_hr_rest,
                AVG(sleep_hours) FILTER (WHERE date >= CURRENT_DATE - 27) AS avg_sleep,
                AVG(fatigue) FILTER (WHERE date >= CURRENT_DATE - 27) AS avg_fatigue,
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY hr_rest)
                    FILTER (WHERE hr_rest IS NOT NULL) AS personal_hr_rest_p25
            FROM wellness
        """)
        (
            wellness_28,
            hr_rest_28,
            avg_hr_rest,
            avg_sleep,
            avg_fatigue,
            personal_hr_rest_p25,
        ) = cur.fetchone()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE start_date >= CURRENT_DATE - 55) AS recent,
                COUNT(DISTINCT DATE_TRUNC('week', start_date))
                    FILTER (WHERE start_date >= CURRENT_DATE - 55) AS active_weeks,
                COUNT(*) AS total
            FROM clean_sessions
            WHERE sport = 'strength_training'
        """)
        strength_recent, strength_weeks, strength_total = cur.fetchone()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date >= CURRENT_DATE - 55) AS recent,
                COUNT(DISTINCT DATE_TRUNC('week', date))
                    FILTER (WHERE date >= CURRENT_DATE - 55) AS active_weeks,
                COUNT(weight_kg) FILTER (WHERE date >= CURRENT_DATE - 55) AS progression_rows,
                COUNT(*) AS total
            FROM fuerza
        """)
        fuerza_recent, fuerza_weeks, progression_rows, fuerza_total = cur.fetchone()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE start_date >= CURRENT_DATE - 55 AND ascent_m >= 500),
                COALESCE(SUM(ascent_m) FILTER (WHERE start_date >= CURRENT_DATE - 27), 0),
                COUNT(*) FILTER (WHERE ascent_m IS NOT NULL),
                PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY ascent_m)
                    FILTER (WHERE ascent_m > 100),
                COALESCE(MAX(ascent_m) FILTER (WHERE start_date >= CURRENT_DATE - 55), 0)
            FROM clean_sessions
        """)
        climb_recent, ascent_recent, ascent_rows, ascent_p90_session, current_max_ascent = cur.fetchone()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date >= CURRENT_DATE - 55),
                COUNT(*) FILTER (
                    WHERE date >= CURRENT_DATE - 55
                      AND COALESCE(moment, '') <> ''
                ),
                COUNT(*) FILTER (
                    WHERE date >= CURRENT_DATE - 55
                      AND COALESCE(gi_response, '') <> ''
                ),
                COUNT(*)
            FROM nutrition_log
        """)
        nutrition_recent, timing_rows, gi_rows, nutrition_total = cur.fetchone()

    recent4 = [row for row in snapshots if int(row.get("sessions") or 0) > 0][-4:]
    historic_ascent = [
        float(row.get("ascent_m_week") or 0)
        for row in snapshots
        if (float(row.get("ascent_m_week") or 0)) > 0
    ]
    recent_rest_days = 28 - sum(int(row.get("active_days") or 0) for row in recent4)
    historical_rest = [
        7 - int(row.get("active_days") or 0) for row in snapshots
        if row.get("active_days") is not None
    ]
    rest_reference = (median(historical_rest) * 4) if historical_rest else None
    recovery = _simple_capability(
        "recuperacion",
        "Recovery",
        [
            _indicator(
                "resting_hr", "Resting HR", _as_float(avg_hr_rest),
                _as_float(personal_hr_rest_p25),
                0.35, min(int(hr_rest_28 or 0) / 14, 1), inverse=True, unit="bpm",
            ),
            _indicator(
                "wellness", "Wellness continuity", float(wellness_28 or 0), 20.0,
                0.35, min(int(wellness_28 or 0) / 20, 1), unit="registros",
            ),
            _indicator(
                "rest_days", "Rest days", float(recent_rest_days),
                _as_float(rest_reference), 0.30, 1.0, unit="days/4 wk",
            ),
        ],
        maturity=min(70, int(wellness_28 or 0) * 2.5),
        recommendation="Log wellness and resting HR consistently for four weeks.",
    )

    # Manual fuerza rows may describe the same sessions already present in Garmin.
    strength_session_count = max(int(strength_recent or 0), int(fuerza_recent or 0))
    strength_active_weeks = max(int(strength_weeks or 0), int(fuerza_weeks or 0))
    strength = _simple_capability(
        "fuerza",
        "Strength",
        [
            _indicator(
                "sessions", "Sessions per week", strength_session_count / 8,
                2.0, 0.40, 1.0 if strength_session_count else 0.0, unit="ses/sem",
            ),
            _indicator(
                "consistency", "Active weeks", float(strength_active_weeks),
                8.0, 0.30, 1.0, unit="weeks",
            ),
            _indicator(
                "progression", "Entries with load", float(progression_rows or 0),
                8.0, 0.30, min(int(progression_rows or 0) / 8, 1), unit="registros",
            ),
        ],
        maturity=min(65, (int(strength_total or 0) + int(fuerza_total or 0)) * 2),
        recommendation="Log two sessions per week with exercise, sets, reps and load.",
    )

    # p75 of climbing-only weeks gives a realistic but ambitious 4-week reference.
    # p90 was using weeks with 0 ascent, underestimating what a good climbing block is.
    ascent_reference = percentile(historic_ascent, 0.75) if historic_ascent else None
    data_confidence = min(int(ascent_rows or 0) / 50, 1)
    climbing = _simple_capability(
        "escalada",
        "Strength-endurance",
        [
            _indicator(
                "weekly_ascent", "Four-week elevation gain", float(ascent_recent or 0),
                (_as_float(ascent_reference) or 0) * 4 or None,
                0.40, data_confidence, unit="m",
            ),
            _indicator(
                "climbing_sessions", "Sessions over 500 m", float(climb_recent or 0),
                4.0, 0.35, data_confidence, unit="sesiones/8 sem",
            ),
            _indicator(
                "best_ascent", "Best single-session gain",
                float(current_max_ascent or 0),
                _as_float(ascent_p90_session) or None,
                0.25, data_confidence, unit="m",
            ),
        ],
        maturity=min(70, int(ascent_rows or 0) / 4),
        recommendation="Accumulate eight weeks with recorded elevation gain and comparable sessions.",
    )
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                ROUND(AVG(avg_altitude_m)::numeric, 0),
                ROUND(MAX(max_altitude_m)::numeric, 0),
                COUNT(*) FILTER (WHERE altitude_band IN ('high', 'very_high')),
                ROUND(AVG(altitude_confidence)::numeric, 2)
            FROM session_environment se
            JOIN clean_sessions cs USING (clean_session_id)
            WHERE cs.start_date >= CURRENT_DATE - 55
              AND se.avg_altitude_m IS NOT NULL
        """)
        avg_altitude, max_altitude, high_sessions, altitude_confidence = cur.fetchone()
    climbing["environment_context"] = {
        "recent_avg_altitude_m": _as_float(avg_altitude),
        "recent_max_altitude_m": _as_float(max_altitude),
        "high_altitude_sessions_8w": int(high_sessions or 0),
        "altitude_confidence": _as_float(altitude_confidence) or 0.0,
        "note": "Altitude is training context; ascent remains a separate climbing stimulus.",
    }

    nutrition = _simple_capability(
        "nutricion_deportiva",
        "Sports Nutrition",
        [
            _indicator(
                "logged_uses", "Sessions with nutrition logged",
                float(nutrition_recent or 0), 8.0, 0.40,
                min(int(nutrition_recent or 0) / 8, 1), unit="registros/8 sem",
            ),
            _indicator(
                "timing", "Timing documented", float(timing_rows or 0),
                float(nutrition_recent or 0) or None, 0.30,
                min(int(nutrition_recent or 0) / 8, 1), unit="registros",
            ),
            _indicator(
                "gi_tolerance", "GI tolerance documented", float(gi_rows or 0),
                float(nutrition_recent or 0) or None, 0.30,
                min(int(gi_rows or 0) / 8, 1), unit="registros",
            ),
        ],
        maturity=min(60, int(nutrition_total or 0) * 4),
        recommendation="Log gels, timing, carbs and GI response across eight sessions.",
    )
    return [recovery, climbing, strength, nutrition]


def _historical_body_composition_block(
    block: list[dict[str, Any]],
    prior_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate one 12-week body composition block using only earlier references."""
    block_start = block[0]["week_start"]
    block_end = block[-1].get("week_end") or block[-1]["week_start"]

    block_weights = sorted(
        [_as_float(row.get("weight_kg")) for row in block
         if _as_float(row.get("weight_kg")) is not None]
    )
    measured_weeks = len(block_weights)
    prior_weights = [
        _as_float(row.get("weight_kg")) for row in prior_snapshots
        if _as_float(row.get("weight_kg")) is not None
    ]

    block_min = block_weights[0] if block_weights else None
    prior_p10 = percentile(prior_weights, 0.10) if len(prior_weights) >= 4 else None

    ordered = [_as_float(row.get("weight_kg")) for row in block
               if _as_float(row.get("weight_kg")) is not None]
    mid = len(ordered) // 2
    start_avg = sum(ordered[:mid]) / mid if mid > 0 else None
    end_avg = (
        sum(ordered[mid:]) / len(ordered[mid:]) if ordered[mid:] else None
    )

    adherence_confidence = min(measured_weeks / 4, 1.0)
    indicators = [
        _indicator(
            "weight_level", "Period minimum weight",
            block_min, prior_p10,
            0.50, adherence_confidence, inverse=True, unit="kg",
        ),
        _indicator(
            "measurement_adherence", "Weeks with measurement",
            float(measured_weeks), 12.0,
            0.30, 1.0, unit="weeks",
        ),
        _indicator(
            "weight_trend", "Period trend",
            end_avg, start_avg,
            0.20, min(measured_weeks / 4, 1.0), inverse=True, unit="kg",
        ),
    ]
    score = weighted_score(indicators)
    confidence = weighted_confidence(indicators)
    valid = measured_weeks >= 3 and prior_p10 is not None and score is not None
    limiting = min(
        (i for i in indicators if i.get("score") is not None),
        key=lambda i: i["score"],
        default=None,
    )
    return {
        "period_start": block_start.isoformat(),
        "period_end": block_end.isoformat(),
        "year": block[len(block) // 2]["week_start"].year,
        "score": score if valid else None,
        "raw_score": score,
        "confidence": confidence,
        "maturity": round(min(85, 20 + len(prior_weights) * 0.8), 1),
        "status": "calculated" if valid else "insufficient_data",
        "reason": (
            None if valid
            else "Se requieren al menos 3 semanas con peso medido y referencias previas."
        ),
        "measured_weeks": measured_weeks,
        "block_min_weight_kg": round(block_min, 1) if block_min is not None else None,
        "prior_reference_p10_kg": round(prior_p10, 1) if prior_p10 is not None else None,
        "limitante_principal": limiting["label"] if limiting else "Insufficient data",
        "indicators": indicators,
    }


def build_body_composition_history(
    snapshots: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    """Build annual best 12-week body composition blocks without future leakage."""
    today = today or date.today()
    complete = [
        row for row in snapshots
        if (row.get("week_end") or row["week_start"]) < today
    ]
    blocks = []
    for end_index in range(11, len(complete)):
        block = complete[end_index - 11:end_index + 1]
        prior = complete[:end_index - 11]
        result = _historical_body_composition_block(block, prior)
        blocks.append(result)

    annual = []
    for year in sorted({row["year"] for row in blocks}):
        candidates = [
            row for row in blocks
            if row["year"] == year and row.get("score") is not None
        ]
        if candidates:
            best = max(
                candidates,
                key=lambda row: (row["score"], row["confidence"], row["period_end"]),
            )
            annual.append({**best, "candidate_blocks": len(candidates)})
        else:
            year_blocks = [row for row in blocks if row["year"] == year]
            annual.append({
                "year": year,
                "score": None,
                "status": "insufficient_data",
                "reason": "Menos de 3 semanas con peso medido en bloques de 12 semanas.",
                "candidate_blocks": 0,
                "evaluated_blocks": len(year_blocks),
                "best_period_tag": "sin_datos_suficientes",
            })

    valid_years = [row for row in annual if row.get("score") is not None]
    comparable_years = [
        row for row in valid_years
        if float(row.get("maturity") or 0) >= 40
        and float(row.get("confidence") or 0) >= 0.60
    ]
    best_year = (
        max(comparable_years, key=lambda row: row["score"])
        if comparable_years else None
    )
    for row in annual:
        row["eligible_for_best_period"] = row in comparable_years
        if row.get("score") is None:
            row.setdefault("best_period_tag", "sin_datos_suficientes")
        elif row not in comparable_years:
            row["best_period_tag"] = "historia_preliminar"
        elif best_year and row["year"] == best_year["year"]:
            row["best_period_tag"] = "mejor_epoca_historica"
        elif best_year and row["score"] >= best_year["score"] * 0.95:
            row["best_period_tag"] = "epoca_destacada"
        elif row["score"] >= 75:
            row["best_period_tag"] = "forma_solida"
        else:
            row["best_period_tag"] = "base_en_desarrollo"

    all_weights_ever = [
        _as_float(row.get("weight_kg")) for row in complete
        if _as_float(row.get("weight_kg")) is not None
    ]
    personal_min = min(all_weights_ever) if all_weights_ever else None

    return {
        "ok": True,
        "version": CAPABILITY_HISTORY_VERSION,
        "capability_key": "composicion_corporal",
        "period_contract": {
            "annual_value": "best_12_week_block_by_minimum_weight",
            "minimum_measured_weeks": 3,
            "score_reference": "prior_p10_weight",
            "best_period_minimum_maturity": 40,
            "best_period_minimum_confidence": 0.60,
            "future_data_used": False,
            "note": "Score 100 = block min weight matched all-time personal p10 at that point in history.",
        },
        "personal_reference": {
            "all_time_min_kg": round(personal_min, 1) if personal_min is not None else None,
            "total_weeks_measured": len(all_weights_ever),
        },
        "years": annual,
        "best_period": best_year,
        "evaluated_blocks": len(blocks),
    }


def calculate_body_composition_history(conn) -> dict[str, Any]:
    history = build_body_composition_history(_load_snapshots(conn))
    with conn.cursor() as cur:
        cur.execute("""
            SELECT score, confidence, maturity, validation_status, created_at
            FROM capability_runs
            WHERE capability_key = 'composicion_corporal'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    history["current_official"] = (
        {
            "score": _as_float(row[0]),
            "confidence": _as_float(row[1]),
            "maturity": _as_float(row[2]),
            "validation_status": row[3],
            "recorded_at": row[4].isoformat(),
        }
        if row else None
    )
    return history


def _load_climbing_sessions(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT start_date, ascent_m
            FROM clean_sessions
            WHERE ascent_m IS NOT NULL AND ascent_m > 0
            ORDER BY start_date
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _historical_climbing_block(
    block: list[dict[str, Any]],
    prior_snapshots: list[dict[str, Any]],
    block_sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    block_start = block[0]["week_start"]
    block_end = block[-1].get("week_end") or block[-1]["week_start"]

    weekly_ascents = sorted([
        _as_float(row.get("ascent_m_week"))
        for row in block
        if (_as_float(row.get("ascent_m_week")) or 0) > 0
    ])
    climb_weeks_count = len(weekly_ascents)
    avg_weekly = sum(weekly_ascents) / climb_weeks_count if weekly_ascents else None

    prior_ascents = [
        _as_float(row.get("ascent_m_week"))
        for row in prior_snapshots
        if (_as_float(row.get("ascent_m_week")) or 0) > 0
    ]
    prior_p90_weekly = percentile(prior_ascents, 0.90) if len(prior_ascents) >= 4 else None

    big_sessions = sum(
        1 for s in block_sessions if (_as_float(s.get("ascent_m")) or 0) >= 500
    )

    data_confidence = min(climb_weeks_count / 6, 1.0)
    sessions_confidence = min(len(prior_ascents) / 50, 1.0)

    indicators = [
        _indicator(
            "weekly_ascent", "Average weekly elevation gain",
            avg_weekly, prior_p90_weekly, 0.40, data_confidence, unit="m",
        ),
        _indicator(
            "climbing_sessions", "Sessions over 500 m",
            float(big_sessions), 4.0, 0.35, sessions_confidence, unit="sesiones/bloque",
        ),
        _indicator(
            "climb_consistency", "Semanas escalando",
            float(climb_weeks_count), 12.0, 0.25, 1.0, unit="semanas/bloque",
        ),
    ]

    score = weighted_score(indicators)
    confidence = weighted_confidence(indicators)
    valid = climb_weeks_count >= 4 and prior_p90_weekly is not None and score is not None
    limiting = min(
        (i for i in indicators if i.get("score") is not None),
        key=lambda i: i["score"],
        default=None,
    )
    return {
        "period_start": block_start.isoformat(),
        "period_end": block_end.isoformat(),
        "year": block[len(block) // 2]["week_start"].year,
        "score": score if valid else None,
        "raw_score": score,
        "confidence": confidence,
        "maturity": round(min(85, 15 + len(prior_ascents) * 0.8), 1),
        "status": "calculated" if valid else "insufficient_data",
        "reason": (
            None if valid
            else "Se requieren al menos 4 semanas con desnivel y referencias previas."
        ),
        "climb_weeks": climb_weeks_count,
        "avg_weekly_ascent_m": round(avg_weekly) if avg_weekly is not None else None,
        "big_sessions": big_sessions,
        "prior_reference_p90_m": round(prior_p90_weekly) if prior_p90_weekly is not None else None,
        "limitante_principal": limiting["label"] if limiting else "Insufficient data",
        "indicators": indicators,
    }


def build_climbing_history(
    snapshots: list[dict[str, Any]],
    climbing_sessions: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    complete = [
        row for row in snapshots
        if (row.get("week_end") or row["week_start"]) < today
    ]
    blocks = []
    for end_index in range(11, len(complete)):
        block = complete[end_index - 11:end_index + 1]
        prior = complete[:end_index - 11]
        block_start = block[0]["week_start"]
        block_end = block[-1].get("week_end") or block[-1]["week_start"]
        block_sessions = [
            s for s in climbing_sessions
            if block_start <= s["start_date"] <= block_end
        ]
        blocks.append(_historical_climbing_block(block, prior, block_sessions))

    annual = []
    for year in sorted({row["year"] for row in blocks}):
        candidates = [row for row in blocks if row["year"] == year and row.get("score") is not None]
        if candidates:
            best = max(candidates, key=lambda row: (row["score"], row["confidence"], row["period_end"]))
            annual.append({**best, "candidate_blocks": len(candidates)})
        else:
            year_blocks = [row for row in blocks if row["year"] == year]
            annual.append({
                "year": year, "score": None, "status": "insufficient_data",
                "reason": "Menos de 4 semanas con desnivel en bloques de 12 semanas.",
                "candidate_blocks": 0, "evaluated_blocks": len(year_blocks),
                "best_period_tag": "sin_datos_suficientes",
            })

    valid_years = [row for row in annual if row.get("score") is not None]
    comparable_years = [
        row for row in valid_years
        if float(row.get("maturity") or 0) >= 40
        and float(row.get("confidence") or 0) >= 0.60
    ]
    best_year = max(comparable_years, key=lambda row: row["score"]) if comparable_years else None

    for row in annual:
        row["eligible_for_best_period"] = row in comparable_years
        if row.get("score") is None:
            row.setdefault("best_period_tag", "sin_datos_suficientes")
        elif row not in comparable_years:
            row["best_period_tag"] = "historia_preliminar"
        elif best_year and row["year"] == best_year["year"]:
            row["best_period_tag"] = "mejor_epoca_historica"
        elif best_year and row["score"] >= best_year["score"] * 0.95:
            row["best_period_tag"] = "epoca_destacada"
        elif row["score"] >= 75:
            row["best_period_tag"] = "forma_solida"
        else:
            row["best_period_tag"] = "base_en_desarrollo"

    all_climbing_weeks = [
        _as_float(row.get("ascent_m_week"))
        for row in complete
        if (_as_float(row.get("ascent_m_week")) or 0) > 0
    ]
    personal_peak_week = max(all_climbing_weeks) if all_climbing_weeks else None

    return {
        "ok": True,
        "version": CAPABILITY_HISTORY_VERSION,
        "capability_key": "escalada",
        "period_contract": {
            "annual_value": "best_12_week_block_by_weekly_ascent",
            "minimum_climb_weeks": 4,
            "score_reference": "prior_p90_weekly_ascent",
            "best_period_minimum_maturity": 40,
            "best_period_minimum_confidence": 0.60,
            "future_data_used": False,
            "note": "Score 100 = block avg weekly ascent matched prior p90 at that point in history.",
        },
        "personal_reference": {
            "all_time_peak_week_m": round(personal_peak_week) if personal_peak_week else None,
            "total_climbing_weeks": len(all_climbing_weeks),
        },
        "years": annual,
        "best_period": best_year,
        "evaluated_blocks": len(blocks),
    }


def calculate_climbing_history(conn) -> dict[str, Any]:
    snapshots = _load_snapshots(conn)
    climbing_sessions = _load_climbing_sessions(conn)
    history = build_climbing_history(snapshots, climbing_sessions)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT score, confidence, maturity, validation_status, created_at
            FROM capability_runs
            WHERE capability_key = 'escalada'
            ORDER BY created_at DESC, id DESC LIMIT 1
        """)
        row = cur.fetchone()
    history["current_official"] = (
        {
            "score": _as_float(row[0]),
            "confidence": _as_float(row[1]),
            "maturity": _as_float(row[2]),
            "validation_status": row[3],
            "recorded_at": row[4].isoformat(),
        }
        if row else None
    )
    return history


def lt_detect_from_records(
    conn,
    sport: str = "cycling",
    min_duration_s: int = 2700,
    bin_width: int = 5,
) -> dict[str, Any]:
    """
    Estimate lactate threshold from session-level aggregates.

    Two methods:
      DMAX — find the HR bin where efficiency (speed/HR) deflects most from the
              line connecting lowest and highest bins. Approximates the transition
              from linear aerobic response to diminishing returns.
      P95 long — p95 of avg_hr in sessions >90 min; proxy for max sustainable effort.

    Limitations: session averages mix intensities within a ride; terrain and altitude
    confound speed-based efficiency. Altitude training inflates HR for the same
    wattage output. Treat as a cross-check against manually calibrated zones, not a
    lab-test replacement.
    """
    sport_filter = (
        list(CYCLING_SPORTS) if sport == "cycling" else list(RUNNING_SPORTS)
    )
    current_lt = {"cycling": 168, "running": 173}.get(sport, 168)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT avg_hr_bpm, efficiency_speed_hr, duration_s
            FROM clean_sessions
            WHERE sport = ANY(%s)
              AND duration_s >= %s
              AND avg_hr_bpm IS NOT NULL AND avg_hr_bpm > 80
              AND efficiency_speed_hr IS NOT NULL AND efficiency_speed_hr > 0.05
            ORDER BY avg_hr_bpm
        """, (sport_filter, min_duration_s))
        rows = cur.fetchall()

    n_sessions = len(rows)
    if n_sessions < 20:
        return {
            "ok": False,
            "sport": sport,
            "reason": f"Datos insuficientes — se requieren ≥20 sesiones largas con HR y eficiencia, hay {n_sessions}.",
            "current_lt_bpm": current_lt,
        }

    sessions = [
        {"hr": float(r[0]), "eff": float(r[1]), "dur": float(r[2])}
        for r in rows
    ]

    # --- Method 1: Efficiency peak deflection ---
    # Only analyse bins in the "training zone" (above p25 of all session HRs).
    # Recovery rides at very low HR have atypical efficiency and confuse DMAX.
    p25_hr = percentile([s["hr"] for s in sessions], 0.25)
    training_sessions = [s for s in sessions if s["hr"] >= p25_hr]

    bins: dict[int, list[float]] = {}
    for s in training_sessions:
        bucket = (int(s["hr"]) // bin_width) * bin_width
        bins.setdefault(bucket, []).append(s["eff"])

    binned = sorted(
        [(hr, median(effs), len(effs)) for hr, effs in bins.items() if len(effs) >= 3]
    )

    # Find the bin with peak efficiency, then find where it drops > 8 % from peak
    dmax_estimate: int | None = None
    dmax_details: dict = {}
    if len(binned) >= 4:
        peak_hr, peak_eff, _ = max(binned, key=lambda t: t[1])
        threshold = peak_eff * 0.92
        # Look for the first bin above the peak where efficiency falls below threshold
        for hr, eff, cnt in binned:
            if hr > peak_hr and eff < threshold:
                dmax_estimate = int(hr)
                dmax_details = {
                    "hr_bin": hr,
                    "eff_median": round(eff, 4),
                    "peak_eff_hr": int(peak_hr),
                    "peak_eff": round(peak_eff, 4),
                    "sessions_in_bin": cnt,
                }
                break

    # --- Method 2: p95 of long sessions (>90 min) ---
    long_hrs = [s["hr"] for s in sessions if s["dur"] >= 5400]
    p95_estimate: int | None = None
    if len(long_hrs) >= 5:
        p95_estimate = int(round(percentile(long_hrs, 0.95)))

    # --- Consensus ---
    estimates = [e for e in (dmax_estimate, p95_estimate) if e is not None]
    if not estimates:
        consensus = None
        confidence = "low"
    elif len(estimates) == 2:
        spread = abs(estimates[0] - estimates[1])
        consensus = round(sum(estimates) / 2)
        confidence = "high" if spread <= 5 else "medium" if spread <= 10 else "low"
    else:
        consensus = estimates[0]
        confidence = "medium"

    # --- Recommendation vs current model ---
    if consensus is None:
        recommendation = "no_estimate"
    else:
        delta = consensus - current_lt
        if abs(delta) <= 3:
            recommendation = "model_confirmed"
        elif abs(delta) <= 7:
            recommendation = "review_suggested"
        else:
            recommendation = "update_recommended"

    # --- HR distribution summary ---
    hr_vals = [s["hr"] for s in sessions]
    distribution = {
        "p50": round(percentile(hr_vals, 0.50)),
        "p75": round(percentile(hr_vals, 0.75)),
        "p90": round(percentile(hr_vals, 0.90)),
        "p95": round(percentile(hr_vals, 0.95)),
        "max": round(max(hr_vals)),
    }

    return {
        "ok": True,
        "sport": sport,
        "n_sessions": n_sessions,
        "n_long_sessions": len(long_hrs),
        "current_lt_bpm": current_lt,
        "method_dmax": {
            "estimate_bpm": dmax_estimate,
            "note": "Deflection point in efficiency-HR curve (5-bpm bins, ≥3 sessions each).",
            **dmax_details,
        },
        "method_p95_long": {
            "estimate_bpm": p95_estimate,
            "sample_size": len(long_hrs),
            "note": "p95 of avg_hr in sessions >90 min — proxy for max sustained effort.",
        },
        "consensus_lt_bpm": consensus,
        "delta_from_current": (consensus - current_lt) if consensus is not None else None,
        "confidence": confidence,
        "recommendation": recommendation,
        "hr_distribution": distribution,
        "caveats": [
            "Session averages mix intensities within a ride; only per-second records give precise LT.",
            "Altitude training (~2300 m) elevates HR for the same workload — detected LT may read lower than sea-level equivalent.",
            "Terrain confounds efficiency: mountain sessions lower speed/HR, biasing the deflection estimate downward for cycling.",
            "Use as a cross-check against calibrated zones, not as an automatic update trigger.",
        ],
        "interpretation": (
            f"Efficiency-based estimates for cycling tend to under-read LT by 5-15 bpm "
            f"when the athlete trains heavily on climbs. "
            f"If the P95-long estimate ({p95_estimate} bpm) agrees with the deflection, "
            f"the zone model is worth revisiting."
            if sport == "cycling" else
            f"Running efficiency is relatively flat across training HRs; "
            f"P95-long ({p95_estimate} bpm) is the primary estimate."
        ),
    }


def readiness(
    capabilities: list[dict[str, Any]],
    event_key: str,
) -> dict[str, Any]:
    """
    Compute event readiness from current capability scores.

    Capabilities with no score contribute 0 — absence of evidence is not proof of
    competence. The confidence field reflects what fraction of the event's weighted
    importance has actual measured data behind it.
    """
    if event_key not in READINESS_EVENTS:
        return {
            "ok": False,
            "reason": f"Evento '{event_key}' no reconocido.",
            "available_events": list(READINESS_EVENTS),
        }
    event = READINESS_EVENTS[event_key]
    weights = event["weights"]
    cap_by_key = {c["key"]: c for c in capabilities}
    total_weight = sum(weights.values())

    components = []
    weighted_sum = 0.0
    data_gaps: list[str] = []

    for cap_key, weight in weights.items():
        cap = cap_by_key.get(cap_key)
        score = _as_float((cap or {}).get("score"))
        cap_status = (cap or {}).get("status", "unknown")
        contribution = (score * weight) if score is not None else 0.0
        weighted_sum += contribution
        if score is None:
            data_gaps.append(cap_key)
        components.append({
            "capability_key": cap_key,
            "nombre": (cap or {}).get("nombre", cap_key),
            "score": score,
            "weight": weight,
            "weighted_contribution": round(contribution, 2),
            "status": cap_status,
            "maturity": (cap or {}).get("maturity"),
            "confidence": (cap or {}).get("confidence"),
        })

    components.sort(key=lambda c: c["weighted_contribution"], reverse=True)
    readiness_score = round(weighted_sum, 1)

    data_weight = sum(c["weight"] for c in components if c["score"] is not None)
    confidence = round(data_weight / total_weight, 2) if total_weight > 0 else 0.0

    if readiness_score >= 90:
        status_tag = "listo"
    elif readiness_score >= 75:
        status_tag = "bien_encaminado"
    elif readiness_score >= 60:
        status_tag = "forma_en_desarrollo"
    elif readiness_score >= 40:
        status_tag = "base_en_construccion"
    else:
        status_tag = "inicio"

    scored = [c for c in components if c["score"] is not None]
    limiting = min(scored, key=lambda c: c["score"], default=None)

    gap_text = (
        "Missing data for: " + ", ".join(data_gaps) + ". Log it for a complete reading."
        if data_gaps else None
    )
    limit_text = (
        f"Limiting factor: {limiting['nombre']} ({limiting['score']:.1f}/100)."
        if limiting else None
    )
    recommendation = " · ".join(filter(None, [gap_text, limit_text]))

    return {
        "ok": True,
        "event_key": event_key,
        "event": event["nombre"],
        "event_type": event["tipo"],
        "descripcion": event["descripcion"],
        "readiness_score": readiness_score,
        "status": status_tag,
        "confidence": confidence,
        "data_gaps": data_gaps,
        "components": components,
        "limiting_factor": limiting["capability_key"] if limiting else None,
        "limiting_factor_nombre": limiting["nombre"] if limiting else None,
        "recommendation": recommendation,
        "note": "Score 0 for capacities without data — absence of evidence, not zero capacity.",
    }


def calculate_readiness(conn, event_key: str) -> dict[str, Any]:
    result = calculate_capabilities(conn)
    return {
        **readiness(result.get("capabilities", []), event_key),
        "generated_from": result.get("generated_from", {}),
    }


# academia() fue movida a academia.py (TD-011 / TD-010C)
# Re-exportada al inicio de este módulo para backward compatibility.

SIMILARITY_WEIGHTS: dict[str, float] = {
    "cycling_efficiency": 0.30,
    "sessions": 0.25,
    "z2_pct_mars": 0.20,
    "km_week": 0.15,
    "weight_kg": 0.10,
}

_SIMILARITY_LABELS: dict[str, str] = {
    "cycling_efficiency": "eficiencia",
    "sessions": "sesiones/sem",
    "z2_pct_mars": "Z2%",
    "km_week": "km/semana",
    "weight_kg": "peso kg",
}

_PATRON_LABELS: dict[str, str] = {
    "pico": "Performance peak",
    "descenso": "Decline in form",
    "abandono": "Training gap",
    "estable": "Steady era",
    "sin_datos": "Not enough data",
}


def _rolling_state(snapshots: list[dict[str, Any]], idx: int, window: int = 4) -> dict[str, float | None]:
    chunk = snapshots[max(0, idx - window + 1): idx + 1]
    result: dict[str, float | None] = {}
    for var in SIMILARITY_WEIGHTS:
        vals = [v for s in chunk if (v := _as_float(s.get(var))) is not None]
        result[var] = sum(vals) / len(vals) if vals else None
    return result


def _normalize_ranges(snapshots: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    from statistics import quantiles as _quantiles
    ranges: dict[str, tuple[float, float]] = {}
    for var in SIMILARITY_WEIGHTS:
        vals = sorted(v for s in snapshots if (v := _as_float(s.get(var))) is not None)
        if len(vals) < 10:
            ranges[var] = (vals[0] if vals else 0.0, vals[-1] if vals else 1.0)
        else:
            q = _quantiles(vals, n=10)
            ranges[var] = (q[0], q[-1])
    return ranges


def _similarity_score(
    v1: dict[str, float | None],
    v2: dict[str, float | None],
    ranges: dict[str, tuple[float, float]],
) -> tuple[float, float]:
    """Returns (similarity 0-1, confidence 0-1). Confidence = weight fraction with data in both."""
    total_weight = sum(SIMILARITY_WEIGHTS.values())
    covered_weight = 0.0
    sq_dist = 0.0
    for var, w in SIMILARITY_WEIGHTS.items():
        a, b = v1.get(var), v2.get(var)
        if a is None or b is None:
            continue
        lo, hi = ranges[var]
        span = hi - lo
        diff = 0.0 if span < 1e-9 else (a - b) / span
        sq_dist += w * diff * diff
        covered_weight += w
    if covered_weight < 1e-9:
        return 0.0, 0.0
    normalized_dist = (sq_dist / covered_weight) ** 0.5
    return round(max(0.0, 1.0 - normalized_dist), 4), round(covered_weight / total_weight, 4)


def _classify_trajectory(
    snapshots: list[dict[str, Any]],
    match_idx: int,
    ref_efficiency: float | None,
    window: int = 8,
) -> tuple[str, dict[str, Any]]:
    """
    Classify the trajectory in the 8 weeks after match_idx.
    Returns (patron, stats_dict).
    """
    after = snapshots[match_idx + 1: match_idx + 1 + window]
    if len(after) < 3:
        return "sin_datos", {}

    total_sessions = sum(_as_float(s.get("sessions")) or 0 for s in after)
    avg_km = sum(_as_float(s.get("km_week")) or 0 for s in after) / len(after)

    if total_sessions < 5:
        return "abandono", {"sesiones_totales": round(total_sessions, 0), "avg_km": round(avg_km, 1)}

    late_effs = [v for s in after[4:] if (v := _as_float(s.get("cycling_efficiency"))) is not None]
    early_effs = [v for s in after[:4] if (v := _as_float(s.get("cycling_efficiency"))) is not None]

    stats: dict[str, Any] = {
        "sesiones_totales": round(total_sessions, 0),
        "avg_km": round(avg_km, 1),
    }

    if ref_efficiency and late_effs:
        avg_late = sum(late_effs) / len(late_effs)
        ratio = avg_late / ref_efficiency
        stats["eficiencia_cambio_pct"] = round((ratio - 1) * 100, 1)
        if ratio > 1.08:
            return "pico", stats
        if ratio < 0.88:
            return "descenso", stats

    if ref_efficiency and early_effs:
        avg_early = sum(early_effs) / len(early_effs)
        ratio_early = avg_early / ref_efficiency
        stats["eficiencia_cambio_pct"] = round((ratio_early - 1) * 100, 1)

    return "estable", stats


def historical_similarity(snapshots: list[dict[str, Any]], top_n: int = 5) -> dict[str, Any]:
    """
    Find historical weeks most similar to current state.
    Excludes the last 12 weeks to avoid near-identical self-comparison.
    """
    if len(snapshots) < 20:
        return {"ok": False, "error": "Insufficient history (minimum 20 weeks)."}

    exclude_recent = 12
    candidate_end = len(snapshots) - exclude_recent
    if candidate_end < 10:
        return {"ok": False, "error": "No hay suficiente historial previo para comparar."}

    current_state = _rolling_state(snapshots, len(snapshots) - 1)
    ranges = _normalize_ranges(snapshots)

    scored: list[tuple[float, float, int]] = []
    for i in range(4, candidate_end):
        state = _rolling_state(snapshots, i)
        sim, conf = _similarity_score(current_state, state, ranges)
        if conf >= 0.40:
            scored.append((sim, conf, i))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top = scored[:top_n]

    matches = []
    patron_counts: dict[str, int] = {}
    for sim, conf, idx in top:
        snap = snapshots[idx]
        state_then = _rolling_state(snapshots, idx)
        ref_eff = state_then.get("cycling_efficiency")
        patron, traj_stats = _classify_trajectory(snapshots, idx, ref_eff)
        patron_counts[patron] = patron_counts.get(patron, 0) + 1

        trajectory = [
            {
                "week": snapshots[j]["week_start"].isoformat() if hasattr(snapshots[j]["week_start"], "isoformat") else str(snapshots[j]["week_start"]),
                "sessions": _as_float(snapshots[j].get("sessions")),
                "km_week": _as_float(snapshots[j].get("km_week")),
                "cycling_efficiency": _as_float(snapshots[j].get("cycling_efficiency")),
                "z2_pct_mars": _as_float(snapshots[j].get("z2_pct_mars")),
            }
            for j in range(idx + 1, min(idx + 9, len(snapshots)))
        ]

        state_display = {
            _SIMILARITY_LABELS.get(k, k): round(v, 4) if v is not None else None
            for k, v in state_then.items()
        }

        matches.append({
            "week": snap["week_start"].isoformat() if hasattr(snap["week_start"], "isoformat") else str(snap["week_start"]),
            "similarity": sim,
            "confidence": conf,
            "estado_entonces": state_display,
            "patron": patron,
            "patron_label": _PATRON_LABELS.get(patron, patron),
            "trajectory_stats": traj_stats,
            "trajectory": trajectory,
        })

    dominant = max(patron_counts, key=patron_counts.get) if patron_counts else "sin_datos"
    dominant_count = patron_counts.get(dominant, 0)
    mensaje = _similarity_mensaje(patron_counts, dominant, dominant_count, len(matches))

    current_display = {
        _SIMILARITY_LABELS.get(k, k): round(v, 4) if v is not None else None
        for k, v in current_state.items()
    }

    return {
        "ok": True,
        "current_state": current_display,
        "matches": matches,
        "patron_distribution": {_PATRON_LABELS.get(k, k): v for k, v in patron_counts.items()},
        "mensaje": mensaje,
        "metadata": {
            "semanas_analizadas": candidate_end,
            "semanas_excluidas_recientes": exclude_recent,
            "min_confidence": 0.40,
        },
    }


def _similarity_mensaje(
    counts: dict[str, int],
    dominant: str,
    dominant_count: int,
    total: int,
) -> str:
    if total == 0:
        return "No sufficiently similar eras were found in the history."
    parts = [f"{_PATRON_LABELS.get(k, k).lower()} ({v})" for k, v in counts.items()]
    dist = ", ".join(parts)
    if dominant == "pico" and dominant_count >= 2:
        return f"Of your {total} most similar eras, {dominant_count} led to a performance peak. History suggests upward potential. Distribution: {dist}."
    if dominant == "abandono" and dominant_count >= 2:
        return f"Of your {total} similar eras, {dominant_count} ended in a training gap. Worth reviewing what interrupted those eras. Distribution: {dist}."
    if dominant == "descenso" and dominant_count >= 2:
        return f"Of your {total} similar eras, {dominant_count} led to a decline in form. Distribution: {dist}."
    return f"Mixed history: {dist}. No clear dominant pattern."


def calculate_historical_similarity(conn) -> dict[str, Any]:
    snapshots = _load_snapshots(conn)
    if not snapshots:
        raise ValueError("athlete_snapshots is empty")
    return historical_similarity(snapshots)


def calculate_capabilities(conn) -> dict[str, Any]:
    snapshots = _load_snapshots(conn)
    if not snapshots:
        raise ValueError("athlete_snapshots is empty")
    capabilities = [
        _aerobic_capability(conn, snapshots),
        _weight_capability(snapshots),
        *_support_capabilities(conn, snapshots),
    ]
    return {
        "ok": True,
        "version": CAPABILITY_VERSION,
        "generated_from": {
            "source": "clean_sessions + athlete_snapshots + personal logs",
            "snapshot_weeks": len(snapshots),
            "latest_week": snapshots[-1]["week_start"].isoformat(),
        },
        "rules": {
            "personal_history_only": True,
            "score_and_confidence_separate": True,
            "missing_data_is_not_zero_performance": True,
            "uses_z2_pct_mars_only": True,
        },
        "capabilities": capabilities,
    }
