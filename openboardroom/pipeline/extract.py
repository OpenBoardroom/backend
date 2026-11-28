"""Extract stage: turn fetched proxy statements into checked ballot items (spec §6.3, §6.4).

Each filing gets a regex pass, one Claude call, the membership check, and the
four extraction checks. Everything one filing writes (its runs, check results,
meeting, ballot items, nominees, item texts, and stage) commits together in one
transaction, so a failure never leaves partial rows and never affects another
filing. The model call happens before that transaction opens, so no
transaction stays open while waiting on the API.

Direct mode (`extract_direct`) calls the model per filing. Batch mode sends
filings in one message batch (`submit`) and applies the results once it ends
(`collect`); a batch result that is missing, refused, or errored (other than
an invalid request) is retried directly.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, cast

import anthropic
import httpx2
import psycopg

from openboardroom.extract import store
from openboardroom.extract.checks import iso_date, meeting_date_verified, run_checks
from openboardroom.extract.llm import (
    BatchResult,
    LlmSettings,
    Outcome,
    batch_outcomes,
    batch_status,
    run_direct,
    submit_batch,
)
from openboardroom.extract.regex import Candidate, find_candidates
from openboardroom.extract.spec import PROMPT_VERSION, SCHEMA_VERSION

Result = Literal["checked", "in_review", "out_of_scope", "failed"]
# Records a filing's runs and returns the llm run its facts and checks belong to.
Recorder = Callable[[Outcome], int]
# API errors and the raw transport errors the SDK lets through.
_API_ERRORS = (anthropic.APIError, httpx2.HTTPError)
_NUL = "\u0000"


@dataclass(frozen=True)
class ExtractResult:
    """What an extract, submit, or collect run did.

    Attributes:
        checked: Filings advanced to `checked`: all four checks passed.
        in_review: Filings sent to `in_review`: a check failed, the model
            refused, or the output couldn't be written.
        out_of_scope: Filings whose meeting date no index membership covers.
        failed: Filings left at `fetched` to be tried again: the call failed
            or the text file is missing. For `submit`, also filings not
            submitted because the batch was refused.
        submitted: For `submit`, filings submitted in the batch. For `collect`,
            runs still waiting: their batch hasn't ended or its results
            couldn't be read.
        errors: One message per failed or unwritable filing and per skipped
            (capped) filing, each naming its accession, and per unreadable batch.
    """

    checked: int
    in_review: int
    out_of_scope: int
    failed: int
    submitted: int
    errors: list[str] = field(default_factory=lambda: [])


class MalformedOutputError(ValueError):
    """Model output that passed JSON parsing but can't be written as rows."""


