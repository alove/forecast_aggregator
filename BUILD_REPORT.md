# f_collector v1.5.0 build report

Built from the user-supplied current `f_collector` repository on 2026-08-14.

## Changes

- Fixed Grant Williams v5 parsing. The adapter now prefers `national_model.election_day.mean`, then the current summary field `election_day_national_margin`, while retaining legacy fallbacks. It no longer eagerly evaluates the obsolete `summary.national_environment` key.
- Made Race to the WH section-tolerant. National House/Senate metrics, House districts, and Senate races are parsed independently. Missing/incomplete race tables now produce a `[PARTIAL]` result with coverage diagnostics when other forecast sections remain usable.
- Added `model_web_url` to every national and state/district export row. `source_url` remains the raw/machine-readable provenance URL; `model_web_url` points to the publisher's human-facing live forecast.
- Bumped export/PostgreSQL schema from 2.0.0 to 2.1.0.
- Added `migrate_model_web_urls.py`, an idempotent one-time history migration that adds the new column to existing Git-tracked CSV history without changing forecast values or row identities.
- Updated PostgreSQL preparation/schema/views to retain `model_web_url`.
- Aligned package, collector, and deployment pipeline versions at 1.5.0.

## Published model URL mapping

- Election StatSheet: House / Senate / Districts pages by metric.
- ElectIndex: `https://electindex.com/forecasts/`
- Grant Williams: `https://grantbw4.github.io/2026-midterms-forecast/`
- Race to the WH: House or Senate forecast page by metric.

## Verification

- 51/51 unit/integration tests pass.
- Python compilation passes.
- Bash syntax validation passes for the collector/deployment scripts.
- Migrated current history validates at 96 national rows and 16,920 state/district rows.
- PostgreSQL input validation passes for the migrated current history.
- Migration is tested for idempotence.
- Race to the WH fixture proves national/Senate data survive a missing House-district table.
- Grant Williams fixture reproduces the current v5 summary shape that caused the reported `national_environment` KeyError.
