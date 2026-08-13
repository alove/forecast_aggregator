from __future__ import annotations

import re

# U.S. Census state FIPS codes. Territories are included for validation and
# future adapters, although the 435-seat House models currently use states only.
_STATE_ROWS = [
    ("AL", "Alabama", "01"), ("AK", "Alaska", "02"),
    ("AZ", "Arizona", "04"), ("AR", "Arkansas", "05"),
    ("CA", "California", "06"), ("CO", "Colorado", "08"),
    ("CT", "Connecticut", "09"), ("DE", "Delaware", "10"),
    ("DC", "District of Columbia", "11"), ("FL", "Florida", "12"),
    ("GA", "Georgia", "13"), ("HI", "Hawaii", "15"),
    ("ID", "Idaho", "16"), ("IL", "Illinois", "17"),
    ("IN", "Indiana", "18"), ("IA", "Iowa", "19"),
    ("KS", "Kansas", "20"), ("KY", "Kentucky", "21"),
    ("LA", "Louisiana", "22"), ("ME", "Maine", "23"),
    ("MD", "Maryland", "24"), ("MA", "Massachusetts", "25"),
    ("MI", "Michigan", "26"), ("MN", "Minnesota", "27"),
    ("MS", "Mississippi", "28"), ("MO", "Missouri", "29"),
    ("MT", "Montana", "30"), ("NE", "Nebraska", "31"),
    ("NV", "Nevada", "32"), ("NH", "New Hampshire", "33"),
    ("NJ", "New Jersey", "34"), ("NM", "New Mexico", "35"),
    ("NY", "New York", "36"), ("NC", "North Carolina", "37"),
    ("ND", "North Dakota", "38"), ("OH", "Ohio", "39"),
    ("OK", "Oklahoma", "40"), ("OR", "Oregon", "41"),
    ("PA", "Pennsylvania", "42"), ("RI", "Rhode Island", "44"),
    ("SC", "South Carolina", "45"), ("SD", "South Dakota", "46"),
    ("TN", "Tennessee", "47"), ("TX", "Texas", "48"),
    ("UT", "Utah", "49"), ("VT", "Vermont", "50"),
    ("VA", "Virginia", "51"), ("WA", "Washington", "53"),
    ("WV", "West Virginia", "54"), ("WI", "Wisconsin", "55"),
    ("WY", "Wyoming", "56"),
    ("AS", "American Samoa", "60"), ("GU", "Guam", "66"),
    ("MP", "Northern Mariana Islands", "69"),
    ("PR", "Puerto Rico", "72"), ("VI", "U.S. Virgin Islands", "78"),
]

ABBR_TO_NAME = {abbr: name for abbr, name, _ in _STATE_ROWS}
ABBR_TO_FIPS = {abbr: fips for abbr, _, fips in _STATE_ROWS}
NAME_TO_ABBR = {name.casefold(): abbr for abbr, name, _ in _STATE_ROWS}

# States with one voting House district after the 2020 census apportionment.
AT_LARGE_STATES = {"AK", "DE", "ND", "SD", "VT", "WY"}


def resolve_state(value: str) -> tuple[str, str, str]:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("state is blank")
    upper = cleaned.upper()
    abbr = upper if upper in ABBR_TO_NAME else NAME_TO_ABBR.get(cleaned.casefold())
    if not abbr:
        raise ValueError(f"unknown state: {value!r}")
    return abbr, ABBR_TO_NAME[abbr], ABBR_TO_FIPS[abbr]


def congressional_district_code(state: str, seat_number: int) -> str:
    abbr, _, fips = resolve_state(state)
    if abbr in {"DC", "AS", "GU", "MP", "PR", "VI"}:
        raise ValueError(f"{abbr} does not have a voting seat in the 435-seat model")
    if not 1 <= int(seat_number) <= 99:
        raise ValueError(f"House seat number must be 1 through 99, got {seat_number!r}")
    value = f"{fips}{int(seat_number):02d}"
    if not re.fullmatch(r"\d{4}", value):
        raise AssertionError(f"district code is not four digits: {value}")
    return value


def ordinal(number: int) -> str:
    n = int(number)
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def plain_house_seat(state: str, seat_number: int) -> str:
    abbr, name, _ = resolve_state(state)
    if abbr in AT_LARGE_STATES and int(seat_number) == 1:
        return f"{name} At-Large Congressional District"
    return f"{name} {ordinal(int(seat_number))} Congressional District"


def plain_senate_seat(state: str, seat_name: str = "", special: bool = False) -> str:
    _, name, _ = resolve_state(state)
    parts = [f"{name} U.S. Senate"]
    if seat_name:
        parts.append(seat_name.strip())
    label = " — ".join(parts)
    if special and "special" not in label.casefold():
        label += " (Special)"
    return label
