"""
routers/gpt_environment.py — Altitude & session environment endpoints
TD-010A split de gpt_analytics.py
"""
import logging
from fastapi import APIRouter, HTTPException
from db import get_db, _ensure_session_environment_table

logger = logging.getLogger("mars_fit")
router = APIRouter(tags=["gpt_environment"])


@router.get("/gpt/environment-summary")
def gpt_environment_summary():
    """Altitude exposure and travel context from session_environment."""
    conn = get_db()
    if not conn:
        raise HTTPException(503, "DB no disponible")
    try:
        _ensure_session_environment_table(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS sessions,
                    COUNT(avg_altitude_m) AS with_altitude,
                    ROUND(AVG(habitual_altitude_m)::numeric, 0) AS habitual_altitude_m,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'above_habitual', 'well_above_habitual'
                    )) AS above_habitual,
                    COUNT(*) FILTER (WHERE relative_altitude_band IN (
                        'below_habitual', 'well_below_habitual'
                    )) AS below_habitual
                FROM session_environment
            """)
            sessions, with_altitude, habitual, above, below = cur.fetchone()
            cur.execute("""
                SELECT altitude_band, COUNT(*)
                FROM session_environment
                GROUP BY altitude_band
                ORDER BY COUNT(*) DESC
            """)
            bands = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute("""
                SELECT country_code, region_label, COUNT(*)
                FROM session_environment
                WHERE country_code IS NOT NULL
                GROUP BY country_code, region_label
                ORDER BY COUNT(*) DESC
            """)
            countries = [
                {"code": row[0], "label": row[1], "sessions": row[2]}
                for row in cur.fetchall()
            ]
            cur.execute("""
                SELECT se.clean_session_id, cs.start_time, cs.name, cs.sport,
                       se.avg_altitude_m, se.max_altitude_m, se.ascent_m,
                       se.country_code, se.region_label, se.relative_altitude_band,
                       se.prior_21d_exposure_days, se.acclimatization_status,
                       se.altitude_confidence
                FROM session_environment se
                JOIN clean_sessions cs USING (clean_session_id)
                WHERE se.avg_altitude_m IS NOT NULL
                ORDER BY se.avg_altitude_m DESC
                LIMIT 12
            """)
            columns = [description[0] for description in cur.description]
            highest = [dict(zip(columns, row)) for row in cur.fetchall()]
        for item in highest:
            if item.get("start_time"):
                item["start_time"] = item["start_time"].isoformat()
            for key in ("avg_altitude_m", "max_altitude_m", "altitude_confidence"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
        return {
            "ok": True, "sessions": sessions, "with_altitude": with_altitude,
            "coverage_pct": round(with_altitude / sessions * 100, 1) if sessions else 0,
            "habitual_altitude_m": float(habitual) if habitual is not None else None,
            "above_habitual": above, "below_habitual": below,
            "altitude_bands": bands, "countries": countries,
            "highest_sessions": highest,
            "interpretation": {
                "absolute_altitude": "Elevation above sea level.",
                "ascent": "Accumulated climbing inside the session.",
                "acclimatization": "Comparable-altitude training days in the prior 21 days.",
                "performance_use": "Context for comparison; it does not automatically add fitness points.",
            },
        }
    except Exception as e:
        logger.error(f"Environment summary error: {e}")
        raise HTTPException(500, str(e))
