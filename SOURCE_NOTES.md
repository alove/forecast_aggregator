# Source implementation notes — August 21, 2026

## Enabled

### Election StatSheet / Mac Tan

Public repository: `https://github.com/thisismactan/US-2026`

Raw files:

- `output/house_forecast_timeline.csv`
- `output/house_district_forecast_timeline.csv`
- `output/senate_forecast_timeline.csv`
- `output/senate_state_forecast_timeline.csv`

The adapter uses the latest date present in all four files. The optional backfill mode processes every date common to all four. The public timelines can contain exact duplicate records; the adapter collapses only fully identical duplicate dictionaries and rejects conflicting values for the same party or race key. The repository is published under the MIT License; retain source attribution when republishing derived data.

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

The House and Senate pages publish the current forecasts through public Infogram embeds. The adapter discovers the current forecast projects on those publisher pages, distinguishing them from polling graphics. It also discovers the public regional House-map companion when the main House project does not expose all district rows.

The parser first reads the static `window.infographicData` payload. It supports modern `props.chartData`, legacy `elements[]` charts, and generic row/record JSON feeds. When a public table arrives only after JavaScript runs, the adapter can reuse an existing Chrome/Chromium browser through Playwright and observe public document/XHR/fetch responses. Those responses pass through the same semantic parsing, geography validation, exact-count checks, and schema validation as static tables. The browser fallback does not log in, submit forms, defeat a challenge, or call a private Infogram API.

Before Playwright starts its Node driver, the adapter temporarily redirects `TMPDIR`, `TMP`, `TEMP`, and Python's tempfile root to a verified writable private cache directory. This avoids macOS accounts whose inherited `/var/folders` temp path rejects `mkdtemp`; the original environment is restored after capture.

National House metrics, verified national Senate toplines, House district forecasts, and Senate race forecasts are parsed independently. If one section changes or disappears, readable sections are retained and the source is reported as `[PARTIAL]` with explicit coverage diagnostics. A House expected-seat projection is derived from district probabilities only when all 435 districts are readable; it is never inferred from a partial race table. National Senate seats and control probabilities are not built from race rows, free text, chart distributions, or values drawn from different tables. Each is emitted only when one compact table explicitly identifies the Senate and requested metric, supplies both D and R, and passes metric-specific plausibility checks. Otherwise the normalized fields are blank and the long-form export contains no rows for that metric on that pull.

Published national House seat values can be displayed to one decimal place and produce a tiny D-plus-R overshoot such as 435.1. The adapter treats an overshoot of at most 0.2 as display rounding only when Other is missing or zero, scales D and R proportionally to 435, and records the six-decimal normalized values. Larger overshoots or a conflicting positive Other value remain hard failures. The Senate verifier is stricter: projected full-chamber seats must be plausible for both major caucuses and leave at most ten seats outside them; control probabilities must directly provide complementary D/R values with no manufactured residual `Other` probability.

Race to the WH does not currently expose a source field that the collector can safely identify as the model snapshot date. Narrative dates, chart annotations, polling dates, and page dates are therefore not promoted into `vendor_forecast_date`; the field is blank in CSV and SQL `NULL` after loading. An explicit, unambiguous machine-readable model timestamp may still populate `vendor_updated_at_utc`. The v1.7.3 installer blanks previously stored Race to the WH `vendor_forecast_date` values, including the incorrectly inferred `2026-04-22`. It also removes earlier RTWH `US Senate Seats by Party` and `US Senate Party Probability` rows that lack the new `rtwh_senate_seats=verified` or `rtwh_senate_control=verified` provenance marker; all other RTWH rows and fields are preserved. Canonical metadata repair preserves the file's existing LF or CRLF record endings; installer validation treats consistent CRLF as a legitimate CSV convention while still rejecting actual trailing spaces, mixed endings, and malformed carriage returns.

## Canonical source-date handling

All adapters now normalize source-supplied dates before rows reach the shared schema. Exact ISO `YYYY-MM-DD` dates are retained. Unambiguous U.S. numeric dates such as `8/12/26` and `8/12/2026` are converted to `2026-08-12`. An optional `vendor_forecast_date` that cannot be trusted is exported as blank/SQL `NULL`; it is never guessed from prose. A required `election_date` that cannot be normalized is a hard source-format or repair failure. The shared normalized-row validator rejects any remaining nonblank, non-ISO date, and installation runs both the export validator and the PostgreSQL input validator against the repaired canonical files.

### Kalshi

Public publisher/API surfaces:

- `https://kalshi.com/category/elections/midterms`
- `https://kalshi.com/category/elections/midterms/house`
- `https://external-api.kalshi.com/trade-api/v2`

Kalshi is an exchange, not a statistical forecast model. The adapter labels its values as live market-implied probabilities and uses only public, unauthenticated Trade API v2 market data. It fetches events with nested markets and uses cursor pagination for the House event series.

The national mapping is explicit and limited to the 2026 congressional contracts:

- House control: `CONTROLH-2026`
- Senate control: `CONTROLS-2026`
- Democratic House seats: `KXDHOUSESEATS-27`
- Republican House seats: `KXRHOUSESEATS-27`
- Democratic Senate seats: `KXDSENATESEATS-27`
- Republican Senate seats: `RSENATESEATS-27`
- House popular-vote margin: `KXHOUSEPOPVOTEMARGIN-27NOV03`

Individual House markets are read from the paginated `KXHOUSERACE` series. Because Kalshi currently exposes both `KXHOUSERACE-XXDD-26` and older `HOUSEXXDD-26` event families, the unified series is preferred and direct legacy event lookups fill only missing districts. At-large seats normalize to district `01`. Primaries and nomination markets are rejected. Individual Senate markets are fetched from the 33 regular-cycle state events plus the Florida and Ohio special-election events.

For an individual binary market, the adapter prefers a valid two-sided YES-equivalent midpoint, using NO quotes converted to their YES equivalents when appropriate. It then falls back to the last traded YES price, followed by a one-sided quote. Kalshi's empty-book `0.0000`/`1.0000` bounds are not treated as a false 50% midpoint. Candidate contracts inside a mutually exclusive event are classified and summed by party, then normalized to 100%; an unknown priced outcome rejects the event instead of being silently assigned.

National seat totals and the House popular-vote projection are derived only from Kalshi's mutually exclusive national outcome ladders. Bucket probabilities are normalized, finite buckets use their midpoint, and an open tail uses one adjacent bucket width. Democratic and Republican expected-seat ladders must be mutually consistent before they are reconciled to 435 House seats or 100 Senate seats. The House vote-margin ladder becomes an expected signed D-minus-R margin and corresponding two-party shares. The adapter never sums an incomplete collection of district or Senate-race probabilities to manufacture a chamber total.

Kalshi provides live prices rather than a source-supplied forecast date, so `vendor_forecast_date` and `vendor_updated_at_utc` remain blank. `rhubarb_pull_time` is the observation time. A result is `published` only when all required national metrics, all 435 House districts, and all 35 Senate races requested by the run are readable; otherwise valid available rows are retained as `published_partial` with exact coverage diagnostics.

## Deliberately not enabled yet

- **VoteHub, FiftyPlusOne, Silver Bulletin/FLIPR, The Economist, and other rendered/subscriber models:** no approved stable machine endpoint is enabled here. This package contains no login automation, paywall bypass, CAPTCHA handling, or access-control circumvention.

This file records implementation scope, not a legal opinion. The collector should be deployed only for sources and uses for which the operator has the necessary rights or permission.
