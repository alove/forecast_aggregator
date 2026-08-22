from __future__ import annotations

import unittest

from forecast_collector.date_utils import (
    canonical_date_or_blank,
    is_iso_date,
    require_canonical_date,
)


class DateUtilsTests(unittest.TestCase):
    def test_iso_and_unambiguous_us_dates_are_canonicalized(self):
        cases = {
            "2026-08-12": "2026-08-12",
            "8/12/26": "2026-08-12",
            "08/12/2026": "2026-08-12",
            "11/3/26": "2026-11-03",
            " 4/2/26 ": "2026-04-02",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_date_or_blank(raw), expected)

    def test_unknown_ambiguous_or_invalid_optional_dates_become_blank(self):
        for raw in (
            "",
            None,
            "April 22, 2026",
            "22/4/26",
            "2026-04",
            "2026-02-30",
            "13/12/26",
            "2026-08-12T00:00:00Z",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_date_or_blank(raw), "")

    def test_required_date_rejects_untrusted_input(self):
        self.assertEqual(
            require_canonical_date("8/12/26", field="forecast_date"),
            "2026-08-12",
        )
        with self.assertRaisesRegex(ValueError, "forecast_date"):
            require_canonical_date("sometime", field="forecast_date")

    def test_iso_predicate_requires_exact_iso_form(self):
        self.assertTrue(is_iso_date("2026-08-12"))
        self.assertFalse(is_iso_date("8/12/26"))
        self.assertFalse(is_iso_date("2026-8-12"))
        self.assertFalse(is_iso_date(""))


if __name__ == "__main__":
    unittest.main()
