#!/usr/bin/env bash
set -Eeuo pipefail

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${FORECAST_READER_USER:?FORECAST_READER_USER is required}"
: "${FORECAST_READER_PASSWORD:?FORECAST_READER_PASSWORD is required}"

validate_identifier() {
  case "$1" in
    ''|[0-9]*|*[!A-Za-z0-9_]*)
      echo "ERROR: invalid PostgreSQL identifier: $1" >&2
      exit 1
      ;;
  esac
}

validate_identifier "$POSTGRES_DB"
validate_identifier "$POSTGRES_USER"
validate_identifier "$FORECAST_READER_USER"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set db_name="$POSTGRES_DB" \
  --set admin_user="$POSTGRES_USER" \
  --set reader_user="$FORECAST_READER_USER" \
  --set reader_password="$FORECAST_READER_PASSWORD" \
  <<'SQL'
CREATE ROLE :"reader_user"
  LOGIN
  PASSWORD :'reader_password'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOREPLICATION
  NOBYPASSRLS
  CONNECTION LIMIT 20;

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE :"db_name" TO :"reader_user";
GRANT USAGE ON SCHEMA public TO :"reader_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"reader_user";

ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
  GRANT SELECT ON TABLES TO :"reader_user";

ALTER ROLE :"reader_user" IN DATABASE :"db_name"
  SET search_path = public;
ALTER ROLE :"reader_user" IN DATABASE :"db_name"
  SET default_transaction_read_only = on;
ALTER ROLE :"reader_user" IN DATABASE :"db_name"
  SET statement_timeout = '120s';
ALTER ROLE :"reader_user" IN DATABASE :"db_name"
  SET idle_in_transaction_session_timeout = '60s';
SQL

echo "Created read-only role: ${FORECAST_READER_USER}"
