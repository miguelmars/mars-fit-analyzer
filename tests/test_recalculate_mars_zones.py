import unittest
from datetime import date, datetime, timedelta

from capability_engine import (
    CAPABILITY_EDUCATION,
    GLOSSARY,
    SIMILARITY_WEIGHTS,
    _classify_trajectory,
    _historical_aerobic_block,
    _historical_body_composition_block,
    _historical_climbing_block,
    _normalize_ranges,
    _rolling_state,
    _similarity_score,
    academia,
    build_aerobic_history,
    build_body_composition_history,
    build_climbing_history,
    historical_similarity,
    inverse_score_ratio,
    readiness,
    score_ratio,
    validate_capability,
    weighted_confidence,
    weighted_score,
)
from tools.backfill_session_environment import (
    altitude_band,
    infer_country,
    relative_altitude_band,
    telemetry_altitude,
)
from tools.recalculate_mars_zones import (
    _build_snapshot_rollup,
    _match_telemetry,
    _numeric_changed,
    extract_original_z2,
    zone_result,
)


class MarsZoneRecalculationTests(unittest.TestCase):
    def test_resume_only_writes_changed_values(self):
        self.assertFalse(_numeric_changed(42.0, 42.0))
        self.assertTrue(_numeric_changed(None, 42.0))
        self.assertFalse(_numeric_changed(None, None))

    def test_telemetry_uses_elapsed_time(self):
        result = zone_result(
            "cycling",
            None,
            [(0, 140), (10, 120)],
            "exact",
            11,
        )
        self.assertEqual(result["z2_pct_mars"], 90.91)
        self.assertEqual(result["z2_confidence_score"], 1.0)

    def test_session_average_is_marked_as_estimate(self):
        result = zone_result("running", 145)
        self.assertEqual(result["z2_pct_mars"], 100.0)
        self.assertEqual(result["zone_confidence"], "session_average_estimate")
        self.assertEqual(result["z2_confidence_score"], 0.4)

    def test_non_aerobic_sport_is_not_eligible(self):
        result = zone_result("strength_training", 140)
        self.assertFalse(result["eligible"])

    def test_original_z2_is_extracted_without_reinterpretation(self):
        self.assertEqual(extract_original_z2({"pct_z2": "42.25"}), 42.25)
        self.assertIsNone(extract_original_z2({"pct_z2": 140}))

    def test_heuristic_match_is_one_to_one(self):
        old = {
            "session_id": "old-1",
            "dt": datetime(2025, 1, 2, 6),
            "distance_km": 20.0,
            "duration_s": 3600,
            "samples": [(0, 140)],
        }
        row = {
            "clean_session_id": "new-1",
            "original_session_id": "",
            "start_time": datetime(2025, 1, 2, 12),
            "distance_km": 20.01,
            "duration_s": 3610,
        }
        used = set()
        _, match, info = _match_telemetry(
            row,
            {"old-1": old},
            {"2025-01-02": [old]},
            used,
        )
        self.assertEqual(match, "linked_high")
        self.assertEqual(info["offset_hours"], 6)
        self.assertEqual(used, {"old-1"})

        _, duplicate_match, _ = _match_telemetry(
            row,
            {"old-1": old},
            {"2025-01-02": [old]},
            used,
        )
        self.assertIsNone(duplicate_match)

    def test_weekly_rollup_weights_duration_and_confidence(self):
        rows = [
            {
                "start_time": datetime(2026, 6, 1, 8),
                "duration_s": 3600,
                "z2_pct_original": None,
                "z2_pct_mars": 50.0,
                "z2_confidence_score": 1.0,
                "zone_model_used": "cycling-model",
            },
            {
                "start_time": datetime(2026, 6, 2, 8),
                "duration_s": 1800,
                "z2_pct_original": None,
                "z2_pct_mars": 100.0,
                "z2_confidence_score": 0.4,
                "zone_model_used": "cycling-model",
            },
        ]
        result = _build_snapshot_rollup(rows)
        self.assertEqual(result[0]["z2_pct_mars"], 66.67)
        self.assertEqual(result[0]["z2_confidence_score"], 0.8)
        self.assertEqual(result[0]["zone_confidence"], "telemetry_dominant")


