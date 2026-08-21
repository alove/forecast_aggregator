# f_collector v1.6.2 build report

Built from the user-supplied current `f_collector` repository on 2026-08-20.

## Race to the WH repair

- Preserves the existing national and state/district CSV layouts and export schema 2.1.0.
- Keeps the static Infogram parser as the first and fastest path.
- Adds the publisher's public regional House-map project as a supplemental source when the main House project does not expose all 435 districts.
- Adds a Playwright/Chrome network fallback for Infogram projects whose current data are delivered only after JavaScript runs. The fallback captures public document, XHR, and fetch responses; it does not log in or bypass access controls.
- Adds semantic parsing for generic JSON row/record feeds in addition to existing modern and legacy Infogram chart layouts.
- Retains partial-section behavior and never derives or fabricates race probabilities from missing data.
- Records browser-fallback use, regional-map provenance, and capture warnings in source diagnostics.

## Installation behavior

- Forces Python, Playwright's Node driver, and Chrome artifacts into a private writable cache directory instead of trusting macOS `/var/folders/.../T`. The browser helper restores the caller's environment after each capture.
- Installs Playwright's Python driver into every existing project virtual environment and reuses an installed Chrome/Chromium executable.
- Runs the complete tests and shell syntax checks before committing.
- Commits and pushes the code update so `sync_forecast_database.sh` sees a clean Git worktree.
- A live publisher check is informative and does not roll back code that has passed the deterministic suite.

## Verification

- 55 deterministic unit/integration tests pass; one local browser-network test is skipped only where the execution environment administratively blocks all browser networking. The added regression test starts with `TMPDIR`, `TMP`, and `TEMP` pointing at a non-directory and verifies that Playwright receives a writable replacement.
- Static shell plus browser-delivered 435 House and 35 Senate rows produces a complete 471-row normalized source snapshot in fixtures.
- Generic live JSON record feeds, regional fallback behavior, append-only exports, Git synchronization, ECS staging, and schema validation remain covered.
- Python compilation and Bash syntax validation pass.
