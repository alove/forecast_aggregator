from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "forecast_database_ecs" / "git_history_guard.py"
SYNC = ROOT / "sync_forecast_database.sh"


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["schema_version", "vendor", "value"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class HistoryGuardTests(unittest.TestCase):
    def run_guard(self, rn: Path, rs: Path, ln: Path, ls: Path):
        return subprocess.run(
            [
                sys.executable,
                str(GUARD),
                "--remote-national", str(rn),
                "--remote-state", str(rs),
                "--local-national", str(ln),
                "--local-state", str(ls),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_append_only_change_reports_vendor_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            base = [{"schema_version": "2.0.0", "vendor": "A", "value": "1"}]
            write_csv(d / "rn.csv", base)
            write_csv(d / "rs.csv", base)
            write_csv(d / "ln.csv", base + [{"schema_version": "2.0.0", "vendor": "B", "value": "2"}])
            write_csv(d / "ls.csv", base + [{"schema_version": "2.0.0", "vendor": "B", "value": "3"}])
            result = self.run_guard(d / "rn.csv", d / "rs.csv", d / "ln.csv", d / "ls.csv")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["changed"])
            self.assertEqual(payload["vendors"], ["B"])
            self.assertEqual(payload["vendor_count"], 1)
            self.assertEqual(payload["national_added_rows"], 1)
            self.assertEqual(payload["state_added_rows"], 1)
            self.assertEqual(len(payload["local_fingerprint"]), 64)

    def test_rewriting_github_history_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            d = Path(temp)
            old = [
                {"schema_version": "2.0.0", "vendor": "A", "value": "1"},
                {"schema_version": "2.0.0", "vendor": "B", "value": "2"},
            ]
            rewritten = [
                {"schema_version": "2.0.0", "vendor": "A", "value": "99"},
                {"schema_version": "2.0.0", "vendor": "B", "value": "2"},
            ]
            for name in ("rn.csv", "rs.csv"):
                write_csv(d / name, old)
            for name in ("ln.csv", "ls.csv"):
                write_csv(d / name, rewritten)
            result = self.run_guard(d / "rn.csv", d / "rs.csv", d / "ln.csv", d / "ls.csv")
            self.assertEqual(result.returncode, 2)
            self.assertIn("rewritten or reordered", result.stderr)


class SyncStaticTests(unittest.TestCase):
    def test_canonical_csvs_are_trackable_but_raw_snapshots_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("collected_data/*", ignore)
        self.assertIn("!collected_data/election_forecasts_2026_national.csv", ignore)
        self.assertIn("!collected_data/election_forecasts_2026_state.csv", ignore)
        self.assertIn("/election_forecasts_2026_national.csv", ignore)
        self.assertIn("/election_forecasts_2026_state.csv", ignore)
        self.assertNotIn("\nelection_forecasts_2026_national.csv\n", ignore)

    def test_sync_has_required_git_safety_and_ordering(self):
        text = SYNC.read_text(encoding="utf-8")
        self.assertIn("Unexplained local Git changes detected", text)
        self.assertIn('git -C "$SCRIPT_DIR" fetch --prune', text)
        self.assertIn('git -C "$SCRIPT_DIR" merge --ff-only', text)
        self.assertIn('git -C "$SCRIPT_DIR" add --all', text)
        self.assertIn('git -C "$SCRIPT_DIR" push', text)
        self.assertLess(text.index("commit_and_push"), text.index("deploy_if_needed"))
        self.assertIn("deployed-fingerprint", text)
        self.assertIn("FORECAST_DATA_GIT_SHA", text)
        self.assertIn("FORECAST_DEPLOYER_VERSION", text)
        self.assertIn("--skip-collect", text)
        self.assertIn("verify_push_path_without_commit", text)

    def test_cloudformation_records_provenance_in_task_environment(self):
        template = (ROOT / "forecast_database_ecs" / "cloudformation" / "election-forecasts-postgres.yml").read_text(encoding="utf-8")
        for token in (
            "FORECAST_DATA_GIT_SHA",
            "FORECAST_DATA_FINGERPRINT",
            "FORECAST_SCHEMA_VERSION",
            "FORECAST_DEPLOYER_VERSION",
            "FORECAST_DEPLOYED_AT",
            "FORECAST_NATIONAL_SHA256",
            "FORECAST_STATE_SHA256",
        ):
            self.assertIn(token, template)


class SyncIntegrationTests(unittest.TestCase):
    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)

    def make_repo(self, temp: Path) -> tuple[Path, Path]:
        remote = temp / "remote.git"
        repo = temp / "repo"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
        shutil.copy2(SYNC, repo / "sync_forecast_database.sh")
        (repo / "forecast_database_ecs").mkdir()
        shutil.copy2(GUARD, repo / "forecast_database_ecs" / "git_history_guard.py")
        (repo / "collected_data").mkdir()
        row = [{"schema_version": "2.0.0", "vendor": "A", "value": "1"}]
        write_csv(repo / "collected_data" / "election_forecasts_2026_national.csv", row)
        write_csv(repo / "collected_data" / "election_forecasts_2026_state.csv", row)
        (repo / ".gitignore").write_text("*.lock\nforecast_database_ecs/image/data/*.csv\n", encoding="utf-8")
        (repo / "README.md").write_text("test\n", encoding="utf-8")
        (repo / "forecast_database_ecs" / "image" / "data").mkdir(parents=True)
        # Idempotent fake collector: append vendor B once to both files.
        (repo / "run.sh").write_text(
            """#!/usr/bin/env bash\nset -eu\npython3 - <<'PY2'\nimport csv\nfrom pathlib import Path\nfor name in ('election_forecasts_2026_national.csv','election_forecasts_2026_state.csv'):\n p=Path('collected_data')/name\n rows=list(csv.DictReader(p.open()))\n if not any(r['vendor']=='B' for r in rows):\n  with p.open('a', newline='') as f:\n   csv.writer(f, quoting=csv.QUOTE_ALL, lineterminator='\\n').writerow(['2.0.0','B','2'])\nPY2\n""",
            encoding="utf-8",
        )
        os.chmod(repo / "run.sh", 0o755)
        # Fake deployer stores deployment metadata under .git so Git remains clean.
        (repo / "election_forecasts_ecs.sh").write_text(
            """#!/usr/bin/env bash\nset -eu\nstate=\"$(git rev-parse --git-dir)/fake_deploy.env\"\ncase \"${1:-}\" in\n validate|smoke) exit 0 ;;\n rebuild) printf '%s\\n%s\\n%s\\n' \"${FORECAST_DATA_GIT_SHA:-}\" \"${FORECAST_DATA_FINGERPRINT:-}\" \"${FORECAST_DEPLOYER_VERSION:-}\" > \"$state\" ;;\n deployed-git-sha) [ ! -f \"$state\" ] || sed -n '1p' \"$state\" ;;\n deployed-fingerprint) [ ! -f \"$state\" ] || sed -n '2p' \"$state\" ;;\n deployed-version) [ ! -f \"$state\" ] || sed -n '3p' \"$state\" ;;\n *) exit 0 ;;\nesac\n""",
            encoding="utf-8",
        )
        os.chmod(repo / "election_forecasts_ecs.sh", 0o755)
        subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
        return repo, remote

    def test_full_run_commits_before_deploy_and_leaves_repo_clean(self):
        with tempfile.TemporaryDirectory() as td:
            repo, remote = self.make_repo(Path(td))
            result = subprocess.run(["bash", "./sync_forecast_database.sh"], cwd=repo, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout, "")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            remote_head = subprocess.run(["git", f"--git-dir={remote}", "rev-parse", "main"], text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(local_head, remote_head)
            self.assertIn("updates from 1 vendors", self.git(repo, "log", "-1", "--pretty=%s").stdout)
            state = (repo / ".git" / "fake_deploy.env").read_text().splitlines()
            self.assertEqual(state[0], local_head)
            self.assertEqual(state[2], "1.4.1")
            second = subprocess.run(["bash", "./sync_forecast_database.sh"], cwd=repo, text=True, capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("no changes relative", second.stdout)
            self.assertIn("no rebuild required", second.stdout)
            self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), local_head)

    def test_skip_collect_force_deploy_avoids_vendor_calls_and_exercises_push_and_deploy(self):
        with tempfile.TemporaryDirectory() as td:
            repo, remote = self.make_repo(Path(td))
            initial_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            result = subprocess.run(
                ["bash", "./sync_forecast_database.sh", "run", "--skip-collect", "--force-deploy"],
                cwd=repo, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Skipping vendor collection", result.stdout)
            self.assertIn("there is no data commit to create", result.stdout)
            self.assertIn("Git push path verified", result.stdout)
            self.assertIn("Building and deploying exact Git commit", result.stdout)
            # If the collector had run, its fixture would append vendor B.
            with (repo / "collected_data" / "election_forecasts_2026_national.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({r["vendor"] for r in rows}, {"A"})
            self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), initial_head)
            remote_head = subprocess.run(
                ["git", f"--git-dir={remote}", "rev-parse", "main"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            self.assertEqual(remote_head, initial_head)
            state = (repo / ".git" / "fake_deploy.env").read_text().splitlines()
            self.assertEqual(state[0], initial_head)
            self.assertEqual(state[2], "1.4.1")
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout, "")

    def test_dirty_repo_fails_before_collection(self):
        with tempfile.TemporaryDirectory() as td:
            repo, _ = self.make_repo(Path(td))
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            before = (repo / "collected_data" / "election_forecasts_2026_national.csv").read_text()
            result = subprocess.run(["bash", "./sync_forecast_database.sh"], cwd=repo, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 4)
            self.assertIn("Unexplained local Git changes", result.stderr)
            self.assertEqual((repo / "collected_data" / "election_forecasts_2026_national.csv").read_text(), before)


if __name__ == "__main__":
    unittest.main()
