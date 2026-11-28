"""Tests for the extract stage, against real Postgres with replayed Claude responses."""

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import psycopg
import pytest
from llm_replay import Recorder, api_error, json_response, jsonl_response, mock_client
from llm_replay import stream_response as stream

from openboardroom.extract import store
from openboardroom.extract.llm import LlmSettings
from openboardroom.extract.spec import PROMPT_VERSION, SCHEMA_VERSION
from openboardroom.pipeline.extract import ExtractResult, collect, extract_direct, submit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AAPL_TEXT = (FIXTURES / "filings" / "aapl-2024-def14a.txt").read_text(encoding="utf-8")
SETTINGS = LlmSettings(model="claude-opus-5-5", effort="high", max_tokens=64000)
ACCESSION = "0001308179-24-000010"
SECOND = "0001308179-24-000011"
THIRD = "0001308179-24-000012"
CIK = "0000320193"
BATCH_ID = "msgbatch_fixture_01"
BATCH_URL = f"https://api.anthropic.com/v1/messages/batches/{BATCH_ID}"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "llm" / name).read_text(encoding="utf-8"))


def aapl(edit: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """The AAPL 2024 response, with its JSON output changed by ``edit``."""
    message = load("aapl-2024.message.json")
    if edit is not None:
        block = next(b for b in message["content"] if b["type"] == "text")
        output = json.loads(block["text"])
        edit(output)
        block["text"] = json.dumps(output)
    return message


def setup(
    conn: psycopg.Connection,
    text_dir: Path,
    accessions: tuple[str, ...] = (ACCESSION,),
    membership_end: str | None = None,
) -> None:
    """Add Apple with an S&P 500 membership and a fetched DEF 14A per accession."""
    row = conn.execute(
        "insert into companies (cik, ticker, name) values (%s, 'AAPL', 'Apple Inc.') returning id",
        (CIK,),
    ).fetchone()
    assert row is not None
    company = row[0]
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) values (%s, %s, '1900-01-01')",
        (company, CIK),
    )
    conn.execute(
        "insert into index_memberships (company_id, index_name, start_date, end_date) "
        "values (%s, 'sp500', '2000-01-01', %s)",
        (company, membership_end),
    )
    text_dir.mkdir(parents=True, exist_ok=True)
    for accession in accessions:
        conn.execute(
            """
            insert into filings (accession, company_id, cik, form, filed_at, source_url, stage)
            values (%s, %s, %s, 'DEF 14A', '2024-01-11', 'https://www.sec.gov/x', 'fetched')
            """,
            (accession, company, CIK),
        )
        (text_dir / f"{accession}.txt").write_text(AAPL_TEXT, encoding="utf-8")


def stage(conn: psycopg.Connection, accession: str = ACCESSION) -> str:
    row = conn.execute("select stage from filings where accession = %s", (accession,)).fetchone()
    assert row is not None
    return row[0]


def counts(conn: psycopg.Connection, accession: str = ACCESSION) -> dict[str, int]:
    """Rows this filing owns in each table the stage writes."""
    row = conn.execute(
        """
        with f as (select id from filings where accession = %s),
        m as (select id from meetings where proxy_filing_id = (select id from f)),
        r as (select id from extraction_runs where filing_id = (select id from f)),
        b as (select id from ballot_items where meeting_id in (select id from m))
        select (select count(*) from r), (select count(*) from check_results
                                          where extraction_run_id in (select id from r)),
               (select count(*) from m), (select count(*) from b),
               (select count(*) from nominees where ballot_item_id in (select id from b)),
               (select count(*) from item_texts where ballot_item_id in (select id from b))
        """,
        (accession,),
    ).fetchone()
    assert row is not None
    names = ("runs", "checks", "meetings", "items", "nominees", "texts")
    return dict(zip(names, row, strict=True))


def runs(conn: psycopg.Connection, accession: str = ACCESSION) -> list[tuple[Any, ...]]:
    """(kind, status, batch_id, detail) per run, in id order."""
    return conn.execute(
        "select r.kind, r.status, r.batch_id, r.detail from extraction_runs r "
        "join filings f on f.id = r.filing_id where f.accession = %s order by r.id",
        (accession,),
    ).fetchall()


