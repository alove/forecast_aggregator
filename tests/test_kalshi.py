from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from forecast_collector.export import (
    split_rows,
    validate_national_rows,
    validate_state_rows,
)
from forecast_collector.http import HttpResponse
from forecast_collector.schema import validate_rows
from forecast_collector.sources.kalshi import (
    HOUSE_SEATS_BY_STATE,
    REGULAR_SENATE_STATES,
    SPECIAL_SENATE_STATES,
    KalshiSource,
)


def market(ticker, label, probability, *, party_text="", last_only=False):
    unit = float(probability) / 100.0
    row = {
        "ticker": ticker,
        "yes_sub_title": label,
        "rules_primary": party_text,
        "status": "active",
    }
    if last_only:
        row["last_price_dollars"] = f"{unit:.4f}"
    else:
        row["yes_bid_dollars"] = f"{max(0.0, unit - 0.005):.4f}"
        row["yes_ask_dollars"] = f"{min(1.0, unit + 0.005):.4f}"
        row["last_price_dollars"] = f"{unit:.4f}"
    return row


def event(ticker, title, markets, *, series="TEST", mutually_exclusive=True):
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": title,
        "sub_title": "",
        "category": "Elections",
        "mutually_exclusive": mutually_exclusive,
        "markets": markets,
    }


def party_event(ticker, title, d, r, other=0.0, *, series="TEST"):
    rows = [
        market(f"{ticker}-D", "Democratic party", d),
        market(f"{ticker}-R", "Republican party", r),
    ]
    if other:
        rows.append(market(f"{ticker}-I", "Independent", other))
    return event(ticker, title, rows, series=series)


def full_fixture():
    events = [
        party_event("CONTROLH-2026", "Which party will win the U.S. House?", 65, 35, series="CONTROLH"),
        party_event("CONTROLS-2026", "Which party will win the U.S. Senate?", 52, 48, series="CONTROLS"),
        event(
            "KXDHOUSESEATS-27", "How many House seats will Democrats hold?",
            [market("KXDHOUSESEATS-27-230", "230", 100)], series="KXDHOUSESEATS",
        ),
        event(
            "KXRHOUSESEATS-27", "How many House seats will Republicans hold?",
            [market("KXRHOUSESEATS-27-205", "205", 100)], series="KXRHOUSESEATS",
        ),
        event(
            "KXDSENATESEATS-27", "How many Senate seats will Democrats hold?",
            [market("KXDSENATESEATS-27-51", "51", 100)], series="KXDSENATESEATS",
        ),
        event(
            "RSENATESEATS-27", "How many Senate seats will Republicans hold?",
            [market("RSENATESEATS-27-49", "49", 100)], series="RSENATESEATS",
        ),
        event(
            "KXHOUSEPOPVOTEMARGIN-27NOV03", "House popular vote margin",
            [market("KXHOUSEPOPVOTEMARGIN-27NOV03-D5", "Democrats, 4 to 6%", 100)],
            series="KXHOUSEPOPVOTEMARGIN",
        ),
    ]
    for abbr, count in HOUSE_SEATS_BY_STATE.items():
        for seat in range(1, count + 1):
            code = "AL" if count == 1 else f"{seat:02d}"
            ticker = f"KXHOUSERACE-{abbr}{code}-26"
            d = 55.0 if (seat + ord(abbr[0])) % 2 else 45.0
            events.append(party_event(
                ticker, f"{abbr}-{code} House winner?", d, 100.0 - d,
                series="KXHOUSERACE",
            ))
    for abbr in sorted(REGULAR_SENATE_STATES):
        ticker = f"SENATE{abbr}-26"
        events.append(party_event(
            ticker, f"{abbr} Senate winner?", 48, 52, series=f"SENATE{abbr}"
        ))
    for abbr in sorted(SPECIAL_SENATE_STATES):
        ticker = f"SENATE{abbr}S-26"
        events.append(party_event(
            ticker, f"{abbr} special Senate winner?", 48, 52, series=f"SENATE{abbr}S"
        ))
    return events


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        payload = self.payloads[url]
        content = json.dumps(payload).encode("utf-8")
        return HttpResponse(url, content, "application/json", "", "")


