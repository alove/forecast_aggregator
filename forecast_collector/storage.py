from __future__ import annotations

import csv
import fcntl
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import OutputValidationError
from .schema import FIELDNAMES


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Legacy internal normalized-row key retained for adapter tests."""
    return (
        str(row.get("vendor", "")),
        str(row.get("vendor_run_id", "")),
        str(row.get("row_type", "")),
        str(row.get("source_record_id", "")),
    )


def export_row_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("vendor", "")),
        str(row.get("vendor_run_id", "")),
        str(row.get("metric_type", "")),
        str(row.get("source_record_id", "")),
        str(row.get("party", "")),
    )


def _existing_keys(path: Path, fieldnames: Sequence[str], key_func) -> set[tuple]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != list(fieldnames):
            raise OutputValidationError(
                f"existing CSV header does not match expected schema {fieldnames[0]}..{fieldnames[-1]}"
            )
        return {key_func(row) for row in reader}


def append_export_rows(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    fieldnames: Sequence[str],
) -> tuple[int, int]:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    incoming = list(rows)
    if not incoming:
        return 0, 0

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = _existing_keys(path, fieldnames, export_row_key)
        selected: list[dict[str, Any]] = []
        skipped = 0
        for row in incoming:
            key = export_row_key(row)
            if key in existing:
                skipped += 1
                continue
            selected.append({field: row.get(field, "") for field in fieldnames})
            existing.add(key)

        if selected:
            needs_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fieldnames,
                    extrasaction="raise",
                    lineterminator="\n",
                    quoting=csv.QUOTE_ALL,
                )
                if needs_header:
                    writer.writeheader()
                writer.writerows(selected)
                handle.flush()
                os.fsync(handle.fileno())
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return len(selected), skipped


def read_export_rows(path: Path, *, fieldnames: Sequence[str]) -> list[dict[str, str]]:
    with path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if list(reader.fieldnames or []) != list(fieldnames):
            raise OutputValidationError("CSV header does not match the requested export schema")
        return list(reader)


# Legacy helpers retained so existing downstream imports do not fail. The CLI
# no longer writes the old combined 72-column file.
def append_rows(path: Path, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    incoming = list(rows)
    if not incoming:
        return 0, 0
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = _existing_keys(path, FIELDNAMES, row_key)
        selected = []
        skipped = 0
        for row in incoming:
            key = row_key(row)
            if key in existing:
                skipped += 1
                continue
            selected.append({field: row.get(field, "") for field in FIELDNAMES})
            existing.add(key)
        if selected:
            needs_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n", quoting=csv.QUOTE_ALL)
                if needs_header:
                    writer.writeheader()
                writer.writerows(selected)
                handle.flush()
                os.fsync(handle.fileno())
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return len(selected), skipped


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDNAMES:
            raise OutputValidationError("CSV header does not match the legacy collector schema")
        return list(reader)