class CapabilityMathTests(unittest.TestCase):
    @staticmethod
    def capability(indicator_scores=(80, 60)):
        indicators = [
            {
                "key": "one",
                "label": "Uno",
                "score": indicator_scores[0],
                "weight": 0.6,
                "confidence": 1.0,
            },
            {
                "key": "two",
                "label": "Dos",
                "score": indicator_scores[1],
                "weight": 0.4,
                "confidence": 0.8,
            },
        ]
        return {
            "key": "motor_aerobico",
            "score": weighted_score(indicators),
            "indicators": indicators,
            "anchors": {
                "historical": {"status": "available", "week": "2023-03-13"},
                "similar_era": {"status": "available", "week": "2024-01-08"},
            },
        }

    def test_score_ratio_is_capped(self):
        self.assertEqual(score_ratio(150, 120), 100.0)
        self.assertEqual(score_ratio(60, 120), 50.0)
        self.assertEqual(inverse_score_ratio(90, 81), 90.0)

    def test_score_and_confidence_are_independent(self):
        indicators = [
            {"score": 80, "weight": 0.6, "confidence": 1.0},
            {"score": 40, "weight": 0.4, "confidence": 0.25},
        ]
        self.assertEqual(weighted_score(indicators), 64.0)
        self.assertEqual(weighted_confidence(indicators), 0.7)

    def test_missing_indicator_does_not_become_zero(self):
        indicators = [
            {"score": 80, "weight": 0.5, "confidence": 1.0},
            {"score": None, "weight": 0.5, "confidence": 0.0},
        ]
        self.assertIsNone(weighted_score(indicators))

    def test_capability_validation_accepts_exact_math_and_double_anchor(self):
        report = validate_capability(self.capability())
        self.assertEqual(report["status"], "accepted")
        self.assertTrue(report["arithmetic_valid"])
        self.assertTrue(report["anchors_valid"])
        self.assertEqual(report["indicators"][0]["weighted_contribution"], 48.0)

    def test_capability_validation_flags_large_indicator_change(self):
        previous = self.capability((50, 60))
        report = validate_capability(self.capability((80, 60)), previous)
        self.assertEqual(report["status"], "review_required")
        self.assertTrue(report["indicators"][0]["requires_review"])
        self.assertEqual(report["indicators"][0]["delta_points"], 30.0)

    def test_capability_validation_blocks_missing_similar_anchor(self):
        capability = self.capability()
        capability["anchors"]["similar_era"] = {"status": "insufficient_data"}
        report = validate_capability(capability)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("double_anchor", report["blockers"])

    def test_capability_validation_reports_invalid_weights(self):
        capability = self.capability()
        capability["indicators"][0]["weight"] = 0.5
        report = validate_capability(capability)
        self.assertEqual(report["status"], "invalid")
        self.assertFalse(report["arithmetic_valid"])
        self.assertIn("arithmetic", report["blockers"])

    @staticmethod
    def aerobic_week(week_start, efficiency=0.15, z2=50.0):
        return {
            "week_start": week_start,
            "week_end": week_start + timedelta(days=6),
            "sessions": 1,
            "cycling_efficiency": efficiency,
            "running_efficiency": None,
            "z2_pct_mars": z2,
            "z2_confidence_score": 1.0,
            "sport_breakdown": {
                "cycling": {"hours": 2.0, "sessions": 1, "km": 40.0},
            },
        }

    @staticmethod
    def aerobic_session(day, efficiency=0.15, distance=40.0, altitude=2300):
        return {
            "start_date": day,
            "sport": "cycling",
            "distance_km": distance,
            "duration_s": 7200,
            "efficiency_speed_hr": efficiency,
            "avg_altitude_m": altitude,
            "habitual_altitude_m": 2310,
            "altitude_confidence": 1.0,
        }

    def test_historical_block_does_not_use_future_sessions(self):
        start = date(2020, 1, 6)
        prior = [self.aerobic_week(start + timedelta(weeks=i)) for i in range(12)]
        block = [
            self.aerobic_week(start + timedelta(weeks=12 + i), efficiency=0.16, z2=60)
            for i in range(12)
        ]
        sessions = [
            self.aerobic_session(start + timedelta(weeks=i))
            for i in range(12)
        ] + [
            self.aerobic_session(start + timedelta(weeks=12 + i), efficiency=0.16)
            for i in range(12)
        ] + [
            self.aerobic_session(start + timedelta(weeks=30), efficiency=0.40)
        ]
        result = _historical_aerobic_block(block, prior, sessions)
        efficiency = next(
            item for item in result["indicators"] if item["key"] == "efficiency"
        )
        self.assertEqual(result["status"], "calculated")
        self.assertEqual(result["environment_context"]["reference_sample"], 12)
        self.assertEqual(efficiency["reference"], 0.15)
        self.assertFalse(result["environment_context"]["future_data_used"])

    def test_aerobic_history_selects_one_best_block_per_year(self):
        start = date(2020, 1, 6)
        snapshots = [
            self.aerobic_week(
                start + timedelta(weeks=i),
                efficiency=0.14 if i < 16 else 0.16,
                z2=45 if i < 16 else 60,
            )
            for i in range(180)
        ]
        sessions = [
            self.aerobic_session(
                start + timedelta(weeks=i),
                efficiency=0.14 if i < 16 else 0.16,
                distance=35 if i < 16 else 50,
            )
            for i in range(180)
        ]
        history = build_aerobic_history(
            snapshots,
            sessions,
            today=date(2025, 1, 1),
        )
        valid = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(valid)
        self.assertEqual(
            sum(row["best_period_tag"] == "mejor_epoca_historica" for row in valid),
            1,
        )
        self.assertFalse(history["period_contract"]["future_data_used"])

    def test_preliminary_history_cannot_be_named_best_period(self):
        start = date(2018, 1, 1)
        snapshots = [
            self.aerobic_week(start + timedelta(weeks=i), efficiency=0.16)
            for i in range(30)
        ]
        sessions = [
            self.aerobic_session(start + timedelta(weeks=i), efficiency=0.16)
            for i in range(30)
        ]
        history = build_aerobic_history(
            snapshots,
            sessions,
            today=date(2019, 1, 1),
        )
        scored = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(scored)
        self.assertTrue(all(not row["eligible_for_best_period"] for row in scored))
        self.assertTrue(all(row["best_period_tag"] == "historia_preliminar" for row in scored))
        self.assertIsNone(history["best_period"])


