"""Fetch stage: download discovered filings and convert them to plain text (spec §6.2)."""

import argparse
import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from openboardroom.companies import SubParsers
from openboardroom.config import Settings
from openboardroom.edgar import EdgarClient, EdgarError
from openboardroom.text import to_text


@dataclass(frozen=True)
class FetchResult:
    """What a fetch run did.

    Attributes:
        fetched: Filings advanced to stage `fetched`.
        failed: Filings left at `discovered` because they couldn't be downloaded.
        errors: One message per failed filing, naming its accession.
        stopped: If the run stopped early because EDGAR refused or couldn't be
            reached, why (naming the filing it stopped at); otherwise None.
    """

    fetched: int
    failed: int
    errors: list[str] = field(default_factory=lambda: [])
    stopped: str | None = None


@dataclass(frozen=True)
class ReconvertResult:
    """What a reconvert run did.

    Attributes:
        changed: Filings whose text, and so `text_sha256`, changed.
        unchanged: Filings whose text was already current.
        failed: Accessions whose cached raw document was missing or didn't match
            `raw_sha256`; their rows and text files were left alone.
    """

    changed: int
    unchanged: int
    failed: list[str] = field(default_factory=lambda: [])


def _stops_run(error: EdgarError) -> bool:
    """Whether `error` means EDGAR as a whole is unavailable to us, not just one filing.

    403 is how SEC blocks a client and 429 is its rate limit; 5xx and network
    errors have already been retried. Sending the remaining filings' requests
    would only prolong a block.
    """
    return error.status is None or error.status in (403, 429) or error.status >= 500


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    """Write `data` to a `.part` file beside `path`, then rename it into place.

    A run killed mid-write leaves only the `.part` file, which is never read.
    """
    part = path.with_suffix(".part")
    part.write_bytes(data)
    part.replace(path)


def _cached(path: Path, sha256: str | None) -> bytes | None:
    """Return the cached document if its hash matches the recorded one."""
    if sha256 is None or not path.is_file():
        return None
    data = path.read_bytes()
    return data if _sha256(data) == sha256 else None


def fetch(
    conn: psycopg.Connection,
    client: EdgarClient,
    cache_dir: Path,
    on_error: Callable[[str], None] | None = None,
) -> FetchResult:
    """Download each `discovered` filing's primary document and convert it to text.

    Filings are taken in id order. The raw document is cached as
    `cache_dir/raw/{accession}.htm`, written to `{accession}.part` first and
    renamed only once complete; a cached `.htm` whose sha256 matches the
    filing's `raw_sha256` is reused without a request. The text goes to
    `cache_dir/text/{accession}.txt`. Both hashes and stage `fetched` are then
    recorded in one UPDATE, so on an autocommit connection each filing is kept
    even if a later one fails; otherwise the caller commits.

    A filing with no primary document, or whose download fails for its own
    reasons (e.g. HTTP 404), stays at `discovered` and is counted as failed;
    the rest are still processed. If EDGAR refuses us (HTTP 403 or 429) or
    can't be reached (5xx or a network error, after retries), the run stops
    at once, leaving that filing and every later one at `discovered`.

    Args:
        conn: Connection with write access.
        client: EDGAR client.
        cache_dir: Root of the filing cache.
        on_error: Called with each per-filing failure message as it happens.

    Returns:
        Counts of fetched and failed filings, a message per failure, and why
        the run stopped early, if it did.
    """
    raw_dir = cache_dir / "raw"
    text_dir = cache_dir / "text"
    raw_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    pending = conn.execute(
        "select id, accession, cik, primary_document, raw_sha256 from filings "
        "where stage = 'discovered' order by id"
    ).fetchall()
    fetched = 0
    errors: list[str] = []

    def fail(message: str) -> None:
        errors.append(message)
        if on_error is not None:
            on_error(message)

    for filing_id, accession, cik, primary_document, raw_sha256 in pending:
        # An empty name would make the URL the filing's directory index.
        if primary_document is None or not primary_document.strip():
            fail(f"{accession}: no primary document recorded")
            continue
        raw_path = raw_dir / f"{accession}.htm"
        raw = _cached(raw_path, raw_sha256)
        if raw is None:
            try:
                raw = client.document(cik, accession, primary_document)
            except EdgarError as exc:
                if _stops_run(exc):
                    return FetchResult(
                        fetched=fetched,
                        failed=len(errors),
                        errors=errors,
                        stopped=f"{accession}: {exc}",
                    )
                fail(f"{accession}: {exc}")
                continue
            _write(raw_path, raw)
        text = to_text(raw).encode("utf-8")
        _write(text_dir / f"{accession}.txt", text)
        conn.execute(
            """
            update filings
            set raw_sha256 = %s, text_sha256 = %s, stage = 'fetched', stage_updated_at = now()
            where id = %s
            """,
            (_sha256(raw), _sha256(text), filing_id),
        )
        fetched += 1
    return FetchResult(fetched=fetched, failed=len(errors), errors=errors)


