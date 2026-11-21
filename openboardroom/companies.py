"""The covered-company list: reading it from CSV, syncing it, and querying it."""

import argparse
import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import psycopg

from openboardroom.config import database_url_from_env

DEFAULT_CSV = Path("data/companies.csv")
CSV_COLUMNS = ["cik", "ticker", "name", "index", "start_date", "end_date"]


class SubParsers(Protocol):
    """The part of argparse's private subparsers action that `register` uses."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Add a subcommand parser."""
        ...


@dataclass(frozen=True)
class CompanyRow:
    """One row of the companies CSV.

    Attributes:
        cik: SEC CIK, zero-padded to 10 digits.
        ticker: Trading symbol.
        name: Registrant name.
        index: Index the company belongs to (`sp100` or `sp500`).
        start_date: First day of membership.
        end_date: Last day of membership, or None while current.
    """

    cik: str
    ticker: str
    name: str
    index: str
    start_date: date
    end_date: date | None


@dataclass(frozen=True)
class SyncResult:
    """How many CSV rows a sync added or updated.

    A row is added when it creates a company or a membership, and updated when it
    only changes existing ones (a new ticker or name, or a new end date).
    """

    added: int
    updated: int


@dataclass(frozen=True)
class CoveredCompany:
    """A company whose index membership overlaps a date window."""

    company_id: int
    cik: str
    ticker: str
    membership_start: date
    membership_end: date | None


def _pad_cik(raw: str) -> str:
    cik = raw.strip()
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f"CIK must be up to 10 digits, got {raw!r}")
    return cik.zfill(10)


def read_companies_csv(path: Path) -> list[CompanyRow]:
    """Read a companies CSV, skipping `#` comment lines.

    Args:
        path: CSV with columns `cik,ticker,name,index,start_date,end_date`.

    Returns:
        The rows, with CIKs zero-padded and dates parsed.

    Raises:
        ValueError: If the header or a CIK is malformed.
    """
    with path.open(newline="", encoding="utf-8") as f:
        lines = [line for line in f if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames != CSV_COLUMNS:
        raise ValueError(f"{path}: expected columns {CSV_COLUMNS}, got {reader.fieldnames}")
    return [
        CompanyRow(
            cik=_pad_cik(r["cik"]),
            ticker=r["ticker"].strip(),
            name=r["name"].strip(),
            index=r["index"].strip(),
            start_date=date.fromisoformat(r["start_date"].strip()),
            end_date=date.fromisoformat(r["end_date"].strip()) if r["end_date"].strip() else None,
        )
        for r in reader
    ]


def sync_companies(conn: psycopg.Connection, csv_path: Path) -> SyncResult:
    """Upsert companies (by CIK) and memberships (by company, index, start date).

    Rows already matching the CSV are left alone, so a repeat sync reports zero
    changes. Nothing is deleted: a company that leaves an index gets an `end_date`.

    Args:
        conn: Connection with write access; the caller commits.
        csv_path: Companies CSV to load.

    Returns:
        How many CSV rows added or updated data.
    """
    added = updated = 0
    for row in read_companies_csv(csv_path):
        row_added = row_updated = False
        # xmax = 0 only for a freshly inserted tuple, which tells insert from update.
        company = conn.execute(
            """
            insert into companies (cik, ticker, name) values (%s, %s, %s)
            on conflict (cik) do update set ticker = excluded.ticker, name = excluded.name
                where (companies.ticker, companies.name)
                    is distinct from (excluded.ticker, excluded.name)
            returning id, xmax = 0
            """,
            (row.cik, row.ticker, row.name),
        ).fetchone()
        if company is None:
            # Unchanged company: the conditional upsert returned nothing.
            existing = conn.execute("select id from companies where cik = %s", (row.cik,))
            found = existing.fetchone()
            assert found is not None
            company_id = found[0]
        else:
            company_id, row_added = company
            row_updated = not row_added
        membership = conn.execute(
            """
            insert into index_memberships (company_id, index_name, start_date, end_date)
            values (%s, %s, %s, %s)
            on conflict (company_id, index_name, start_date) do update
                set end_date = excluded.end_date
                where index_memberships.end_date is distinct from excluded.end_date
            returning xmax = 0
            """,
            (company_id, row.index, row.start_date, row.end_date),
        ).fetchone()
        if membership is not None:
            row_added = row_added or membership[0]
            row_updated = row_updated or not membership[0]
        if row_added:
            added += 1
        elif row_updated:
            updated += 1
    return SyncResult(added=added, updated=updated)


def covered_companies(
    conn: psycopg.Connection, index: str, start: date, end: date
) -> list[CoveredCompany]:
    """List companies whose membership in `index` overlaps `start`..`end` inclusive.

    Args:
        conn: Database connection.
        index: Index name (`sp100` or `sp500`).
        start: First day of the window.
        end: Last day of the window.

    Returns:
        One entry per overlapping membership, ordered by ticker.
    """
    rows = conn.execute(
        """
        select c.id, c.cik, c.ticker, m.start_date, m.end_date
        from index_memberships m join companies c on c.id = m.company_id
        where m.index_name = %s
          and m.start_date <= %s
          and (m.end_date is null or m.end_date >= %s)
        order by c.ticker, m.start_date
        """,
        (index, end, start),
    ).fetchall()
    return [CoveredCompany(*r) for r in rows]


def _run_sync(args: argparse.Namespace) -> int:
    with psycopg.connect(database_url_from_env()) as conn:
        result = sync_companies(conn, args.csv)
    print(f"companies sync: {result.added} added, {result.updated} updated")
    return 0


def register(subparsers: SubParsers) -> None:
    """Add the `companies` command group to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    companies = subparsers.add_parser("companies", help="Manage the covered-company list.")
    actions = companies.add_subparsers(dest="action", metavar="ACTION", required=True)
    sync = actions.add_parser("sync", help="Load the companies CSV into the database.")
    sync.add_argument(
        "--csv", type=Path, default=DEFAULT_CSV, help=f"CSV to load (default: {DEFAULT_CSV})"
    )
    sync.set_defaults(func=_run_sync)
