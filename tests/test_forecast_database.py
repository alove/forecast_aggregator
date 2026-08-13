from __future__ import annotations

import csv
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_ROOT = ROOT / "forecast_database_ecs"
IMAGE_ROOT = DB_ROOT / "image"
PREPARER_PATH = IMAGE_ROOT / "prepare_election_forecasts.py"
NATIONAL_SAMPLE = ROOT / "sample_output" / "example_national_2026-08-12.csv"
STATE_SAMPLE = ROOT / "sample_output" / "example_state_2026-08-12.csv"


def load_preparer():
    spec = importlib.util.spec_from_file_location("prepare_election_forecasts", PREPARER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load forecast database preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ForecastDatabasePreparerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preparer = load_preparer()

    def test_sample_exports_prepare_complete_postgres_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(PREPARER_PATH),
                    "--national-input",
                    str(NATIONAL_SAMPLE),
                    "--state-input",
                    str(STATE_SAMPLE),
                    "--output-dir",
                    str(output),
                    "--copy-root",
                    "/docker-entrypoint-initdb.d",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("National: 48 rows", result.stdout)
            self.assertIn("State:    8,460 rows", result.stdout)

            expected = {
                "10-schema.sql",
                "20-load.sql",
                "30-post-load.sql",
                "manifest.json",
                "election_forecasts_2026_national.csv",
                "election_forecasts_2026_state.csv",
            }
            self.assertTrue(expected.issubset({path.name for path in output.iterdir()}))

            schema_sql = (output / "10-schema.sql").read_text(encoding="utf-8")
            post_sql = (output / "30-post-load.sql").read_text(encoding="utf-8")
            load_sql = (output / "20-load.sql").read_text(encoding="utf-8")
            self.assertIn("election_forecasts_2026_national", schema_sql)
            self.assertIn("election_forecasts_2026_state", schema_sql)
            self.assertIn("election_forecasts_2026_latest_national", post_sql)
            self.assertIn("election_forecasts_2026_latest_state", post_sql)
            self.assertIn("election_forecasts_2026_latest_vendor_runs", post_sql)
            self.assertIn("/docker-entrypoint-initdb.d/election_forecasts_2026_national.csv", load_sql)
            self.assertIn("/docker-entrypoint-initdb.d/election_forecasts_2026_state.csv", load_sql)

            # COPY uses NULL ''. Prepared empty fields must therefore be
            # unquoted; a quoted empty string would fail casts for optional
            # dates, timestamps, numerics, and booleans.
            national_csv = (output / "election_forecasts_2026_national.csv").read_text(
                encoding="utf-8"
            )
            state_csv = (output / "election_forecasts_2026_state.csv").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(',"",', national_csv)
            self.assertNotIn(',"",', state_csv)

    def test_leading_zero_geography_identifiers_remain_text(self):
        with NATIONAL_SAMPLE.open(encoding="utf-8", newline="") as handle:
            national_header = next(csv.reader(handle))
        with STATE_SAMPLE.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            state_rows = list(reader)
            state_header = reader.fieldnames
        self.assertEqual(national_header, self.preparer.NATIONAL_FIELDS)
        self.assertEqual(state_header, self.preparer.STATE_FIELDS)
        self.assertTrue(any(row["state_fips"] == "01" for row in state_rows))
        self.assertTrue(any(row["congressional_district"] == "0101" for row in state_rows))

        schema_sql = self.preparer.build_schema_sql()
        self.assertIn("state_fips TEXT NOT NULL", schema_sql)
        self.assertIn("congressional_district TEXT", schema_sql)

    def test_bad_sfcd_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            bad_state = temp_path / "bad_state.csv"
            with STATE_SAMPLE.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                rows = list(reader)
                fields = reader.fieldnames or []
            house_row = next(row for row in rows if row["congressional_district"])
            house_row["congressional_district"] = "101"
            house_row["geography_id"] = "101"
            with bad_state.open("w", encoding="utf-8", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=fields, quoting=csv.QUOTE_ALL)
                writer.writeheader()
                writer.writerows(rows)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PREPARER_PATH),
                    "--national-input",
                    str(NATIONAL_SAMPLE),
                    "--state-input",
                    str(bad_state),
                    "--validate-only",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("four digits", result.stderr)


