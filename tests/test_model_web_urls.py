from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from forecast_collector.model_links import model_web_url_for

ROOT = Path(__file__).resolve().parents[1]
MIGRATOR = ROOT / "migrate_model_web_urls.py"


class ModelWebUrlTests(unittest.TestCase):
    def test_vendor_metric_links_point_to_published_models(self):
        self.assertEqual(
            model_web_url_for("ElectIndex", "US House Seats by Party"),
            "https://electindex.com/forecasts/",
        )
        self.assertEqual(
            model_web_url_for("Grant Williams", "US Senate Race Party Probability"),
            "https://grantbw4.github.io/2026-midterms-forecast/",
        )
        self.assertEqual(
            model_web_url_for("Election StatSheet", "US House District Party Probability"),
            "https://www.electionstatsheet.com/districts",
        )
        self.assertEqual(
            model_web_url_for("Race to the WH", "US Senate Seats by Party"),
            "https://www.racetothewh.com/senate/26",
        )
        self.assertEqual(
            model_web_url_for("Race to the WH", "US House Popular Vote Projection"),
            "https://www.racetothewh.com/house",
        )

    def test_migration_adds_column_to_old_history_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            national = output / "election_forecasts_2026_national.csv"
            state = output / "election_forecasts_2026_state.csv"
            old_common = [
                "schema_version", "rhubarb_pull_time", "observed_datetime_utc", "vendor",
                "vendor_model", "vendor_run_id", "vendor_forecast_date", "vendor_updated_at_utc",
                "model_status", "election_date", "metric_type", "party", "value", "unit",
                "median_value", "low_value", "high_value", "basis", "source_record_id",
                "source_url", "source_file", "data_quality", "notes",
            ]
            national_fields = old_common + ["geography_type", "geography_id", "geography_name"]
            state_fields = old_common + [
                "geography_type", "geography_id", "state_fips", "state_abbreviation", "state",
                "congressional_district", "house_seat_number", "house_seat", "senate_seat",
                "special_election", "rating",
            ]
            nrow = {field: "" for field in national_fields}
            nrow.update({
                "schema_version": "2.0.0", "rhubarb_pull_time": "2026-08-14T14:03:27+00:00",
                "observed_datetime_utc": "2026-08-14T14:03:27+00:00", "vendor": "ElectIndex",
                "vendor_run_id": "run-1", "election_date": "2026-11-03",
                "metric_type": "US House Seats by Party", "party": "D", "value": "230",
                "unit": "seats", "source_record_id": "national", "source_url": "https://raw.example/x",
                "geography_type": "national", "geography_id": "US", "geography_name": "United States",
            })
            srow = {field: "" for field in state_fields}
            srow.update({
                "schema_version": "2.0.0", "rhubarb_pull_time": "2026-08-14T14:03:27+00:00",
                "observed_datetime_utc": "2026-08-14T14:03:27+00:00", "vendor": "Election StatSheet",
                "vendor_run_id": "run-1", "election_date": "2026-11-03",
                "metric_type": "US House District Party Probability", "party": "D", "value": "55",
                "unit": "percent", "source_record_id": "NY-01", "source_url": "https://raw.example/y",
                "geography_type": "congressional_district", "geography_id": "3601", "state_fips": "36",
                "state_abbreviation": "NY", "state": "New York", "congressional_district": "3601",
                "house_seat_number": "1", "house_seat": "New York 1st Congressional District",
            })
            for path, fields, row in ((national, national_fields, nrow), (state, state_fields, srow)):
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL, lineterminator="\n")
                    writer.writeheader(); writer.writerow(row)

            first = subprocess.run(
                [sys.executable, str(MIGRATOR), "--output-dir", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("MIGRATED", first.stdout)
            with national.open(encoding="utf-8", newline="") as handle:
                migrated = next(csv.DictReader(handle))
            self.assertEqual(migrated["schema_version"], "2.1.0")
            self.assertEqual(migrated["model_web_url"], "https://electindex.com/forecasts/")
            before = (national.read_bytes(), state.read_bytes())
            second = subprocess.run(
                [sys.executable, str(MIGRATOR), "--output-dir", str(output)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(before, (national.read_bytes(), state.read_bytes()))
            self.assertIn("already current", second.stdout)


if __name__ == "__main__":
    unittest.main()
