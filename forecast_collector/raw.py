from __future__ import annotations

from pathlib import Path
import re

from .models import RawArtifact


def save_raw_artifacts(
    raw_dir: Path,
    vendor: str,
    run_id: str,
    artifacts: list[RawArtifact],
) -> list[Path]:
    safe_vendor = re.sub(r"[^A-Za-z0-9_.-]+", "_", vendor).strip("_")
    safe_run = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("_")
    target = raw_dir.expanduser().resolve() / safe_vendor / safe_run
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for artifact in artifacts:
        filename = Path(artifact.filename).name
        path = target / filename
        path.write_bytes(artifact.content)
        written.append(path)
    return written
