"""Tests for the covered-company list and its sync into Postgres."""

import re
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from openboardroom.cli import main
from openboardroom.companies import (
    CikRange,
    CoveredCompany,
    MembershipRange,
    SyncResult,
    covered_companies,
    read_companies_csv,
    read_company_ciks_csv,
    sync_companies,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE = FIXTURES / "companies_small.csv"
XOM = FIXTURES / "companies_xom.csv"
XOM_CIKS = FIXTURES / "company_ciks_xom.csv"
DATA = Path(__file__).resolve().parent.parent / "data"
CIKS_HEADER = "ticker,cik,valid_from,valid_to,note\n"
HEADER = "cik,ticker,name,index,start_date,end_date,dates_approximate\n"


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
    sync_companies(conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,,false"))
    result = sync_companies(
        conn, write_csv(tmp_path, "0000320193,APLE,Apple Inc.,sp100,2026-01-01,,false")
    )
    assert result == SyncResult(added=0, updated=1)
    rows = conn.execute("select cik, ticker from companies").fetchall()
    assert rows == [("0000320193", "APLE")]


def test_unpadded_cik_is_stored_zero_padded(conn: psycopg.Connection, tmp_path: Path) -> None:
    """CIKs in the CSV may be unpadded; they are stored as 10 digits."""
    sync_companies(conn, write_csv(tmp_path, "320193,AAPL,Apple Inc.,sp100,2026-01-01,,false"))
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
        ciks=[CikRange(cik="0001652044", valid_from=date(1900, 1, 1), valid_to=None)],
        memberships=[MembershipRange(start=date(2026, 3, 1), end=date(2026, 6, 30))],
    )
    second_half = covered_companies(conn, "sp100", date(2026, 7, 1), date(2026, 12, 31))
    assert [c.ticker for c in second_half] == ["AAPL", "MSFT"]


def test_membership_end_date_change_counts_as_update(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    """Ending an existing membership is reported as an update."""
    sync_companies(conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,,false"))
    result = sync_companies(
        conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,2026-09-30,false")
    )
    assert result == SyncResult(added=0, updated=1)


def test_reader_skips_comments_and_rejects_bad_cik(tmp_path: Path) -> None:
    """`#` lines are skipped; a non-numeric CIK or a wrong header is an error."""
    rows = read_companies_csv(FIXTURE)
    assert [r.ticker for r in rows] == ["AAPL", "MSFT", "GOOGL"]
    assert rows[0].end_date is None
    with pytest.raises(ValueError, match="CIK"):
        read_companies_csv(write_csv(tmp_path, "AAPL,AAPL,Apple Inc.,sp100,2026-01-01,,false"))
    bad_header = tmp_path / "bad.csv"
    bad_header.write_text("cik,ticker\n0000320193,AAPL\n")
    with pytest.raises(ValueError, match="expected columns"):
        read_companies_csv(bad_header)


def test_committed_list_is_well_formed(conn: psycopg.Connection) -> None:
    """data/companies.csv parses, has one row per CIK, records its source, and loads."""
    path = DATA / "companies.csv"
    header = path.read_text().splitlines()[0]
    assert header.startswith("# source: iShares S&P 100 ETF (OEF) holdings, as of ")
    assert "https://www.ishares.com/" in header
    assert re.search(r"sha256 [0-9a-f]{64}", header)
    rows = read_companies_csv(path)
    assert len(rows) == 100
    assert len({r.cik for r in rows}) == len(rows)
    assert all(r.index == "sp100" and r.start_date == date(2026, 1, 1) for r in rows)
    assert all(r.dates_approximate for r in rows)
    assert sync_companies(conn, path, DATA / "company_ciks.csv") == SyncResult(added=100, updated=0)


def test_committed_cik_list_has_xom_succession() -> None:
    """data/company_ciks.csv records ExxonMobil's change of filer on 2026-07-01."""
    rows = read_company_ciks_csv(DATA / "company_ciks.csv")
    xom = [(r.cik, r.valid_from, r.valid_to) for r in rows if r.ticker == "XOM"]
    assert xom == [
        ("0000034088", date(1994, 3, 4), date(2026, 6, 30)),
        ("0002115436", date(2026, 7, 1), None),
    ]


def test_companies_sync_without_user_agent(
    monkeypatch: pytest.MonkeyPatch,
    role_urls: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`obr companies sync --csv PATH --ciks PATH` needs only OBR_DATABASE_URL."""
    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.delenv("OBR_EDGAR_USER_AGENT", raising=False)
    with psycopg.connect(role_urls["pipeline_rw"]) as check:
        before = check.execute("select count(*) from companies").fetchone()
    try:
        assert main(["companies", "sync", "--csv", str(XOM), "--ciks", str(XOM_CIKS)]) == 0
        out = capsys.readouterr().out
        assert "added" in out and "updated" in out
        with psycopg.connect(role_urls["pipeline_rw"]) as check:
            ranges = check.execute("select count(*) from company_ciks").fetchone()
        assert ranges == (2,)
    finally:
        with psycopg.connect(role_urls["pipeline_rw"]) as cleanup:
            for table in ("company_ciks", "index_memberships"):
                cleanup.execute(
                    sql.SQL(
                        "delete from {} where company_id in "
                        "(select id from companies where ticker = 'XOM')"
                    ).format(sql.Identifier(table))
                )
            cleanup.execute("delete from companies where ticker = 'XOM'")
    assert before == (0,)


def test_covered_companies_returns_overlapping_ranges(conn: psycopg.Connection) -> None:
    """A company is listed once, with only the CIK ranges in use during the window."""
    sync_companies(conn, XOM, XOM_CIKS)
    old = CikRange(cik="0000034088", valid_from=date(1994, 3, 4), valid_to=date(2026, 6, 30))
    new = CikRange(cik="0002115436", valid_from=date(2026, 7, 1), valid_to=None)

    season = covered_companies(conn, "sp100", date(2026, 1, 1), date(2026, 12, 31))
    later = covered_companies(conn, "sp100", date(2027, 1, 1), date(2027, 12, 31))

    assert [(c.ticker, c.cik, c.ciks) for c in season] == [("XOM", "0002115436", [old, new])]
    assert [(c.ticker, c.ciks) for c in later] == [("XOM", [new])]


def test_covered_companies_one_entry_per_company(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Two overlapping memberships of one company give one entry spanning both."""
    sync_companies(
        conn,
        write_csv(
            tmp_path,
            "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,2026-03-31,false",
            "0000320193,AAPL,Apple Inc.,sp100,2026-06-01,,false",
        ),
    )

    covered = covered_companies(conn, "sp100", date(2026, 1, 1), date(2026, 12, 31))

    assert [(c.ticker, c.membership_start, c.membership_end) for c in covered] == [
        ("AAPL", date(2026, 1, 1), None)
    ]
    # The gap between them stays visible, so discovery can skip filings dated in it.
    assert covered[0].memberships == [
        MembershipRange(start=date(2026, 1, 1), end=date(2026, 3, 31)),
        MembershipRange(start=date(2026, 6, 1), end=None),
    ]


def test_dates_approximate_loaded(conn: psycopg.Connection) -> None:
    """The CSV's dates_approximate flag reaches index_memberships, and a change updates it."""
    sync_companies(conn, FIXTURE)
    rows = conn.execute(
        """
        select c.ticker, m.dates_approximate
        from index_memberships m join companies c on c.id = m.company_id
        order by c.ticker
        """
    ).fetchall()
    assert rows == [("AAPL", False), ("GOOGL", False), ("MSFT", True)]


def test_dates_approximate_change_counts_as_update(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    sync_companies(conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,,true"))
    result = sync_companies(
        conn, write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,,false")
    )
    assert result == SyncResult(added=0, updated=1)


def test_new_cik_keeps_the_same_company(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Moving a company to a new CIK, with the old one in the CIK file, updates it in place."""
    sync_companies(
        conn, write_csv(tmp_path, "0000034088,XOM,Exxon Mobil Corp,sp100,2026-01-01,,true")
    )
    before = conn.execute("select id from companies").fetchall()

    result = sync_companies(conn, XOM, XOM_CIKS)

    assert result == SyncResult(added=0, updated=1)
    assert conn.execute("select id, cik, name from companies").fetchall() == [
        (before[0][0], "0002115436", "ExxonMobil Holdings Corp")
    ]
    ranges = conn.execute(
        "select cik, valid_from, valid_to from company_ciks order by valid_from"
    ).fetchall()
    assert ranges == [
        ("0000034088", date(1994, 3, 4), date(2026, 6, 30)),
        ("0002115436", date(2026, 7, 1), None),
    ]
    assert sync_companies(conn, XOM, XOM_CIKS) == SyncResult(added=0, updated=0)


def write_ciks(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "company_ciks.csv"
    path.write_text("# source: test\n" + CIKS_HEADER + "".join(r + "\n" for r in rows))
    return path


def test_cik_file_errors(conn: psycopg.Connection, tmp_path: Path) -> None:
    """Unknown tickers, a closed current CIK, and a bad header are errors."""
    with pytest.raises(ValueError, match="not in"):
        sync_companies(conn, XOM, write_ciks(tmp_path, "MSFT,0000789019,1986-03-13,,"))
    with pytest.raises(ValueError, match="current CIK"):
        sync_companies(conn, XOM, write_ciks(tmp_path, "XOM,0002115436,2026-07-01,2026-08-01,"))
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker,cik\nXOM,0002115436\n")
    with pytest.raises(ValueError, match="expected columns"):
        read_company_ciks_csv(bad)


def test_bad_dates_approximate_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dates_approximate"):
        read_companies_csv(write_csv(tmp_path, "0000320193,AAPL,Apple Inc.,sp100,2026-01-01,,yes"))


def test_cik_claimed_by_two_companies_rejected(conn: psycopg.Connection, tmp_path: Path) -> None:
    """A CIK file that would join two existing companies is an error, not a silent merge."""
    sync_companies(
        conn,
        write_csv(
            tmp_path,
            "0000034088,XOM,Exxon Mobil Corp,sp100,2026-01-01,,true",
            "0002115436,XOMH,ExxonMobil Holdings Corp,sp100,2026-01-01,,true",
        ),
    )
    with pytest.raises(ValueError, match="more than one company"):
        sync_companies(conn, XOM, XOM_CIKS)


def test_sync_without_cik_file_keeps_recorded_ranges(conn: psycopg.Connection) -> None:
    """The default range for a current CIK never overwrites one the CIK file recorded."""
    sync_companies(conn, XOM, XOM_CIKS)

    assert sync_companies(conn, XOM) == SyncResult(added=0, updated=0)
    assert conn.execute(
        "select valid_from from company_ciks where cik = '0002115436'"
    ).fetchall() == [(date(2026, 7, 1),)]


def test_closed_current_cik_rejected_without_cik_file(conn: psycopg.Connection) -> None:
    """A current CIK whose recorded range has ended is an error even without the CIK file."""
    sync_companies(conn, XOM, XOM_CIKS)
    conn.execute("update company_ciks set valid_to = '2026-08-01' where cik = '0002115436'")

    with pytest.raises(ValueError, match="current CIK"):
        sync_companies(conn, XOM)
