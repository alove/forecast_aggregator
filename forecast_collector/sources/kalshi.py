from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
from statistics import median
from typing import Any, Iterable
from urllib.parse import urlencode

from ..errors import SourceFormatError
from ..http import HttpClient
from ..models import RawArtifact, SourceResult
from ..schema import blank_row, probability_rating, rounded
from ..states import (
    ABBR_TO_FIPS,
    ABBR_TO_NAME,
    congressional_district_code,
    plain_house_seat,
    plain_senate_seat,
    resolve_state,
)
from .base import ForecastSource


API_BASE = "https://external-api.kalshi.com/trade-api/v2"
MIDTERMS_PAGE = "https://kalshi.com/category/elections/midterms"
HOUSE_PAGE = "https://kalshi.com/category/elections/midterms/house"

HOUSE_SEATS_BY_STATE = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8,
    "CT": 5, "DE": 1, "FL": 28, "GA": 14, "HI": 2, "ID": 2,
    "IL": 17, "IN": 9, "IA": 4, "KS": 4, "KY": 6, "LA": 6,
    "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4,
    "MO": 8, "MT": 2, "NE": 3, "NV": 4, "NH": 2, "NJ": 12,
    "NM": 3, "NY": 26, "NC": 14, "ND": 1, "OH": 15, "OK": 5,
    "OR": 6, "PA": 17, "RI": 2, "SC": 7, "SD": 1, "TN": 9,
    "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2,
    "WI": 8, "WY": 1,
}

REGULAR_SENATE_STATES = {
    "AL", "AK", "AR", "CO", "DE", "GA", "ID", "IL", "IA", "KS",
    "KY", "LA", "ME", "MA", "MI", "MN", "MS", "MT", "NE", "NH",
    "NJ", "NM", "NC", "OK", "OR", "RI", "SC", "SD", "TN", "TX",
    "VA", "WV", "WY",
}
SPECIAL_SENATE_STATES = {"FL", "OH"}
EXPECTED_SENATE_KEYS = {(abbr, False) for abbr in REGULAR_SENATE_STATES} | {
    (abbr, True) for abbr in SPECIAL_SENATE_STATES
}

NATIONAL_EVENT_TICKERS = {
    "house_control": "CONTROLH-2026",
    "senate_control": "CONTROLS-2026",
    "house_seats_d": "KXDHOUSESEATS-27",
    "house_seats_r": "KXRHOUSESEATS-27",
    "senate_seats_d": "KXDSENATESEATS-27",
    "senate_seats_r": "RSENATESEATS-27",
    "house_popular_vote": "KXHOUSEPOPVOTEMARGIN-27NOV03",
}

_HOUSE_UNIFIED_EVENT_RE = re.compile(r"^KXHOUSERACE-([A-Z]{2})(\d{2}|AL)-26$")
_HOUSE_LEGACY_EVENT_RE = re.compile(r"^HOUSE([A-Z]{2})(\d{2}|AL)-26$")
_SENATE_EVENT_RE = re.compile(r"^SENATE([A-Z]{2})(S?)-26$")
_RANGE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to|through)\s*(-?\d+(?:\.\d+)?)", re.I)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PRIMARY_WORDS = ("primary", "nominee", "nomination", "runoff")
EXPECTED_HOUSE_KEYS = {
    f"{abbr}-{seat:02d}"
    for abbr, count in HOUSE_SEATS_BY_STATE.items()
    for seat in range(1, count + 1)
}


@dataclass(frozen=True)
class PricedMarket:
    ticker: str
    probability_pct: float
    price_basis: str
    label: str
    party: str = ""


@dataclass(frozen=True)
class NumericBucket:
    label: str
    probability_pct: float
    kind: str
    low: float | None = None
    high: float | None = None


