"""All SQL for the extract stage.

The functions here neither commit nor open transactions; the stage
(`openboardroom.pipeline.extract`) decides the transaction boundaries.
"""

import dataclasses
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from openboardroom.extract.checks import CheckResult
from openboardroom.extract.llm import LlmSettings, Outcome
from openboardroom.extract.regex import Candidate
from openboardroom.extract.spec import PROMPT_VERSION, SCHEMA_VERSION

# One model-written text per kind, from these keys of an output item.
_TEXT_KINDS = ("summary", "proponent_argument", "board_argument")


@dataclasses.dataclass(frozen=True)
class Filing:
    """A proxy filing awaiting extraction.

    Attributes:
        id: `filings.id`.
        accession: EDGAR accession number; also the batch `custom_id`.
        company_id: The filing's company.
    """

    id: int
    accession: str
    company_id: int


@dataclasses.dataclass(frozen=True)
class SubmittedRun:
    """An llm run waiting on a message batch.

    Attributes:
        run_id: `extraction_runs.id`.
        batch_id: The batch it was submitted in.
        filing: The filing it extracts.
    """

    run_id: int
    batch_id: str
    filing: Filing


def pending_filings(
    conn: psycopg.Connection, limit: int | None = None, accession: str | None = None
) -> list[Filing]:
    """Return fetched DEF 14A filings that are not waiting on a batch, in id order.

    Args:
        conn: Database connection.
        limit: At most this many filings; all when None.
        accession: Only this filing, when given.

    Returns:
        The filings to extract.
    """
    rows = conn.execute(
        """
        select f.id, f.accession, f.company_id from filings f
        where f.form = 'DEF 14A' and f.stage = 'fetched'
          and (%(accession)s::text is null or f.accession = %(accession)s)
          and not exists (
              select 1 from extraction_runs r
              where r.filing_id = f.id and r.status = 'submitted'
          )
        order by f.id
        limit %(limit)s
        """,
        {"accession": accession, "limit": limit},
    ).fetchall()
    return [Filing(*row) for row in rows]


def submitted_runs(conn: psycopg.Connection) -> list[SubmittedRun]:
    """Return every llm run still waiting on a batch, by batch id, then run id.

    Args:
        conn: Database connection.

    Returns:
        The runs, so each batch's runs are adjacent.
    """
    rows = conn.execute(
        """
        select r.id, r.batch_id, f.id, f.accession, f.company_id
        from extraction_runs r join filings f on f.id = r.filing_id
        where r.kind = 'llm' and r.status = 'submitted'
        order by r.batch_id, r.id
        """
    ).fetchall()
    return [SubmittedRun(run_id, batch_id, Filing(*filing)) for run_id, batch_id, *filing in rows]


def _id(cursor: psycopg.Cursor[Any]) -> int:
    row = cursor.fetchone()
    assert row is not None  # an insert ... returning always returns its row
    return row[0]


def insert_regex_run(conn: psycopg.Connection, filing_id: int, candidates: list[Candidate]) -> int:
    """Record the regex pass, with its candidates as the run's output.

    Args:
        conn: Database connection.
        filing_id: The filing.
        candidates: What `find_candidates` returned.

    Returns:
        The run id.
    """
    output = [dataclasses.asdict(c) for c in candidates]
    return _id(
        conn.execute(
            "insert into extraction_runs (filing_id, kind, output, status, finished_at) "
            "values (%s, 'regex', %s, 'succeeded', now()) returning id",
            (filing_id, Jsonb(output)),
        )
    )


def insert_llm_run(
    conn: psycopg.Connection,
    filing_id: int,
    settings: LlmSettings,
    outcome: Outcome | None,
    started_at: datetime,
    batch_id: str | None = None,
) -> int:
    """Record a model call.

    Args:
        conn: Database connection.
        filing_id: The filing.
        settings: Model settings of the request.
        outcome: The call's outcome; None for a request submitted in a batch,
            which is recorded as `submitted` and finished later.
        started_at: When the request was sent.
        batch_id: The batch, for a batch request.

    Returns:
        The run id. The model is the one that answered, or the requested one
        when no response named it.
    """
    status = "submitted" if outcome is None else outcome.status
    model = outcome.model if outcome is not None and outcome.model else settings.model
    return _id(
        conn.execute(
            """
            insert into extraction_runs (
                filing_id, kind, model, effort, prompt_version, schema_version,
                input_tokens, output_tokens, output, status, batch_id, detail,
                started_at, finished_at
            )
            values (%s, 'llm', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    case when %s = 'submitted' then null else now() end)
            returning id
            """,
            (
                filing_id,
                model,
                settings.effort,
                PROMPT_VERSION,
                SCHEMA_VERSION,
                outcome.input_tokens if outcome else None,
                outcome.output_tokens if outcome else None,
                Jsonb(outcome.output) if outcome and outcome.output is not None else None,
                status,
                batch_id,
                outcome.detail if outcome else None,
                started_at,
                status,
            ),
        )
    )


def finish_llm_run(conn: psycopg.Connection, run_id: int, outcome: Outcome) -> None:
    """Record the final outcome of a submitted run, keeping its batch id and start time.

    Args:
        conn: Database connection.
        run_id: The submitted run.
        outcome: What the batch returned (or why it couldn't be used).
    """
    conn.execute(
        """
        update extraction_runs
        set status = %s, model = coalesce(%s, model), input_tokens = %s, output_tokens = %s,
            output = %s, detail = %s, finished_at = now()
        where id = %s
        """,
        (
            outcome.status,
            outcome.model,
            outcome.input_tokens,
            outcome.output_tokens,
            Jsonb(outcome.output) if outcome.output is not None else None,
            outcome.detail,
            run_id,
        ),
    )