class BodyCompositionHistoryTests(unittest.TestCase):
    """Tests for E25B: Composición Corporal capability history."""

    @staticmethod
    def weight_week(week_start, weight_kg=None):
        return {
            "week_start": week_start,
            "week_end": week_start + timedelta(days=6),
            "weight_kg": weight_kg,
        }

    def _prior_with_weights(self, start, n_weeks, weight=75.0):
        return [self.weight_week(start + timedelta(weeks=i), weight) for i in range(n_weeks)]

    def test_block_requires_three_measured_weeks(self):
        start = date(2020, 1, 6)
        prior = self._prior_with_weights(start, 30)
        # Block with only 2 measured weeks
        block = [self.weight_week(start + timedelta(weeks=30 + i)) for i in range(12)]
        block[0]["weight_kg"] = 73.0
        block[6]["weight_kg"] = 74.0
        result = _historical_body_composition_block(block, prior)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["score"])
        self.assertEqual(result["measured_weeks"], 2)

    def test_block_requires_prior_reference(self):
        start = date(2020, 1, 6)
        # Block with 6 measured weeks but no prior data at all
        block = [self.weight_week(start + timedelta(weeks=i), 74.0) for i in range(12)]
        result = _historical_body_composition_block(block, prior_snapshots=[])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["score"])

    def test_block_valid_with_enough_data(self):
        start = date(2020, 1, 6)
        prior = self._prior_with_weights(start, 30, weight=80.0)
        block = [self.weight_week(start + timedelta(weeks=30 + i), 75.0) for i in range(12)]
        result = _historical_body_composition_block(block, prior)
        self.assertEqual(result["status"], "calculated")
        self.assertIsNotNone(result["score"])
        self.assertEqual(result["measured_weeks"], 12)
        self.assertIsNotNone(result["block_min_weight_kg"])
        self.assertIsNotNone(result["prior_reference_p10_kg"])

    def test_history_does_not_use_future_data(self):
        # All blocks built by build_body_composition_history must only
        # reference prior_snapshots that predate the block start.
        start = date(2020, 1, 6)
        # 60 weeks of data: 30 prior + 12 block + 18 future
        snapshots = [self.weight_week(start + timedelta(weeks=i), 75.0) for i in range(60)]
        today = start + timedelta(weeks=42)  # cut before the last 18 weeks
        history = build_body_composition_history(snapshots, today=today)
        self.assertFalse(history["period_contract"]["future_data_used"])

    def test_history_selects_one_best_block_per_year(self):
        start = date(2018, 1, 1)
        # Build 4 years of weekly snapshots with weight on every week
        snapshots = []
        for i in range(208):  # 4 years
            w = 70.0 if i < 52 else 80.0  # 2018 is lighter (better)
            snapshots.append(self.weight_week(start + timedelta(weeks=i), w))
        history = build_body_composition_history(snapshots, today=date(2022, 1, 1))
        valid = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(valid)
        best_tags = [row for row in valid if row["best_period_tag"] == "mejor_epoca_historica"]
        self.assertEqual(len(best_tags), 1)

    def test_preliminary_history_cannot_be_best_period(self):
        # maturity requires >= 25 prior measured weeks; with only 12 prior weeks,
        # all blocks will be tagged historia_preliminar.
        start = date(2022, 1, 1)
        # Only 24 total weeks — not enough prior for maturity >= 40%
        snapshots = [self.weight_week(start + timedelta(weeks=i), 75.0) for i in range(24)]
        history = build_body_composition_history(snapshots, today=date(2023, 1, 1))
        scored = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(scored)
        self.assertTrue(all(not row["eligible_for_best_period"] for row in scored))
        self.assertTrue(
            all(row["best_period_tag"] == "historia_preliminar" for row in scored)
        )
        self.assertIsNone(history["best_period"])

    def test_personal_reference_tracks_all_time_minimum(self):
        start = date(2020, 1, 6)
        snapshots = [self.weight_week(start + timedelta(weeks=i), 75.0) for i in range(60)]
        snapshots[10]["weight_kg"] = 69.5  # all-time minimum
        history = build_body_composition_history(snapshots, today=date(2022, 1, 1))
        self.assertEqual(history["personal_reference"]["all_time_min_kg"], 69.5)
        self.assertEqual(history["personal_reference"]["total_weeks_measured"], 60)


