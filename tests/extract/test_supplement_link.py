"""Tests for linking supplements (DEFA14A) to their meetings (spec §7.1, update 2026-11-25)."""

from datetime import date

import psycopg
from review_seed import add_supplement, seed_filing

from openboardroom.extract import supplement
from openboardroom.extract.supplement import Supplement

PROXY = "0000320193-26-000001"
SUPP = "0000320193-26-000101"


def _meeting(conn: psycopg.Connection, filing_id: int) -> int:
    row = conn.execute(
        "select id from meetings where proxy_filing_id = %s", (filing_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def test_links_same_day_supplement(conn: psycopg.Connection) -> None:
    proxy = seed_filing(conn, PROXY, filed_at="2026-01-10")
    supp = add_supplement(conn, SUPP, "2026-01-10")

    assert supplement.pending_supplements(conn) == [
        Supplement(supp, SUPP, _meeting(conn, proxy.filing_id))
    ]


def test_supplement_after_meeting_has_no_meeting(conn: psycopg.Connection) -> None:
    seed_filing(conn, PROXY, filed_at="2026-01-10", meeting_date=date(2026, 3, 1))
    supp = add_supplement(conn, SUPP, "2026-03-02")

    assert supplement.pending_supplements(conn) == [Supplement(supp, SUPP, None)]


def test_supplement_with_no_earlier_proxy_has_no_meeting(conn: psycopg.Connection) -> None:
    seed_filing(conn, PROXY, filed_at="2026-01-10")
    supp = add_supplement(conn, SUPP, "2026-01-09")

    assert supplement.pending_supplements(conn) == [Supplement(supp, SUPP, None)]


def test_links_latest_proxy_whose_meeting_is_ahead(conn: psycopg.Connection) -> None:
    annual = seed_filing(conn, PROXY, filed_at="2026-01-10", meeting_date=date(2026, 4, 20))
    special = seed_filing(
        conn, "0000320193-26-000002", filed_at="2026-02-01", meeting_date=date(2026, 3, 1)
    )
    before_special = add_supplement(conn, SUPP, "2026-02-15")
    after_special = add_supplement(conn, "0000320193-26-000102", "2026-03-10")

    linked = {s.filing_id: s.meeting_id for s in supplement.pending_supplements(conn)}

    assert linked == {
        before_special: _meeting(conn, special.filing_id),
        after_special: _meeting(conn, annual.filing_id),
    }


def test_out_of_scope_proxy_never_links(conn: psycopg.Connection) -> None:
    seed_filing(conn, PROXY, stage="out_of_scope", item_list=[])
    conn.execute("delete from meetings")
    supp = add_supplement(conn, SUPP, "2026-01-20")

    assert supplement.pending_supplements(conn) == [Supplement(supp, SUPP, None)]


def test_waits_for_unextracted_proxy_or_one_without_items(conn: psycopg.Connection) -> None:
    proxy = seed_filing(conn, PROXY, stage="in_review", item_list=[])
    add_supplement(conn, SUPP, "2026-01-20")
    assert supplement.pending_supplements(conn) == []

    conn.execute("delete from meetings where proxy_filing_id = %s", (proxy.filing_id,))
    conn.execute("update filings set stage = 'fetched' where id = %s", (proxy.filing_id,))
    assert supplement.pending_supplements(conn) == []


def test_skips_supplement_waiting_on_a_batch_and_filters(conn: psycopg.Connection) -> None:
    seed_filing(conn, PROXY)
    waiting = add_supplement(conn, SUPP, "2026-01-20")
    conn.execute(
        "insert into extraction_runs (filing_id, kind, status, batch_id) "
        "values (%s, 'supplement', 'submitted', 'b')",
        (waiting,),
    )
    other = add_supplement(conn, "0000320193-26-000102", "2026-01-21")
    third = add_supplement(conn, "0000320193-26-000103", "2026-01-22")

    assert [s.filing_id for s in supplement.pending_supplements(conn)] == [other, third]
    assert [s.filing_id for s in supplement.pending_supplements(conn, limit=1)] == [other]
    assert [
        s.filing_id for s in supplement.pending_supplements(conn, accession="0000320193-26-000103")
    ] == [third]


def test_meeting_items_lists_numbers_and_nominees(conn: psycopg.Connection) -> None:
    proxy = seed_filing(conn, PROXY)

    items = supplement.meeting_items(conn, _meeting(conn, proxy.filing_id))

    assert items == [
        {"number": "1", "title": "Election of Directors", "type": "management",
         "board_recommendation": "for_each_nominee", "status": "active",
         "nominees": ["Ann Lee", "Bo Chen"]},
        {"number": "2", "title": "Report on Water Use", "type": "shareholder",
         "board_recommendation": "against", "status": "active", "nominees": []},
    ]  # fmt: skip
