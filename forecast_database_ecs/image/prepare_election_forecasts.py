#!/usr/bin/env python3
"""Validate and prepare f_collector CSV history for PostgreSQL.

The preparer deliberately uses only the Python standard library. It validates
both collector exports, rewrites them in a stable column order, and emits the
PostgreSQL initialization files used by both the disposable ECS database and
the optional local loader.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

SCHEMA_VERSION = "2.1.0"

NATIONAL_TABLE = "election_forecasts_2026_national"
STATE_TABLE = "election_forecasts_2026_state"
METADATA_TABLE = "election_forecasts_2026_load_metadata"
LATEST_NATIONAL_VIEW = "election_forecasts_2026_latest_national"
LATEST_STATE_VIEW = "election_forecasts_2026_latest_state"
LATEST_RUNS_VIEW = "election_forecasts_2026_latest_vendor_runs"

SHARED_FIELDS = [
    "schema_version",
    "rhubarb_pull_time",
    "observed_datetime_utc",
    "vendor",
    "vendor_model",
    "vendor_run_id",
    "vendor_forecast_date",
    "vendor_updated_at_utc",
    "model_status",
    "election_date",
    "metric_type",
    "party",
    "value",
    "unit",
    "median_value",
    "low_value",
    "high_value",
    "basis",
    "source_record_id",
    "source_url",
    "model_web_url",
    "source_file",
    "data_quality",
    "notes",
]

NATIONAL_FIELDS = SHARED_FIELDS + [
    "geography_type",
    "geography_id",
    "geography_name",
]

STATE_FIELDS = SHARED_FIELDS + [
    "geography_type",
    "geography_id",
    "state_fips",
    "state_abbreviation",
    "state",
    "congressional_district",
    "house_seat_number",
    "house_seat",
    "senate_seat",
    "special_election",
    "rating",
]

NATIONAL_METRICS = {
    "US House Seats by Party": ("seats", {"D", "R", "Other"}),
    "US House Party Probability": ("percent", {"D", "R", "Other"}),
    "US Senate Seats by Party": ("seats", {"D", "R", "Other"}),
    "US Senate Party Probability": ("percent", {"D", "R", "Other"}),
    "US House Popular Vote Projection": ("percent", {"D", "R", "Other"}),
    "US House Popular Vote Margin": ("percentage_points", {"D-R"}),
}

STATE_METRICS = {
    "US House District Party Probability": ("percent", {"D", "R", "Other"}, "house"),
    "US House District Vote Projection": ("percent", {"D", "R", "Other"}, "house"),
    "US Senate Race Party Probability": ("percent", {"D", "R", "Other"}, "senate"),
    "US Senate Race Vote Projection": ("percent", {"D", "R", "Other"}, "senate"),
}

NUMERIC_FIELDS = {"value", "median_value", "low_value", "high_value"}
DATE_FIELDS = {"vendor_forecast_date", "election_date"}
TIMESTAMP_FIELDS = {
    "rhubarb_pull_time",
    "observed_datetime_utc",
    "vendor_updated_at_utc",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and prepare f_collector national/state CSVs for PostgreSQL."
    )
    parser.add_argument("--national-input", type=Path, required=True)
    parser.add_argument("--state-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--copy-root",
        type=Path,
        default=Path("/docker-entrypoint-initdb.d"),
        help="Path PostgreSQL's psql client should use for prepared CSVs.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate inputs and print a summary without writing prepared files.",
    )
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def parse_iso_date(value: str, *, field: str, row_number: int, required: bool = False) -> None:
    if not value:
        if required:
            raise ValueError(f"row {row_number}: {field} is required")
        return
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid {field}: {value!r}") from exc


def parse_iso_timestamp(
    value: str,
    *,
    field: str,
    row_number: int,
    required: bool = False,
    utc_second_precision: bool = False,
) -> datetime | None:
    if not value:
        if required:
            raise ValueError(f"row {row_number}: {field} is required")
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"row {row_number}: {field} must include a timezone: {value!r}")
    if utc_second_precision:
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError(f"row {row_number}: {field} must be UTC: {value!r}")
        if parsed.microsecond != 0:
            raise ValueError(f"row {row_number}: {field} must be truncated to the second")
    return parsed


def parse_number(
    value: str,
    *,
    field: str,
    row_number: int,
    required: bool = False,
) -> float | None:
    if value == "":
        if required:
            raise ValueError(f"row {row_number}: {field} is required")
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid numeric {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"row {row_number}: {field} must be finite")
    return number


def normalize_boolean(value: str, *, row_number: int) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    if lowered in {"true", "t", "1", "yes", "y"}:
        return "true"
    if lowered in {"false", "f", "0", "no", "n"}:
        return "false"
    raise ValueError(f"row {row_number}: invalid special_election boolean: {value!r}")


def validate_header(actual: Sequence[str], expected: Sequence[str], *, label: str) -> None:
    if list(actual) != list(expected):
        missing = [field for field in expected if field not in actual]
        extra = [field for field in actual if field not in expected]
        raise ValueError(
            f"{label} CSV header does not match export schema {SCHEMA_VERSION}; "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )


def require(row: Mapping[str, str], field: str, *, row_number: int) -> str:
    value = row.get(field, "")
    if value == "":
        raise ValueError(f"row {row_number}: {field} is required")
    return value


def validate_shared(row: dict[str, str], *, row_number: int) -> tuple[datetime, tuple[str, ...]]:
    if row["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"row {row_number}: unsupported schema_version {row['schema_version']!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )

    pull_time = parse_iso_timestamp(
        require(row, "rhubarb_pull_time", row_number=row_number),
        field="rhubarb_pull_time",
        row_number=row_number,
        required=True,
        utc_second_precision=True,
    )
    observed = parse_iso_timestamp(
        require(row, "observed_datetime_utc", row_number=row_number),
        field="observed_datetime_utc",
        row_number=row_number,
        required=True,
        utc_second_precision=True,
    )
    assert pull_time is not None and observed is not None
    if pull_time != observed:
        raise ValueError(
            f"row {row_number}: observed_datetime_utc must equal rhubarb_pull_time"
        )

    require(row, "vendor", row_number=row_number)
    require(row, "vendor_run_id", row_number=row_number)
    require(row, "metric_type", row_number=row_number)
    require(row, "party", row_number=row_number)
    require(row, "unit", row_number=row_number)
    require(row, "source_record_id", row_number=row_number)
    require(row, "source_url", row_number=row_number)
    require(row, "model_web_url", row_number=row_number)
    parse_number(row["value"], field="value", row_number=row_number, required=True)

    for field in ("median_value", "low_value", "high_value"):
        parse_number(row[field], field=field, row_number=row_number)

    parse_iso_date(
        row["vendor_forecast_date"],
        field="vendor_forecast_date",
        row_number=row_number,
    )
    parse_iso_date(
        row["election_date"], field="election_date", row_number=row_number, required=True
    )
    parse_iso_timestamp(
        row["vendor_updated_at_utc"],
        field="vendor_updated_at_utc",
        row_number=row_number,
    )

    identity = (
        row["vendor"],
        row["vendor_run_id"],
        row["metric_type"],
        row["source_record_id"],
        row["party"],
    )
    return pull_time, identity


def validate_value_range(row: Mapping[str, str], *, row_number: int) -> None:
    value = parse_number(row["value"], field="value", row_number=row_number, required=True)
    assert value is not None
    unit = row["unit"]
    if unit == "percent" and not 0.0 <= value <= 100.0:
        raise ValueError(f"row {row_number}: percent value outside 0-100: {value}")
    if unit == "percentage_points" and not -100.0 <= value <= 100.0:
        raise ValueError(
            f"row {row_number}: percentage-point value outside -100 to 100: {value}"
        )
    if unit == "seats" and value < 0.0:
        raise ValueError(f"row {row_number}: seat value cannot be negative: {value}")


def validate_national(row: dict[str, str], *, row_number: int) -> None:
    metric = row["metric_type"]
    if metric not in NATIONAL_METRICS:
        raise ValueError(f"row {row_number}: invalid national metric_type: {metric!r}")
    expected_unit, parties = NATIONAL_METRICS[metric]
    if row["unit"] != expected_unit:
        raise ValueError(
            f"row {row_number}: {metric!r} must use unit {expected_unit!r}"
        )
    if row["party"] not in parties:
        raise ValueError(
            f"row {row_number}: invalid party {row['party']!r} for {metric!r}"
        )
    if row["geography_type"] != "national":
        raise ValueError(f"row {row_number}: national geography_type must be 'national'")
    if row["geography_id"] != "US":
        raise ValueError(f"row {row_number}: national geography_id must be 'US'")
    if row["geography_name"] != "United States":
        raise ValueError(
            f"row {row_number}: national geography_name must be 'United States'"
        )
    validate_value_range(row, row_number=row_number)


def validate_state(row: dict[str, str], *, row_number: int) -> None:
    metric = row["metric_type"]
    if metric not in STATE_METRICS:
        raise ValueError(f"row {row_number}: invalid state metric_type: {metric!r}")
    expected_unit, parties, chamber = STATE_METRICS[metric]
    if row["unit"] != expected_unit:
        raise ValueError(
            f"row {row_number}: {metric!r} must use unit {expected_unit!r}"
        )
    if row["party"] not in parties:
        raise ValueError(
            f"row {row_number}: invalid party {row['party']!r} for {metric!r}"
        )

    state_fips = require(row, "state_fips", row_number=row_number)
    if not re.fullmatch(r"\d{2}", state_fips):
        raise ValueError(f"row {row_number}: state_fips must be two digits")
    abbreviation = require(row, "state_abbreviation", row_number=row_number)
    if not re.fullmatch(r"[A-Z]{2}", abbreviation):
        raise ValueError(f"row {row_number}: state_abbreviation must be two uppercase letters")
    require(row, "state", row_number=row_number)
    row["special_election"] = normalize_boolean(
        row["special_election"], row_number=row_number
    )

    if chamber == "house":
        if row["geography_type"] != "congressional_district":
            raise ValueError(
                f"row {row_number}: House metric must use congressional_district geography"
            )
        district = require(row, "congressional_district", row_number=row_number)
        if not re.fullmatch(r"\d{4}", district):
            raise ValueError(
                f"row {row_number}: congressional_district must be four digits (SFCD)"
            )
        if not district.startswith(state_fips):
            raise ValueError(
                f"row {row_number}: congressional_district must begin with state_fips"
            )
        if row["geography_id"] != district:
            raise ValueError(
                f"row {row_number}: House geography_id must equal congressional_district"
            )
        seat = require(row, "house_seat_number", row_number=row_number)
        try:
            seat_number = int(seat)
        except ValueError as exc:
            raise ValueError(
                f"row {row_number}: house_seat_number must be an integer"
            ) from exc
        if not 1 <= seat_number <= 99:
            raise ValueError(f"row {row_number}: house_seat_number must be 1-99")
        if int(district[2:]) != seat_number:
            raise ValueError(
                f"row {row_number}: SFCD district suffix must match house_seat_number"
            )
        require(row, "house_seat", row_number=row_number)
        if row["senate_seat"]:
            raise ValueError(f"row {row_number}: House row must not contain senate_seat")
    else:
        if row["geography_type"] != "state":
            raise ValueError(f"row {row_number}: Senate metric must use state geography")
        if row["geography_id"] != state_fips:
            raise ValueError(f"row {row_number}: Senate geography_id must equal state_fips")
        if row["congressional_district"] or row["house_seat_number"] or row["house_seat"]:
            raise ValueError(f"row {row_number}: Senate row contains House-only fields")
        require(row, "senate_seat", row_number=row_number)

    validate_value_range(row, row_number=row_number)


def read_validated_csv(
    path: Path,
    *,
    expected_fields: Sequence[str],
    label: str,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{label} CSV not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"{label} CSV is empty: {path}")

    rows: list[dict[str, str]] = []
    identities: set[tuple[str, ...]] = set()
    pull_times: list[datetime] = []
    vendors: Counter[str] = Counter()
    run_ids: set[tuple[str, str]] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        validate_header(reader.fieldnames or [], expected_fields, label=label)
        for row_number, source_row in enumerate(reader, start=2):
            if None in source_row:
                raise ValueError(f"row {row_number}: too many CSV fields")
            row = {field: (source_row.get(field) or "").strip() for field in expected_fields}
            if any("\x00" in value for value in row.values()):
                raise ValueError(f"row {row_number}: NUL byte is not allowed")

            pull_time, identity = validate_shared(row, row_number=row_number)
            if label == "national":
                validate_national(row, row_number=row_number)
            else:
                validate_state(row, row_number=row_number)

            if identity in identities:
                raise ValueError(
                    f"row {row_number}: duplicate export identity: "
                    + " | ".join(identity)
                )
            identities.add(identity)
            pull_times.append(pull_time)
            vendors[row["vendor"]] += 1
            run_ids.add((row["vendor"], row["vendor_run_id"]))
            rows.append(row)

    if not rows:
        raise ValueError(f"{label} CSV has a header but no data rows")

    return rows, {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "row_count": len(rows),
        "vendor_count": len(vendors),
        "vendor_run_count": len(run_ids),
        "vendors": dict(sorted(vendors.items())),
        "minimum_pull_time": min(pull_times).isoformat(),
        "maximum_pull_time": max(pull_times).isoformat(),
    }


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fields),
            extrasaction="raise",
            # PostgreSQL CSV COPY treats an unquoted empty field as NULL. This
            # matters for optional DATE, TIMESTAMPTZ, numeric, and BOOLEAN
            # columns; quoting every empty string would make COPY try to cast
            # "" into those types.
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def shared_columns_sql() -> str:
    return """
    schema_version TEXT NOT NULL,
    rhubarb_pull_time TIMESTAMPTZ NOT NULL,
    observed_datetime_utc TIMESTAMPTZ NOT NULL,
    vendor TEXT NOT NULL,
    vendor_model TEXT,
    vendor_run_id TEXT NOT NULL,
    vendor_forecast_date DATE,
    vendor_updated_at_utc TIMESTAMPTZ,
    model_status TEXT,
    election_date DATE NOT NULL,
    metric_type TEXT NOT NULL,
    party TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit TEXT NOT NULL,
    median_value DOUBLE PRECISION,
    low_value DOUBLE PRECISION,
    high_value DOUBLE PRECISION,
    basis TEXT,
    source_record_id TEXT NOT NULL,
    source_url TEXT,
    model_web_url TEXT NOT NULL,
    source_file TEXT,
    data_quality TEXT,
    notes TEXT""".strip()


def build_schema_sql() -> str:
    return f"""\\set ON_ERROR_STOP on