def checks(conn: psycopg.Connection, accession: str = ACCESSION) -> list[tuple[Any, ...]]:
    return conn.execute(
        "select c.check_name, c.passed, c.detail from check_results c "
        "join extraction_runs r on r.id = c.extraction_run_id "
        "join filings f on f.id = r.filing_id where f.accession = %s order by c.id",
        (accession,),
    ).fetchall()


def direct(
    conn: psycopg.Connection, text_dir: Path, *responses: httpx2.Response, **kwargs: Any
) -> tuple[ExtractResult, Recorder]:
    recorder = Recorder(*responses)
    result = extract_direct(conn, mock_client(recorder), SETTINGS, text_dir, **kwargs)
    return result, recorder


# --- direct mode --------------------------------------------------------------


def test_direct_happy_path(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)

    result, recorder = direct(conn, tmp_path, stream(aapl()))

    assert result == ExtractResult(checked=1, in_review=0, out_of_scope=0, failed=0, submitted=0)
    assert len(recorder.requests) == 1
    assert stage(conn) == "checked"
    assert counts(conn) == {
        "runs": 2, "checks": 4, "meetings": 1, "items": 8, "nominees": 8, "texts": 24,
    }  # fmt: skip
    assert [(kind, status) for kind, status, _, _ in runs(conn)] == [
        ("regex", "succeeded"),
        ("llm", "succeeded"),
    ]
    assert checks(conn) == [
        ("count", True, None),
        ("quotes", True, None),
        ("schema", True, None),
        ("consistency", True, None),
    ]
    meeting = conn.execute("select kind, meeting_date::text, record_date::text from meetings")
    assert meeting.fetchone() == ("annual", "2024-02-28", "2024-01-02")
    names = conn.execute(
        "select n.name from nominees n join ballot_items b on b.id = n.ballot_item_id "
        "where b.number = '1' order by n.position"
    ).fetchall()
    assert [n for (n,) in names] == [
        "Wanda Austin", "Tim Cook", "Alex Gorsky", "Andrea Jung",
        "Art Levinson", "Monica Lozano", "Ron Sugar", "Sue Wagner",
    ]  # fmt: skip
    numbers = conn.execute("select number from ballot_items order by id").fetchall()
    assert [n for (n,) in numbers] == [str(n) for n in range(1, 9)]
    texts = conn.execute(
        "select distinct review_status, origin, version from item_texts"
    ).fetchall()
    assert texts == [("pending", "model", 1)]
    published = conn.execute("select count(*) from ballot_items where published_at is not null")
    assert published.fetchone() == (0,)
    # Every fact row points at the llm run, not the regex run.
    llm = conn.execute("select id from extraction_runs where kind = 'llm'").fetchone()
    assert llm is not None
    refs = conn.execute(
        "select distinct extraction_run_id from ballot_items "
        "union select distinct extraction_run_id from item_texts "
        "union select distinct extraction_run_id from check_results"
    ).fetchall()
    assert refs == [llm]


def test_direct_writes_items_when_a_check_fails(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)

    def paraphrase(output: dict[str, Any]) -> None:
        output["items"][1]["source_quote"] = "Shareholders are asked to approve the auditor firm"

    result, _ = direct(conn, tmp_path, stream(aapl(paraphrase)))

    assert result == ExtractResult(checked=0, in_review=1, out_of_scope=0, failed=0, submitted=0)
    assert stage(conn) == "in_review"
    assert counts(conn)["items"] == 8
    assert counts(conn)["texts"] == 24
    quotes = [row for row in checks(conn) if row[0] == "quotes"]
    assert quotes == [("quotes", False, "item 2: source_quote not found in filing text")]


