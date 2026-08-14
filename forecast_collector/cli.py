from __future__ import annotations

import argparse
from collections import Counter
import csv
from pathlib import Path
import sys
import traceback

from .errors import CollectorError, OutputValidationError
from .export import (
    EXPORT_SCHEMA_VERSION,
    NATIONAL_FIELDNAMES,
    STATE_FIELDNAMES,
    split_rows,
    validate_national_rows,
    validate_state_rows,
)
from .http import HttpClient
from .raw import save_raw_artifacts
from .schema import utc_now_iso, validate_rows
from .sources import ALL_SOURCES
from .storage import append_export_rows, read_export_rows


def _default_national_output() -> Path:
    return Path.cwd() / "election_forecasts_2026_national.csv"


def _default_state_output() -> Path:
    return Path.cwd() / "election_forecasts_2026_state.csv"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="election-forecast-collector",
        description="Collect and append normalized public 2026 congressional forecasts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect", help="Fetch, normalize, validate, and append forecasts")
    collect.add_argument(
        "--output-dir", type=Path, default=None,
        help=(
            "Directory for both CSVs and, by default, raw_snapshots/. "
            "Defaults to the current working directory."
        ),
    )
    collect.add_argument(
        "--national-output", type=Path, default=None,
        help=(
            "National/chamber CSV. Overrides --output-dir for this file; "
            "default filename: election_forecasts_2026_national.csv"
        ),
    )
    collect.add_argument(
        "--state-output", type=Path, default=None,
        help=(
            "State/district race CSV. Overrides --output-dir for this file; "
            "default filename: election_forecasts_2026_state.csv"
        ),
    )
    collect.add_argument(
        "--source", action="append", choices=sorted(ALL_SOURCES),
        help="Source slug; repeat to select multiple. Default: every enabled source.",
    )
    collect.add_argument("--skip-house-districts", action="store_true")
    collect.add_argument("--skip-senate-races", action="store_true")
    collect.add_argument(
        "--backfill-election-statsheet", action="store_true",
        help="Append every common historical Election StatSheet date, not just latest.",
    )
    collect.add_argument("--save-raw", action="store_true", help="Save exact downloaded source files")
    collect.add_argument("--raw-dir", type=Path, default=None, help="Default: ./raw_snapshots")
    collect.add_argument("--timeout", type=float, default=30.0)
    collect.add_argument("--retries", type=int, default=3)
    collect.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing")
    collect.add_argument(
        "--allow-partial-success", action="store_true",
        help="Exit zero when at least one source succeeds and another fails.",
    )
    collect.add_argument("--verbose", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate a national or state export CSV")
    validate.add_argument("csv_path", type=Path)

    subparsers.add_parser("sources", help="List enabled source adapters and coverage")
    subparsers.add_parser("schema", help="Print both export CSV schemas")
    return parser


def command_sources() -> int:
    print("Enabled sources:")
    print("  election-statsheet  national House/Senate, House districts, Senate races, historical timeline")
    print("  electindex          national House/Senate/PV, House districts, Senate races, latest CSV snapshot")
    print("  grant-williams      national House/Senate/PV, House districts, Senate races, latest atomic JSON bundle")
    print("  race-to-the-wh      national House/Senate/PV, House districts, Senate races, public Infogram forecast")
    print(
        "\nThe first three adapters use public raw GitHub CSV/JSON endpoints. "
        "Race to the WH discovers the current public House and Senate Infogram embeds "
        "from the publisher pages and parses their embedded static forecast data."
    )
    return 0


def command_schema() -> int:
    print(f"export_schema_version={EXPORT_SCHEMA_VERSION}")
    print("\nNATIONAL CSV")
    for index, field in enumerate(NATIONAL_FIELDNAMES, start=1):
        print(f"{index:02d} {field}")
    print("\nSTATE / DISTRICT CSV")
    for index, field in enumerate(STATE_FIELDNAMES, start=1):
        print(f"{index:02d} {field}")
    return 0


def _read_header(path: Path) -> list[str]:
    with path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        return list(next(csv.reader(handle), []))


def command_validate(path: Path) -> int:
    path = path.expanduser()
    header = _read_header(path)
    if header == NATIONAL_FIELDNAMES:
        rows = read_export_rows(path, fieldnames=NATIONAL_FIELDNAMES)
        count = validate_national_rows(rows)
        kind = "national"
    elif header == STATE_FIELDNAMES:
        rows = read_export_rows(path, fieldnames=STATE_FIELDNAMES)
        count = validate_state_rows(rows)
        kind = "state"
    else:
        raise OutputValidationError("CSV header is neither the national nor state export schema")
    vendors = Counter(row["vendor"] for row in rows)
    runs = {(row["vendor"], row["vendor_run_id"]) for row in rows}
    print(f"PASS: {path.resolve()}")
    print(f"Type: {kind}; rows: {count:,}; vendor runs: {len(runs):,}")
    for vendor, vendor_count in sorted(vendors.items()):
        print(f"  {vendor}: {vendor_count:,} rows")
    return 0


def command_collect(args: argparse.Namespace) -> int:
    output_dir = (args.output_dir or Path.cwd()).expanduser()
    national_output = (
        args.national_output or (output_dir / "election_forecasts_2026_national.csv")
    ).expanduser()
    state_output = (
        args.state_output or (output_dir / "election_forecasts_2026_state.csv")
    ).expanduser()
    raw_dir = (args.raw_dir or (output_dir / "raw_snapshots")).expanduser()
    selected = args.source or sorted(ALL_SOURCES)
    client = HttpClient(timeout=args.timeout, retries=args.retries)
    observed = utc_now_iso()
    all_rows: list[dict] = []
    results = []
    failures: list[tuple[str, BaseException]] = []

    for slug in selected:
        source = ALL_SOURCES[slug]()
        try:
            result = source.collect(
                client,
                observed_datetime_utc=observed,
                include_house_districts=not args.skip_house_districts,
                include_senate_races=not args.skip_senate_races,
                backfill=(args.backfill_election_statsheet and slug == "election-statsheet"),
            )
            validate_rows(result.rows)
            all_rows.extend(result.rows)
            results.append(result)
            forecast_dates = list(result.details.get("forecast_dates", []))
            if not forecast_dates:
                date_summary = "date not supplied"
            elif len(forecast_dates) <= 3:
                date_summary = ", ".join(forecast_dates)
            else:
                date_summary = (
                    f"{forecast_dates[0]} through {forecast_dates[-1]}; "
                    f"{len(forecast_dates):,} dates"
                )
            partial = bool(result.details.get("partial"))
            status = "PARTIAL" if partial else "PASS"
            print(f"[{status}] {result.source_name}: {len(result.rows):,} normalized source rows ({date_summary})")
            if partial:
                sections = list(result.details.get("partial_sections", []))
                if sections:
                    print("          " + "; ".join(str(item) for item in sections))
            collapsed = result.details.get("duplicate_rows_collapsed", {})
            if args.verbose and collapsed:
                print(f"       exact source duplicates collapsed: {collapsed}")
        except BaseException as exc:  # keep independent public sources independent
            failures.append((slug, exc))
            print(f"[FAIL] {slug}: {exc}", file=sys.stderr)
            if args.verbose:
                traceback.print_exc()

    if not all_rows:
        print("No source produced a valid snapshot; CSVs were not modified.", file=sys.stderr)
        return 2

    validate_rows(all_rows)
    national_rows, state_rows = split_rows(all_rows)
    validate_national_rows(national_rows)
    if state_rows:
        validate_state_rows(state_rows)

    print(f"Rhubarb pull time: {observed}")
    print(f"Export rows: {len(national_rows):,} national; {len(state_rows):,} state/district")

    if args.dry_run:
        print("DRY RUN: both export schemas validated; no files written.")
    else:
        n_added, n_skipped = append_export_rows(
            national_output, national_rows, fieldnames=NATIONAL_FIELDNAMES
        )
        s_added, s_skipped = append_export_rows(
            state_output, state_rows, fieldnames=STATE_FIELDNAMES
        ) if state_rows else (0, 0)
        print(f"National CSV: {national_output.resolve()}")
        print(f"  appended: {n_added:,}; already present: {n_skipped:,}")
        print(f"State CSV: {state_output.resolve()}")
        print(f"  appended: {s_added:,}; already present: {s_skipped:,}")
        if args.save_raw:
            raw_count = 0
            for result in results:
                run_ids = result.details.get("run_ids", ["unknown"])
                run_id = str(run_ids[-1]) if run_ids else "unknown"
                paths = save_raw_artifacts(raw_dir, result.source_name, run_id, result.raw_artifacts)
                raw_count += len(paths)
            print(f"Raw snapshots: {raw_count} file(s) under {raw_dir.resolve()}")

    if failures:
        print("Some sources failed; successful sources were still processed.", file=sys.stderr)
        return 0 if args.allow_partial_success else 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sources":
            return command_sources()
        if args.command == "schema":
            return command_schema()
        if args.command == "validate":
            return command_validate(args.csv_path)
        if args.command == "collect":
            return command_collect(args)
        parser.error("unknown command")
    except (CollectorError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2
