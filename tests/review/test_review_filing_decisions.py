"""Tests for fixing, clearing, and requeueing filings in review."""

from datetime import date
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql
from review_seed import seed_filing

from openboardroom.review import store
from openboardroom.review.store import ReviewError

ACCESSION = "0000320193-26-000001"


def _count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(sql.SQL("select count(*) from {}").format(sql.Identifier(table))).fetchone()
    assert row is not None
    return row[0]


def _publish(conn: psycopg.Connection, item_id: int) -> None:
    conn.execute("update ballot_items set published_at = now() where id = %s", (item_id,))


def _decision(conn: psycopg.Connection, review_id: int) -> tuple[str, str] | None:
    return conn.execute(
        "select decision, reason from reviews where id = %s", (review_id,)
    ).fetchone()


def test_fix_item_updates_and_records_before_after(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="in_review")
    item_id = seeded.item_ids[1]

    review_id = store.fix_item(conn, item_id, "co_filers", "B Fund; C Trust", "quinn", "named")
    store.fix_item(conn, item_id, "vote_threshold_pct", "66.67", "quinn", "supermajority")
    store.fix_item(conn, item_id, "abstentions_count_against", "true", "quinn", "stated")
    store.fix_item(conn, item_id, "representative", "", "quinn", "none named")

    row = conn.execute(
        "select co_filers, vote_threshold_pct, abstentions_count_against, representative "
        "from ballot_items where id = %s",
        (item_id,),
    ).fetchone()
    assert row == (["B Fund", "C Trust"], Decimal("66.67"), True, None)
    review = conn.execute(
        "select decision, ballot_item_id, reason, before, after from reviews where id = %s",
        (review_id,),
    ).fetchone()
    assert review == (
        "fix", item_id, "named",
        {"field": "co_filers", "value": []},
        {"field": "co_filers", "value": ["B Fund", "C Trust"]},
    )  # fmt: skip
    assert _count(conn, "corrections") == 0


def test_fix_published_item_writes_correction(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="published")
    item_id = seeded.item_ids[1]
    _publish(conn, item_id)

    review_id = store.fix_item(conn, item_id, "lead_filer", "A Fund LP", "quinn", "full name")

    assert conn.execute(
        "select field, before, after, reason, review_id from corrections"
    ).fetchall() == [("lead_filer", "A Fund", "A Fund LP", "full name", review_id)]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_quote", "x", "can't be fixed"),
        ("category", "merger", "not allowed"),
        ("vote_threshold_pct", "abc", "not a number"),
        ("abstentions_count_against", "maybe", "true or false"),
    ],
)
def test_fix_rejects_unknown_field_and_bad_value(
    conn: psycopg.Connection, field: str, value: str, message: str
) -> None:
    seeded = seed_filing(conn, stage="in_review")
    before = conn.execute(
        "select category, vote_threshold_pct from ballot_items where id = %s",
        (seeded.item_ids[1],),
    ).fetchone()

    with pytest.raises(ReviewError, match=message):
        store.fix_item(conn, seeded.item_ids[1], field, value, "quinn", "try")

    assert (
        conn.execute(
            "select category, vote_threshold_pct from ballot_items where id = %s",
            (seeded.item_ids[1],),
        ).fetchone()
        == before
    )
    assert _count(conn, "reviews") == 0


def test_fix_needs_a_reason_and_a_known_item(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="in_review")
    with pytest.raises(ReviewError, match="reason"):
        store.fix_item(conn, seeded.item_ids[0], "title", "T", "quinn", " ")
    with pytest.raises(ReviewError, match="no item"):
        store.fix_item(conn, 999_999, "title", "T", "quinn", "r")


def test_fix_meeting_parses_dates_and_integers(conn: psycopg.Connection) -> None:
    seed_filing(conn, stage="in_review")

    store.fix_meeting(conn, ACCESSION, "shares_outstanding", "615657898", "quinn", "F18")
    store.fix_meeting(conn, ACCESSION, "record_date", "2026-01-05", "quinn", "stated")

    assert conn.execute("select shares_outstanding, record_date from meetings").fetchone() == (
        615657898, date(2026, 1, 5),
    )  # fmt: skip
    with pytest.raises(ReviewError, match="not a date"):
        store.fix_meeting(conn, ACCESSION, "meeting_date", "May 5", "quinn", "r")
    with pytest.raises(ReviewError, match="not a whole number"):
        store.fix_meeting(conn, ACCESSION, "shares_outstanding", "1.5", "quinn", "r")
    with pytest.raises(ReviewError, match="can't be fixed"):
        store.fix_meeting(conn, ACCESSION, "date_quote", "x", "quinn", "r")
    with pytest.raises(ReviewError, match="no meeting"):
        store.fix_meeting(conn, "0000000000-00-000000", "kind", "annual", "quinn", "r")


def test_fix_meeting_of_published_filing_corrects_each_published_item(
    conn: psycopg.Connection,
) -> None:
    seeded = seed_filing(conn, stage="published")
    _publish(conn, seeded.item_ids[0])

    store.fix_meeting(conn, ACCESSION, "meeting_time", "10:00 a.m. Pacific Time", "quinn", "r")

    rows = conn.execute("select ballot_item_id, field, before, after from corrections").fetchall()
    assert rows == [
        (seeded.item_ids[0], "meeting.meeting_time", "9:00 a.m. Pacific Time",
         "10:00 a.m. Pacific Time"),
    ]  # fmt: skip


