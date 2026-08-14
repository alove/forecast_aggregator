import unittest

from forecast_collector.schema import validate_rows
from forecast_collector.sources.grant_williams import GrantWilliamsSource


class GrantWilliamsTests(unittest.TestCase):
    def test_normalize_small_atomic_bundle(self):
        metadata = {
            "updated_at": "2026-08-12T15:43:13+00:00",
            "model_version": "4.0.0", "model_status": "healthy",
            "run_id": "run-123", "election_date": "2026-11-03", "warnings": [],
        }
        house = {
            "metadata": metadata,
            "summary": {
                "mean_dem_seats": 240.5, "median_dem_seats": 241,
                "median_rep_seats": 194, "ci_90_low": 215, "ci_90_high": 270,
                "prob_dem_majority": .75, "prob_rep_majority": .25,
                "national_environment": 6.0,
            },
            "national_model": {"election_day": {"ci_90": [2.0, 10.0]}},
            "districts": [{
                "id": "NY-01", "state": "NY", "district_number": 1,
                "prob_dem": .55, "mean_vote_share": 51.2,
                "ci_90_low": 45.0, "ci_90_high": 57.4,
                "category": "lean_d", "data_quality": "fundamentals_only",
                "polls_used": 0, "open_seat": False,
            }],
        }
        senate = {
            "metadata": dict(metadata),
            "summary": {
                "mean_dem_seats": 51.2, "median_dem_seats": 51,
                "ci_90_low": 47, "ci_90_high": 56,
                "prob_dem_control": .58, "prob_rep_control": .42, "seats_up": 1,
            },
            "races": [{
                "id": "IA", "state": "IA", "prob_dem": .51,
                "posterior_margin": .4, "credible_interval_90": [-8, 9],
                "category": "toss_up", "special": False,
                "data_quality": "silver_average", "polls_used": 1, "open_seat": True,
            }],
        }
        rows, run_id = GrantWilliamsSource().normalize(
            house, senate,
            observed_datetime_utc="2026-08-12T16:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertEqual(run_id, "run-123")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["congressional_district"], "3601")
        self.assertEqual(rows[0]["house_popular_vote_d_pct"], 53.0)
        self.assertEqual(validate_rows(rows), 3)
    def test_current_v5_national_margin_shape_without_summary_national_environment(self):
        metadata = {
            "updated_at": "2026-08-13T03:07:28+00:00",
            "model_version": "5.0.0", "model_status": "healthy",
            "run_id": "forecast-current", "election_date": "2026-11-03", "warnings": [],
        }
        house = {
            "metadata": metadata,
            "summary": {
                "mean_dem_seats": 250.35, "median_dem_seats": 245,
                "median_rep_seats": 190, "ci_90_low": 206, "ci_90_high": 313,
                "prob_dem_majority": .8614, "prob_rep_majority": .1386,
                "election_day_national_margin": 5.67,
            },
            "national_model": {"election_day": {"mean": 5.674, "ci_90": [2.081, 9.244]}},
            "districts": [{
                "id": "NY-01", "state": "NY", "district_number": 1,
                "prob_dem": .55, "mean_vote_share": 51.2,
                "ci_90_low": 45.0, "ci_90_high": 57.4,
                "category": "lean_d", "data_quality": "healthy",
                "polls_used": 0, "open_seat": False,
            }],
        }
        senate = {
            "metadata": dict(metadata),
            "summary": {
                "mean_dem_seats": 51.32, "median_dem_seats": 51,
                "ci_90_low": 47, "ci_90_high": 56,
                "prob_dem_control": .5989, "prob_rep_control": .4011, "seats_up": 1,
            },
            "races": [{
                "id": "IA", "state": "IA", "prob_dem": .51,
                "posterior_margin": .4, "credible_interval_90": [-8, 9],
                "category": "toss_up", "special": False,
                "data_quality": "healthy", "polls_used": 1, "open_seat": True,
            }],
        }
        rows, _ = GrantWilliamsSource().normalize(
            house, senate,
            observed_datetime_utc="2026-08-13T04:00:00+00:00",
            include_house_districts=True, include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertEqual(rows[0]["house_popular_vote_margin_d_minus_r_pct"], 5.674)
        self.assertEqual(rows[0]["house_popular_vote_d_p05"], 51.0405)
        self.assertEqual(validate_rows(rows), 3)

