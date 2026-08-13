#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "ERROR: collector environment does not exist. Run $SCRIPT_DIR/setup.sh first." >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  set -- collect
fi

# Deliberately do not cd to SCRIPT_DIR. Relative CSV paths resolve from the
# caller's current directory while PYTHONPATH points at the collector package.
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV_DIR/bin/python" -m forecast_collector "$@"
