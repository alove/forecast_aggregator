from .electindex import ElectIndexSource
from .election_statsheet import ElectionStatSheetSource
from .grant_williams import GrantWilliamsSource

ALL_SOURCES = {
    ElectionStatSheetSource.slug: ElectionStatSheetSource,
    ElectIndexSource.slug: ElectIndexSource,
    GrantWilliamsSource.slug: GrantWilliamsSource,
}

__all__ = [
    "ALL_SOURCES",
    "ElectIndexSource",
    "ElectionStatSheetSource",
    "GrantWilliamsSource",
]
