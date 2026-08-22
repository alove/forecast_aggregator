from __future__ import annotations

from datetime import datetime
from math import isfinite
import re
from typing import Any, Iterable

from .date_utils import is_iso_date
from .errors import OutputValidationError

SCHEMA_VERSION = "1.0.0"

FIELDNAMES = [
    "schema_version",
    "observed_datetime_utc",
    "vendor",
    "vendor_model",
    "vendor_run_id",
    "vendor_forecast_date",
    "vendor_updated_at_utc",
    "model_status",
    "election_date",
    "row_type",
    "source_record_id",
    "source_url",
    "source_file",
    "house_seats_basis",
    "house_seats_d",
    "house_seats_r",
    "house_seats_other",
    "house_seats_d_median",
    "house_seats_r_median",
    "house_seats_other_median",
    "house_seats_d_p05",
    "house_seats_d_p95",
    "house_control_d_pct",
    "house_control_r_pct",
    "house_control_other_pct",
    "house_popular_vote_basis",
    "house_popular_vote_d_pct",
    "house_popular_vote_r_pct",
    "house_popular_vote_other_pct",
    "house_popular_vote_margin_d_minus_r_pct",
    "house_popular_vote_d_p05",
    "house_popular_vote_d_p95",
    "senate_seats_basis",
    "senate_seats_d",
    "senate_seats_r",
    "senate_seats_other",
    "senate_seats_d_median",
    "senate_seats_r_median",
    "senate_seats_other_median",
    "senate_seats_d_p05",
    "senate_seats_d_p95",
    "senate_control_d_pct",
    "senate_control_r_pct",
    "senate_control_other_pct",
    "congressional_district",
    "state_fips",
    "state_abbreviation",
    "state",
    "house_seat_number",
    "house_seat",
    "house_d_pct",
    "house_r_pct",
    "house_other_pct",
    "house_d_vote_pct",
    "house_r_vote_pct",
    "house_other_vote_pct",
    "house_vote_d_p05",
    "house_vote_d_p95",
    "house_rating",
    "senate_seat",
    "senate_d_pct",
    "senate_r_pct",
    "senate_other_pct",
    "senate_d_vote_pct",
    "senate_r_vote_pct",
    "senate_other_vote_pct",
    "senate_vote_d_p05",
    "senate_vote_d_p95",
    "senate_rating",
    "special_election",
    "data_quality",
    "notes",
]

ROW_TYPES = {"national", "house_district", "senate_race"}
PERCENTAGE_FIELDS = {
    field
    for field in FIELDNAMES
    if field.endswith("_pct") and field != "house_popular_vote_margin_d_minus_r_pct"
}
SEAT_FIELDS = {
    field for field in FIELDNAMES if field.startswith(("house_seats_", "senate_seats_"))
    and field not in {"house_seats_basis", "senate_seats_basis"}
}

SNAPSHOT_IDENTITY_FIELDS = (
    "schema_version",
    "observed_datetime_utc",
    "vendor",
    "vendor_model",
    "vendor_run_id",
    "vendor_forecast_date",
    "vendor_updated_at_utc",
    "model_status",
    "election_date",
)
_NATIONAL_START = FIELDNAMES.index("house_seats_basis")
_NATIONAL_END = FIELDNAMES.index("senate_control_other_pct") + 1
REPEATED_NATIONAL_FIELDS = tuple(FIELDNAMES[_NATIONAL_START:_NATIONAL_END])


def utc_now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def blank_row() -> dict[str, Any]:
    row: dict[str, Any] = {field: "" for field in FIELDNAMES}
    row["schema_version"] = SCHEMA_VERSION
    return row


def rounded(value: Any, digits: int = 6) -> float | str:
    if value in (None, ""):
        return ""
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"non-finite number: {value!r}")
    return round(result, digits)


def pct_from_unit(value: Any) -> float | str:
    if value in (None, ""):
        return ""
    return rounded(float(value) * 100.0)


def pct_from_margin_dem(margin: Any) -> tuple[float, float, float]:
    m = float(margin)
    return rounded((100.0 + m) / 2.0), rounded((100.0 - m) / 2.0), 0.0


def complement_pct(value: Any) -> float:
    return rounded(100.0 - float(value))  # type: ignore[return-value]


def residual_pct(*values: Any) -> float:
    numbers = [float(v) for v in values if v not in (None, "")]
    return rounded(max(0.0, 100.0 - sum(numbers)))  # type: ignore[return-value]


def probability_rating(dem_pct: Any) -> str:
    p = float(dem_pct)
    if p >= 85:
        return "Safe D"
    if p >= 70:
        return "Likely D"
    if p >= 55:
        return "Lean D"
    if p >= 45:
        return "Toss-up"
    if p >= 30:
        return "Lean R"
    if p >= 15:
        return "Likely R"
    return "Safe R"


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


def _validate_sum(row: dict[str, Any], fields: tuple[str, str, str], label: str) -> None:
    values = [_as_float(row, field) for field in fields]
    present = [value for value in values if value is not None]
    if present and len(present) != 3:
        raise OutputValidationError(f"{label} must supply all D/R/Other values or none")
    if len(present) == 3 and abs(sum(present) - 100.0) > 0.05:
        raise OutputValidationError(f"{label} sums to {sum(present):.6f}, not 100")


