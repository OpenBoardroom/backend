"""Tests for the extraction stage's constraints: run vocabularies, one meeting per proxy."""

from typing import LiteralString

import psycopg
import pytest


def _one(conn: psycopg.Connection, query: LiteralString, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _filing(conn: psycopg.Connection) -> tuple[int, int]:
    """Insert a company and one filing; return their ids."""
    company_id = _one(
        conn,
        "insert into companies (cik, ticker, name) values "
        "('0000320193', 'AAPL', 'Apple Inc.') returning id",
    )
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) values (%s, '0000320193', "
        "'1900-01-01')",
        (company_id,),
    )
    filing_id = _one(
        conn,
        "insert into filings (accession, company_id, cik, form, filed_at, source_url) "
        "values ('0001308179-26-000008', %s, '0000320193', 'DEF 14A', '2026-01-08', "
        "'https://www.sec.gov/x.htm') returning id",
        (company_id,),
    )
    return company_id, filing_id


def test_one_meeting_per_proxy_filing(conn: psycopg.Connection) -> None:
    """A proxy filing describes one meeting, so a second meeting for it is rejected."""
    company_id, filing_id = _filing(conn)
    insert = "insert into meetings (company_id, proxy_filing_id) values (%s, %s)"
    conn.execute(insert, (company_id, filing_id))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (company_id, filing_id))


@pytest.mark.parametrize("kind", ["regex", "llm", "reconcile"])
def test_extraction_run_accepts_each_kind(conn: psycopg.Connection, kind: str) -> None:
    _, filing_id = _filing(conn)
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status) values (%s, %s, 'succeeded')",
        (filing_id, kind),
    )


@pytest.mark.parametrize("status", ["submitted", "succeeded", "refused", "failed"])
def test_extraction_run_accepts_each_status(conn: psycopg.Connection, status: str) -> None:
    _, filing_id = _filing(conn)
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status) values (%s, 'llm', %s)",
        (filing_id, status),
    )


def test_extraction_run_rejects_unknown_kind(conn: psycopg.Connection) -> None:
    _, filing_id = _filing(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into extraction_runs (filing_id, kind, status) values (%s, 'other', 'failed')",
            (filing_id,),
        )


def test_extraction_run_rejects_unknown_status(conn: psycopg.Connection) -> None:
    _, filing_id = _filing(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into extraction_runs (filing_id, kind, status) values (%s, 'llm', 'done')",
            (filing_id,),
        )


def test_extraction_run_batch_id_and_detail(conn: psycopg.Connection) -> None:
    """A submitted batch run records its batch id; a failed one records why."""
    _, filing_id = _filing(conn)
    row = conn.execute(
        "insert into extraction_runs (filing_id, kind, status, batch_id, detail) "
        "values (%s, 'llm', 'failed', 'msgbatch_1', 'timed out') returning batch_id, detail",
        (filing_id,),
    ).fetchone()
    assert row == ("msgbatch_1", "timed out")


def test_filing_stage_accepts_out_of_scope(conn: psycopg.Connection) -> None:
    _, filing_id = _filing(conn)
    conn.execute("update filings set stage = 'out_of_scope' where id = %s", (filing_id,))