class KalshiSource(ForecastSource):
    """Adapter for Kalshi's public 2026 congressional prediction markets.

    Kalshi is a live exchange rather than a model publisher.  The normalized
    values are market-implied probabilities.  Chamber seat counts and the
    House popular-vote projection are expected values derived from mutually
    exclusive Kalshi outcome ladders; they are never inferred from an
    incomplete collection of district or state races.
    """

    name = "Kalshi"
    slug = "kalshi"
    model_name = "Kalshi 2026 Congressional Prediction Markets"

    def collect(
        self,
        client: HttpClient,
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        backfill: bool = False,
    ) -> SourceResult:
        if backfill:
            raise SourceFormatError("Kalshi collection currently supports the live market snapshot, not backfill")

        warnings: list[str] = []
        events: dict[str, dict[str, Any]] = {}
        house_fallback = {
            "attempted": 0,
            "fetched": 0,
            "loaded": 0,
            "unavailable": 0,
            "unreadable": 0,
            "skipped": False,
            "warnings": [],
        }

        for label, ticker in NATIONAL_EVENT_TICKERS.items():
            try:
                events[ticker] = self._fetch_event(client, ticker)
            except Exception as exc:
                warnings.append(f"{label} event {ticker} unavailable: {type(exc).__name__}: {exc}")

        if include_house_districts:
            house_series_succeeded = False
            try:
                for event in self._fetch_series_events(client, "KXHOUSERACE"):
                    ticker = str(event.get("event_ticker", "")).strip().upper()
                    if ticker:
                        events[ticker] = event
                house_series_succeeded = True
            except Exception as exc:
                warnings.append(f"House race series unavailable: {type(exc).__name__}: {exc}")

            if house_series_succeeded:
                house_fallback = self._supplement_legacy_house_events(client, events)
                warnings.extend(house_fallback["warnings"])

        if include_senate_races:
            for abbr in sorted(REGULAR_SENATE_STATES):
                ticker = f"SENATE{abbr}-26"
                try:
                    events[ticker] = self._fetch_event(client, ticker)
                except Exception as exc:
                    warnings.append(f"Senate event {ticker} unavailable: {type(exc).__name__}: {exc}")
            for abbr in sorted(SPECIAL_SENATE_STATES):
                ticker = f"SENATE{abbr}S-26"
                try:
                    events[ticker] = self._fetch_event(client, ticker)
                except Exception as exc:
                    warnings.append(f"Senate special event {ticker} unavailable: {type(exc).__name__}: {exc}")

        rows, run_id, diagnostics = self.normalize_events(
            list(events.values()),
            observed_datetime_utc=observed_datetime_utc,
            include_house_districts=include_house_districts,
            include_senate_races=include_senate_races,
            require_complete_counts=False,
            acquisition_warnings=warnings,
        )
        if not rows:
            raise SourceFormatError("Kalshi produced no readable 2026 congressional market data")

        raw_payload = {
            "api_base": API_BASE,
            "observed_datetime_utc": observed_datetime_utc,
            "events": sorted(events.values(), key=lambda event: str(event.get("event_ticker", ""))),
            "warnings": warnings,
            "house_legacy_fallback": house_fallback,
        }
        raw = json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return SourceResult(
            source_name=self.name,
            rows=rows,
            raw_artifacts=[RawArtifact("kalshi_2026_congressional_events.json", raw)],
            details={
                "forecast_dates": [],
                "run_ids": [run_id],
                "model_status": diagnostics["model_status"],
                "partial": diagnostics["partial"],
                "partial_sections": diagnostics["partial_sections"],
                "house_record_count": diagnostics["house_record_count"],
                "senate_record_count": diagnostics["senate_record_count"],
                "national_metrics": diagnostics["national_metrics"],
                "house_legacy_fallback_attempted": house_fallback["attempted"],
                "house_legacy_fallback_fetched": house_fallback["fetched"],
                "house_legacy_fallback_loaded": house_fallback["loaded"],
                "house_legacy_fallback_unavailable": house_fallback["unavailable"],
                "house_legacy_fallback_unreadable": house_fallback["unreadable"],
                "warnings": warnings,
            },
        )

    @staticmethod
    def _response_json(response: Any, *, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise SourceFormatError(f"Kalshi {label} response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SourceFormatError(f"Kalshi {label} response is not a JSON object")
        return payload

    def _fetch_event(self, client: HttpClient, ticker: str) -> dict[str, Any]:
        url = f"{API_BASE}/events/{ticker}?with_nested_markets=true"
        payload = self._response_json(client.get(url), label=f"event {ticker}")
        event = payload.get("event")
        if not isinstance(event, dict):
            raise SourceFormatError(f"Kalshi event {ticker} response has no event object")
        if not isinstance(event.get("markets"), list) and isinstance(payload.get("markets"), list):
            event = dict(event)
            event["markets"] = payload["markets"]
        return event

    def _fetch_series_events(self, client: HttpClient, series_ticker: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        raw_max_pages = os.environ.get("EFC_KALSHI_MAX_EVENT_PAGES", "20")
        try:
            max_pages = max(1, int(raw_max_pages))
        except ValueError:
            max_pages = 20
        for _ in range(max_pages):
            params = {
                "limit": "200",
                "series_ticker": series_ticker,
                "status": "open",
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            url = f"{API_BASE}/events?{urlencode(params)}"
            payload = self._response_json(client.get(url), label=f"series {series_ticker}")
            page = payload.get("events")
            if not isinstance(page, list):
                raise SourceFormatError(f"Kalshi series {series_ticker} response has no events list")
            result.extend(event for event in page if isinstance(event, dict))
            next_cursor = str(payload.get("cursor", "") or "")
            if not next_cursor:
                return result
            if next_cursor in seen_cursors:
                raise SourceFormatError(f"Kalshi series {series_ticker} repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise SourceFormatError(
            f"Kalshi series {series_ticker} exceeded EFC_KALSHI_MAX_EVENT_PAGES={max_pages}"
        )

    @staticmethod
    def _house_event_identity(ticker: str) -> tuple[str, int, int] | None:
        """Return state, seat, and source priority for a 2026 House event.

        Kalshi currently has two public ticker families.  KXHOUSERACE is the
        unified modern series, while older events remain under one series per
        district (for example HOUSECA27-26 and HOUSEAKAL-26).  Both resolve by
        winning party.  The unified event wins only when both forms are valid.
        """
        cleaned = str(ticker or "").strip().upper()
        for pattern, priority in (
            (_HOUSE_UNIFIED_EVENT_RE, 2),
            (_HOUSE_LEGACY_EVENT_RE, 1),
        ):
            match = pattern.fullmatch(cleaned)
            if not match:
                continue
            abbr, seat_text = match.groups()
            seat = 1 if seat_text == "AL" else int(seat_text)
            if abbr not in HOUSE_SEATS_BY_STATE or not 1 <= seat <= HOUSE_SEATS_BY_STATE[abbr]:
                return None
            return abbr, seat, priority
        return None

    @staticmethod
    def _legacy_house_event_ticker(abbr: str, seat: int) -> str:
        count = HOUSE_SEATS_BY_STATE[abbr]
        code = "AL" if count == 1 else f"{int(seat):02d}"
        return f"HOUSE{abbr}{code}-26"

    def _supplement_legacy_house_events(
        self,
        client: HttpClient,
        events: dict[str, dict[str, Any]],
        *,
        expected_keys: set[str] | None = None,
        max_fetches: int | None = None,
    ) -> dict[str, Any]:
        """Fill missing unified House markets from Kalshi's legacy tickers.

        The fallback is attempted only after the unified series produced at
        least one readable general-election event.  That protects against an
        API-wide series failure triggering 435 speculative direct requests.
        Failures are aggregated into one diagnostic rather than one warning per
        missing district.
        """
        expected = set(expected_keys or EXPECTED_HOUSE_KEYS)
        before = self._house_records(events.values())
        missing = sorted(expected - set(before))
        result: dict[str, Any] = {
            "attempted": 0,
            "fetched": 0,
            "loaded": 0,
            "unavailable": 0,
            "unreadable": 0,
            "skipped": False,
            "warnings": [],
        }
        if not missing:
            return result
        if not before:
            result["skipped"] = True
            result["warnings"].append(
                "House unified series returned no readable general-election markets; "
                "legacy direct-event fallback was skipped"
            )
            return result

        if max_fetches is None:
            raw_limit = os.environ.get("EFC_KALSHI_MAX_LEGACY_HOUSE_FETCHES", "435")
            try:
                max_fetches = max(0, int(raw_limit))
            except ValueError:
                max_fetches = 435
                result["warnings"].append(
                    "invalid EFC_KALSHI_MAX_LEGACY_HOUSE_FETCHES value; using 435"
                )
        if len(missing) > max_fetches:
            result["skipped"] = True
            result["warnings"].append(
                f"House legacy fallback needs {len(missing)} requests, above "
                f"EFC_KALSHI_MAX_LEGACY_HOUSE_FETCHES={max_fetches}; fallback skipped"
            )
            return result

        unavailable: list[str] = []
        for key in missing:
            abbr, seat_text = key.split("-", 1)
            ticker = self._legacy_house_event_ticker(abbr, int(seat_text))
            result["attempted"] += 1
            try:
                loaded = self._fetch_event(client, ticker)
            except Exception:
                unavailable.append(ticker)
                continue
            events[ticker] = loaded
            result["fetched"] += 1

        after = self._house_records(events.values())
        result["loaded"] = len((set(after) - set(before)) & expected)
        result["unavailable"] = len(unavailable)
        result["unreadable"] = max(0, result["fetched"] - result["loaded"])
        if unavailable:
            examples = ", ".join(unavailable[:5])
            suffix = "" if len(unavailable) <= 5 else ", ..."
            result["warnings"].append(
                f"House legacy fallback could not fetch {len(unavailable)}/{len(missing)} "
                f"missing district events (examples: {examples}{suffix})"
            )
        if result["unreadable"]:
            result["warnings"].append(
                f"House legacy fallback fetched {result['unreadable']} event(s) without "
                "a complete mutually exclusive party-price set"
            )
        return result

    def normalize_events(
        self,
        events: Iterable[dict[str, Any]],
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
        acquisition_warnings: Iterable[str] = (),
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        by_ticker: dict[str, dict[str, Any]] = {}
        for event in events:
            ticker = str(event.get("event_ticker", "")).strip().upper()
            if ticker:
                by_ticker[ticker] = event

        national, national_sources, metric_diagnostics = self._national_metrics(by_ticker)
        house_records = self._house_records(by_ticker.values()) if include_house_districts else {}
        senate_records = self._senate_records(by_ticker.values()) if include_senate_races else {}

        missing_metrics = [
            name for name, present in (
                ("House seat projection", bool(national.get("house_seats_basis"))),
                ("House control probability", national.get("house_control_d_pct", "") != ""),
                ("House popular vote", bool(national.get("house_popular_vote_basis"))),
                ("Senate seat projection", bool(national.get("senate_seats_basis"))),
                ("Senate control probability", national.get("senate_control_d_pct", "") != ""),
            ) if not present
        ]
        partial_sections: list[str] = []
        if missing_metrics:
            partial_sections.append("missing national metrics: " + ", ".join(missing_metrics))
        if include_house_districts and len(house_records) != 435:
            partial_sections.append(f"House district markets: {len(house_records)}/435 readable")
        if include_senate_races and len(senate_records) != 35:
            partial_sections.append(f"Senate race markets: {len(senate_records)}/35 readable")
        partial_sections.extend(str(item) for item in acquisition_warnings)
        partial_sections.extend(metric_diagnostics)
        partial = bool(partial_sections)

        if require_complete_counts:
            count_errors = []
            if include_house_districts and len(house_records) != 435:
                count_errors.append(f"expected 435 House markets, found {len(house_records)}")
            if include_senate_races and len(senate_records) != 35:
                count_errors.append(f"expected 35 Senate markets, found {len(senate_records)}")
            if missing_metrics:
                count_errors.append("missing national metrics: " + ", ".join(missing_metrics))
            if count_errors:
                raise SourceFormatError("; ".join(count_errors))

        canonical_events = [self._canonical_event(event) for event in by_ticker.values()]
        canonical = json.dumps(
            sorted(canonical_events, key=lambda event: event["event_ticker"]),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        run_id = f"kalshi-undated-{sha256(canonical).hexdigest()[:16]}"
        model_status = "published_partial" if partial else "published"

        common = {
            "observed_datetime_utc": observed_datetime_utc,
            "vendor": self.name,
            "vendor_model": self.model_name,
            "vendor_run_id": run_id,
            # Kalshi exposes live market prices, not a source-supplied model date.
            # Never promote page text, event dates, or market close times into this field.
            "vendor_forecast_date": "",
            "vendor_updated_at_utc": "",
            "model_status": model_status,
            "election_date": "2026-11-03",
            **national,
        }

        national_row = blank_row()
        national_row.update(common)
        national_row.update({
            "row_type": "national",
            "source_record_id": "national",
            "source_url": MIDTERMS_PAGE,
            "source_file": "Kalshi Trade API v2: " + ", ".join(sorted(national_sources)),
            "data_quality": "published_market_odds",
            "notes": (
                "Live Kalshi market-implied probabilities. Seat counts and House popular vote are "
                "expected values derived from normalized mutually exclusive outcome ladders; race "
                "probabilities are not summed to manufacture chamber totals. "
                + ("Partial snapshot: " + "; ".join(partial_sections) if partial_sections else "Complete requested snapshot.")
            ),
        })
        rows: list[dict[str, Any]] = [national_row]

        for key in sorted(house_records):
            record = house_records[key]
            abbr, state_name, fips = resolve_state(record["state_abbreviation"])
            seat = int(record["seat_number"])
            row = blank_row()
            row.update(common)
            row.update({
                "row_type": "house_district",
                "source_record_id": record["event_ticker"],
                "source_url": f"{API_BASE}/events/{record['event_ticker']}",
                "source_file": "Kalshi Trade API v2 event",
                "congressional_district": congressional_district_code(abbr, seat),
                "state_fips": fips,
                "state_abbreviation": abbr,
                "state": state_name,
                "house_seat_number": seat,
                "house_seat": plain_house_seat(abbr, seat),
                "house_d_pct": record["D"],
                "house_r_pct": record["R"],
                "house_other_pct": record["Other"],
                "house_rating": probability_rating(record["D"]),
                "data_quality": record["data_quality"],
                "notes": record["notes"],
            })
            rows.append(row)

        for key in sorted(senate_records):
            record = senate_records[key]
            abbr, state_name, fips = resolve_state(record["state_abbreviation"])
            special = bool(record["special"])
            row = blank_row()
            row.update(common)
            row.update({
                "row_type": "senate_race",
                "source_record_id": record["event_ticker"],
                "source_url": f"{API_BASE}/events/{record['event_ticker']}",
                "source_file": "Kalshi Trade API v2 event",
                "state_fips": fips,
                "state_abbreviation": abbr,
                "state": state_name,
                "senate_seat": plain_senate_seat(abbr, special=special),
                "senate_d_pct": record["D"],
                "senate_r_pct": record["R"],
                "senate_other_pct": record["Other"],
                "senate_rating": probability_rating(record["D"]),
                "special_election": special,
                "data_quality": record["data_quality"],
                "notes": record["notes"],
            })
            rows.append(row)

        diagnostics = {
            "partial": partial,
            "partial_sections": partial_sections,
            "model_status": model_status,
            "house_record_count": len(house_records),
            "senate_record_count": len(senate_records),
            "national_metrics": sorted(name for name in (
                "house_seats" if national.get("house_seats_basis") else "",
                "house_control" if national.get("house_control_d_pct", "") != "" else "",
                "house_popular_vote" if national.get("house_popular_vote_basis") else "",
                "senate_seats" if national.get("senate_seats_basis") else "",
                "senate_control" if national.get("senate_control_d_pct", "") != "" else "",
            ) if name),
        }
        return rows, run_id, diagnostics

    def _national_metrics(
        self, by_ticker: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, Any], set[str], list[str]]:
        values: dict[str, Any] = {}
        sources: set[str] = set()
        diagnostics: list[str] = []

        house_control = self._party_probabilities(by_ticker.get(NATIONAL_EVENT_TICKERS["house_control"]))
        if house_control:
            values.update({
                "house_control_d_pct": house_control[0]["D"],
                "house_control_r_pct": house_control[0]["R"],
                "house_control_other_pct": house_control[0]["Other"],
            })
            sources.add(NATIONAL_EVENT_TICKERS["house_control"])

        senate_control = self._party_probabilities(by_ticker.get(NATIONAL_EVENT_TICKERS["senate_control"]))
        if senate_control:
            values.update({
                "senate_control_d_pct": senate_control[0]["D"],
                "senate_control_r_pct": senate_control[0]["R"],
                "senate_control_other_pct": senate_control[0]["Other"],
            })
            sources.add(NATIONAL_EVENT_TICKERS["senate_control"])

        house_d = self._distribution_expected(
            by_ticker.get(NATIONAL_EVENT_TICKERS["house_seats_d"]), upper_bound=435.0
        )
        house_r = self._distribution_expected(
            by_ticker.get(NATIONAL_EVENT_TICKERS["house_seats_r"]), upper_bound=435.0
        )
        house_pair = self._reconcile_party_seats(house_d, house_r, total=435.0, tolerance=3.0)
        if house_pair:
            values.update({
                "house_seats_basis": (
                    "Kalshi market-implied expected seats derived from normalized Democratic and "
                    "Republican mutually exclusive seat-count ladders; open tails use one adjacent "
                    "bucket width and the two party expectations are reconciled to 435"
                ),
                "house_seats_d": house_pair[0],
                "house_seats_r": house_pair[1],
                "house_seats_other": 0.0,
            })
            if house_d:
                sources.add(NATIONAL_EVENT_TICKERS["house_seats_d"])
            if house_r:
                sources.add(NATIONAL_EVENT_TICKERS["house_seats_r"])
        elif house_d or house_r:
            diagnostics.append("House seat-count ladders were internally inconsistent and were left null")

        senate_d = self._distribution_expected(
            by_ticker.get(NATIONAL_EVENT_TICKERS["senate_seats_d"]), upper_bound=100.0
        )
        senate_r = self._distribution_expected(
            by_ticker.get(NATIONAL_EVENT_TICKERS["senate_seats_r"]), upper_bound=100.0
        )
        senate_pair = self._reconcile_party_seats(senate_d, senate_r, total=100.0, tolerance=2.0)
        if senate_pair:
            values.update({
                "senate_seats_basis": (
                    "Kalshi market-implied expected seats derived from normalized Democratic and "
                    "Republican mutually exclusive seat-count ladders; open tails use one adjacent "
                    "bucket width and the two party expectations are reconciled to 100"
                ),
                "senate_seats_d": senate_pair[0],
                "senate_seats_r": senate_pair[1],
                "senate_seats_other": 0.0,
            })
            if senate_d:
                sources.add(NATIONAL_EVENT_TICKERS["senate_seats_d"])
            if senate_r:
                sources.add(NATIONAL_EVENT_TICKERS["senate_seats_r"])
        elif senate_d or senate_r:
            diagnostics.append("Senate seat-count ladders were internally inconsistent and were left null")

        margin = self._margin_distribution_expected(
            by_ticker.get(NATIONAL_EVENT_TICKERS["house_popular_vote"])
        )
        if margin is not None:
            d_pct = float(rounded((100.0 + margin) / 2.0))
            r_pct = float(rounded(100.0 - d_pct))
            values.update({
                "house_popular_vote_basis": (
                    "two-party House popular-vote shares implied by the normalized Kalshi "
                    "KXHOUSEPOPVOTEMARGIN mutually exclusive margin ladder"
                ),
                "house_popular_vote_d_pct": d_pct,
                "house_popular_vote_r_pct": r_pct,
                "house_popular_vote_other_pct": 0.0,
                "house_popular_vote_margin_d_minus_r_pct": float(rounded(margin)),
            })
            sources.add(NATIONAL_EVENT_TICKERS["house_popular_vote"])

        return values, sources, diagnostics

    @classmethod
    def _house_records(cls, events: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        priorities: dict[str, int] = {}
        for event in events:
            ticker = str(event.get("event_ticker", "")).strip().upper()
            identity = cls._house_event_identity(ticker)
            if identity is None or cls._is_primary(event):
                continue
            abbr, seat, priority = identity
            priced = cls._party_probabilities(event)
            if not priced:
                continue
            probs, markets = priced
            key = f"{abbr}-{seat:02d}"
            if priority <= priorities.get(key, -1):
                continue
            priorities[key] = priority
            records[key] = {
                "event_ticker": ticker,
                "state_abbreviation": abbr,
                "seat_number": seat,
                **probs,
                "data_quality": cls._event_data_quality(markets),
                "notes": cls._event_note(event, markets),
            }
        return records

    @classmethod
    def _senate_records(cls, events: Iterable[dict[str, Any]]) -> dict[tuple[str, bool], dict[str, Any]]:
        records: dict[tuple[str, bool], dict[str, Any]] = {}
        for event in events:
            ticker = str(event.get("event_ticker", "")).strip().upper()
            match = _SENATE_EVENT_RE.fullmatch(ticker)
            if not match or cls._is_primary(event):
                continue
            abbr, special_marker = match.groups()
            special = bool(special_marker) or abbr in SPECIAL_SENATE_STATES
            key = (abbr, special)
            if key not in EXPECTED_SENATE_KEYS:
                continue
            priced = cls._party_probabilities(event)
            if not priced:
                continue
            probs, markets = priced
            records[key] = {
                "event_ticker": ticker,
                "state_abbreviation": abbr,
                "special": special,
                **probs,
                "data_quality": cls._event_data_quality(markets),
                "notes": cls._event_note(event, markets),
            }
        return records

    @staticmethod
    def _is_primary(event: dict[str, Any]) -> bool:
        text = " ".join(str(event.get(field, "")) for field in (
            "event_ticker", "series_ticker", "title", "sub_title"
        )).casefold()
        return any(word in text for word in _PRIMARY_WORDS)

    @classmethod
    def _party_probabilities(
        cls, event: dict[str, Any] | None
    ) -> tuple[dict[str, float], list[PricedMarket]] | None:
        if not isinstance(event, dict) or not event.get("mutually_exclusive"):
            return None
        markets = event.get("markets")
        if not isinstance(markets, list):
            return None
        priced: list[PricedMarket] = []
        unknown_priced = False
        recognized_unpriced = False
        for market in markets:
            if not isinstance(market, dict):
                continue
            party = cls._market_party(market)
            price = cls._market_probability(market)
            if price is None:
                # A known candidate/party with no readable market price makes the
                # mutually exclusive distribution incomplete. Do not silently
                # renormalize the remaining candidates to 100%.
                if party:
                    recognized_unpriced = True
                continue
            label = cls._market_label(market)
            if not party:
                unknown_priced = True
                continue
            priced.append(PricedMarket(
                ticker=str(market.get("ticker", "")),
                probability_pct=price[0],
                price_basis=price[1],
                label=label,
                party=party,
            ))
        if unknown_priced or recognized_unpriced or not priced:
            return None
        totals = {"D": 0.0, "R": 0.0, "Other": 0.0}
        for market in priced:
            totals[market.party] += market.probability_pct
        total = sum(totals.values())
        if total <= 0:
            return None
        normalized = {
            party: float(rounded(100.0 * value / total))
            for party, value in totals.items()
        }
        # Force the exact residual after independent rounding.
        normalized["Other"] = float(rounded(100.0 - normalized["D"] - normalized["R"]))
        if normalized["Other"] < -0.05:
            return None
        normalized["Other"] = max(0.0, normalized["Other"])
        return normalized, priced

    @staticmethod
    def _market_party(market: dict[str, Any]) -> str:
        ticker = str(market.get("ticker", "")).strip().upper()
        suffix = ticker.rsplit("-", 1)[-1]
        if suffix in {"D", "DEM"}:
            return "D"
        if suffix in {"R", "REP", "GOP"}:
            return "R"
        if suffix in {"I", "IND", "L", "LIB", "G", "GRN", "O", "OTH"}:
            return "Other"
        text = " ".join(str(market.get(field, "")) for field in (
            "yes_sub_title", "subtitle", "title", "primary_participant_key", "rules_primary"
        )).casefold()
        d = bool(re.search(r"\b(democrat(?:ic)?(?: party)?|dem)\b", text))
        r = bool(re.search(r"\b(republican(?: party)?|gop|rep)\b", text))
        other = bool(re.search(r"\b(independent|libertarian|green party|other party|nonpartisan)\b", text))
        if sum((d, r, other)) != 1:
            return ""
        return "D" if d else "R" if r else "Other"

    @staticmethod
    def _market_label(market: dict[str, Any]) -> str:
        for field in ("yes_sub_title", "subtitle", "title", "functional_strike"):
            value = str(market.get(field, "")).strip()
            if value:
                return value
        return str(market.get("ticker", "")).strip()

    @staticmethod
    def _dollar(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number > 1:
            return None
        return number

    @classmethod
    def _market_probability(cls, market: dict[str, Any]) -> tuple[float, str] | None:
        yes_bid = cls._dollar(market.get("yes_bid_dollars"))
        yes_ask = cls._dollar(market.get("yes_ask_dollars"))
        no_bid = cls._dollar(market.get("no_bid_dollars"))
        no_ask = cls._dollar(market.get("no_ask_dollars"))
        last = cls._dollar(market.get("last_price_dollars"))

        # Kalshi uses 0.0000/1.0000 when a side of the book is empty.  Those are
        # bounds, not a two-sided 50% market, so exclude them from midpoint
        # construction.  NO quotes are converted to their YES equivalents.
        bid_candidates: list[tuple[float, str]] = []
        ask_candidates: list[tuple[float, str]] = []
        if yes_bid is not None and yes_bid > 0.0:
            bid_candidates.append((yes_bid, "yes"))
        if no_ask is not None and no_ask < 1.0:
            bid_candidates.append((1.0 - no_ask, "no-implied"))
        if yes_ask is not None and yes_ask < 1.0:
            ask_candidates.append((yes_ask, "yes"))
        if no_bid is not None and no_bid > 0.0:
            ask_candidates.append((1.0 - no_bid, "no-implied"))

        best_bid = max(bid_candidates, default=None, key=lambda item: item[0])
        best_ask = min(ask_candidates, default=None, key=lambda item: item[0])
        if best_bid and best_ask and best_bid[0] <= best_ask[0]:
            basis = (
                "yes bid/ask midpoint"
                if best_bid[1] == best_ask[1] == "yes"
                else "yes-equivalent bid/ask midpoint"
            )
            return float(rounded(50.0 * (best_bid[0] + best_ask[0]))), basis
        if last is not None:
            return float(rounded(100.0 * last)), "last traded yes price"
        if best_bid:
            basis = "one-sided yes bid" if best_bid[1] == "yes" else "one-sided no-implied yes bid"
            return float(rounded(100.0 * best_bid[0])), basis
        if best_ask:
            basis = "one-sided yes ask" if best_ask[1] == "yes" else "one-sided no-implied yes ask"
            return float(rounded(100.0 * best_ask[0])), basis
        return None

    @classmethod
    def _distribution_expected(
        cls, event: dict[str, Any] | None, *, upper_bound: float
    ) -> tuple[float, list[PricedMarket]] | None:
        if not isinstance(event, dict) or not event.get("mutually_exclusive"):
            return None
        markets = event.get("markets")
        if not isinstance(markets, list):
            return None
        raw: list[tuple[NumericBucket, PricedMarket]] = []
        finite_widths: list[float] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            price = cls._market_probability(market)
            if price is None:
                # An expected value requires the complete mutually exclusive
                # ladder. Dropping an unpriced bucket and renormalizing the rest
                # can materially bias the result, so leave the metric null.
                return None
            label = cls._market_label(market)
            bucket = cls._numeric_bucket(label, price[0])
            if bucket is None:
                return None
            priced = PricedMarket(
                ticker=str(market.get("ticker", "")), probability_pct=price[0],
                price_basis=price[1], label=label,
            )
            raw.append((bucket, priced))
            if bucket.kind == "range" and bucket.low is not None and bucket.high is not None:
                finite_widths.append(abs(bucket.high - bucket.low) + 1.0)
            elif bucket.kind == "exact":
                finite_widths.append(1.0)
        if not raw:
            return None
        typical_width = float(median(finite_widths)) if finite_widths else 1.0
        total_price = sum(item[0].probability_pct for item in raw)
        if total_price <= 0:
            return None
        expected = 0.0
        priced_markets: list[PricedMarket] = []
        for bucket, priced in raw:
            representative = cls._bucket_representative(
                bucket, typical_width=typical_width, upper_bound=upper_bound, integer=True
            )
            expected += representative * bucket.probability_pct / total_price
            priced_markets.append(priced)
        return float(rounded(expected)), priced_markets

    @staticmethod
    def _numeric_bucket(label: str, probability_pct: float) -> NumericBucket | None:
        cleaned = label.strip().replace("%", "")
        lower = cleaned.casefold()
        range_match = _RANGE_RE.search(cleaned)
        if range_match:
            low, high = sorted((float(range_match.group(1)), float(range_match.group(2))))
            return NumericBucket(label, probability_pct, "range", low, high)
        numbers = [float(value) for value in _NUMBER_RE.findall(cleaned)]
        if not numbers:
            return None
        value = numbers[0]
        if any(word in lower for word in ("below", "less than", "fewer than", "under", "<")):
            return NumericBucket(label, probability_pct, "below", None, value)
        if any(word in lower for word in ("above", "more than", "over", "greater than", ">")):
            return NumericBucket(label, probability_pct, "above", value, None)
        if any(word in lower for word in ("or fewer", "or less", "at most")):
            return NumericBucket(label, probability_pct, "at_most", None, value)
        if any(word in lower for word in ("or more", "at least")):
            return NumericBucket(label, probability_pct, "at_least", value, None)
        return NumericBucket(label, probability_pct, "exact", value, value)

    @staticmethod
    def _bucket_representative(
        bucket: NumericBucket, *, typical_width: float, upper_bound: float, integer: bool
    ) -> float:
        if bucket.kind == "range" and bucket.low is not None and bucket.high is not None:
            return (bucket.low + bucket.high) / 2.0
        if bucket.kind == "exact" and bucket.low is not None:
            return bucket.low
        if bucket.kind in {"below", "at_most"} and bucket.high is not None:
            adjustment = (typical_width + 1.0) / 2.0 if integer else typical_width / 2.0
            boundary = bucket.high + (1.0 if bucket.kind == "at_most" and integer else 0.0)
            return max(0.0, boundary - adjustment)
        if bucket.kind in {"above", "at_least"} and bucket.low is not None:
            adjustment = (typical_width + 1.0) / 2.0 if integer else typical_width / 2.0
            boundary = bucket.low - (1.0 if bucket.kind == "at_least" and integer else 0.0)
            return min(upper_bound, boundary + adjustment)
        raise ValueError(f"unhandled bucket: {bucket}")

    @staticmethod
    def _reconcile_party_seats(
        d_distribution: tuple[float, list[PricedMarket]] | None,
        r_distribution: tuple[float, list[PricedMarket]] | None,
        *,
        total: float,
        tolerance: float,
    ) -> tuple[float, float] | None:
        if d_distribution and r_distribution:
            d_raw, r_raw = d_distribution[0], r_distribution[0]
            if abs((d_raw + r_raw) - total) > tolerance:
                return None
            d = (d_raw + (total - r_raw)) / 2.0
            r = total - d
        elif d_distribution:
            d = d_distribution[0]
            r = total - d
        elif r_distribution:
            r = r_distribution[0]
            d = total - r
        else:
            return None
        if min(d, r) < 0 or max(d, r) > total:
            return None
        d = float(rounded(d))
        r = float(rounded(total - d))
        return d, r

    @classmethod
    def _margin_distribution_expected(cls, event: dict[str, Any] | None) -> float | None:
        if not isinstance(event, dict) or not event.get("mutually_exclusive"):
            return None
        markets = event.get("markets")
        if not isinstance(markets, list):
            return None
        parsed: list[tuple[NumericBucket, float, float]] = []
        widths: list[float] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            price = cls._market_probability(market)
            if price is None:
                # As with seat ladders, do not construct a national expectation
                # from an incomplete mutually exclusive margin distribution.
                return None
            label = cls._market_label(market)
            lower = label.casefold()
            if "tie" in lower or "even" in lower:
                bucket = NumericBucket(label, price[0], "exact", 0.0, 0.0)
                sign = 1.0
            else:
                d = bool(re.search(r"\b(democrat(?:s|ic)?|dem)\b", lower))
                r = bool(re.search(r"\b(republican(?:s)?|gop|rep)\b", lower))
                if d == r:
                    return None
                bucket = cls._numeric_bucket(label, price[0])
                if bucket is None:
                    return None
                sign = 1.0 if d else -1.0
            if bucket.kind == "range" and bucket.low is not None and bucket.high is not None:
                widths.append(abs(bucket.high - bucket.low))
            parsed.append((bucket, sign, price[0]))
        if not parsed:
            return None
        typical_width = float(median([width for width in widths if width > 0])) if any(
            width > 0 for width in widths
        ) else 2.0
        total_price = sum(price for _, _, price in parsed)
        if total_price <= 0:
            return None
        expected = 0.0
        for bucket, sign, price in parsed:
            magnitude = cls._bucket_representative(
                bucket, typical_width=typical_width, upper_bound=100.0, integer=False
            )
            expected += sign * magnitude * price / total_price
        if not -100.0 <= expected <= 100.0:
            return None
        return float(rounded(expected))

    @staticmethod
    def _event_data_quality(markets: list[PricedMarket]) -> str:
        bases = {market.price_basis for market in markets}
        if bases and all("bid/ask midpoint" in basis for basis in bases):
            return "published_market_midpoint"
        if any("bid/ask midpoint" in basis for basis in bases):
            return "published_market_mixed_prices"
        return "published_market_last_or_one_sided_price"

    @staticmethod
    def _event_note(event: dict[str, Any], markets: list[PricedMarket]) -> str:
        title = str(event.get("title", "")).strip()
        basis = sorted({market.price_basis for market in markets})
        tickers = ", ".join(sorted(market.ticker for market in markets))
        return (
            f"Kalshi mutually exclusive event {event.get('event_ticker', '')}: {title}. "
            f"Outcome prices normalized to 100%; price basis: {', '.join(basis)}. "
            f"Markets: {tickers}."
        )

    @staticmethod
    def _canonical_event(event: dict[str, Any]) -> dict[str, Any]:
        markets = []
        for market in event.get("markets", []) if isinstance(event.get("markets"), list) else []:
            if not isinstance(market, dict):
                continue
            markets.append({
                field: market.get(field, "") for field in (
                    "ticker", "event_ticker", "yes_sub_title", "subtitle", "title",
                    "primary_participant_key", "yes_bid_dollars", "yes_ask_dollars",
                    "no_bid_dollars", "no_ask_dollars", "last_price_dollars",
                    "strike_type", "floor_strike", "cap_strike",
                    "functional_strike", "custom_strike", "status", "is_provisional",
                )
            })
        return {
            "event_ticker": str(event.get("event_ticker", "")),
            "series_ticker": str(event.get("series_ticker", "")),
            "title": str(event.get("title", "")),
            "sub_title": str(event.get("sub_title", "")),
            "mutually_exclusive": bool(event.get("mutually_exclusive")),
            "markets": sorted(markets, key=lambda market: str(market.get("ticker", ""))),
        }