def validate_rows(rows: Iterable[dict[str, Any]]) -> int:
    seen: set[tuple[str, str, str, str]] = set()
    groups: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    count = 0
    for index, row in enumerate(rows, start=1):
        count += 1
        missing_columns = [field for field in FIELDNAMES if field not in row]
        if missing_columns:
            raise OutputValidationError(
                f"row {index} is missing schema columns: {', '.join(missing_columns[:5])}"
            )
        for field in (
            "schema_version", "observed_datetime_utc", "vendor", "vendor_run_id",
            "row_type", "source_record_id", "source_url",
        ):
            if row.get(field, "") in (None, ""):
                raise OutputValidationError(f"row {index} has blank required field {field}")
        if row["schema_version"] != SCHEMA_VERSION:
            raise OutputValidationError(
                f"row {index} schema {row['schema_version']!r} != {SCHEMA_VERSION!r}"
            )
        if row["row_type"] not in ROW_TYPES:
            raise OutputValidationError(f"row {index} has unknown row_type {row['row_type']!r}")
        try:
            parsed = datetime.fromisoformat(str(row["observed_datetime_utc"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutputValidationError(
                f"row {index} observed_datetime_utc is not ISO-8601"
            ) from exc
        if parsed.tzinfo is None:
            raise OutputValidationError(f"row {index} observed_datetime_utc lacks timezone")

        forecast_date = str(row.get("vendor_forecast_date", "") or "").strip()
        if forecast_date and not is_iso_date(forecast_date):
            raise OutputValidationError(
                f"row {index} vendor_forecast_date must be YYYY-MM-DD or blank; "
                f"got {forecast_date!r}"
            )
        election_date = str(row.get("election_date", "") or "").strip()
        if not is_iso_date(election_date):
            raise OutputValidationError(
                f"row {index} election_date must be YYYY-MM-DD; got {election_date!r}"
            )

        key = (
            str(row["vendor"]), str(row["vendor_run_id"]),
            str(row["row_type"]), str(row["source_record_id"]),
        )
        if key in seen:
            raise OutputValidationError(f"duplicate normalized record in batch: {key}")
        seen.add(key)
        group_key = (str(row["vendor"]), str(row["vendor_run_id"]))
        groups.setdefault(group_key, []).append((index, row))

        for field in PERCENTAGE_FIELDS:
            number = _as_float(row, field)
            if number is not None and not -0.000001 <= number <= 100.000001:
                raise OutputValidationError(f"row {index} {field} outside 0..100: {number}")
        margin = _as_float(row, "house_popular_vote_margin_d_minus_r_pct")
        if margin is not None and not -100.000001 <= margin <= 100.000001:
            raise OutputValidationError(f"row {index} House vote margin outside -100..100")
        for field in SEAT_FIELDS:
            number = _as_float(row, field)
            if number is not None and number < -0.000001:
                raise OutputValidationError(f"row {index} {field} is negative")

        house_total = [_as_float(row, field) for field in (
            "house_seats_d", "house_seats_r", "house_seats_other"
        )]
        if all(value is not None for value in house_total):
            total = sum(value for value in house_total if value is not None)
            if abs(total - 435.0) > 0.05:
                raise OutputValidationError(f"row {index} House seats sum to {total}, not 435")
        senate_total = [_as_float(row, field) for field in (
            "senate_seats_d", "senate_seats_r", "senate_seats_other"
        )]
        if all(value is not None for value in senate_total):
            total = sum(value for value in senate_total if value is not None)
            if abs(total - 100.0) > 0.05:
                raise OutputValidationError(f"row {index} Senate seats sum to {total}, not 100")

        _validate_sum(row, ("house_control_d_pct", "house_control_r_pct", "house_control_other_pct"), "House control probabilities")
        _validate_sum(row, ("senate_control_d_pct", "senate_control_r_pct", "senate_control_other_pct"), "Senate control probabilities")
        _validate_sum(row, ("house_popular_vote_d_pct", "house_popular_vote_r_pct", "house_popular_vote_other_pct"), "House popular vote")

        if row["row_type"] == "house_district":
            code = str(row.get("congressional_district", ""))
            if not re.fullmatch(r"\d{4}", code):
                raise OutputValidationError(
                    f"row {index} congressional_district must be four digits, got {code!r}"
                )
            if not re.fullmatch(r"\d{2}", str(row.get("state_fips", ""))):
                raise OutputValidationError(f"row {index} state_fips must be two digits")
            if row.get("house_seat_number", "") in (None, ""):
                raise OutputValidationError(f"row {index} House district lacks seat number")
            _validate_sum(row, ("house_d_pct", "house_r_pct", "house_other_pct"), "House race probabilities")
            _validate_sum(row, ("house_d_vote_pct", "house_r_vote_pct", "house_other_vote_pct"), "House race vote shares")
        if row["row_type"] == "senate_race":
            if row.get("senate_seat", "") in (None, ""):
                raise OutputValidationError(f"row {index} Senate race lacks seat label")
            _validate_sum(row, ("senate_d_pct", "senate_r_pct", "senate_other_pct"), "Senate race probabilities")
            _validate_sum(row, ("senate_d_vote_pct", "senate_r_vote_pct", "senate_other_vote_pct"), "Senate race vote shares")
    if count == 0:
        raise OutputValidationError("no normalized rows were produced")

    # A vendor run is a denormalized snapshot. Require exactly one national
    # row and ensure every race row repeats the same snapshot metadata and
    # national toplines. This prevents a partially mixed or malformed run from
    # looking valid merely because each row is individually well-formed.
    repeated_fields = SNAPSHOT_IDENTITY_FIELDS + REPEATED_NATIONAL_FIELDS
    for group_key, members in groups.items():
        national_members = [member for member in members if member[1]["row_type"] == "national"]
        if len(national_members) != 1:
            raise OutputValidationError(
                f"vendor run {group_key} must contain exactly one national row; "
                f"found {len(national_members)}"
            )
        _, reference = national_members[0]
        for row_index, row in members:
            for field in repeated_fields:
                if row.get(field, "") != reference.get(field, ""):
                    raise OutputValidationError(
                        f"row {row_index} {field} differs from the national row "
                        f"for vendor run {group_key}"
                    )
    return count
