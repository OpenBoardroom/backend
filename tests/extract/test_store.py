"""Tests for the extract stage's SQL, against real Postgres."""

from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from openboardroom.extract import store
from openboardroom.extract.llm import LlmSettings, Outcome
from openboardroom.extract.regex import Candidate
from openboardroom.extract.spec import PROMPT_VERSION, SCHEMA_VERSION

SETTINGS = LlmSettings(model="claude-opus-5-5", effort="high", max_tokens=64000)
STARTED = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
CIK = "0000320193"


def add_company(conn: psycopg.Connection, cik: str = CIK, ticker: str = "AAPL") -> int:
    row = conn.execute(
        "insert into companies (cik, ticker, name) values (%s, %s, %s) returning id",
        (cik, ticker, f"{ticker} Inc."),
    ).fetchone()
    assert row is not None
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) values (%s, %s, '1900-01-01')",
        (row[0], cik),
    )
    return row[0]


def add_membership(
    conn: psycopg.Connection, company_id: int, start: str, end: str | None, index: str = "sp500"
) -> None:
    conn.execute(
        "insert into index_memberships (company_id, index_name, start_date, end_date) "
        "values (%s, %s, %s, %s)",
        (company_id, index, start, end),
    )


def add_filing(
    conn: psycopg.Connection,
    company_id: int,
    accession: str,
    form: str = "DEF 14A",
    stage: str = "fetched",
    cik: str = CIK,
) -> int:
    row = conn.execute(
        """
        insert into filings (accession, company_id, cik, form, filed_at, source_url, stage)
        values (%s, %s, %s, %s, '2024-01-11', 'https://www.sec.gov/x', %s) returning id
        """,
        (accession, company_id, cik, form, stage),
    ).fetchone()
    assert row is not None
    return row[0]


def run_row(conn: psycopg.Connection, run_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        "select kind, model, effort, prompt_version, schema_version, input_tokens, "
        "output_tokens, output, status, batch_id, detail, started_at, finished_at "
        "from extraction_runs where id = %s",
        (run_id,),
    )
    row = cursor.fetchone()
    assert row is not None and cursor.description is not None
    return dict(zip([c.name for c in cursor.description], row, strict=True))


# --- membership (ruling R3) ---------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "covered"),
    [
        ("2000-01-01", None, True),
        ("2024-02-28", None, True),
        ("2024-02-29", None, False),
        ("2000-01-01", "2024-02-28", True),
        ("2000-01-01", "2024-02-27", False),
        ("2000-01-01", "2023-12-31", False),
    ],
)
def test_covered_by_index_compares_the_date_to_each_membership(
    conn: psycopg.Connection, start: str, end: str | None, covered: bool
) -> None:
    company = add_company(conn)
    add_membership(conn, company, start, end)

    assert store.covered_by_index(conn, company, date(2024, 2, 28)) is covered


