"""Tests for the fetch stage (spec §6.2). HTTP is faked with httpx.MockTransport."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from openboardroom.cli import main
from openboardroom.config import Settings
from openboardroom.edgar import EdgarClient, RateLimiter
from openboardroom.pipeline.fetch import FetchResult, fetch
from openboardroom.text import to_text

FILINGS = Path(__file__).resolve().parents[1] / "fixtures" / "filings"
APPLE_HTML = (FILINGS / "aapl-2024-def14a.htm").read_bytes()
ACCESSION = "0001308179-24-000010"
DOCUMENT = "laapl2024_def14a.htm"
OTHER = "0001308179-24-000011"
OTHER_DOCUMENT = "laapl2024_defa14a.htm"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeArchive:
    """Serves documents by URL path and records every requested path."""

    def __init__(self, documents: dict[str, bytes]) -> None:
        """Map Archives paths (``/Archives/edgar/data/...``) to document bodies."""
        self.documents = documents
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer with the document at the request's path, or 404."""
        self.requested.append(request.url.path)
        if request.url.path not in self.documents:
            return httpx.Response(404)
        return httpx.Response(200, content=self.documents[request.url.path])

    def client(self) -> EdgarClient:
        """Return a real EdgarClient that talks to this fake, with no waits."""
        settings = Settings(
            database_url="postgresql://u:p@localhost/db",
            edgar_user_agent="Test Org test@example.org",
            edgar_max_rps=10.0,
            cache_dir=Path("./cache"),
        )
        return EdgarClient(
            settings,
            transport=httpx.MockTransport(self.handler),
            limiter=RateLimiter(10.0, clock=lambda: 0.0, sleep=lambda _: None),
            sleep=lambda _: None,
        )


def path(accession: str, document: str) -> str:
    return f"/Archives/edgar/data/320193/{accession.replace('-', '')}/{document}"


def apple_archive() -> FakeArchive:
    return FakeArchive({path(ACCESSION, DOCUMENT): APPLE_HTML})


def add_filing(
    conn: psycopg.Connection,
    accession: str = ACCESSION,
    document: str | None = DOCUMENT,
    stage: str = "discovered",
) -> None:
    """Insert an Apple filing; its stage_updated_at is set in the past."""
    company = conn.execute(
        "insert into companies (cik, ticker, name) values ('0000320193', 'AAPL', 'Apple Inc.') "
        "on conflict (cik) do update set name = excluded.name returning id"
    ).fetchone()
    assert company is not None
    conn.execute(
        "insert into company_ciks (company_id, cik, valid_from) "
        "values (%s, '0000320193', '1900-01-01') on conflict (cik) do nothing",
        (company[0],),
    )
    conn.execute(
        """
        insert into filings (accession, company_id, cik, form, filed_at, source_url,
                             primary_document, stage, stage_updated_at)
        values (%s, %s, '0000320193', 'DEF 14A', '2024-01-11', %s, %s, %s, '2000-01-01')
        """,
        (
            accession,
            company[0],
            f"https://www.sec.gov{path(accession, document or '')}",
            document,
            stage,
        ),
    )


