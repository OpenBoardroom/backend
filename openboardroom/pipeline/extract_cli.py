"""The `obr extract` command: direct, batch-submit, and batch-collect modes."""

import argparse
import sys
from pathlib import Path

import anthropic
import psycopg

from openboardroom.companies import SubParsers
from openboardroom.config import (
    ConfigError,
    cache_dir_from_env,
    database_url_from_env,
    extract_settings_from_env,
)
from openboardroom.extract.llm import LlmSettings
from openboardroom.pipeline.extract import ExtractResult, collect, extract_direct, submit

# The most filings sent in one message batch. `--submit` loops, so a larger
# backlog goes out as several batches of at most this many.
BATCH_SIZE = 100


def _positive(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _submit_batches(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    limit: int | None,
) -> ExtractResult:
    """Submit pending filings in batches of at most `BATCH_SIZE`, `limit` in all.

    Stops when a batch submits nothing (no filings left, or the batch was
    refused), so a filing that fails every time can't loop forever. Failures
    reappear in each round, so their errors are listed once and `failed` is
    the largest count any round saw.
    """
    total = ExtractResult(0, 0, 0, 0, 0)
    remaining = limit
    while remaining is None or remaining > 0:
        size = BATCH_SIZE if remaining is None else min(BATCH_SIZE, remaining)
        result = submit(conn, client, settings, text_dir, limit=size)
        total = ExtractResult(
            checked=total.checked + result.checked,
            in_review=total.in_review + result.in_review,
            out_of_scope=total.out_of_scope + result.out_of_scope,
            failed=max(total.failed, result.failed),
            submitted=total.submitted + result.submitted,
            errors=total.errors + [e for e in result.errors if e not in total.errors],
        )
        if result.submitted == 0:
            break
        if remaining is not None:
            remaining -= result.submitted
    return total


def _stage(args: argparse.Namespace) -> ExtractResult:
    settings = extract_settings_from_env()
    database_url = database_url_from_env()
    text_dir = cache_dir_from_env() / "text"
    # Autocommit: each filing's writes commit on their own, so a crash later in
    # the run can't discard results that were already paid for.
    with psycopg.connect(database_url, autocommit=True) as conn:
        # The SDK reads ANTHROPIC_API_KEY itself; this code never touches it.
        client = anthropic.Anthropic()
        if args.collect:
            return collect(conn, client, settings, text_dir)
        if args.submit:
            return _submit_batches(conn, client, settings, text_dir, args.limit)
        return extract_direct(
            conn, client, settings, text_dir, limit=args.limit, accession=args.filing
        )


def _run(args: argparse.Namespace) -> int:
    if args.collect and (args.limit is not None or args.filing is not None):
        args.parser.error("--collect takes no --limit or --filing")
    if args.submit and args.filing is not None:
        args.parser.error("--filing works only in direct mode, not with --submit")
    try:
        result = _stage(args)
    except ConfigError as exc:
        print(f"extract: {exc}", file=sys.stderr)
        return 1
    except TypeError as exc:
        # The SDK raises TypeError when it has no credentials at the first request.
        if "authentication method" not in str(exc):
            raise
        print(
            "extract: no Anthropic credentials; set ANTHROPIC_API_KEY for this command",
            file=sys.stderr,
        )
        return 1
    for error in result.errors:
        print(f"extract failed: {error}", file=sys.stderr)
    print(
        f"extract: {result.checked} checked, {result.in_review} in review, "
        f"{result.out_of_scope} out of scope, {result.failed} failed, "
        f"{result.submitted} submitted"
    )
    return 1 if result.failed or result.errors else 0


def register(subparsers: SubParsers) -> None:
    """Add the `extract` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser(
        "extract",
        help="Extract ballot items from fetched proxy statements with Claude.",
        description=(
            "With no mode flag, extract filings now with direct calls. "
            "--submit sends them as message batches; --collect applies finished batches later."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--submit", action="store_true", help=f"send filings in batches of up to {BATCH_SIZE}"
    )
    mode.add_argument("--collect", action="store_true", help="apply the results of ended batches")
    parser.add_argument("--limit", type=_positive, help="at most this many filings")
    parser.add_argument("--filing", metavar="ACCESSION", help="only this filing (direct mode)")
    parser.set_defaults(func=_run, parser=parser)