class KalshiPriceTests(unittest.TestCase):
    def test_midpoint_and_last_trade_fallback(self):
        midpoint = {
            "yes_bid_dollars": "0.4200",
            "yes_ask_dollars": "0.4600",
            "last_price_dollars": "0.9000",
        }
        self.assertEqual(KalshiSource._market_probability(midpoint), (44.0, "yes bid/ask midpoint"))
        last = {"last_price_dollars": "0.3750"}
        self.assertEqual(KalshiSource._market_probability(last), (37.5, "last traded yes price"))

    def test_bucket_expected_value_with_open_tail(self):
        distribution = event(
            "DIST", "Seat ladder",
            [
                market("DIST-B193", "Below 193", 20),
                market("DIST-195", "193-197", 30),
                market("DIST-200", "198-202", 50),
            ],
        )
        result = KalshiSource._distribution_expected(distribution, upper_bound=435)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 196.5)

    def test_margin_ladder_becomes_signed_expected_margin(self):
        distribution = event(
            "MARGIN", "House margin",
            [
                market("MARGIN-D", "Democrats, 4 to 6%", 60),
                market("MARGIN-R", "Republicans, 0 to 2%", 40),
            ],
        )
        self.assertEqual(KalshiSource._margin_distribution_expected(distribution), 2.6)

    def test_candidate_markets_are_aggregated_by_party(self):
        candidate_event = event(
            "KXHOUSERACE-NY01-26", "NY-01 House winner?",
            [
                market("KXHOUSERACE-NY01-26-ALFA", "Alice", 30, party_text="Democratic party"),
                market("KXHOUSERACE-NY01-26-BETA", "Bob", 20, party_text="Democratic party"),
                market("KXHOUSERACE-NY01-26-GAMMA", "Carol", 50, party_text="Republican party"),
            ], series="KXHOUSERACE",
        )
        result = KalshiSource._party_probabilities(candidate_event)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], {"D": 50.0, "R": 50.0, "Other": 0.0})

    def test_unknown_priced_outcome_rejects_event_instead_of_guessing(self):
        bad = event(
            "KXHOUSERACE-NY01-26", "NY-01 House winner?",
            [market("KXHOUSERACE-NY01-26-WHO", "Unknown candidate", 100)],
            series="KXHOUSERACE",
        )
        self.assertIsNone(KalshiSource._party_probabilities(bad))

    def test_empty_book_bounds_do_not_become_false_fifty_percent(self):
        empty = {
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "1.0000",
            "no_bid_dollars": "0.0000",
            "no_ask_dollars": "1.0000",
        }
        self.assertIsNone(KalshiSource._market_probability(empty))

    def test_no_side_quotes_are_converted_to_yes_equivalent_midpoint(self):
        no_side = {
            "no_ask_dollars": "0.6000",  # Implies a 40% YES bid.
            "no_bid_dollars": "0.5000",  # Implies a 50% YES ask.
        }
        self.assertEqual(
            KalshiSource._market_probability(no_side),
            (45.0, "yes-equivalent bid/ask midpoint"),
        )

    def test_recognized_unpriced_candidate_rejects_event(self):
        incomplete = event(
            "KXHOUSERACE-NY01-26", "NY-01 House winner?",
            [
                market("KXHOUSERACE-NY01-26-D", "Democratic party", 60),
                {
                    "ticker": "KXHOUSERACE-NY01-26-R",
                    "yes_sub_title": "Republican party",
                    "yes_bid_dollars": "0.0000",
                    "yes_ask_dollars": "1.0000",
                },
            ],
            series="KXHOUSERACE",
        )
        self.assertIsNone(KalshiSource._party_probabilities(incomplete))

    def test_incomplete_numeric_ladder_is_left_null(self):
        incomplete = event(
            "DIST", "Seat ladder",
            [
                market("DIST-230", "230", 60),
                {
                    "ticker": "DIST-231",
                    "yes_sub_title": "231",
                    "yes_bid_dollars": "0.0000",
                    "yes_ask_dollars": "1.0000",
                },
            ],
        )
        self.assertIsNone(KalshiSource._distribution_expected(incomplete, upper_bound=435))

    def test_party_event_must_be_mutually_exclusive(self):
        not_exclusive = event(
            "CONTROLH-2026", "House control",
            [market("CONTROLH-2026-D", "Democratic party", 60)],
            mutually_exclusive=False,
        )
        self.assertIsNone(KalshiSource._party_probabilities(not_exclusive))


