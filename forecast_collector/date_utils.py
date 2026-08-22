from __future__ import annotations

"""Canonical date handling for source metadata and accumulated CSV history.

The collector's public export contract uses ISO ``YYYY-MM-DD`` dates. Some U.S.
publishers have historically switched between ISO and display-oriented
``M/D/YY`` values without changing the underlying forecast. We convert only
those two unambiguous, explicitly supported representations. Unknown or
malformed optional forecast dates become blank/SQL NULL rather than being
invented or guessed.
"""

from datetime import date
import re
from typing import Any

_US_DATE_RE = re.compile(r"^(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2}|\d{4})$")


def canonical_date_or_blank(value: Any) -> str:
    """Return an ISO date for a trusted representation, otherwise ``""``.

    Accepted inputs:

    * ``YYYY-MM-DD``;
    * U.S. ``M/D/YYYY``;
    * U.S. ``M/D/YY`` where the two-digit year is interpreted as ``20YY``.

    The two-digit form is intentionally interpreted in the 2000s because this
    package is a 2026 election collector. We do not try locale-dependent
    day/month formats, prose dates, chart labels, timestamps, or partial dates.
    """

    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass

    match = _US_DATE_RE.fullmatch(text)
    if not match:
        return ""
    month = int(match.group("month"))
    day = int(match.group("day"))
    year_text = match.group("year")
    year = int(year_text)
    if len(year_text) == 2:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def require_canonical_date(value: Any, *, field: str) -> str:
    """Return a canonical date or raise for a required/invalid value."""

    normalized = canonical_date_or_blank(value)
    if not normalized:
        raise ValueError(f"{field} is not a trusted calendar date: {value!r}")
    return normalized


def is_iso_date(value: Any) -> bool:
    """Return whether ``value`` is exactly an ISO ``YYYY-MM-DD`` date."""

    if value in (None, ""):
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        return date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False