BEGIN;

DROP VIEW IF EXISTS public.{quote_ident(LATEST_RUNS_VIEW)};
DROP VIEW IF EXISTS public.{quote_ident(LATEST_STATE_VIEW)};
DROP VIEW IF EXISTS public.{quote_ident(LATEST_NATIONAL_VIEW)};
DROP TABLE IF EXISTS public.{quote_ident(METADATA_TABLE)} CASCADE;
DROP TABLE IF EXISTS public.{quote_ident(STATE_TABLE)} CASCADE;
DROP TABLE IF EXISTS public.{quote_ident(NATIONAL_TABLE)} CASCADE;

CREATE TABLE public.{quote_ident(NATIONAL_TABLE)} (
{shared_columns_sql()},
    geography_type TEXT NOT NULL,
    geography_id TEXT NOT NULL,
    geography_name TEXT NOT NULL,
    CONSTRAINT {quote_ident(NATIONAL_TABLE + '_schema_ck')} CHECK (schema_version = {sql_literal(SCHEMA_VERSION)}),
    CONSTRAINT {quote_ident(NATIONAL_TABLE + '_geography_ck')} CHECK (
        geography_type = 'national' AND geography_id = 'US' AND geography_name = 'United States'
    ),
    CONSTRAINT {quote_ident(NATIONAL_TABLE + '_unit_ck')} CHECK (unit IN ('percent', 'seats', 'percentage_points'))
);