class KalshiNormalizationTests(unittest.TestCase):
    def test_full_435_house_35_senate_and_national_fixture(self):
        rows, run_id, diagnostics = KalshiSource().normalize_events(
            full_fixture(),
            observed_datetime_utc="2026-08-21T14:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=True,
        )
        self.assertEqual(len(rows), 471)
        self.assertEqual(validate_rows(rows), 471)
        self.assertTrue(run_id.startswith("kalshi-undated-"))
        self.assertFalse(diagnostics["partial"])
        self.assertEqual(diagnostics["model_status"], "published")
        self.assertEqual(diagnostics["house_record_count"], 435)
        self.assertEqual(diagnostics["senate_record_count"], 35)
        self.assertEqual(rows[0]["vendor_forecast_date"], "")
        self.assertEqual(rows[0]["vendor_updated_at_utc"], "")
        self.assertEqual(rows[0]["house_seats_d"], 230.0)
        self.assertEqual(rows[0]["house_seats_r"], 205.0)
        self.assertEqual(rows[0]["senate_seats_d"], 51.0)
        self.assertEqual(rows[0]["senate_seats_r"], 49.0)
        self.assertEqual(rows[0]["house_popular_vote_margin_d_minus_r_pct"], 5.0)
        self.assertEqual(rows[0]["house_popular_vote_d_pct"], 52.5)

        national, state = split_rows(rows)
        self.assertEqual(validate_national_rows(national), len(national))
        self.assertEqual(validate_state_rows(state), len(state))
        self.assertEqual({row["metric_type"] for row in national}, {
            "US House Seats by Party", "US House Party Probability",
            "US Senate Seats by Party", "US Senate Party Probability",
            "US House Popular Vote Projection", "US House Popular Vote Margin",
        })
        self.assertEqual({row["metric_type"] for row in state}, {
            "US House District Party Probability", "US Senate Race Party Probability",
        })

    def test_partial_snapshot_keeps_readable_national_values(self):
        rows, _, diagnostics = KalshiSource().normalize_events(
            [party_event("CONTROLH-2026", "House control", 60, 40, series="CONTROLH")],
            observed_datetime_utc="2026-08-21T14:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(validate_rows(rows), 1)
        self.assertTrue(diagnostics["partial"])
        self.assertEqual(rows[0]["model_status"], "published_partial")
        self.assertEqual(rows[0]["house_control_d_pct"], 60.0)
        self.assertEqual(rows[0]["vendor_forecast_date"], "")

    def test_primary_market_is_excluded(self):
        primary = party_event(
            "KXHOUSERACE-NY01-26", "NY-01 Democratic primary winner?", 60, 40,
            series="KXHOUSERACE",
        )
        records = KalshiSource._house_records([primary])
        self.assertEqual(records, {})

    def test_large_inconsistent_seat_ladders_are_left_null(self):
        events = [
            event("KXDHOUSESEATS-27", "D House seats", [market("D-250", "250", 100)]),
            event("KXRHOUSESEATS-27", "R House seats", [market("R-220", "220", 100)]),
        ]
        rows, _, diagnostics = KalshiSource().normalize_events(
            events,
            observed_datetime_utc="2026-08-21T14:00:00+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertEqual(rows[0]["house_seats_d"], "")
        self.assertIn("internally inconsistent", "; ".join(diagnostics["partial_sections"]))

    def test_legacy_house_tickers_and_at_large_are_normalized(self):
        records = KalshiSource._house_records([
            party_event("HOUSECA27-26", "CA-27 House winner?", 70, 30, series="HOUSECA27"),
            party_event("HOUSEAKAL-26", "AK-AL House winner?", 45, 55, series="HOUSEAKAL"),
        ])
        self.assertEqual(set(records), {"CA-27", "AK-01"})
        self.assertEqual(records["CA-27"]["event_ticker"], "HOUSECA27-26")
        self.assertEqual(records["AK-01"]["seat_number"], 1)

    def test_unified_house_event_precedes_legacy_duplicate(self):
        records = KalshiSource._house_records([
            party_event("HOUSECA27-26", "CA-27 legacy", 80, 20, series="HOUSECA27"),
            party_event("KXHOUSERACE-CA27-26", "CA-27 unified", 60, 40, series="KXHOUSERACE"),
        ])
        self.assertEqual(records["CA-27"]["D"], 60.0)
        self.assertEqual(records["CA-27"]["event_ticker"], "KXHOUSERACE-CA27-26")

    def test_run_id_is_deterministic_across_event_order(self):
        fixture = full_fixture()
        first = KalshiSource().normalize_events(
            fixture,
            observed_datetime_utc="2026-08-21T14:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=True,
        )[1]
        second = KalshiSource().normalize_events(
            list(reversed(fixture)),
            observed_datetime_utc="2026-08-22T14:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=True,
        )[1]
        self.assertEqual(first, second)


class KalshiAcquisitionTests(unittest.TestCase):
    def test_house_series_pagination(self):
        first_url = (
            "https://external-api.kalshi.com/trade-api/v2/events?"
            "limit=200&series_ticker=KXHOUSERACE&status=open&with_nested_markets=true"
        )
        second_url = first_url + "&cursor=next-token"
        client = FakeClient({
            first_url: {"events": [{"event_ticker": "A"}], "cursor": "next-token"},
            second_url: {"events": [{"event_ticker": "B"}], "cursor": ""},
        })
        rows = KalshiSource()._fetch_series_events(client, "KXHOUSERACE")
        self.assertEqual([row["event_ticker"] for row in rows], ["A", "B"])
        self.assertEqual(client.urls, [first_url, second_url])

    def test_invalid_event_page_limit_uses_safe_default(self):
        first_url = (
            "https://external-api.kalshi.com/trade-api/v2/events?"
            "limit=200&series_ticker=KXHOUSERACE&status=open&with_nested_markets=true"
        )
        client = FakeClient({first_url: {"events": [], "cursor": ""}})
        with patch.dict(os.environ, {"EFC_KALSHI_MAX_EVENT_PAGES": "not-a-number"}):
            self.assertEqual(KalshiSource()._fetch_series_events(client, "KXHOUSERACE"), [])
        self.assertEqual(client.urls, [first_url])

    def test_legacy_house_fallback_fills_only_missing_districts(self):
        legacy_url = (
            "https://external-api.kalshi.com/trade-api/v2/events/"
            "HOUSECA27-26?with_nested_markets=true"
        )
        client = FakeClient({
            legacy_url: {
                "event": party_event(
                    "HOUSECA27-26", "CA-27 House winner?", 75, 25, series="HOUSECA27"
                )
            }
        })
        events = {
            "KXHOUSERACE-IA02-26": party_event(
                "KXHOUSERACE-IA02-26", "IA-02 House winner?", 45, 55,
                series="KXHOUSERACE",
            )
        }
        result = KalshiSource()._supplement_legacy_house_events(
            client,
            events,
            expected_keys={"IA-02", "CA-27"},
            max_fetches=2,
        )
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["loaded"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertIn("HOUSECA27-26", events)
        self.assertEqual(client.urls, [legacy_url])

    def test_legacy_house_fallback_limit_skips_without_requests(self):
        events = {
            "KXHOUSERACE-IA02-26": party_event(
                "KXHOUSERACE-IA02-26", "IA-02 House winner?", 45, 55,
                series="KXHOUSERACE",
            )
        }
        client = FakeClient({})
        result = KalshiSource()._supplement_legacy_house_events(
            client,
            events,
            expected_keys={"IA-02", "CA-27", "NY-01"},
            max_fetches=1,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(client.urls, [])
        self.assertIn("above EFC_KALSHI_MAX_LEGACY_HOUSE_FETCHES", result["warnings"][0])

    def test_legacy_house_fallback_skips_after_empty_unified_result(self):
        client = FakeClient({})
        result = KalshiSource()._supplement_legacy_house_events(
            client,
            {},
            expected_keys={"CA-27"},
            max_fetches=1,
        )
        self.assertTrue(result["skipped"])
        self.assertEqual(result["attempted"], 0)
        self.assertIn("no readable general-election markets", result["warnings"][0])

    def test_get_event_accepts_deprecated_top_level_markets_shape(self):
        url = "https://external-api.kalshi.com/trade-api/v2/events/CONTROLH-2026?with_nested_markets=true"
        client = FakeClient({
            url: {
                "event": {"event_ticker": "CONTROLH-2026"},
                "markets": [{"ticker": "CONTROLH-2026-D"}],
            }
        })
        loaded = KalshiSource()._fetch_event(client, "CONTROLH-2026")
        self.assertEqual(loaded["markets"][0]["ticker"], "CONTROLH-2026-D")


if __name__ == "__main__":
    unittest.main()
