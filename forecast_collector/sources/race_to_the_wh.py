from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import html as html_lib
import json
import re
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

from ..browser_capture import BrowserCapture, capture_public_pages
from ..errors import SourceFormatError
from ..http import HttpClient
from ..models import RawArtifact, SourceResult
from ..schema import (
    blank_row,
    pct_from_margin_dem,
    probability_rating,
    rounded,
)
from ..states import (
    ABBR_TO_FIPS,
    ABBR_TO_NAME,
    AT_LARGE_STATES,
    congressional_district_code,
    plain_house_seat,
    plain_senate_seat,
    resolve_state,
)
from .base import ForecastSource


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}
_FIPS_TO_ABBR = {fips: abbr for abbr, fips in ABBR_TO_FIPS.items()}
_STATE_NAME_PATTERN = "|".join(
    re.escape(name) for name in sorted(ABBR_TO_NAME.values(), key=len, reverse=True)
)


@dataclass(frozen=True)
class InfogramTable:
    entity_path: str
    title: str
    sheet_name: str
    rows: list[list[str]]

    @property
    def context(self) -> str:
        return _clean_text(f"{self.title} {self.sheet_name}")


@dataclass
class RaceRecord:
    state_abbreviation: str
    state: str
    state_fips: str
    source_record_id: str
    d_probability: float | str = ""
    r_probability: float | str = ""
    other_probability: float | str = ""
    d_vote: float | str = ""
    r_vote: float | str = ""
    other_vote: float | str = ""
    rating: str = ""
    special: bool = False
    seat_number: int | None = None
    source_context: str = ""

    def completeness(self) -> int:
        fields = (
            self.d_probability, self.r_probability, self.other_probability,
            self.d_vote, self.r_vote, self.other_vote,
        )
        return sum(value not in (None, "") for value in fields)


