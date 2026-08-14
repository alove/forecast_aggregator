import json
import unittest

from forecast_collector.errors import SourceFormatError
from forecast_collector.http import HttpResponse
from forecast_collector.schema import validate_rows
from forecast_collector.sources.race_to_the_wh import (
    RaceToTheWHSource,
    extract_infogram_tables,
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
        self.assertTrue(run_id.startswith("race-to-the-wh-2026-08-12-"))
        self.assertEqual(forecast_date, "2026-08-12")
        self.assertEqual(diagnostics["vendor_updated_at_utc"], "2026-08-12T17:45:09+00:00")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["vendor_updated_at_utc"], "2026-08-12T17:45:09+00:00")
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


if __name__ == "__main__":
    unittest.main()
