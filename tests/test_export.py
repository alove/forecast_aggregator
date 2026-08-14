import unittest

from forecast_collector.export import (
    split_rows,
    validate_national_rows,
    validate_state_rows,
)
from forecast_collector.schema import blank_row


class ExportTests(unittest.TestCase):
    def _national(self):
        row = blank_row()
        row.update({
            "observed_datetime_utc": "2026-08-12T21:11:07+00:00",
            "vendor": "ElectIndex",
            "vendor_model": "Test Model",
            "vendor_run_id": "run-1",
            "vendor_forecast_date": "2026-08-12",
            "election_date": "2026-11-03",
            "row_type": "national",
            "source_record_id": "national",
            "source_url": "https://example.com",
            "house_seats_basis": "mean seats",
            "house_seats_d": 225,
            "house_seats_r": 210,
            "house_seats_other": 0,
            "house_control_d_pct": 70,
            "house_control_r_pct": 30,
            "house_control_other_pct": 0,
            "house_popular_vote_basis": "two-party vote",
            "house_popular_vote_d_pct": 53,
            "house_popular_vote_r_pct": 47,
            "house_popular_vote_other_pct": 0,
            "house_popular_vote_margin_d_minus_r_pct": 6,
            "senate_seats_basis": "mean seats",
            "senate_seats_d": 51,
            "senate_seats_r": 49,
            "senate_seats_other": 0,
            "senate_control_d_pct": 55,
            "senate_control_r_pct": 45,
            "senate_control_other_pct": 0,
        })
        return row

    def test_national_is_long_form_and_has_pull_time_to_second(self):
        national, state = split_rows([self._national()])
        self.assertEqual(state, [])
        self.assertEqual(validate_national_rows(national), len(national))
        keys = {(r["metric_type"], r["party"]) for r in national}
        self.assertIn(("US House Seats by Party", "D"), keys)
        self.assertIn(("US House Party Probability", "R"), keys)
        self.assertIn(("US Senate Seats by Party", "D"), keys)
        self.assertIn(("US Senate Party Probability", "R"), keys)
        self.assertIn(("US House Popular Vote Projection", "D"), keys)
        self.assertIn(("US House Popular Vote Margin", "D-R"), keys)
        self.assertTrue(all(r["rhubarb_pull_time"] == "2026-08-12T21:11:07+00:00" for r in national))
        self.assertTrue(all(r["model_web_url"] == "https://electindex.com/forecasts/" for r in national))

    def test_house_and_senate_races_go_to_state_file(self):
        national = self._national()
        house = blank_row()
        house.update(national)
        house.update({
            "row_type": "house_district", "source_record_id": "AL-01",
            "congressional_district": "0101", "state_fips": "01",
            "state_abbreviation": "AL", "state": "Alabama", "house_seat_number": 1,
            "house_seat": "Alabama 1st Congressional District",
            "house_d_pct": 40, "house_r_pct": 60, "house_other_pct": 0,
            "house_d_vote_pct": 45, "house_r_vote_pct": 55, "house_other_vote_pct": 0,
        })
        senate = blank_row()
        senate.update(national)
        senate.update({
            "row_type": "senate_race", "source_record_id": "GA:regular",
            "state_fips": "13", "state_abbreviation": "GA", "state": "Georgia",
            "senate_seat": "Georgia U.S. Senate", "senate_d_pct": 52,
            "senate_r_pct": 48, "senate_other_pct": 0,
        })
        n_rows, s_rows = split_rows([national, house, senate])
        self.assertEqual(validate_national_rows(n_rows), len(n_rows))
        self.assertEqual(validate_state_rows(s_rows), len(s_rows))
        self.assertTrue(any(r["congressional_district"] == "0101" for r in s_rows))
        self.assertTrue(any(r["metric_type"] == "US Senate Race Party Probability" for r in s_rows))
