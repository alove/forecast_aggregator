from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, Iterable

from .errors import OutputValidationError

EXPORT_SCHEMA_VERSION = "2.0.0"

COMMON_FIELDS = [
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
    "source_file",
    "data_quality",
    "notes",
]

NATIONAL_FIELDNAMES = COMMON_FIELDS + [
    "geography_type",
    "geography_id",
    "geography_name",
]

STATE_FIELDNAMES = COMMON_FIELDS + [
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

NATIONAL_METRIC_TYPES = {
    "US House Seats by Party",
    "US House Party Probability",
    "US Senate Seats by Party",
    "US Senate Party Probability",
    "US House Popular Vote Projection",
    "US House Popular Vote Margin",
}

STATE_METRIC_TYPES = {
    "US House District Party Probability",
    "US House District Vote Projection",
    "US Senate Race Party Probability",
    "US Senate Race Vote Projection",
}


def _blank(fieldnames: list[str]) -> dict[str, Any]:
    row = {field: "" for field in fieldnames}
    row["schema_version"] = EXPORT_SCHEMA_VERSION
    return row


def _common(source: dict[str, Any], metric_type: str, party: str, value: Any, unit: str) -> dict[str, Any]:
    row = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "rhubarb_pull_time": source.get("observed_datetime_utc", ""),
        "observed_datetime_utc": source.get("observed_datetime_utc", ""),
        "vendor": source.get("vendor", ""),
        "vendor_model": source.get("vendor_model", ""),
        "vendor_run_id": source.get("vendor_run_id", ""),
        "vendor_forecast_date": source.get("vendor_forecast_date", ""),
        "vendor_updated_at_utc": source.get("vendor_updated_at_utc", ""),
        "model_status": source.get("model_status", ""),
        "election_date": source.get("election_date", ""),
        "metric_type": metric_type,
        "party": party,
        "value": value,
        "unit": unit,
        "source_record_id": source.get("source_record_id", ""),
        "source_url": source.get("source_url", ""),
        "source_file": source.get("source_file", ""),
        "data_quality": source.get("data_quality", ""),
        "notes": source.get("notes", ""),
    }
    return row


def _has(value: Any) -> bool:
    return value not in (None, "")


def _emit_party_metric(
    target: list[dict[str, Any]],
    source: dict[str, Any],
    fieldnames: list[str],
    *,
    metric_type: str,
    unit: str,
    fields: dict[str, str],
    median_fields: dict[str, str] | None = None,
    low_fields: dict[str, str] | None = None,
    high_fields: dict[str, str] | None = None,
    basis: Any = "",
    extra: dict[str, Any] | None = None,
) -> None:
    median_fields = median_fields or {}
    low_fields = low_fields or {}
    high_fields = high_fields or {}
    for party, field in fields.items():
        value = source.get(field, "")
        if not _has(value):
            continue
        row = _blank(fieldnames)
        row.update(_common(source, metric_type, party, value, unit))
        row["median_value"] = source.get(median_fields.get(party, ""), "") if party in median_fields else ""
        row["low_value"] = source.get(low_fields.get(party, ""), "") if party in low_fields else ""
        row["high_value"] = source.get(high_fields.get(party, ""), "") if party in high_fields else ""
        row["basis"] = basis
        if extra:
            row.update(extra)
        target.append(row)