class ClimbingHistoryTests(unittest.TestCase):
    """Tests for E25D: Escalada capability history."""

    @staticmethod
    def climb_week(week_start, ascent_m=None):
        return {
            "week_start": week_start,
            "week_end": week_start + timedelta(days=6),
            "ascent_m_week": ascent_m,
        }

    @staticmethod
    def climb_session(day, ascent_m=600):
        return {"start_date": day, "ascent_m": ascent_m}

    def _prior_with_climbs(self, start, n_weeks, ascent=800):
        return [self.climb_week(start + timedelta(weeks=i), ascent) for i in range(n_weeks)]

    def test_block_requires_four_climbing_weeks(self):
        start = date(2020, 1, 6)
        prior = self._prior_with_climbs(start, 30)
        block = [self.climb_week(start + timedelta(weeks=30 + i)) for i in range(12)]
        # Only 3 weeks with ascent
        for i in [0, 4, 8]:
            block[i]["ascent_m_week"] = 500
        result = _historical_climbing_block(block, prior, [])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["score"])

    def test_block_requires_prior_reference(self):
        start = date(2020, 1, 6)
        block = [self.climb_week(start + timedelta(weeks=i), 800) for i in range(12)]
        result = _historical_climbing_block(block, prior_snapshots=[], block_sessions=[])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["score"])

    def test_block_counts_big_sessions(self):
        start = date(2020, 1, 6)
        prior = self._prior_with_climbs(start, 30)
        block = [self.climb_week(start + timedelta(weeks=30 + i), 800) for i in range(12)]
        sessions = [
            self.climb_session(start + timedelta(weeks=30 + i), 600) for i in range(5)
        ] + [
            self.climb_session(start + timedelta(weeks=30 + i), 300) for i in range(5, 12)
        ]
        result = _historical_climbing_block(block, prior, sessions)
        self.assertEqual(result["status"], "calculated")
        self.assertEqual(result["big_sessions"], 5)

    def test_history_does_not_use_future_data(self):
        start = date(2020, 1, 6)
        snapshots = [self.climb_week(start + timedelta(weeks=i), 800) for i in range(60)]
        sessions = [self.climb_session(start + timedelta(weeks=i)) for i in range(60)]
        today = start + timedelta(weeks=42)
        history = build_climbing_history(snapshots, sessions, today=today)
        self.assertFalse(history["period_contract"]["future_data_used"])

    def test_history_selects_one_best_block_per_year(self):
        start = date(2018, 1, 1)
        snapshots = [
            self.climb_week(start + timedelta(weeks=i), 800 if i < 104 else 1500)
            for i in range(208)
        ]
        sessions = [
            self.climb_session(start + timedelta(weeks=i), 600 if i < 104 else 1200)
            for i in range(208)
        ]
        history = build_climbing_history(snapshots, sessions, today=date(2022, 1, 1))
        valid = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(valid)
        best_tags = [row for row in valid if row["best_period_tag"] == "mejor_epoca_historica"]
        self.assertEqual(len(best_tags), 1)

    def test_preliminary_history_cannot_be_best_period(self):
        start = date(2022, 1, 1)
        snapshots = [self.climb_week(start + timedelta(weeks=i), 800) for i in range(24)]
        sessions = [self.climb_session(start + timedelta(weeks=i)) for i in range(24)]
        history = build_climbing_history(snapshots, sessions, today=date(2023, 1, 1))
        scored = [row for row in history["years"] if row.get("score") is not None]
        self.assertTrue(scored)
        self.assertTrue(all(not row["eligible_for_best_period"] for row in scored))

    def test_personal_reference_tracks_peak_week(self):
        start = date(2020, 1, 6)
        snapshots = [self.climb_week(start + timedelta(weeks=i), 800) for i in range(60)]
        snapshots[10]["ascent_m_week"] = 4500
        sessions = []
        history = build_climbing_history(snapshots, sessions, today=date(2022, 1, 1))
        self.assertEqual(history["personal_reference"]["all_time_peak_week_m"], 4500)


