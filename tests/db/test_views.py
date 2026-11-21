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


def _item(
    conn: psycopg.Connection,
    *,
    published: bool,
    cik: str = "0000320193",
    filer_cik: str | None = None,
) -> int:
    """Insert a company, filing, run, meeting, and one ballot item; return the item id.

    The filing is filed under ``filer_cik``, which defaults to the company's ``cik``.
    """
    company_id = _one(
        conn,
        "insert into companies (cik, ticker, name) values (%s, 'AAPL', 'Apple Inc.') returning id",
        (cik,),
    )
    # A filing's CIK must be one of its company's CIKs.
    for company_cik in dict.fromkeys([cik, filer_cik or cik]):
        conn.execute(
            "insert into company_ciks (company_id, cik, valid_from) values (%s, %s, '1900-01-01')",
            (company_id, company_cik),
        )
    filing_id = _one(
        conn,
        """
        insert into filings (accession, company_id, cik, form, filed_at, source_url)
        values ('0001308179-26-000008', %s, %s, 'DEF 14A', '2026-01-08',
                'https://www.sec.gov/x.htm')
        returning id
        """,
        (company_id, filer_cik or cik),
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


def test_filer_cik_is_filing_cik(admin_conn: psycopg.Connection) -> None:
    # ExxonMobil filed its 2026 proxy under its old CIK, then changed filer (spec §4).
    item_id = _item(admin_conn, published=True, cik="0002115436", filer_cik="0000034088")
    _as_web_ro(admin_conn)

    row = admin_conn.execute(
        "select cik, filer_cik from published_ballot_items where id = %s", (item_id,)
    ).fetchone()

    assert row == ("0002115436", "0000034088")


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


def _hidden_rows(conn: psycopg.Connection) -> set[str]:
    """Insert rows no view may show, and return the values that would reveal them.

    Builds one published item (so every view has visible rows to scan) and a
    second company whose only item is unpublished. Every value that exists only
    in hidden rows starts with ``SECRET``, apart from the hidden item's id and the
    hidden filing's CIK, which are returned with them.
    """
    public_id = _item(conn, published=True)
    conn.execute(
        """
        insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
        values (%s, 'summary', 1, 'SECRET pending text', 'model', 'pending')
        """,
        (public_id,),
    )
    company_id = _one(
        conn,
        "insert into companies (cik, ticker, name) values ('0000000001', 'SECRET', 'SECRET co') "
        "returning id",
    )
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) "
        "values (%s, '0000000001', '1900-01-01')",
        (company_id,),
    )
    filing_id = _one(
        conn,
        """
        insert into filings (accession, company_id, cik, form, filed_at, source_url)
        values ('0000000001-26-000001', %s, '0000000001', 'DEF 14A', '2026-01-08', 'SECRET url')
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
        insert into meetings (company_id, proxy_filing_id, kind, date_quote)
        values (%s, %s, 'annual', 'SECRET meeting')
        returning id
        """,
        (company_id, filing_id),
    )
    item_id = _one(
        conn,
        "insert into ballot_items (meeting_id, number, title, extraction_run_id) "
        "values (%s, '1', 'SECRET item', %s) returning id",
        (meeting_id, run_id),
    )
    nominee_id = _one(
        conn,
        "insert into nominees (ballot_item_id, name, position) values (%s, 'SECRET nominee', 1) "
        "returning id",
        (item_id,),
    )
    conn.execute(
        """
        insert into item_texts (ballot_item_id, kind, version, body, origin, review_status)
        values (%s, 'summary', 1, 'SECRET text', 'model', 'approved')
        """,
        (item_id,),
    )
    # Published by its own column, hidden only because its item is unpublished.
    conn.execute(
        """
        insert into vote_results (ballot_item_id, nominee_id, outcome, source_filing_id,
                                  published_at)
        values (%s, %s, 'SECRET result', %s, now())
        """,
        (item_id, nominee_id, filing_id),
    )
    conn.execute(
        """
        insert into corrections (ballot_item_id, field, before, after, reason)
        values (%s, 'status', '"active"', '"withdrawn"', 'SECRET correction')
        """,
        (item_id,),
    )
    return {str(item_id), "0000000001"}


# Each probe is a column whose value comes from a table the view joins or
# filters through, so a predicate pushed into the view would scan hidden rows
# before the view's own filter removes them. Probing a column of the table that
# carries the filter proves nothing: the planner runs the zero-cost
# `published_at is not null` first in the same scan.
LEAK_PROBES = [
    # companies is joined, and holds the hidden-only company.
    ("published_ballot_items", "company_name"),
    # filings is joined too, and the hidden filing's CIK appears in no visible row.
    ("published_ballot_items", "filer_cik"),
    # The view's filter is an exists() on ballot_items, joined after meetings is scanned.
    ("published_meetings", "date_quote"),
    # Only a predicate on a `distinct on` key (ballot_item_id, kind) can be pushed
    # below the distinct and the join to ballot_items, and kind holds no hidden
    # value, so the hidden item's id is the probe.
    ("published_item_texts", "ballot_item_id"),
    # filings is joined, and holds the hidden-only filing.
    ("published_vote_results", "source_url"),
    # corrections is scanned before the join that filters on ballot_items.
    ("public_corrections", "reason"),
]


def _leaked(conn: psycopg.Connection, view: str, column: str) -> list[str]:
    """As web_ro, query ``view`` through a cheap leaky function; return what it saw."""
    seen: list[str] = []
    conn.add_notice_handler(lambda diag: seen.append(diag.message_primary or ""))
    _as_web_ro(conn)
    # web_ro has the default TEMP privilege, so it can create this. Declaring it
    # immutable is a lie an attacker can tell; it lets the planner push it below
    # `distinct on`, which a volatile function can't cross.
    conn.execute(
        """
        create function pg_temp.leak(value text) returns boolean
        language plpgsql immutable cost 0.0000001 as $$
        begin
            raise notice '%', value;
            return true;
        end
        $$
        """
    )
    conn.execute(
        sql.SQL("select * from {} where pg_temp.leak({}::text)").format(
            sql.Identifier(view), sql.Identifier(column)
        )
    ).fetchall()
    return seen


def _secret(values: list[str], hidden_ids: set[str]) -> list[str]:
    return [value for value in values if value.startswith("SECRET") or value in hidden_ids]


@pytest.mark.parametrize(("view", "column"), LEAK_PROBES)
def test_leaky_function_sees_no_hidden_rows(
    admin_conn: psycopg.Connection, view: str, column: str
) -> None:
    hidden_ids = _hidden_rows(admin_conn)

    assert _secret(_leaked(admin_conn, view, column), hidden_ids) == []


@pytest.mark.parametrize(("view", "column"), LEAK_PROBES)
def test_leak_probe_fails_without_barrier(
    admin_conn: psycopg.Connection, view: str, column: str
) -> None:
    # Control for the test above: with the barrier removed (inside this
    # rolled-back transaction), the same probe must see hidden values.
    # Otherwise the test above would pass whether or not the view is a barrier.
    hidden_ids = _hidden_rows(admin_conn)
    admin_conn.execute(
        sql.SQL("alter view {} reset (security_barrier)").format(sql.Identifier(view))
    )

    assert _secret(_leaked(admin_conn, view, column), hidden_ids) != []
