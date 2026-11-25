"""Supplements stage: read each fetched DEFA14A for ballot changes (spec §6.3 item 3, §7.1).

Withdrawals and recommendation changes apply at once when their quote
verifies; other changes wait for a reviewer as pending changes. Direct mode
calls the model one supplement at a time; ``submit`` sends them as one message
batch and ``collect`` applies ended batches, retrying a failed or refused
result once directly, as the extract stage does.
"""

import argparse
import sys
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

from openboardroom.companies import SubParsers
from openboardroom.config import (
    ConfigError,
    cache_dir_from_env,
    database_url_from_env,
    extract_settings_from_env,
)
from openboardroom.extract import store
from openboardroom.extract import supplement as supp
from openboardroom.extract.checks import CheckResult
from openboardroom.extract.llm import (
    BatchResult,
    LlmSettings,
    Outcome,
    batch_outcomes,
    batch_status,
    run_supplement,
    submit_requests,
    supplement_request_params,
)
from openboardroom.extract.spec import SUPPLEMENT_PROMPT_VERSION
from openboardroom.extract.supplement import Supplement

Mode = Literal["direct", "submit", "collect"]
_API_ERRORS = (anthropic.APIError, httpx2.HTTPError)
_KIND = "supplement"
# As for proxies: two billed failures at the current version need a person.
_MAX_BILLED_FAILURES = 2


@dataclass(frozen=True)
class SupplementResult:
    """What one supplements run did.

    Attributes:
        checked: Supplements whose changes were written (or that changed nothing).
        in_review: Supplements sent to review: a change failed its checks, or
            the model refused.
        out_of_scope: Supplements no meeting links to.
        failed: Supplements left ``fetched``: the call failed or the text is missing.
        submitted: Supplements sent in a batch (``submit``).
        waiting: Supplements whose batch hasn't ended or couldn't be read (``collect``).
        applied: Automatic changes recorded as applied.
        pending: Changes waiting for a reviewer.
        errors: One message per failure, capped supplement, or unreadable batch.
    """

    checked: int = 0
    in_review: int = 0
    out_of_scope: int = 0
    failed: int = 0
    submitted: int = 0
    waiting: int = 0
    applied: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=lambda: [])


class _Tally:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.errors: list[str] = []

    def add(self, result: str, accession: str, detail: str | None = None) -> None:
        self.counts[result] += 1
        if detail is not None:
            self.errors.append(f"{accession}: {detail}")

    def result(self) -> SupplementResult:
        names = ("checked", "in_review", "out_of_scope", "failed", "submitted", "waiting",
                 "applied", "pending")  # fmt: skip
        return SupplementResult(**{n: self.counts[n] for n in names}, errors=self.errors)


def _now() -> datetime:
    return datetime.now(UTC)


def _text(text_dir: Path, accession: str) -> str | None:
    path = text_dir / f"{accession}.txt"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _capped(conn: psycopg.Connection, filing_id: int) -> bool:
    row = conn.execute(
        "select count(*) from extraction_runs where filing_id = %s and kind = %s "
        "and status = 'failed' and input_tokens is not null and prompt_version = %s",
        (filing_id, _KIND, SUPPLEMENT_PROMPT_VERSION),
    ).fetchone()
    return bool(row and row[0] >= _MAX_BILLED_FAILURES)


def _record(
    conn: psycopg.Connection,
    filing_id: int,
    settings: LlmSettings,
    outcome: Outcome | None,
    started: datetime,
    batch_id: str | None = None,
) -> int:
    return store.insert_llm_run(
        conn, filing_id, settings, outcome, started, batch_id, kind=_KIND,
        prompt_version=SUPPLEMENT_PROMPT_VERSION, schema_version=SUPPLEMENT_PROMPT_VERSION,
    )  # fmt: skip


def _checks(found: list[str]) -> list[CheckResult]:
    """Sort a supplement's problems into the schema, quotes, and consistency checks."""
    groups: dict[str, list[str]] = {"schema": [], "quotes": [], "consistency": []}
    for problem in found:
        name = (
            "schema"
            if not problem.startswith("change ")
            else "quotes"
            if "source_quote" in problem
            else "consistency"
        )
        groups[name].append(problem)
    return [
        CheckResult(cast(Any, name), not items, "; ".join(items) or None)
        for name, items in groups.items()
    ]


