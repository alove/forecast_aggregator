from __future__ import annotations

"""Narrow, auditable repairs for known-bad canonical forecast metadata."""

from dataclasses import dataclass
import argparse
import csv
import os
from pathlib import Path
from typing import Iterable

from .date_utils import canonical_date_or_blank


RTWH_VENDOR = "Race to the WH"
FORECAST_DATE_FIELD = "vendor_forecast_date"
ELECTION_DATE_FIELD = "election_date"
VENDOR_FIELD = "vendor"
METRIC_TYPE_FIELD = "metric_type"
NOTES_FIELD = "notes"
GEOGRAPHY_TYPE_FIELD = "geography_type"

# The pre-v1.7.1 RTWH parser could mistake unrelated Infogram chart fragments
# for national Senate toplines. New verified rows carry the corresponding
# marker in ``notes``; old rows without that evidence are removed rather than
# retained as fabricated forecasts or rewritten to zero/NULL values.
RTWH_SENATE_VERIFICATION_MARKERS = {
    "US Senate Seats by Party": "rtwh_senate_seats=verified",
    "US Senate Party Probability": "rtwh_senate_control=verified",
}


@dataclass(frozen=True)
class RepairSummary:
    path: Path
    row_count: int
    date_nulled_count: int
    removed_row_count: int
    date_normalized_count: int = 0
    election_date_normalized_count: int = 0

    @property
    def changed_count(self) -> int:
        return (
            self.date_nulled_count
            + self.removed_row_count
            + self.date_normalized_count
            + self.election_date_normalized_count
        )


def _is_unverified_rtwh_senate_national_row(row: dict[str, str]) -> bool:
    if row.get(VENDOR_FIELD, "").strip() != RTWH_VENDOR:
        return False
    geography_type = row.get(GEOGRAPHY_TYPE_FIELD, "").strip()
    if geography_type and geography_type != "national":
        return False
    metric_type = row.get(METRIC_TYPE_FIELD, "").strip()
    marker = RTWH_SENATE_VERIFICATION_MARKERS.get(metric_type)
    if not marker:
        return False
    return marker not in row.get(NOTES_FIELD, "")


def repair_rtwh_canonical_data(path: Path) -> RepairSummary:
    """Repair known-untrusted metadata and legacy date representations.

    The permitted corrections are intentionally narrow and auditable:

    1. Blank every RTWH ``vendor_forecast_date`` because the publisher pages do
       not currently expose a trustworthy snapshot date.
    2. Remove pre-v1.7.1 RTWH national Senate seat/control rows lacking the new
       metric-specific verification marker.
    3. Convert unambiguous legacy U.S. dates such as ``8/12/26`` to ISO
       ``2026-08-12`` for other vendors. Unknown optional forecast dates become
       blank/SQL NULL rather than being guessed.
    4. Canonicalize the required election date when it uses that same
       unambiguous U.S. representation. An unrecognizable election date fails
       loudly because it is required and must never be silently invented.

    All other fields, row order, headers, and formatting are preserved. The
    repair is atomic and idempotent.
    """

    path = Path(path)
    if not path.exists():
        return RepairSummary(
            path=path,
            row_count=0,
            date_nulled_count=0,
            removed_row_count=0,
        )

    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    line_ending = "\r\n" if b"\r\n" in raw else "\n"
    encoding = "utf-8-sig" if had_bom else "utf-8"

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [
            field
            for field in (VENDOR_FIELD, FORECAST_DATE_FIELD)
            if field not in fieldnames
        ]
        if missing:
            raise ValueError(
                f"{path} is missing required field(s): {', '.join(missing)}"
            )
        rows = list(reader)

    retained: list[dict[str, str]] = []
    date_nulled = 0
    date_normalized = 0
    election_date_normalized = 0
    removed = 0

    for row_number, row in enumerate(rows, start=2):
        if _is_unverified_rtwh_senate_national_row(row):
            removed += 1
            continue

        raw_forecast_date = row.get(FORECAST_DATE_FIELD, "").strip()
        if row.get(VENDOR_FIELD, "").strip() == RTWH_VENDOR:
            if raw_forecast_date:
                row[FORECAST_DATE_FIELD] = ""
                date_nulled += 1
        elif raw_forecast_date:
            normalized = canonical_date_or_blank(raw_forecast_date)
            if not normalized:
                # Optional and untrusted: preserve the forecast values, but do
                # not attach a date that we cannot defend.
                row[FORECAST_DATE_FIELD] = ""
                date_nulled += 1
            elif normalized != raw_forecast_date:
                row[FORECAST_DATE_FIELD] = normalized
                date_normalized += 1

        if ELECTION_DATE_FIELD in fieldnames:
            raw_election_date = row.get(ELECTION_DATE_FIELD, "").strip()
            normalized_election_date = canonical_date_or_blank(raw_election_date)
            if not normalized_election_date:
                raise ValueError(
                    f"{path}: row {row_number} has an invalid required "
                    f"election_date: {raw_election_date!r}"
                )
            if normalized_election_date != raw_election_date:
                row[ELECTION_DATE_FIELD] = normalized_election_date
                election_date_normalized += 1

        retained.append(row)

    if date_nulled or date_normalized or election_date_normalized or removed:
        temp = path.with_name(f".{path.name}.canonical-repair.{os.getpid()}")
        try:
            with temp.open("w", encoding=encoding, newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    extrasaction="raise",
                    # Canonical exports use QUOTE_ALL. Reusing the dialect
                    # prevents a broad formatting-only history diff.
                    quoting=csv.QUOTE_ALL,
                    lineterminator=line_ending,
                )
                writer.writeheader()
                writer.writerows(retained)
            os.chmod(temp, path.stat().st_mode)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    return RepairSummary(
        path=path,
        row_count=len(rows),
        date_nulled_count=date_nulled,
        removed_row_count=removed,
        date_normalized_count=date_normalized,
        election_date_normalized_count=election_date_normalized,
    )


