#!/usr/bin/env bash
set -Eeuo pipefail

: "${PGDATA:?PGDATA is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${FORECAST_READER_USER:?FORECAST_READER_USER is required}"

validate_hba_token() {
  case "$1" in
    ''|*[!A-Za-z0-9_]*)
      echo "ERROR: invalid pg_hba token: $1" >&2
      exit 1
      ;;
  esac
}

validate_hba_token "$POSTGRES_DB"
validate_hba_token "$FORECAST_READER_USER"

cat > "$PGDATA/pg_hba.conf" <<EOF_HBA
# Local initialization and container health checks.
local   all                     all                         trust

# The only remotely accessible login is the read-only Rhubarb datasource role.
# It must use TLS plus SCRAM authentication.
hostssl ${POSTGRES_DB}          ${FORECAST_READER_USER}     0.0.0.0/0       scram-sha-256

# Reject every other remote connection, including clear-text PostgreSQL.
hostssl all                     all                         0.0.0.0/0       reject
hostnossl all                   all                         0.0.0.0/0       reject
hostssl all                     all                         ::/0            reject
hostnossl all                   all                         ::/0            reject
EOF_HBA

chown postgres:postgres "$PGDATA/pg_hba.conf"
chmod 0600 "$PGDATA/pg_hba.conf"

echo "Installed TLS-only pg_hba.conf for the forecast reader role."