def _apply(
    conn: psycopg.Connection,
    supplement: Supplement,
    text: str,
    outcome: Outcome,
    finish: Callable[[Outcome], int],
    tally: _Tally,
) -> None:
    """Record one supplement's outcome and act on it, in one transaction."""
    assert supplement.meeting_id is not None
    with conn.transaction():
        run_id = finish(outcome)
        if outcome.status == "failed":
            tally.add("failed", supplement.accession, outcome.detail)
            return
        if outcome.status == "refused":
            store.set_stage(conn, supplement.filing_id, "in_review")
            tally.add("in_review", supplement.accession)
            return
        items = supp.meeting_items(conn, supplement.meeting_id)
        found = supp.problems(outcome.output, text, items)
        if found:
            store.insert_check_results(conn, run_id, _checks(found))
            store.set_stage(conn, supplement.filing_id, "in_review")
            tally.add("in_review", supplement.accession)
            return
        changes = cast(dict[str, Any], outcome.output)["changes"]
        counts = supp.write_changes(conn, supplement, run_id, changes, _now())
        store.set_stage(conn, supplement.filing_id, "checked")
        tally.counts["applied"] += counts.applied
        tally.counts["pending"] += counts.pending
        tally.add("checked", supplement.accession)


def _ready(
    conn: psycopg.Connection,
    text_dir: Path,
    tally: _Tally,
    limit: int | None,
    accession: str | None,
) -> list[tuple[Supplement, str]]:
    """Linked supplements with text, after setting unlinked ones out of scope."""
    ready: list[tuple[Supplement, str]] = []
    for supplement in supp.pending_supplements(conn, None, accession):
        if limit is not None and len(ready) >= limit:
            break
        if _capped(conn, supplement.filing_id):
            tally.errors.append(
                f"{supplement.accession}: skipped: {_MAX_BILLED_FAILURES} billed failed "
                f"supplement runs at prompt version {SUPPLEMENT_PROMPT_VERSION}; needs a person"
            )
            continue
        if supplement.meeting_id is None:
            with conn.transaction():
                store.set_stage(conn, supplement.filing_id, "out_of_scope")
            tally.add("out_of_scope", supplement.accession)
            continue
        text = _text(text_dir, supplement.accession)
        if text is None:
            tally.add("failed", supplement.accession, "text file missing")
            continue
        ready.append((supplement, text))
    return ready


def _direct(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    supplement: Supplement,
    text: str,
    tally: _Tally,
) -> None:
    assert supplement.meeting_id is not None
    items = supp.meeting_items(conn, supplement.meeting_id)
    started = _now()
    outcome = run_supplement(client, settings, text, items)

    def record(result: Outcome) -> int:
        return _record(conn, supplement.filing_id, settings, result, started)

    _apply(conn, supplement, text, outcome, record, tally)


def _submit(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    ready: list[tuple[Supplement, str]],
    tally: _Tally,
) -> None:
    if not ready:
        return
    requests = [
        (
            s.accession,
            supplement_request_params(
                settings, text, supp.meeting_items(conn, cast(int, s.meeting_id))
            ),
        )
        for s, text in ready
    ]
    started = _now()
    try:
        batch_id = submit_requests(client, requests)
    except _API_ERRORS as error:
        tally.counts["failed"] += len(requests)
        tally.errors.append(f"batch submit failed: {type(error).__name__}")
        return
    with conn.transaction():
        for supplement, _ in ready:
            _record(conn, supplement.filing_id, settings, None, started, batch_id)
    tally.counts["submitted"] += len(ready)


def _collect_one(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    run: store.SubmittedRun,
    result: BatchResult,
    tally: _Tally,
) -> None:
    accession = run.filing.accession
    supplement = supp.linked(conn, accession)
    text = _text(text_dir, accession)
    if supplement is None or supplement.meeting_id is None or text is None:
        why = "text file missing" if text is None else "no longer linked to a meeting"
        with conn.transaction():
            store.finish_llm_run(conn, run.run_id, replace(result.outcome, status="failed",
                                                           detail=why))  # fmt: skip
        tally.add("failed", accession, why)
        return

    def finish(outcome: Outcome) -> int:
        store.finish_llm_run(conn, run.run_id, outcome)
        return run.run_id

    if not result.retry:
        _apply(conn, supplement, text, result.outcome, finish, tally)
        return
    batch_final = replace(result.outcome, detail=f"{result.outcome.detail}; retried directly")
    items = supp.meeting_items(conn, supplement.meeting_id)
    started = _now()
    outcome = run_supplement(client, settings, text, items)

    def supersede(direct: Outcome) -> int:
        store.finish_llm_run(conn, run.run_id, batch_final)
        return _record(conn, supplement.filing_id, settings, direct, started)

    _apply(conn, supplement, text, outcome, supersede, tally)


