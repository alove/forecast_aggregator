from __future__ import annotations

import csv
from hashlib import sha256
import io
import json
from typing import Any

from ..date_utils import canonical_date_or_blank
from ..errors import SourceFormatError
from ..http import HttpClient
from ..models import RawArtifact, SourceResult
from ..schema import (
    blank_row,
    pct_from_unit,
    probability_rating,
    residual_pct,
    rounded,
)
from ..states import congressional_district_code, plain_house_seat, plain_senate_seat, resolve_state
from .base import ForecastSource


class ElectionStatSheetSource(ForecastSource):
    name = "Election StatSheet"
    slug = "election-statsheet"
    model_name = "Mac Tan 2026 Bayesian Congressional Forecast"
    repo_url = "https://github.com/thisismactan/US-2026/tree/main/output"
    urls = {
        "house_forecast_timeline.csv": "https://raw.githubusercontent.com/thisismactan/US-2026/main/output/house_forecast_timeline.csv",
        "house_district_forecast_timeline.csv": "https://raw.githubusercontent.com/thisismactan/US-2026/main/output/house_district_forecast_timeline.csv",
        "senate_forecast_timeline.csv": "https://raw.githubusercontent.com/thisismactan/US-2026/main/output/senate_forecast_timeline.csv",
        "senate_state_forecast_timeline.csv": "https://raw.githubusercontent.com/thisismactan/US-2026/main/output/senate_state_forecast_timeline.csv",
    }

    required_columns = {
        "house_forecast_timeline.csv": {
            "forecast_date", "party", "prob_majority", "seats_pct_05",
            "seats_avg", "seats_pct_95", "vote_pct_05", "vote_avg", "vote_pct_95",
        },
        "house_district_forecast_timeline.csv": {
            "forecast_date", "year", "state", "seat_number", "r_prob",
            "r_pct_05", "r_avg", "r_pct_95",
        },
        "senate_forecast_timeline.csv": {
            "forecast_date", "party", "prob_majority", "seats_pct_05",
            "seats_avg", "seats_pct_95",
        },
        "senate_state_forecast_timeline.csv": {
            "forecast_date", "state", "seat_name", "r_prob",
            "r_pct_05", "r_avg", "r_pct_95",
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
    def _dedupe_exact_rows(
        rows: list[dict[str, str]],
        *,
        key_fields: tuple[str, ...],
        filename: str,
        forecast_date: str,
    ) -> tuple[list[dict[str, str]], int]:
        """Collapse byte-equivalent duplicate records while rejecting conflicts.

        Election StatSheet's historical files have occasionally contained the
        same race row twice for a date. Silently choosing between *different*
        rows would be unsafe, so only fully identical duplicate dictionaries
        are collapsed.
        """
        seen: dict[tuple[str, ...], dict[str, str]] = {}
        ordered: list[dict[str, str]] = []
        collapsed = 0
        for row in rows:
            key = tuple((row.get(field) or "").strip() for field in key_fields)
            if any(not value for value in key):
                raise SourceFormatError(
                    f"{forecast_date}: {filename} has a blank key field in {key_fields}"
                )
            prior = seen.get(key)
            if prior is None:
                seen[key] = row
                ordered.append(row)
                continue
            if row != prior:
                rendered = ", ".join(
                    f"{field}={value!r}" for field, value in zip(key_fields, key)
                )
                raise SourceFormatError(
                    f"{forecast_date}: {filename} has conflicting duplicate rows for {rendered}"
                )
            collapsed += 1
        return ordered, collapsed

    def _clean_snapshot_rows(
        self,
        date_rows: dict[str, list[dict[str, str]]],
        *,
        forecast_date: str,
    ) -> tuple[dict[str, list[dict[str, str]]], dict[str, int]]:
        key_fields = {
            "house_forecast_timeline.csv": ("party",),
            "house_district_forecast_timeline.csv": ("state", "seat_number"),
            "senate_forecast_timeline.csv": ("party",),
            "senate_state_forecast_timeline.csv": ("state", "seat_name"),
        }
        cleaned: dict[str, list[dict[str, str]]] = {}
        collapsed: dict[str, int] = {}
        for filename, keys in key_fields.items():
            if filename not in date_rows:
                raise SourceFormatError(f"{forecast_date}: missing {filename}")
            cleaned_rows, count = self._dedupe_exact_rows(
                date_rows[filename],
                key_fields=keys,
                filename=filename,
                forecast_date=forecast_date,
            )
            cleaned[filename] = cleaned_rows
            if count:
                collapsed[filename] = count
        return cleaned, collapsed

    @staticmethod
    def _canonicalize_timeline_dates(
        parsed: dict[str, list[dict[str, str]]]
    ) -> dict[str, list[dict[str, str]]]:
        normalized_parsed: dict[str, list[dict[str, str]]] = {}
        for filename, rows in parsed.items():
            normalized_rows: list[dict[str, str]] = []
            for source_row in rows:
                forecast_date = canonical_date_or_blank(source_row.get("forecast_date"))
                if not forecast_date:
                    # An untrusted display date cannot be ordered safely in a
                    # historical timeline, so it is excluded rather than
                    # guessed. Other valid dates in the same file remain usable.
                    continue
                row = dict(source_row)
                row["forecast_date"] = forecast_date
                normalized_rows.append(row)
            normalized_parsed[filename] = normalized_rows
        return normalized_parsed

    def collect(
        self,
        client: HttpClient,
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        backfill: bool = False,
    ) -> SourceResult:
        payloads: dict[str, bytes] = {}
        parsed: dict[str, list[dict[str, str]]] = {}
        for filename, url in self.urls.items():
            response = client.get(url)
            payloads[filename] = response.content
            parsed[filename] = self._parse_csv(
                filename, response.content, self.required_columns[filename]
            )

        parsed = self._canonicalize_timeline_dates(parsed)

        date_sets = [
            {row["forecast_date"] for row in rows if row.get("forecast_date")}
            for rows in parsed.values()
        ]
        common_dates = sorted(set.intersection(*date_sets))
        if not common_dates:
            raise SourceFormatError("Election StatSheet files have no common forecast date")
        selected_dates = common_dates if backfill else [common_dates[-1]]

        normalized: list[dict[str, Any]] = []
        runs: list[str] = []
        duplicate_rows_collapsed: dict[str, dict[str, int]] = {}
        for forecast_date in selected_dates:
            date_rows = {
                filename: [row for row in rows if row["forecast_date"] == forecast_date]
                for filename, rows in parsed.items()
            }
            date_rows, collapsed = self._clean_snapshot_rows(
                date_rows, forecast_date=forecast_date
            )
            if collapsed:
                duplicate_rows_collapsed[forecast_date] = collapsed
            snapshot_rows, run_id = self.normalize_snapshot(
                date_rows,
                forecast_date=forecast_date,
                observed_datetime_utc=observed_datetime_utc,
                include_house_districts=include_house_districts,
                include_senate_races=include_senate_races,
                require_complete_counts=True,
                rows_are_clean=True,
            )
            normalized.extend(snapshot_rows)
            runs.append(run_id)

        return SourceResult(
            source_name=self.name,
            rows=normalized,
            raw_artifacts=[RawArtifact(filename, content) for filename, content in payloads.items()],
            details={
                "forecast_dates": selected_dates,
                "run_ids": runs,
                "latest_available_date": common_dates[-1],
                "duplicate_rows_collapsed": duplicate_rows_collapsed,
            },
        )

    def normalize_snapshot(
        self,
        date_rows: dict[str, list[dict[str, str]]],
        *,
        forecast_date: str,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
        rows_are_clean: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        if not rows_are_clean:
            date_rows, _ = self._clean_snapshot_rows(
                date_rows, forecast_date=forecast_date
            )
        canonical_rows = {
            filename: sorted(
                rows,
                key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
            )
            for filename, rows in date_rows.items()
        }
        canonical = json.dumps(
            canonical_rows, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        run_id = f"ess-{forecast_date}-{sha256(canonical).hexdigest()[:16]}"

        house_by_party = {row["party"].strip().lower(): row for row in date_rows["house_forecast_timeline.csv"]}
        senate_by_party = {row["party"].strip().lower(): row for row in date_rows["senate_forecast_timeline.csv"]}
        if not {"dem", "rep"}.issubset(house_by_party):
            raise SourceFormatError(f"{forecast_date}: House national rows lack dem/rep")
        if not {"dem", "rep"}.issubset(senate_by_party):
            raise SourceFormatError(f"{forecast_date}: Senate national rows lack dem/rep")

        hd, hr = house_by_party["dem"], house_by_party["rep"]
        sd, sr = senate_by_party["dem"], senate_by_party["rep"]
        si = senate_by_party.get("ind", {})
        house_d = rounded(hd["seats_avg"])
        house_r = rounded(hr["seats_avg"])
        house_o = rounded(435.0 - float(house_d) - float(house_r))
        senate_d = rounded(sd["seats_avg"])
        senate_r = rounded(sr["seats_avg"])
        senate_o = rounded(si.get("seats_avg", 100.0 - float(senate_d) - float(senate_r)))
        house_control_d = pct_from_unit(hd["prob_majority"])
        house_control_r = pct_from_unit(hr["prob_majority"])
        senate_control_d = pct_from_unit(sd["prob_majority"])
        senate_control_r = pct_from_unit(sr["prob_majority"])
        house_vote_d = pct_from_unit(hd["vote_avg"])
        house_vote_r = pct_from_unit(hr["vote_avg"])

        common = {
            "observed_datetime_utc": observed_datetime_utc,
            "vendor": self.name,
            "vendor_model": self.model_name,
            "vendor_run_id": run_id,
            "vendor_forecast_date": forecast_date,
            "vendor_updated_at_utc": "",
            "model_status": "published",
            "election_date": "2026-11-03",
            "house_seats_basis": "posterior mean expected seats",
            "house_seats_d": house_d,
            "house_seats_r": house_r,
            "house_seats_other": house_o,
            "house_seats_d_p05": rounded(hd["seats_pct_05"]),
            "house_seats_d_p95": rounded(hd["seats_pct_95"]),
            "house_control_d_pct": house_control_d,
            "house_control_r_pct": house_control_r,
            "house_control_other_pct": residual_pct(house_control_d, house_control_r),
            "house_popular_vote_basis": "two-party House popular vote posterior mean",
            "house_popular_vote_d_pct": house_vote_d,
            "house_popular_vote_r_pct": house_vote_r,
            "house_popular_vote_other_pct": 0.0,
            "house_popular_vote_margin_d_minus_r_pct": rounded(float(house_vote_d) - float(house_vote_r)),
            "house_popular_vote_d_p05": pct_from_unit(hd["vote_pct_05"]),
            "house_popular_vote_d_p95": pct_from_unit(hd["vote_pct_95"]),
            "senate_seats_basis": "posterior mean seats; independents separate where modeled",
            "senate_seats_d": senate_d,
            "senate_seats_r": senate_r,
            "senate_seats_other": senate_o,
            "senate_seats_d_p05": rounded(sd["seats_pct_05"]),
            "senate_seats_d_p95": rounded(sd["seats_pct_95"]),
            "senate_control_d_pct": senate_control_d,
            "senate_control_r_pct": senate_control_r,
            "senate_control_other_pct": residual_pct(senate_control_d, senate_control_r),
        }

        national = blank_row()
        national.update(common)
        national.update({
            "row_type": "national",
            "source_record_id": "national",
            "source_url": self.repo_url,
            "source_file": "house_forecast_timeline.csv + senate_forecast_timeline.csv",
            "notes": "Control 'Other' is residual probability not assigned to a D or R majority/control outcome.",
        })
        result: list[dict[str, Any]] = [national]

        district_rows = date_rows["house_district_forecast_timeline.csv"]
        if require_complete_counts and len(district_rows) != 435:
            raise SourceFormatError(
                f"{forecast_date}: expected 435 House district rows, found {len(district_rows)}"
            )
        if include_house_districts:
            for source in district_rows:
                abbr, state_name, fips = resolve_state(source["state"])
                seat = int(source["seat_number"])
                r_prob = pct_from_unit(source["r_prob"])
                d_prob = rounded(100.0 - float(r_prob))
                r_vote = pct_from_unit(source["r_avg"])
                d_vote = rounded(100.0 - float(r_vote))
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "house_district",
                    "source_record_id": f"{abbr}-{seat:02d}",
                    "source_url": self.urls["house_district_forecast_timeline.csv"],
                    "source_file": "house_district_forecast_timeline.csv",
                    "congressional_district": congressional_district_code(abbr, seat),
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "house_seat_number": seat,
                    "house_seat": plain_house_seat(abbr, seat),
                    "house_d_pct": d_prob,
                    "house_r_pct": r_prob,
                    "house_other_pct": 0.0,
                    "house_d_vote_pct": d_vote,
                    "house_r_vote_pct": r_vote,
                    "house_other_vote_pct": 0.0,
                    "house_vote_d_p05": rounded(100.0 - float(pct_from_unit(source["r_pct_95"]))),
                    "house_vote_d_p95": rounded(100.0 - float(pct_from_unit(source["r_pct_05"]))),
                    "house_rating": probability_rating(d_prob),
                    "notes": "Race rating is derived by this collector from the vendor win probability.",
                })
                result.append(row)

        senate_rows = date_rows["senate_state_forecast_timeline.csv"]
        if require_complete_counts and len(senate_rows) != 35:
            raise SourceFormatError(
                f"{forecast_date}: expected 35 Senate race rows, found {len(senate_rows)}"
            )
        if include_senate_races:
            for source in senate_rows:
                abbr, state_name, fips = resolve_state(source["state"])
                seat_name = source["seat_name"].strip()
                special = seat_name.casefold() != "class ii"
                r_prob = pct_from_unit(source["r_prob"])
                d_prob = rounded(100.0 - float(r_prob))
                r_vote = pct_from_unit(source["r_avg"])
                d_vote = rounded(100.0 - float(r_vote))
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "senate_race",
                    "source_record_id": f"{abbr}:{seat_name}",
                    "source_url": self.urls["senate_state_forecast_timeline.csv"],
                    "source_file": "senate_state_forecast_timeline.csv",
                    "state_fips": fips,
                    "state_abbreviation": abbr,
                    "state": state_name,
                    "senate_seat": plain_senate_seat(abbr, seat_name, special),
                    "senate_d_pct": d_prob,
                    "senate_r_pct": r_prob,
                    "senate_other_pct": 0.0,
                    "senate_d_vote_pct": d_vote,
                    "senate_r_vote_pct": r_vote,
                    "senate_other_vote_pct": 0.0,
                    "senate_vote_d_p05": rounded(100.0 - float(pct_from_unit(source["r_pct_95"]))),
                    "senate_vote_d_p95": rounded(100.0 - float(pct_from_unit(source["r_pct_05"]))),
                    "senate_rating": probability_rating(d_prob),
                    "special_election": special,
                    "notes": "Special-election flag is inferred because the regular 2026 Senate class is Class II; rating is collector-derived.",
                })
                result.append(row)
        return result, run_id
