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

echo "Created isolated Python environment: $VENV_DIR"
echo "No third-party packages are required."
echo "From an output directory, run: $SCRIPT_DIR/run.sh collect"
