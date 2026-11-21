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
DEFAULT_CIKS_CSV = Path("data/company_ciks.csv")
CSV_COLUMNS = ["cik", "ticker", "name", "index", "start_date", "end_date", "dates_approximate"]
CIKS_CSV_COLUMNS = ["ticker", "cik", "valid_from", "valid_to", "note"]
# valid_from for a current CIK with no row in the CIK file: in use since before
# coverage began, start not recorded (see the company_ciks.valid_from comment).
UNRECORDED_VALID_FROM = date(1900, 1, 1)


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
        dates_approximate: True when the membership dates are not exact (spec §4).
    """

    cik: str
    ticker: str
    name: str
    index: str
    start_date: date
    end_date: date | None
    dates_approximate: bool


@dataclass(frozen=True)
class CompanyCikRow:
    """One row of the company CIKs CSV: a CIK a company filed under, and when.

    Attributes:
        ticker: Ticker of the company in the companies CSV.
        cik: SEC CIK, zero-padded to 10 digits.
        valid_from: First day the company filed under this CIK.
        valid_to: Last day, or None while it is the current CIK.
        note: Free-text explanation (e.g. the filing that changed the filer).
    """

    ticker: str
    cik: str
    valid_from: date
    valid_to: date | None
    note: str | None


@dataclass(frozen=True)
class SyncResult:
    """How many CSV rows a sync added or updated.

    A row is added when it creates a company or a membership, and updated when it
    only changes existing ones (a new ticker or name, or a new end date).
    """

    added: int
    updated: int


@dataclass(frozen=True)
class CikRange:
    """A CIK a company filed under, with the dates it was in use (inclusive)."""

    cik: str
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True)
class MembershipRange:
    """One index membership, with its dates (inclusive)."""

    start: date
    end: date | None


@dataclass(frozen=True)
class CoveredCompany:
    """A company whose index membership overlaps a date window.

    Attributes:
        company_id: The company's id.
        cik: The company's current CIK.
        ticker: Trading symbol.
        membership_start: Earliest start of the memberships overlapping the window.
        membership_end: Latest end of those memberships, or None if one is open.
        ciks: The company's CIK ranges that overlap the window, oldest first.
        memberships: The memberships overlapping the window, oldest first. Unlike
            the merged span, these keep any gap between memberships.
    """

    company_id: int
    cik: str
    ticker: str
    membership_start: date
    membership_end: date | None
    ciks: list[CikRange]
    memberships: list[MembershipRange]


def _pad_cik(raw: str) -> str:
    cik = raw.strip()
    if not cik.isdigit() or len(cik) > 10:
        raise ValueError(f"CIK must be up to 10 digits, got {raw!r}")
    return cik.zfill(10)


def _optional_date(raw: str) -> date | None:
    return date.fromisoformat(raw.strip()) if raw.strip() else None


def _parse_bool(raw: str, column: str) -> bool:
    value = raw.strip()
    if value not in ("true", "false"):
        raise ValueError(f"{column} must be true or false, got {raw!r}")
    return value == "true"


def _read_rows(path: Path, columns: list[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        lines = [line for line in f if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames != columns:
        raise ValueError(f"{path}: expected columns {columns}, got {reader.fieldnames}")
    return list(reader)


def read_companies_csv(path: Path) -> list[CompanyRow]:
    """Read a companies CSV, skipping `#` comment lines.

    Args:
        path: CSV with columns
            `cik,ticker,name,index,start_date,end_date,dates_approximate`.

    Returns:
        The rows, with CIKs zero-padded, dates and flags parsed.

    Raises:
        ValueError: If the header, a CIK, or a `dates_approximate` value is malformed.
    """
    return [
        CompanyRow(
            cik=_pad_cik(r["cik"]),
            ticker=r["ticker"].strip(),
            name=r["name"].strip(),
            index=r["index"].strip(),
            start_date=date.fromisoformat(r["start_date"].strip()),
            end_date=_optional_date(r["end_date"]),
            dates_approximate=_parse_bool(r["dates_approximate"], "dates_approximate"),
        )
        for r in _read_rows(path, CSV_COLUMNS)
    ]


def read_company_ciks_csv(path: Path) -> list[CompanyCikRow]:
    """Read a company CIKs CSV, skipping `#` comment lines.

    Args:
        path: CSV with columns `ticker,cik,valid_from,valid_to,note`.

    Returns:
        The rows, with CIKs zero-padded, dates parsed, and blank notes as None.

    Raises:
        ValueError: If the header or a CIK is malformed.
    """
    return [
        CompanyCikRow(
            ticker=r["ticker"].strip(),
            cik=_pad_cik(r["cik"]),
            valid_from=date.fromisoformat(r["valid_from"].strip()),
            valid_to=_optional_date(r["valid_to"]),
            note=r["note"].strip() or None,
        )
        for r in _read_rows(path, CIKS_CSV_COLUMNS)
    ]


def _find_company(conn: psycopg.Connection, ciks: list[str]) -> int | None:
    """Return the one company that already owns any of `ciks`, or None."""
    rows = conn.execute(
        """
        select company_id from company_ciks where cik = any(%(ciks)s)
        union
        select id from companies where cik = any(%(ciks)s)
        """,
        {"ciks": ciks},
    ).fetchall()
    if len(rows) > 1:
        raise ValueError(f"CIKs {ciks} belong to more than one company")
    return rows[0][0] if rows else None


def _upsert_cik_range(conn: psycopg.Connection, company_id: int, row: CompanyCikRow) -> bool:
    """Insert or update one CIK range; return whether anything changed."""
    result = conn.execute(
        """
        insert into company_ciks (company_id, cik, valid_from, valid_to, note)
        values (%s, %s, %s, %s, %s)
        on conflict (cik) do update
            set valid_from = excluded.valid_from, valid_to = excluded.valid_to,
                note = excluded.note
            where (company_ciks.valid_from, company_ciks.valid_to, company_ciks.note)
                is distinct from (excluded.valid_from, excluded.valid_to, excluded.note)
        returning id
        """,
        (company_id, row.cik, row.valid_from, row.valid_to, row.note),
    ).fetchone()
    return result is not None


def _ensure_open_range(conn: psycopg.Connection, company_id: int, row: CompanyRow) -> bool:
    """Give a current CIK the CIK file doesn't list an open range; return whether one was added.

    An existing range is kept as recorded, so syncing without the CIK file never
    overwrites dates it set, but it must still be open.

    Raises:
        ValueError: If the CIK's existing range has an end date.
    """
    inserted = conn.execute(
        """
        insert into company_ciks (company_id, cik, valid_from) values (%s, %s, %s)
        on conflict (cik) do nothing
        returning id
        """,
        (company_id, row.cik, UNRECORDED_VALID_FROM),
    ).fetchone()
    if inserted is not None:
        return True
    existing = conn.execute(
        "select valid_to from company_ciks where cik = %s", (row.cik,)
    ).fetchone()
    if existing is not None and existing[0] is not None:
        raise ValueError(f"{row.ticker}: current CIK {row.cik} has an end date")
    return False


def sync_companies(
    conn: psycopg.Connection, csv_path: Path, ciks_path: Path | None = None
) -> SyncResult:
    """Upsert companies, their CIK ranges, and their index memberships.

    A company is found by any CIK it has filed under, so moving it to a new
    current CIK (with the old one listed in `ciks_path`) updates it in place.
    Every company's current CIK gets an open-ended range; one not in the CIK
    file starts at `UNRECORDED_VALID_FROM`. Memberships are keyed by company,
    index, and start date. Rows already matching the files are left alone, so a
    repeat sync reports zero changes. Nothing is deleted: a company that leaves
    an index gets an `end_date`.

    Args:
        conn: Connection with write access; the caller commits.
        csv_path: Companies CSV to load.
        ciks_path: Company CIKs CSV to load, or None to record current CIKs only.

    Returns:
        How many companies-CSV rows added or updated data, counting changes to
        that company's CIK ranges with the row.

    Raises:
        ValueError: If a file is malformed, the CIK file names a ticker the
            companies CSV lacks or closes a company's current CIK, or the CIKs
            listed for a company already belong to more than one company.
    """
    rows = read_companies_csv(csv_path)
    cik_rows = read_company_ciks_csv(ciks_path) if ciks_path is not None else []
    tickers = {r.ticker for r in rows}
    ranges: dict[str, list[CompanyCikRow]] = {}
    for cik_row in cik_rows:
        if cik_row.ticker not in tickers:
            raise ValueError(f"{ciks_path}: ticker {cik_row.ticker!r} is not in {csv_path}")
        ranges.setdefault(cik_row.ticker, []).append(cik_row)

    added = updated = 0
    for row in rows:
        own = ranges.get(row.ticker, [])
        listed = next((r for r in own if r.cik == row.cik), None)
        if listed is not None and listed.valid_to is not None:
            raise ValueError(f"{row.ticker}: current CIK {row.cik} has an end date")

        # A row is added when it creates a company or a membership, and updated
        # when it only changes existing data (including a new CIK range).
        row_added = row_updated = False
        company_id = _find_company(conn, [row.cik, *(r.cik for r in own)])
        if company_id is None:
            created = conn.execute(
                "insert into companies (cik, ticker, name) values (%s, %s, %s) returning id",
                (row.cik, row.ticker, row.name),
            ).fetchone()
            assert created is not None
            company_id = created[0]
            row_added = True
        else:
            changed = conn.execute(
                """
                update companies set cik = %s, ticker = %s, name = %s
                where id = %s and (cik, ticker, name) is distinct from (%s, %s, %s)
                """,
                (row.cik, row.ticker, row.name, company_id, row.cik, row.ticker, row.name),
            )
            row_updated = changed.rowcount > 0

        for cik_row in own:
            if _upsert_cik_range(conn, company_id, cik_row):
                row_updated = True
        if listed is None:
            row_updated = _ensure_open_range(conn, company_id, row) or row_updated

        # xmax = 0 only for a freshly inserted tuple, which tells insert from update.
        membership = conn.execute(
            """
            insert into index_memberships
                (company_id, index_name, start_date, end_date, dates_approximate)
            values (%s, %s, %s, %s, %s)
            on conflict (company_id, index_name, start_date) do update
                set end_date = excluded.end_date, dates_approximate = excluded.dates_approximate
                where (index_memberships.end_date, index_memberships.dates_approximate)
                    is distinct from (excluded.end_date, excluded.dates_approximate)
            returning xmax = 0
            """,
            (company_id, row.index, row.start_date, row.end_date, row.dates_approximate),
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
        One entry per company, ordered by ticker, with its overlapping
        memberships (each, and their merged span) and the CIK ranges in use
        during the window.
    """
    companies = conn.execute(
        """
        select c.id, c.cik, c.ticker, min(m.start_date),
            case when bool_or(m.end_date is null) then null else max(m.end_date) end
        from index_memberships m join companies c on c.id = m.company_id
        where m.index_name = %s
          and m.start_date <= %s
          and (m.end_date is null or m.end_date >= %s)
        group by c.id
        order by c.ticker
        """,
        (index, end, start),
    ).fetchall()
    ranges: dict[int, list[CikRange]] = {}
    for company_id, cik, valid_from, valid_to in conn.execute(
        """
        select company_id, cik, valid_from, valid_to from company_ciks
        where company_id = any(%s)
          and valid_from <= %s
          and (valid_to is null or valid_to >= %s)
        order by valid_from
        """,
        ([c[0] for c in companies], end, start),
    ).fetchall():
        ranges.setdefault(company_id, []).append(CikRange(cik, valid_from, valid_to))
    memberships: dict[int, list[MembershipRange]] = {}
    for company_id, start_date, end_date in conn.execute(
        """
        select company_id, start_date, end_date from index_memberships
        where company_id = any(%s)
          and index_name = %s
          and start_date <= %s
          and (end_date is null or end_date >= %s)
        order by start_date
        """,
        ([c[0] for c in companies], index, end, start),
    ).fetchall():
        memberships.setdefault(company_id, []).append(MembershipRange(start_date, end_date))
    return [
        CoveredCompany(
            company_id,
            cik,
            ticker,
            first,
            last,
            ranges.get(company_id, []),
            memberships.get(company_id, []),
        )
        for company_id, cik, ticker, first, last in companies
    ]


def _run_sync(args: argparse.Namespace) -> int:
    with psycopg.connect(database_url_from_env()) as conn:
        result = sync_companies(conn, args.csv, args.ciks)
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
    sync.add_argument(
        "--ciks",
        type=Path,
        default=DEFAULT_CIKS_CSV,
        help=f"Company CIKs CSV to load (default: {DEFAULT_CIKS_CSV})",
    )
    sync.set_defaults(func=_run_sync)