def test_covered_by_index_passes_if_any_membership_covers(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    add_membership(conn, company, "2000-01-01", "2010-01-01", "sp100")
    add_membership(conn, company, "2020-01-01", None, "sp500")

    assert store.covered_by_index(conn, company, date(2024, 2, 28)) is True
    assert store.covered_by_index(conn, company, date(2015, 1, 1)) is False


def test_covered_by_index_ignores_other_companies(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    other = add_company(conn, "0000789019", "MSFT")
    add_membership(conn, other, "2000-01-01", None)

    assert store.covered_by_index(conn, company, date(2024, 2, 28)) is False


# --- selection ----------------------------------------------------------------


def test_pending_filings_takes_fetched_def14a_without_a_submitted_run(
    conn: psycopg.Connection,
) -> None:
    company = add_company(conn)
    second = add_filing(conn, company, "0000320193-24-000002")
    first = add_filing(conn, company, "0000320193-24-000001")
    add_filing(conn, company, "0000320193-24-000003", form="DEFA14A")
    add_filing(conn, company, "0000320193-24-000004", stage="discovered")
    add_filing(conn, company, "0000320193-24-000005", stage="in_review")
    waiting = add_filing(conn, company, "0000320193-24-000006")
    store.insert_llm_run(conn, waiting, SETTINGS, None, STARTED, batch_id="msgbatch_1")
    failed_before = add_filing(conn, company, "0000320193-24-000007")
    failed = Outcome("failed", None, None, None, None, "boom")
    store.insert_llm_run(conn, failed_before, SETTINGS, failed, STARTED)

    filings = store.pending_filings(conn)

    assert [f.id for f in filings] == [second, first, failed_before]
    assert filings[0] == store.Filing(second, "0000320193-24-000002", company)


def billed(conn: psycopg.Connection, filing_id: int, tokens: int | None = 100) -> None:
    outcome = Outcome("failed", None, "claude-opus-5-5", tokens, tokens, "stop_reason max_tokens")
    store.insert_llm_run(conn, filing_id, SETTINGS, outcome, STARTED)


def test_pending_filings_skips_two_billed_failures_at_the_current_versions(
    conn: psycopg.Connection,
) -> None:
    company = add_company(conn)
    once = add_filing(conn, company, "0000320193-24-000001")
    billed(conn, once)
    twice = add_filing(conn, company, "0000320193-24-000002")
    billed(conn, twice)
    billed(conn, twice)
    unbilled = add_filing(conn, company, "0000320193-24-000003")
    billed(conn, unbilled, tokens=None)
    billed(conn, unbilled, tokens=None)
    older = add_filing(conn, company, "0000320193-24-000004")
    billed(conn, older)
    billed(conn, older)
    conn.execute("update extraction_runs set schema_version = '0' where filing_id = %s", (older,))

    assert [f.id for f in store.pending_filings(conn)] == [once, unbilled, older]
    capped = store.capped_filings(conn)
    assert capped == [store.Filing(twice, "0000320193-24-000002", company)]
    assert store.capped_filings(conn, accession="0000320193-24-000001") == []
    assert store.MAX_BILLED_FAILURES == 2


def test_pending_filings_limit_and_accession(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    first = add_filing(conn, company, "0000320193-24-000001")
    second = add_filing(conn, company, "0000320193-24-000002")

    assert [f.id for f in store.pending_filings(conn, limit=1)] == [first]
    only = store.pending_filings(conn, accession="0000320193-24-000002")
    assert [f.id for f in only] == [second]


def test_submitted_runs_groups_by_batch_in_run_order(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    a = add_filing(conn, company, "0000320193-24-000001")
    b = add_filing(conn, company, "0000320193-24-000002")
    c = add_filing(conn, company, "0000320193-24-000003")
    run_b = store.insert_llm_run(conn, b, SETTINGS, None, STARTED, batch_id="msgbatch_2")
    run_a = store.insert_llm_run(conn, a, SETTINGS, None, STARTED, batch_id="msgbatch_1")
    store.insert_llm_run(conn, c, SETTINGS, Outcome("failed", None, None, None, None, "x"), STARTED)

    runs = store.submitted_runs(conn)

    assert [(r.batch_id, r.run_id, r.filing.id) for r in runs] == [
        ("msgbatch_1", run_a, a),
        ("msgbatch_2", run_b, b),
    ]
    assert runs[0].filing == store.Filing(a, "0000320193-24-000001", company)


# --- runs ---------------------------------------------------------------------


def test_insert_regex_run_stores_the_candidates(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    candidates = [Candidate(1, "Proposal No. 1 Election", (3, 40))]

    run = run_row(conn, store.insert_regex_run(conn, filing, candidates))

    assert run["kind"] == "regex"
    assert run["status"] == "succeeded"
    assert run["output"] == [{"number": 1, "heading": "Proposal No. 1 Election", "lines": [3, 40]}]
    assert run["model"] is None
    assert run["finished_at"] is not None


def test_insert_llm_run_records_an_outcome(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    outcome = Outcome("succeeded", {"items": []}, "claude-opus-5", 100, 20, None)

    run = run_row(conn, store.insert_llm_run(conn, filing, SETTINGS, outcome, STARTED))

    assert run == {
        "kind": "llm",
        "model": "claude-opus-5",
        "effort": "high",
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input_tokens": 100,
        "output_tokens": 20,
        "output": {"items": []},
        "status": "succeeded",
        "batch_id": None,
        "detail": None,
        "started_at": STARTED,
        "finished_at": run["finished_at"],
    }
    assert run["finished_at"] is not None


def test_insert_llm_run_falls_back_to_the_requested_model(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    outcome = Outcome("failed", None, None, None, None, "APIConnectionError")

    run = run_row(conn, store.insert_llm_run(conn, filing, SETTINGS, outcome, STARTED))

    assert (run["model"], run["status"], run["detail"]) == (
        "claude-opus-5-5",
        "failed",
        "APIConnectionError",
    )


def test_insert_llm_run_without_an_outcome_is_submitted(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")

    run_id = store.insert_llm_run(conn, filing, SETTINGS, None, STARTED, batch_id="msgbatch_1")
    run = run_row(conn, run_id)

    assert (run["status"], run["batch_id"], run["finished_at"], run["output"]) == (
        "submitted",
        "msgbatch_1",
        None,
        None,
    )


def test_finish_llm_run_records_the_final_outcome(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    run_id = store.insert_llm_run(conn, filing, SETTINGS, None, STARTED, batch_id="msgbatch_1")
    outcome = Outcome("refused", None, "claude-opus-5-5", 900, 12, "refusal: category None")

    store.finish_llm_run(conn, run_id, outcome)

    run = run_row(conn, run_id)
    assert (run["status"], run["batch_id"], run["detail"], run["input_tokens"]) == (
        "refused",
        "msgbatch_1",
        "refusal: category None",
        900,
    )
    assert run["started_at"] == STARTED
    assert run["finished_at"] is not None


def test_finish_llm_run_keeps_the_requested_model_when_none_came_back(
    conn: psycopg.Connection,
) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    run_id = store.insert_llm_run(conn, filing, SETTINGS, None, STARTED, batch_id="msgbatch_1")

    store.finish_llm_run(conn, run_id, Outcome("failed", None, None, None, None, "expired"))

    assert run_row(conn, run_id)["model"] == "claude-opus-5-5"


# --- facts --------------------------------------------------------------------


def test_set_stage_moves_the_filing(conn: psycopg.Connection) -> None:
    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    conn.execute("update filings set stage_updated_at = '2000-01-01' where id = %s", (filing,))

    store.set_stage(conn, filing, "in_review")

    row = conn.execute(
        "select stage, stage_updated_at > '2000-01-01' from filings where id = %s", (filing,)
    ).fetchone()
    assert row == ("in_review", True)


def test_insert_check_results(conn: psycopg.Connection) -> None:
    from openboardroom.extract.checks import CheckResult

    filing = add_filing(conn, add_company(conn), "0000320193-24-000001")
    run_id = store.insert_llm_run(conn, filing, SETTINGS, None, STARTED, batch_id="b")

    store.insert_check_results(
        conn, run_id, [CheckResult("count", True, None), CheckResult("quotes", False, "bad")]
    )

    rows = conn.execute(
        "select check_name, passed, detail from check_results "
        "where extraction_run_id = %s order by id",
        (run_id,),
    ).fetchall()
    assert rows == [("count", True, None), ("quotes", False, "bad")]


def test_insert_meeting_and_item(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    filing_id = add_filing(conn, company, "0000320193-24-000001")
    filing = store.Filing(filing_id, "0000320193-24-000001", company)
    run_id = store.insert_llm_run(conn, filing_id, SETTINGS, None, STARTED, batch_id="b")

    meeting_id = store.insert_meeting(
        conn,
        filing,
        kind="annual",
        meeting_date=date(2024, 2, 28),
        meeting_time="9:00 A.M. Pacific Time",
        format="virtual",
        record_date=None,
        shares_outstanding=15,
        date_quote="held on February 28",
    )
    item = {
        "number": 4,
        "title": "Shareholder proposal",
        "summary": None,
        "type": "shareholder",
        "category": "shareholder_proposal",
        "effect": "precatory",
        "lead_filer": "A Fund",
        "representative": "B",
        "co_filers": ["C", "D"],
        "board_recommendation": "against",
        "recommendation_quote": "q1",
        "vote_base": "votes_cast",
        "vote_threshold_pct": 50.5,
        "abstentions_count_against": True,
        "vote_standard_text": "majority",
        "vote_standard_quote": "q2",
        "request": "a report",
        "nominees": ["X", "Y"],
        "discussion": {"proponent": "The proponent argues p.", "board": None},
        "source_quote": "q3",
    }
    store.insert_item(conn, meeting_id, run_id, item)

    meeting = conn.execute(
        "select company_id, proxy_filing_id, kind, meeting_date, meeting_time, format, "
        "record_date, shares_outstanding, date_quote from meetings where id = %s",
        (meeting_id,),
    ).fetchone()
    assert meeting == (
        company,
        filing_id,
        "annual",
        date(2024, 2, 28),
        "9:00 A.M. Pacific Time",
        "virtual",
        None,
        15,
        "held on February 28",
    )
    row = conn.execute(
        "select id, number, title, type, category, effect, lead_filer, representative, "
        "co_filers, board_recommendation, recommendation_quote, vote_base, "
        "vote_threshold_pct::text, abstentions_count_against, vote_standard_text, "
        "vote_standard_quote, request, status, source_quote, extraction_run_id, published_at "
        "from ballot_items where meeting_id = %s",
        (meeting_id,),
    ).fetchone()
    assert row is not None
    item_id, *values = row
    assert values == [
        "4", "Shareholder proposal", "shareholder", "shareholder_proposal", "precatory",
        "A Fund", "B", ["C", "D"], "against", "q1", "votes_cast", "50.5", True, "majority",
        "q2", "a report", "active", "q3", run_id, None,
    ]  # fmt: skip
    nominees = conn.execute(
        "select position, name from nominees where ballot_item_id = %s order by position",
        (item_id,),
    ).fetchall()
    assert nominees == [(1, "X"), (2, "Y")]
    texts = conn.execute(
        "select kind, version, body, origin, extraction_run_id, review_status "
        "from item_texts where ballot_item_id = %s order by kind",
        (item_id,),
    ).fetchall()
    assert texts == [
        ("board_argument", 1, None, "model", run_id, "pending"),
        ("proponent_argument", 1, "The proponent argues p.", "model", run_id, "pending"),
        ("summary", 1, None, "model", run_id, "pending"),
    ]


def test_insert_item_without_discussion_or_co_filers(conn: psycopg.Connection) -> None:
    company = add_company(conn)
    filing_id = add_filing(conn, company, "0000320193-24-000001")
    filing = store.Filing(filing_id, "0000320193-24-000001", company)
    run_id = store.insert_llm_run(conn, filing_id, SETTINGS, None, STARTED, batch_id="b")
    meeting_id = store.insert_meeting(
        conn,
        filing,
        kind=None,
        meeting_date=None,
        meeting_time=None,
        format=None,
        record_date=None,
        shares_outstanding=None,
        date_quote=None,
    )

    store.insert_item(conn, meeting_id, run_id, {"number": 1, "summary": "S."})

    row = conn.execute(
        "select bi.co_filers, array_agg(t.body order by t.kind) from ballot_items bi "
        "join item_texts t on t.ballot_item_id = bi.id where bi.meeting_id = %s "
        "group by bi.co_filers",
        (meeting_id,),
    ).fetchone()
    assert row == ([], [None, None, "S."])


def test_blank_text_is_stored_as_null(conn: psycopg.Connection) -> None:
    """Ruling R14: the model writes "" for "not stated"; the database keeps NULL for it."""
    company = add_company(conn)
    filing_id = add_filing(conn, company, "0000320193-24-000001")
    filing = store.Filing(filing_id, "0000320193-24-000001", company)
    run_id = store.insert_llm_run(conn, filing_id, SETTINGS, None, STARTED, batch_id="b")
    meeting_id = store.insert_meeting(
        conn,
        filing,
        kind="annual",
        meeting_date=None,
        meeting_time="",
        format=None,
        record_date=None,
        shares_outstanding=None,
        date_quote="  ",
    )
    blank = ("lead_filer", "representative", "recommendation_quote", "vote_standard_text",
             "vote_standard_quote", "request", "title", "source_quote")  # fmt: skip
    item: dict[str, object] = dict.fromkeys(blank, "")
    item.update(number=1, summary=" ", discussion={"proponent": "", "board": "\n"})

    store.insert_item(conn, meeting_id, run_id, item)

    meeting = conn.execute(
        "select meeting_time, date_quote from meetings where id = %s", (meeting_id,)
    ).fetchone()
    assert meeting == (None, None)
    row = conn.execute(
        f"select {', '.join(blank)} from ballot_items where meeting_id = %s", (meeting_id,)
    ).fetchone()
    assert row == (None,) * len(blank)
    bodies = conn.execute("select body from item_texts").fetchall()
    assert bodies == [(None,)] * 3


def test_insert_item_stores_a_lettered_number(conn: psycopg.Connection) -> None:
    # Finding F24: the stored number is the label as printed, "2a".
    company = add_company(conn)
    filing_id = add_filing(conn, company, "0000320193-24-000001")
    filing = store.Filing(filing_id, "0000320193-24-000001", company)
    run_id = store.insert_llm_run(conn, filing_id, SETTINGS, None, STARTED, batch_id="b")
    meeting_id = store.insert_meeting(
        conn,
        filing,
        kind=None,
        meeting_date=None,
        meeting_time=None,
        format=None,
        record_date=None,
        shares_outstanding=None,
        date_quote=None,
    )

    store.insert_item(conn, meeting_id, run_id, {"number": 2, "part": "a", "summary": "S."})
    store.insert_item(conn, meeting_id, run_id, {"number": 2, "part": "b", "summary": "S."})
    store.insert_item(conn, meeting_id, run_id, {"number": 3, "part": "", "summary": "S."})

    rows = conn.execute(
        "select number from ballot_items where meeting_id = %s order by id", (meeting_id,)
    ).fetchall()
    assert rows == [("2a",), ("2b",), ("3",)]
