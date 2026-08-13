# 2026 Election Forecast Collector

Local-first append-only collector for publicly available 2026 U.S. congressional election forecasts. It runs on macOS or Linux with Python 3.10+ and uses only the Python standard library.

## Enabled sources

1. **Election StatSheet / Mac Tan** — national House seats and House popular vote, national Senate seats, all 435 House district probabilities, all 35 Senate race probabilities, and a historical timeline.
2. **ElectIndex** — public chamber totals, projected national House vote counts, all 435 House races, and all 35 Senate races.
3. **Grant Williams** — an atomic public House/Senate JSON bundle with national forecasts, all 435 House districts, and all Senate races in the cycle.

These three adapters use stable, public raw CSV/JSON endpoints. **Race to the WH is the fourth qualifying model discussed during source research, but it is not enabled yet because a stable public machine-readable endpoint for its complete forecast bundle has not been verified.** Its public rendered pages are not silently scraped by this release.

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