def split_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    national: list[dict[str, Any]] = []
    state: list[dict[str, Any]] = []

    for source in rows:
        row_type = source.get("row_type")
        if row_type == "national":
            us = {"geography_type": "national", "geography_id": "US", "geography_name": "United States"}
            _emit_party_metric(
                national, source, NATIONAL_FIELDNAMES,
                metric_type="US House Seats by Party", unit="seats",
                fields={"D": "house_seats_d", "R": "house_seats_r", "Other": "house_seats_other"},
                median_fields={"D": "house_seats_d_median", "R": "house_seats_r_median", "Other": "house_seats_other_median"},
                low_fields={"D": "house_seats_d_p05"}, high_fields={"D": "house_seats_d_p95"},
                basis=source.get("house_seats_basis", ""), extra=us,
            )
            _emit_party_metric(
                national, source, NATIONAL_FIELDNAMES,
                metric_type="US House Party Probability", unit="percent",
                fields={"D": "house_control_d_pct", "R": "house_control_r_pct", "Other": "house_control_other_pct"},
                basis="probability of controlling the U.S. House", extra=us,
            )
            _emit_party_metric(
                national, source, NATIONAL_FIELDNAMES,
                metric_type="US Senate Seats by Party", unit="seats",
                fields={"D": "senate_seats_d", "R": "senate_seats_r", "Other": "senate_seats_other"},
                median_fields={"D": "senate_seats_d_median", "R": "senate_seats_r_median", "Other": "senate_seats_other_median"},
                low_fields={"D": "senate_seats_d_p05"}, high_fields={"D": "senate_seats_d_p95"},
                basis=source.get("senate_seats_basis", ""), extra=us,
            )
            _emit_party_metric(
                national, source, NATIONAL_FIELDNAMES,
                metric_type="US Senate Party Probability", unit="percent",
                fields={"D": "senate_control_d_pct", "R": "senate_control_r_pct", "Other": "senate_control_other_pct"},
                basis="probability of controlling the U.S. Senate", extra=us,
            )
            _emit_party_metric(
                national, source, NATIONAL_FIELDNAMES,
                metric_type="US House Popular Vote Projection", unit="percent",
                fields={"D": "house_popular_vote_d_pct", "R": "house_popular_vote_r_pct", "Other": "house_popular_vote_other_pct"},
                low_fields={"D": "house_popular_vote_d_p05"}, high_fields={"D": "house_popular_vote_d_p95"},
                basis=source.get("house_popular_vote_basis", ""), extra=us,
            )
            margin = source.get("house_popular_vote_margin_d_minus_r_pct", "")
            if _has(margin):
                row = _blank(NATIONAL_FIELDNAMES)
                row.update(_common(source, "US House Popular Vote Margin", "D-R", margin, "percentage_points"))
                row["basis"] = source.get("house_popular_vote_basis", "")
                row.update(us)
                national.append(row)
            continue

        if row_type == "house_district":
            extra = {
                "geography_type": "congressional_district",
                "geography_id": source.get("congressional_district", ""),
                "state_fips": source.get("state_fips", ""),
                "state_abbreviation": source.get("state_abbreviation", ""),
                "state": source.get("state", ""),
                "congressional_district": source.get("congressional_district", ""),
                "house_seat_number": source.get("house_seat_number", ""),
                "house_seat": source.get("house_seat", ""),
                "senate_seat": "",
                "special_election": source.get("special_election", ""),
                "rating": source.get("house_rating", ""),
            }
            _emit_party_metric(
                state, source, STATE_FIELDNAMES,
                metric_type="US House District Party Probability", unit="percent",
                fields={"D": "house_d_pct", "R": "house_r_pct", "Other": "house_other_pct"},
                basis="probability of winning the congressional district", extra=extra,
            )
            _emit_party_metric(
                state, source, STATE_FIELDNAMES,
                metric_type="US House District Vote Projection", unit="percent",
                fields={"D": "house_d_vote_pct", "R": "house_r_vote_pct", "Other": "house_other_vote_pct"},
                low_fields={"D": "house_vote_d_p05"}, high_fields={"D": "house_vote_d_p95"},
                basis="projected district vote share", extra=extra,
            )
            continue

        if row_type == "senate_race":
            extra = {
                "geography_type": "state",
                "geography_id": source.get("state_fips", ""),
                "state_fips": source.get("state_fips", ""),
                "state_abbreviation": source.get("state_abbreviation", ""),
                "state": source.get("state", ""),
                "congressional_district": "",
                "house_seat_number": "",
                "house_seat": "",
                "senate_seat": source.get("senate_seat", ""),
                "special_election": source.get("special_election", ""),
                "rating": source.get("senate_rating", ""),
            }
            _emit_party_metric(
                state, source, STATE_FIELDNAMES,
                metric_type="US Senate Race Party Probability", unit="percent",
                fields={"D": "senate_d_pct", "R": "senate_r_pct", "Other": "senate_other_pct"},
                basis="probability of winning the Senate race", extra=extra,
            )
            _emit_party_metric(
                state, source, STATE_FIELDNAMES,
                metric_type="US Senate Race Vote Projection", unit="percent",
                fields={"D": "senate_d_vote_pct", "R": "senate_r_vote_pct", "Other": "senate_other_vote_pct"},
                low_fields={"D": "senate_vote_d_p05"}, high_fields={"D": "senate_vote_d_p95"},
                basis="projected Senate race vote share", extra=extra,
            )

    return national, state


