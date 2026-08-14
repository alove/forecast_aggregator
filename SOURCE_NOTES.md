# Source implementation notes — August 14, 2026

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

### Race to the WH / Logan Phillips

Publisher pages:

- `https://www.racetothewh.com/house`
- `https://www.racetothewh.com/senate/26`

The House and Senate pages publish the current forecasts through public Infogram embeds. The adapter first discovers the current forecast embed on each publisher page, distinguishing it from the polling graphics that also appear on those pages. It then parses the static `window.infographicData` payload delivered by the public Infogram embed.

The parser supports both Infogram structures seen in public projects: modern `props.chartData` entities and the legacy `elements[]` chart structure with direct `data` and `sheetnames` fields. Tables are selected semantically rather than by fragile element IDs. National House metrics, national Senate metrics, House district forecasts, and Senate race forecasts are parsed as independent sections. If one section changes or disappears, readable sections are retained and the source is reported as `[PARTIAL]` with explicit coverage diagnostics.

A House expected-seat projection is derived from district probabilities only when all 435 districts are readable; it is never inferred from a partial race table. The published House popular-vote figure is labeled as Race to the WH's adjusted two-party projection because the provider's methodology imputes uncontested districts.

The current public embed IDs are retained as fallbacks, but the normal path discovers the embeds from the publisher pages on every run. Raw mode saves both landing pages, both exact Infogram responses, and an extracted-table diagnostic file. A source fails completely only when no usable forecast metric or race section can be identified; incomplete sections no longer discard valid sections.

## Deliberately not enabled yet

- **VoteHub, FiftyPlusOne, Silver Bulletin/FLIPR, The Economist, and other rendered/subscriber models:** no approved stable machine endpoint is enabled here. This package contains no login automation, paywall bypass, CAPTCHA handling, or access-control circumvention.

This file records implementation scope, not a legal opinion. The collector should be deployed only for sources and uses for which the operator has the necessary rights or permission.
