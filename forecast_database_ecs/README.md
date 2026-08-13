# Rhubarb election-forecast ECS PostgreSQL database

This package deploys the two accumulated `f_collector` exports as one
self-contained PostgreSQL datasource in the Rhubarb staging AWS environment.
It mirrors the standalone Stack Overflow survey database pattern:

- a dedicated ECR repository;
- a dedicated ECS/Fargate cluster and service;
- an internet-facing Network Load Balancer on PostgreSQL TCP/5432;
- PostgreSQL-managed TLS and SCRAM authentication;
- generated admin and read-only passwords in AWS Secrets Manager;
- a stable Secrets Manager path containing the complete read-only URI;
- a CloudWatch log group; and
- a single CloudFormation stack that can be created, refreshed, inspected, and
  destroyed without touching the Rhubarb application stack.

The running Fargate task is intentionally ephemeral. The authoritative history
remains in:

```text
~/f_collector/collected_data/election_forecasts_2026_national.csv
~/f_collector/collected_data/election_forecasts_2026_state.csv
```

A deploy validates those files, copies them into the Docker build context, and
bakes the complete history into an immutable PostgreSQL image. If ECS replaces
the task, PostgreSQL reconstructs the same database from the image. A refresh
polls the forecast sources, appends new observations locally, and deploys a new
image containing the expanded history.

## Default AWS resources

```text
Region:             us-east-2
CloudFormation:     rhubarb-staging-election-forecasts-postgres
ECR repository:     rhubarb/election-forecasts-postgres
ECS cluster:        rhubarb-staging-election-forecast-cluster
ECS service:        rhubarb-staging-election-forecast-service
Task family:        rhubarb-staging-election-forecast-task
NLB:                rhubarb-stg-forecast-nlb
Target group:       rhubarb-stg-forecast-tg
CloudWatch logs:    /ecs/rhubarb-staging-election-forecasts
Database:           election_forecasts
Read-only user:     rhubarb_forecast_reader
URI secret:         rhubarb/staging/election-forecasts-postgres/DATABASE_URL
Local URI file:     forecast_database_ecs/.outputs/election_forecasts_connection.env
```

The reader password is deliberately random, not guessable. The **location** of
that credential is stable and easy to retrieve.

## Requirements

- AWS CLI v2 authenticated to the intended AWS account;
- Docker Desktop or Docker Engine with `docker buildx`;
- Python 3.10 or newer;
- the collector environment created by `./setup.sh`;
- AWS permissions for CloudFormation, ECS, ECR, EC2/VPC security groups,
  Elastic Load Balancing, IAM, CloudWatch Logs, and Secrets Manager; and
- a default VPC with two public subnets in different Availability Zones, or
  explicit `VPC_ID` and `SUBNET_IDS` overrides.

The image is always built for `linux/amd64`, including from Apple Silicon Macs,
to match the Fargate task architecture.

A non-secret override template is available at
`forecast_database_ecs/config.env.example`. Copy it elsewhere or source it and
change only the values you need; passwords are never stored in that file.

## First deployment

From the repository root:

```bash
cd ~/f_collector

# Poll sources and append new observations locally.
./election_forecasts_ecs.sh collect

# Validate both complete accumulated CSVs without touching AWS.
./election_forecasts_ecs.sh validate

# Build/push the immutable database image and create the AWS service.
./election_forecasts_ecs.sh up
```

`up` uses existing CSVs and collects only when one is missing. It does not poll
vendors merely because you redeploy infrastructure.

The default ingress matches the Stack Overflow survey deployment:

```text
0.0.0.0/0 -> public NLB TCP/5432 -> ECS PostgreSQL
```

PostgreSQL still permits only the generated read-only account remotely and
requires TLS/SCRAM. Restrict the listener whenever a narrower Rhubarb egress
CIDR is available:

```bash
ALLOWED_CIDR='203.0.113.10/32' ./election_forecasts_ecs.sh up
```

To use explicit networking:

```bash
VPC_ID='vpc-...' \
SUBNET_IDS='subnet-a subnet-b' \
./election_forecasts_ecs.sh up
```

## Normal refresh procedure

```bash
cd ~/f_collector
EFC_CONTACT_EMAIL='you@example.com' \
./election_forecasts_ecs.sh refresh
```

`refresh` performs one synchronous workflow:

1. runs the collector against all enabled sources;
2. appends only new vendor-run identities to both local CSVs;
3. validates the complete files and their schema;
4. stages exact copies into the Docker build context;
5. builds and pushes a no-cache `linux/amd64` image;
6. updates the CloudFormation stack to the immutable image digest; and
7. rewrites the fixed Secrets Manager URI and local connection file.

If one selected collector source fails, refresh fails by default rather than
silently publishing a partial polling run. To deploy the successful sources
while retaining the collector's partial-success behavior:

```bash
ALLOW_PARTIAL_COLLECT=true ./election_forecasts_ecs.sh refresh
```