CREATE TABLE public.{quote_ident(STATE_TABLE)} (
{shared_columns_sql()},
    geography_type TEXT NOT NULL,
    geography_id TEXT NOT NULL,
    state_fips TEXT NOT NULL,
    state_abbreviation TEXT NOT NULL,
    state TEXT NOT NULL,
    congressional_district TEXT,
    house_seat_number SMALLINT,
    house_seat TEXT,
    senate_seat TEXT,
    special_election BOOLEAN,
    rating TEXT,
    CONSTRAINT {quote_ident(STATE_TABLE + '_schema_ck')} CHECK (schema_version = {sql_literal(SCHEMA_VERSION)}),
    CONSTRAINT {quote_ident(STATE_TABLE + '_geography_ck')} CHECK (geography_type IN ('state', 'congressional_district')),
    CONSTRAINT {quote_ident(STATE_TABLE + '_unit_ck')} CHECK (unit = 'percent'),
    CONSTRAINT {quote_ident(STATE_TABLE + '_fips_ck')} CHECK (state_fips ~ '^[0-9]{{2}}$'),
    CONSTRAINT {quote_ident(STATE_TABLE + '_district_ck')} CHECK (
        congressional_district IS NULL OR congressional_district ~ '^[0-9]{{4}}$'
    )
);

