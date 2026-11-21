"""Fetch stage: download discovered filings and convert them to plain text (spec §6.2)."""

import argparse
import hashlib
import sys
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
    """

    fetched: int
    failed: int
    errors: list[str] = field(default_factory=lambda: [])


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


def fetch(conn: psycopg.Connection, client: EdgarClient, cache_dir: Path) -> FetchResult:
    """Download each `discovered` filing's primary document and convert it to text.

    Filings are taken in id order. The raw document is cached as
    `cache_dir/raw/{accession}.htm`, written to `{accession}.part` first and
    renamed only once complete; a cached `.htm` whose sha256 matches the
    filing's `raw_sha256` is reused without a request. The text goes to
    `cache_dir/text/{accession}.txt`. Both hashes and stage `fetched` are then
    recorded in one UPDATE, so on an autocommit connection each filing is kept
    even if a later one fails; otherwise the caller commits.

    A filing that can't be downloaded, or has no primary document, stays at
    `discovered` and is counted as failed; the rest are still processed.

    Args:
        conn: Connection with write access.
        client: EDGAR client.
        cache_dir: Root of the filing cache.

    Returns:
        Counts of fetched and failed filings, with a message per failure.
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
    for filing_id, accession, cik, primary_document, raw_sha256 in pending:
        if primary_document is None:
            errors.append(f"{accession}: no primary document recorded")
            continue
        raw_path = raw_dir / f"{accession}.htm"
        raw = _cached(raw_path, raw_sha256)
        if raw is None:
            try:
                raw = client.document(cik, accession, primary_document)
            except EdgarError as exc:
                errors.append(f"{accession}: {exc}")
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


def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    # Autocommit: each filing's single UPDATE commits on its own, so a failure
    # later in the run doesn't undo filings already fetched.
    with (
        EdgarClient(settings) as client,
        psycopg.connect(settings.database_url, autocommit=True) as conn,
    ):
        result = fetch(conn, client, settings.cache_dir)
    for error in result.errors:
        print(f"fetch failed: {error}", file=sys.stderr)
    print(f"fetch: {result.fetched} fetched, {result.failed} failed")
    return 1 if result.failed else 0


def register(subparsers: SubParsers) -> None:
    """Add the `fetch` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser(
        "fetch", help="Download discovered filings and convert them to plain text."
    )
    parser.set_defaults(func=_run)