class RaceToTheWHSource(ForecastSource):
    """Adapter for Race to the WH's public House and Senate Infograms.

    Race to the WH does not currently publish a documented CSV or JSON feed for
    the combined forecast. Its public Infogram embeds contain a static
    ``window.infographicData`` JSON payload. This adapter reads that payload,
    identifies the national and race-level tables semantically, and refuses to
    emit a snapshot unless the requested race collections are complete.
    """

    name = "Race to the WH"
    slug = "race-to-the-wh"
    model_name = "Race to the WH 2026 Congressional Forecast"
    house_page_url = "https://www.racetothewh.com/house"
    senate_page_url = "https://www.racetothewh.com/senate/26"
    house_map_page_url = "https://www.racetothewh.com/house/26map"
    fallback_house_embed_url = (
        "https://e.infogram.com/cf74856e-8d17-40f6-b10d-3d23a3ee3cff"
        "?embed_type=responsive_iframe&src=embed"
    )
    fallback_senate_embed_url = (
        "https://e.infogram.com/_/vs9b6iAeARko8cuwH51x"
        "?embed_type=responsive_iframe&src=embed"
    )
    fallback_house_map_embed_url = (
        "https://e.infogram.com/_/lXf0SXsGWnuyOkj93JiS"
        "?embed_type=responsive_iframe&src=embed"
    )

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
            raise SourceFormatError("Race to the WH publishes a latest forecast, not a backfill timeline")

        house_page = client.get(self.house_page_url)
        senate_page = client.get(self.senate_page_url)
        house_embed_url = self.discover_embed_url(
            house_page.text(), kind="house", fallback=self.fallback_house_embed_url
        )
        senate_embed_url = self.discover_embed_url(
            senate_page.text(), kind="senate", fallback=self.fallback_senate_embed_url
        )
        house_embed = client.get(house_embed_url)
        senate_embed = client.get(senate_embed_url)

        house_payloads: list[Any] = [
            self.extract_infographic_data(house_embed.text(), label="House")
        ]
        senate_payloads: list[Any] = [
            self.extract_infographic_data(senate_embed.text(), label="Senate")
        ]
        raw_artifacts = [
            RawArtifact("house_page.html", house_page.content),
            RawArtifact("senate_page.html", senate_page.content),
            RawArtifact("house_infogram.html", house_embed.content),
            RawArtifact("senate_infogram.html", senate_embed.content),
        ]
        collection_warnings: list[str] = []
        house_map_embed_url = ""
        browser_fallback_used = False

        def normalize_current() -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
            return self.normalize_infograms(
                _combine_payloads(house_payloads, label="house"),
                _combine_payloads(senate_payloads, label="senate"),
                observed_datetime_utc=observed_datetime_utc,
                include_house_districts=include_house_districts,
                include_senate_races=include_senate_races,
                require_complete_counts=True,
                house_embed_url=house_embed_url,
                senate_embed_url=senate_embed_url,
            )

        initial_normalize_error: SourceFormatError | None = None
        try:
            rows, run_id, forecast_date, diagnostics = normalize_current()
        except SourceFormatError as exc:
            initial_normalize_error = exc
            rows, run_id, forecast_date = [], "", ""
            diagnostics = {
                "partial": True,
                "partial_sections": [f"static Infogram parse: {exc}"],
                "house_record_count": 0,
                "senate_record_count": 0,
                "national": {},
            }
            collection_warnings.append(f"static Infogram parse was incomplete: {exc}")

        # The main House project has periodically omitted the all-district table
        # from its static shell. The publisher's regional map is a public
        # companion project and can supply the same district forecast data.
        if include_house_districts and diagnostics.get("house_record_count", 0) < 435:
            try:
                house_map_page = client.get(self.house_map_page_url)
                house_map_embed_url = self.discover_embed_url(
                    house_map_page.text(),
                    kind="house",
                    fallback=self.fallback_house_map_embed_url,
                )
                house_map_embed = client.get(house_map_embed_url)
                house_payloads.append(
                    self.extract_infographic_data(
                        house_map_embed.text(), label="House regional map"
                    )
                )
                raw_artifacts.extend([
                    RawArtifact("house_map_page.html", house_map_page.content),
                    RawArtifact("house_map_infogram.html", house_map_embed.content),
                ])
                rows, run_id, forecast_date, diagnostics = normalize_current()
            except Exception as exc:
                collection_warnings.append(
                    f"public House regional companion was unavailable: {type(exc).__name__}: {exc}"
                )

        needs_house_browser = bool(
            (include_house_districts and diagnostics.get("house_record_count", 0) < 435)
            or _missing_chamber_topline(diagnostics, chamber="house")
        )
        needs_senate_browser = bool(
            (include_senate_races and diagnostics.get("senate_record_count", 0) < 35)
            or _missing_chamber_topline(diagnostics, chamber="senate")
        )

        # Infogram supports charts connected to live JSON/database sources. For
        # those projects current data arrives only after JavaScript runs. When
        # the static shell is incomplete, observe the same public browser
        # responses and fold their tables into the normal semantic parser.
        if needs_house_browser:
            house_urls = [house_embed_url]
            if house_map_embed_url:
                house_urls.append(house_map_embed_url)
            capture = capture_public_pages(house_urls)
            browser_fallback_used = browser_fallback_used or capture.available
            collection_warnings.extend(capture.warnings)
            captured_payload = _browser_capture_payload(capture, label="house")
            if captured_payload is not None:
                house_payloads.append(captured_payload)

        if needs_senate_browser:
            capture = capture_public_pages([senate_embed_url])
            browser_fallback_used = browser_fallback_used or capture.available
            collection_warnings.extend(capture.warnings)
            captured_payload = _browser_capture_payload(capture, label="senate")
            if captured_payload is not None:
                senate_payloads.append(captured_payload)

        if needs_house_browser or needs_senate_browser:
            try:
                rows, run_id, forecast_date, diagnostics = normalize_current()
                initial_normalize_error = None
            except SourceFormatError as exc:
                if initial_normalize_error is not None:
                    raise SourceFormatError(
                        f"static and browser-backed Race to the WH parsing failed; "
                        f"static={initial_normalize_error}; browser={exc}"
                    ) from exc
                raise

        diagnostics["collection_warnings"] = list(dict.fromkeys(collection_warnings))
        diagnostics["browser_fallback_used"] = browser_fallback_used
        diagnostics["house_map_embed_url"] = house_map_embed_url
        diagnostics_bytes = json.dumps(
            diagnostics, sort_keys=True, indent=2, ensure_ascii=False
        ).encode("utf-8")
        raw_artifacts.append(RawArtifact("extracted_tables.json", diagnostics_bytes))
        return SourceResult(
            source_name=self.name,
            rows=rows,
            raw_artifacts=raw_artifacts,
            details={
                "forecast_dates": [forecast_date] if forecast_date else [],
                "run_ids": [run_id],
                "model_status": "published_partial" if diagnostics.get("partial") else "published",
                "partial": bool(diagnostics.get("partial")),
                "partial_sections": list(diagnostics.get("partial_sections", [])),
                "house_record_count": diagnostics.get("house_record_count", 0),
                "senate_record_count": diagnostics.get("senate_record_count", 0),
                "house_embed_url": house_embed_url,
                "senate_embed_url": senate_embed_url,
                "house_map_embed_url": house_map_embed_url,
                "browser_fallback_used": browser_fallback_used,
                "collection_warnings": list(dict.fromkeys(collection_warnings)),
            },
        )

    @classmethod
    def discover_embed_url(
        cls,
        page_html: str | bytes,
        *,
        kind: str | None = None,
        target: str | None = None,
        fallback: str,
    ) -> str:
        """Find the forecast Infogram on a provider page.

        The page can contain additional polling graphics. Candidates are scored
        by their nearby title text, with the known current embed retained only
        as a last-resort fallback.
        """

        if isinstance(page_html, bytes):
            page_html = page_html.decode("utf-8-sig", errors="replace")
        selected_kind = _norm(kind or target)
        if selected_kind not in {"house", "senate"}:
            raise ValueError("kind must be 'house' or 'senate'")
        decoded = html_lib.unescape(page_html).replace("\\/", "/")
        candidates: list[tuple[int, int, str]] = []
        url_pattern = re.compile(
            r"(?:https?:)?//e\.infogram\.com/[^\s\"'<>]+",
            re.IGNORECASE,
        )
        for match in url_pattern.finditer(decoded):
            raw = match.group(0)
            url = "https:" + raw if raw.startswith("//") else raw
            url = url.rstrip(".,);]")
            tag_start = decoded.rfind("<", 0, match.start())
            tag_end = decoded.find(">", match.end())
            if tag_start >= 0 and tag_end >= 0:
                local_context = decoded[tag_start:tag_end + 1]
            else:
                local_context = decoded[max(0, match.start() - 180):match.end() + 180]
            context = _NON_WORD_RE.sub(
                " ", html_lib.unescape(local_context).casefold()
            ).strip()
            candidates.append((cls._embed_score(context, selected_kind), match.start(), url))

        data_id_pattern = re.compile(
            r"data-id\s*=\s*[\"']([^\"']+)[\"']",
            re.IGNORECASE,
        )
        for match in data_id_pattern.finditer(decoded):
            embed_id = match.group(1).strip()
            if not embed_id:
                continue
            tag_start = decoded.rfind("<", 0, match.start())
            tag_end = decoded.find(">", match.end())
            if tag_start >= 0 and tag_end >= 0:
                local_context = decoded[tag_start:tag_end + 1]
            else:
                local_context = decoded[max(0, match.start() - 180):match.end() + 180]
            context = _NON_WORD_RE.sub(
                " ", html_lib.unescape(local_context).casefold()
            ).strip()
            url = f"https://e.infogram.com/{embed_id}?embed_type=responsive_iframe&src=embed"
            candidates.append((cls._embed_score(context, selected_kind), match.start(), url))

        if candidates:
            score, _, chosen = max(candidates, key=lambda item: (item[0], -item[1]))
            if score > 0 or len(candidates) == 1:
                return cls._canonical_embed_url(chosen)
        return cls._canonical_embed_url(fallback)

    @staticmethod
    def _embed_score(context: str, kind: str) -> int:
        score = 0
        if kind in context:
            score += 4
        if "forecast" in context:
            score += 5
        if f"2026 {kind}" in context or f"{kind} 2026" in context:
            score += 5
        if "3 0" in context:
            score += 1
        if "polling" in context or "latest polls" in context:
            score -= 8
        if "vertical graphics" in context:
            score -= 5
        return score

    @staticmethod
    def _canonical_embed_url(url: str) -> str:
        url = html_lib.unescape(url.strip()).replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            url = urljoin("https://e.infogram.com/", url)
        if "?" not in url:
            return url + "?embed_type=responsive_iframe&src=embed"
        return url

    @staticmethod
    def extract_infographic_data(
        embed_html: str | bytes, *, label: str = "Infogram"
    ) -> dict[str, Any]:
        """Decode Infogram's static ``window.infographicData`` assignment."""

        if isinstance(embed_html, bytes):
            embed_html = embed_html.decode("utf-8-sig", errors="replace")
        variants = [embed_html]
        decoded = html_lib.unescape(embed_html)
        if decoded != embed_html:
            variants.append(decoded)
        decoder = json.JSONDecoder()
        markers = (
            "window.infographicData",
            "window[\"infographicData\"]",
            "window['infographicData']",
        )
        for text in variants:
            for marker in markers:
                offset = 0
                while True:
                    position = text.find(marker, offset)
                    if position < 0:
                        break
                    offset = position + len(marker)
                    equals = text.find("=", offset, offset + 200)
                    if equals < 0:
                        continue
                    start = equals + 1
                    while start < len(text) and text[start].isspace():
                        start += 1
                    try:
                        if text.startswith("JSON.parse", start):
                            opening = text.find("(", start, start + 100)
                            if opening < 0:
                                continue
                            encoded, _ = decoder.raw_decode(text[opening + 1:].lstrip())
                            if not isinstance(encoded, str):
                                continue
                            payload = json.loads(encoded)
                        else:
                            payload, _ = decoder.raw_decode(text[start:])
                            if isinstance(payload, str):
                                payload = json.loads(payload)
                        if isinstance(payload, dict):
                            return payload
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue

            # Some publish templates use an application/json script instead of
            # assigning the object directly. Restrict the fallback to objects
            # that contain Infogram's characteristic elements/content tree.
            for match in re.finditer(r"\{\s*\"(?:elements|project)\"\s*:", text):
                try:
                    payload, _ = decoder.raw_decode(text[match.start():])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and _contains_chart_data(payload):
                    return payload
        raise SourceFormatError(
            f"{label} Infogram no longer exposes a readable window.infographicData JSON payload"
        )

    @staticmethod
    def extract_tables(payload: dict[str, Any]) -> list[InfogramTable]:
        """Return every readable table embedded in an Infogram payload."""

        return extract_infogram_tables(payload)

    @staticmethod
    def extract_house_records(
        tables: list[InfogramTable], *, require_complete_counts: bool = False
    ) -> dict[str, RaceRecord]:
        """Extract House district records from already-decoded Infogram tables."""

        return select_house_records(
            tables, require_complete_counts=require_complete_counts
        )

    @staticmethod
    def extract_senate_records(
        tables: list[InfogramTable], *, require_complete_counts: bool = False
    ) -> dict[str, RaceRecord]:
        """Extract Senate race records from already-decoded Infogram tables."""

        return select_senate_records(
            tables, require_complete_counts=require_complete_counts
        )

    def normalize(
        self,
        house_payload: dict[str, Any],
        senate_payload: dict[str, Any],
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
        house_embed_url: str | None = None,
        senate_embed_url: str | None = None,
    ) -> tuple[list[dict[str, Any]], str, str, str]:
        """Normalize two payloads and return compact snapshot metadata.

        Production collection retains a larger diagnostics object through
        :meth:`normalize_infograms`; this wrapper is intentionally convenient
        for fixture tests and manual parser checks.
        """

        rows, run_id, forecast_date, diagnostics = self.normalize_infograms(
            house_payload,
            senate_payload,
            observed_datetime_utc=observed_datetime_utc,
            include_house_districts=include_house_districts,
            include_senate_races=include_senate_races,
            require_complete_counts=require_complete_counts,
            house_embed_url=house_embed_url,
            senate_embed_url=senate_embed_url,
        )
        return (
            rows,
            run_id,
            forecast_date,
            str(diagnostics.get("vendor_updated_at_utc", "")),
        )

    def normalize_infograms(
        self,
        house_payload: dict[str, Any],
        senate_payload: dict[str, Any],
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        require_complete_counts: bool = True,
        house_embed_url: str | None = None,
        senate_embed_url: str | None = None,
    ) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
        house_embed_url = house_embed_url or self.fallback_house_embed_url
        senate_embed_url = senate_embed_url or self.fallback_senate_embed_url
        house_tables = extract_infogram_tables(house_payload)
        senate_tables = extract_infogram_tables(senate_payload)
        if not house_tables and not senate_tables:
            raise SourceFormatError(
                "Race to the WH House and Senate Infograms contain no readable chart tables"
            )

        house_texts = collect_context_strings(house_payload)
        senate_texts = collect_context_strings(senate_payload)
        vendor_updated_at_utc = latest_payload_timestamp(house_payload, senate_payload)

        # Race-level and national sections are intentionally independent.  A
        # publisher layout change that hides the 435-district table must not
        # discard still-readable national House/Senate projections (and vice
        # versa).  Coverage is reported as partial below instead of raising.
        house_records = select_house_records(
            house_tables, require_complete_counts=False
        ) if house_tables else {}
        senate_records = select_senate_records(
            senate_tables, require_complete_counts=False
        ) if senate_tables else {}

        house_seats = extract_party_metric(
            house_tables, house_texts, metric="seats", chamber="house", chamber_size=435
        )
        if house_seats is None and len(house_records) == 435:
            d_expected = sum(float(record.d_probability) for record in house_records.values()) / 100.0
            r_expected = sum(float(record.r_probability) for record in house_records.values()) / 100.0
            o_expected = 435.0 - d_expected - r_expected
            house_seats = {
                "D": float(rounded(d_expected)),
                "R": float(rounded(r_expected)),
                "Other": float(rounded(max(0.0, o_expected))),
                "context": "sum of all 435 published district win probabilities",
            }
        house_control = extract_party_metric(
            house_tables, house_texts, metric="control", chamber="house", chamber_size=100
        )
        house_vote = extract_house_popular_vote(house_tables, house_texts)
        senate_seats = extract_party_metric(
            senate_tables, senate_texts, metric="seats", chamber="senate", chamber_size=100
        )
        senate_control = extract_party_metric(
            senate_tables, senate_texts, metric="control", chamber="senate", chamber_size=100
        )

        missing_national = []
        for label, value in (
            ("House seat projection", house_seats),
            ("House control probability", house_control),
            ("House popular-vote projection", house_vote),
            ("Senate seat projection", senate_seats),
            ("Senate control probability", senate_control),
        ):
            if value is None:
                missing_national.append(label)

        partial_sections: list[str] = []
        if missing_national:
            partial_sections.append("missing national metrics: " + ", ".join(missing_national))
        if include_house_districts and len(house_records) != 435:
            partial_sections.append(
                f"House district forecasts: {len(house_records)}/435 readable"
            )
        if include_senate_races and len(senate_records) != 35:
            partial_sections.append(
                f"Senate race forecasts: {len(senate_records)}/35 readable"
            )

        national_metrics = [house_seats, house_control, house_vote, senate_seats, senate_control]
        if not any(value is not None for value in national_metrics) and not house_records and not senate_records:
            raise SourceFormatError(
                "Race to the WH Infograms were readable but no usable forecast metrics or races were identified"
            )
        forecast_date = extract_forecast_date(house_texts + senate_texts)
        if not forecast_date and vendor_updated_at_utc:
            forecast_date = vendor_updated_at_utc[:10]

        canonical = {
            "house_seats": _without_context(house_seats or {}),
            "house_control": _without_context(house_control or {}),
            "house_vote": _without_context(house_vote or {}),
            "senate_seats": _without_context(senate_seats or {}),
            "senate_control": _without_context(senate_control or {}),
            "house_records": [record_to_canonical(record) for record in sorted(
                house_records.values(), key=lambda item: item.source_record_id
            )],
            "senate_records": [record_to_canonical(record) for record in sorted(
                senate_records.values(), key=lambda item: item.source_record_id
            )],
        }
        digest = sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:20]
        run_id = f"race-to-the-wh-{forecast_date or 'undated'}-{digest}"

        def metric_value(metric: dict[str, Any] | None, key: str) -> Any:
            return metric.get(key, "") if isinstance(metric, dict) else ""

        common = {
            "observed_datetime_utc": observed_datetime_utc,
            "vendor": self.name,
            "vendor_model": self.model_name,
            "vendor_run_id": run_id,
            "vendor_forecast_date": forecast_date,
            "vendor_updated_at_utc": vendor_updated_at_utc,
            "model_status": "published_partial" if partial_sections else "published",
            "election_date": "2026-11-03",
            "house_seats_basis": str(metric_value(house_seats, "context") or "published projected seats"),
            "house_seats_d": metric_value(house_seats, "D"),
            "house_seats_r": metric_value(house_seats, "R"),
            "house_seats_other": metric_value(house_seats, "Other"),
            "house_control_d_pct": metric_value(house_control, "D"),
            "house_control_r_pct": metric_value(house_control, "R"),
            "house_control_other_pct": metric_value(house_control, "Other"),
            "house_popular_vote_basis": (
                "Race to the WH adjusted two-party national House vote projection; "
                "uncontested districts are imputed by the provider"
            ) if house_vote is not None else "",
            "house_popular_vote_d_pct": metric_value(house_vote, "D"),
            "house_popular_vote_r_pct": metric_value(house_vote, "R"),
            "house_popular_vote_other_pct": metric_value(house_vote, "Other"),
            "house_popular_vote_margin_d_minus_r_pct": metric_value(house_vote, "margin"),
            "senate_seats_basis": str(metric_value(senate_seats, "context") or "published projected caucus seats"),
            "senate_seats_d": metric_value(senate_seats, "D"),
            "senate_seats_r": metric_value(senate_seats, "R"),
            "senate_seats_other": metric_value(senate_seats, "Other"),
            "senate_control_d_pct": metric_value(senate_control, "D"),
            "senate_control_r_pct": metric_value(senate_control, "R"),
            "senate_control_other_pct": metric_value(senate_control, "Other"),
        }
        result: list[dict[str, Any]] = []
        if any(value is not None for value in national_metrics):
            national = blank_row()
            national.update(common)
            national.update({
                "row_type": "national",
                "source_record_id": "national",
                "source_url": self.house_page_url,
                "source_file": "House and Senate public Infogram embeds",
                "data_quality": (
                    "partial semantic parse of public Infogram data tables"
                    if partial_sections else
                    "strict semantic parse of public Infogram data tables"
                ),
                "notes": (
                    f"house_embed={house_embed_url}; senate_embed={senate_embed_url}; "
                    f"house_tables={len(house_tables)}; senate_tables={len(senate_tables)}; "
                    f"partial_sections={' | '.join(partial_sections) if partial_sections else 'none'}"
                ),
            })
            result.append(national)

        if include_house_districts:
            for record in sorted(house_records.values(), key=lambda item: item.source_record_id):
                if record.seat_number is None:
                    raise SourceFormatError(f"House record lacks district number: {record.source_record_id}")
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "house_district",
                    "source_record_id": record.source_record_id,
                    "source_url": house_embed_url,
                    "source_file": "house_infogram.html",
                    "congressional_district": congressional_district_code(
                        record.state_abbreviation, record.seat_number
                    ),
                    "state_fips": record.state_fips,
                    "state_abbreviation": record.state_abbreviation,
                    "state": record.state,
                    "house_seat_number": record.seat_number,
                    "house_seat": plain_house_seat(record.state_abbreviation, record.seat_number),
                    "house_d_pct": record.d_probability,
                    "house_r_pct": record.r_probability,
                    "house_other_pct": record.other_probability,
                    "house_d_vote_pct": record.d_vote,
                    "house_r_vote_pct": record.r_vote,
                    "house_other_vote_pct": record.other_vote,
                    "house_rating": record.rating,
                    "special_election": record.special,
                    "data_quality": "published Race to the WH district forecast",
                    "notes": f"infogram_context={record.source_context}",
                })
                result.append(row)

        if include_senate_races:
            for record in sorted(senate_records.values(), key=lambda item: item.source_record_id):
                row = blank_row()
                row.update(common)
                row.update({
                    "row_type": "senate_race",
                    "source_record_id": record.source_record_id,
                    "source_url": senate_embed_url,
                    "source_file": "senate_infogram.html",
                    "state_fips": record.state_fips,
                    "state_abbreviation": record.state_abbreviation,
                    "state": record.state,
                    "senate_seat": plain_senate_seat(
                        record.state_abbreviation, special=record.special
                    ),
                    "senate_d_pct": record.d_probability,
                    "senate_r_pct": record.r_probability,
                    "senate_other_pct": record.other_probability,
                    "senate_d_vote_pct": record.d_vote,
                    "senate_r_vote_pct": record.r_vote,
                    "senate_other_vote_pct": record.other_vote,
                    "senate_rating": record.rating,
                    "special_election": record.special,
                    "data_quality": "published Race to the WH Senate race forecast",
                    "notes": f"infogram_context={record.source_context}",
                })
                result.append(row)

        diagnostics = {
            "adapter": self.slug,
            "run_id": run_id,
            "forecast_date": forecast_date,
            "vendor_updated_at_utc": vendor_updated_at_utc,
            "partial": bool(partial_sections),
            "partial_sections": partial_sections,
            "house_record_count": len(house_records),
            "senate_record_count": len(senate_records),
            "national": canonical | {
                "house_seats_context": metric_value(house_seats, "context"),
                "house_control_context": metric_value(house_control, "context"),
                "house_vote_context": metric_value(house_vote, "context"),
                "senate_seats_context": metric_value(senate_seats, "context"),
                "senate_control_context": metric_value(senate_control, "context"),
            },
            "house_tables": [table_to_diagnostic(table) for table in house_tables],
            "senate_tables": [table_to_diagnostic(table) for table in senate_tables],
        }
        return result, run_id, forecast_date, diagnostics