CREATE TABLE public.{quote_ident(METADATA_TABLE)} (
    loaded_at_utc TIMESTAMPTZ NOT NULL,
    export_schema_version TEXT NOT NULL,
    national_sha256 TEXT NOT NULL,
    state_sha256 TEXT NOT NULL,
    national_size_bytes BIGINT NOT NULL,
    state_size_bytes BIGINT NOT NULL,
    national_row_count BIGINT NOT NULL,
    state_row_count BIGINT NOT NULL,
    national_vendor_count INTEGER NOT NULL,
    state_vendor_count INTEGER NOT NULL,
    national_vendor_run_count INTEGER NOT NULL,
    state_vendor_run_count INTEGER NOT NULL,
    minimum_rhubarb_pull_time TIMESTAMPTZ NOT NULL,
    maximum_rhubarb_pull_time TIMESTAMPTZ NOT NULL,
    national_source_path TEXT NOT NULL,
    state_source_path TEXT NOT NULL,
    loader_version TEXT NOT NULL
);

COMMIT;
"""


def build_copy_sql(copy_root: Path) -> str:
    national_path = (copy_root / f"{NATIONAL_TABLE}.csv").as_posix()
    state_path = (copy_root / f"{STATE_TABLE}.csv").as_posix()
    national_columns = ", ".join(quote_ident(field) for field in NATIONAL_FIELDS)
    state_columns = ", ".join(quote_ident(field) for field in STATE_FIELDS)
    return f"""\\set ON_ERROR_STOP on
