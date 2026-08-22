import json
import unittest
from unittest.mock import patch

from forecast_collector.browser_capture import BrowserCapture
from forecast_collector.errors import SourceFormatError
from forecast_collector.export import split_rows
from forecast_collector.http import HttpResponse
from forecast_collector.schema import validate_rows
from forecast_collector.sources.race_to_the_wh import (
    RaceToTheWHSource,
    _normalize_party_values,
    extract_infogram_tables,
    extract_verified_senate_metric,
    parse_house_table,
    select_house_records,
)


def infogram_payload(*entities):
    return {
        "updatedAt": "2026-08-12T17:45:09Z",
        "elements": {
            "content": {
                "content": {
                    "entities": {
                        f"entity-{index}": entity
                        for index, entity in enumerate(entities, start=1)
                    }
                }
            }
        },
    }


def chart(title, rows, sheet="Sheet 1"):
    return {
        "id": title.lower().replace(" ", "-"),
        "props": {
            "title": title,
            "chartData": {
                "sheetnames": [sheet],
                "data": [rows],
            },
        },
    }


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

SENATE_STATES = [
    "AL", "AK", "AR", "CO", "DE", "GA", "ID", "IL", "IA", "KS",
    "KY", "LA", "ME", "MA", "MI", "MN", "MS", "MT", "NE", "NH",
    "NJ", "NM", "NC", "OK", "OR", "RI", "SC", "SD", "TN", "TX",
    "VA", "WV", "WY", "FL Special", "OH Special",
]


class FakeClient:
    def __init__(self, bodies):
        self.bodies = bodies

    def get(self, url):
        body = self.bodies[url]
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HttpResponse(url, body, "text/html; charset=utf-8", "", "")


