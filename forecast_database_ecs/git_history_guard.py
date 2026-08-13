#!/usr/bin/env python3
"""Validate GitHub-canonical election forecast CSV history and describe changes.

The remote files are materialized by sync_forecast_database.sh using `git show`.
This helper deliberately treats the CSVs as append-only: every remote row must
appear in the same position at the start of the candidate local file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Sequence


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        if not header:
            raise ValueError(f"CSV has no header: {path}")
        return header, list(reader)


def schema_version(rows: list[dict[str, str]], *, path: Path) -> str:
    if not rows:
        raise ValueError(f"CSV has no data rows: {path}")
    versions = {row.get("schema_version", "") for row in rows}
    if "" in versions or len(versions) != 1:
        raise ValueError(f"CSV must contain exactly one non-empty schema_version: {path}")
    return next(iter(versions))


def append_only_delta(
    remote_path: Path,
    local_path: Path,
    *,
    label: str,
) -> tuple[str, str, list[dict[str, str]], str]:
    remote_header, remote_rows = read_csv(remote_path)
    local_header, local_rows = read_csv(local_path)
    if remote_header != local_header:
        raise ValueError(f"{label}: local header differs from GitHub canonical header")
    remote_version = schema_version(remote_rows, path=remote_path)
    local_version = schema_version(local_rows, path=local_path)
    if remote_version != local_version:
        raise ValueError(
            f"{label}: schema_version changed from {remote_version!r} to {local_version!r}; "
            "make the schema migration explicit rather than appending through the sync runner"
        )
    if len(local_rows) < len(remote_rows):
        raise ValueError(
            f"{label}: local candidate lost {len(remote_rows) - len(local_rows):,} historical rows"
        )
    if local_rows[: len(remote_rows)] != remote_rows:
        mismatch = next(
            (
                index
                for index, (old, new) in enumerate(
                    zip(remote_rows, local_rows[: len(remote_rows)]), start=2
                )
                if old != new
            ),
            None,
        )
        suffix = f" near CSV line {mismatch}" if mismatch else ""
        raise ValueError(
            f"{label}: existing GitHub history was rewritten or reordered{suffix}; "
            "only appending new rows is permitted"
        )
    return (
        sha256_file(remote_path),
        sha256_file(local_path),
        local_rows[len(remote_rows) :],
        local_version,
    )


def fingerprint(national_sha: str, state_sha: str, schema: str) -> str:
    material = f"{national_sha}\n{state_sha}\n{schema}\n".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def compare(args: argparse.Namespace) -> dict[str, object]:
    remote_national = args.remote_national.resolve()
    remote_state = args.remote_state.resolve()
    local_national = args.local_national.resolve()
    local_state = args.local_state.resolve()

    for path in (remote_national, remote_state, local_national, local_state):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"required CSV is missing or empty: {path}")

    rnat, lnat, nat_added, nat_version = append_only_delta(
        remote_national, local_national, label="national CSV"
    )
    rstate, lstate, state_added, state_version = append_only_delta(
        remote_state, local_state, label="state CSV"
    )
    if nat_version != state_version:
        raise ValueError(
            f"national/state schema versions differ: {nat_version!r} vs {state_version!r}"
        )

    added_rows = nat_added + state_added
    vendors = sorted({row.get("vendor", "").strip() for row in added_rows if row.get("vendor", "").strip()})
    changed = rnat != lnat or rstate != lstate
    if changed and not added_rows:
        raise ValueError(
            "CSV bytes changed but no appended rows were detected; refusing a formatting-only or destructive rewrite"
        )
    if not changed and added_rows:
        raise ValueError("internal comparison inconsistency: appended rows without a SHA change")

    return {
        "changed": changed,
        "vendors": vendors,
        "vendor_count": len(vendors),
        "national_added_rows": len(nat_added),
        "state_added_rows": len(state_added),
        "remote_national_sha256": rnat,
        "remote_state_sha256": rstate,
        "local_national_sha256": lnat,
        "local_state_sha256": lstate,
        "schema_version": nat_version,
        "remote_fingerprint": fingerprint(rnat, rstate, nat_version),
        "local_fingerprint": fingerprint(lnat, lstate, nat_version),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-national", type=Path, required=True)
    parser.add_argument("--remote-state", type=Path, required=True)
    parser.add_argument("--local-national", type=Path, required=True)
    parser.add_argument("--local-state", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "lines"), default="json")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = compare(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr, flush=True)
        return 2
    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        for key in sorted(result):
            value = result[key]
            if isinstance(value, list):
                value = ",".join(value)
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