\\copy public.{quote_ident(NATIONAL_TABLE)} ({national_columns}) FROM {sql_literal(national_path)} WITH (FORMAT CSV, HEADER TRUE, NULL '', ENCODING 'UTF8');
\\copy public.{quote_ident(STATE_TABLE)} ({state_columns}) FROM {sql_literal(state_path)} WITH (FORMAT CSV, HEADER TRUE, NULL '', ENCODING 'UTF8');
"""


def build_post_load_sql(
    *, national_meta: Mapping[str, object], state_meta: Mapping[str, object]
) -> str:
    loaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    minimum_pull = min(
        str(national_meta["minimum_pull_time"]), str(state_meta["minimum_pull_time"])
    )
    maximum_pull = max(
        str(national_meta["maximum_pull_time"]), str(state_meta["maximum_pull_time"])
    )

    return f"""\\set ON_ERROR_STOP on
BEGIN;

CREATE UNIQUE INDEX {quote_ident(NATIONAL_TABLE + '_identity_uidx')}
ON public.{quote_ident(NATIONAL_TABLE)}
    (vendor, vendor_run_id, metric_type, source_record_id, party);

CREATE INDEX {quote_ident(NATIONAL_TABLE + '_metric_idx')}
ON public.{quote_ident(NATIONAL_TABLE)}
    (metric_type, party, vendor_forecast_date DESC, rhubarb_pull_time DESC);