def validate_canonical_csv_text_format(path: Path) -> None:
    """Validate canonical CSV text without misclassifying CRLF as whitespace.

    Git's ``diff --check`` treats the carriage return in a changed CRLF line as
    trailing whitespace. The canonical forecast files may legitimately use
    either LF or CRLF, so installation validates them directly instead. Mixed
    endings, bare carriage returns, missing final newlines, and spaces or tabs
    immediately before a line ending remain hard failures.
    """

    path = Path(path)
    raw = path.read_bytes()
    if not raw:
        raise ValueError(f"{path} is empty")

    has_crlf = b"\r\n" in raw
    if has_crlf:
        remainder = raw.replace(b"\r\n", b"")
        if b"\r" in remainder or b"\n" in remainder:
            raise ValueError(f"{path} has mixed or malformed line endings")
        expected_ending = b"\r\n"
    else:
        if b"\r" in raw:
            raise ValueError(f"{path} contains a bare carriage return")
        expected_ending = b"\n"

    if not raw.endswith(expected_ending):
        raise ValueError(f"{path} is missing its final line ending")

    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if line.endswith(b"\r\n"):
            body = line[:-2]
        elif line.endswith(b"\n"):
            body = line[:-1]
        else:
            raise ValueError(f"{path}: line {line_number} has no line ending")
        if body.endswith((b" ", b"\t")):
            raise ValueError(
                f"{path}: line {line_number} has trailing spaces or tabs"
            )


def null_untrusted_rtwh_forecast_dates(path: Path) -> RepairSummary:
    """Backward-compatible entry point for the expanded canonical repair."""

    return repair_rtwh_canonical_data(path)


def repair_paths(paths: Iterable[Path]) -> list[RepairSummary]:
    return [repair_rtwh_canonical_data(Path(path)) for path in paths]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Null untrusted forecast dates, canonicalize unambiguous legacy "
            "dates, and remove unverified RTWH national Senate toplines."
        )
    )
    parser.add_argument("csv_paths", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for summary in repair_paths(args.csv_paths):
        print(
            f"{summary.path}: normalized {summary.date_normalized_count} legacy "
            f"forecast date value(s); nulled {summary.date_nulled_count} "
            f"untrusted forecast date value(s); normalized "
            f"{summary.election_date_normalized_count} election date value(s); "
            f"removed {summary.removed_row_count} unverified RTWH national "
            f"Senate row(s) across {summary.row_count} original row(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
