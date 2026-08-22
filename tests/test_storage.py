from pathlib import Path
import tempfile
import unittest

from forecast_collector.export import NATIONAL_FIELDNAMES, split_rows
from forecast_collector.schema import blank_row
from forecast_collector.storage import append_export_rows, read_export_rows


class StorageTests(unittest.TestCase):
    def test_idempotent_long_form_append(self):
        source = blank_row()
        source.update({
            "observed_datetime_utc": "2026-08-12T12:00:00+00:00",
            "vendor": "Test", "vendor_run_id": "run-1", "row_type": "national",
            "election_date": "2026-11-03",
            "source_record_id": "national", "source_url": "https://example.com",
            "house_seats_d": 220, "house_seats_r": 215, "house_seats_other": 0,
        })
        rows, _ = split_rows([source])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "national.csv"
            self.assertEqual(append_export_rows(path, rows, fieldnames=NATIONAL_FIELDNAMES), (3, 0))
            self.assertEqual(append_export_rows(path, rows, fieldnames=NATIONAL_FIELDNAMES), (0, 3))
            self.assertEqual(len(read_export_rows(path, fieldnames=NATIONAL_FIELDNAMES)), 3)
