#!/usr/bin/env bash
set -Eeuo pipefail

# The NLB uses TCP passthrough, so PostgreSQL terminates TLS. Generate a fresh
# self-signed certificate whenever Fargate starts a new ephemeral database task.
# Clients intentionally use sslmode=require rather than certificate pinning.
TLS_DIR="${TLS_DIR:-/var/lib/postgresql/tls}"
TLS_CERT="${TLS_CERT:-$TLS_DIR/server.crt}"
TLS_KEY="${TLS_KEY:-$TLS_DIR/server.key}"
TLS_COMMON_NAME="${TLS_COMMON_NAME:-rhubarb-election-forecasts-postgres}"

if [ ! -s "$TLS_CERT" ] || [ ! -s "$TLS_KEY" ]; then
  install -d -m 0700 -o postgres -g postgres "$TLS_DIR"
  umask 077
  openssl req \
    -x509 \
    -newkey rsa:2048 \
    -sha256 \
    -nodes \
    -days 3650 \
    -subj "/CN=${TLS_COMMON_NAME}" \
    -keyout "$TLS_KEY" \
    -out "$TLS_CERT" \
    >/dev/null 2>&1
  chown postgres:postgres "$TLS_CERT" "$TLS_KEY"
  chmod 0644 "$TLS_CERT"
  chmod 0600 "$TLS_KEY"
fi

# Keep PostgreSQL aligned with the ECS/NLB listener when DB_PORT is overridden.
if [ "${1:-}" = "postgres" ]; then
  set -- "$@" -c "port=${POSTGRES_PORT:-${PGPORT:-5432}}"
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
