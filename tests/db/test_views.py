"""Tests for the published views: the publication rules of ADR-0003 and spec §7.1."""

from collections.abc import Iterator
from typing import LiteralString

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def admin_conn(pg_url: str) -> Iterator[psycopg.Connection]:
    """Connect as the superuser inside a transaction that is rolled back after the test.

    Tests insert rows here, then ``set local role web_ro`` to read the views as the
    frontend would, in the same transaction, so the uncommitted rows are visible.
    """
    connection = psycopg.connect(pg_url)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _one(conn: psycopg.Connection, query: LiteralString, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _item(conn: psycopg.Connection, *, published: bool) -> int:
    """Insert a company, filing, run, meeting, and one ballot item; return the item id."""
    company_id = _one(
        conn,
        "insert into companies (cik, ticker, name) values ('0000320193', 'AAPL', 'Apple Inc.') "
        "returning id",
    )
    filing_id = _one(
        conn,
        """
        insert into filings (accession, company_id, form, filed_at, source_url)
        values ('0001308179-26-000008', %s, 'DEF 14A', '2026-01-08', 'https://www.sec.gov/x.htm')
        returning id
        """,
        (company_id,),
    )
    run_id = _one(
        conn,
        "insert into extraction_runs (filing_id, kind, status) values (%s, 'llm', 'succeeded') "
        "returning id",
        (filing_id,),
    )
    meeting_id = _one(
        conn,
        """
        insert into meetings (company_id, proxy_filing_id, kind, meeting_date)
        values (%s, %s, 'annual', '2026-02-24')
        returning id
        """,
        (company_id, filing_id),
    )
    return _one(
        conn,
        """
        insert into ballot_items (
            meeting_id, number, title, category, board_recommendation,
            extraction_run_id, published_at
        )
        values (%s, '1', 'Elect directors', 'director_election', 'for_each_nominee', %s, %s)
        returning id
        """,
        (meeting_id, run_id, "2026-01-09T00:00:00Z" if published else None),
    )


def _as_web_ro(conn: psycopg.Connection) -> None:
    conn.execute("set local role web_ro")


def test_unpublished_item_hidden(admin_conn: psycopg.Connection) -> None:
    published = _item(admin_conn, published=True)
    admin_conn.execute(
        """
        insert into ballot_items (meeting_id, number, title, extraction_run_id)
        select meeting_id, '2', 'Unpublished', extraction_run_id from ballot_items where id = %s
        """,
        (published,),
    )
    _as_web_ro(admin_conn)

    items = admin_conn.execute("select number from published_ballot_items").fetchall()
    meetings = admin_conn.execute("select count(*) from published_meetings").fetchone()

    assert items == [("1",)]
    assert meetings == (1,)


def test_meeting_without_published_items_hidden(admin_conn: psycopg.Connection) -> None:
    _item(admin_conn, published=False)
    _as_web_ro(admin_conn)

    assert admin_conn.execute("select count(*) from published_meetings").fetchone() == (0,)


def test_only_latest_approved_text(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    for version, status in [(1, "approved"), (2, "approved"), (3, "pending")]:
        admin_conn.execute(
            """
            insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
            values (%s, 'summary', %s, %s, 'model', %s)
            """,
            (item_id, version, f"v{version}", status),
        )
    _as_web_ro(admin_conn)

    rows = admin_conn.execute(
        "select kind, version, body from published_item_texts where ballot_item_id = %s",
        (item_id,),
    ).fetchall()

    assert rows == [("summary", 2, "v2")]


def test_texts_of_unpublished_item_hidden(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=False)
    admin_conn.execute(
        """
        insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
        values (%s, 'summary', 1, 'text', 'model', 'approved')
        """,
        (item_id,),
    )
    _as_web_ro(admin_conn)

    assert admin_conn.execute("select count(*) from published_item_texts").fetchone() == (0,)


def test_pending_change_flagged_value_unchanged(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    admin_conn.execute(
        """
        insert into pending_changes (ballot_item_id, field, proposed, extraction_run_id, reason)
        select id, 'board_recommendation', '"against"', extraction_run_id, 'rerun'
        from ballot_items where id = %s
        """,
        (item_id,),
    )
    _as_web_ro(admin_conn)

    row = admin_conn.execute(
        "select has_pending_change, board_recommendation from published_ballot_items where id = %s",
        (item_id,),
    ).fetchone()

    assert row == (True, "for_each_nominee")


def test_resolved_change_not_flagged(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    admin_conn.execute(
        """
        insert into pending_changes
            (ballot_item_id, field, proposed, extraction_run_id, reason, status)
        select id, 'title', '"New"', extraction_run_id, 'rerun', 'rejected'
        from ballot_items where id = %s
        """,
        (item_id,),
    )
    _as_web_ro(admin_conn)

    row = admin_conn.execute(
        "select has_pending_change from published_ballot_items where id = %s", (item_id,)
    ).fetchone()

    assert row == (False,)


def test_item_carries_company_meeting_and_source(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    _as_web_ro(admin_conn)

    row = admin_conn.execute(
        "select cik, ticker, company_name, meeting_date::text, source_url "
        "from published_ballot_items where id = %s",
        (item_id,),
    ).fetchone()

    assert row == (
        "0000320193",
        "AAPL",
        "Apple Inc.",
        "2026-02-24",
        "https://www.sec.gov/x.htm",
    )


def test_only_published_vote_results(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    nominee_id = _one(
        admin_conn,
        "insert into nominees (ballot_item_id, name, position) values (%s, 'A. Director', 1) "
        "returning id",
        (item_id,),
    )
    filing_id = _one(admin_conn, "select max(id) from filings")
    admin_conn.execute(
        """
        insert into vote_results (ballot_item_id, nominee_id, votes_for, source_filing_id,
                                  published_at)
        values (%s, null, 10, %s, null), (%s, %s, 20, %s, now())
        """,
        (item_id, filing_id, item_id, nominee_id, filing_id),
    )
    _as_web_ro(admin_conn)

    rows = admin_conn.execute(
        "select nominee_name, votes_for from published_vote_results"
    ).fetchall()

    assert rows == [("A. Director", 20)]


def test_public_corrections_joined(admin_conn: psycopg.Connection) -> None:
    item_id = _item(admin_conn, published=True)
    admin_conn.execute(
        """
        insert into corrections (ballot_item_id, field, before, after, reason)
        values (%s, 'status', '"active"', '"withdrawn"', 'DEFA14A withdrawal')
        """,
        (item_id,),
    )
    _as_web_ro(admin_conn)

    row = admin_conn.execute(
        "select number, title, ticker, meeting_date::text, field, reason from public_corrections"
    ).fetchone()

    assert row == ("1", "Elect directors", "AAPL", "2026-02-24", "status", "DEFA14A withdrawal")


def test_web_ro_cannot_read_tables(role_urls: dict[str, str]) -> None:
    # A real web_ro login, not `set role`, so the test checks the grants the
    # frontend's Hyperdrive connection will have.
    with psycopg.connect(role_urls["web_ro"]) as web_ro:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web_ro.execute("select * from ballot_items")
        web_ro.rollback()

        web_ro.execute("select * from published_ballot_items").fetchall()


def test_correction_on_unpublished_item_hidden(admin_conn: psycopg.Connection) -> None:
    # A DEFA14A change can write a correction before the item publishes (spec §7.1);
    # the log must not reveal the unpublished item (ADR-0003 §2).
    item_id = _item(admin_conn, published=False)
    admin_conn.execute(
        """
        insert into corrections (ballot_item_id, field, before, after, reason)
        values (%s, 'status', '"active"', '"withdrawn"', 'DEFA14A withdrawal')
        """,
        (item_id,),
    )
    _as_web_ro(admin_conn)

    assert admin_conn.execute("select count(*) from public_corrections").fetchone() == (0,)


def _unpublished_secrets(conn: psycopg.Connection) -> None:
    """Insert one hidden row per view, each holding a value that starts with ``SECRET``."""
    item_id = _item(conn, published=False)
    conn.execute("update ballot_items set title = 'SECRET item' where id = %s", (item_id,))
    conn.execute("update meetings set date_quote = 'SECRET meeting'")
    filing_id = _one(conn, "select max(id) from filings")
    conn.execute(
        """
        insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
        values (%s, 'summary', 1, 'SECRET text', 'model', 'approved')
        """,
        (item_id,),
    )
    conn.execute(
        "insert into vote_results (ballot_item_id, outcome, source_filing_id) "
        "values (%s, 'SECRET result', %s)",
        (item_id, filing_id),
    )
    conn.execute(
        """
        insert into corrections (ballot_item_id, field, before, after, reason)
        values (%s, 'status', '"active"', '"withdrawn"', 'SECRET correction')
        """,
        (item_id,),
    )
    # Rows hidden by their own status on a published item, in a second meeting
    # so the first meeting stays unpublished.
    published_id = _one(
        conn,
        """
        with m as (
            insert into meetings (company_id, proxy_filing_id, kind)
            select company_id, proxy_filing_id, 'special' from meetings limit 1
            returning id
        )
        insert into ballot_items (meeting_id, number, title, extraction_run_id, published_at)
        select m.id, '1', 'Public', (select max(id) from extraction_runs), now() from m
        returning id
        """,
    )
    conn.execute(
        """
        insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
        values (%s, 'summary', 1, 'SECRET pending text', 'model', 'pending')
        """,
        (published_id,),
    )
    conn.execute(
        "insert into vote_results (ballot_item_id, outcome, source_filing_id) "
        "values (%s, 'SECRET unreconciled result', %s)",
        (published_id, filing_id),
    )


@pytest.mark.parametrize(
    ("view", "column"),
    [
        ("published_ballot_items", "title"),
        ("published_meetings", "date_quote"),
        ("published_item_texts", "body"),
        ("published_vote_results", "outcome"),
        ("public_corrections", "reason"),
    ],
)
def test_leaky_function_sees_no_hidden_rows(
    admin_conn: psycopg.Connection, view: str, column: str
) -> None:
    # web_ro may create temp functions. A cheap one in a WHERE clause would be
    # planned below the view's own filter, and see hidden rows, unless the view
    # is a security barrier.
    _unpublished_secrets(admin_conn)
    _as_web_ro(admin_conn)
    seen: list[str] = []
    admin_conn.add_notice_handler(lambda diag: seen.append(diag.message_primary or ""))
    admin_conn.execute(
        """
        create function pg_temp.leak(value text) returns boolean
        language plpgsql cost 0.0000001 as $$
        begin
            raise notice '%', value;
            return true;
        end
        $$
        """
    )

    admin_conn.execute(
        sql.SQL("select * from {} where pg_temp.leak({}::text)").format(
            sql.Identifier(view), sql.Identifier(column)
        )
    ).fetchall()

    assert [value for value in seen if value.startswith("SECRET")] == []
