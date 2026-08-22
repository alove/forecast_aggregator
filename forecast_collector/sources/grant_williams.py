from __future__ import annotations

from typing import Any

from ..date_utils import canonical_date_or_blank
from ..errors import SourceFormatError
from ..http import HttpClient
from ..models import RawArtifact, SourceResult
from ..schema import (
    blank_row,
    pct_from_margin_dem,
    pct_from_unit,
    residual_pct,
    rounded,
)
from ..states import congressional_district_code, plain_house_seat, plain_senate_seat, resolve_state
from .base import ForecastSource


class GrantWilliamsSource(ForecastSource):
    name = "Grant Williams"
    slug = "grant-williams"
    house_url = "https://raw.githubusercontent.com/grantbw4/2026-midterms-forecast/master/outputs/forecast.json"
    senate_url = "https://raw.githubusercontent.com/grantbw4/2026-midterms-forecast/master/outputs/senate_forecast.json"
    dashboard_url = "https://grantbw4.github.io/2026-midterms-forecast/"

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
            raise SourceFormatError("Grant Williams publishes a latest bundle, not a backfill timeline")
        # The two public files are committed as one forecast bundle. Fetching
        # through a moving branch can still briefly straddle a deployment, so
        # retry the pair once when their atomic identifiers do not match.
        for bundle_attempt in range(2):
            house_response = client.get(self.house_url)
            senate_response = client.get(self.senate_url)
            try:
                house = house_response.json()
                senate = senate_response.json()
            except Exception as exc:
                raise SourceFormatError("Grant Williams output is not valid JSON") from exc
            try:
                rows, run_id = self.normalize(
                    house,
                    senate,
                    observed_datetime_utc=observed_datetime_utc,
                    include_house_districts=include_house_districts,
                    include_senate_races=include_senate_races,
                    require_complete_counts=True,
                )
                break
            except SourceFormatError as exc:
                atomic_mismatch = "do not match; retry later" in str(exc)
                if bundle_attempt == 0 and atomic_mismatch:
                    continue
                raise
        else:  # pragma: no cover - loop either breaks or raises
            raise SourceFormatError("Grant Williams bundle could not be read atomically")
        return SourceResult(
            source_name=self.name,
            rows=rows,
            raw_artifacts=[
                RawArtifact("forecast.json", house_response.content),
                RawArtifact("senate_forecast.json", senate_response.content),
            ],
            details={
                "forecast_dates": [rows[0]["vendor_forecast_date"]]
                if rows and rows[0].get("vendor_forecast_date") else [],
                "run_ids": [run_id],
                "model_status": house["metadata"].get("model_status", ""),
            },
        )

    def normalize(
        self,
        house: dict[str, Any],
        senate: dict[str, Any],
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
    ) -> tuple[list[dict[str, Any]], str]:
        try:
            hm = house["metadata"]
            sm = senate["metadata"]
            hs = house["summary"]
            ss = senate["summary"]
            districts = house["districts"]
            races = senate["races"]
        except (KeyError, TypeError) as exc:
            raise SourceFormatError(f"Grant Williams bundle is missing required object: {exc}") from exc

        run_id = str(hm.get("run_id", ""))
        if not run_id or run_id != str(sm.get("run_id", "")):
            raise SourceFormatError("House and Senate JSON run_id values do not match; retry later")
        updated_at = str(hm.get("updated_at", ""))
        if not updated_at or updated_at != str(sm.get("updated_at", "")):
            raise SourceFormatError("House and Senate JSON updated_at values do not match; retry later")
        forecast_date = canonical_date_or_blank(updated_at[:10])
        election_date = canonical_date_or_blank(
            hm.get("election_date", "2026-11-03")
        )
        if not election_date:
            raise SourceFormatError(
                f"Grant Williams election_date is not trustworthy: "
                f"{hm.get('election_date')!r}"
            )

        if require_complete_counts and len(districts) != 435:
            raise SourceFormatError(f"expected 435 Grant Williams districts, found {len(districts)}")
        expected_races = int(ss.get("seats_up", 35))
        if require_complete_counts and len(races) != expected_races:
            raise SourceFormatError(
                f"expected {expected_races} Grant Williams Senate races, found {len(races)}"
            )

        house_d = rounded(hs["mean_dem_seats"])
        house_r = rounded(435.0 - float(house_d))
        senate_d = rounded(ss["mean_dem_seats"])
        senate_r = rounded(100.0 - float(senate_d))
        house_control_d = pct_from_unit(hs["prob_dem_majority"])
        house_control_r = pct_from_unit(hs["prob_rep_majority"])
        senate_control_d = pct_from_unit(ss["prob_dem_control"])
        senate_control_r = pct_from_unit(ss["prob_rep_control"])
        election_day = house.get("national_model", {}).get("election_day", {})
        national_margin_raw = election_day.get("mean")
        if national_margin_raw in (None, ""):
            national_margin_raw = hs.get("election_day_national_margin")
        if national_margin_raw in (None, ""):
            legacy_environment = house.get("national_environment", {})
            if isinstance(legacy_environment, dict):
                legacy_election_day = legacy_environment.get("election_day", {})
                if isinstance(legacy_election_day, dict):
                    national_margin_raw = legacy_election_day.get("mean")
        if national_margin_raw in (None, ""):
            national_margin_raw = hs.get("national_environment")
        if national_margin_raw in (None, ""):
            raise SourceFormatError(
                "Grant Williams bundle is missing the election-day national House margin"
            )
        national_margin = rounded(national_margin_raw)
        house_vote_d, house_vote_r, house_vote_o = pct_from_margin_dem(national_margin)
        election_day_ci = election_day.get("ci_90", ["", ""])
        if not isinstance(election_day_ci, list) or len(election_day_ci) < 2:
            legacy_environment = house.get("national_environment", {})
            legacy_election_day = (
                legacy_environment.get("election_day", {})
                if isinstance(legacy_environment, dict) else {}
            )
            election_day_ci = (
                legacy_election_day.get("ci_90", ["", ""])
                if isinstance(legacy_election_day, dict) else ["", ""]
            )
        if not isinstance(election_day_ci, list) or len(election_day_ci) < 2:
            election_day_ci = ["", ""]
        vote_low = pct_from_margin_dem(election_day_ci[0])[0] if election_day_ci[0] != "" else ""
        vote_high = pct_from_margin_dem(election_day_ci[1])[0] if election_day_ci[1] != "" else ""
        warnings = hm.get("warnings", [])
        warning_text = "; ".join(str(item) for item in warnings) if warnings else ""
        statuses = sorted({str(hm.get("model_status", "")), str(sm.get("model_status", ""))} - {""})

        model_version = str(hm.get("model_version", ""))
        common = {
            "observed_datetime_utc": observed_datetime_utc,
            "vendor": self.name,
            "vendor_model": f"2026 Midterms Forecast v{model_version}" if model_version else "2026 Midterms Forecast",
            "vendor_run_id": run_id,
            "vendor_forecast_date": forecast_date,
            "vendor_updated_at_utc": updated_at,
            "model_status": "+".join(statuses),
            "election_date": election_date,
            "house_seats_basis": "posterior mean expected seats",
            "house_seats_d": house_d,
            "house_seats_r": house_r,
            "house_seats_other": 0.0,
            "house_seats_d_median": rounded(hs.get("median_dem_seats")),
            "house_seats_r_median": rounded(hs.get("median_rep_seats")),
            "house_seats_other_median": 0.0,
            "house_seats_d_p05": rounded(hs.get("ci_90_low")),
            "house_seats_d_p95": rounded(hs.get("ci_90_high")),
            "house_control_d_pct": house_control_d,
            "house_control_r_pct": house_control_r,
            "house_control_other_pct": residual_pct(house_control_d, house_control_r),
            "house_popular_vote_basis": "Democratic two-party election-day House popular-vote margin",
            "house_popular_vote_d_pct": house_vote_d,
            "house_popular_vote_r_pct": house_vote_r,
            "house_popular_vote_other_pct": house_vote_o,
            "house_popular_vote_margin_d_minus_r_pct": national_margin,
            "house_popular_vote_d_p05": vote_low,
            "house_popular_vote_d_p95": vote_high,
            "senate_seats_basis": "posterior mean caucus seats; aligned independents are included in the Democratic caucus",
            "senate_seats_d": senate_d,
            "senate_seats_r": senate_r,
            "senate_seats_other": 0.0,
            "senate_seats_d_median": rounded(ss.get("median_dem_seats")),
            "senate_seats_r_median": rounded(100.0 - float(ss["median_dem_seats"])),
            "senate_seats_other_median": 0.0,
            "senate_seats_d_p05": rounded(ss.get("ci_90_low")),
            "senate_seats_d_p95": rounded(ss.get("ci_90_high")),
            "senate_control_d_pct": senate_control_d,
            "senate_control_r_pct": senate_control_r,
            "senate_control_other_pct": residual_pct(senate_control_d, senate_control_r),
        }

        national = blank_row()
        national.update(common)
        national.update({
            "row_type": "national",
            "source_record_id": "national",
            "source_url": self.dashboard_url,
            "source_file": "forecast.json + senate_forecast.json",
            "data_quality": str(hm.get("model_status", "")),
            "notes": warning_text,
        })
        result: list[dict[str, Any]] = [national]

        if include_house_districts:
            for source in districts:
                try:
                    abbr, state_name, fips = resolve_state(str(source["state"]))
                    seat = int(source["district_number"])
                    d_prob = pct_from_unit(source["prob_dem"])
                    d_vote = rounded(source["mean_vote_share"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise SourceFormatError(f"invalid Grant Williams House district: {source}") from exc
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "house_district",
                    "source_record_id": str(source.get("id", f"{abbr}-{seat:02d}")),
                    "source_url": self.house_url,
                    "source_file": "forecast.json",
                    "congressional_district": congressional_district_code(abbr, seat),
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "house_seat_number": seat,
                    "house_seat": plain_house_seat(abbr, seat),
                    "house_d_pct": d_prob,
                    "house_r_pct": rounded(100.0 - float(d_prob)),
                    "house_other_pct": 0.0,
                    "house_d_vote_pct": d_vote,
                    "house_r_vote_pct": rounded(100.0 - float(d_vote)),
                    "house_other_vote_pct": 0.0,
                    "house_vote_d_p05": rounded(source.get("ci_90_low")),
                    "house_vote_d_p95": rounded(source.get("ci_90_high")),
                    "house_rating": str(source.get("category", "")).replace("_", " ").title(),
                    "data_quality": str(source.get("data_quality", "")),
                    "notes": f"polls_used={source.get('polls_used', '')}; open_seat={source.get('open_seat', '')}",
                })
                result.append(row)

        if include_senate_races:
            for source in races:
                try:
                    abbr, state_name, fips = resolve_state(str(source["state"]))
                    d_prob = pct_from_unit(source["prob_dem"])
                    d_vote, r_vote, o_vote = pct_from_margin_dem(source["posterior_margin"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise SourceFormatError(f"invalid Grant Williams Senate race: {source}") from exc
                special = bool(source.get("special", False))
                interval = source.get("credible_interval_90", ["", ""])
                d_low = pct_from_margin_dem(interval[0])[0] if len(interval) >= 2 and interval[0] != "" else ""
                d_high = pct_from_margin_dem(interval[1])[0] if len(interval) >= 2 and interval[1] != "" else ""
                record_id = str(source.get("id", abbr)) + (":special" if special else ":regular")
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "senate_race",
                    "source_record_id": record_id,
                    "source_url": self.senate_url,
                    "source_file": "senate_forecast.json",
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "senate_seat": plain_senate_seat(abbr, special=special),
                    "senate_d_pct": d_prob,
                    "senate_r_pct": rounded(100.0 - float(d_prob)),
                    "senate_other_pct": 0.0,
                    "senate_d_vote_pct": d_vote,
                    "senate_r_vote_pct": r_vote,
                    "senate_other_vote_pct": o_vote,
                    "senate_vote_d_p05": d_low,
                    "senate_vote_d_p95": d_high,
                    "senate_rating": str(source.get("category", "")).replace("_", " ").title(),
                    "special_election": special,
                    "data_quality": str(source.get("data_quality", "")),
                    "notes": f"polls_used={source.get('polls_used', '')}; open_seat={source.get('open_seat', '')}",
                })
                result.append(row)
        return result, run_id
