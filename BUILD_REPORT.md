# f_collector v1.6.3 build report

Built from the user-supplied `f_collector` repository plus the installed v1.6.2 Race to the WH browser/temp repair on 2026-08-21.

## Race to the WH seat-total repair

- Preserves the existing national and state/district CSV layouts and export schema 2.1.0.
- Keeps the common schema requirement that House seats total 435 and Senate seats total 100.
- Repairs the Race to the WH adapter's handling of tiny publisher display-rounding overshoots. When displayed D and R values alone exceed a chamber total by no more than 0.2 and Other is absent or zero, the adapter scales D and R proportionally to the exact chamber total before export.
- Preserves exact published D/R values when their residual is non-negative and assigns that residual to Other.
- Rejects overshoots larger than 0.2 rather than normalizing potentially misparsed data.
- Rejects a positive published Other value that conflicts with D and R already exceeding the chamber total.
- Applies a final six-decimal correction so the normalized tuple sums to the requested total at export precision.

## Existing v1.6.2 behavior retained

- Static Infogram parsing remains the first path.
- The public regional House-map project remains the supplemental source when the main project lacks all 435 districts.
- The Playwright/Chrome public-network fallback remains available for dynamically delivered Infogram tables.
- Python, Playwright, and Chrome artifacts continue to use the private writable cache directory rather than a broken inherited macOS temp path.
- Partial-section diagnostics, provenance, append-only exports, Git safety, and database deployment behavior are unchanged.

## Verification

- 63 deterministic unit/integration tests run successfully; one browser-network integration test is skipped only because this build environment administratively blocks browser networking.
- Eight focused rounding tests cover exact totals, a 0.1-seat shortfall, the observed 435.1-seat overshoot, explicit zero Other, control-probability rounding, conflicting positive Other, and rejection of a one-seat overshoot.
- A live-shaped national fixture containing House projections of 218.6 D and 216.5 R normalizes to 218.549759 D, 216.450241 R, and 0 Other, then passes the unchanged common schema validator.
- Existing complete 435-House/35-Senate fixtures, browser fallback, regional fallback, append-only storage, Git synchronization, ECS staging, and schema validation remain covered.
- Python compilation and Bash syntax validation pass.
