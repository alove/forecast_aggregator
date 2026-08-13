import unittest

from forecast_collector.schema import validate_rows
from forecast_collector.sources.election_statsheet import ElectionStatSheetSource


class ElectionStatSheetTests(unittest.TestCase):
    def test_normalize_small_fixture(self):
        source = ElectionStatSheetSource()
        date_rows = {
            "house_forecast_timeline.csv": [
                {"forecast_date": "2026-08-12", "party": "dem", "prob_majority": ".6", "seats_pct_05": "200", "seats_avg": "225", "seats_pct_95": "250", "vote_pct_05": ".49", "vote_avg": ".54", "vote_pct_95": ".59"},
                {"forecast_date": "2026-08-12", "party": "rep", "prob_majority": ".4", "seats_pct_05": "185", "seats_avg": "210", "seats_pct_95": "235", "vote_pct_05": ".41", "vote_avg": ".46", "vote_pct_95": ".51"},
            ],
            "house_district_forecast_timeline.csv": [
                {"forecast_date": "2026-08-12", "year": "2026", "state": "Alabama", "seat_number": "1", "r_prob": ".8", "r_pct_05": ".52", "r_avg": ".60", "r_pct_95": ".68"},
            ],
            "senate_forecast_timeline.csv": [
                {"forecast_date": "2026-08-12", "party": "dem", "prob_majority": ".3", "seats_pct_05": "45", "seats_avg": "49", "seats_pct_95": "53"},
                {"forecast_date": "2026-08-12", "party": "rep", "prob_majority": ".69", "seats_pct_05": "47", "seats_avg": "50.8", "seats_pct_95": "55"},
                {"forecast_date": "2026-08-12", "party": "ind", "prob_majority": "0", "seats_pct_05": "0", "seats_avg": ".2", "seats_pct_95": "1"},
            ],
            "senate_state_forecast_timeline.csv": [
                {"forecast_date": "2026-08-12", "state": "Alabama", "seat_name": "Class II", "r_prob": ".9", "r_pct_05": ".53", "r_avg": ".62", "r_pct_95": ".70"},
            ],
        }
        rows, run_id = source.normalize_snapshot(
            date_rows,
            forecast_date="2026-08-12",
            observed_datetime_utc="2026-08-12T12:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertTrue(run_id.startswith("ess-2026-08-12-"))
        self.assertEqual(len(rows), 3)
        district = rows[1]
        self.assertEqual(district["congressional_district"], "0101")
        self.assertEqual(district["house_d_pct"], 20.0)
        self.assertEqual(validate_rows(rows), 3)


class ElectionStatSheetDuplicateTests(unittest.TestCase):
    def test_identical_duplicate_rows_are_collapsed(self):
        source = ElectionStatSheetSource()
        row = {
            "forecast_date": "2026-07-24",
            "year": "2026",
            "state": "Arizona",
            "seat_number": "1",
            "r_prob": ".4",
            "r_pct_05": ".3",
            "r_avg": ".48",
            "r_pct_95": ".6",
        }
        cleaned, count = source._dedupe_exact_rows(
            [row, dict(row)],
            key_fields=("state", "seat_number"),
            filename="house_district_forecast_timeline.csv",
            forecast_date="2026-07-24",
        )
        self.assertEqual(cleaned, [row])
        self.assertEqual(count, 1)

    def test_conflicting_duplicate_rows_are_rejected(self):
        from forecast_collector.errors import SourceFormatError

        source = ElectionStatSheetSource()
        first = {"state": "Arizona", "seat_number": "1", "r_prob": ".4"}
        second = {"state": "Arizona", "seat_number": "1", "r_prob": ".5"}
        with self.assertRaises(SourceFormatError):
            source._dedupe_exact_rows(
                [first, second],
                key_fields=("state", "seat_number"),
                filename="house_district_forecast_timeline.csv",
                forecast_date="2026-07-24",
            )