def test_direct_refusal_goes_to_review_with_nothing_else(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)

    result, _ = direct(conn, tmp_path, stream(load("refusal.json")))

    assert result == ExtractResult(checked=0, in_review=1, out_of_scope=0, failed=0, submitted=0)
    assert stage(conn) == "in_review"
    assert counts(conn) == {
        "runs": 2, "checks": 0, "meetings": 0, "items": 0, "nominees": 0, "texts": 0,
    }  # fmt: skip
    llm = runs(conn)[1]
    assert llm[:2] == ("llm", "refused")
    assert "general_harms" in llm[3]


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (lambda: api_error(500, "api_error", "Internal error"), "InternalServerError"),
        (lambda: stream(load("max-tokens.json")), "stop_reason max_tokens"),
    ],
    ids=["api-error", "max-tokens"],
)
def test_direct_failure_leaves_the_filing_fetched(
    conn: psycopg.Connection,
    tmp_path: Path,
    response: Callable[[], httpx2.Response],
    detail: str,
) -> None:
    setup(conn, tmp_path)

    result, _ = direct(conn, tmp_path, response())

    assert (result.failed, result.checked, result.in_review) == (1, 0, 0)
    assert len(result.errors) == 1
    assert result.errors[0].startswith(f"{ACCESSION}: ")
    assert detail in result.errors[0]
    assert stage(conn) == "fetched"
    assert counts(conn) == {
        "runs": 2, "checks": 0, "meetings": 0, "items": 0, "nominees": 0, "texts": 0,
    }  # fmt: skip
    llm = runs(conn)[1]
    assert llm[:2] == ("llm", "failed")
    assert detail in llm[3]


def test_direct_out_of_scope(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path, membership_end="2023-12-31")

    result, _ = direct(conn, tmp_path, stream(aapl()))

    assert result == ExtractResult(checked=0, in_review=0, out_of_scope=1, failed=0, submitted=0)
    assert stage(conn) == "out_of_scope"
    assert counts(conn) == {
        "runs": 2, "checks": 0, "meetings": 0, "items": 0, "nominees": 0, "texts": 0,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("date", "failing"),
    [
        (None, {"schema", "consistency"}),
        ("", {"consistency"}),
        ("February 28, 2024", {"consistency"}),
        ("20240228", {"consistency"}),
    ],
    ids=["null", "blank", "unparsable", "not-iso"],
)
def test_direct_without_a_usable_date_skips_membership_and_fails_a_check(
    conn: psycopg.Connection, tmp_path: Path, date: str | None, failing: set[str]
) -> None:
    # Out of the index on any date: a usable date would make this out_of_scope.
    setup(conn, tmp_path, membership_end="2001-01-01")

    def set_date(output: dict[str, Any]) -> None:
        output["meeting"]["date"] = date

    result, _ = direct(conn, tmp_path, stream(aapl(set_date)))

    assert result.in_review == 1
    assert stage(conn) == "in_review"
    assert counts(conn)["items"] == 8
    row = conn.execute("select meeting_date from meetings").fetchone()
    assert row == (None,)
    failed = {name for name, passed, _ in checks(conn) if not passed}
    assert failed == failing