class ReadinessTests(unittest.TestCase):
    """Tests for E26: Goal Engine readiness()."""

    def _caps(self, aerobico=80.0, escalada=70.0, composicion=90.0,
               nutricion=None, recuperacion=None, fuerza=None):
        def cap(key, nombre, score):
            return {"key": key, "nombre": nombre, "score": score,
                    "status": "calculated" if score is not None else "insufficient_data",
                    "confidence": 0.9 if score is not None else 0.0,
                    "maturity": 80 if score is not None else 0}
        return [
            cap("motor_aerobico", "Motor Aeróbico", aerobico),
            cap("escalada", "Escalada", escalada),
            cap("composicion_corporal", "Composición Corporal", composicion),
            cap("nutricion_deportiva", "Nutrición Deportiva", nutricion),
            cap("recuperacion", "Recuperación", recuperacion),
            cap("fuerza", "Fuerza", fuerza),
        ]

    def test_unknown_event_returns_error(self):
        r = readiness(self._caps(), "evento_inventado")
        self.assertFalse(r["ok"])
        self.assertIn("available_events", r)

    def test_weighted_sum_is_correct(self):
        # Escalera: aerobico 0.35, escalada 0.30, composicion 0.15, nutri 0.10, recup 0.10
        # All data present: 80*0.35 + 70*0.30 + 90*0.15 = 28 + 21 + 13.5 = 62.5
        caps = self._caps(aerobico=80.0, escalada=70.0, composicion=90.0,
                          nutricion=None, recuperacion=None)
        r = readiness(caps, "escalera_al_infierno")
        self.assertTrue(r["ok"])
        self.assertEqual(r["readiness_score"], 62.5)

    def test_none_score_contributes_zero(self):
        caps = self._caps(aerobico=100.0, escalada=100.0, composicion=100.0,
                          nutricion=None, recuperacion=None)
        r = readiness(caps, "escalera_al_infierno")
        # max possible with 2 gaps: 100*0.35 + 100*0.30 + 100*0.15 = 80.0
        self.assertEqual(r["readiness_score"], 80.0)
        self.assertIn("nutricion_deportiva", r["data_gaps"])
        self.assertIn("recuperacion", r["data_gaps"])

    def test_confidence_reflects_data_coverage(self):
        # With 3 of 5 capabilities (weights 0.35+0.30+0.15=0.80 of 1.0)
        caps = self._caps(aerobico=80.0, escalada=70.0, composicion=90.0,
                          nutricion=None, recuperacion=None)
        r = readiness(caps, "escalera_al_infierno")
        self.assertEqual(r["confidence"], 0.80)

    def test_full_data_confidence_is_one(self):
        caps = self._caps(aerobico=80.0, escalada=70.0, composicion=90.0,
                          nutricion=50.0, recuperacion=60.0)
        r = readiness(caps, "escalera_al_infierno")
        self.assertEqual(r["confidence"], 1.0)

    def test_limiting_factor_is_lowest_scored(self):
        caps = self._caps(aerobico=90.0, escalada=40.0, composicion=85.0,
                          nutricion=70.0, recuperacion=65.0)
        r = readiness(caps, "escalera_al_infierno")
        self.assertEqual(r["limiting_factor"], "escalada")

    def test_status_tags_are_correct(self):
        r90 = readiness(self._caps(aerobico=100, escalada=100, composicion=100,
                                    nutricion=100, recuperacion=100), "escalera_al_infierno")
        self.assertEqual(r90["status"], "listo")
        r60 = readiness(self._caps(aerobico=60, escalada=60, composicion=60,
                                    nutricion=60, recuperacion=60), "escalera_al_infierno")
        self.assertEqual(r60["status"], "forma_en_desarrollo")

    def test_components_sorted_by_contribution(self):
        caps = self._caps(aerobico=80.0, escalada=70.0, composicion=90.0,
                          nutricion=None, recuperacion=None)
        r = readiness(caps, "escalera_al_infierno")
        contribs = [c["weighted_contribution"] for c in r["components"]]
        self.assertEqual(contribs, sorted(contribs, reverse=True))


