"""Tests for the core schema: vocabularies, uniqueness, and reversible migrations."""

import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

Dbmate = Callable[..., subprocess.CompletedProcess[str]]

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _company(conn: psycopg.Connection, cik: str = "0000320193") -> int:
    row = conn.execute(
        "insert into companies (cik, ticker, name) values (%s, 'AAPL', 'Apple Inc.') returning id",
        (cik,),
    ).fetchone()
    assert row is not None
    return row[0]


def _filing(conn: psycopg.Connection, company_id: int, accession: str) -> int:
    row = conn.execute(
        """
        insert into filings (accession, company_id, form, filed_at, source_url)
        values (%s, %s, 'DEF 14A', '2026-01-08', 'https://www.sec.gov/x.htm')
        returning id
        """,
        (accession, company_id),
    ).fetchone()
    assert row is not None
    return row[0]


def _meeting(conn: psycopg.Connection) -> int:
    """Insert a company, proxy filing, extraction run, and meeting; return the meeting id."""
    company_id = _company(conn)
    filing_id = _filing(conn, company_id, "0001308179-26-000008")
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status) values (%s, 'llm', 'succeeded')",
        (filing_id,),
    )
    row = conn.execute(
        """
        insert into meetings (company_id, proxy_filing_id, kind, meeting_date)
        values (%s, %s, 'annual', '2026-02-24')
        returning id
        """,
        (company_id, filing_id),
    ).fetchone()
    assert row is not None
    return row[0]


def _insert_item(conn: psycopg.Connection, meeting_id: int, number: str, category: str) -> None:
    conn.execute(
        """
        insert into ballot_items (meeting_id, number, title, category, extraction_run_id)
        values (%s, %s, 'T', %s, (select max(id) from extraction_runs))
        """,
        (meeting_id, number, category),
    )


def test_category_rejects_unknown_value(conn: psycopg.Connection) -> None:
    meeting_id = _meeting(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_item(conn, meeting_id, "1", "merger")


def test_accession_is_unique(conn: psycopg.Connection) -> None:
    company_id = _company(conn)
    _filing(conn, company_id, "0001308179-26-000008")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _filing(conn, company_id, "0001308179-26-000008")


def test_item_number_unique_per_meeting(conn: psycopg.Connection) -> None:
    meeting_id = _meeting(conn)
    _insert_item(conn, meeting_id, "1", "director_election")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_item(conn, meeting_id, "1", "say_on_pay")


def _tables(url: str) -> list[str]:
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            """
            select table_name from information_schema.tables
            where table_schema = 'public' and table_name <> 'schema_migrations'
            order by table_name
            """
        ).fetchall()
    return [row[0] for row in rows]


def test_migrations_round_trip(
    pg_url: str, dbmate: Dbmate, admin_url_for: Callable[[str], str]
) -> None:
    name = f"round_trip_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        url = admin_url_for(name)

        dbmate(url, "up")
        before = _tables(url)
        assert "ballot_items" in before
        assert "vote_results" in before
        assert "published_ballot_items" in before

        # One `down` per migration rolls the database back to empty.
        for _ in MIGRATIONS_DIR.glob("*.sql"):
            dbmate(url, "down")
        assert _tables(url) == []

        dbmate(url, "up")
        assert _tables(url) == before
    finally:
        with psycopg.connect(pg_url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
