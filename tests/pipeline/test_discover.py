"""Tests for EDGAR filing discovery (spec §6.1). HTTP is faked with httpx.MockTransport.

The submissions fixtures are trimmed copies of real EDGAR JSON (downloaded
2026-11-21). ``submissions_page.json`` is synthetic: same shape as a real
``filings.files`` page, with made-up accessions.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from openboardroom.cli import main
from openboardroom.companies import sync_companies
from openboardroom.config import Settings
from openboardroom.edgar import EdgarClient, EdgarError, RateLimiter
from openboardroom.pipeline.discover import DiscoverResult, SuccessorNotice, discover

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EDGAR = FIXTURES / "edgar"
XOM = FIXTURES / "companies_xom.csv"
XOM_CIKS = FIXTURES / "company_ciks_xom.csv"
HEADER = "cik,ticker,name,index,start_date,end_date,dates_approximate\n"
APPLE_PAGE = "CIK0000320193-submissions-001.json"

SEASON = (date(2026, 1, 1), date(2026, 12, 31))


def load(name: str) -> dict[str, Any]:
    return json.loads((EDGAR / name).read_text())


class FakeEdgar:
    """Serves submissions JSON by file name and records every requested name."""

    def __init__(self, files: dict[str, dict[str, Any]]) -> None:
        """Map EDGAR file names (``CIK0000320193.json``) to the JSON to return."""
        self.files = files
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer a request with the named file's JSON, or 404."""
        name = request.url.path.rsplit("/", 1)[-1]
        self.requested.append(name)
        if name not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, json=self.files[name])

    def client(self) -> EdgarClient:
        """Return a real EdgarClient that talks to this fake, with no rate-limit waits."""
        settings = Settings(
            database_url="postgresql://u:p@localhost/db",
            edgar_user_agent="Test Org test@example.org",
            edgar_max_rps=10.0,
            cache_dir=Path("./cache"),
        )
        limiter = RateLimiter(10.0, clock=lambda: 0.0, sleep=lambda _: None)
        return EdgarClient(
            settings,
            transport=httpx.MockTransport(self.handler),
            limiter=limiter,
            sleep=lambda _: None,
        )


def apple_edgar() -> FakeEdgar:
    return FakeEdgar(
        {
            "CIK0000320193.json": load("submissions_aapl.json"),
            APPLE_PAGE: load("submissions_page.json"),
        }
    )


def xom_edgar() -> FakeEdgar:
    return FakeEdgar(
        {
            "CIK0000034088.json": load("submissions_xom_0000034088.json"),
            "CIK0002115436.json": load("submissions_xom_0002115436.json"),
        }
    )


def apple_only(tmp_path: Path, *memberships: str) -> Path:
    """Write a companies CSV with one AAPL row per ``start,end`` membership."""
    path = tmp_path / "companies.csv"
    rows = "".join(f"0000320193,AAPL,Apple Inc.,sp100,{m},false\n" for m in memberships)
    path.write_text(HEADER + rows)
    return path


def stored(conn: psycopg.Connection) -> list[tuple[str, str, str, str]]:
    rows = conn.execute(
        "select filed_at::date::text, form, accession, cik from filings "
        "order by filed_at, accession"
    ).fetchall()
    return [tuple(r) for r in rows]


def run(conn: psycopg.Connection, edgar: FakeEdgar, start: date, end: date) -> DiscoverResult:
    with edgar.client() as client:
        return discover(conn, client, start, end)