def _make_snap(week_start, cycling_efficiency=None, sessions=None, z2_pct_mars=None, km_week=None, weight_kg=None):
    return {
        "week_start": date.fromisoformat(week_start),
        "cycling_efficiency": cycling_efficiency,
        "sessions": sessions,
        "z2_pct_mars": z2_pct_mars,
        "km_week": km_week,
        "weight_kg": weight_kg,
    }


class HistoricalSimilarityTests(unittest.TestCase):
    def _build_snapshots(self, n=30, base_eff=0.155, base_sessions=4):
        snaps = []
        for i in range(n):
            d = date(2022, 1, 3) + timedelta(weeks=i)
            snaps.append(_make_snap(
                d.isoformat(),
                cycling_efficiency=base_eff + (i % 5) * 0.002,
                sessions=base_sessions + (i % 3),
                z2_pct_mars=60.0 + (i % 10),
                km_week=120.0 + (i % 8) * 5,
            ))
        return snaps

    def test_requires_minimum_history(self):
        result = historical_similarity([_make_snap("2024-01-01")] * 10)
        self.assertFalse(result["ok"])

    def test_excludes_recent_12_weeks(self):
        snaps = self._build_snapshots(30)
        result = historical_similarity(snaps)
        if result["ok"]:
            cutoff = snaps[-12]["week_start"]
            for m in result["matches"]:
                self.assertLess(date.fromisoformat(m["week"]), cutoff)

    def test_returns_top_n_matches(self):
        snaps = self._build_snapshots(40)
        result = historical_similarity(snaps, top_n=3)
        if result["ok"]:
            self.assertLessEqual(len(result["matches"]), 3)

    def test_similarity_is_highest_first(self):
        snaps = self._build_snapshots(40)
        result = historical_similarity(snaps, top_n=5)
        if result["ok"] and len(result["matches"]) >= 2:
            sims = [m["similarity"] for m in result["matches"]]
            self.assertEqual(sims, sorted(sims, reverse=True))

    def test_rolling_state_averages_window(self):
        snaps = [_make_snap("2024-01-01", cycling_efficiency=0.10),
                 _make_snap("2024-01-08", cycling_efficiency=0.20),
                 _make_snap("2024-01-15", cycling_efficiency=0.30)]
        state = _rolling_state(snaps, 2, window=3)
        self.assertAlmostEqual(state["cycling_efficiency"], 0.20, places=5)

    def test_rolling_state_handles_none(self):
        snaps = [_make_snap("2024-01-01", cycling_efficiency=None),
                 _make_snap("2024-01-08", cycling_efficiency=0.15)]
        state = _rolling_state(snaps, 1, window=2)
        self.assertAlmostEqual(state["cycling_efficiency"], 0.15, places=5)

    def test_similarity_identical_vectors_is_one(self):
        snaps = [_make_snap("2024-01-01", cycling_efficiency=0.155, sessions=4, z2_pct_mars=65.0, km_week=120.0)] * 10
        ranges = _normalize_ranges(snaps)
        v = _rolling_state(snaps, 5)
        sim, conf = _similarity_score(v, v, ranges)
        self.assertAlmostEqual(sim, 1.0, places=3)

    def test_similarity_all_none_returns_zero_confidence(self):
        ranges = {k: (0.0, 1.0) for k in SIMILARITY_WEIGHTS}
        v1 = {k: None for k in SIMILARITY_WEIGHTS}
        v2 = {k: 0.5 for k in SIMILARITY_WEIGHTS}
        sim, conf = _similarity_score(v1, v2, ranges)
        self.assertEqual(conf, 0.0)
        self.assertEqual(sim, 0.0)

    def test_classify_trajectory_abandono(self):
        base = _make_snap("2024-01-01", sessions=5, cycling_efficiency=0.155, km_week=120)
        dead = [_make_snap(f"2024-{m:02d}-01", sessions=0, cycling_efficiency=0.10, km_week=0) for m in range(2, 10)]
        patron, stats = _classify_trajectory([base] + dead, 0, 0.155)
        self.assertEqual(patron, "abandono")

    def test_classify_trajectory_pico(self):
        base = _make_snap("2024-01-01", sessions=5, cycling_efficiency=0.155, km_week=120)
        follow = [
            _make_snap((date(2024, 1, 8) + timedelta(weeks=i)).isoformat(),
                       sessions=5, cycling_efficiency=0.155 + i * 0.003, km_week=130)
            for i in range(8)
        ]
        patron, stats = _classify_trajectory([base] + follow, 0, 0.155)
        self.assertEqual(patron, "pico")

    def test_classify_trajectory_descenso(self):
        base = _make_snap("2024-01-01", sessions=5, cycling_efficiency=0.155, km_week=120)
        follow = [
            _make_snap((date(2024, 1, 8) + timedelta(weeks=i)).isoformat(),
                       sessions=3, cycling_efficiency=0.120 - i * 0.002, km_week=80)
            for i in range(8)
        ]
        patron, stats = _classify_trajectory([base] + follow, 0, 0.155)
        self.assertEqual(patron, "descenso")

    def test_result_has_required_keys(self):
        snaps = self._build_snapshots(40)
        result = historical_similarity(snaps)
        if result["ok"]:
            self.assertIn("current_state", result)
            self.assertIn("matches", result)
            self.assertIn("patron_distribution", result)
            self.assertIn("mensaje", result)
            if result["matches"]:
                m = result["matches"][0]
                for key in ("week", "similarity", "confidence", "patron", "patron_label", "trajectory"):
                    self.assertIn(key, m)


