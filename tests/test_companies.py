"""Tests for the covered-company list and its sync into Postgres."""

import re
from datetime import date
from pathlib import Path

import psycopg
import pytest

from openboardroom.cli import main
from openboardroom.companies import (
    CoveredCompany,
    SyncResult,
    covered_companies,
    read_companies_csv,
    sync_companies,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "companies_small.csv"
HEADER = "cik,ticker,name,index,start_date,end_date\n"


def write_csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text("# source: test, as of 2026-01-01\n" + HEADER + "".join(r + "\n" for r in rows))
    return path


def test_sync_is_idempotent(conn: psycopg.Connection) -> None:
    """A second sync of the same file changes nothing."""
    assert sync_companies(conn, FIXTURE) == SyncResult(added=3, updated=0)
    assert sync_companies(conn, FIXTURE) == SyncResult(added=0, updated=0)
    assert conn.execute("select count(*) from index_memberships").fetchone() == (3,)


def test_ticker_change_updates_row(conn: psycopg.Connection, tmp_path: Path) -> None:
    """A changed ticker for a known CIK updates that company instead of adding one."""
    sync_companies(conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,"))
    result = sync_companies(
        conn, write_csv(tmp_path, "0000320193,APLE,Apple Inc.,sp100,2026-01-01,")
    )
    assert result == SyncResult(added=0, updated=1)
    rows = conn.execute("select cik, ticker from companies").fetchall()
    assert rows == [("0000320193", "APLE")]


def test_unpadded_cik_is_stored_zero_padded(conn: psycopg.Connection, tmp_path: Path) -> None:
    """CIKs in the CSV may be unpadded; they are stored as 10 digits."""
    sync_companies(conn, write_csv(tmp_path, "320193,AAPL,Apple Inc.,sp100,2026-01-01,"))
    assert conn.execute("select cik from companies").fetchall() == [("0000320193",)]


def test_partial_membership_overlap(conn: psycopg.Connection) -> None:
    """Membership counts when it overlaps the window at all."""
    sync_companies(conn, FIXTURE)
    full_year = covered_companies(conn, "sp100", date(2026, 1, 1), date(2026, 12, 31))
    assert [c.ticker for c in full_year] == ["AAPL", "GOOGL", "MSFT"]
    googl = next(c for c in full_year if c.ticker == "GOOGL")
    assert googl == CoveredCompany(
        company_id=googl.company_id,
        cik="0001652044",
        ticker="GOOGL",
        membership_start=date(2026, 3, 1),
        membership_end=date(2026, 6, 30),
    )
    second_half = covered_companies(conn, "sp100", date(2026, 7, 1), date(2026, 12, 31))
    assert [c.ticker for c in second_half] == ["AAPL", "MSFT"]


def test_membership_end_date_change_counts_as_update(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Ending an existing membership is reported as an update."""
    sync_companies(conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,"))
    result = sync_companies(
        conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,2026-09-30")
    )
    assert result == SyncResult(added=0, updated=1)


def test_reader_skips_comments_and_rejects_bad_cik(tmp_path: Path) -> None:
    """`#` lines are skipped; a non-numeric CIK or a wrong header is an error."""
    rows = read_companies_csv(FIXTURE)
    assert [r.ticker for r in rows] == ["AAPL", "MSFT", "GOOGL"]
    assert rows[0].end_date is None
    with pytest.raises(ValueError, match="CIK"):
        read_companies_csv(write_csv(tmp_path, "AAPL,AAPL,Apple Inc.,sp100,2026-01-01,"))
    bad_header = tmp_path / "bad.csv"
    bad_header.write_text("cik,ticker\n0000320193,AAPL\n")
    with pytest.raises(ValueError, match="expected columns"):
        read_companies_csv(bad_header)


def test_committed_list_is_well_formed(conn: psycopg.Connection) -> None:
    """data/companies.csv parses, has one row per CIK, records its source, and loads."""
    path = Path(__file__).resolve().parent.parent / "data" / "companies.csv"
    header = path.read_text().splitlines()[0]
    assert header.startswith("# source: iShares S&P 100 ETF (OEF) holdings, as of ")
    assert "https://www.ishares.com/" in header
    assert re.search(r"sha256 [0-9a-f]{64}", header)
    rows = read_companies_csv(path)
    assert len(rows) == 100
    assert len({r.cik for r in rows}) == len(rows)
    assert all(r.index == "sp100" and r.start_date == date(2026, 1, 1) for r in rows)
    assert sync_companies(conn, path) == SyncResult(added=100, updated=0)


def test_companies_sync_without_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    role_urls: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`obr companies sync --csv PATH` needs only OBR_DATABASE_URL, and reports counts."""
    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.delenv("OBR_EDGAR_USER_AGENT", raising=False)
    with psycopg.connect(role_urls["pipeline_rw"]) as check:
        before = check.execute("select count(*) from companies").fetchone()
    try:
        assert main(["companies", "sync", "--csv", str(FIXTURE)]) == 0
        out = capsys.readouterr().out
        assert "added" in out and "updated" in out
    finally:
        with psycopg.connect(role_urls["pipeline_rw"]) as cleanup:
            cleanup.execute(
                "delete from index_memberships where company_id in "
                "(select id from companies where cik = any(%s))",
                (["0000320193", "0000789019", "0001652044"],),
            )
            cleanup.execute(
                "delete from companies where cik = any(%s)",
                (["0000320193", "0000789019", "0001652044"],),
            )
    assert before == (0,)
