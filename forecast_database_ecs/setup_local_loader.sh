#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  echo "ERROR: $PYTHON_BIN was not found" >&2
  exit 127
}

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements-local.txt"

echo "Local PostgreSQL loader environment: $VENV_DIR"
echo "Run: $VENV_DIR/bin/python $SCRIPT_DIR/../load_election_forecasts_local.py"
