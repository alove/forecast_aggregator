from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_sources_and_schema_commands(self):
        sources = subprocess.run(
            [sys.executable, "-m", "forecast_collector", "sources"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(sources.returncode, 0, sources.stderr)
        self.assertIn("election-statsheet", sources.stdout)
        self.assertIn("electindex", sources.stdout)
        self.assertIn("grant-williams", sources.stdout)
        self.assertIn("Race to the WH", sources.stdout)
        self.assertIn("kalshi", sources.stdout)

        schema = subprocess.run(
            [sys.executable, "-m", "forecast_collector", "schema"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(schema.returncode, 0, schema.stderr)
        self.assertIn("rhubarb_pull_time", schema.stdout)
        self.assertIn("metric_type", schema.stdout)
        self.assertIn("NATIONAL CSV", schema.stdout)
        self.assertIn("STATE / DISTRICT CSV", schema.stdout)