class AcademiaTests(unittest.TestCase):
    def test_all_six_capabilities_have_education(self):
        expected = {"motor_aerobico", "composicion_corporal", "recuperacion", "escalada", "fuerza", "nutricion_deportiva"}
        self.assertEqual(set(CAPABILITY_EDUCATION.keys()), expected)

    def test_each_capability_has_required_fields(self):
        required = {"qué_es", "qué_significa_score", "cómo_mejorar", "indicadores"}
        for key, edu in CAPABILITY_EDUCATION.items():
            self.assertTrue(required.issubset(edu.keys()), f"{key} missing fields")

    def test_indicator_keys_are_non_empty(self):
        for key, edu in CAPABILITY_EDUCATION.items():
            inds = edu.get("indicadores", {})
            self.assertGreater(len(inds), 0, f"{key} has no indicators")

    def test_glossary_has_required_terms(self):
        required = {"score_100", "madurez", "confianza", "z2", "lt_bpm", "eficiencia_vel_fc", "bloque_12_semanas", "readiness"}
        self.assertTrue(required.issubset(GLOSSARY.keys()))

    def test_each_glossary_term_has_definition_and_example(self):
        for key, term in GLOSSARY.items():
            self.assertIn("término", term, f"{key} missing 'término'")
            self.assertIn("definición", term, f"{key} missing 'definición'")
            self.assertIn("ejemplo", term, f"{key} missing 'ejemplo'")

    def test_academia_returns_capability_education(self):
        result = academia("motor_aerobico")
        self.assertTrue(result["ok"])
        self.assertIn("educacion", result)
        self.assertIn("qué_es", result["educacion"])

    def test_academia_returns_glossary_term(self):
        result = academia("z2")
        self.assertTrue(result["ok"])
        self.assertIn("termino", result)
        self.assertEqual(result["termino"]["término"], "Zone 2 (Z2)")

    def test_academia_returns_full_glosario(self):
        result = academia("glosario")
        self.assertTrue(result["ok"])
        self.assertIn("glosario", result)
        self.assertGreater(len(result["glosario"]), 5)

    def test_academia_unknown_key_returns_error(self):
        result = academia("clave_inventada")
        self.assertFalse(result["ok"])
        self.assertIn("disponibles", result)
        self.assertIn("motor_aerobico", result["disponibles"])

    def test_academia_all_capability_keys_resolve(self):
        for key in CAPABILITY_EDUCATION:
            result = academia(key)
            self.assertTrue(result["ok"], f"academia({key}) returned not ok")

    def test_academia_all_glossary_keys_resolve(self):
        for key in GLOSSARY:
            result = academia(key)
            self.assertTrue(result["ok"], f"academia({key}) returned not ok")


class SessionEnvironmentTests(unittest.TestCase):
    def test_altitude_bands_are_explicit(self):
        self.assertEqual(altitude_band(700), "low")
        self.assertEqual(altitude_band(2200), "high")
        self.assertEqual(altitude_band(2800), "very_high")
        self.assertEqual(relative_altitude_band(50), "habitual")
        self.assertEqual(relative_altitude_band(-900), "well_below_habitual")

    def test_altitude_average_is_time_weighted(self):
        result = telemetry_altitude([
            {"t": 0, "altitude": 2000},
            {"t": 10, "altitude": 2200},
            {"t": 11, "altitude": 2200},
        ])
        self.assertAlmostEqual(result["avg"], 2033.33, places=2)
        self.assertEqual(result["start"], 2000)
        self.assertEqual(result["max"], 2200)

    def test_country_inference_is_conservative(self):
        self.assertEqual(infer_country(19.55, -99.25)[0], "MX")
        self.assertEqual(infer_country(53.35, -6.26)[0], "IE")
        self.assertIsNone(infer_country(0, 0)[0])


if __name__ == "__main__":
    unittest.main()