def _combine_payloads(payloads: Iterable[Any], *, label: str) -> dict[str, Any]:
    return {
        "capture_label": label,
        "payloads": [payload for payload in payloads if payload not in (None, "")],
    }


def _missing_chamber_topline(diagnostics: dict[str, Any], *, chamber: str) -> bool:
    national = diagnostics.get("national", {})
    if not isinstance(national, dict):
        return True
    if chamber == "house":
        keys = ("house_seats", "house_control", "house_vote")
    elif chamber == "senate":
        keys = ("senate_seats", "senate_control")
    else:
        raise ValueError("chamber must be house or senate")
    return any(not national.get(key) for key in keys)


def _decode_json_documents(text: str) -> list[Any]:
    candidates: list[Any] = []
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return candidates
    try:
        candidates.append(json.loads(stripped))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # JSONP and common JavaScript assignment wrappers used by live feeds.
    wrappers = (
        re.compile(r"^[\w.$]+\s*\(\s*(\{.*\}|\[.*\])\s*\)\s*;?\s*$", re.DOTALL),
        re.compile(r"^(?:var|let|const)\s+[\w$]+\s*=\s*(\{.*\}|\[.*\])\s*;?\s*$", re.DOTALL),
    )
    for pattern in wrappers:
        match = pattern.match(stripped)
        if not match:
            continue
        try:
            candidates.append(json.loads(match.group(1)))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Application/json script blocks are common in newer publish templates.
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            candidates.append(json.loads(html_lib.unescape(match.group(1)).strip()))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return candidates


