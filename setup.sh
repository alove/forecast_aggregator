#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN was not found. Install Python 3.10 or newer." >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required.")
PY

if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
  echo "ERROR: Python could not create a virtual environment." >&2
  echo "On Debian/Ubuntu, install the matching python3-venv package and retry." >&2
  exit 1
fi

"$VENV_DIR/bin/python" -m pip install --upgrade 'playwright>=1.55,<2'

echo "Created isolated Python environment: $VENV_DIR"
echo "Installed Playwright's Python driver for the public Infogram live-data fallback."
echo "The fallback reuses an existing Chrome/Chromium browser; it does not download another browser."
echo "Collect locally:       $SCRIPT_DIR/run.sh collect --output-dir $SCRIPT_DIR/collected_data"
echo "Validate DB inputs:    $SCRIPT_DIR/election_forecasts_ecs.sh validate"
echo "Deploy forecast DB:    $SCRIPT_DIR/election_forecasts_ecs.sh up"
