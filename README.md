# 2026 Election Forecast Collector

Local-first append-only collector for publicly available 2026 U.S. congressional election forecasts. It runs on macOS or Linux with Python 3.10+ and uses only the Python standard library.

## Enabled sources

1. **Election StatSheet / Mac Tan** — national House seats and House popular vote, national Senate seats, all 435 House district probabilities, all 35 Senate race probabilities, and a historical timeline.
2. **ElectIndex** — public chamber totals, projected national House vote counts, all 435 House races, and all 35 Senate races.
3. **Grant Williams** — an atomic public House/Senate JSON bundle with national forecasts, all 435 House districts, and all Senate races in the cycle.
4. **Race to the WH / Logan Phillips** — the public House and Senate forecast Infograms: national chamber projections and control probabilities, the adjusted national House popular-vote projection, all 435 House districts, and all 35 Senate races.

The first three adapters use stable public raw CSV/JSON endpoints. Race to the WH publishes its forecast through public Infogram embeds. That adapter discovers the current embeds from the publisher's House and Senate pages, reads the static data payload already delivered with each public embed, and refuses the source run unless it can identify the required national metrics plus exactly 435 House districts and 35 Senate races. It does not use a browser, log in, or bypass an access control.

## Install

```bash
./setup.sh
```

The installer creates a project-local `.venv`. No third-party packages are required.

Set a contact email in the HTTP user agent before automated use:

```bash
export EFC_CONTACT_EMAIL='you@example.com'
```

## Collect

Run from the directory where you want the data files to live:

```bash
/path/to/f_collector/run.sh collect --save-raw
```

The collector now writes **two long-form CSVs**:

```text
./election_forecasts_2026_national.csv
./election_forecasts_2026_state.csv
```

### National CSV

One row = one vendor, one forecast run, one metric, one party, one value.

`metric_type` is one of:

- `US House Seats by Party`
- `US House Party Probability`
- `US Senate Seats by Party`
- `US Senate Party Probability`
- `US House Popular Vote Projection`
- `US House Popular Vote Margin`

Examples:

```text
metric_type,party,value,unit
US House Seats by Party,D,237.8,seats
US House Party Probability,D,71.7,percent
US House Popular Vote Projection,D,53.7,percent
US House Popular Vote Margin,D-R,7.4,percentage_points
```

### State / district CSV

One row = one vendor, one forecast run, one race, one metric, one party, one value.

`metric_type` is one of:

- `US House District Party Probability`
- `US House District Vote Projection`
- `US Senate Race Party Probability`
- `US Senate Race Vote Projection`

House rows retain `state_fips`, state name, and four-character SFCD in `congressional_district`. Senate rows retain state/FIPS and the Senate seat label.

### Pull timestamp

Both files include `rhubarb_pull_time`, a timezone-aware UTC ISO-8601 timestamp truncated to the second, e.g.:

```text
2026-08-13T01:11:07+00:00
```

`observed_datetime_utc` is retained as an alias for compatibility and has the same value.

## Idempotency

A second run does **not** append the same model snapshot again merely because the pull time is later. Export identity is:

```text
vendor + vendor_run_id + metric_type + source_record_id + party
```

If a provider publishes a new/corrected run ID, the new values append as a new observation.

## Commands

```bash
# All enabled providers
./run.sh collect --save-raw

# One provider
./run.sh collect --source election-statsheet
./run.sh collect --source electindex
./run.sh collect --source grant-williams
./run.sh collect --source race-to-the-wh

# Historical Election StatSheet backfill
./run.sh collect --source election-statsheet --backfill-election-statsheet

# Put both CSVs (and raw_snapshots/ when --save-raw is used) in one directory
./run.sh collect --output-dir ~/election-model-average --save-raw

# Explicit files (each overrides --output-dir for that file)
./run.sh collect \
  --national-output /path/national.csv \
  --state-output /path/state.csv

# National only (no individual race retrieval)
./run.sh collect --skip-house-districts --skip-senate-races

# Fetch/parse/validate without writing
./run.sh collect --dry-run

# Validate either new CSV
./run.sh validate ./election_forecasts_2026_national.csv
./run.sh validate ./election_forecasts_2026_state.csv

./run.sh sources
./run.sh schema
```

## Data conventions