def _browser_capture_payload(capture: BrowserCapture, *, label: str) -> dict[str, Any] | None:
    payloads: list[Any] = list(capture.globals)
    document_summaries: list[dict[str, Any]] = []
    for document in capture.documents:
        decoded = _decode_json_documents(document.text)
        try:
            decoded.append(
                RaceToTheWHSource.extract_infographic_data(
                    document.text, label=f"browser-captured {label} response"
                )
            )
        except SourceFormatError:
            pass
        if decoded:
            payloads.extend(decoded)
            document_summaries.append({
                "url": document.url,
                "content_type": document.content_type,
                "decoded_payload_count": len(decoded),
                "bytes": len(document.text.encode("utf-8")),
            })
    for index, rows in enumerate(capture.tables, start=1):
        payloads.append({
            "type": "table",
            "title": f"browser rendered {label} table {index}",
            "data": [rows],
            "sheetnames": [f"Rendered {index}"],
        })
    if not payloads and not capture.text_lines:
        return None
    return {
        "capture_type": "public_browser_network",
        "capture_label": label,
        "documents": document_summaries,
        "payloads": payloads,
        "rendered_text_lines": capture.text_lines,
        "warnings": capture.warnings,
    }


def _clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("formattedValue", "formatted_value", "displayValue", "value", "text", "label", "name"):
            if key in value and value[key] not in (None, ""):
                return _clean_text(value[key])
        return ""
    text = html_lib.unescape(str(value)).replace("\u00a0", " ")
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _norm(value: Any) -> str:
    return _NON_WORD_RE.sub(" ", _clean_text(value).casefold()).strip()


