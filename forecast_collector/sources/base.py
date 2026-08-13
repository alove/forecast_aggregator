from __future__ import annotations

from abc import ABC, abstractmethod

from ..http import HttpClient
from ..models import SourceResult


class ForecastSource(ABC):
    name: str
    slug: str

    @abstractmethod
    def collect(
        self,
        client: HttpClient,
        *,
        observed_datetime_utc: str,
        include_house_districts: bool,
        include_senate_races: bool,
        backfill: bool = False,
    ) -> SourceResult:
        raise NotImplementedError