def test_direct_unverified_date_goes_to_review_not_out_of_scope(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Ruling R15: a date its quote doesn't back can't decide scope."""
    # In the index through 2024; the model's wrong date (2025) is outside it.
    setup(conn, tmp_path, membership_end="2024-12-31")

    def wrong_year(output: dict[str, Any]) -> None:
        output["meeting"]["date"] = "2025-02-28"

    result, _ = direct(conn, tmp_path, stream(aapl(wrong_year)))

    assert result == ExtractResult(checked=0, in_review=1, out_of_scope=0, failed=0, submitted=0)
    assert stage(conn) == "in_review"
    assert counts(conn)["items"] == 8
    consistency = next(row for row in checks(conn) if row[0] == "consistency")
    assert consistency[1:] == (False, "meeting.date 2025-02-28 does not appear in date_quote")


def test_direct_date_with_an_invented_quote_goes_to_review(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, membership_end="2023-12-31")

    def invent(output: dict[str, Any]) -> None:
        output["meeting"]["date_quote"] = "The meeting is to be held on February 28, 2024 online"

    result, _ = direct(conn, tmp_path, stream(aapl(invent)))

    assert result.in_review == 1
    assert stage(conn) == "in_review"
    failed = {name for name, passed, _ in checks(conn) if not passed}
    assert failed == {"quotes"}


def test_direct_stores_blank_text_as_null(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)

    direct(conn, tmp_path, stream(aapl()))

    row = conn.execute(
        "select lead_filer, representative, request from ballot_items where number = '1'"
    ).fetchone()
    assert row == (None, None, None)
    blank = conn.execute(
        "select count(*) from ballot_items where lead_filer = '' or representative = '' "
        "or request = '' or recommendation_quote = '' or vote_standard_quote = ''"
    ).fetchone()
    assert blank == (0,)


def test_direct_survives_a_nul_in_the_output(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Postgres can't store NUL in text or jsonb; one such response must not stall the run."""
    setup(conn, tmp_path, (ACCESSION, SECOND))

    def nul(output: dict[str, Any]) -> None:
        output["items"][0]["summary"] = "A vote\u0000 on directors."

    result, _ = direct(conn, tmp_path, stream(aapl(nul)), stream(aapl()))

    assert result == ExtractResult(
        checked=1, in_review=1, out_of_scope=0, failed=0, submitted=0, errors=result.errors
    )
    assert len(result.errors) == 1 and result.errors[0].startswith(f"{ACCESSION}: ")
    assert (stage(conn), stage(conn, SECOND)) == ("in_review", "checked")
    row = conn.execute(
        "select status, detail, output->'items'->0->>'summary' from extraction_runs "
        "where kind = 'llm' and filing_id = (select id from filings where accession = %s)",
        (ACCESSION,),
    ).fetchone()
    assert row is not None
    status, detail, summary = row
    assert status == "failed"
    assert detail.startswith("could not write the extraction: ")
    assert detail.endswith("; NUL characters removed from the stored output")
    assert summary == "A vote on directors."


def test_direct_with_a_null_meeting_writes_an_empty_meeting(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)

    def drop_meeting(output: dict[str, Any]) -> None:
        output["meeting"] = None

    result, _ = direct(conn, tmp_path, stream(aapl(drop_meeting)))

    assert result.in_review == 1
    assert counts(conn)["meetings"] == 1
    assert counts(conn)["items"] == 8


def test_direct_is_idempotent(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)
    direct(conn, tmp_path, stream(aapl()))

    result, recorder = direct(conn, tmp_path)

    assert result == ExtractResult(checked=0, in_review=0, out_of_scope=0, failed=0, submitted=0)
    assert recorder.requests == []


def test_direct_retries_a_failed_filing_next_time(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)
    direct(conn, tmp_path, api_error(529, "overloaded_error", "Overloaded"))

    result, _ = direct(conn, tmp_path, stream(aapl()))

    assert result.checked == 1
    assert [(kind, status) for kind, status, _, _ in runs(conn)] == [
        ("regex", "succeeded"),
        ("llm", "failed"),
        ("regex", "succeeded"),
        ("llm", "succeeded"),
    ]


def test_direct_honours_limit_and_accession(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND, THIRD))

    first, _ = direct(conn, tmp_path, stream(aapl()), limit=1)
    third, _ = direct(conn, tmp_path, stream(aapl()), accession=THIRD)

    assert (first.checked, third.checked) == (1, 1)
    assert [stage(conn, a) for a in (ACCESSION, SECOND, THIRD)] == ["checked", "fetched", "checked"]


def test_direct_counts_a_missing_text_file_as_failed(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))
    (tmp_path / f"{ACCESSION}.txt").unlink()

    result, recorder = direct(conn, tmp_path, stream(aapl()))

    assert (result.failed, result.checked) == (1, 1)
    assert result.errors == [f"{ACCESSION}: text file missing: {tmp_path / f'{ACCESSION}.txt'}"]
    assert len(recorder.requests) == 1
    assert stage(conn) == "fetched"
    assert counts(conn)["runs"] == 0
    assert stage(conn, SECOND) == "checked"


def test_direct_skips_defa14a(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)
    conn.execute("update filings set form = 'DEFA14A'")

    result, recorder = direct(conn, tmp_path)

    assert result.checked == 0
    assert recorder.requests == []


