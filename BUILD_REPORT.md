# Build report — f_collector 1.3.0

## Change delivered

Added a complete standalone **2026 election forecast PostgreSQL datasource** to
`f_collector`. It loads the accumulated national and state/district CSV exports
into one database and mirrors the existing Rhubarb Stack Overflow survey
pattern:

- dedicated ECR repository;
- dedicated ECS/Fargate cluster, service, and task definition;
- internet-facing Network Load Balancer on PostgreSQL TCP/5432;
- one CloudFormation stack for the complete lifecycle;
- PostgreSQL-terminated TLS and SCRAM authentication;
- generated administrator and read-only passwords in AWS Secrets Manager;
- a fixed Secrets Manager path containing the complete read-only URI;
- CloudWatch PostgreSQL logs; and
- immutable image-based refreshes from the complete local append-only history.

The originally attached `ecs_examp.zip` contained only an empty directory. The
implementation was therefore compared against the complete original
`rhubarb-stackoverflow-survey-ecs` package from the project file library rather
than reconstructed from memory.

## New lifecycle

The primary command is:

```bash
./election_forecasts_ecs.sh ACTION
```

Supported actions:

```text
collect
validate
stage
up
refresh
rebuild
status
connection
credentials
smoke
logs
down
```

Compatibility wrappers are included:

```text
deploy_election_forecasts_ecs.sh
status_election_forecasts_ecs.sh
destroy_election_forecasts_ecs.sh
```

`up` uses the existing accumulated CSVs and collects only when one is absent.
`refresh` polls the selected forecast vendors, appends new observations locally,
validates the complete history, rebuilds the database image without cache, and
updates the ECS service to the immutable ECR image digest. `rebuild` performs
the image/database update without polling vendors.

`down` removes only the disposable AWS/database artifacts. It never deletes
`f_collector/collected_data`.

## Default resources and connection

```text
AWS region:          us-east-2
CloudFormation:      rhubarb-staging-election-forecasts-postgres
ECR repository:      rhubarb/election-forecasts-postgres
ECS cluster:         rhubarb-staging-election-forecast-cluster
ECS service:         rhubarb-staging-election-forecast-service
NLB:                 rhubarb-stg-forecast-nlb
Database:            election_forecasts
Read-only user:      rhubarb_forecast_reader
URI secret:          rhubarb/staging/election-forecasts-postgres/DATABASE_URL
Local URI file:      forecast_database_ecs/.outputs/election_forecasts_connection.env
```

The credential location and user/database names are predictable; the password
is a generated 40-character secret. The URI includes `sslmode=require`.

## Database objects

```text
public.election_forecasts_2026_national
public.election_forecasts_2026_state
public.election_forecasts_2026_load_metadata
public.election_forecasts_2026_latest_national
public.election_forecasts_2026_latest_state
public.election_forecasts_2026_latest_vendor_runs
```

The base tables preserve the complete local append-only history present at
build time. `state_fips` and four-character `congressional_district`/SFCD values
are stored as PostgreSQL `TEXT`, preserving leading zeroes. The latest views use
vendor, metric/race identity, forecast date, provider update time, pull time,
and run ID to select the newest published observation.

The load metadata records source paths, SHA-256 hashes, byte sizes, row counts,
vendor counts, vendor-run counts, pull-time range, export schema version, and
load time.

## Security behavior

- Only `rhubarb_forecast_reader` is remotely accepted by `pg_hba.conf`.
- Remote connections require TLS and SCRAM-SHA-256.
- Every other TLS or clear-text remote PostgreSQL login is rejected.
- The reader has `CONNECT`, schema `USAGE`, and `SELECT` only.
- Public database and schema creation privileges are revoked.
- The reader defaults to read-only transactions and has statement and idle
  transaction timeouts.
- The administrator password is injected into the task but is not exposed as a
  public connection URI.
- The default public ingress remains `0.0.0.0/0` to match the reference survey
  deployment, but the script warns prominently and supports `ALLOWED_CIDR`.

PostgreSQL uses a task-local self-signed certificate. `sslmode=require` encrypts
traffic but does not authenticate the endpoint certificate; a narrower ingress
CIDR should be used whenever Rhubarb's stable egress range is known.

## Data preparation

Added a standard-library-only validator and PostgreSQL bundle generator:

```text
forecast_database_ecs/image/prepare_election_forecasts.py
```

It requires export schema `2.0.0`, validates all timestamps, metric types,
parties, units, values, dates, booleans, geography identifiers, and unique
export identities, then generates:

```text
10-schema.sql
20-load.sql
30-post-load.sql
election_forecasts_2026_national.csv
election_forecasts_2026_state.csv
manifest.json
```

Prepared CSVs use unquoted empty fields so PostgreSQL `COPY ... NULL ''`
correctly loads optional dates, timestamps, numerics, and booleans as SQL
`NULL` rather than attempting to cast quoted empty strings.

## Local PostgreSQL loader

A separate optional loader is included:

```text
forecast_database_ecs/setup_local_loader.sh
load_election_forecasts_local.sh
load_election_forecasts_local.py
```

It uses a dedicated `forecast_database_ecs/.venv` and `psycopg`, leaving the
collector's standard-library-only environment unchanged. It defaults to the
local Rhubarb Docker PostgreSQL endpoint at `127.0.0.1:5434/postgres` and uses
only `ELECTION_FORECASTS_DATABASE_URL` as its URI environment override.

## Verification completed

- Python compilation: **PASS**
- Bash syntax validation for every shell script: **PASS**
- Unit and fixture-integration suite: **39/39 PASS**
- Sample national validation: **48 rows, 3 vendors, 3 vendor runs**
- Sample state/district validation: **8,460 rows, 3 vendors, 3 vendor runs**
- PostgreSQL schema/load/view generation: **PASS**
- Leading-zero FIPS and SFCD preservation: **PASS**
- Quoted-empty/SQL-NULL regression check: **PASS**
- Local-loader `--validate-only`: **PASS**
- CloudFormation YAML composition and structural audit: **PASS**
- Collector Docker/Compose split-output regression check: **PASS**
- Mocked full `up` lifecycle across AWS CLI, ECR, Docker buildx, CloudFormation,
  Secrets Manager, and connection-file generation: **PASS (42 external calls)**
- Clean release extraction, virtual-environment creation, test execution, and
  executable-bit check: **PASS**

## Environment limitation

No real AWS credentials or Docker daemon were available in the build
container, so this report does **not** claim that a live ECR image was built or a
real AWS stack was created here. The complete non-AWS data path and a mocked
end-to-end deployment path were exercised. The first real deployment should be
run from the authenticated Mac/Linux host with:

```bash
cd ~/f_collector
./election_forecasts_ecs.sh validate
./election_forecasts_ecs.sh up
./election_forecasts_ecs.sh smoke
```