class ForecastDatabaseDeploymentTests(unittest.TestCase):
    def test_help_requires_no_aws_or_docker(self):
        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        result = subprocess.run(
            ["bash", str(ROOT / "election_forecasts_ecs.sh"), "help"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Missing required command", result.stderr)
        self.assertIn("The down action never deletes", result.stdout)

    def test_lifecycle_validation_requires_no_aws_or_docker(self):
        env = os.environ.copy()
        env["NATIONAL_CSV"] = str(NATIONAL_SAMPLE)
        env["STATE_CSV"] = str(STATE_SAMPLE)
        result = subprocess.run(
            ["bash", str(ROOT / "election_forecasts_ecs.sh"), "validate"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("input validation passed", result.stdout)

    def test_stage_copies_exact_validated_csv_bytes(self):
        staged_national = IMAGE_ROOT / "data" / "election_forecasts_2026_national.csv"
        staged_state = IMAGE_ROOT / "data" / "election_forecasts_2026_state.csv"
        env = os.environ.copy()
        env["NATIONAL_CSV"] = str(NATIONAL_SAMPLE)
        env["STATE_CSV"] = str(STATE_SAMPLE)
        try:
            result = subprocess.run(
                ["bash", str(ROOT / "election_forecasts_ecs.sh"), "stage"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(staged_national.read_bytes(), NATIONAL_SAMPLE.read_bytes())
            self.assertEqual(staged_state.read_bytes(), STATE_SAMPLE.read_bytes())
        finally:
            staged_national.unlink(missing_ok=True)
            staged_state.unlink(missing_ok=True)

    def test_shell_scripts_parse(self):
        scripts = [
            ROOT / "election_forecasts_ecs.sh",
            ROOT / "deploy_election_forecasts_ecs.sh",
            ROOT / "status_election_forecasts_ecs.sh",
            ROOT / "destroy_election_forecasts_ecs.sh",
            ROOT / "load_election_forecasts_local.sh",
            DB_ROOT / "setup_local_loader.sh",
            IMAGE_ROOT / "docker-entrypoint-rhubarb.sh",
            IMAGE_ROOT / "initdb" / "90-create-reader.sh",
            IMAGE_ROOT / "initdb" / "99-pg-hba.sh",
        ]
        for script in scripts:
            result = subprocess.run(
                ["bash", "-n", str(script)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    def test_deployment_uses_fixed_reader_and_secret_location(self):
        lifecycle = (ROOT / "election_forecasts_ecs.sh").read_text(encoding="utf-8")
        template = (
            DB_ROOT / "cloudformation" / "election-forecasts-postgres.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('READER_USER="${READER_USER:-rhubarb_forecast_reader}"', lifecycle)
        self.assertIn("election-forecasts-postgres/DATABASE_URL", lifecycle)
        self.assertIn("Default: rhubarb_forecast_reader", template)
        self.assertIn("FORECAST_READER_PASSWORD", template)
        self.assertNotIn('DATA_DIR="${DATA_DIR:-/collected_data}"', lifecycle)
        self.assertIn('$SCRIPT_DIR/collected_data', lifecycle)
        self.assertIn('mkdir -p "$(dirname -- "$CONNECTION_FILE")"', lifecycle)
        self.assertIn('rm -f "$CONNECTION_FILE"', lifecycle)

    def test_remote_hba_allows_only_tls_reader(self):
        hba = (IMAGE_ROOT / "initdb" / "99-pg-hba.sh").read_text(encoding="utf-8")
        self.assertIn("hostssl ${POSTGRES_DB}", hba)
        self.assertIn("${FORECAST_READER_USER}", hba)
        self.assertIn("scram-sha-256", hba)
        self.assertIn("hostnossl all", hba)
        self.assertIn("reject", hba)

    def test_collector_container_uses_split_output_directory(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "docker-compose.example.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--output-dir", "/data"', dockerfile)
        self.assertIn('"--output-dir", "/data"', compose)
        self.assertNotIn('"--output", "/data/election_forecasts_2026.csv"', dockerfile)
        self.assertNotIn('"--output", "/data/election_forecasts_2026.csv"', compose)

    def test_docker_build_is_self_contained(self):
        dockerfile = (IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY data/election_forecasts_2026_national.csv", dockerfile)
        self.assertIn("COPY data/election_forecasts_2026_state.csv", dockerfile)
        self.assertIn("postgres:16-bookworm", dockerfile)
        self.assertNotIn("curl ", dockerfile)
        self.assertNotIn("wget ", dockerfile)


if __name__ == "__main__":
    unittest.main()
