"""Tests for the review queue and the filing view, against real Postgres."""

import psycopg
from review_seed import PASSED, seed_filing

from openboardroom.extract.checks import CheckResult
from openboardroom.review import store


def _approve_all(conn: psycopg.Connection, filing_id: int) -> None:
    conn.execute(
        "update item_texts set review_status = 'approved' where ballot_item_id in "
        "(select bi.id from ballot_items bi join meetings m on m.id = bi.meeting_id "
        "where m.proxy_filing_id = %s)",
        (filing_id,),
    )


def test_queue_holds_in_review_and_pending_texts(conn: psycopg.Connection) -> None:
    seed_filing(conn, "0000000001-26-000001", stage="in_review", cik="0000000001", ticker="A")
    seed_filing(conn, "0000000002-26-000001", stage="checked", cik="0000000002", ticker="B")
    done = seed_filing(conn, "0000000003-26-000001", stage="checked", cik="0000000003")
    _approve_all(conn, done.filing_id)
    seed_filing(conn, "0000000004-26-000001", stage="fetched", cik="0000000004", ticker="D")

    queued = store.queue(conn)

    assert [(q.accession, q.stage, q.pending_texts) for q in queued] == [
        ("0000000001-26-000001", "in_review", 6),
        ("0000000002-26-000001", "checked", 6),
    ]
    assert queued[0].ticker == "A"


def test_queue_is_oldest_first(conn: psycopg.Connection) -> None:
    seed_filing(conn, "0000000001-26-000001", filed_at="2026-03-01", cik="0000000001")
    seed_filing(conn, "0000000002-26-000001", filed_at="2026-01-01", cik="0000000002")

    assert [q.accession for q in store.queue(conn)] == [
        "0000000002-26-000001",
        "0000000001-26-000001",
    ]


def test_load_returns_latest_text_versions_and_failed_checks(conn: psycopg.Connection) -> None:
    checks = [*PASSED[:3], CheckResult("consistency", False, "item 2: request is required")]
    seeded = seed_filing(conn, stage="in_review", checks=checks)
    item_id = seeded.item_ids[1]
    conn.execute(
        "insert into item_texts (ballot_item_id, kind, version, body, origin) "
        "values (%s, 'summary', 2, 'Second.', 'model')",
        (item_id,),
    )

    view = store.load(conn, "0000320193-26-000001")

    assert view is not None
    assert (view.stage, view.ticker, view.source_url) == (
        "in_review", "AAPL", "https://www.sec.gov/Archives/x.htm",
    )  # fmt: skip
    assert view.failed_checks == [("consistency", "item 2: request is required")]
    assert view.meeting is not None and view.meeting["shares_outstanding"] == 1000
    election, proposal = view.items
    assert (election.number, election.nominees, election.published) == (
        "1", ["Ann Lee", "Bo Chen"], False,
    )  # fmt: skip
    assert proposal.fields["lead_filer"] == "A Fund"
    summary = next(t for t in proposal.texts if t.kind == "summary")
    assert (summary.version, summary.body, summary.origin) == (2, "Second.", "model")
    assert [t.kind for t in proposal.texts] == ["summary", "proponent_argument", "board_argument"]


def test_load_unknown_accession_is_none(conn: psycopg.Connection) -> None:
    assert store.load(conn, "0000000000-00-000000") is None


def test_load_filing_without_meeting(conn: psycopg.Connection) -> None:
    # A refused extraction goes to review with no meeting or items.
    seeded = seed_filing(conn, stage="in_review", item_list=[])
    conn.execute("delete from meetings where proxy_filing_id = %s", (seeded.filing_id,))

    view = store.load(conn, "0000320193-26-000001")

    assert view is not None and (view.meeting, view.items) == (None, [])
    assert [q.accession for q in store.queue(conn)] == ["0000320193-26-000001"]


def test_queue_keeps_texts_that_need_manual_writing(conn: psycopg.Connection) -> None:
    # Final review #3: after two rejections a text needs a person; it must stay visible.
    seeded = seed_filing(conn)
    _approve_all(conn, seeded.filing_id)
    conn.execute(
        "update item_texts set review_status = 'needs_manual' "
        "where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[1],),
    )

    queued = store.queue(conn)

    assert [(q.pending_texts, q.needs_manual) for q in queued] == [(0, 1)]
