# Export schema 2.0.0

The collector writes two append-only, long-form CSVs.

## Shared fields

| Field | Meaning |
|---|---|
| `schema_version` | Export schema version (`2.0.0`) |
| `rhubarb_pull_time` | UTC time Rhubarb retrieved the source, ISO-8601 to the second |
| `observed_datetime_utc` | Compatibility alias of `rhubarb_pull_time` |
| `vendor` | Forecast publisher/model vendor |
| `vendor_model` | Model name/version when available |
| `vendor_run_id` | Stable source run/snapshot ID |
| `vendor_forecast_date` | Provider forecast date |
| `vendor_updated_at_utc` | Provider update timestamp when supplied |
| `model_status` | Provider model status |
| `election_date` | Election date |
| `metric_type` | Human-readable metric discriminator |
| `party` | `D`, `R`, `Other`, or `D-R` for margin |
| `value` | Primary numeric observation |
| `unit` | `percent`, `seats`, or `percentage_points` |
| `median_value` | Source median if published |
| `low_value` | Lower uncertainty bound if published |
| `high_value` | Upper uncertainty bound if published |
| `basis` | Source/model interpretation note for the metric |
| `source_record_id` | Provider row/race identifier |
| `source_url` | Public source location |
| `source_file` | Raw source file(s) |
| `data_quality` | Provider quality/status text |
| `notes` | Additional provenance |

## National file

Default: `election_forecasts_2026_national.csv`

Additional fields: `geography_type`, `geography_id`, `geography_name`. They are `national`, `US`, and `United States`.

Metric types:

- `US House Seats by Party`
- `US House Party Probability`
- `US Senate Seats by Party`
- `US Senate Party Probability`
- `US House Popular Vote Projection`
- `US House Popular Vote Margin`

## State / district file

Default: `election_forecasts_2026_state.csv`

Additional fields:

- `geography_type`: `congressional_district` or `state`
- `geography_id`: four-digit SFCD for House; two-digit state FIPS for Senate
- `state_fips`
- `state_abbreviation`
- `state`
- `congressional_district`
- `house_seat_number`
- `house_seat`
- `senate_seat`
- `special_election`
- `rating`

Metric types:

- `US House District Party Probability`
- `US House District Vote Projection`
- `US Senate Race Party Probability`
- `US Senate Race Vote Projection`
