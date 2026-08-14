#!/usr/bin/env python3
"""One-time/idempotent migration from export schema 2.0.0 to 2.1.0.

Adds model_web_url to every historical national/state row without changing the
row identity or any forecast value.  The published-model URL is deterministic
from vendor + metric_type, so the migration can be rerun safely.
"""
from __future__ import annotations

import argparse
import csv
import os
import tempfile
from pathlib import Path

from forecast_collector.export import NATIONAL_FIELDNAMES, STATE_FIELDNAMES
from forecast_collector.model_links import model_web_url_for

OLD_VERSION = "2.0.0"
NEW_VERSION = "2.1.0"


def old_fields(new_fields: list[str]) -> list[str]:
    return [field for field in new_fields if field != "model_web_url"]


def migrate(path: Path, expected_new: list[str]) -> tuple[int, bool]:
    if not path.exists():
        raise FileNotFoundError(f"missing canonical CSV: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = list(reader)

    expected_old = old_fields(expected_new)
    if header not in (expected_old, expected_new):
        raise ValueError(
            f"{path}: unexpected header; expected export schema {OLD_VERSION} or {NEW_VERSION}"
        )

    changed = header == expected_old
    for index, row in enumerate(rows, start=2):
        version = (row.get("schema_version") or "").strip()
        if version not in {OLD_VERSION, NEW_VERSION}:
            raise ValueError(f"{path}:{index}: unsupported schema_version {version!r}")
        url = model_web_url_for(row.get("vendor", ""), row.get("metric_type", ""))
        if not url:
            raise ValueError(
                f"{path}:{index}: no published model URL mapping for "
                f"vendor={row.get('vendor')!r} metric_type={row.get('metric_type')!r}"
            )
        if row.get("model_web_url") != url or version != NEW_VERSION:
            changed = True
        row["schema_version"] = NEW_VERSION
        row["model_web_url"] = url

    if not changed:
        return len(rows), False

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=expected_new,
                extrasaction="raise",
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return len(rows), True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "collected_data",
    )
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    targets = [
        (output / "election_forecasts_2026_national.csv", NATIONAL_FIELDNAMES),
        (output / "election_forecasts_2026_state.csv", STATE_FIELDNAMES),
    ]
    any_changed = False
    for path, fields in targets:
        count, changed = migrate(path, fields)
        any_changed |= changed
        print(f"{'MIGRATED' if changed else 'OK'}: {path} ({count:,} rows)")
    print(
        f"Export schema {NEW_VERSION}: "
        + ("historical rows updated with model_web_url" if any_changed else "already current")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
