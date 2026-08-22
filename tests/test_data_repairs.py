from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from forecast_collector.data_repairs import (
    null_untrusted_rtwh_forecast_dates,
    repair_rtwh_canonical_data,
    validate_canonical_csv_text_format,
)


class DataRepairTests(unittest.TestCase):
    def test_only_untrusted_rtwh_forecast_dates_are_nulled_and_repair_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "forecast.csv"
            fieldnames = ["vendor", "vendor_forecast_date", "value", "notes"]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerows([
                    {
                        "vendor": "Race to the WH",
                        "vendor_forecast_date": "2026-04-22",
                        "value": "61",
                        "notes": "narrative date was not a model timestamp",
                    },
                    {
                        "vendor": "Race to the WH",
                        "vendor_forecast_date": "",
                        "value": "62",
                        "notes": "already null",
                    },
                    {
                        "vendor": "Kalshi",
                        "vendor_forecast_date": "2026-08-21",
                        "value": "63",
                        "notes": "other vendor is untouched",
                    },
                ])

            bytes_before = path.read_bytes()
            first = null_untrusted_rtwh_forecast_dates(path)
            self.assertEqual(first.row_count, 3)
            self.assertEqual(first.date_nulled_count, 1)
            self.assertEqual(first.removed_row_count, 0)
            self.assertEqual(first.changed_count, 1)
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["vendor_forecast_date"], "")
            self.assertEqual(rows[0]["value"], "61")
            self.assertEqual(
                rows[0]["notes"], "narrative date was not a model timestamp"
            )
            self.assertEqual(rows[1]["vendor_forecast_date"], "")
            self.assertEqual(rows[2]["vendor_forecast_date"], "2026-08-21")

            bytes_after_first = path.read_bytes()
            expected = bytes_before.replace(
                b'"Race to the WH","2026-04-22","61"',
                b'"Race to the WH","","61"',
                1,
            )
            self.assertEqual(bytes_after_first, expected)
            second = null_untrusted_rtwh_forecast_dates(path)
            self.assertEqual(second.changed_count, 0)
            self.assertEqual(path.read_bytes(), bytes_after_first)

    def test_unverified_rtwh_senate_national_rows_are_removed_narrowly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "national.csv"
            fieldnames = [
                "vendor",
                "vendor_run_id",
                "vendor_forecast_date",
                "metric_type",
                "geography_type",
                "party",
                "value",
                "notes",
            ]
            rows = [
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "bad-run",
                    "vendor_forecast_date": "2026-04-22",
                    "metric_type": "US Senate Seats by Party",
                    "geography_type": "national",
                    "party": "D",
                    "value": "0.6",
                    "notes": "old permissive parser",
                },
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "bad-run",
                    "vendor_forecast_date": "2026-04-22",
                    "metric_type": "US Senate Party Probability",
                    "geography_type": "national",
                    "party": "Other",
                    "value": "66.363",
                    "notes": "old permissive parser",
                },
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "house-run",
                    "vendor_forecast_date": "2026-04-22",
                    "metric_type": "US House Party Probability",
                    "geography_type": "national",
                    "party": "D",
                    "value": "76.3",
                    "notes": "valid House metric",
                },
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "verified-seats",
                    "vendor_forecast_date": "",
                    "metric_type": "US Senate Seats by Party",
                    "geography_type": "national",
                    "party": "D",
                    "value": "49",
                    "notes": "rtwh_senate_seats=verified",
                },
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "verified-control",
                    "vendor_forecast_date": "",
                    "metric_type": "US Senate Party Probability",
                    "geography_type": "national",
                    "party": "D",
                    "value": "40",
                    "notes": "rtwh_senate_control=verified",
                },
                {
                    "vendor": "Race to the WH",
                    "vendor_run_id": "state-control-label",
                    "vendor_forecast_date": "2026-04-22",
                    "metric_type": "US Senate Party Probability",
                    "geography_type": "state",
                    "party": "D",
                    "value": "52",
                    "notes": "non-national row must never be removed",
                },
                {
                    "vendor": "Kalshi",
                    "vendor_run_id": "kalshi-run",
                    "vendor_forecast_date": "",
                    "metric_type": "US Senate Seats by Party",
                    "geography_type": "national",
                    "party": "D",
                    "value": "49.5",
                    "notes": "other vendor",
                },
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerows(rows)

            first = repair_rtwh_canonical_data(path)
            self.assertEqual(first.row_count, 7)
            self.assertEqual(first.removed_row_count, 2)
            self.assertEqual(first.date_nulled_count, 2)
            self.assertEqual(first.changed_count, 4)

            with path.open("r", encoding="utf-8", newline="") as handle:
                repaired = list(csv.DictReader(handle))
            self.assertEqual(len(repaired), 5)
            self.assertFalse(any(row["vendor_run_id"] == "bad-run" for row in repaired))
            house = next(row for row in repaired if row["vendor_run_id"] == "house-run")
            self.assertEqual(house["vendor_forecast_date"], "")
            self.assertEqual(house["value"], "76.3")
            self.assertTrue(any(row["vendor_run_id"] == "verified-seats" for row in repaired))
            self.assertTrue(any(row["vendor_run_id"] == "verified-control" for row in repaired))
            state_row = next(
                row for row in repaired if row["vendor_run_id"] == "state-control-label"
            )
            self.assertEqual(state_row["geography_type"], "state")
            self.assertEqual(state_row["vendor_forecast_date"], "")
            self.assertTrue(any(row["vendor_run_id"] == "kalshi-run" for row in repaired))

            bytes_after_first = path.read_bytes()
            second = repair_rtwh_canonical_data(path)
            self.assertEqual(second.changed_count, 0)
            self.assertEqual(path.read_bytes(), bytes_after_first)

    def test_wrong_verification_marker_does_not_preserve_the_other_metric(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "national.csv"
            fieldnames = [
                "vendor",
                "vendor_forecast_date",
                "metric_type",
                "notes",
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerow({
                    "vendor": "Race to the WH",
                    "vendor_forecast_date": "",
                    "metric_type": "US Senate Party Probability",
                    "notes": "rtwh_senate_seats=verified",
                })
            summary = repair_rtwh_canonical_data(path)
            self.assertEqual(summary.removed_row_count, 1)
            with path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])


    def test_legacy_short_dates_are_canonicalized_and_unknown_optional_date_is_nulled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dates.csv"
            fieldnames = [
                "vendor",
                "vendor_forecast_date",
                "election_date",
                "value",
                "notes",
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                    quoting=csv.QUOTE_ALL,
                )
                writer.writeheader()
                writer.writerows([
                    {
                        "vendor": "ElectIndex",
                        "vendor_forecast_date": "8/12/26",
                        "election_date": "11/3/26",
                        "value": "50",
                        "notes": "unambiguous U.S. dates",
                    },
                    {
                        "vendor": "Another Vendor",
                        "vendor_forecast_date": "Spring 2026",
                        "election_date": "2026-11-03",
                        "value": "51",
                        "notes": "prose date must not be guessed",
                    },
                ])

            summary = repair_rtwh_canonical_data(path)
            self.assertEqual(summary.date_normalized_count, 1)
            self.assertEqual(summary.date_nulled_count, 1)
            self.assertEqual(summary.election_date_normalized_count, 1)
            self.assertEqual(summary.removed_row_count, 0)
            self.assertEqual(summary.changed_count, 3)

            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["vendor_forecast_date"], "2026-08-12")
            self.assertEqual(rows[0]["election_date"], "2026-11-03")
            self.assertEqual(rows[1]["vendor_forecast_date"], "")
            self.assertEqual(rows[1]["election_date"], "2026-11-03")

            bytes_after = path.read_bytes()
            second = repair_rtwh_canonical_data(path)
            self.assertEqual(second.changed_count, 0)
            self.assertEqual(path.read_bytes(), bytes_after)

    def test_crlf_repair_preserves_crlf_and_passes_text_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "crlf.csv"
            path.write_bytes(
                b'"vendor","vendor_forecast_date","election_date"\r\n'
                b'"ElectIndex","8/12/26","11/3/26"\r\n'
            )

            summary = repair_rtwh_canonical_data(path)
            self.assertEqual(summary.date_normalized_count, 1)
            self.assertEqual(summary.election_date_normalized_count, 1)
            raw = path.read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            validate_canonical_csv_text_format(path)

    def test_csv_text_validation_rejects_trailing_space_before_crlf(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad-space.csv"
            path.write_bytes(b'"vendor" \r\n"ElectIndex"\r\n')
            with self.assertRaisesRegex(ValueError, "trailing spaces or tabs"):
                validate_canonical_csv_text_format(path)

    def test_csv_text_validation_rejects_mixed_line_endings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mixed.csv"
            path.write_bytes(b'"vendor"\r\n"ElectIndex"\n')
            with self.assertRaisesRegex(ValueError, "mixed or malformed"):
                validate_canonical_csv_text_format(path)

    def test_invalid_required_election_date_fails_without_rewriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad-election-date.csv"
            path.write_text(
                '"vendor","vendor_forecast_date","election_date"\n'
                '"ElectIndex","8/12/26","election someday"\n',
                encoding="utf-8",
            )
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "invalid required election_date"):
                repair_rtwh_canonical_data(path)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_required_columns_fail_loudly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.csv"
            path.write_text("vendor,value\nRace to the WH,50\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "vendor_forecast_date"):
                repair_rtwh_canonical_data(path)

    def test_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.csv"
            result = repair_rtwh_canonical_data(path)
            self.assertEqual(result.row_count, 0)
            self.assertEqual(result.changed_count, 0)


if __name__ == "__main__":
    unittest.main()
