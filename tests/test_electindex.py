import unittest

from forecast_collector.errors import SourceFormatError
from forecast_collector.schema import validate_rows
from forecast_collector.sources.electindex import ElectIndexSource


class ElectIndexTests(unittest.TestCase):
    def test_normalize_small_fixture_and_independent_mapping(self):
        parsed = {
            "chambers.csv": [
                {"chamber": "House", "avg_dem_seats": "237.8", "avg_gop_seats": "197.2", "projected_dem_seats": "231", "dem_control_pct": "71.7"},
                {"chamber": "Senate", "avg_dem_seats": "50.2", "avg_gop_seats": "49.8", "projected_dem_seats": "50", "dem_control_pct": "47.4"},
            ],
            "national_indicators.csv": [{
                "date": "2026-08-12", "house_pv_dem": "58158815", "house_pv_rep": "50052121",
                "house_pv_oth": "203384", "house_pv_margin": "7.49",
            }],
            "races_summary.csv": [
                {
                    "race_code": "NY-01", "race_type": "house", "state": "NY", "district": "01",
                    "dem_name": "Democrat", "rep_name": "Republican", "ind_name": "",
                    "dem_prob": "60", "rep_prob": "40", "ind_prob": "0", "rating": "Lean D",
                    "dem_pct": "53", "rep_pct": "46", "oth_pct": "1", "ind_pct": "0",
                    "dem_votes": "530", "rep_votes": "460", "oth_votes": "10", "ind_votes": "0",
                    "total_votes": "1000", "poll_count": "2",
                },
                {
                    "race_code": "NE-SEN", "race_type": "senate", "state": "NE", "district": "SEN",
                    "dem_name": "(No Democrat)", "rep_name": "Republican", "ind_name": "Independent",
                    "dem_prob": "31.1", "rep_prob": "68.9", "ind_prob": "31.1", "rating": "Lean R",
                    "dem_pct": "0", "rep_pct": "52.5", "oth_pct": "0", "ind_pct": "47.5",
                    "dem_votes": "0", "rep_votes": "525", "oth_votes": "0", "ind_votes": "475",
                    "total_votes": "1000", "poll_count": "1",
                },
            ],
        }
        rows, run_id, model_date = ElectIndexSource().normalize(
            parsed,
            observed_datetime_utc="2026-08-12T20:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertTrue(run_id.startswith("electindex-2026-08-12-"))
        self.assertEqual(model_date, "2026-08-12")
        self.assertEqual(rows[1]["congressional_district"], "3601")
        self.assertEqual(rows[2]["senate_d_pct"], 0.0)
        self.assertEqual(rows[2]["senate_other_pct"], 31.1)
        self.assertEqual(validate_rows(rows), 3)


    @staticmethod
    def _national_only_fixture(raw_date: str):
        return {
            "chambers.csv": [
                {
                    "chamber": "House", "avg_dem_seats": "220", "avg_gop_seats": "215",
                    "projected_dem_seats": "220", "dem_control_pct": "55", "races": "435",
                },
                {
                    "chamber": "Senate", "avg_dem_seats": "50", "avg_gop_seats": "50",
                    "projected_dem_seats": "50", "dem_control_pct": "50", "races": "35",
                },
            ],
            "national_indicators.csv": [{
                "date": raw_date, "house_pv_dem": "53", "house_pv_rep": "47",
                "house_pv_oth": "0", "house_pv_margin": "6",
            }],
            "races_summary.csv": [],
        }

    def test_short_us_source_date_is_normalized_before_export(self):
        rows, run_id, model_date = ElectIndexSource().normalize(
            self._national_only_fixture("8/12/26"),
            observed_datetime_utc="2026-08-21T23:42:32+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertEqual(model_date, "2026-08-12")
        self.assertEqual(rows[0]["vendor_forecast_date"], "2026-08-12")
        self.assertTrue(run_id.startswith("electindex-2026-08-12-"))
        self.assertEqual(validate_rows(rows), 1)

    def test_single_untrusted_optional_source_date_becomes_null(self):
        rows, run_id, model_date = ElectIndexSource().normalize(
            self._national_only_fixture("Forecast as of sometime in August"),
            observed_datetime_utc="2026-08-21T23:42:32+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertEqual(model_date, "")
        self.assertEqual(rows[0]["vendor_forecast_date"], "")
        self.assertTrue(run_id.startswith("electindex-undated-"))
        self.assertEqual(validate_rows(rows), 1)



class ElectIndexNoMajorPartyValidationTests(unittest.TestCase):
    def test_no_democrat_conflicting_independent_probability_is_rejected(self):
        row = {
            "race_code": "XX-SEN",
            "dem_name": "(No Democrat)",
            "rep_name": "Republican",
            "dem_prob": "30",
            "rep_prob": "70",
            "ind_prob": "20",
        }
        with self.assertRaises(SourceFormatError):
            ElectIndexSource()._win_probabilities(row)


class ElectIndexRunIdTests(unittest.TestCase):
    def test_governor_and_old_national_rows_do_not_change_congressional_run_id(self):
        from copy import deepcopy

        parsed = {
            "chambers.csv": [
                {
                    "chamber": "House", "avg_dem_seats": "220", "avg_gop_seats": "215",
                    "projected_dem_seats": "220", "dem_control_pct": "55", "races": "435",
                },
                {
                    "chamber": "Senate", "avg_dem_seats": "50", "avg_gop_seats": "50",
                    "projected_dem_seats": "50", "dem_control_pct": "50", "races": "35",
                },
            ],
            "national_indicators.csv": [{
                "date": "2026-08-12", "house_pv_dem": "53", "house_pv_rep": "47",
                "house_pv_oth": "0", "house_pv_margin": "6",
            }],
            "races_summary.csv": [{
                "race_code": "NY-01", "race_type": "house", "state": "NY", "district": "1",
                "dem_name": "D", "rep_name": "R", "ind_name": "",
                "dem_prob": "55", "rep_prob": "45", "ind_prob": "0", "rating": "Lean D",
                "dem_pct": "53", "rep_pct": "47", "oth_pct": "0", "ind_pct": "0",
                "dem_votes": "53", "rep_votes": "47", "oth_votes": "0", "ind_votes": "0",
                "total_votes": "100",
            }],
        }
        source = ElectIndexSource()
        _, first_run, _ = source.normalize(
            parsed,
            observed_datetime_utc="2026-08-12T20:00:00+00:00",
            include_house_districts=True,
            include_senate_races=False,
            require_complete_counts=False,
        )
        changed = deepcopy(parsed)
        changed["national_indicators.csv"].insert(0, {
            "date": "2026-08-11", "house_pv_dem": "52", "house_pv_rep": "48",
            "house_pv_oth": "0", "house_pv_margin": "4",
        })
        changed["races_summary.csv"].append({
            "race_code": "NY-GOV", "race_type": "governor", "state": "NY", "district": "GOV",
        })
        _, second_run, _ = source.normalize(
            changed,
            observed_datetime_utc="2026-08-12T20:00:00+00:00",
            include_house_districts=True,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertEqual(first_run, second_run)