Limit a refresh to named adapters:

```bash
COLLECT_SOURCES='electindex election-statsheet' \
./election_forecasts_ecs.sh refresh
```

Rebuild the database from the current CSVs without polling vendors:

```bash
./election_forecasts_ecs.sh rebuild
```

## Credentials and Rhubarb connection

Print only the URI:

```bash
./election_forecasts_ecs.sh connection
```

Print all connection components and retrieval locations:

```bash
./election_forecasts_ecs.sh credentials
```

Retrieve the URI directly from AWS later:

```bash
aws secretsmanager get-secret-value \
  --region us-east-2 \
  --secret-id rhubarb/staging/election-forecasts-postgres/DATABASE_URL \
  --query SecretString \
  --output text
```

Or source the local mode-600 file after deployment:

```bash
source ~/f_collector/forecast_database_ecs/.outputs/election_forecasts_connection.env
printf '%s\n' "$ELECTION_FORECASTS_DATABASE_URL"
```

Use that URI for a Rhubarb PostgreSQL datasource. It includes
`sslmode=require`. Only the reader is remotely permitted; the generated admin
credential is injected into the task and is not exposed as a connection URI.

## Database objects

```text
public.election_forecasts_2026_national
public.election_forecasts_2026_state
public.election_forecasts_2026_load_metadata
public.election_forecasts_2026_latest_national
public.election_forecasts_2026_latest_state
public.election_forecasts_2026_latest_vendor_runs
```

The two base tables contain the complete append-only local history present at
build time. The `latest_*` views select each vendor's most recent observation
for each metric/race/party identity.

### Current national forecasts

```sql
SELECT
    vendor,
    metric_type,
    party,
    value,
    unit,
    vendor_forecast_date,
    rhubarb_pull_time
FROM public.election_forecasts_2026_latest_national
ORDER BY metric_type, vendor, party;
```

### Current House district probabilities

```sql
SELECT
    vendor,
    congressional_district,
    state,
    house_seat,
    party,
    value AS probability_pct,
    vendor_forecast_date
FROM public.election_forecasts_2026_latest_state
WHERE metric_type = 'US House District Party Probability'
ORDER BY congressional_district, vendor, party;
```

### Current Senate race probabilities

```sql
SELECT
    vendor,
    state,
    senate_seat,
    party,
    value AS probability_pct,
    vendor_forecast_date
FROM public.election_forecasts_2026_latest_state
WHERE metric_type = 'US Senate Race Party Probability'
ORDER BY state, vendor, party;
```

### Confirm what was loaded

```sql
SELECT *
FROM public.election_forecasts_2026_load_metadata;
```

## Operations

```bash
./election_forecasts_ecs.sh status
./election_forecasts_ecs.sh logs
./election_forecasts_ecs.sh smoke   # requires local psql
```

The smoke test confirms that the remote login is read-only and reports row,
vendor, and latest-pull counts for both tables.

## Scheduling from a Mac or Linux host

A host-driven refresh requires the machine to be awake, Docker running, AWS
credentials available, and network access to the forecast sources. Example
cron entry:

```cron
20 9 * * * cd /home/USER/f_collector && EFC_CONTACT_EMAIL=you@example.com ./election_forecasts_ecs.sh refresh >> collector.log 2>&1
```

Ready-to-edit examples are included at:

```text
deploy/election-forecast-database-refresh.service.example
deploy/election-forecast-database-refresh.timer.example
deploy/com.rhubarb.election-forecast-database-refresh.plist.example
```

On macOS, use the `launchd` example rather than cron if Docker Desktop and login
session behavior matter. The scheduled user must have working AWS credentials
and permission to use Docker.

## Local PostgreSQL load

A separate optional virtual environment keeps `psycopg` out of the collector's
standard-library-only environment:

```bash
./forecast_database_ecs/setup_local_loader.sh
./load_election_forecasts_local.sh
```

The loader defaults to Rhubarb's local Docker PostgreSQL:

```text
postgresql://postgres:postgres@127.0.0.1:5434/postgres
```

Override with a URI:

```bash
./load_election_forecasts_local.sh \
  --database-url 'postgresql://user:password@host:5432/database?sslmode=require'
```

## Destroy

```bash
./election_forecasts_ecs.sh down
```

This removes the CloudFormation stack, generated AWS secrets, the fixed
connection URI secret, local connection output, and—by default—the dedicated
ECR repository. It **never deletes `collected_data`**.

Keep ECR images:

```bash
PURGE_ECR=false ./election_forecasts_ecs.sh down
```

Also remove the duplicate CSVs staged in the Docker build context:

```bash
PURGE_STAGED_DATA=true ./election_forecasts_ecs.sh down
```

## Data licensing

The database preserves source URLs, source-file labels, vendor names, and
forecast metadata. Rights and attribution remain source-specific. Do not remove
required attribution or assume that one vendor's license applies to another
vendor's data.
