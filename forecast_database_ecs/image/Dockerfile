# syntax=docker/dockerfile:1.7
FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS builder

WORKDIR /work
COPY prepare_election_forecasts.py /work/prepare_election_forecasts.py
COPY data/election_forecasts_2026_national.csv /work/election_forecasts_2026_national.csv
COPY data/election_forecasts_2026_state.csv /work/election_forecasts_2026_state.csv
RUN python /work/prepare_election_forecasts.py \
      --national-input /work/election_forecasts_2026_national.csv \
      --state-input /work/election_forecasts_2026_state.csv \
      --output-dir /prepared \
      --copy-root /docker-entrypoint-initdb.d

FROM --platform=$TARGETPLATFORM postgres:16-bookworm

LABEL org.opencontainers.image.title="Rhubarb 2026 Election Forecast PostgreSQL"
LABEL org.opencontainers.image.description="Disposable read-only PostgreSQL service containing f_collector national, state, Senate-race, and House-district forecast history"
LABEL org.opencontainers.image.vendor="Rhubarb Analytics Inc."
LABEL org.opencontainers.image.licenses="LicenseRef-Source-Specific"

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /prepared/10-schema.sql /docker-entrypoint-initdb.d/10-schema.sql
COPY --from=builder /prepared/20-load.sql /docker-entrypoint-initdb.d/20-load.sql
COPY --from=builder /prepared/30-post-load.sql /docker-entrypoint-initdb.d/30-post-load.sql
COPY --from=builder /prepared/election_forecasts_2026_national.csv /docker-entrypoint-initdb.d/election_forecasts_2026_national.csv
COPY --from=builder /prepared/election_forecasts_2026_state.csv /docker-entrypoint-initdb.d/election_forecasts_2026_state.csv
COPY --from=builder /prepared/manifest.json /opt/rhubarb-election-forecasts/manifest.json
COPY initdb/90-create-reader.sh /docker-entrypoint-initdb.d/90-create-reader.sh
COPY initdb/99-pg-hba.sh /docker-entrypoint-initdb.d/99-pg-hba.sh
COPY docker-entrypoint-rhubarb.sh /usr/local/bin/docker-entrypoint-rhubarb.sh

RUN chmod 0755 \
        /usr/local/bin/docker-entrypoint-rhubarb.sh \
        /docker-entrypoint-initdb.d/90-create-reader.sh \
        /docker-entrypoint-initdb.d/99-pg-hba.sh \
    && chmod 0444 \
        /docker-entrypoint-initdb.d/10-schema.sql \
        /docker-entrypoint-initdb.d/20-load.sql \
        /docker-entrypoint-initdb.d/30-post-load.sql \
        /docker-entrypoint-initdb.d/election_forecasts_2026_national.csv \
        /docker-entrypoint-initdb.d/election_forecasts_2026_state.csv \
        /opt/rhubarb-election-forecasts/manifest.json

EXPOSE 5432

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-rhubarb.sh"]
CMD ["postgres", "-c", "listen_addresses=*", "-c", "ssl=on", "-c", "ssl_cert_file=/var/lib/postgresql/tls/server.crt", "-c", "ssl_key_file=/var/lib/postgresql/tls/server.key", "-c", "ssl_min_protocol_version=TLSv1.2", "-c", "password_encryption=scram-sha-256", "-c", "max_connections=50", "-c", "shared_buffers=256MB", "-c", "work_mem=8MB", "-c", "maintenance_work_mem=128MB", "-c", "idle_in_transaction_session_timeout=60000", "-c", "log_min_duration_statement=5000"]