class RaceToTheWHInfogramTests(unittest.TestCase):
    def test_extract_infographic_data_from_static_assignment(self):
        payload = {"hello": "world", "value": 7}
        html = (
            "<html><script>window.infographicData="
            + json.dumps(payload)
            + ";window.foo=1;</script></html>"
        ).encode()
        self.assertEqual(RaceToTheWHSource.extract_infographic_data(html), payload)

    def test_discover_forecast_embed_and_avoid_polling_embed(self):
        html = """
        <div class="infogram-embed" data-id="poll-id" data-title="2026 House Polling"></div>
        <div class="infogram-embed" data-id="forecast-id" data-title="2026 House Forecast 3.0"></div>
        """
        url = RaceToTheWHSource.discover_embed_url(
            html, target="house", fallback="https://fallback.invalid"
        )
        self.assertIn("forecast-id", url)
        self.assertNotIn("poll-id", url)

    def test_legacy_infogram_chart_layout_is_supported(self):
        payload = {
            "updated_at": "2026-08-12T17:45:09Z",
            "elements": [
                {
                    "type": "chart",
                    "title": "House District Forecast",
                    "data": [[
                        ["District", "Dem Chance", "GOP Chance", "Margin"],
                        ["NY-01", "60%", "40%", "D+2"],
                    ]],
                    "sheetnames": ["Forecast"],
                }
            ],
        }
        tables = extract_infogram_tables(payload)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].sheet_name, "Forecast")
        records = parse_house_table(tables[0])
        self.assertEqual(records["NY-01"].d_probability, 60.0)

    def test_exact_house_seat_projection_is_unchanged(self):
        self.assertEqual(
            _normalize_party_values(
                "231", "204", target=435.0, percentages=False
            ),
            (231.0, 204.0, 0.0),
        )

    def test_small_house_seat_shortfall_becomes_other(self):
        values = _normalize_party_values(
            "218.4", "216.5", target=435.0, percentages=False
        )
        self.assertEqual(values, (218.4, 216.5, 0.1))
        self.assertEqual(sum(values), 435.0)

    def test_tiny_house_seat_overshoot_is_proportionally_reconciled(self):
        values = _normalize_party_values(
            "218.6", "216.5", target=435.0, percentages=False
        )
        self.assertEqual(values, (218.549759, 216.450241, 0.0))
        self.assertEqual(sum(values), 435.0)
        self.assertAlmostEqual(
            values[0] / values[1], 218.6 / 216.5, places=6
        )

    def test_explicit_zero_other_does_not_preserve_a_435_1_total(self):
        values = _normalize_party_values(
            "218.6", "216.5", "0", target=435.0, percentages=False
        )
        self.assertEqual(values, (218.549759, 216.450241, 0.0))
        self.assertEqual(sum(values), 435.0)

    def test_tiny_control_probability_overshoot_is_reconciled(self):
        values = _normalize_party_values(
            "50.1%", "50.0%", target=100.0, percentages=True
        )
        self.assertEqual(values, (50.04995, 49.95005, 0.0))
        self.assertEqual(sum(values), 100.0)

    def test_positive_other_conflicting_with_major_party_overshoot_is_rejected(self):
        self.assertIsNone(
            _normalize_party_values(
                "218.6", "216.5", "0.1", target=435.0, percentages=False
            )
        )

    def test_large_house_seat_overshoot_is_rejected(self):
        self.assertIsNone(
            _normalize_party_values(
                "219", "217", target=435.0, percentages=False
            )
        )
        self.assertIsNone(
            _normalize_party_values(
                "219", "217", "0", target=435.0, percentages=False
            )
        )

    def test_live_like_435_1_house_topline_passes_schema_validation(self):
        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "50.5%", "218.6"],
                    ["Republicans", "49.5%", "216.5"],
                ],
            )
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "50%", "50"],
                    ["Republicans", "50%", "50"],
                ],
            )
        )
        rows, _, _, diagnostics = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-21T12:53:25+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertFalse(diagnostics["partial"])
        self.assertEqual(len(rows), 1)
        national = rows[0]
        self.assertEqual(national["house_seats_d"], 218.549759)
        self.assertEqual(national["house_seats_r"], 216.450241)
        self.assertEqual(national["house_seats_other"], 0.0)
        self.assertEqual(
            national["house_seats_d"]
            + national["house_seats_r"]
            + national["house_seats_other"],
            435.0,
        )
        self.assertEqual(validate_rows(rows), 1)

    def test_normalize_house_senate_and_popular_vote(self):
        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "73.4%", "231"],
                    ["Republicans", "26.6%", "204"],
                ],
            ),
            chart(
                "National Political Environment / House Popular Vote Projection",
                [
                    ["Measure", "Projection"],
                    ["Election Day House Popular Vote", "D+7.2"],
                ],
            ),
            chart(
                "All 435 House District Forecasts",
                [
                    [
                        "District", "Democratic Win Probability",
                        "Republican Win Probability", "Projected Margin",
                    ],
                    ["NY-01", "55%", "45%", "D+1.8"],
                    ["Alaska At-Large", "42%", "58%", "R+3.5"],
                ],
            ),
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "54%", "51"],
                    ["Republicans", "46%", "49"],
                ],
            ),
            chart(
                "Every 2026 Senate Race Forecast",
                [
                    [
                        "State", "Democratic Win Probability",
                        "Republican Win Probability", "Projected Margin",
                    ],
                    ["Georgia", "61%", "39%", "D+2.4"],
                    ["Ohio Special", "48%", "52%", "R+0.8"],
                ],
            ),
        )
        rows, run_id, forecast_date, diagnostics = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-12T18:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=False,
        )
        self.assertTrue(run_id.startswith("race-to-the-wh-undated-"))
        self.assertEqual(forecast_date, "")
        self.assertEqual(diagnostics["vendor_updated_at_utc"], "2026-08-12T17:45:09+00:00")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["vendor_updated_at_utc"], "2026-08-12T17:45:09+00:00")
        self.assertEqual(rows[0]["vendor_forecast_date"], "")
        self.assertEqual(rows[0]["house_seats_d"], 231.0)
        self.assertEqual(rows[0]["house_seats_r"], 204.0)
        self.assertEqual(rows[0]["senate_seats_d"], 51.0)
        self.assertEqual(rows[0]["senate_control_d_pct"], 54.0)
        self.assertEqual(rows[0]["house_popular_vote_margin_d_minus_r_pct"], 7.2)
        self.assertEqual(rows[1]["congressional_district"], "0201")
        self.assertEqual(rows[2]["congressional_district"], "3601")
        senate_rows = [row for row in rows if row["row_type"] == "senate_race"]
        self.assertTrue(any(row["special_election"] for row in senate_rows))
        self.assertEqual(validate_rows(rows), 5)

    def test_narrative_chart_date_is_not_promoted_to_forecast_date(self):
        house = infogram_payload(
            chart(
                "Model update April 22, 2026 / National House Forecast",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "60%", "225"],
                    ["Republicans", "40%", "210"],
                ],
            )
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "50%", "50"],
                    ["Republicans", "50%", "50"],
                ],
            )
        )
        rows, run_id, forecast_date, diagnostics = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-21T14:00:00+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        self.assertEqual(forecast_date, "")
        self.assertTrue(run_id.startswith("race-to-the-wh-undated-"))
        self.assertEqual(rows[0]["vendor_forecast_date"], "")
        self.assertEqual(diagnostics["forecast_date"], "")

    def test_generic_favorite_probability_uses_projected_margin_party(self):
        table = chart(
            "House District Forecast",
            [
                ["District", "Chance of Winning", "Projected Margin"],
                ["NY-01", "62%", "D+3.1"],
                ["TX-01", "88%", "R+15.0"],
            ],
        )
        payload = infogram_payload(table)
        records = select_house_records(
            extract_infogram_tables(payload), require_complete_counts=False
        )
        self.assertEqual(records["NY-01"].d_probability, 62.0)
        self.assertEqual(records["NY-01"].r_probability, 38.0)
        self.assertEqual(records["TX-01"].d_probability, 12.0)
        self.assertEqual(records["TX-01"].r_probability, 88.0)

    def test_incomplete_race_table_is_rejected_in_strict_mode(self):
        payload = infogram_payload(
            chart(
                "House District Forecast",
                [
                    ["District", "Dem Chance", "GOP Chance", "Margin"],
                    ["NY-01", "60%", "40%", "D+2"],
                ],
            )
        )
        with self.assertRaisesRegex(SourceFormatError, "expected 435"):
            select_house_records(
                extract_infogram_tables(payload), require_complete_counts=True
            )

    def test_complete_435_house_and_35_senate_snapshot_passes_strict_mode(self):
        district_counts = {
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
        house_rows = [[
            "District", "Democratic Win Probability",
            "Republican Win Probability", "Projected Margin",
        ]]
        for state, count in district_counts.items():
            for seat in range(1, count + 1):
                house_rows.append([f"{state}-{seat:02d}", "53%", "47%", "D+1.0"])
        self.assertEqual(len(house_rows) - 1, 435)

        regular_senate_states = [
            "Alabama", "Alaska", "Arkansas", "Colorado", "Delaware", "Georgia",
            "Idaho", "Illinois", "Iowa", "Kansas", "Kentucky", "Louisiana",
            "Maine", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
            "Montana", "Nebraska", "New Hampshire", "New Jersey", "New Mexico",
            "North Carolina", "Oklahoma", "Oregon", "Rhode Island",
            "South Carolina", "South Dakota", "Tennessee", "Texas", "Virginia",
            "West Virginia", "Wyoming",
        ]
        senate_rows = [[
            "State", "Democratic Win Probability",
            "Republican Win Probability", "Projected Margin",
        ]]
        senate_rows.extend(
            [[state, "48%", "52%", "R+1.0"] for state in regular_senate_states]
        )
        senate_rows.extend([
            ["Florida Special", "42%", "58%", "R+5.0"],
            ["Ohio Special", "51%", "49%", "D+0.5"],
        ])
        self.assertEqual(len(senate_rows) - 1, 35)

        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "70%", "230"],
                    ["Republicans", "30%", "205"],
                ],
            ),
            chart(
                "National House Popular Vote Projection",
                [["Measure", "Projection"], ["Election Day House Popular Vote", "D+6"]],
            ),
            chart("All 435 House District Forecasts", house_rows),
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "40%", "49"],
                    ["Republicans", "60%", "51"],
                ],
            ),
            chart("Every 2026 Senate Race Forecast", senate_rows),
        )
        rows, _, _, _ = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-12T18:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
            require_complete_counts=True,
        )
        self.assertEqual(len(rows), 471)
        self.assertEqual(validate_rows(rows), 471)

    def test_collect_full_435_house_and_35_senate_fixture(self):
        house_rows = [[
            "District", "Democratic Win Probability",
            "Republican Win Probability", "Projected Margin",
        ]]
        for state, count in HOUSE_SEATS_BY_STATE.items():
            for district in range(1, count + 1):
                house_rows.append([f"{state}-{district:02d}", "55%", "45%", "D+2"])
        self.assertEqual(len(house_rows) - 1, 435)

        senate_rows = [[
            "State", "Democratic Win Probability",
            "Republican Win Probability", "Projected Margin",
        ]]
        for state in SENATE_STATES:
            senate_rows.append([state, "48%", "52%", "R+1"])
        self.assertEqual(len(senate_rows) - 1, 35)

        house_payload = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "73%", "231"],
                    ["Republicans", "27%", "204"],
                ],
            ),
            chart(
                "National House Popular Vote Projection",
                [["Metric", "Projection"], ["House Popular Vote", "D+7"]],
            ),
            chart("All 435 House District Forecasts", house_rows),
        )
        senate_payload = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "54%", "51"],
                    ["Republicans", "46%", "49"],
                ],
            ),
            chart("Every 2026 Senate Race Forecast", senate_rows),
        )
        source = RaceToTheWHSource()
        house_embed = source.fallback_house_embed_url
        senate_embed = source.fallback_senate_embed_url
        house_html = (
            '<div class="infogram-embed" '
            'data-id="cf74856e-8d17-40f6-b10d-3d23a3ee3cff" '
            'data-title="2026 House Forecast 3.0"></div>'
        )
        senate_html = (
            '<div class="infogram-embed" '
            'data-id="_/vs9b6iAeARko8cuwH51x" '
            'data-title="2026 Senate Forecast 3.0"></div>'
        )
        bodies = {
            source.house_page_url: house_html,
            source.senate_page_url: senate_html,
            house_embed: "window.infographicData=" + json.dumps(house_payload) + ";",
            senate_embed: "window.infographicData=" + json.dumps(senate_payload) + ";",
        }
        result = source.collect(
            FakeClient(bodies),
            observed_datetime_utc="2026-08-12T18:00:00+00:00",
            include_house_districts=True,
            include_senate_races=True,
        )
        self.assertEqual(len(result.rows), 471)
        self.assertEqual(validate_rows(result.rows), 471)
        self.assertEqual(len(result.raw_artifacts), 5)
        self.assertEqual(
            len([row for row in result.rows if row["row_type"] == "house_district"]),
            435,
        )
        self.assertEqual(
            len([row for row in result.rows if row["row_type"] == "senate_race"]),
            35,
        )

    def test_missing_house_district_table_preserves_national_and_senate_data(self):
        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "73%", "231"],
                    ["Republicans", "27%", "204"],
                ],
            ),
            chart(
                "National House Popular Vote Projection",
                [["Metric", "Projection"], ["House Popular Vote", "D+7"]],
            ),
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "54%", "51"],
                    ["Republicans", "46%", "49"],
                ],
            ),
            chart(
                "Senate Race Forecast",
                [
                    ["State", "Democratic Win Probability", "Republican Win Probability", "Projected Margin"],
                    ["Georgia", "61%", "39%", "D+2.4"],
                ],
            ),
        )
        rows, _, _, diagnostics = RaceToTheWHSource().normalize_infograms(
            house, senate,
            observed_datetime_utc="2026-08-14T14:03:27+00:00",
            include_house_districts=True, include_senate_races=True,
            require_complete_counts=True,
        )
        self.assertTrue(diagnostics["partial"])
        self.assertIn("House district forecasts: 0/435 readable", diagnostics["partial_sections"])
        national = next(row for row in rows if row["row_type"] == "national")
        self.assertEqual(national["house_seats_d"], 231.0)
        self.assertEqual(national["house_popular_vote_margin_d_minus_r_pct"], 7.0)
        self.assertEqual(national["senate_seats_d"], 51.0)
        senate_rows = [row for row in rows if row["row_type"] == "senate_race"]
        self.assertEqual(len(senate_rows), 1)
        self.assertEqual(validate_rows(rows), 2)

    def test_generic_live_json_feed_rows_are_supported(self):
        payload = {
            "results": [
                {
                    "District": "NY-01",
                    "Democratic Win Probability": "62%",
                    "Republican Win Probability": "38%",
                    "Projected Margin": "D+3.2",
                },
                {
                    "District": "TX-01",
                    "Democratic Win Probability": "8%",
                    "Republican Win Probability": "92%",
                    "Projected Margin": "R+22.0",
                },
            ]
        }
        tables = extract_infogram_tables(payload)
        records = select_house_records(tables, require_complete_counts=False)
        self.assertEqual(set(records), {"NY-01", "TX-01"})
        self.assertEqual(records["NY-01"].d_probability, 62.0)
        self.assertEqual(records["TX-01"].r_probability, 92.0)

    def test_browser_network_payload_completes_static_shell(self):
        house_rows = []
        for state, count in HOUSE_SEATS_BY_STATE.items():
            for district in range(1, count + 1):
                house_rows.append({
                    "District": f"{state}-{district:02d}",
                    "Democratic Win Probability": "55%",
                    "Republican Win Probability": "45%",
                    "Projected Margin": "D+2",
                })
        senate_rows = [
            {
                "State": state,
                "Democratic Win Probability": "48%",
                "Republican Win Probability": "52%",
                "Projected Margin": "R+1",
            }
            for state in SENATE_STATES
        ]
        house_static = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "73%", "231"],
                    ["Republicans", "27%", "204"],
                ],
            ),
            chart(
                "National House Popular Vote Projection",
                [["Metric", "Projection"], ["House Popular Vote", "D+7"]],
            ),
        )
        senate_static = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "54%", "51"],
                    ["Republicans", "46%", "49"],
                ],
            )
        )
        source = RaceToTheWHSource()
        house_embed = source.fallback_house_embed_url
        senate_embed = source.fallback_senate_embed_url
        bodies = {
            source.house_page_url: '<div data-id="cf74856e-8d17-40f6-b10d-3d23a3ee3cff" data-title="2026 House Forecast"></div>',
            source.senate_page_url: '<div data-id="_/vs9b6iAeARko8cuwH51x" data-title="2026 Senate Forecast"></div>',
            house_embed: "window.infographicData=" + json.dumps(house_static) + ";",
            senate_embed: "window.infographicData=" + json.dumps(senate_static) + ";",
        }
        captures = [
            BrowserCapture(
                requested_urls=[house_embed],
                globals=[{"live_house_feed": house_rows}],
            ),
            BrowserCapture(
                requested_urls=[senate_embed],
                globals=[{"live_senate_feed": senate_rows}],
            ),
        ]
        with patch(
            "forecast_collector.sources.race_to_the_wh.capture_public_pages",
            side_effect=captures,
        ):
            result = source.collect(
                FakeClient(bodies),
                observed_datetime_utc="2026-08-20T20:00:00+00:00",
                include_house_districts=True,
                include_senate_races=True,
            )
        self.assertFalse(result.details["partial"])
        self.assertTrue(result.details["browser_fallback_used"])
        self.assertEqual(result.details["house_record_count"], 435)
        self.assertEqual(result.details["senate_record_count"], 35)
        self.assertEqual(len(result.rows), 471)
        self.assertEqual(validate_rows(result.rows), 471)

    def test_bad_senate_topline_fragment_is_omitted_not_normalized(self):
        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "76.3%", "231"],
                    ["Republicans", "23.7%", "204"],
                ],
            )
        )
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "1%", "0.6"],
                    ["Republicans", "32.637%", "99.4"],
                ],
                sheet="11 data",
            )
        )

        rows, _, _, diagnostics = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-21T22:00:00+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        national = next(row for row in rows if row["row_type"] == "national")
        self.assertEqual(national["senate_seats_d"], "")
        self.assertEqual(national["senate_seats_r"], "")
        self.assertEqual(national["senate_control_d_pct"], "")
        self.assertEqual(national["senate_control_r_pct"], "")
        self.assertIn("Senate seat projection", diagnostics["partial_sections"][0])
        self.assertIn("Senate control probability", diagnostics["partial_sections"][0])
        self.assertIn("rtwh_senate_seats=unavailable", national["notes"])
        self.assertIn("rtwh_senate_control=unavailable", national["notes"])

        exported, _ = split_rows(rows)
        metric_types = {row["metric_type"] for row in exported}
        self.assertNotIn("US Senate Seats by Party", metric_types)
        self.assertNotIn("US Senate Party Probability", metric_types)
        self.assertIn("US House Seats by Party", metric_types)
        self.assertIn("US House Party Probability", metric_types)

    def test_verified_explicit_senate_topline_is_retained_and_marked(self):
        senate = infogram_payload(
            chart(
                "National Senate Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning Senate", "Projected Seats"],
                    ["Democrats", "40%", "49"],
                    ["Republicans", "60%", "51"],
                ],
            )
        )
        tables = extract_infogram_tables(senate)
        seats = extract_verified_senate_metric(tables, metric="seats")
        control = extract_verified_senate_metric(tables, metric="control")
        self.assertEqual(seats["D"], 49.0)
        self.assertEqual(seats["R"], 51.0)
        self.assertEqual(seats["Other"], 0.0)
        self.assertEqual(control["D"], 40.0)
        self.assertEqual(control["R"], 60.0)
        self.assertEqual(control["Other"], 0.0)

        house = infogram_payload(
            chart(
                "National House Majority and Projected Seats",
                [
                    ["Party", "Chance of Winning House", "Projected Seats"],
                    ["Democrats", "55%", "220"],
                    ["Republicans", "45%", "215"],
                ],
            )
        )
        rows, _, _, _ = RaceToTheWHSource().normalize_infograms(
            house,
            senate,
            observed_datetime_utc="2026-08-21T22:00:00+00:00",
            include_house_districts=False,
            include_senate_races=False,
            require_complete_counts=False,
        )
        national = rows[0]
        self.assertIn("rtwh_senate_seats=verified", national["notes"])
        self.assertIn("rtwh_senate_control=verified", national["notes"])
        exported, _ = split_rows(rows)
        senate_rows = [
            row for row in exported
            if row["metric_type"] in {
                "US Senate Seats by Party",
                "US Senate Party Probability",
            }
        ]
        self.assertEqual(len(senate_rows), 6)

    def test_senate_control_does_not_create_other_from_missing_probability(self):
        payload = infogram_payload(
            chart(
                "National Senate Majority Probability",
                [
                    ["Party", "Chance of Winning Senate"],
                    ["Democrats", "1%"],
                    ["Republicans", "32.637%"],
                ],
            )
        )
        self.assertIsNone(
            extract_verified_senate_metric(
                extract_infogram_tables(payload), metric="control"
            )
        )

    def test_senate_seats_rejects_distribution_or_race_scale_values(self):
        payload = infogram_payload(
            chart(
                "National Senate Projected Seats",
                [
                    ["Party", "Projected Seats"],
                    ["Democrats", "0.6"],
                    ["Republicans", "99.4"],
                ],
            )
        )
        self.assertIsNone(
            extract_verified_senate_metric(
                extract_infogram_tables(payload), metric="seats"
            )
        )

    def test_senate_national_metrics_are_never_built_across_text_fragments(self):
        payload = {
            "title": "2026 Senate Forecast",
            "text_a": "Democrats chance of Senate control 41%",
            "text_b": "Republicans chance of Senate control 59%",
            "text_c": "Democrats projected Senate seats 49",
            "text_d": "Republicans projected Senate seats 51",
        }
        tables = extract_infogram_tables(payload)
        self.assertEqual(tables, [])
        self.assertIsNone(extract_verified_senate_metric(tables, metric="control"))
        self.assertIsNone(extract_verified_senate_metric(tables, metric="seats"))



if __name__ == "__main__":
    unittest.main()