def _contains_chart_data(node: Any) -> bool:
    if isinstance(node, dict):
        if "chartData" in node or "chart_data" in node or _looks_like_legacy_chart(node):
            return True
        return any(_contains_chart_data(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_chart_data(value) for value in node)
    return False


def _looks_like_legacy_chart(node: dict[str, Any]) -> bool:
    """Recognize Infogram's older public-project chart representation.

    Older embeds store each chart directly in ``elements`` with ``data`` and
    ``sheetnames`` fields instead of nesting the same information under
    ``props.chartData``. Race to the WH can be republished through either
    Infogram renderer, so the adapter intentionally supports both layouts.
    """

    node_type = _norm(node.get("type", ""))
    has_chart_identity = (
        node_type in {"chart", "table", "map"}
        or "chart_id" in node
        or "chart_type_nr" in node
        or "chartType" in node
    )
    return bool(has_chart_identity and any(key in node for key in ("data", "sheets", "values")))


def _iter_nodes(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_nodes(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_nodes(value, path + (str(index),))


def _owner_title(owner: dict[str, Any], path: tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in ("title", "name", "description", "chartTitle", "chart_title", "type"):
        value = _clean_text(owner.get(key, ""))
        if value and value not in parts:
            parts.append(value)
    props = owner.get("props")
    if isinstance(props, dict):
        for key in ("title", "name", "description", "chartTitle", "chart_title"):
            value = _clean_text(props.get(key, ""))
            if value and value not in parts:
                parts.append(value)
    if not parts and path:
        parts.append(path[-1])
    return " | ".join(parts)


def _coerce_table_rows(value: Any) -> list[list[str]]:
    if isinstance(value, dict):
        for key in ("data", "rows", "values", "cells"):
            if key in value:
                return _coerce_table_rows(value[key])
        if value and all(not isinstance(item, (dict, list)) for item in value.values()):
            return [[_clean_text(key) for key in value], [_clean_text(item) for item in value.values()]]
        return []
    if not isinstance(value, list) or not value:
        return []
    if all(isinstance(item, dict) for item in value):
        headers: list[str] = []
        for item in value:
            assert isinstance(item, dict)
            for key in item:
                if key not in headers:
                    headers.append(str(key))
        return [headers] + [[_clean_text(item.get(key, "")) for key in headers] for item in value]
    if all(isinstance(item, (list, tuple)) for item in value):
        rows = [[_clean_text(cell) for cell in item] for item in value]
        width = max((len(row) for row in rows), default=0)
        return [row + [""] * (width - len(row)) for row in rows]
    return []


def _tables_from_chart_data(
    chart_data: Any,
    *,
    entity_path: str,
    title: str,
) -> list[InfogramTable]:
    if isinstance(chart_data, str):
        try:
            chart_data = json.loads(chart_data)
        except json.JSONDecodeError:
            return []
    if not isinstance(chart_data, dict):
        return []

    sheet_names_raw = (
        chart_data.get("sheetnames")
        or chart_data.get("sheetNames")
        or chart_data.get("sheet_names")
        or []
    )
    sheet_names = [_clean_text(value) or f"Sheet {index + 1}" for index, value in enumerate(sheet_names_raw)]
    data = chart_data.get("data", chart_data.get("sheets", chart_data.get("values")))
    tables: list[InfogramTable] = []

    if isinstance(data, dict):
        if isinstance(data.get("sheets"), list):
            data = data["sheets"]
        else:
            for index, (name, sheet_data) in enumerate(data.items()):
                rows = _coerce_table_rows(sheet_data)
                if rows:
                    tables.append(InfogramTable(entity_path, title, _clean_text(name) or f"Sheet {index + 1}", rows))
            return tables

    if isinstance(data, list):
        direct_rows = _coerce_table_rows(data)
        if direct_rows:
            # A direct 2-D list is one sheet. A list of 3-D sheet arrays is not.
            if data and isinstance(data[0], (list, tuple)) and data[0] and isinstance(data[0][0], (list, tuple, dict)):
                direct_rows = []
        if direct_rows:
            tables.append(InfogramTable(entity_path, title, sheet_names[0] if sheet_names else "Sheet 1", direct_rows))
            return tables

        for index, sheet_data in enumerate(data):
            name = sheet_names[index] if index < len(sheet_names) else f"Sheet {index + 1}"
            if isinstance(sheet_data, dict):
                name = _clean_text(
                    sheet_data.get("sheetName", sheet_data.get("sheet_name", sheet_data.get("name", name)))
                ) or name
            rows = _coerce_table_rows(sheet_data)
            if rows:
                tables.append(InfogramTable(entity_path, title, name, rows))
    return tables


def _looks_like_forecast_rows(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return False
    width = max((len(row) for row in rows), default=0)
    if width < 2 or width > 80 or len(rows) > 5_000:
        return False
    sample_rows = rows[: min(30, len(rows))]
    text = _norm(" ".join(cell for row in sample_rows for cell in row[: min(30, len(row))]))
    if not text:
        return False
    keywords = (
        "district", "state", "race", "seat", "chance", "probability",
        "forecast", "projected", "projection", "margin", "majority",
        "control", "popular vote", "rating", "democrat", "republican",
        "gop", "dem chance", "rep chance", "d pct", "r pct",
    )
    semantic = any(keyword in text for keyword in keywords)
    location = bool(re.search(r"\b[A-Z]{2}[\s-]?(?:AL|0?[1-9]|[1-4][0-9]|5[0-2])\b", " ".join(
        cell for row in sample_rows for cell in row[:4]
    ), re.IGNORECASE))
    party = any(word in text.split() for word in (
        "d", "r", "dem", "dems", "democrat", "democrats", "democratic",
        "gop", "rep", "republican", "republicans",
    ))
    rating = any(phrase in text for phrase in (
        "safe d", "safe r", "likely d", "likely r", "lean d", "lean r",
        "tilt d", "tilt r", "toss up", "tossup",
    ))
    return semantic or (location and (party or rating))


def _generic_tables_from_node(
    node: Any,
    *,
    entity_path: str,
    title: str,
) -> list[InfogramTable]:
    candidates: list[tuple[str, Any]] = []
    if isinstance(node, list):
        candidates.append(("Captured data", node))
    elif isinstance(node, dict):
        for key in (
            "rows", "records", "results", "items", "values", "cells", "data",
        ):
            if key in node:
                candidates.append((_clean_text(key) or "Captured data", node[key]))
    tables: list[InfogramTable] = []
    for sheet_name, value in candidates:
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
            and any(
                isinstance(cell, (dict, list))
                for item in value
                for cell in item.values()
            )
        ):
            # This is usually a collection of Infogram entities, not a row
            # table. Its actual chart data will be discovered recursively.
            continue
        rows = _coerce_table_rows(value)
        if rows and _looks_like_forecast_rows(rows):
            tables.append(InfogramTable(entity_path, title, sheet_name, rows))
    return tables


def extract_infogram_tables(payload: dict[str, Any]) -> list[InfogramTable]:
    tables: list[InfogramTable] = []
    seen: set[str] = set()

    def add_table(table: InfogramTable) -> None:
        canonical = json.dumps(
            table.rows,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical not in seen:
            seen.add(canonical)
            tables.append(table)

    for path, node in _iter_nodes(payload):
        entity_path = "/".join(path) or "root"
        if isinstance(node, dict):
            title = _owner_title(node, path)
            chart_data = node.get("chartData", node.get("chart_data"))
            if chart_data is None and _looks_like_legacy_chart(node):
                chart_data = node
            if chart_data is not None:
                for table in _tables_from_chart_data(
                    chart_data, entity_path=entity_path, title=title
                ):
                    add_table(table)
            for table in _generic_tables_from_node(
                node, entity_path=entity_path, title=title
            ):
                add_table(table)
        elif isinstance(node, list):
            title = path[-1] if path else "Captured data"
            for table in _generic_tables_from_node(
                node, entity_path=entity_path, title=title
            ):
                add_table(table)
    return tables


def collect_context_strings(payload: dict[str, Any]) -> list[str]:
    strings: list[str] = []
    seen: set[str] = set()
    for _, node in _iter_nodes(payload):
        if isinstance(node, str):
            text = _clean_text(node)
            if 2 <= len(text) <= 500 and text not in seen:
                seen.add(text)
                strings.append(text)
        elif isinstance(node, dict):
            direct: list[str] = []
            for key, value in node.items():
                if key in {"chartData", "chart_data"} or isinstance(value, (dict, list)):
                    continue
                text = _clean_text(value)
                if text and len(text) <= 160:
                    direct.append(text)
            combined = _clean_text(" ".join(direct))
            if 2 <= len(combined) <= 500 and combined not in seen:
                seen.add(combined)
                strings.append(combined)
        elif isinstance(node, list) and node and all(not isinstance(item, (dict, list)) for item in node):
            combined = _clean_text(" ".join(_clean_text(item) for item in node))
            if 2 <= len(combined) <= 500 and combined not in seen:
                seen.add(combined)
                strings.append(combined)
    return strings


def _combined_headers(rows: list[list[str]], header_index: int) -> list[str]:
    width = max((len(row) for row in rows[:header_index + 1]), default=0)
    levels: list[list[str]] = []
    start = max(0, header_index - 2)
    for level_index in range(start, header_index + 1):
        source = rows[level_index] + [""] * (width - len(rows[level_index]))
        level: list[str] = []
        carry = ""
        for cell in source:
            text = _clean_text(cell)
            if text:
                carry = text
                level.append(text)
            elif level_index < header_index and _party_from_text(carry):
                level.append(carry)
            else:
                level.append("")
        levels.append(level)
    headers: list[str] = []
    for column in range(width):
        parts: list[str] = []
        for level in levels:
            text = level[column]
            if text and text not in parts:
                parts.append(text)
        headers.append(_clean_text(" ".join(parts)))
    return headers


def _party_from_text(value: Any) -> str:
    text = _norm(value)
    words = set(text.split())
    if words & {"dem", "dems", "democrat", "democrats", "democratic", "d"}:
        return "D"
    if words & {"gop", "rep", "reps", "republican", "republicans", "r"}:
        return "R"
    if words & {"ind", "independent", "independents", "other", "others", "third"}:
        return "Other"
    return ""


def _parse_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and number not in {float("inf"), float("-inf")} else None
    text = _clean_text(value).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_pct(value: Any) -> float | None:
    number = _parse_number(value)
    if number is None:
        return None
    text = _clean_text(value)
    if text.startswith("<"):
        number /= 2.0
    elif text.startswith(">"):
        number = (number + 100.0) / 2.0
    return number


def _normalize_party_values(
    d_value: Any,
    r_value: Any,
    other_value: Any = None,
    *,
    target: float = 100.0,
    percentages: bool,
) -> tuple[float, float, float] | None:
    d = _parse_pct(d_value) if percentages else _parse_number(d_value)
    r = _parse_pct(r_value) if percentages else _parse_number(r_value)
    other = _parse_pct(other_value) if percentages else _parse_number(other_value)
    if d is None and r is None:
        return None
    if percentages:
        present = [value for value in (d, r, other) if value is not None]
        if present and max(present) <= 1.000001 and sum(present) <= 1.050001:
            d = d * 100.0 if d is not None else None
            r = r * 100.0 if r is not None else None
            other = other * 100.0 if other is not None else None
    if d is None and r is not None and other is None:
        d = target - r
    if r is None and d is not None and other is None:
        r = target - d
    if d is None or r is None:
        return None
    if other is None:
        other = target - d - r
    if min(d, r, other) < -0.2:
        return None
    total = d + r + other
    if abs(total - target) > max(1.0, target * 0.015):
        return None
    other = max(0.0, other)
    # Rebalance tiny display-rounding drift into Other, preserving the two
    # published major-party values.
    other += target - (d + r + other)
    return (
        float(rounded(d)),
        float(rounded(r)),
        float(rounded(max(0.0, other))),
    )


def _parse_margin_dem(value: Any, *, row_context: str = "", header_context: str = "") -> float | None:
    text = _clean_text(value)
    combined = _clean_text(f"{text} {row_context} {header_context}")
    patterns = (
        (r"\b(?:d|dem|democrat|democrats|democratic)\s*[+]?\s*(\d+(?:\.\d+)?)\b", 1.0),
        (r"\b(?:r|gop|rep|republican|republicans)\s*[+]?\s*(\d+(?:\.\d+)?)\b", -1.0),
        (r"(\d+(?:\.\d+)?)\s*(?:point|pt|%)?\s*(?:dem|democrat|democratic)\b", 1.0),
        (r"(\d+(?:\.\d+)?)\s*(?:point|pt|%)?\s*(?:gop|rep|republican)\b", -1.0),
    )
    lowered = combined.casefold()
    for pattern, sign in patterns:
        match = re.search(pattern, lowered)
        if match:
            return float(rounded(sign * float(match.group(1))))
    number = _parse_number(text)
    header = _norm(header_context)
    if number is None:
        return None
    if "d r" in header or "dem margin" in header or "democratic margin" in header:
        return float(rounded(number))
    winner = _party_from_text(row_context)
    if winner == "D":
        return float(rounded(abs(number)))
    if winner == "R":
        return float(rounded(-abs(number)))
    if number < 0:
        return float(rounded(number))
    return None


def _header_is_probability(header: str, party: str) -> bool:
    normalized = _norm(header)
    if _party_from_text(normalized) != party:
        return False
    return any(word in normalized for word in ("prob", "chance", "odds", "win pct", "win percent", "win percentage"))


def _header_is_vote(header: str, party: str) -> bool:
    normalized = _norm(header)
    if _party_from_text(normalized) != party:
        return False
    if any(word in normalized for word in ("prob", "chance", "odds")):
        return False
    return any(word in normalized for word in ("vote", "share", "projected pct", "projection pct", "forecast pct"))


def _find_header(headers: list[str], predicate: Any) -> int | None:
    for index, header in enumerate(headers):
        if predicate(header):
            return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index]


def _parse_house_location(value: Any, *, state_hint: str = "") -> tuple[str, str, str, int] | None:
    text = _clean_text(value).replace("–", "-").replace("—", "-")
    if not text and not state_hint:
        return None
    compact = _norm(text)

    if re.fullmatch(r"\d{4}", compact):
        abbr = _FIPS_TO_ABBR.get(compact[:2])
        seat = int(compact[2:])
        if abbr and abbr not in {"DC", "AS", "GU", "MP", "PR", "VI"} and seat >= 1:
            _, state_name, fips = resolve_state(abbr)
            return abbr, state_name, fips, seat

    abbreviation = re.search(
        r"\b([A-Z]{2})\s*(?:-|\s)\s*(AL|AT\s*LARGE|\d{1,2})(?:\b|$)",
        text.upper(),
    )
    if abbreviation:
        abbr = abbreviation.group(1)
        seat_text = abbreviation.group(2)
        try:
            _, state_name, fips = resolve_state(abbr)
        except ValueError:
            pass
        else:
            seat = 1 if seat_text.replace(" ", "") in {"AL", "ATLARGE"} else int(seat_text)
            return abbr, state_name, fips, seat

    state_match = re.search(rf"\b({_STATE_NAME_PATTERN})\b", text, re.IGNORECASE)
    if state_match:
        abbr, state_name, fips = resolve_state(state_match.group(1))
        tail = text[state_match.end():]
        seat_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", tail, re.IGNORECASE)
        if seat_match:
            return abbr, state_name, fips, int(seat_match.group(1))
        if re.search(r"\b(?:at[- ]?large|al)\b", tail, re.IGNORECASE) or abbr in AT_LARGE_STATES:
            return abbr, state_name, fips, 1

    if state_hint:
        try:
            abbr, state_name, fips = resolve_state(state_hint)
        except ValueError:
            return None
        seat_match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text, re.IGNORECASE)
        if seat_match:
            return abbr, state_name, fips, int(seat_match.group(1))
        if re.search(r"\b(?:at[- ]?large|al)\b", text, re.IGNORECASE) or abbr in AT_LARGE_STATES:
            return abbr, state_name, fips, 1
    return None


def _parse_senate_location(value: Any) -> tuple[str, str, str, bool] | None:
    text = _clean_text(value)
    if not text:
        return None
    special = "special" in text.casefold()
    upper = text.strip().upper()
    if upper in ABBR_TO_NAME:
        abbr, state_name, fips = resolve_state(upper)
        return abbr, state_name, fips, special
    state_match = re.search(rf"\b({_STATE_NAME_PATTERN})\b", text, re.IGNORECASE)
    if state_match:
        abbr, state_name, fips = resolve_state(state_match.group(1))
        return abbr, state_name, fips, special
    abbreviation = re.search(r"\b([A-Z]{2})\b", text.upper())
    if abbreviation and abbreviation.group(1) in ABBR_TO_NAME:
        abbr, state_name, fips = resolve_state(abbreviation.group(1))
        return abbr, state_name, fips, special
    return None


def _race_header_candidates(rows: list[list[str]], *, kind: str) -> list[tuple[int, list[str], int]]:
    candidates: list[tuple[int, list[str], int]] = []
    for header_index in range(min(5, len(rows))):
        headers = _combined_headers(rows, header_index)
        normalized = [_norm(header) for header in headers]
        score = 0
        if kind == "house":
            if any("district" in header or header in {"race", "seat", "cd"} for header in normalized):
                score += 5
        else:
            if any(header == "state" or "senate race" in header or header == "race" for header in normalized):
                score += 5
        if any(_header_is_probability(header, "D") for header in headers):
            score += 3
        if any(_header_is_probability(header, "R") for header in headers):
            score += 3
        if any("margin" in header or "projected result" in header for header in normalized):
            score += 2
        if any(_header_is_vote(header, "D") for header in headers):
            score += 2
        if any(_header_is_vote(header, "R") for header in headers):
            score += 2
        candidates.append((header_index, headers, score))
    return sorted(candidates, key=lambda item: item[2], reverse=True)


def _extract_probability_and_vote(
    row: list[str], headers: list[str]
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None, str]:
    d_prob_index = _find_header(headers, lambda header: _header_is_probability(header, "D"))
    r_prob_index = _find_header(headers, lambda header: _header_is_probability(header, "R"))
    o_prob_index = _find_header(headers, lambda header: _header_is_probability(header, "Other"))
    probabilities = _normalize_party_values(
        _cell(row, d_prob_index), _cell(row, r_prob_index), _cell(row, o_prob_index),
        percentages=True,
    )

    row_text = _clean_text(" | ".join(row))
    margin_index = _find_header(
        headers,
        lambda header: any(term in _norm(header) for term in (
            "margin", "projected result", "forecast result", "projected lead", "projection"
        )),
    )
    party_index = _find_header(
        headers,
        lambda header: _norm(header) in {
            "party", "winner", "favored party", "projected winner", "favorite"
        },
    )
    margin = _parse_margin_dem(
        _cell(row, margin_index),
        row_context=_clean_text(f"{_cell(row, party_index)} {row_text}"),
        header_context=_cell(headers, margin_index),
    )
    if margin is None:
        margin = _parse_margin_dem(row_text, row_context=row_text)

    # Some Race to the WH tables publish only the favorite's probability in a
    # generic "Chance of Winning" column. Resolve which party that probability
    # belongs to from the explicit winner column or the signed projected margin.
    if probabilities is None:
        generic_probability_index = _find_header(
            headers,
            lambda header: (
                _party_from_text(header) == ""
                and any(term in _norm(header) for term in (
                    "chance of winning", "win probability", "winning probability",
                    "forecast probability", "probability", "odds"
                ))
            ),
        )
        favorite_probability = _parse_pct(_cell(row, generic_probability_index))
        if favorite_probability is not None and favorite_probability <= 1.000001:
            favorite_probability *= 100.0
        favorite_party = _party_from_text(_cell(row, party_index))
        if not favorite_party and margin is not None:
            favorite_party = "D" if margin >= 0 else "R"
        if not favorite_party:
            favorite_party = _party_from_text(row_text)
        if (
            favorite_probability is not None
            and 0.0 <= favorite_probability <= 100.0
            and favorite_party in {"D", "R"}
        ):
            if favorite_party == "D":
                probabilities = (
                    float(rounded(favorite_probability)),
                    float(rounded(100.0 - favorite_probability)),
                    0.0,
                )
            else:
                probabilities = (
                    float(rounded(100.0 - favorite_probability)),
                    float(rounded(favorite_probability)),
                    0.0,
                )

    d_vote_index = _find_header(headers, lambda header: _header_is_vote(header, "D"))
    r_vote_index = _find_header(headers, lambda header: _header_is_vote(header, "R"))
    o_vote_index = _find_header(headers, lambda header: _header_is_vote(header, "Other"))
    votes = _normalize_party_values(
        _cell(row, d_vote_index), _cell(row, r_vote_index), _cell(row, o_vote_index),
        percentages=True,
    )
    if votes is None:
        if margin is not None:
            votes = tuple(float(value) for value in pct_from_margin_dem(margin))

    rating_index = _find_header(
        headers,
        lambda header: _norm(header) in {"rating", "race rating", "category", "classification"},
    )
    rating = _clean_text(_cell(row, rating_index))
    if not rating and probabilities is not None:
        rating = probability_rating(probabilities[0])
    return probabilities, votes, rating


def parse_house_table(table: InfogramTable) -> dict[str, RaceRecord]:
    best: tuple[int, dict[str, RaceRecord]] = (-1, {})
    for header_index, headers, header_score in _race_header_candidates(table.rows, kind="house"):
        state_index = _find_header(headers, lambda header: _norm(header) == "state")
        district_index = _find_header(
            headers,
            lambda header: (
                "district" in _norm(header)
                or _norm(header) in {"race", "seat", "cd", "congressional seat"}
            ),
        )
        records: dict[str, RaceRecord] = {}
        conflicts = 0
        for row in table.rows[header_index + 1:]:
            state_hint = _cell(row, state_index)
            location = _parse_house_location(_cell(row, district_index), state_hint=state_hint)
            if location is None:
                for candidate in row[: min(4, len(row))]:
                    location = _parse_house_location(candidate, state_hint=state_hint)
                    if location:
                        break
            if location is None:
                continue
            abbr, state_name, fips, seat_number = location
            try:
                district_code = congressional_district_code(abbr, seat_number)
            except ValueError:
                continue
            probabilities, votes, rating = _extract_probability_and_vote(row, headers)
            if probabilities is None and votes is None:
                continue
            if probabilities is None:
                # Vote projection alone does not imply a win probability.
                continue
            source_record_id = f"{abbr}-{seat_number:02d}"
            record = RaceRecord(
                state_abbreviation=abbr,
                state=state_name,
                state_fips=fips,
                source_record_id=source_record_id,
                d_probability=probabilities[0],
                r_probability=probabilities[1],
                other_probability=probabilities[2],
                d_vote=votes[0] if votes else "",
                r_vote=votes[1] if votes else "",
                other_vote=votes[2] if votes else "",
                rating=rating,
                seat_number=seat_number,
                source_context=_clean_text(f"{table.context} {district_code}"),
            )
            existing = records.get(source_record_id)
            if existing is None:
                records[source_record_id] = record
            else:
                try:
                    records[source_record_id] = merge_race_records(existing, record)
                except SourceFormatError:
                    conflicts += 1
        score = len(records) * 100 + sum(record.completeness() for record in records.values()) + header_score - conflicts * 1000
        if score > best[0]:
            best = (score, records)
    return best[1]


def parse_senate_table(table: InfogramTable) -> dict[str, RaceRecord]:
    best: tuple[int, dict[str, RaceRecord]] = (-1, {})
    for header_index, headers, header_score in _race_header_candidates(table.rows, kind="senate"):
        state_index = _find_header(
            headers,
            lambda header: _norm(header) in {"state", "race", "senate race", "seat"},
        )
        special_index = _find_header(headers, lambda header: "special" in _norm(header))
        records: dict[str, RaceRecord] = {}
        conflicts = 0
        for row in table.rows[header_index + 1:]:
            location = _parse_senate_location(_cell(row, state_index))
            if location is None:
                for candidate in row[: min(4, len(row))]:
                    location = _parse_senate_location(candidate)
                    if location:
                        break
            if location is None:
                continue
            abbr, state_name, fips, special = location
            special_text = _norm(_cell(row, special_index))
            if special_text in {"yes", "true", "special", "1"}:
                special = True
            probabilities, votes, rating = _extract_probability_and_vote(row, headers)
            if probabilities is None:
                continue
            source_record_id = f"{abbr}:{'special' if special else 'regular'}"
            record = RaceRecord(
                state_abbreviation=abbr,
                state=state_name,
                state_fips=fips,
                source_record_id=source_record_id,
                d_probability=probabilities[0],
                r_probability=probabilities[1],
                other_probability=probabilities[2],
                d_vote=votes[0] if votes else "",
                r_vote=votes[1] if votes else "",
                other_vote=votes[2] if votes else "",
                rating=rating,
                special=special,
                source_context=_clean_text(f"{table.context} {source_record_id}"),
            )
            existing = records.get(source_record_id)
            if existing is None:
                records[source_record_id] = record
            else:
                try:
                    records[source_record_id] = merge_race_records(existing, record)
                except SourceFormatError:
                    conflicts += 1
        score = len(records) * 100 + sum(record.completeness() for record in records.values()) + header_score - conflicts * 1000
        if score > best[0]:
            best = (score, records)
    return best[1]


def merge_race_records(first: RaceRecord, second: RaceRecord) -> RaceRecord:
    if first.source_record_id != second.source_record_id:
        raise SourceFormatError("cannot merge different race records")
    merged = RaceRecord(**first.__dict__)
    for field in (
        "d_probability", "r_probability", "other_probability",
        "d_vote", "r_vote", "other_vote", "rating",
    ):
        old = getattr(merged, field)
        new = getattr(second, field)
        if old in (None, ""):
            setattr(merged, field, new)
        elif new not in (None, "") and old != new:
            if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(float(old) - float(new)) <= 0.051:
                continue
            raise SourceFormatError(
                f"conflicting Race to the WH values for {first.source_record_id} {field}: {old!r} vs {new!r}"
            )
    if second.source_context and second.source_context not in merged.source_context:
        merged.source_context = _clean_text(f"{merged.source_context}; {second.source_context}")
    return merged


def _select_and_enrich_records(
    parsed_tables: list[tuple[InfogramTable, dict[str, RaceRecord]]],
    *,
    expected_count: int,
    label: str,
    require_complete_counts: bool,
) -> dict[str, RaceRecord]:
    nonempty = [(table, records) for table, records in parsed_tables if records]
    if not nonempty:
        if require_complete_counts:
            raise SourceFormatError(f"no {label} forecast table could be identified in the Infogram")
        return {}
    anchor_table, anchor = max(
        nonempty,
        key=lambda item: (
            len(item[1]),
            sum(record.completeness() for record in item[1].values()),
        ),
    )
    merged = {key: RaceRecord(**record.__dict__) for key, record in anchor.items()}
    for table, records in nonempty:
        if table is anchor_table:
            continue
        overlap = set(merged) & set(records)
        minimum_overlap = max(1, int(0.8 * min(len(merged), expected_count)))
        if len(overlap) < minimum_overlap:
            continue
        for key in overlap:
            try:
                merged[key] = merge_race_records(merged[key], records[key])
            except SourceFormatError:
                # Auxiliary trend/change tables can contain older values. The
                # largest complete table remains authoritative for conflicts.
                continue
    if require_complete_counts and len(merged) != expected_count:
        raise SourceFormatError(
            f"expected {expected_count} Race to the WH {label} records, found {len(merged)}; "
            "the Infogram layout or table coverage may have changed"
        )
    return merged


def select_house_records(
    tables: list[InfogramTable], *, require_complete_counts: bool
) -> dict[str, RaceRecord]:
    return _select_and_enrich_records(
        [(table, parse_house_table(table)) for table in tables],
        expected_count=435,
        label="House district",
        require_complete_counts=require_complete_counts,
    )


def select_senate_records(
    tables: list[InfogramTable], *, require_complete_counts: bool
) -> dict[str, RaceRecord]:
    return _select_and_enrich_records(
        [(table, parse_senate_table(table)) for table in tables],
        expected_count=35,
        label="Senate race",
        require_complete_counts=require_complete_counts,
    )


def _metric_context_score(context: str, *, metric: str, chamber: str) -> int:
    normalized = _norm(context)
    score = 0
    if chamber in normalized:
        score += 2
    if metric == "seats":
        phrases = ("projected seats", "expected seats", "average seats", "avg seats", "most likely seats", "seat projection")
        score += 8 if any(phrase in normalized for phrase in phrases) else 0
        score += 3 if "seats" in normalized else 0
        score -= 10 if "seats up" in normalized else 0
    elif metric == "control":
        score += 7 if any(word in normalized for word in ("majority", "control")) else 0
        score += 5 if any(word in normalized for word in ("chance", "probability", "odds")) else 0
        score += 3 if f"win the {chamber}" in normalized else 0
    elif metric == "popular_vote":
        score += 10 if "popular vote" in normalized else 0
        score += 9 if "national vote" in normalized else 0
        score += 8 if "national environment" in normalized or "political environment" in normalized else 0
        score += 2 if "house vote" in normalized else 0
    return score


def _party_column_candidates(
    table: InfogramTable,
) -> Iterator[tuple[str, str, Any, Any, Any]]:
    if len(table.rows) > 80:
        return
    for header_index in range(min(5, len(table.rows))):
        headers = _combined_headers(table.rows, header_index)
        d_index = _find_header(headers, lambda header: _party_from_text(header) == "D")
        r_index = _find_header(headers, lambda header: _party_from_text(header) == "R")
        o_index = _find_header(headers, lambda header: _party_from_text(header) == "Other")
        if d_index is None or r_index is None:
            continue
        for row in table.rows[header_index + 1:]:
            label_cells = [cell for index, cell in enumerate(row) if index not in {d_index, r_index, o_index}]
            metric_label = _clean_text(" | ".join(label_cells))
            context = _clean_text(f"{table.context} {metric_label}")
            yield (
                context,
                metric_label,
                _cell(row, d_index),
                _cell(row, r_index),
                _cell(row, o_index),
            )


def _party_row_candidates(
    table: InfogramTable,
) -> Iterator[tuple[str, str, Any, Any, Any]]:
    if len(table.rows) > 80:
        return
    for header_index in range(min(5, len(table.rows))):
        headers = _combined_headers(table.rows, header_index)
        party_rows: dict[str, list[str]] = {}
        for row in table.rows[header_index + 1:]:
            party = ""
            for cell in row[: min(3, len(row))]:
                party = _party_from_text(cell)
                if party:
                    break
            if party and party not in party_rows:
                party_rows[party] = row
        if "D" not in party_rows or "R" not in party_rows:
            continue
        width = max(len(party_rows["D"]), len(party_rows["R"]), len(headers))
        for column in range(width):
            metric_label = _cell(headers, column)
            context = _clean_text(f"{table.context} {metric_label}")
            yield (
                context,
                metric_label,
                _cell(party_rows["D"], column),
                _cell(party_rows["R"], column),
                _cell(party_rows.get("Other", []), column),
            )


def _inline_party_candidates(
    texts: Iterable[str], *, metric: str, chamber: str
) -> Iterator[tuple[str, str, Any, Any, Any]]:
    found: dict[str, list[tuple[int, float, str]]] = {"D": [], "R": [], "Other": []}
    for text in texts:
        score = _metric_context_score(text, metric=metric, chamber=chamber)
        if score <= 0:
            continue
        normalized = text.casefold()
        for party, labels in (
            ("D", ("democrats", "democratic", "democrat", "dem")),
            ("R", ("republicans", "republican", "gop", "rep")),
            ("Other", ("independents", "independent", "other")),
        ):
            for label in labels:
                patterns = (
                    rf"\b{label}\b[^\d]{{0,45}}([-+]?\d+(?:\.\d+)?)\s*%?",
                    rf"([-+]?\d+(?:\.\d+)?)\s*%?[^a-z]{{0,25}}\b{label}\b",
                )
                matched = False
                for pattern in patterns:
                    match = re.search(pattern, normalized)
                    if match:
                        found[party].append((score, float(match.group(1)), text))
                        matched = True
                        break
                if matched:
                    break
    if found["D"] and found["R"]:
        d = max(found["D"], key=lambda item: item[0])
        r = max(found["R"], key=lambda item: item[0])
        other = max(found["Other"], key=lambda item: item[0]) if found["Other"] else (0, "", "")
        context = _clean_text(f"{d[2]} {r[2]} {other[2]}")
        yield context, context, d[1], r[1], other[1]


def _metric_label_score(label: str, *, metric: str, chamber: str) -> int:
    """Prefer the actual value column/row over a broad chart title.

    A compact national chart commonly has both ``Chance of Winning`` and
    ``Projected Seats`` columns under a title containing both concepts. The
    chart-level context alone cannot distinguish those columns, so this score
    gives the candidate-specific label decisive weight.
    """

    normalized = _norm(label)
    if not normalized:
        return 0
    probability_terms = ("chance", "probability", "prob", "odds", "majority", "control")
    seat_terms = ("seat", "seats", "projected seats", "expected seats", "average seats")
    vote_terms = (
        "popular vote", "national vote", "house vote", "vote share",
        "national environment", "political environment", "margin",
    )
    if metric == "seats":
        score = 18 if any(term in normalized for term in seat_terms) else 0
        if any(term in normalized for term in probability_terms):
            score -= 22
        if any(term in normalized for term in vote_terms):
            score -= 14
        return score
    if metric == "control":
        score = 18 if any(term in normalized for term in probability_terms) else 0
        if any(term in normalized for term in seat_terms):
            score -= 18
        if any(term in normalized for term in vote_terms):
            score -= 14
        if chamber and chamber in normalized:
            score += 2
        return score
    if metric == "popular_vote":
        score = 18 if any(term in normalized for term in vote_terms) else 0
        if any(term in normalized for term in probability_terms):
            score -= 18
        if any(term in normalized for term in seat_terms):
            score -= 18
        return score
    return 0


def extract_party_metric(
    tables: list[InfogramTable],
    texts: list[str],
    *,
    metric: str,
    chamber: str,
    chamber_size: int,
) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    percentages = metric == "control"
    for table in tables:
        table_context_norm = _norm(table.context)
        if len(table.rows) > 12 and any(word in table_context_norm for word in ("district", "race", "state")):
            continue
        for context, metric_label, d_raw, r_raw, o_raw in (
            list(_party_column_candidates(table)) + list(_party_row_candidates(table))
        ):
            score = (
                _metric_context_score(context, metric=metric, chamber=chamber)
                + _metric_label_score(metric_label, metric=metric, chamber=chamber)
            )
            if score <= 0:
                continue
            values = _normalize_party_values(
                d_raw, r_raw, o_raw,
                target=float(chamber_size),
                percentages=percentages,
            )
            if values is None:
                continue
            if metric == "seats" and values[2] > max(10.0, chamber_size * 0.10):
                # A probability column misread as seats often leaves hundreds
                # of seats in Other. Published chamber projections should not.
                continue
            candidates.append((score, {
                "D": values[0], "R": values[1], "Other": values[2], "context": context,
            }))

    for context, metric_label, d_raw, r_raw, o_raw in _inline_party_candidates(
        texts, metric=metric, chamber=chamber
    ):
        score = (
            _metric_context_score(context, metric=metric, chamber=chamber)
            + _metric_label_score(metric_label, metric=metric, chamber=chamber)
        )
        values = _normalize_party_values(
            d_raw, r_raw, o_raw,
            target=float(chamber_size),
            percentages=percentages,
        )
        if values is not None and not (
            metric == "seats" and values[2] > max(10.0, chamber_size * 0.10)
        ):
            candidates.append((score, {
                "D": values[0], "R": values[1], "Other": values[2], "context": context,
            }))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def extract_house_popular_vote(
    tables: list[InfogramTable], texts: list[str]
) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        if len(table.rows) > 80:
            continue
        for context, metric_label, d_raw, r_raw, o_raw in (
            list(_party_column_candidates(table)) + list(_party_row_candidates(table))
        ):
            score = (
                _metric_context_score(context, metric="popular_vote", chamber="house")
                + _metric_label_score(metric_label, metric="popular_vote", chamber="house")
            )
            if score <= 0:
                continue
            values = _normalize_party_values(
                d_raw, r_raw, o_raw, target=100.0, percentages=True
            )
            if values is not None:
                candidates.append((score + 3, {
                    "D": values[0], "R": values[1], "Other": values[2],
                    "margin": float(rounded(values[0] - values[1])),
                    "context": context,
                }))
        for row in table.rows:
            row_text = _clean_text(" | ".join(row))
            context = _clean_text(f"{table.context} {row_text}")
            score = _metric_context_score(context, metric="popular_vote", chamber="house")
            if score <= 0:
                continue
            margin = _parse_margin_dem(row_text, row_context=context, header_context=context)
            if margin is not None:
                d, r, other = pct_from_margin_dem(margin)
                candidates.append((score, {
                    "D": float(d), "R": float(r), "Other": float(other),
                    "margin": float(margin), "context": context,
                }))

    for text in texts:
        score = _metric_context_score(text, metric="popular_vote", chamber="house")
        if score <= 0:
            continue
        margin = _parse_margin_dem(text, row_context=text, header_context=text)
        if margin is not None:
            d, r, other = pct_from_margin_dem(margin)
            candidates.append((score, {
                "D": float(d), "R": float(r), "Other": float(other),
                "margin": float(margin), "context": text,
            }))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _normalize_timestamp(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def latest_payload_timestamp(*payloads: dict[str, Any]) -> str:
    """Return the latest project-level timestamp exposed by Infogram."""

    keys = {
        "updatedat", "updated_at", "modifiedat", "modified_at",
        "publishedat", "published_at", "lastupdated", "last_updated",
    }
    candidates: list[str] = []
    for payload in payloads:
        for path, node in _iter_nodes(payload):
            if not path:
                continue
            if len(path) > 4:
                continue
            if any(
                _norm(part) in {"theme", "themes", "chart", "charts", "entity", "entities"}
                for part in path[:-1]
            ):
                continue
            key = path[-1].casefold()
            normalized_key = key.replace("-", "_")
            collapsed_key = normalized_key.replace("_", "")
            if normalized_key not in keys and collapsed_key not in keys:
                continue
            timestamp = _normalize_timestamp(node)
            if timestamp:
                candidates.append(timestamp)
    return max(candidates) if candidates else ""


def extract_forecast_date(texts: Iterable[str]) -> str:
    candidates: list[tuple[int, date]] = []
    month_names = "|".join(name.title() for name in _MONTHS)
    pattern = re.compile(
        rf"\b(?P<prefix>last\s+updated|updated|forecast\s+updated|as\s+of|update)\b"
        rf"[^A-Za-z0-9]{{0,30}}(?P<month>{month_names})\s+"
        rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?",
        re.IGNORECASE,
    )
    for text in texts:
        for match in pattern.finditer(text):
            year = int(match.group("year") or 2026)
            month = _MONTHS[match.group("month").casefold()]
            day = int(match.group("day"))
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            prefix = match.group("prefix").casefold()
            score = 3 if "last" in prefix or "as of" in prefix else 1
            candidates.append((score, parsed))
    if not candidates:
        return ""
    max_score = max(score for score, _ in candidates)
    return max(parsed for score, parsed in candidates if score == max_score).isoformat()


def record_to_canonical(record: RaceRecord) -> dict[str, Any]:
    result = dict(record.__dict__)
    result.pop("source_context", None)
    return result


def _without_context(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "context"}


def table_to_diagnostic(table: InfogramTable) -> dict[str, Any]:
    return {
        "entity_path": table.entity_path,
        "title": table.title,
        "sheet_name": table.sheet_name,
        "row_count": len(table.rows),
        "column_count": max((len(row) for row in table.rows), default=0),
        "preview": table.rows[:8],
    }
