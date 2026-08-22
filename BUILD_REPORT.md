# f_collector v1.7.3 build report

Built from the user-supplied `f_collector` repository plus the v1.7.2 Race to the WH/Kalshi/date update on 2026-08-22.

## Exact installer failure repaired

The v1.7.2 installer completed all 109 tests, normalized/export CSV validation, and the production PostgreSQL input validation. It then stopped at Git's whitespace gate with output such as:

```text
collected_data/election_forecasts_2026_national.csv:1: trailing whitespace.
...^M
```

The canonical CSV was using valid CRLF record endings. `git diff --check` treats the carriage return on every changed CRLF record as trailing whitespace, even though it is part of the file's established line-ending convention. Because the installer runs with `set -Eeuo pipefail`, that nonzero result triggered its pre-commit rollback.

v1.7.3 corrects the gate without weakening source-code checks:

- Managed Python, shell, test, and documentation files are staged and checked strictly with `git diff --cached --check`.
- Canonical CSVs are checked by a dedicated byte-level validator that permits consistently used LF or CRLF.
- Mixed line endings, bare carriage returns, missing final newlines, and spaces/tabs before either line ending remain hard failures.
- The canonical metadata repair continues to preserve each input file's existing LF/CRLF convention, preventing a whole-file line-ending rewrite.
- A failed pre-commit installation now clearly reports that the repository was restored to its original Git commit.

## Forecast behavior retained

- Untrusted Race to the WH model dates are stored as blank/SQL `NULL`.
- Unverified RTWH national Senate seat/control rows are removed and future rows require metric-specific same-table verification.
- Unambiguous short dates such as `8/12/26` normalize to ISO `2026-08-12`; unknown optional dates become `NULL`.
- Kalshi remains enabled for national House/Senate control, seat ladders, House popular-vote markets, House districts, and Senate races.
- Missing publisher or market coverage remains missing; national totals are never manufactured from incomplete race coverage.

## Verification

- 112 deterministic project tests pass, including three new CRLF/CSV whitespace regressions.
- The exact v1.7.2 failure was reproduced against a clean synthetic Git repository whose canonical CSVs use CRLF.
- The v1.7.3 installer passed the complete project suite, export validation, production PostgreSQL validation, strict staged-code whitespace checks, CRLF-aware canonical CSV checks, commit/push synchronization, and clean-worktree verification.
- A second installation was idempotent and created no additional data repair or Git commit.
- The installer also passed with `TMPDIR`, `TMP`, and `TEMP` deliberately pointed at an invalid path.