def test_direct_rolls_back_a_failed_write(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Rulings R4 and R10: no partial rows; a failed run keeps the output; a person reviews."""
    setup(conn, tmp_path)

    def duplicate(output: dict[str, Any]) -> None:
        output["items"].append(copy.deepcopy(output["items"][0]))

    result, _ = direct(conn, tmp_path, stream(aapl(duplicate)))

    assert (result.failed, result.checked, result.in_review) == (0, 0, 1)
    assert "UniqueViolation" in result.errors[0]
    assert stage(conn) == "in_review"
    assert counts(conn) == {
        "runs": 2, "checks": 0, "meetings": 0, "items": 0, "nominees": 0, "texts": 0,
    }  # fmt: skip
    row = conn.execute(
        "select status, detail, output is not null, input_tokens, output_tokens "
        "from extraction_runs where kind = 'llm'"
    ).fetchone()
    assert row is not None
    status, detail, has_output, input_tokens, output_tokens = row
    assert (status, has_output, input_tokens, output_tokens) == ("failed", True, 107179, 5931)
    assert detail.startswith("could not write the extraction: UniqueViolation: ")
    # The connection is still usable for the next filing.
    assert stage(conn) == "in_review"


def test_direct_sends_a_threshold_the_column_rejects_to_review(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)

    def zero_threshold(output: dict[str, Any]) -> None:
        output["items"][2]["vote_threshold_pct"] = 0

    result, _ = direct(conn, tmp_path, stream(aapl(zero_threshold)))

    assert (result.failed, result.in_review) == (0, 1)
    assert result.errors[0].startswith(
        f"{ACCESSION}: could not write the extraction: CheckViolation: "
    )
    assert stage(conn) == "in_review"
    assert counts(conn) == {
        "runs": 2, "checks": 0, "meetings": 0, "items": 0, "nominees": 0, "texts": 0,
    }  # fmt: skip
    assert runs(conn)[1][:2] == ("llm", "failed")


def billed_failure(conn: psycopg.Connection, prompt_version: str | None = None) -> None:
    """Record a failed llm run that was billed (it has tokens) for ACCESSION."""
    conn.execute(
        """
        insert into extraction_runs (filing_id, kind, model, prompt_version, schema_version,
                                     input_tokens, output_tokens, status, detail)
        select id, 'llm', 'claude-opus-5-5', %s, %s, 107179, 64000, 'failed',
               'stop_reason max_tokens'
        from filings where accession = %s
        """,
        (prompt_version or PROMPT_VERSION, SCHEMA_VERSION, ACCESSION),
    )


def test_direct_skips_and_reports_a_filing_after_two_billed_failures(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))
    billed_failure(conn)
    billed_failure(conn)

    result, recorder = direct(conn, tmp_path, stream(aapl()))
    submitted = submit_all(conn, tmp_path)

    assert result.checked == 1
    assert len(recorder.requests) == 1
    assert stage(conn) == "fetched"
    reason = (
        f"{ACCESSION}: skipped: 2 billed failed llm runs at prompt version {PROMPT_VERSION}, "
        f"schema version {SCHEMA_VERSION}; needs a person"
    )
    assert result.errors == [reason]
    assert (submitted.submitted, submitted.errors) == (0, [reason])


def test_direct_skips_a_capped_filing_named_by_accession(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))
    billed_failure(conn)
    billed_failure(conn)

    other, _ = direct(conn, tmp_path, stream(aapl()), accession=SECOND)
    capped, recorder = direct(conn, tmp_path, accession=ACCESSION)

    # Only the named filing is considered or reported.
    assert (other.checked, other.errors) == (1, [])
    assert recorder.requests == []
    assert capped.checked == 0
    assert len(capped.errors) == 1
    assert capped.errors[0].startswith(f"{ACCESSION}: skipped: ")


def test_direct_still_tries_a_filing_with_unbilled_or_older_failures(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    direct(conn, tmp_path, api_error(529, "overloaded_error", "Overloaded"))
    direct(conn, tmp_path, api_error(529, "overloaded_error", "Overloaded"))
    billed_failure(conn, prompt_version="0")
    billed_failure(conn, prompt_version="0")

    result, _ = direct(conn, tmp_path, stream(aapl()))

    assert (result.checked, result.errors) == (1, [])


def items_not_a_list(output: dict[str, Any]) -> None:
    output["items"] = "none"


def item_not_an_object(output: dict[str, Any]) -> None:
    output["items"][0] = "Proposal 1"


def nominees_not_a_list(output: dict[str, Any]) -> None:
    output["items"][0]["nominees"] = "Tim Cook"


def discussion_not_an_object(output: dict[str, Any]) -> None:
    output["items"][0]["discussion"] = "text"


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (items_not_a_list, "items is not a list"),
        (item_not_an_object, "item 1 is not an object"),
        (nominees_not_a_list, "item 1: nominees is not a list"),
        (discussion_not_an_object, "item 1: discussion is not an object"),
    ],
    ids=["items", "item", "nominees", "discussion"],
)
def test_direct_fails_on_output_it_cannot_write(
    conn: psycopg.Connection,
    tmp_path: Path,
    edit: Callable[[dict[str, Any]], None],
    reason: str,
) -> None:
    setup(conn, tmp_path)

    result, _ = direct(conn, tmp_path, stream(aapl(edit)))

    assert (result.failed, result.in_review) == (0, 1)
    assert f"MalformedOutputError: {reason}" in result.errors[0]
    assert stage(conn) == "in_review"
    assert counts(conn)["meetings"] == 0
    assert counts(conn)["checks"] == 0


# --- batch mode ---------------------------------------------------------------


def batch(status: str) -> httpx2.Response:
    return json_response(
        {
            "id": BATCH_ID,
            "type": "message_batch",
            "processing_status": status,
            "request_counts": dict.fromkeys(
                ("processing", "succeeded", "errored", "canceled", "expired"), 0
            ),
            "created_at": "2026-11-22T00:00:00Z",
            "expires_at": "2026-11-23T00:00:00Z",
            "ended_at": None,
            "archived_at": None,
            "cancel_initiated_at": None,
            "results_url": f"{BATCH_URL}/results" if status == "ended" else None,
        }
    )


def result_line(
    accession: str, message: dict[str, Any] | None, error_type: str = "api_error"
) -> dict[str, Any]:
    if message is None:
        error = {"type": "error", "error": {"type": error_type, "message": "Fixture error."}}
        return {"custom_id": accession, "result": {"type": "errored", "error": error}}
    return {"custom_id": accession, "result": {"type": "succeeded", "message": message}}


def batch_refusal() -> dict[str, Any]:
    lines: list[dict[str, Any]] = json.loads(
        (FIXTURES / "llm" / "batch-results.json").read_text(encoding="utf-8")
    )
    entry = next(line for line in lines if line["custom_id"] == "0000000000-24-000002")
    return entry["result"]["message"]


def submit_all(conn: psycopg.Connection, text_dir: Path, **kwargs: Any) -> ExtractResult:
    recorder = Recorder(batch("in_progress"))
    return submit(conn, mock_client(recorder), SETTINGS, text_dir, **kwargs)


def do_collect(conn: psycopg.Connection, text_dir: Path, *responses: httpx2.Response) -> Any:
    recorder = Recorder(*responses)
    return collect(conn, mock_client(recorder), SETTINGS, text_dir), recorder


def test_submit_records_submitted_runs(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))
    recorder = Recorder(batch("in_progress"))

    result = submit(conn, mock_client(recorder), SETTINGS, tmp_path)

    assert result == ExtractResult(checked=0, in_review=0, out_of_scope=0, failed=0, submitted=2)
    assert len(recorder.requests) == 1
    sent = [r["custom_id"] for r in recorder.body()["requests"]]
    assert sent == [ACCESSION, SECOND]
    for accession in (ACCESSION, SECOND):
        assert runs(conn, accession) == [
            ("regex", "succeeded", None, None),
            ("llm", "submitted", BATCH_ID, None),
        ]
        assert stage(conn, accession) == "fetched"


def test_submit_honours_limit_and_skips_submitted(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))

    first = submit_all(conn, tmp_path, limit=1)
    second = submit_all(conn, tmp_path)
    again = submit(conn, mock_client(Recorder()), SETTINGS, tmp_path)
    nothing, recorder = direct(conn, tmp_path)

    assert (first.submitted, second.submitted, again.submitted) == (1, 1, 0)
    assert nothing.checked == 0
    assert recorder.requests == []
    assert [r[1] for r in runs(conn, SECOND)] == ["succeeded", "submitted"]


def test_submit_counts_a_missing_text_file_as_failed(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND))
    (tmp_path / f"{SECOND}.txt").unlink()

    result = submit_all(conn, tmp_path)

    assert (result.submitted, result.failed) == (1, 1)
    assert result.errors == [f"{SECOND}: text file missing: {tmp_path / f'{SECOND}.txt'}"]
    assert runs(conn, SECOND) == []


def test_submit_limit_counts_only_filings_with_text(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND, THIRD))
    (tmp_path / f"{ACCESSION}.txt").unlink()
    (tmp_path / f"{SECOND}.txt").unlink()

    result = submit_all(conn, tmp_path, limit=1)

    assert (result.submitted, result.failed) == (1, 2)
    assert runs(conn, THIRD)[1][1] == "submitted"
    assert runs(conn, ACCESSION) == []


def test_direct_limit_counts_only_filings_with_text(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND, THIRD))
    (tmp_path / f"{ACCESSION}.txt").unlink()

    result, _ = direct(conn, tmp_path, stream(aapl()), limit=1)

    assert (result.checked, result.failed) == (1, 1)
    assert [stage(conn, a) for a in (ACCESSION, SECOND, THIRD)] == ["fetched", "checked", "fetched"]


def test_submit_records_nothing_when_the_batch_is_refused(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    recorder = Recorder(api_error(500, "api_error", "Internal error"))

    result = submit(conn, mock_client(recorder), SETTINGS, tmp_path)

    assert (result.submitted, result.failed) == (0, 1)
    assert result.errors == ["batch submit failed: InternalServerError: status 500"]
    assert runs(conn) == []


def test_collect_before_the_batch_ends_changes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)

    result, recorder = do_collect(conn, tmp_path, batch("in_progress"))

    assert result == ExtractResult(checked=0, in_review=0, out_of_scope=0, failed=0, submitted=1)
    assert len(recorder.requests) == 1
    assert runs(conn)[1][1] == "submitted"


def test_collect_applies_results_and_retries_directly(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path, (ACCESSION, SECOND, THIRD))
    submit_all(conn, tmp_path)
    results = jsonl_response(
        [
            # Results arrive in any order.
            result_line(THIRD, batch_refusal()),
            result_line(ACCESSION, aapl()),
            result_line(SECOND, None),
        ]
    )

    result, recorder = do_collect(
        conn,
        tmp_path,
        batch("ended"),
        batch("ended"),
        results,
        stream(aapl()),  # SECOND's direct retry
        stream(load("refusal.json")),  # THIRD refused again
    )

    assert result == ExtractResult(checked=2, in_review=1, out_of_scope=0, failed=0, submitted=0)
    assert len(recorder.requests) == 5
    assert [stage(conn, a) for a in (ACCESSION, SECOND, THIRD)] == [
        "checked",
        "checked",
        "in_review",
    ]
    # A batch success finishes the submitted run in place.
    assert [r[:3] for r in runs(conn, ACCESSION)] == [
        ("regex", "succeeded", None),
        ("llm", "succeeded", BATCH_ID),
    ]
    assert counts(conn, ACCESSION)["items"] == 8
    # A retry finishes the batch run with what the batch returned, then adds the direct run.
    second = runs(conn, SECOND)
    assert [r[:3] for r in second] == [
        ("regex", "succeeded", None),
        ("llm", "failed", BATCH_ID),
        ("llm", "succeeded", None),
    ]
    errored = "batch errored: api_error: Fixture error."
    assert second[1][3] == f"{errored}; retried directly"
    assert counts(conn, SECOND) == {
        "runs": 3, "checks": 4, "meetings": 1, "items": 8, "nominees": 8, "texts": 24,
    }  # fmt: skip
    third = runs(conn, THIRD)
    assert [r[:3] for r in third] == [
        ("regex", "succeeded", None),
        ("llm", "refused", BATCH_ID),
        ("llm", "refused", None),
    ]
    assert third[1][3].endswith("; retried directly")
    assert counts(conn, THIRD)["meetings"] == 0
    # Facts point at the run that produced them: the direct one for SECOND.
    row = conn.execute(
        "select distinct b.extraction_run_id = r.id from ballot_items b "
        "join meetings m on m.id = b.meeting_id join filings f on f.id = m.proxy_filing_id "
        "join extraction_runs r on r.filing_id = f.id and r.kind = 'llm' and r.batch_id is null "
        "where f.accession = %s",
        (SECOND,),
    ).fetchall()
    assert row == [(True,)]

    # Nothing is left to collect.
    again, recorder = do_collect(conn, tmp_path)
    assert again == ExtractResult(checked=0, in_review=0, out_of_scope=0, failed=0, submitted=0)
    assert recorder.requests == []


def test_collect_does_not_retry_an_invalid_request(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)
    results = jsonl_response([result_line(ACCESSION, None, "invalid_request_error")])

    result, recorder = do_collect(conn, tmp_path, batch("ended"), batch("ended"), results)

    assert result.failed == 1
    assert len(recorder.requests) == 3  # no direct retry
    detail = "batch errored: invalid_request_error: Fixture error."
    assert result.errors == [f"{ACCESSION}: {detail}"]
    assert runs(conn)[1] == ("llm", "failed", BATCH_ID, detail)
    assert stage(conn) == "fetched"


def test_collect_keeps_a_failed_batch_result_final(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)
    results = jsonl_response([result_line(ACCESSION, load("max-tokens.json"))])

    result, recorder = do_collect(conn, tmp_path, batch("ended"), batch("ended"), results)

    assert result.failed == 1
    assert "stop_reason max_tokens" in result.errors[0]
    assert len(recorder.requests) == 3
    assert runs(conn)[1][:3] == ("llm", "failed", BATCH_ID)
    assert stage(conn) == "fetched"


def test_collect_leaves_runs_submitted_when_results_cannot_be_read(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Ruling R9: a transport error while reading results changes nothing."""
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)

    result, _ = do_collect(
        conn, tmp_path, batch("ended"), batch("ended"), api_error(500, "api_error", "Down")
    )

    assert result.submitted == 1
    assert result.errors == [f"batch {BATCH_ID}: InternalServerError: status 500"]
    assert runs(conn)[1][1] == "submitted"
    assert stage(conn) == "fetched"