class _Tally:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []

    def add(self, result: Result, accession: str, detail: str | None) -> None:
        self.counts[result] += 1
        if detail is not None:
            self.errors.append(f"{accession}: {detail}")

    def skip_capped(self, conn: psycopg.Connection, accession: str | None) -> None:
        for filing in store.capped_filings(conn, accession):
            self.errors.append(
                f"{filing.accession}: skipped: {store.MAX_BILLED_FAILURES} billed failed llm "
                f"runs at prompt version {PROMPT_VERSION}, schema version {SCHEMA_VERSION}; "
                "needs a person"
            )

    def result(self) -> ExtractResult:
        return ExtractResult(
            checked=self.counts["checked"],
            in_review=self.counts["in_review"],
            out_of_scope=self.counts["out_of_scope"],
            failed=self.counts["failed"],
            submitted=self.counts["submitted"],
            errors=self.errors,
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _text_path(text_dir: Path, accession: str) -> Path:
    return text_dir / f"{accession}.txt"


def _read_text(text_dir: Path, accession: str) -> str | None:
    path = _text_path(text_dir, accession)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _missing_text(text_dir: Path, accession: str) -> str:
    return f"text file missing: {_text_path(text_dir, accession)}"


def _api_error(error: Exception) -> str:
    if isinstance(error, anthropic.APIStatusError):
        return f"{type(error).__name__}: status {error.status_code}"
    return f"{type(error).__name__}: {error}"


def _meeting(output: dict[str, Any]) -> dict[str, Any]:
    """The meeting object, or an empty one when the model gave none (the checks flag that)."""
    meeting = output.get("meeting")
    return cast(dict[str, Any], meeting) if isinstance(meeting, dict) else {}


def _items(output: dict[str, Any]) -> list[dict[str, Any]]:
    """The items, checked only as far as writing them needs.

    Raises:
        MalformedOutputError: The shape can't be written as rows.
    """
    items = output.get("items")
    if not isinstance(items, list):
        raise MalformedOutputError("items is not a list")
    checked: list[dict[str, Any]] = []
    for position, item in enumerate(cast(list[object], items), 1):
        if not isinstance(item, dict):
            raise MalformedOutputError(f"item {position} is not an object")
        fields = cast(dict[str, Any], item)
        if not isinstance(fields.get("nominees") or [], list):
            raise MalformedOutputError(f"item {position}: nominees is not a list")
        if not isinstance(fields.get("discussion") or {}, dict):
            raise MalformedOutputError(f"item {position}: discussion is not an object")
        checked.append(fields)
    return checked


def _advance(
    conn: psycopg.Connection,
    filing: store.Filing,
    text: str,
    candidates: list[Candidate],
    outcome: Outcome,
    run_id: int,
) -> Result:
    """Write the facts and move the filing, inside the caller's transaction."""
    if outcome.status == "failed":
        return "failed"
    if outcome.status == "refused" or outcome.output is None:
        store.set_stage(conn, filing.id, "in_review")
        return "in_review"
    output = cast(dict[str, Any], outcome.output)
    meeting = _meeting(output)
    day = iso_date(meeting.get("date"))
    # Only a date its verbatim quote backs may drop the filing unreviewed (ruling
    # R15); any other date goes through the checks, which then fail on it.
    if (
        day is not None
        and meeting_date_verified(output, text)
        and not store.covered_by_index(conn, filing.company_id, day)
    ):
        store.set_stage(conn, filing.id, "out_of_scope")
        return "out_of_scope"
    items = _items(output)
    results = run_checks(output, text, candidates)
    store.insert_check_results(conn, run_id, results)
    meeting_id = store.insert_meeting(
        conn,
        filing,
        kind=meeting.get("kind"),
        meeting_date=day,
        meeting_time=meeting.get("time"),
        format=meeting.get("format"),
        record_date=iso_date(meeting.get("record_date")),
        shares_outstanding=meeting.get("shares_outstanding"),
        date_quote=meeting.get("date_quote"),
    )
    for item in items:
        store.insert_item(conn, meeting_id, run_id, item)
    stage: Result = "checked" if all(r.passed for r in results) else "in_review"
    store.set_stage(conn, filing.id, stage)
    return stage


def _apply(
    conn: psycopg.Connection,
    filing: store.Filing,
    text: str,
    candidates: list[Candidate],
    outcome: Outcome,
    record: Recorder,
) -> tuple[Result, str | None]:
    """Write one filing's outcome in one transaction.

    If the write fails, the transaction rolls back and, in a new one, the
    runs are recorded with the llm run `failed`, keeping the model's output
    and tokens, and the error in its detail (ruling R4), and the filing moves
    to `in_review` with no facts (ruling R10): output that can't be written
    would fail the same way again, so a person looks at it instead of the
    model being billed for it again. If even that write fails, as it does
    when the output holds a NUL character (Postgres stores none in text or
    jsonb), it is retried once with NULs removed from the stored output.

    Returns:
        What happened, and the detail when it failed or couldn't be written.
    """
    try:
        with conn.transaction():
            run_id = record(outcome)
            result = _advance(conn, filing, text, candidates, outcome, run_id)
    except (psycopg.Error, MalformedOutputError) as error:
        detail = _strip_nul(f"could not write the extraction: {type(error).__name__}: {error}")
        failed = replace(outcome, status="failed", detail=detail)
        try:
            with conn.transaction():
                record(failed)
                store.set_stage(conn, filing.id, "in_review")
        except psycopg.Error:
            # One bad response must not stall the run.
            note = "; NUL characters removed from the stored output"
            with conn.transaction():
                record(replace(failed, output=_strip_nul(outcome.output), detail=detail + note))
                store.set_stage(conn, filing.id, "in_review")
        return "in_review", detail
    return result, outcome.detail if result == "failed" else None


def _strip_nul[T](value: T) -> T:
    """The value with NUL characters removed from every string inside it."""
    if isinstance(value, str):
        return cast(T, value.replace(_NUL, ""))
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        return cast(T, {_strip_nul(k): _strip_nul(v) for k, v in items})
    if isinstance(value, list):
        return cast(T, [_strip_nul(v) for v in cast(list[object], value)])
    return value


def _record_new(
    conn: psycopg.Connection,
    settings: LlmSettings,
    filing: store.Filing,
    candidates: list[Candidate],
    started_at: datetime,
) -> Recorder:
    """Record the regex run and a new llm run, as direct mode does."""

    def record(outcome: Outcome) -> int:
        store.insert_regex_run(conn, filing.id, candidates)
        return store.insert_llm_run(conn, filing.id, settings, outcome, started_at)

    return record


def extract_direct(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    limit: int | None = None,
    accession: str | None = None,
) -> ExtractResult:
    """Extract fetched DEF 14A filings one at a time with direct calls.

    Takes `fetched` DEF 14A filings with no `submitted` run, in id order,
    except filings capped for repeated billed failures, which are reported
    in `errors` instead. Each filing's writes commit (or roll back) as one transaction; on a
    connection that isn't autocommit, that is a savepoint and the caller
    commits.

    Args:
        conn: Connection with write access.
        client: Anthropic client.
        settings: Model settings.
        text_dir: Directory of `{accession}.txt` files from the fetch stage.
        limit: At most this many filings with a text file; missing-text
            filings are reported but don't count.
        accession: Only this filing.

    Returns:
        Counts per result, and a message per failed filing.
    """
    tally = _Tally()
    tally.skip_capped(conn, accession)
    done = 0
    # As in `submit`, `limit` counts filings with text, not missing-text failures.
    for filing in store.pending_filings(conn, None, accession):
        if limit is not None and done >= limit:
            break
        text = _read_text(text_dir, filing.accession)
        if text is None:
            tally.add("failed", filing.accession, _missing_text(text_dir, filing.accession))
            continue
        done += 1
        candidates = find_candidates(text)
        started_at = _now()
        outcome = run_direct(client, settings, text, candidates)
        record = _record_new(conn, settings, filing, candidates, started_at)
        result, detail = _apply(conn, filing, text, candidates, outcome, record)
        tally.add(result, filing.accession, detail)
    return tally.result()


def submit(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    limit: int | None = None,
) -> ExtractResult:
    """Send fetched DEF 14A filings to the model as one message batch.

    Takes the same filings as `extract_direct`. Once the batch is accepted,
    each filing's regex run and `submitted` llm run (with the batch id) are
    recorded in one transaction; `collect` finishes them. If the batch is
    refused, nothing is recorded and the filings stay selectable. Capped
    filings are reported in `errors`, as in `extract_direct`.

    Args:
        conn: Connection with write access.
        client: Anthropic client.
        settings: Model settings.
        text_dir: Directory of `{accession}.txt` files from the fetch stage.
        limit: At most this many filings with a text file; missing-text
            filings are reported but don't count.

    Returns:
        How many filings were submitted or failed, and a message per failure.

    Raises:
        psycopg.Error: The runs of an accepted batch couldn't be recorded. The
            batch is canceled first, on a best-effort basis, so it isn't
            billed and then submitted again; a note on the error names it.
    """
    tally = _Tally()
    tally.skip_capped(conn, None)
    ready: list[tuple[store.Filing, list[Candidate]]] = []
    requests: list[tuple[str, str, list[Candidate]]] = []
    # `limit` counts filings with text: unbilled missing-text failures are never
    # capped and sort first, so counting them would starve the healthy ones.
    for filing in store.pending_filings(conn, None):
        if limit is not None and len(ready) >= limit:
            break
        text = _read_text(text_dir, filing.accession)
        if text is None:
            tally.add("failed", filing.accession, _missing_text(text_dir, filing.accession))
            continue
        candidates = find_candidates(text)
        ready.append((filing, candidates))
        requests.append((filing.accession, text, candidates))
    if not requests:
        return tally.result()
    started_at = _now()
    try:
        batch_id = submit_batch(client, settings, requests)
    except _API_ERRORS as error:
        tally.counts["failed"] += len(requests)
        tally.errors.append(f"batch submit failed: {_api_error(error)}")
        return tally.result()
    try:
        with conn.transaction():
            for filing, candidates in ready:
                store.insert_regex_run(conn, filing.id, candidates)
                store.insert_llm_run(conn, filing.id, settings, None, started_at, batch_id)
    except psycopg.Error as error:
        # Unrecorded, the batch would never be collected; its filings would
        # be submitted again and billed twice.
        try:
            client.messages.batches.cancel(batch_id)
            note = f"batch {batch_id} was submitted but not recorded; cancel requested"
        except _API_ERRORS as cancel_error:
            note = (
                f"batch {batch_id} was submitted but not recorded; cancel failed "
                f"({_api_error(cancel_error)}): cancel it by hand"
            )
        error.add_note(note)
        raise
    tally.counts["submitted"] += len(ready)
    return tally.result()


def _collect_run(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    run: store.SubmittedRun,
    batch_result: BatchResult,
) -> tuple[Result, str | None]:
    """Apply one filing's batch result, retrying it directly when it says to."""
    filing = run.filing
    batch_outcome = batch_result.outcome
    text = _read_text(text_dir, filing.accession)
    if text is None:
        detail = _missing_text(text_dir, filing.accession)
        failed = replace(batch_outcome, status="failed", detail=detail)
        with conn.transaction():
            store.finish_llm_run(conn, run.run_id, failed)
        return "failed", detail
    candidates = find_candidates(text)

    if not batch_result.retry:

        def finish(outcome: Outcome) -> int:
            store.finish_llm_run(conn, run.run_id, outcome)
            return run.run_id

        return _apply(conn, filing, text, candidates, batch_outcome, finish)

    # The batch run keeps what the batch returned; the retry is a run of its own.
    batch_final = replace(batch_outcome, detail=f"{batch_outcome.detail}; retried directly")
    started_at = _now()
    outcome = run_direct(client, settings, text, candidates)

    def supersede(direct: Outcome) -> int:
        store.finish_llm_run(conn, run.run_id, batch_final)
        return store.insert_llm_run(conn, filing.id, settings, direct, started_at)

    return _apply(conn, filing, text, candidates, outcome, supersede)


def collect(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
) -> ExtractResult:
    """Apply the results of every ended batch that has `submitted` runs.

    A batch still in progress is left alone. If a batch's status or results
    can't be read, its runs stay `submitted` and nothing changes (ruling
    R9); the next collect tries again. Otherwise each filing is applied in
    its own transaction, as in direct mode. A result that is missing,
    expired, canceled, refused, or errored is retried once directly: the
    batch run is finished with what the batch returned (an error keeps its
    type and message; the detail ends "retried directly") and the direct call
    is recorded as a new llm run, whose result is final. An
    `invalid_request_error` is not retried, since it would fail the same way:
    its run is finished `failed` with the error, and the filing stays
    `fetched`.

    Args:
        conn: Connection with write access.
        client: Anthropic client.
        settings: Model settings, used for direct retries.
        text_dir: Directory of `{accession}.txt` files from the fetch stage.

    Returns:
        Counts per result, runs still waiting, and a message per failed
        filing or unreadable batch.
    """
    tally = _Tally()
    for batch_id, group in groupby(store.submitted_runs(conn), key=lambda r: r.batch_id):
        runs = list(group)
        try:
            if batch_status(client, batch_id) != "ended":
                tally.counts["submitted"] += len(runs)
                continue
            outcomes = batch_outcomes(client, batch_id, [r.filing.accession for r in runs])
        except _API_ERRORS as error:
            tally.counts["submitted"] += len(runs)
            tally.errors.append(f"batch {batch_id}: {_api_error(error)}")
            continue
        for run in runs:
            outcome = outcomes[run.filing.accession]
            result, detail = _collect_run(conn, client, settings, text_dir, run, outcome)
            tally.add(result, run.filing.accession, detail)
    return tally.result()