- `party`: `D`, `R`, `Other`, or `D-R` for the signed House popular-vote margin row.
- `value`: the single primary value represented by the row.
- `unit`: `percent`, `seats`, or `percentage_points`.
- `median_value`, `low_value`, `high_value`: populated when the source publishes those intervals/medians.
- Percentages are 0–100, not 0–1.
- `congressional_district` is four text digits: state FIPS + two-digit district. Examples: Alabama 1 = `0101`, New York 7 = `3607`, Wyoming at-large = `5601`.
- Spreadsheet software may auto-convert `0101` to `101`; import the field as text.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The source adapters still validate their complete internal snapshots before conversion to the two export files. The export layer then independently validates metric types, values, geography identifiers, second-level timestamps, and duplicate identities.

For a first check of the rendered-source adapter without modifying either CSV:

```bash
./run.sh collect \
  --source race-to-the-wh \
  --output-dir ~/f_collector/collected_data \
  --save-raw \
  --dry-run \
  --verbose
```

If Race to the WH changes its Infogram layout, the adapter exits that source with a visible error rather than appending a partial or malformed snapshot. Other selected sources remain independent.

## GitHub-canonical collection and database deployment

For scheduled production use, use `sync_forecast_database.sh` rather than the
lower-level `election_forecasts_ecs.sh refresh` action. The sync runner makes
the configured GitHub branch the canonical historical store for the two CSVs.

```bash
# Normal run: fetch/fast-forward, collect, compare to GitHub, append-only guard,
# commit/push changed CSVs, and deploy only when AWS data is stale.
EFC_CONTACT_EMAIL='you@example.com' ./sync_forecast_database.sh

# Report local/GitHub/AWS state without collecting or deploying.
./sync_forecast_database.sh status

# Recovery/deploy-only path: do not poll vendors; deploy current GitHub data if stale.
./sync_forecast_database.sh deploy

# Skip all vendor polling, compare the existing CSVs to GitHub, and verify the
# Git push path. Deploy only if AWS is stale.
./sync_forecast_database.sh run --skip-collect

# Full deployment-path test without hitting vendor sites: skip collection and
# force a new image build/ECR push/ECS deployment even when the CSVs are unchanged.
./sync_forecast_database.sh run --skip-collect --force-deploy

# Rebuild current GitHub data even if AWS already serves the same fingerprint.
./sync_forecast_database.sh deploy --force-deploy
```

The runner refuses to operate when the worktree/index contains unexplained
changes, uses only fast-forward Git synchronization, requires the GitHub CSV
history to be an exact prefix of the newly collected files, and pushes the
canonical CSV commit **before** changing AWS. If AWS deployment fails after the
push, the next run detects the stale deployed fingerprint and retries.

Only these files under `collected_data/` are Git-tracked:

```text
collected_data/election_forecasts_2026_national.csv
collected_data/election_forecasts_2026_state.csv
```

Raw snapshots and lock files remain ignored.

## Standalone ECS PostgreSQL datasource

The repository includes a deployable read-only PostgreSQL service containing
both accumulated CSVs in one database. It follows the same standalone
ECS/Fargate + ECR + CloudFormation + public NLB pattern used for the Rhubarb
Stack Overflow survey datasource.

```bash
# Validate the files currently in ~/f_collector/collected_data.
./election_forecasts_ecs.sh validate

# First deploy using the existing files.
./election_forecasts_ecs.sh up

# Poll sources, append locally, rebuild the image, and update ECS.
EFC_CONTACT_EMAIL='you@example.com' ./election_forecasts_ecs.sh refresh

# Retrieve the stable read-only connection.
./election_forecasts_ecs.sh credentials

# Inspect or remove the standalone service.
./election_forecasts_ecs.sh status
./election_forecasts_ecs.sh logs
./election_forecasts_ecs.sh down
```

Default database objects:

```text
public.election_forecasts_2026_national
public.election_forecasts_2026_state
public.election_forecasts_2026_load_metadata
public.election_forecasts_2026_latest_national
public.election_forecasts_2026_latest_state
public.election_forecasts_2026_latest_vendor_runs
```

The database name is `election_forecasts`, the fixed reader name is
`rhubarb_forecast_reader`, and the complete generated URI is written to:

```text
AWS Secrets Manager:
  rhubarb/staging/election-forecasts-postgres/DATABASE_URL

Local mode-600 file:
  forecast_database_ecs/.outputs/election_forecasts_connection.env
```

The Fargate database is ephemeral; the authoritative append-only history is the
Git-tracked `collected_data` pair on the configured GitHub branch. The complete
deployment and refresh procedure is in
[`forecast_database_ecs/README.md`](forecast_database_ecs/README.md).