def test_collect_leaves_runs_submitted_when_the_status_cannot_be_read(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("unreachable", request=request)

    result = collect(conn, mock_client(unreachable), SETTINGS, tmp_path)

    assert result.submitted == 1
    assert result.errors == [f"batch {BATCH_ID}: APIConnectionError: Connection error."]
    assert runs(conn)[1][1] == "submitted"


def test_collect_fails_a_filing_whose_text_is_gone(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)
    (tmp_path / f"{ACCESSION}.txt").unlink()
    results = jsonl_response([result_line(ACCESSION, aapl())])

    result, _ = do_collect(conn, tmp_path, batch("ended"), batch("ended"), results)

    assert result.failed == 1
    assert result.errors == [f"{ACCESSION}: text file missing: {tmp_path / f'{ACCESSION}.txt'}"]
    status, detail = conn.execute(
        "select status, detail from extraction_runs where kind = 'llm'"
    ).fetchone() or (None, None)
    assert status == "failed"
    assert detail == f"text file missing: {tmp_path / f'{ACCESSION}.txt'}"
    assert stage(conn) == "fetched"


def test_collect_rolls_back_a_failed_write(conn: psycopg.Connection, tmp_path: Path) -> None:
    setup(conn, tmp_path)
    submit_all(conn, tmp_path)

    def duplicate(output: dict[str, Any]) -> None:
        output["items"].append(copy.deepcopy(output["items"][0]))

    results = jsonl_response([result_line(ACCESSION, aapl(duplicate))])

    result, _ = do_collect(conn, tmp_path, batch("ended"), batch("ended"), results)

    assert (result.failed, result.in_review) == (0, 1)
    assert runs(conn)[1][:3] == ("llm", "failed", BATCH_ID)
    assert counts(conn)["meetings"] == 0
    assert stage(conn) == "in_review"


@pytest.mark.parametrize(
    "cancel",
    [lambda: batch("canceling"), lambda: api_error(500, "api_error", "Down")],
    ids=["cancelled", "cancel-fails"],
)
def test_submit_cancels_a_batch_it_cannot_record(
    conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: Callable[[], httpx2.Response],
) -> None:
    setup(conn, tmp_path)

    def lost_connection(*_: object, **__: object) -> int:
        # Fault injection only: the database fails while the runs are recorded.
        raise psycopg.errors.AdminShutdown("terminating connection")

    monkeypatch.setattr(store, "insert_llm_run", lost_connection)
    recorder = Recorder(batch("in_progress"), cancel())

    with pytest.raises(psycopg.errors.AdminShutdown) as raised:
        submit(conn, mock_client(recorder), SETTINGS, tmp_path)

    assert [(r.method, r.url.path) for r in recorder.requests] == [
        ("POST", "/v1/messages/batches"),
        ("POST", f"/v1/messages/batches/{BATCH_ID}/cancel"),
    ]
    assert any(BATCH_ID in note for note in raised.value.__notes__)
    monkeypatch.undo()
    assert runs(conn) == []
