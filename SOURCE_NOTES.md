# Source implementation notes — August 12, 2026

## Enabled

### Election StatSheet / Mac Tan

Public repository: `https://github.com/thisismactan/US-2026`

Raw files:

- `output/house_forecast_timeline.csv`
- `output/house_district_forecast_timeline.csv`
- `output/senate_forecast_timeline.csv`
- `output/senate_state_forecast_timeline.csv`

The adapter uses the latest date present in all four files. The optional backfill mode processes every date common to all four. The public timelines currently contain a small number of exact duplicate records on July 24, 2026; the adapter collapses only fully identical duplicate dictionaries and rejects conflicting values for the same party or race key. The repository is published under the MIT License; retain source attribution when republishing derived data.

### Grant Williams

Public repository: `https://github.com/grantbw4/2026-midterms-forecast`

Raw files:

- `outputs/forecast.json`
- `outputs/senate_forecast.json`

The adapter requires matching House and Senate `run_id` and `updated_at` values, so it never combines halves of different forecast runs. It consumes the published forecast bundle only; it does not query or redistribute the underlying polling feeds embedded in the model's provenance.

### ElectIndex

Public repository: `https://github.com/ElectIndex/26_us_forecast_data`

Raw files:

- `output/chambers.csv`
- `output/national_indicators.csv`
- `output/races_summary.csv`

The adapter uses mean chamber seats, the published Democratic control probability and its binary complement, projected all-party House vote counts, and the House/Senate rows from the race summary. Where ElectIndex represents an independent candidate in the Democratic probability field because no Democrat is running, the collector maps that probability to Other and sets Democratic probability to zero.

## Deliberately not enabled yet

- **Race to the WH:** this was the fourth qualifying public model identified in the source review. Its public site exposes the rendered 2026 House and Senate forecasts, including all 435 House races, but a stable public machine-readable CSV/JSON/API endpoint for the complete House + Senate + national House-vote bundle has not yet been verified. It is therefore not enabled rather than relying on an untested/brittle rendered-page parser.
- **VoteHub, FiftyPlusOne, Silver Bulletin/FLIPR, The Economist, and other rendered/subscriber models:** no approved stable machine endpoint is enabled here. This package contains no login automation, paywall bypass, CAPTCHA handling, or access-control circumvention.

This file records implementation scope, not a legal opinion. The collector should be deployed only for sources and uses for which the operator has the necessary rights or permission.
