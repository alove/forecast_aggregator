from __future__ import annotations

import csv
from hashlib import sha256
import io
import json
from typing import Any

from ..errors import SourceFormatError
from ..http import HttpClient
from ..models import RawArtifact, SourceResult
from ..schema import blank_row, probability_rating, residual_pct, rounded
from ..states import congressional_district_code, plain_house_seat, plain_senate_seat, resolve_state
from .base import ForecastSource


class ElectIndexSource(ForecastSource):
    """Adapter for ElectIndex's public machine-readable forecast outputs."""

    name = "ElectIndex"
    slug = "electindex"
    model_name = "ElectIndex 2026 U.S. Election Forecast"
    repo_url = "https://github.com/ElectIndex/26_us_forecast_data/tree/main/output"
    urls = {
        "chambers.csv": (
            "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/"
            "refs/heads/main/output/chambers.csv"
        ),
        "national_indicators.csv": (
            "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/"
            "refs/heads/main/output/national_indicators.csv"
        ),
        "races_summary.csv": (
            "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/"
            "refs/heads/main/output/races_summary.csv"
        ),
    }

    required_columns = {
        "chambers.csv": {
            "chamber", "avg_dem_seats", "avg_gop_seats", "projected_dem_seats",
            "dem_control_pct", "races",
        },
        "national_indicators.csv": {
            "date", "house_pv_dem", "house_pv_rep", "house_pv_oth", "house_pv_margin",
        },
        "races_summary.csv": {
            "race_code", "race_type", "state", "district", "dem_name", "rep_name",
            "dem_prob", "rep_prob", "ind_prob", "rating", "ind_pct", "dem_pct",
            "rep_pct", "oth_pct", "dem_votes", "rep_votes", "oth_votes", "ind_votes",
            "total_votes",
        },
    }

    @staticmethod
    def _parse_csv(filename: str, content: bytes, required: set[str]) -> list[dict[str, str]]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourceFormatError(f"{filename} is not UTF-8") from exc
        reader = csv.DictReader(io.StringIO(text))
        fields = set(reader.fieldnames or [])
        missing = required - fields
        if missing:
            raise SourceFormatError(f"{filename} is missing columns: {sorted(missing)}")
        rows = list(reader)
        if not rows:
            raise SourceFormatError(f"{filename} has no rows")
        return rows

    @staticmethod
    def _single_consistent_row(
        rows: list[dict[str, str]], *, label: str
    ) -> dict[str, str]:
        if not rows:
            raise SourceFormatError(f"ElectIndex snapshot has no {label} row")
        first = rows[0]
        if any(row != first for row in rows[1:]):
            raise SourceFormatError(f"ElectIndex snapshot has conflicting {label} rows")
        return first

    @staticmethod
    def _number(value: Any, *, field: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise SourceFormatError(f"{field} is not numeric: {value!r}") from exc
        if result != result or result in {float("inf"), float("-inf")}:
            raise SourceFormatError(f"{field} is not finite: {value!r}")
        return result

    @classmethod
    def _optional_number(cls, value: Any, *, field: str) -> float | None:
        if value in (None, ""):
            return None
        return cls._number(value, field=field)

    @staticmethod
    def _clamp_pct(value: float, *, field: str) -> float:
        if value < -0.05 or value > 100.05:
            raise SourceFormatError(f"{field} is outside 0..100: {value}")
        return float(rounded(min(100.0, max(0.0, value))))

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
            raise SourceFormatError("ElectIndex publishes a latest snapshot, not a backfill timeline")

        payloads: dict[str, bytes] = {}
        parsed: dict[str, list[dict[str, str]]] = {}
        for filename, url in self.urls.items():
            response = client.get(url)
            payloads[filename] = response.content
            parsed[filename] = self._parse_csv(
                filename, response.content, self.required_columns[filename]
            )

        rows, run_id, model_date = self.normalize(
            parsed,
            observed_datetime_utc=observed_datetime_utc,
            include_house_districts=include_house_districts,
            include_senate_races=include_senate_races,
            require_complete_counts=True,
        )
        return SourceResult(
            source_name=self.name,
            rows=rows,
            raw_artifacts=[RawArtifact(filename, content) for filename, content in payloads.items()],
            details={
                "forecast_dates": [model_date],
                "run_ids": [run_id],
                "model_status": "published",
            },
        )

    def normalize(
        self,
        parsed: dict[str, list[dict[str, str]]],
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
    ) -> tuple[list[dict[str, Any]], str, str]:
        try:
            chamber_rows = parsed["chambers.csv"]
            national_rows = parsed["national_indicators.csv"]
            race_rows = parsed["races_summary.csv"]
        except KeyError as exc:
            raise SourceFormatError(f"ElectIndex snapshot is missing {exc.args[0]}") from exc

        model_dates = sorted(row["date"] for row in national_rows if row.get("date"))
        if not model_dates:
            raise SourceFormatError("national_indicators.csv has no dated rows")
        model_date = model_dates[-1]
        national = self._single_consistent_row(
            [row for row in national_rows if row.get("date") == model_date],
            label=f"national-indicators {model_date}",
        )

        house = self._single_consistent_row(
            [row for row in chamber_rows if row.get("chamber", "").strip().lower() == "house"],
            label="House chamber",
        )
        senate = self._single_consistent_row(
            [row for row in chamber_rows if row.get("chamber", "").strip().lower() == "senate"],
            label="Senate chamber",
        )

        house_d = self._number(house["avg_dem_seats"], field="House avg_dem_seats")
        house_r = self._number(house["avg_gop_seats"], field="House avg_gop_seats")
        house_o = 435.0 - house_d - house_r
        senate_d = self._number(senate["avg_dem_seats"], field="Senate avg_dem_seats")
        senate_r = self._number(senate["avg_gop_seats"], field="Senate avg_gop_seats")
        senate_o = 100.0 - senate_d - senate_r
        if house_o < -0.05 or senate_o < -0.05:
            raise SourceFormatError("ElectIndex expected seat totals exceed chamber size")

        house_control_d = self._clamp_pct(
            self._number(house["dem_control_pct"], field="House dem_control_pct"),
            field="House dem_control_pct",
        )
        senate_control_d = self._clamp_pct(
            self._number(senate["dem_control_pct"], field="Senate dem_control_pct"),
            field="Senate dem_control_pct",
        )
        house_control_r = float(rounded(100.0 - house_control_d))
        senate_control_r = float(rounded(100.0 - senate_control_d))

        pv_d_count = self._number(national["house_pv_dem"], field="house_pv_dem")
        pv_r_count = self._number(national["house_pv_rep"], field="house_pv_rep")
        pv_o_count = self._number(national["house_pv_oth"], field="house_pv_oth")
        pv_total = pv_d_count + pv_r_count + pv_o_count
        if pv_total <= 0:
            raise SourceFormatError("ElectIndex projected national House vote total is not positive")
        house_vote_d = float(rounded(100.0 * pv_d_count / pv_total))
        house_vote_r = float(rounded(100.0 * pv_r_count / pv_total))
        house_vote_o = float(rounded(100.0 - house_vote_d - house_vote_r))
        house_vote_margin = self._number(
            national["house_pv_margin"], field="house_pv_margin"
        )

        filtered = [
            row for row in race_rows
            if row.get("race_type", "").strip().lower() in {"house", "senate"}
        ]
        # The repository also contains gubernatorial races and may retain
        # historical national rows. Neither should manufacture a new
        # congressional run ID when the current congressional snapshot is
        # unchanged.
        canonical_snapshot = {
            "house_chamber": house,
            "senate_chamber": senate,
            "national": national,
            "congressional_races": sorted(
                filtered,
                key=lambda row: (
                    row.get("race_type", "").strip().lower(),
                    row.get("race_code", "").strip(),
                ),
            ),
        }
        canonical = json.dumps(
            canonical_snapshot, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        run_id = f"electindex-{model_date}-{sha256(canonical).hexdigest()[:16]}"
        common = {
            "observed_datetime_utc": observed_datetime_utc,
            "vendor": self.name,
            "vendor_model": self.model_name,
            "vendor_run_id": run_id,
            "vendor_forecast_date": model_date,
            "vendor_updated_at_utc": "",
            "model_status": "published",
            "election_date": "2026-11-03",
            "house_seats_basis": "published mean expected seats (avg_dem_seats / avg_gop_seats)",
            "house_seats_d": rounded(house_d),
            "house_seats_r": rounded(house_r),
            "house_seats_other": rounded(max(0.0, house_o)),
            "house_control_d_pct": house_control_d,
            "house_control_r_pct": house_control_r,
            "house_control_other_pct": 0.0,
            "house_popular_vote_basis": "all-party projected national House vote counts",
            "house_popular_vote_d_pct": house_vote_d,
            "house_popular_vote_r_pct": house_vote_r,
            "house_popular_vote_other_pct": house_vote_o,
            "house_popular_vote_margin_d_minus_r_pct": rounded(house_vote_margin),
            "senate_seats_basis": "published mean expected seats (avg_dem_seats / avg_gop_seats)",
            "senate_seats_d": rounded(senate_d),
            "senate_seats_r": rounded(senate_r),
            "senate_seats_other": rounded(max(0.0, senate_o)),
            "senate_control_d_pct": senate_control_d,
            "senate_control_r_pct": senate_control_r,
            "senate_control_other_pct": 0.0,
        }

        national_row = blank_row()
        national_row.update(common)
        national_row.update({
            "row_type": "national",
            "source_record_id": "national",
            "source_url": self.repo_url,
            "source_file": "chambers.csv + national_indicators.csv",
            "notes": (
                "ElectIndex publishes only Democratic chamber-control probability in chambers.csv; "
                "Republican control is stored as the binary complement. "
                f"Published projected Democratic seats: House={house.get('projected_dem_seats', '')}; "
                f"Senate={senate.get('projected_dem_seats', '')}."
            ),
        })
        result: list[dict[str, Any]] = [national_row]

        house_rows = [row for row in filtered if row["race_type"].strip().lower() == "house"]
        senate_rows = [row for row in filtered if row["race_type"].strip().lower() == "senate"]
        if require_complete_counts:
            expected_house = int(float(house.get("races", 435)))
            expected_senate = int(float(senate.get("races", 35)))
            if len(house_rows) != expected_house or expected_house != 435:
                raise SourceFormatError(
                    f"expected 435 ElectIndex House races, found {len(house_rows)} "
                    f"(chambers.csv says {expected_house})"
                )
            if len(senate_rows) != expected_senate:
                raise SourceFormatError(
                    f"expected {expected_senate} ElectIndex Senate races, found {len(senate_rows)}"
                )

        seen: set[str] = set()
        for source in filtered:
            race_type = source["race_type"].strip().lower()
            race_code = source["race_code"].strip()
            if not race_code:
                raise SourceFormatError("ElectIndex race has a blank race_code")
            if race_code in seen:
                raise SourceFormatError(f"duplicate ElectIndex race_code: {race_code}")
            seen.add(race_code)
            try:
                abbr, state_name, fips = resolve_state(source["state"])
                d_prob, r_prob, o_prob = self._win_probabilities(source)
                d_vote, r_vote, o_vote = self._vote_shares(source)
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceFormatError(f"invalid ElectIndex race {race_code}: {exc}") from exc

            if race_type == "house":
                if not include_house_districts:
                    continue
                district_text = source["district"].strip().upper()
                seat = 1 if district_text in {"AL", "AT-LARGE", "AT LARGE"} else int(float(district_text))
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "house_district",
                    "source_record_id": race_code,
                    "source_url": self.urls["races_summary.csv"],
                    "source_file": "races_summary.csv",
                    "congressional_district": congressional_district_code(abbr, seat),
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "house_seat_number": seat,
                    "house_seat": plain_house_seat(abbr, seat),
                    "house_d_pct": d_prob,
                    "house_r_pct": r_prob,
                    "house_other_pct": o_prob,
                    "house_d_vote_pct": d_vote,
                    "house_r_vote_pct": r_vote,
                    "house_other_vote_pct": o_vote,
                    "house_rating": source.get("rating", ""),
                    "data_quality": "published",
                    "notes": self._candidate_note(source),
                })
                result.append(row)
            else:
                if not include_senate_races:
                    continue
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "senate_race",
                    "source_record_id": race_code,
                    "source_url": self.urls["races_summary.csv"],
                    "source_file": "races_summary.csv",
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "senate_seat": plain_senate_seat(abbr),
                    "senate_d_pct": d_prob,
                    "senate_r_pct": r_prob,
                    "senate_other_pct": o_prob,
                    "senate_d_vote_pct": d_vote,
                    "senate_r_vote_pct": r_vote,
                    "senate_other_vote_pct": o_vote,
                    "senate_rating": source.get("rating", ""),
                    "special_election": "",
                    "data_quality": "published",
                    "notes": self._candidate_note(source),
                })
                result.append(row)

        return result, run_id, model_date

    def _win_probabilities(self, row: dict[str, str]) -> tuple[float, float, float]:
        race = row.get("race_code", "race")
        d = self._clamp_pct(self._number(row["dem_prob"], field=f"{race} dem_prob"), field=f"{race} dem_prob")
        r = self._clamp_pct(self._number(row["rep_prob"], field=f"{race} rep_prob"), field=f"{race} rep_prob")
        ind = self._optional_number(row.get("ind_prob"), field=f"{race} ind_prob") or 0.0
        no_dem = row.get("dem_name", "").strip().casefold().startswith("(no democrat")
        no_rep = row.get("rep_name", "").strip().casefold().startswith("(no republican")

        # ElectIndex currently duplicates an independent candidate's probability
        # in the absent major party's field. Map that candidate to Other, then
        # derive the residual so the normalized D/R/Other triplet is exact.
        if no_dem:
            d = 0.0
            o = 100.0 - r
            if ind > 0 and abs(ind - o) > 0.15:
                raise SourceFormatError(
                    f"{race} independent probability {ind} conflicts with residual {o}"
                )
        elif no_rep:
            r = 0.0
            o = 100.0 - d
            if ind > 0 and abs(ind - o) > 0.15:
                raise SourceFormatError(
                    f"{race} independent probability {ind} conflicts with residual {o}"
                )
        else:
            o = 100.0 - d - r
        if o < -0.05:
            raise SourceFormatError(f"{race} win probabilities exceed 100")
        return d, r, float(rounded(max(0.0, o)))

    def _vote_shares(self, row: dict[str, str]) -> tuple[float, float, float]:
        race = row.get("race_code", "race")
        total = self._optional_number(row.get("total_votes"), field=f"{race} total_votes")
        counts = {
            key: self._optional_number(row.get(key), field=f"{race} {key}")
            for key in ("dem_votes", "rep_votes", "oth_votes", "ind_votes")
        }
        if total is not None and total > 0 and all(value is not None for value in counts.values()):
            component_total = sum(float(value or 0.0) for value in counts.values())
            if component_total <= 0:
                raise SourceFormatError(f"{race} projected component vote total is not positive")
            # A few published rows differ from total_votes by one or two votes.
            # Normalize from the four component counts so the D/R/Other triplet
            # remains a valid probability distribution.
            d = 100.0 * float(counts["dem_votes"] or 0.0) / component_total
            r = 100.0 * float(counts["rep_votes"] or 0.0) / component_total
            o = max(0.0, 100.0 - d - r)
            return float(rounded(d)), float(rounded(r)), float(rounded(o))

        d = self._number(row["dem_pct"], field=f"{race} dem_pct")
        r = self._number(row["rep_pct"], field=f"{race} rep_pct")
        o = self._number(row["oth_pct"], field=f"{race} oth_pct")
        ind = self._optional_number(row.get("ind_pct"), field=f"{race} ind_pct") or 0.0
        no_dem = row.get("dem_name", "").strip().casefold().startswith("(no democrat")
        no_rep = row.get("rep_name", "").strip().casefold().startswith("(no republican")
        if no_dem:
            d = 0.0
        if no_rep:
            r = 0.0
        o += ind
        # Some published percentage columns are rounded. Preserve D and R and
        # use Other as the residual so normalized output always sums to 100.
        o = 100.0 - d - r
        if o < -0.05:
            raise SourceFormatError(f"{race} projected vote shares exceed 100")
        return (
            self._clamp_pct(d, field=f"{race} Democratic vote share"),
            self._clamp_pct(r, field=f"{race} Republican vote share"),
            self._clamp_pct(max(0.0, o), field=f"{race} Other vote share"),
        )

    @staticmethod
    def _candidate_note(row: dict[str, str]) -> str:
        pieces = []
        if row.get("dem_name"):
            pieces.append(f"D={row['dem_name'].strip()}")
        if row.get("rep_name"):
            pieces.append(f"R={row['rep_name'].strip()}")
        if row.get("ind_name"):
            pieces.append(f"Other={row['ind_name'].strip()}")
        return "; ".join(pieces)
