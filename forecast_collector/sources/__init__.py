from .electindex import ElectIndexSource
from .election_statsheet import ElectionStatSheetSource
from .grant_williams import GrantWilliamsSource
from .kalshi import KalshiSource
from .race_to_the_wh import RaceToTheWHSource

ALL_SOURCES = {
    ElectionStatSheetSource.slug: ElectionStatSheetSource,
    ElectIndexSource.slug: ElectIndexSource,
    GrantWilliamsSource.slug: GrantWilliamsSource,
    KalshiSource.slug: KalshiSource,
    RaceToTheWHSource.slug: RaceToTheWHSource,
}

__all__ = [
    "ALL_SOURCES",
    "ElectIndexSource",
    "ElectionStatSheetSource",
    "GrantWilliamsSource",
    "KalshiSource",
    "RaceToTheWHSource",
]
