"""Tests for writing a supplement's changes: automatic ones applied, the rest pending."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from review_seed import add_supplement, seed_filing

from openboardroom.extract.supplement import ChangeCounts, Supplement, write_changes

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SUPP = "0000320193-26-000101"
QUOTE = "the proponent withdrew its proposal and it will not be presented at the meeting"


def _setup(
    conn: psycopg.Connection, *, published: bool = False
) -> tuple[Supplement, int, list[int]]:
    proxy = seed_filing(conn, stage="published" if published else "checked")
    if published:
        conn.execute("update ballot_items set published_at = now()")
    supp_id = add_supplement(conn, SUPP, "2026-01-20")
    meeting = conn.execute(
        "select id from meetings where proxy_filing_id = %s", (proxy.filing_id,)
    ).fetchone()
    run = conn.execute(
        "insert into extraction_runs (filing_id, kind, status) "
        "values (%s, 'supplement', 'succeeded') returning id",
        (supp_id,),
    ).fetchone()
    assert meeting is not None and run is not None
    return Supplement(supp_id, SUPP, meeting[0]), run[0], proxy.item_ids


def _change(**fields: Any) -> dict[str, Any]:
    change: dict[str, Any] = {
        "type": "withdraw", "number": 2, "part": "", "recommendation": None, "nominees": [],
        "field": None, "value": "", "item": None, "source_quote": QUOTE,
    }  # fmt: skip
    change.update(fields)
    return change


def _item(conn: psycopg.Connection, item_id: int) -> tuple[str, str]:
    row = conn.execute(
        "select status, board_recommendation from ballot_items where id = %s", (item_id,)
    ).fetchone()
    assert row is not None
    return row


def _changes(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "select ballot_item_id, field, proposed, status, reason, decided_at is not null "
        "from pending_changes order by id"
    ).fetchall()


def _corrections(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "select ballot_item_id, field, before, after, reason, review_id from corrections"
    ).fetchall()


def test_withdraw_applies_and_corrects_published_item(conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(conn, published=True)

    counts = write_changes(conn, supplement, run_id, [_change()], NOW)

    assert counts == ChangeCounts(applied=1, pending=0, skipped=0)
    assert _item(conn, items[1])[0] == "withdrawn"
    assert _changes(conn) == [(items[1], "status", "withdrawn", "applied", "supplement", True)]
    assert _corrections(conn) == [
        (items[1], "status", "active", "withdrawn", f"supplement {SUPP}", None)
    ]


def test_withdraw_of_unpublished_item_writes_no_correction(conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(conn)

    write_changes(conn, supplement, run_id, [_change()], NOW)

    assert _item(conn, items[1])[0] == "withdrawn"
    assert _corrections(conn) == []


def test_recommendation_change_applies(conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(conn, published=True)
    change = _change(type="change_recommendation", recommendation="none")

    write_changes(conn, supplement, run_id, [change], NOW)

    assert _item(conn, items[1])[1] == "none"
    assert _corrections(conn)[0][1:4] == ("board_recommendation", "against", "none")


def test_already_true_change_is_recorded_without_update(conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(conn, published=True)
    change = _change(type="change_recommendation", recommendation="against")
    assert _item(conn, items[1])[1] == "against"

    counts = write_changes(conn, supplement, run_id, [change], NOW)

    assert counts == ChangeCounts(applied=1, pending=0, skipped=0)
    assert _changes(conn)[0][3] == "applied"
    assert _corrections(conn) == []


def test_review_types_become_pending(conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(conn)
    added = {
        "title": "New Item", "type": "management", "category": "other", "effect": "binding",
        "board_recommendation": "for", "lead_filer": "", "representative": "", "request": "",
        "summary": "Shareholders are asked to approve a new item.",
    }  # fmt: skip
    changes = [
        _change(type="change_nominees", number=1, nominees=["Ann Lee", "Cy Diaz"]),
        _change(type="amend_text", field="request", value="a report on water risk"),
        _change(type="add_item", number=3, item=added),
    ]

    counts = write_changes(conn, supplement, run_id, changes, NOW)

    assert counts == ChangeCounts(applied=0, pending=3, skipped=0)
    rows = _changes(conn)
    assert rows[0][:4] == (items[0], "nominees", ["Ann Lee", "Cy Diaz"], "pending")
    assert rows[1][:4] == (items[1], "request", "a report on water risk", "pending")
    assert rows[2][:2] == (None, "item")
    assert rows[2][2] == {"number": 3, "part": "", **added}
    meeting = conn.execute("select distinct meeting_id from pending_changes").fetchall()
    assert meeting == [(supplement.meeting_id,)]
    assert _item(conn, items[1]) == ("active", "against")


def test_rerun_skips_recorded_changes(conn: psycopg.Connection) -> None:
    supplement, run_id, _ = _setup(conn, published=True)
    changes = [_change(), _change(type="amend_text", field="title", value="Water Report")]

    first = write_changes(conn, supplement, run_id, changes, NOW)
    second = write_changes(conn, supplement, run_id, changes, NOW)

    assert first == ChangeCounts(applied=1, pending=1, skipped=0)
    assert second == ChangeCounts(applied=0, pending=0, skipped=2)
    assert len(_corrections(conn)) == 1
    assert len(_changes(conn)) == 2


@pytest.fixture
def admin_conn(pg_url: str) -> Iterator[psycopg.Connection]:
    """A superuser transaction, rolled back after the test, so it can switch to ``web_ro``."""
    connection = psycopg.connect(pg_url)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_published_view_flags_pending_change(admin_conn: psycopg.Connection) -> None:
    supplement, run_id, items = _setup(admin_conn, published=True)
    write_changes(
        admin_conn, supplement, run_id,
        [_change(type="amend_text", field="title", value="Water Report")], NOW,
    )  # fmt: skip

    admin_conn.execute("set local role web_ro")

    flags = admin_conn.execute(
        "select id, title, has_pending_change from published_ballot_items order by id"
    ).fetchall()
    assert flags == [
        (items[0], "Election of Directors", False),
        (items[1], "Report on Water Use", True),
    ]
