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
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) values (%s, %s, '1900-01-01')",
        (row[0], cik),
    )
    return row[0]


def _filing(conn: psycopg.Connection, company_id: int, accession: str) -> int:
    row = conn.execute(
        """
        insert into filings (accession, company_id, cik, form, filed_at, source_url)
        values (%s, %s, '0000320193', 'DEF 14A', '2026-01-08', 'https://www.sec.gov/x.htm')
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


def test_category_accepts_share_capital_authority(conn: psycopg.Connection) -> None:
    _insert_item(conn, _meeting(conn), "5", "share_capital_authority")


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


def _review_ids(conn: psycopg.Connection) -> tuple[int, int]:
    """Insert a meeting with one item and one text; return (filing id, item id)."""
    meeting_id = _meeting(conn)
    _insert_item(conn, meeting_id, "1", "other")
    row = conn.execute(
        "select m.proxy_filing_id, bi.id from ballot_items bi "
        "join meetings m on m.id = bi.meeting_id"
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def test_review_decision_rejects_unknown_value(conn: psycopg.Connection) -> None:
    filing_id, _ = _review_ids(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into reviews (filing_id, decision, reviewer) values (%s, 'maybe', 'q')",
            (filing_id,),
        )


def test_review_may_name_an_item(conn: psycopg.Connection) -> None:
    filing_id, item_id = _review_ids(conn)
    conn.execute(
        "insert into reviews (filing_id, ballot_item_id, decision, reviewer) "
        "values (%s, %s, 'fix', 'q')",
        (filing_id, item_id),
    )


def test_deleting_a_reviewed_text_keeps_the_review(conn: psycopg.Connection) -> None:
    filing_id, item_id = _review_ids(conn)
    row = conn.execute(
        "insert into item_texts (ballot_item_id, kind, version, body, origin) "
        "values (%s, 'summary', 1, 'S.', 'model') returning id",
        (item_id,),
    ).fetchone()
    assert row is not None
    conn.execute(
        "insert into reviews (filing_id, item_text_id, ballot_item_id, decision, reviewer) "
        "values (%s, %s, %s, 'approve', 'q')",
        (filing_id, row[0], item_id),
    )
    conn.execute("delete from item_texts where id = %s", (row[0],))
    conn.execute("delete from ballot_items where id = %s", (item_id,))
    assert conn.execute("select item_text_id, ballot_item_id from reviews").fetchall() == [
        (None, None)
    ]


def test_extraction_run_kind_accepts_texts(conn: psycopg.Connection) -> None:
    filing_id, _ = _review_ids(conn)
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status) values (%s, 'texts', 'succeeded')",
        (filing_id,),
    )


def _change(conn: psycopg.Connection, meeting_id: int, item_id: int | None, field: str) -> None:
    conn.execute(
        "insert into pending_changes "
        "(meeting_id, ballot_item_id, field, extraction_run_id, reason) "
        "values (%s, %s, %s, (select max(id) from extraction_runs), 'supplement')",
        (meeting_id, item_id, field),
    )


def test_pending_change_for_added_item_names_meeting_only(conn: psycopg.Connection) -> None:
    _, item_id = _review_ids(conn)
    row = conn.execute("select meeting_id from ballot_items where id = %s", (item_id,)).fetchone()
    assert row is not None
    meeting_id = row[0]
    _change(conn, meeting_id, None, "item")
    _change(conn, meeting_id, item_id, "title")
    for bad_item, field in ((item_id, "item"), (None, "title")):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _change(conn, meeting_id, bad_item, field)


def test_extraction_run_kind_accepts_supplement(conn: psycopg.Connection) -> None:
    filing_id, _ = _review_ids(conn)
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status) "
        "values (%s, 'supplement', 'succeeded')",
        (filing_id,),
    )


def test_review_decision_accepts_change_decisions(conn: psycopg.Connection) -> None:
    filing_id, _ = _review_ids(conn)
    for decision in ("apply_change", "reject_change"):
        conn.execute(
            "insert into reviews (filing_id, decision, reviewer) values (%s, %s, 'q')",
            (filing_id, decision),
        )
