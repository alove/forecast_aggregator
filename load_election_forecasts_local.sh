#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/forecast_database_ecs/.venv}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "ERROR: local-loader environment is missing." >&2
  echo "Run: $SCRIPT_DIR/forecast_database_ecs/setup_local_loader.sh" >&2
  exit 1
fi
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/load_election_forecasts_local.py" "$@"