def _as_float(row: dict[str, Any], field: str) -> float | None:
    value = row.get(field, "")
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OutputValidationError(f"{field} is not numeric: {value!r}") from exc
    if not isfinite(number):
        raise OutputValidationError(f"{field} is not finite: {value!r}")
    return number


def _validate_common(rows: Iterable[dict[str, Any]], fieldnames: list[str], metric_types: set[str]) -> int:
    count = 0
    seen: set[tuple[str, str, str, str, str]] = set()
    for index, row in enumerate(rows, start=1):
        count += 1
        missing = [field for field in fieldnames if field not in row]
        if missing:
            raise OutputValidationError(f"row {index} missing export columns: {', '.join(missing[:5])}")
        for field in ("schema_version", "rhubarb_pull_time", "vendor", "vendor_run_id", "metric_type", "value", "unit", "source_record_id", "source_url"):
            if row.get(field, "") in (None, ""):
                raise OutputValidationError(f"row {index} has blank required field {field}")
        if row["schema_version"] != EXPORT_SCHEMA_VERSION:
            raise OutputValidationError(f"row {index} export schema mismatch")
        if row["metric_type"] not in metric_types:
            raise OutputValidationError(f"row {index} unknown metric_type {row['metric_type']!r}")
        try:
            parsed = datetime.fromisoformat(str(row["rhubarb_pull_time"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutputValidationError(f"row {index} rhubarb_pull_time is not ISO-8601") from exc
        if parsed.tzinfo is None or parsed.microsecond != 0:
            raise OutputValidationError(f"row {index} rhubarb_pull_time must be timezone-aware to the second")
        value = _as_float(row, "value")
        if row["unit"] == "percent" and value is not None and not -0.000001 <= value <= 100.000001:
            raise OutputValidationError(f"row {index} percent value outside 0..100")
        key = (
            str(row.get("vendor", "")), str(row.get("vendor_run_id", "")),
            str(row.get("metric_type", "")), str(row.get("source_record_id", "")),
            str(row.get("party", "")),
        )
        if key in seen:
            raise OutputValidationError(f"duplicate export record in batch: {key}")
        seen.add(key)
    return count


def validate_national_rows(rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    count = _validate_common(rows, NATIONAL_FIELDNAMES, NATIONAL_METRIC_TYPES)
    for index, row in enumerate(rows, start=1):
        if row.get("geography_type") != "national" or row.get("geography_id") != "US":
            raise OutputValidationError(f"national row {index} has invalid geography")
    return count


def validate_state_rows(rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    count = _validate_common(rows, STATE_FIELDNAMES, STATE_METRIC_TYPES)
    for index, row in enumerate(rows, start=1):
        if row.get("geography_type") == "congressional_district":
            district = str(row.get("congressional_district", ""))
            if len(district) != 4 or not district.isdigit():
                raise OutputValidationError(f"state row {index} congressional_district must be four digits")
        elif row.get("geography_type") == "state":
            fips = str(row.get("state_fips", ""))
            if len(fips) != 2 or not fips.isdigit():
                raise OutputValidationError(f"state row {index} state_fips must be two digits")
        else:
            raise OutputValidationError(f"state row {index} has invalid geography_type")
    return count
