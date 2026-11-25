"""Seed extracted filings for the review tests, through the extract stage's own writers."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from openboardroom.extract import store
from openboardroom.extract.checks import CheckResult
from openboardroom.extract.llm import LlmSettings, Outcome

SETTINGS = LlmSettings(model="claude-opus-5-5", effort="high", max_tokens=64000)
STARTED = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PASSED = [CheckResult(name, True, None) for name in ("count", "quotes", "schema", "consistency")]


def items() -> list[dict[str, Any]]:
    """A routine director election and a shareholder proposal with both arguments."""
    return [
        {
            "number": 1, "part": "", "title": "Election of Directors", "type": "management",
            "category": "director_election", "effect": "binding",
            "board_recommendation": "for_each_nominee", "summary": "Elect two directors.",
            "nominees": ["Ann Lee", "Bo Chen"], "discussion": None,
            "source_quote": "to elect the two directors named in this proxy statement",
        },
        {
            "number": 2, "part": "", "title": "Report on Water Use", "type": "shareholder",
            "category": "shareholder_proposal", "effect": "precatory", "lead_filer": "A Fund",
            "board_recommendation": "against", "request": "a report on water use",
            "summary": "A request for a report on water use.",
            "discussion": {
                "proponent": "The proponent argues that water risk is rising.",
                "board": "The board argues that current reporting covers it.",
            },
            "source_quote": "Shareholders request that the Board publish a report on water use",
        },
    ]  # fmt: skip


@dataclass(frozen=True)
class Seeded:
    """Ids of a seeded filing.

    Attributes:
        filing_id: The filing.
        run_id: Its llm run.
        item_ids: Its ballot items, in the order given.
    """

    filing_id: int
    run_id: int
    item_ids: list[int]


def seed_filing(
    conn: psycopg.Connection,
    accession: str = "0000320193-26-000001",
    *,
    stage: str = "checked",
    filed_at: str = "2026-01-10",
    cik: str = "0000320193",
    ticker: str = "AAPL",
    item_list: Sequence[dict[str, Any]] | None = None,
    checks: Sequence[CheckResult] = PASSED,
) -> Seeded:
    """Insert a company (if new), a DEF 14A at ``stage``, and its run, checks, and items."""
    row = conn.execute("select id from companies where cik = %s", (cik,)).fetchone()
    if row is None:
        row = conn.execute(
            "insert into companies (cik, ticker, name) values (%s, %s, %s) returning id",
            (cik, ticker, f"{ticker} Inc."),
        ).fetchone()
        assert row is not None
        conn.execute(
            "insert into company_ciks (company_id, cik, valid_from) values (%s, %s, '1900-01-01')",
            (row[0], cik),
        )
    company_id = row[0]
    filing = conn.execute(
        "insert into filings (accession, company_id, cik, form, filed_at, source_url, stage) "
        "values (%s, %s, %s, 'DEF 14A', %s, 'https://www.sec.gov/Archives/x.htm', %s) "
        "returning id",
        (accession, company_id, cik, filed_at, stage),
    ).fetchone()
    assert filing is not None
    filing_id = filing[0]
    outcome = Outcome("succeeded", {}, "claude-opus-5-5", 100, 10, None)
    run_id = store.insert_llm_run(conn, filing_id, SETTINGS, outcome, STARTED)
    store.insert_check_results(conn, run_id, list(checks))
    meeting_id = store.insert_meeting(
        conn,
        store.Filing(filing_id, accession, company_id),
        kind="annual",
        meeting_date=date(2026, 3, 1),
        meeting_time="9:00 a.m. Pacific Time",
        format="virtual",
        record_date=date(2026, 1, 2),
        shares_outstanding=1000,
        date_quote="held on March 1, 2026",
    )
    for item in item_list if item_list is not None else items():
        store.insert_item(conn, meeting_id, run_id, item)
    ids = conn.execute(
        "select id from ballot_items where meeting_id = %s order by id", (meeting_id,)
    ).fetchall()
    return Seeded(filing_id, run_id, [i for (i,) in ids])