CREATE INDEX {quote_ident(NATIONAL_TABLE + '_vendor_idx')}
ON public.{quote_ident(NATIONAL_TABLE)}
    (vendor, vendor_run_id);

CREATE UNIQUE INDEX {quote_ident(STATE_TABLE + '_identity_uidx')}
ON public.{quote_ident(STATE_TABLE)}
    (vendor, vendor_run_id, metric_type, source_record_id, party);

CREATE INDEX {quote_ident(STATE_TABLE + '_race_idx')}
ON public.{quote_ident(STATE_TABLE)}
    (metric_type, geography_id, party, vendor_forecast_date DESC, rhubarb_pull_time DESC);

CREATE INDEX {quote_ident(STATE_TABLE + '_state_idx')}
ON public.{quote_ident(STATE_TABLE)}
    (state_abbreviation, congressional_district, senate_seat);

CREATE INDEX {quote_ident(STATE_TABLE + '_vendor_idx')}
ON public.{quote_ident(STATE_TABLE)}
    (vendor, vendor_run_id);

CREATE VIEW public.{quote_ident(LATEST_NATIONAL_VIEW)} AS
SELECT DISTINCT ON (vendor, metric_type, source_record_id, party)
    n.*
FROM public.{quote_ident(NATIONAL_TABLE)} AS n
ORDER BY
    vendor,
    metric_type,
    source_record_id,
    party,
    vendor_forecast_date DESC NULLS LAST,
    vendor_updated_at_utc DESC NULLS LAST,
    rhubarb_pull_time DESC,
    vendor_run_id DESC;

CREATE VIEW public.{quote_ident(LATEST_STATE_VIEW)} AS
SELECT DISTINCT ON (vendor, metric_type, source_record_id, party)
    s.*
FROM public.{quote_ident(STATE_TABLE)} AS s
ORDER BY
    vendor,
    metric_type,
    source_record_id,
    party,
    vendor_forecast_date DESC NULLS LAST,
    vendor_updated_at_utc DESC NULLS LAST,
    rhubarb_pull_time DESC,
    vendor_run_id DESC;

CREATE VIEW public.{quote_ident(LATEST_RUNS_VIEW)} AS
WITH all_runs AS (
    SELECT DISTINCT
           vendor, vendor_model, vendor_run_id, vendor_forecast_date,
           vendor_updated_at_utc, rhubarb_pull_time, source_url, model_web_url,
           'national'::TEXT AS source_table
    FROM public.{quote_ident(NATIONAL_TABLE)}
    UNION
    SELECT DISTINCT
           vendor, vendor_model, vendor_run_id, vendor_forecast_date,
           vendor_updated_at_utc, rhubarb_pull_time, source_url, model_web_url,
           'state'::TEXT AS source_table
    FROM public.{quote_ident(STATE_TABLE)}
)
SELECT DISTINCT ON (vendor, source_table)
       vendor, vendor_model, vendor_run_id, vendor_forecast_date,
       vendor_updated_at_utc, rhubarb_pull_time, source_url, model_web_url, source_table
FROM all_runs
ORDER BY
    vendor,
    source_table,
    vendor_forecast_date DESC NULLS LAST,
    vendor_updated_at_utc DESC NULLS LAST,
    rhubarb_pull_time DESC,
    vendor_run_id DESC,
    source_url DESC NULLS LAST,
    model_web_url DESC NULLS LAST;

