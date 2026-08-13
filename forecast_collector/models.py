from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RawArtifact:
    filename: str
    content: bytes


@dataclass
class SourceResult:
    source_name: str
    rows: list[dict[str, Any]]
    raw_artifacts: list[RawArtifact] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
