import unittest

from forecast_collector.errors import OutputValidationError
from forecast_collector.schema import blank_row, validate_rows


class SchemaTests(unittest.TestCase):
    def test_negative_house_margin_is_valid(self):
        row = blank_row()
        row.update({
            "observed_datetime_utc": "2026-08-12T12:00:00+00:00",
            "vendor": "Test",
            "vendor_run_id": "run-1",
            "row_type": "national",
            "source_record_id": "national",
            "source_url": "https://example.com",
            "house_seats_d": 200,
            "house_seats_r": 235,
            "house_seats_other": 0,
            "senate_seats_d": 48,
            "senate_seats_r": 52,
            "senate_seats_other": 0,
            "house_control_d_pct": 20,
            "house_control_r_pct": 80,
            "house_control_other_pct": 0,
            "senate_control_d_pct": 10,
            "senate_control_r_pct": 90,
            "senate_control_other_pct": 0,
            "house_popular_vote_d_pct": 48,
            "house_popular_vote_r_pct": 52,
            "house_popular_vote_other_pct": 0,
            "house_popular_vote_margin_d_minus_r_pct": -4,
        })
        self.assertEqual(validate_rows([row]), 1)

    def test_same_record_id_is_valid_in_different_runs(self):
        rows = []
        for run in ("run-1", "run-2"):
            row = blank_row()
            row.update({
                "observed_datetime_utc": "2026-08-12T12:00:00+00:00",
                "vendor": "Test", "vendor_run_id": run,
                "row_type": "national", "source_record_id": "national",
                "source_url": "https://example.com",
            })
            rows.append(row)
        self.assertEqual(validate_rows(rows), 2)


class SnapshotConsistencyTests(unittest.TestCase):
    @staticmethod
    def _base(row_type: str, source_record_id: str):
        row = blank_row()
        row.update({
            "observed_datetime_utc": "2026-08-12T12:00:00+00:00",
            "vendor": "Test",
            "vendor_run_id": "run-1",
            "row_type": row_type,
            "source_record_id": source_record_id,
            "source_url": "https://example.com",
            "house_seats_d": 220,
            "house_seats_r": 215,
            "house_seats_other": 0,
        })
        return row

    def test_mixed_snapshot_toplines_are_rejected(self):
        national = self._base("national", "national")
        district = self._base("house_district", "AL-01")
        district.update({
            "congressional_district": "0101",
            "state_fips": "01",
            "state_abbreviation": "AL",
            "state": "Alabama",
            "house_seat_number": 1,
            "house_seat": "Alabama 1st Congressional District",
            "house_seats_d": 221,
        })
        with self.assertRaises(OutputValidationError):
            validate_rows([national, district])

    def test_race_without_national_row_is_rejected(self):
        district = self._base("house_district", "AL-01")
        district.update({
            "congressional_district": "0101",
            "state_fips": "01",
            "state_abbreviation": "AL",
            "state": "Alabama",
            "house_seat_number": 1,
            "house_seat": "Alabama 1st Congressional District",
        })
        with self.assertRaises(OutputValidationError):
            validate_rows([district])