def filing(conn: psycopg.Connection, accession: str = ACCESSION) -> tuple[Any, ...]:
    row = conn.execute(
        "select stage, raw_sha256, text_sha256, stage_updated_at > '2000-01-01' "
        "from filings where accession = %s",
        (accession,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def run(conn: psycopg.Connection, archive: FakeArchive, cache: Path) -> FetchResult:
    with archive.client() as client:
        return fetch(conn, client, cache)


def test_fetch_advances_stage_and_records_hashes(conn: psycopg.Connection, tmp_path: Path) -> None:
    add_filing(conn)
    archive = apple_archive()

    result = run(conn, archive, tmp_path)

    assert result == FetchResult(fetched=1, failed=0)
    assert archive.requested == [path(ACCESSION, DOCUMENT)]
    raw = tmp_path / "raw" / f"{ACCESSION}.htm"
    text = tmp_path / "text" / f"{ACCESSION}.txt"
    assert raw.read_bytes() == APPLE_HTML
    assert text.read_text(encoding="utf-8") == to_text(APPLE_HTML)
    assert not (tmp_path / "raw" / f"{ACCESSION}.part").exists()
    assert filing(conn) == ("fetched", sha256(APPLE_HTML), sha256(text.read_bytes()), True)


def test_partial_download_is_refetched(conn: psycopg.Connection, tmp_path: Path) -> None:
    add_filing(conn)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{ACCESSION}.part").write_bytes(APPLE_HTML[:1000])
    archive = apple_archive()

    result = run(conn, archive, tmp_path)

    assert result == FetchResult(fetched=1, failed=0)
    assert archive.requested == [path(ACCESSION, DOCUMENT)]
    assert (tmp_path / "raw" / f"{ACCESSION}.htm").read_bytes() == APPLE_HTML
    assert not (tmp_path / "raw" / f"{ACCESSION}.part").exists()
    assert filing(conn)[:2] == ("fetched", sha256(APPLE_HTML))


def test_cached_document_not_refetched(conn: psycopg.Connection, tmp_path: Path) -> None:
    add_filing(conn)
    run(conn, apple_archive(), tmp_path)

    again = apple_archive()
    assert run(conn, again, tmp_path) == FetchResult(fetched=0, failed=0)
    assert again.requested == []

    # A filing sent back to `discovered` reuses its cached document.
    conn.execute("update filings set stage = 'discovered' where accession = %s", (ACCESSION,))
    (tmp_path / "text" / f"{ACCESSION}.txt").unlink()
    reset = apple_archive()

    assert run(conn, reset, tmp_path) == FetchResult(fetched=1, failed=0)
    assert reset.requested == []
    assert filing(conn)[:2] == ("fetched", sha256(APPLE_HTML))
    assert (tmp_path / "text" / f"{ACCESSION}.txt").exists()


def test_cached_document_with_wrong_hash_is_refetched(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    add_filing(conn)
    run(conn, apple_archive(), tmp_path)
    conn.execute("update filings set stage = 'discovered' where accession = %s", (ACCESSION,))
    (tmp_path / "raw" / f"{ACCESSION}.htm").write_bytes(b"<p>tampered</p>")
    archive = apple_archive()

    assert run(conn, archive, tmp_path) == FetchResult(fetched=1, failed=0)
    assert archive.requested == [path(ACCESSION, DOCUMENT)]
    assert (tmp_path / "raw" / f"{ACCESSION}.htm").read_bytes() == APPLE_HTML


def test_download_error_leaves_filing_discovered(conn: psycopg.Connection, tmp_path: Path) -> None:
    add_filing(conn, OTHER, OTHER_DOCUMENT)  # not served: 404
    add_filing(conn)
    archive = apple_archive()

    result = run(conn, archive, tmp_path)

    assert (result.fetched, result.failed) == (1, 1)
    assert len(result.errors) == 1
    assert OTHER in result.errors[0]
    assert "HTTP 404" in result.errors[0]
    assert filing(conn, OTHER) == ("discovered", None, None, False)
    assert not (tmp_path / "raw" / f"{OTHER}.htm").exists()
    assert filing(conn)[0] == "fetched"


def test_filing_without_primary_document_fails(conn: psycopg.Connection, tmp_path: Path) -> None:
    add_filing(conn, document=None)
    archive = apple_archive()

    result = run(conn, archive, tmp_path)

    assert (result.fetched, result.failed) == (0, 1)
    assert "no primary document" in result.errors[0]
    assert archive.requested == []
    assert filing(conn)[0] == "discovered"


def test_only_discovered_filings_are_fetched_in_id_order(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    add_filing(conn, OTHER, OTHER_DOCUMENT)
    add_filing(conn)
    add_filing(conn, "0001308179-24-000012", "later.htm", stage="extracted")
    archive = FakeArchive(
        {path(ACCESSION, DOCUMENT): APPLE_HTML, path(OTHER, OTHER_DOCUMENT): b"<p>Supplement</p>"}
    )

    result = run(conn, archive, tmp_path)

    assert result == FetchResult(fetched=2, failed=0)
    assert archive.requested == [path(OTHER, OTHER_DOCUMENT), path(ACCESSION, DOCUMENT)]
    assert (tmp_path / "text" / f"{OTHER}.txt").read_text(encoding="utf-8") == "Supplement\n"


CliEnv = Callable[..., list[tuple[bool, Path]]]


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, role_urls: dict[str, str], tmp_path: Path) -> CliEnv:
    """Point `obr` at the test database and replace `fetch` with one returning ``result``.

    The returned list records, per call, whether the connection was in
    autocommit mode and the cache directory passed.
    """

    def setup(result: FetchResult) -> list[tuple[bool, Path]]:
        calls: list[tuple[bool, Path]] = []

        def fake(conn: psycopg.Connection, client: EdgarClient, cache_dir: Path) -> FetchResult:
            calls.append((conn.autocommit, cache_dir))
            return result

        monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
        monkeypatch.setenv("OBR_EDGAR_USER_AGENT", "Test Org test@example.org")
        monkeypatch.setenv("OBR_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr("openboardroom.pipeline.fetch.fetch", fake)
        return calls

    return setup


def test_cli_prints_counts(
    cli_env: CliEnv, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = cli_env(FetchResult(fetched=3, failed=0))

    code = main(["fetch"])

    assert code == 0
    # Autocommit keeps each fetched filing even if a later one fails.
    assert calls == [(True, tmp_path)]
    assert "fetch: 3 fetched, 0 failed" in capsys.readouterr().out


def test_cli_exits_nonzero_when_a_filing_failed(
    cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env(FetchResult(fetched=2, failed=1, errors=[f"{OTHER}: EDGAR returned HTTP 404"]))

    code = main(["fetch"])

    captured = capsys.readouterr()
    assert code != 0
    assert "fetch: 2 fetched, 1 failed" in captured.out
    assert f"{OTHER}: EDGAR returned HTTP 404" in captured.err
