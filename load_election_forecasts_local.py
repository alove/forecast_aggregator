#!/usr/bin/env python3
"""Load f_collector national and state CSV history into a PostgreSQL database.

This optional local loader uses the same validator/schema generator as the ECS
image. It defaults to the local Rhubarb Docker PostgreSQL connection and never
modifies the source CSVs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PACKAGE_DIR = SCRIPT_DIR / "forecast_database_ecs"
IMAGE_DIR = DB_PACKAGE_DIR / "image"
PREPARER = IMAGE_DIR / "prepare_election_forecasts.py"
DEFAULT_DATA_DIR = SCRIPT_DIR / "collected_data"

NATIONAL_TABLE = "election_forecasts_2026_national"
STATE_TABLE = "election_forecasts_2026_state"
METADATA_TABLE = "election_forecasts_2026_load_metadata"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load f_collector national/state CSV history into PostgreSQL."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ELECTION_FORECASTS_DATABASE_URL", ""),
        help="Full PostgreSQL URI. Overrides host/port/database/user/password.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5434)
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    parser.add_argument("--sslmode", default="prefer")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--national-input", type=Path)
    parser.add_argument("--state-input", type=Path)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the CSVs without connecting to PostgreSQL.",
    )
    return parser.parse_args()


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def execute_generated_sql(cursor: Any, path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("\\set")]
    cleaned = "\n".join(lines)
    # The preparer emits ordinary semicolon-terminated SQL and no procedural
    # blocks, so deterministic statement splitting is sufficient here.
    for statement in cleaned.split(";"):
        statement = statement.strip()
        if statement:
            cursor.execute(statement)


def copy_csv(cursor: Any, *, table: str, path: Path) -> None:
    assert sql is not None
    command = sql.SQL(
        "COPY public.{} FROM STDIN WITH "
        "(FORMAT CSV, HEADER TRUE, NULL '', ENCODING 'UTF8')"
    ).format(sql.Identifier(table))
    with cursor.copy(command) as copy:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                copy.write(chunk)


def connection_kwargs(args: argparse.Namespace) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if args.database_url:
        return (args.database_url,), {}
    return (), {
        "host": args.host,
        "port": args.port,
        "dbname": args.database,
        "user": args.user,
        "password": args.password,
        "sslmode": args.sslmode,
    }


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    national_input = (
        args.national_input.expanduser().resolve()
        if args.national_input
        else data_dir / "election_forecasts_2026_national.csv"
    )
    state_input = (
        args.state_input.expanduser().resolve()
        if args.state_input
        else data_dir / "election_forecasts_2026_state.csv"
    )

    validate_command = [
        sys.executable,
        str(PREPARER),
        "--national-input",
        str(national_input),
        "--state-input",
        str(state_input),
        "--validate-only",
    ]
    run(validate_command)
    if args.validate_only:
        return 0

    if psycopg is None or sql is None:
        raise RuntimeError(
            "Missing dependency psycopg. Run: "
            "./forecast_database_ecs/setup_local_loader.sh"
        )

    with tempfile.TemporaryDirectory(prefix="election-forecast-prepare-") as temp_dir:
        prepared = Path(temp_dir)
        run(
            [
                sys.executable,
                str(PREPARER),
                "--national-input",
                str(national_input),
                "--state-input",
                str(state_input),
                "--output-dir",
                str(prepared),
                "--copy-root",
                str(prepared),
            ]
        )

        positional, keyword = connection_kwargs(args)
        destination = (
            "the URI supplied by --database-url/ELECTION_FORECASTS_DATABASE_URL"
            if args.database_url
            else f"{args.host}:{args.port}/{args.database} as {args.user}"
        )
        print(f"Connecting to {destination}")
        with psycopg.connect(*positional, **keyword, autocommit=False) as connection:
            with connection.cursor() as cursor:
                execute_generated_sql(cursor, prepared / "10-schema.sql")
                copy_csv(
                    cursor,
                    table=NATIONAL_TABLE,
                    path=prepared / f"{NATIONAL_TABLE}.csv",
                )
                copy_csv(
                    cursor,
                    table=STATE_TABLE,
                    path=prepared / f"{STATE_TABLE}.csv",
                )
                execute_generated_sql(cursor, prepared / "30-post-load.sql")
                cursor.execute(
                    sql.SQL(
                        "SELECT (SELECT count(*) FROM public.{}), "
                        "       (SELECT count(*) FROM public.{})"
                    ).format(sql.Identifier(NATIONAL_TABLE), sql.Identifier(STATE_TABLE))
                )
                national_count, state_count = cursor.fetchone()
            connection.commit()

    print("Load complete.")
    print(f"  public.{NATIONAL_TABLE}: {national_count:,} rows")
    print(f"  public.{STATE_TABLE}: {state_count:,} rows")
    print(f"  public.{METADATA_TABLE}")
    print("  public.election_forecasts_2026_latest_national")
    print("  public.election_forecasts_2026_latest_state")
    print("  public.election_forecasts_2026_latest_vendor_runs")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