def _collect(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    tally: _Tally,
) -> None:
    for batch_id, group in groupby(store.submitted_runs(conn, _KIND), key=lambda r: r.batch_id):
        runs = list(group)
        try:
            if batch_status(client, batch_id) != "ended":
                tally.counts["waiting"] += len(runs)
                continue
            results = batch_outcomes(client, batch_id, [r.filing.accession for r in runs])
        except _API_ERRORS as error:
            tally.counts["waiting"] += len(runs)
            tally.errors.append(f"batch {batch_id}: {type(error).__name__}")
            continue
        for run in runs:
            _collect_one(
                conn, client, settings, text_dir, run, results[run.filing.accession], tally
            )


def run(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    *,
    mode: Mode,
    limit: int | None = None,
    accession: str | None = None,
) -> SupplementResult:
    """Process fetched supplements.

    Args:
        conn: Connection with write access, in autocommit mode outside tests.
        client: Anthropic client.
        settings: Model settings (``OBR_EXTRACT_MODEL`` and ``OBR_EXTRACT_EFFORT``).
        text_dir: Directory of ``{accession}.txt`` files from the fetch stage.
        mode: ``direct`` calls now; ``submit`` sends a batch; ``collect``
            applies ended batches.
        limit: At most this many supplements with text (direct and submit).
        accession: Only this supplement (direct).

    Returns:
        Counts and failure messages.
    """
    tally = _Tally()
    if mode == "collect":
        _collect(conn, client, settings, text_dir, tally)
        return tally.result()
    ready = _ready(conn, text_dir, tally, limit, accession)
    if mode == "submit":
        _submit(conn, client, settings, ready, tally)
    else:
        for supplement, text in ready:
            _direct(conn, client, settings, supplement, text, tally)
    return tally.result()


def _run(args: argparse.Namespace) -> int:
    if args.collect and (args.limit is not None or args.filing is not None):
        args.parser.error("--collect takes no --limit or --filing")
    if args.submit and args.filing is not None:
        args.parser.error("--filing works only in direct mode, not with --submit")
    mode: Mode = "collect" if args.collect else "submit" if args.submit else "direct"
    try:
        settings = extract_settings_from_env()
        text_dir = cache_dir_from_env() / "text"
        # Autocommit: each supplement's writes commit on their own.
        with psycopg.connect(database_url_from_env(), autocommit=True) as conn:
            # The SDK reads ANTHROPIC_API_KEY itself; this code never touches it.
            result = run(
                conn, anthropic.Anthropic(), settings, text_dir, mode=mode,
                limit=args.limit, accession=args.filing,
            )  # fmt: skip
    except ConfigError as error:
        print(f"supplements: {error}", file=sys.stderr)
        return 1
    for error in result.errors:
        print(f"supplements failed: {error}", file=sys.stderr)
    print(
        f"supplements: {result.checked} checked, {result.in_review} in review, "
        f"{result.out_of_scope} out of scope, {result.failed} failed, "
        f"{result.submitted} submitted, {result.waiting} waiting; "
        f"{result.applied} changes applied, {result.pending} pending"
    )
    return 1 if result.failed or result.errors else 0


def _positive(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def register(subparsers: SubParsers) -> None:
    """Add the `supplements` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser(
        "supplements",
        help="Read fetched proxy supplements (DEFA14A) for ballot changes with Claude.",
        description=(
            "With no mode flag, read supplements now with direct calls. "
            "--submit sends them as one message batch; --collect applies ended batches."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--submit", action="store_true", help="send supplements as one batch")
    mode.add_argument("--collect", action="store_true", help="apply the results of ended batches")
    parser.add_argument("--limit", type=_positive, help="at most this many supplements")
    parser.add_argument("--filing", metavar="ACCESSION", help="only this supplement (direct mode)")
    parser.set_defaults(func=_run, parser=parser)