INSERT INTO public.{quote_ident(METADATA_TABLE)} (
    loaded_at_utc,
    export_schema_version,
    national_sha256,
    state_sha256,
    national_size_bytes,
    state_size_bytes,
    national_row_count,
    state_row_count,
    national_vendor_count,
    state_vendor_count,
    national_vendor_run_count,
    state_vendor_run_count,
    minimum_rhubarb_pull_time,
    maximum_rhubarb_pull_time,
    national_source_path,
    state_source_path,
    loader_version
) VALUES (
    {sql_literal(loaded_at)}::timestamptz,
    {sql_literal(SCHEMA_VERSION)},
    {sql_literal(str(national_meta['sha256']))},
    {sql_literal(str(state_meta['sha256']))},
    {int(national_meta['size_bytes'])},
    {int(state_meta['size_bytes'])},
    {int(national_meta['row_count'])},
    {int(state_meta['row_count'])},
    {int(national_meta['vendor_count'])},
    {int(state_meta['vendor_count'])},
    {int(national_meta['vendor_run_count'])},
    {int(state_meta['vendor_run_count'])},
    {sql_literal(minimum_pull)}::timestamptz,
    {sql_literal(maximum_pull)}::timestamptz,
    {sql_literal(str(national_meta['path']))},
    {sql_literal(str(state_meta['path']))},
    '1.0.0'
);

COMMENT ON TABLE public.{quote_ident(NATIONAL_TABLE)} IS
    'Append-only national and chamber-level observations produced by f_collector.';
COMMENT ON TABLE public.{quote_ident(STATE_TABLE)} IS
    'Append-only congressional-district and state Senate-race observations produced by f_collector.';
COMMENT ON VIEW public.{quote_ident(LATEST_NATIONAL_VIEW)} IS
    'Latest national observation for each vendor, metric, source record, and party.';
COMMENT ON VIEW public.{quote_ident(LATEST_STATE_VIEW)} IS
    'Latest race observation for each vendor, metric, source record, and party.';

ANALYZE public.{quote_ident(NATIONAL_TABLE)};
ANALYZE public.{quote_ident(STATE_TABLE)};

COMMIT;
"""


def print_summary(national_meta: Mapping[str, object], state_meta: Mapping[str, object]) -> None:
    print("Election forecast database input validation passed")
    print(
        f"National: {int(national_meta['row_count']):,} rows; "
        f"{int(national_meta['vendor_count']):,} vendors; "
        f"{int(national_meta['vendor_run_count']):,} vendor runs"
    )
    print(
        f"State:    {int(state_meta['row_count']):,} rows; "
        f"{int(state_meta['vendor_count']):,} vendors; "
        f"{int(state_meta['vendor_run_count']):,} vendor runs"
    )
    print(
        "Pull range: "
        f"{min(str(national_meta['minimum_pull_time']), str(state_meta['minimum_pull_time']))} "
        "through "
        f"{max(str(national_meta['maximum_pull_time']), str(state_meta['maximum_pull_time']))}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    national_rows, national_meta = read_validated_csv(
        args.national_input,
        expected_fields=NATIONAL_FIELDS,
        label="national",
    )
    state_rows, state_meta = read_validated_csv(
        args.state_input,
        expected_fields=STATE_FIELDS,
        label="state",
    )
    print_summary(national_meta, state_meta)

    if args.validate_only:
        return 0
    if args.output_dir is None:
        raise ValueError("--output-dir is required unless --validate-only is used")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    national_output = output_dir / f"{NATIONAL_TABLE}.csv"
    state_output = output_dir / f"{STATE_TABLE}.csv"
    write_csv(national_output, NATIONAL_FIELDS, national_rows)
    write_csv(state_output, STATE_FIELDS, state_rows)

    (output_dir / "10-schema.sql").write_text(build_schema_sql(), encoding="utf-8")
    (output_dir / "20-load.sql").write_text(
        build_copy_sql(args.copy_root), encoding="utf-8"
    )
    (output_dir / "30-post-load.sql").write_text(
        build_post_load_sql(national_meta=national_meta, state_meta=state_meta),
        encoding="utf-8",
    )
    manifest = {
        "loader_version": "1.0.0",
        "export_schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "national": national_meta,
        "state": state_meta,
        "database_objects": {
            "national_table": f"public.{NATIONAL_TABLE}",
            "state_table": f"public.{STATE_TABLE}",
            "metadata_table": f"public.{METADATA_TABLE}",
            "latest_national_view": f"public.{LATEST_NATIONAL_VIEW}",
            "latest_state_view": f"public.{LATEST_STATE_VIEW}",
            "latest_vendor_runs_view": f"public.{LATEST_RUNS_VIEW}",
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Prepared PostgreSQL initialization files: {output_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
