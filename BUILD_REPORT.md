# Build report — f_collector 1.1.1

## Change
Added `collect --output-dir PATH`.

When supplied, the collector writes these defaults beneath that directory:

- `election_forecasts_2026_national.csv`
- `election_forecasts_2026_state.csv`
- `raw_snapshots/` when `--save-raw` is enabled

`--national-output`, `--state-output`, and `--raw-dir` remain available and override the corresponding path derived from `--output-dir`.

If `--output-dir` is omitted, behavior is unchanged: outputs default to the current working directory.

## Verification
- Python compilation: PASS
- Unit tests: 20/20 PASS
- CLI help after clean local venv setup: PASS