def set_stage(conn: psycopg.Connection, filing_id: int, stage: str) -> None:
    """Move a filing to `stage`.

    Args:
        conn: Database connection.
        filing_id: The filing.
        stage: The new stage.
    """
    conn.execute(
        "update filings set stage = %s, stage_updated_at = now() where id = %s",
        (stage, filing_id),
    )


def covered_by_index(conn: psycopg.Connection, company_id: int, day: date) -> bool:
    """Whether any of the company's index memberships covers `day` (ruling R3).

    Args:
        conn: Database connection.
        company_id: The company.
        day: The meeting date.

    Returns:
        True when some membership starts on or before `day` and has not ended
        before it.
    """
    row = conn.execute(
        """
        select exists (
            select 1 from index_memberships
            where company_id = %(company)s and start_date <= %(day)s
              and (end_date is null or end_date >= %(day)s)
        )
        """,
        {"company": company_id, "day": day},
    ).fetchone()
    assert row is not None
    return row[0]


def insert_check_results(conn: psycopg.Connection, run_id: int, results: list[CheckResult]) -> None:
    """Record check results against the llm run they judged.

    Args:
        conn: Database connection.
        run_id: The llm run.
        results: What `run_checks` returned.
    """
    with conn.cursor() as cursor:
        cursor.executemany(
            "insert into check_results (extraction_run_id, check_name, passed, detail) "
            "values (%s, %s, %s, %s)",
            [(run_id, r.name, r.passed, r.detail) for r in results],
        )


def insert_meeting(
    conn: psycopg.Connection,
    filing: Filing,
    *,
    kind: object,
    meeting_date: date | None,
    meeting_time: object,
    format: object,  # noqa: A002 - the column's name
    record_date: date | None,
    shares_outstanding: object,
    date_quote: object,
) -> int:
    """Record the meeting a proxy filing describes.

    Values other than the dates are written as the model gave them; a value
    the columns reject raises, and the caller rolls the filing back.

    Args:
        conn: Database connection.
        filing: The proxy filing.
        kind: `annual`, `special`, or None.
        meeting_date: Meeting date, or None when absent or unparsable.
        meeting_time: Time as written, with time zone.
        format: `virtual`, `in_person`, `hybrid`, or None.
        record_date: Record date, or None.
        shares_outstanding: Shares outstanding on the record date.
        date_quote: The quote that states the date.

    Returns:
        The meeting id.
    """
    return _id(
        conn.execute(
            """
            insert into meetings (
                company_id, proxy_filing_id, kind, meeting_date, meeting_time, format,
                record_date, shares_outstanding, date_quote
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                filing.company_id,
                filing.id,
                kind,
                meeting_date,
                meeting_time,
                format,
                record_date,
                shares_outstanding,
                date_quote,
            ),
        )
    )


def insert_item(
    conn: psycopg.Connection, meeting_id: int, run_id: int, item: Mapping[str, Any]
) -> None:
    """Record one ballot item with its nominees and its three pending texts.

    `item` is one entry of the model's `items`, already known to be an object
    whose `nominees` is a list and whose `discussion` is an object or null.
    The item number is stored as text. Texts are version 1, origin `model`,
    review status `pending`, with a null body when the model gave none.

    Args:
        conn: Database connection.
        meeting_id: The meeting.
        run_id: The llm run that produced the item.
        item: The item's fields.
    """
    number = item.get("number")
    item_id = _id(
        conn.execute(
            """
            insert into ballot_items (
                meeting_id, number, title, type, category, effect, lead_filer, representative,
                co_filers, board_recommendation, recommendation_quote, vote_base,
                vote_threshold_pct, abstentions_count_against, vote_standard_text,
                vote_standard_quote, request, source_quote, extraction_run_id
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                meeting_id,
                None if number is None else str(number),
                item.get("title"),
                item.get("type"),
                item.get("category"),
                item.get("effect"),
                item.get("lead_filer"),
                item.get("representative"),
                # The column is not null; a missing list means no co-filers.
                item.get("co_filers") or [],
                item.get("board_recommendation"),
                item.get("recommendation_quote"),
                item.get("vote_base"),
                item.get("vote_threshold_pct"),
                item.get("abstentions_count_against"),
                item.get("vote_standard_text"),
                item.get("vote_standard_quote"),
                item.get("request"),
                item.get("source_quote"),
                run_id,
            ),
        )
    )
    with conn.cursor() as cursor:
        nominees: list[Any] = item.get("nominees") or []
        cursor.executemany(
            "insert into nominees (ballot_item_id, name, position) values (%s, %s, %s)",
            [(item_id, name, position) for position, name in enumerate(nominees, 1)],
        )
        discussion: Mapping[str, Any] = item.get("discussion") or {}
        bodies = (item.get("summary"), discussion.get("proponent"), discussion.get("board"))
        cursor.executemany(
            "insert into item_texts (ballot_item_id, kind, version, body, origin, "
            "extraction_run_id, review_status) values (%s, %s, 1, %s, 'model', %s, 'pending')",
            [(item_id, kind, body, run_id) for kind, body in zip(_TEXT_KINDS, bodies, strict=True)],
        )