def test_discovers_proxy_supplement_and_results(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    edgar = FakeEdgar({"CIK0000320193.json": load("submissions_aapl.json")})

    result = run(conn, edgar, date(2026, 1, 1), date(2026, 12, 31))

    assert result.new == 3
    assert result.existing == 0
    assert stored(conn) == [
        ("2026-01-08", "DEF 14A", "0001308179-26-000008", "0000320193"),
        ("2026-01-08", "DEFA14A", "0001308179-26-000009", "0000320193"),
        ("2026-02-24", "8-K", "0001140361-26-006577", "0000320193"),
    ]
    row = conn.execute(
        "select stage, source_url, primary_document from filings "
        "where accession = '0001308179-26-000008'"
    ).fetchone()
    assert row == (
        "discovered",
        "https://www.sec.gov/Archives/edgar/data/320193/000130817926000008/aapl014016-def14a.htm",
        "aapl014016-def14a.htm",
    )
    # filings.recent reaches back past 2026, so no paged file is read.
    assert APPLE_PAGE not in edgar.requested


def test_8k_without_507_ignored(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    run(conn, apple_edgar(), *SEASON)

    accessions = {r[2] for r in stored(conn)}
    # 2026-01-29 earnings (2.02,9.01) and 2026-04-20 (5.02) are 8-Ks without 5.07.
    assert "0000320193-26-000005" not in accessions
    assert "0001140361-26-015711" not in accessions


def test_rerun_is_noop(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    run(conn, apple_edgar(), *SEASON)

    again = run(conn, apple_edgar(), *SEASON)

    assert again == DiscoverResult(new=0, existing=3)
    assert conn.execute("select count(*) from filings").fetchone() == (3,)


def test_company_with_no_filings_in_window(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))

    result = run(conn, apple_edgar(), date(2026, 3, 1), date(2026, 3, 31))

    assert result.new == 0
    assert stored(conn) == []


def test_reads_paged_file_for_older_window(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2010-01-01,"))
    edgar = apple_edgar()

    result = run(conn, edgar, date(2015, 1, 1), date(2015, 12, 31))

    assert APPLE_PAGE in edgar.requested
    assert result.new == 2
    assert [(r[0], r[1]) for r in stored(conn)] == [
        ("2015-01-28", "DEF 14A"),
        ("2015-03-10", "8-K"),
    ]


def test_membership_limits_window(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Apple joins on 2026-02-01, so its 2026-01-08 proxy is outside its membership."""
    sync_companies(conn, apple_only(tmp_path, "2026-02-01,"))

    result = run(conn, apple_edgar(), *SEASON)

    assert result.new == 1
    assert [r[2] for r in stored(conn)] == ["0001140361-26-006577"]


def synthetic_submissions(*rows: tuple[str, str, str, str]) -> dict[str, Any]:
    """Build submissions JSON from (form, filingDate, accession, items) rows."""
    return {
        "cik": "320193",
        "filings": {
            "recent": {
                "form": [r[0] for r in rows],
                "filingDate": [r[1] for r in rows],
                "accessionNumber": [r[2] for r in rows],
                "items": [r[3] for r in rows],
                "primaryDocument": ["doc.htm" for _ in rows],
            },
            "files": [],
        },
    }


def test_membership_gap_excludes_filings(conn: psycopg.Connection, tmp_path: Path) -> None:
    """A filing between two memberships is excluded, even inside their merged span."""
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,2026-03-31", "2026-06-01,"))
    edgar = FakeEdgar(
        {
            "CIK0000320193.json": synthetic_submissions(
                ("DEFA14A", "2026-06-15", "0009999999-26-000002", ""),
                ("DEFA14A", "2026-04-15", "0009999999-26-000001", ""),
            )
        }
    )

    result = run(conn, edgar, *SEASON)

    assert result.new == 1
    assert [r[2] for r in stored(conn)] == ["0009999999-26-000002"]


def test_xom_filings_come_from_both_ciks(conn: psycopg.Connection) -> None:
    """2026 proxy and results come from the old CIK; later supplements from the new one."""
    sync_companies(conn, XOM, XOM_CIKS)

    result = run(conn, xom_edgar(), *SEASON)

    rows = stored(conn)
    old = [r for r in rows if r[3] == "0000034088"]
    new = [r for r in rows if r[3] == "0002115436"]
    assert [r[1] for r in old].count("DEF 14A") == 1
    assert [r[1] for r in old].count("DEFA14A") == 10
    assert [(r[0], r[1], r[2]) for r in old if r[1] == "8-K"] == [
        ("2026-05-29", "8-K", "0000034088-26-000078")
    ]
    assert all(r[0] <= "2026-06-30" for r in old)
    assert [(r[0], r[1]) for r in new] == [("2026-10-16", "DEFA14A"), ("2026-11-12", "DEFA14A")]
    assert result.new == 14
    url = conn.execute(
        "select source_url from filings where accession = '0001193125-26-457440'"
    ).fetchone()
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/2115436/000119312526457440/d173488ddefa14a.htm",
    )


def test_filing_outside_cik_range_excluded(conn: psycopg.Connection) -> None:
    """A filing under a CIK is kept only while that CIK is in use for the company."""
    sync_companies(conn, XOM, XOM_CIKS)
    conn.execute("update company_ciks set valid_to = '2026-04-30' where cik = '0000034088'")

    run(conn, xom_edgar(), *SEASON)

    old = [r for r in stored(conn) if r[3] == "0000034088"]
    assert old
    assert all(r[0] <= "2026-04-30" for r in old)
    assert "0000034088-26-000078" not in {r[2] for r in old}


def test_8k12b_reported_not_stored(conn: psycopg.Connection) -> None:
    sync_companies(conn, XOM, XOM_CIKS)
    company = conn.execute("select id from companies where ticker = 'XOM'").fetchone()
    assert company is not None

    result = run(conn, xom_edgar(), *SEASON)

    assert result.successor_notices == [
        SuccessorNotice(
            company_id=company[0],
            cik="0002115436",
            accession="0001193125-26-291990",
            filed=date(2026, 7, 1),
        )
    ]
    assert conn.execute(
        "select count(*) from filings where accession = '0001193125-26-291990'"
    ).fetchone() == (0,)


def test_mismatched_arrays_raise_naming_cik(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    body = synthetic_submissions(("DEF 14A", "2026-01-08", "0009999999-26-000001", ""))
    body["filings"]["recent"]["items"] = []
    edgar = FakeEdgar({"CIK0000320193.json": body})

    with pytest.raises(EdgarError, match="0000320193"):
        run(conn, edgar, *SEASON)


def test_bad_filing_date_raises_naming_cik(conn: psycopg.Connection, tmp_path: Path) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    body = synthetic_submissions(("DEF 14A", "Jan 8 2026", "0009999999-26-000001", ""))
    edgar = FakeEdgar({"CIK0000320193.json": body})

    with pytest.raises(EdgarError, match="0000320193"):
        run(conn, edgar, *SEASON)


@pytest.mark.parametrize(
    ("body", "missing"),
    [
        ({"cik": "320193"}, "filings"),
        ({"filings": {"files": []}}, "filings.recent"),
        ({"filings": {"recent": {"form": []}}}, "filingDate"),
    ],
)
def test_malformed_submissions_raise_naming_cik(
    conn: psycopg.Connection, tmp_path: Path, body: dict[str, Any], missing: str
) -> None:
    sync_companies(conn, apple_only(tmp_path, "2026-01-01,"))
    edgar = FakeEdgar({"CIK0000320193.json": body})

    with pytest.raises(EdgarError, match=f"0000320193.*{missing}"):
        run(conn, edgar, *SEASON)


CliEnv = Callable[..., None]


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, role_urls: dict[str, str]) -> CliEnv:
    """Point `obr` at the test database and replace `discover` with ``fake``."""

    def setup(fake: Callable[..., DiscoverResult]) -> None:
        monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
        monkeypatch.setenv("OBR_EDGAR_USER_AGENT", "Test Org test@example.org")
        monkeypatch.setattr("openboardroom.pipeline.discover.discover", fake)

    return setup


def test_cli_prints_counts_and_notices(cli_env: CliEnv, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[tuple[date, date]] = []

    def fake(conn: psycopg.Connection, client: EdgarClient, start: date, end: date) -> Any:
        seen.append((start, end))
        notice = SuccessorNotice(7, "0002115436", "0001193125-26-291990", date(2026, 7, 1))
        return DiscoverResult(new=14, existing=2, successor_notices=[notice])

    cli_env(fake)

    code = main(["discover", "--since", "2026-01-01", "--until", "2026-12-31"])

    out = capsys.readouterr().out
    assert code == 0
    assert seen == [SEASON]
    assert "14 new, 2 existing" in out
    assert "0001193125-26-291990" in out
    assert "0002115436" in out


def test_cli_exits_nonzero_on_edgar_error(
    cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(*_: object) -> DiscoverResult:
        raise EdgarError("EDGAR returned HTTP 503 for https://data.sec.gov/x")

    cli_env(fake)

    code = main(["discover", "--since", "2026-01-01", "--until", "2026-12-31"])

    assert code != 0
    assert "HTTP 503" in capsys.readouterr().err
