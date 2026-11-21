"""Tests for company filer IDs: one company, several EDGAR CIKs over time (spec §4)."""

from datetime import date
from pathlib import Path

import psycopg
import pytest

from openboardroom.companies import sync_companies

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
XOM = FIXTURES / "companies_xom.csv"
XOM_CIKS = FIXTURES / "company_ciks_xom.csv"


def test_company_has_multiple_ciks(conn: psycopg.Connection) -> None:
    """XOM's old and new CIKs both belong to the one XOM company, with their dates."""
    sync_companies(conn, XOM, XOM_CIKS)

    rows = conn.execute(
        """
        select c.ticker, c.cik, cc.cik, cc.valid_from, cc.valid_to
        from company_ciks cc join companies c on c.id = cc.company_id
        order by cc.valid_from
        """
    ).fetchall()

    assert rows == [
        ("XOM", "0002115436", "0000034088", date(1994, 3, 4), date(2026, 6, 30)),
        ("XOM", "0002115436", "0002115436", date(2026, 7, 1), None),
    ]


def test_cik_unique_across_companies(conn: psycopg.Connection) -> None:
    """A CIK identifies one filer, so it can't belong to two companies."""
    ids = [
        row[0]
        for row in conn.execute(
            "insert into companies (cik, ticker, name) values "
            "('0000000001', 'A', 'A Co'), ('0000000002', 'B', 'B Co') returning id"
        ).fetchall()
    ]
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) values (%s, '0000000003', %s)",
        (ids[0], date(2020, 1, 1)),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into company_ciks (company_id, cik, valid_from) values (%s, '0000000003', %s)",
            (ids[1], date(2020, 1, 1)),
        )


def test_current_cik_has_open_range(conn: psycopg.Connection) -> None:
    """Every company's current CIK gets an open-ended range, even without a CIK file."""
    sync_companies(conn, FIXTURES / "companies_small.csv")

    missing = conn.execute(
        """
        select c.ticker from companies c
        where not exists (
            select 1 from company_ciks cc
            where cc.company_id = c.id and cc.cik = c.cik and cc.valid_to is null
        )
        """
    ).fetchall()

    assert conn.execute("select count(*) from companies").fetchone() == (3,)
    assert missing == []


def test_filing_records_its_own_cik(conn: psycopg.Connection) -> None:
    """filings.cik is required and must be a 10-digit CIK."""
    company_id = conn.execute(
        "insert into companies (cik, ticker, name) values ('0000000001', 'A', 'A Co') returning id"
    ).fetchone()
    assert company_id is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            insert into filings (accession, company_id, cik, form, filed_at, source_url)
            values ('0000000001-26-000001', %s, '34088', 'DEF 14A', '2026-01-08', 'u')
            """,
            company_id,
        )