def reconvert(conn: psycopg.Connection, cache_dir: Path) -> ReconvertResult:
    """Rebuild every downloaded filing's text from its cached raw document.

    Run after a change to `to_text`. Never downloads: a filing whose cached
    `.htm` is missing or doesn't match `raw_sha256` is reported as failed and
    left as it was. Stages are not changed.

    Args:
        conn: Connection to the pipeline database; each filing's update commits
            on its own when the connection is in autocommit mode.
        cache_dir: Root of the filing cache.

    Returns:
        Counts of changed and unchanged filings, and the failed accessions.
    """
    rows = conn.execute(
        "select id, accession, raw_sha256, text_sha256 from filings "
        "where raw_sha256 is not null order by id"
    ).fetchall()
    text_dir = cache_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    changed = unchanged = 0
    failed: list[str] = []
    for filing_id, accession, raw_sha256, text_sha256 in rows:
        raw = _cached(cache_dir / "raw" / f"{accession}.htm", raw_sha256)
        if raw is None:
            failed.append(accession)
            continue
        text = to_text(raw).encode("utf-8")
        text_path = text_dir / f"{accession}.txt"
        if _sha256(text) == text_sha256 and text_path.is_file():
            unchanged += 1
            continue
        _write(text_path, text)
        conn.execute(
            "update filings set text_sha256 = %s where id = %s", (_sha256(text), filing_id)
        )
        changed += 1
    return ReconvertResult(changed=changed, unchanged=unchanged, failed=failed)


def _run_reconvert(settings: Settings) -> int:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        result = reconvert(conn, settings.cache_dir)
    for accession in result.failed:
        print(
            f"fetch --reconvert failed: {accession}: cached raw document missing "
            "or doesn't match its recorded hash",
            file=sys.stderr,
        )
    print(
        f"fetch --reconvert: {result.changed} changed, {result.unchanged} unchanged, "
        f"{len(result.failed)} failed"
    )
    return 1 if result.failed else 0


def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.reconvert:
        return _run_reconvert(settings)
    # Autocommit: each filing's single UPDATE commits on its own, so a failure
    # later in the run doesn't undo filings already fetched.
    with (
        EdgarClient(settings) as client,
        psycopg.connect(settings.database_url, autocommit=True) as conn,
    ):
        result = fetch(
            conn,
            client,
            settings.cache_dir,
            on_error=lambda error: print(f"fetch failed: {error}", file=sys.stderr),
        )
    if result.stopped is not None:
        print(
            f"fetch stopped early: EDGAR refused the request or couldn't be reached "
            f"({result.stopped}). Remaining filings stay discovered; run fetch again later.",
            file=sys.stderr,
        )
    print(f"fetch: {result.fetched} fetched, {result.failed} failed")
    return 1 if result.failed or result.stopped is not None else 0


def register(subparsers: SubParsers) -> None:
    """Add the `fetch` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser(
        "fetch", help="Download discovered filings and convert them to plain text."
    )
    parser.add_argument(
        "--reconvert",
        action="store_true",
        help="Rebuild every cached filing's text from its raw document (no downloads).",
    )
    parser.set_defaults(func=_run)