def test_clear_moves_in_review_to_checked(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="in_review")

    review_id = store.clear(conn, ACCESSION, "quinn", "count confirmed by hand")

    assert conn.execute(
        "select stage from filings where id = %s", (seeded.filing_id,)
    ).fetchone() == ("checked",)
    assert _decision(conn, review_id) == ("clear", "count confirmed by hand")


def test_clear_refuses_wrong_stage_or_no_items(conn: psycopg.Connection) -> None:
    seed_filing(conn, stage="checked")
    with pytest.raises(ReviewError, match="in_review"):
        store.clear(conn, ACCESSION, "quinn", "r")
    seed_filing(conn, "0000000002-26-000001", stage="in_review", cik="0000000002", item_list=[])
    with pytest.raises(ReviewError, match="requeue"):
        store.clear(conn, "0000000002-26-000001", "quinn", "r")
    with pytest.raises(ReviewError, match="no filing"):
        store.clear(conn, "0000000000-00-000000", "quinn", "r")


def test_requeue_deletes_extraction_rows_and_keeps_its_review(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="in_review")
    regex_run = conn.execute(
        "insert into extraction_runs (filing_id, kind, status) values (%s, 'regex', 'succeeded') "
        "returning id",
        (seeded.filing_id,),
    ).fetchone()
    assert regex_run is not None

    review_id = store.requeue(conn, ACCESSION, "quinn", "dropped item 7")

    assert conn.execute(
        "select stage from filings where id = %s", (seeded.filing_id,)
    ).fetchone() == ("fetched",)
    for table in ("meetings", "ballot_items", "nominees", "item_texts", "check_results",
                  "extraction_runs"):  # fmt: skip
        assert _count(conn, table) == 0, table
    assert _decision(conn, review_id) == ("requeue", "dropped item 7")


def test_requeue_after_text_decisions_keeps_those_reviews(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="checked")
    approve_ids = store.approve_item(conn, seeded.item_ids[0], "quinn")

    store.requeue(conn, ACCESSION, "quinn", "re-extract")

    assert conn.execute(
        "select decision, item_text_id, ballot_item_id from reviews where id = %s",
        (approve_ids[0],),
    ).fetchone() == ("approve", None, None)


def test_requeue_refuses_published_filing(conn: psycopg.Connection) -> None:
    # A published filing reopened for review (reconciliation, Plan 4) is back in_review.
    seeded = seed_filing(conn, stage="in_review")
    _publish(conn, seeded.item_ids[0])

    with pytest.raises(ReviewError, match="published"):
        store.requeue(conn, ACCESSION, "quinn", "r")

    assert _count(conn, "ballot_items") == 2
    assert _count(conn, "reviews") == 0


def test_requeue_refuses_when_vote_results_or_pending_changes_exist(
    conn: psycopg.Connection,
) -> None:
    seeded = seed_filing(conn, stage="in_review")
    conn.execute(
        "insert into vote_results (ballot_item_id, votes_for, source_filing_id) values (%s, 1, %s)",
        (seeded.item_ids[0], seeded.filing_id),
    )
    with pytest.raises(ReviewError, match="vote results"):
        store.requeue(conn, ACCESSION, "quinn", "r")
    conn.execute("delete from vote_results")
    conn.execute(
        "insert into pending_changes "
        "(meeting_id, ballot_item_id, field, extraction_run_id, reason) "
        "select meeting_id, id, 'title', %s, 'rerun' from ballot_items where id = %s",
        (seeded.run_id, seeded.item_ids[0]),
    )
    with pytest.raises(ReviewError, match="changes from supplements"):
        store.requeue(conn, ACCESSION, "quinn", "r")
    assert _count(conn, "ballot_items") == 2


@pytest.mark.parametrize("stage", ["fetched", "discovered", "published"])
def test_requeue_refuses_other_stages(conn: psycopg.Connection, stage: str) -> None:
    # Final review #5: a fetched filing may be waiting on a batch; requeue would bill it twice.
    seed_filing(conn, stage=stage)
    with pytest.raises(ReviewError, match=f"is {stage}"):
        store.requeue(conn, ACCESSION, "quinn", "r")
    assert _count(conn, "ballot_items") == 2


def test_requeue_allows_out_of_scope(conn: psycopg.Connection) -> None:
    seed_filing(conn, stage="out_of_scope")
    store.requeue(conn, ACCESSION, "quinn", "membership dates fixed")
    assert _count(conn, "ballot_items") == 0


@pytest.mark.parametrize(
    ("field", "value"), [("status", ""), ("shares_outstanding", "99999999999999999999")]
)
def test_fix_database_errors_become_review_errors(
    conn: psycopg.Connection, field: str, value: str
) -> None:
    # Final review (promoted minor): any value the database refuses is a ReviewError.
    seeded = seed_filing(conn, stage="in_review")
    with pytest.raises(ReviewError, match="not allowed"):
        if field == "status":
            store.fix_item(conn, seeded.item_ids[0], field, value, "quinn", "r")
        else:
            store.fix_meeting(conn, ACCESSION, field, value, "quinn", "r")
    assert _count(conn, "reviews") == 0
