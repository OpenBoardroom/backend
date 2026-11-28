"""Tests for text review decisions and the corrections they write."""

from typing import Any

import psycopg
import pytest
from review_seed import seed_filing

from openboardroom.review import store
from openboardroom.review.store import ReviewError


def _text(conn: psycopg.Connection, item_id: int, kind: str = "summary") -> dict[str, Any]:
    cursor = conn.execute(
        "select id, version, body, origin, review_status from item_texts "
        "where ballot_item_id = %s and kind = %s order by version desc limit 1",
        (item_id, kind),
    )
    row = cursor.fetchone()
    assert row is not None and cursor.description is not None
    return dict(zip([c.name for c in cursor.description], row, strict=True))


def _review(conn: psycopg.Connection, review_id: int) -> dict[str, Any]:
    cursor = conn.execute(
        "select filing_id, item_text_id, ballot_item_id, decision, reason, reviewer, before, after "
        "from reviews where id = %s",
        (review_id,),
    )
    row = cursor.fetchone()
    assert row is not None and cursor.description is not None
    return dict(zip([c.name for c in cursor.description], row, strict=True))


def _publish(conn: psycopg.Connection, item_id: int) -> None:
    conn.execute("update ballot_items set published_at = now() where id = %s", (item_id,))


def test_approve_sets_status_and_records_review(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    text = _text(conn, seeded.item_ids[0])

    review_id = store.approve_text(conn, text["id"], "quinn")

    assert _text(conn, seeded.item_ids[0])["review_status"] == "approved"
    review = _review(conn, review_id)
    assert (review["decision"], review["reviewer"], review["item_text_id"]) == (
        "approve", "quinn", text["id"],
    )  # fmt: skip
    assert (review["filing_id"], review["ballot_item_id"]) == (
        seeded.filing_id, seeded.item_ids[0],
    )  # fmt: skip


def test_approve_item_approves_all_pending_including_empty(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    election = seeded.item_ids[0]

    review_ids = store.approve_item(conn, election, "quinn")

    assert len(review_ids) == 3
    statuses = conn.execute(
        "select kind, body, review_status from item_texts where ballot_item_id = %s order by kind",
        (election,),
    ).fetchall()
    assert statuses == [
        ("board_argument", None, "approved"),
        ("proponent_argument", None, "approved"),
        ("summary", "Elect two directors.", "approved"),
    ]


def test_edit_adds_reviewer_version_and_keeps_model_text(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    item_id = seeded.item_ids[1]
    model = _text(conn, item_id)

    review_id = store.edit_text(conn, model["id"], "A report on water use.", "quinn", "tighter")

    edited = _text(conn, item_id)
    assert (edited["version"], edited["origin"], edited["review_status"], edited["body"]) == (
        2, "reviewer", "approved", "A report on water use.",
    )  # fmt: skip
    kept = conn.execute(
        "select body, review_status from item_texts where id = %s", (model["id"],)
    ).fetchone()
    assert kept == ("A request for a report on water use.", "rejected")
    review = _review(conn, review_id)
    assert (review["decision"], review["reason"]) == ("edit", "tighter")
    assert review["before"] == {"body": "A request for a report on water use."}
    assert review["after"] == {"body": "A report on water use."}
    # The latest approved version is what published_item_texts shows
    # (test_only_latest_approved_text); a never-public text needs no correction.
    assert conn.execute("select count(*) from corrections").fetchone() == (0,)


def test_edit_of_approved_text_on_published_item_writes_correction(
    conn: psycopg.Connection,
) -> None:
    seeded = seed_filing(conn)
    item_id = seeded.item_ids[1]
    text = _text(conn, item_id)
    store.approve_text(conn, text["id"], "quinn")
    _publish(conn, item_id)

    review_id = store.edit_text(conn, text["id"], "Fixed.", "quinn", "typo")

    row = conn.execute(
        "select ballot_item_id, field, before, after, reason, review_id from corrections"
    ).fetchone()
    assert row == (
        item_id, "summary", "A request for a report on water use.", "Fixed.", "typo", review_id,
    )  # fmt: skip
    old = conn.execute(
        "select review_status from item_texts where id = %s", (text["id"],)
    ).fetchone()
    assert old == ("approved",)


@pytest.mark.parametrize("reason", ["", "  "])
def test_reject_requires_reason(conn: psycopg.Connection, reason: str) -> None:
    seeded = seed_filing(conn)
    text = _text(conn, seeded.item_ids[1])
    with pytest.raises(ReviewError, match="reason"):
        store.reject_text(conn, text["id"], "quinn", reason)
    assert _text(conn, seeded.item_ids[1])["review_status"] == "pending"


def test_reject_records_the_reason(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    text = _text(conn, seeded.item_ids[1], "proponent_argument")

    review_id = store.reject_text(conn, text["id"], "quinn", "drops the 2025 figure")

    assert _text(conn, seeded.item_ids[1], "proponent_argument")["review_status"] == "rejected"
    assert _review(conn, review_id)["reason"] == "drops the 2025 figure"


def test_second_rejection_in_a_row_needs_manual(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    item_id = seeded.item_ids[1]
    store.reject_text(conn, _text(conn, item_id)["id"], "quinn", "first")
    conn.execute(
        "insert into item_texts (ballot_item_id, kind, version, body, origin) "
        "values (%s, 'summary', 2, 'Again.', 'model')",
        (item_id,),
    )

    store.reject_text(conn, _text(conn, item_id)["id"], "quinn", "second")

    assert _text(conn, item_id)["review_status"] == "needs_manual"


def test_decision_on_non_pending_or_old_text_raises(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    item_id = seeded.item_ids[1]
    text = _text(conn, item_id)
    store.approve_text(conn, text["id"], "quinn")

    with pytest.raises(ReviewError, match="not pending"):
        store.approve_text(conn, text["id"], "quinn")
    with pytest.raises(ReviewError, match="not pending"):
        store.reject_text(conn, text["id"], "quinn", "late")
    store.edit_text(conn, text["id"], "New.", "quinn", "reword")
    with pytest.raises(ReviewError, match="latest version"):
        store.edit_text(conn, text["id"], "Newer.", "quinn", "again")
    with pytest.raises(ReviewError, match="latest version"):
        store.approve_text(conn, text["id"], "quinn")
    with pytest.raises(ReviewError, match="no text"):
        store.approve_text(conn, 999_999, "quinn")


def test_blank_edit_raises(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    text = _text(conn, seeded.item_ids[1])
    with pytest.raises(ReviewError, match="body"):
        store.edit_text(conn, text["id"], "  ", "quinn", "blank")
    with pytest.raises(ReviewError, match="reason"):
        store.edit_text(conn, text["id"], "Fine.", "quinn", "")


def test_approving_a_replacement_of_a_public_text_writes_correction(
    conn: psycopg.Connection,
) -> None:
    # Final review #1, defence in depth: any approval that replaces a public text is a correction.
    seeded = seed_filing(conn)
    item_id = seeded.item_ids[1]
    store.approve_text(conn, _text(conn, item_id)["id"], "quinn")
    _publish(conn, item_id)
    conn.execute(
        "insert into item_texts (ballot_item_id, kind, version, body, origin) "
        "values (%s, 'summary', 2, 'Replacement.', 'model')",
        (item_id,),
    )

    review_id = store.approve_text(conn, _text(conn, item_id)["id"], "quinn")

    assert conn.execute(
        "select field, before, after, reason, review_id from corrections"
    ).fetchall() == [
        ("summary", "A request for a report on water use.", "Replacement.",
         "approved a new version", review_id),
    ]  # fmt: skip
