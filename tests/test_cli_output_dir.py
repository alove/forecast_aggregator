import unittest
from pathlib import Path
from unittest.mock import patch

from forecast_collector.cli import build_parser, command_collect


class OutputDirTests(unittest.TestCase):
    def test_parser_accepts_output_dir(self):
        args = build_parser().parse_args([
            "collect", "--output-dir", "~/election-model-average", "--save-raw"
        ])
        self.assertEqual(args.output_dir, Path("~/election-model-average"))
        self.assertTrue(args.save_raw)

    def test_explicit_file_arguments_are_still_available(self):
        args = build_parser().parse_args([
            "collect",
            "--output-dir", "/tmp/base",
            "--national-output", "/tmp/custom-national.csv",
            "--state-output", "/tmp/custom-state.csv",
            "--raw-dir", "/tmp/custom-raw",
            "--dry-run",
        ])
        self.assertEqual(args.output_dir, Path("/tmp/base"))
        self.assertEqual(args.national_output, Path("/tmp/custom-national.csv"))
        self.assertEqual(args.state_output, Path("/tmp/custom-state.csv"))
        self.assertEqual(args.raw_dir, Path("/tmp/custom-raw"))


if __name__ == "__main__":
    unittest.main()
