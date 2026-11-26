"""Tests for reviewing pending changes from supplements (spec §7.1, update 2026-11-25)."""

import io
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from review_seed import add_supplement, seed_filing

from openboardroom.extract.checks import CheckResult
from openboardroom.extract.store import insert_check_results
from openboardroom.extract.supplement import Supplement, write_changes
from openboardroom.review import render, session, store
from openboardroom.review.store import ReviewError

PROXY = "0000320193-26-000001"
SUPP = "0000320193-26-000101"
QUOTE = "the board has nominated a new director in place of a nominee who withdrew"
NOW = datetime(2026, 11, 25, tzinfo=UTC)
ADDED = {
    "title": "Approval of Stock Split", "type": "management", "category": "charter_bylaw_amendment",
    "effect": "binding", "board_recommendation": "for", "lead_filer": "", "representative": "",
    "request": "", "summary": "Shareholders are asked to approve a stock split.",
}  # fmt: skip


def _change(**fields: Any) -> dict[str, Any]:
    change: dict[str, Any] = {
        "type": "change_nominees", "number": 1, "part": "", "recommendation": None,
        "nominees": ["Ann Lee", "Cy Diaz"], "field": None, "value": "", "item": None,
        "source_quote": QUOTE,
    }  # fmt: skip
    change.update(fields)
    return change


def _seed(
    conn: psycopg.Connection, *changes: dict[str, Any], stage: str = "published"
) -> list[int]:
    proxy = seed_filing(conn, PROXY, stage=stage)
    if stage == "published":
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
    write_changes(conn, Supplement(supp_id, SUPP, meeting[0]), run[0], list(changes), NOW)
    return [i for (i,) in conn.execute("select id from pending_changes order by id").fetchall()]


def _corrections(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute("select field, before, after, reason from corrections").fetchall()


def test_apply_nominee_change_on_published_election_corrects(conn: psycopg.Connection) -> None:
    (change_id,) = _seed(conn, _change())

    review_id = store.apply_change(conn, change_id, "quinn", "matches the supplement")

    nominees = conn.execute("select name from nominees order by position").fetchall()
    assert nominees == [("Ann Lee",), ("Cy Diaz",)]
    assert _corrections(conn) == [
        (
            "nominees",
            ["Ann Lee", "Bo Chen"],
            ["Ann Lee", "Cy Diaz"],
            f"supplement {SUPP}: matches the supplement",
        )  # fmt: skip
    ]
    row = conn.execute(
        "select status, review_id, decided_at is not null from pending_changes where id = %s",
        (change_id,),
    ).fetchone()
    assert row == ("applied", review_id, True)
    assert conn.execute("select decision from reviews where id = %s", (review_id,)).fetchone() == (
        "apply_change",
    )


def test_apply_amend_text_uses_fix_rules(conn: psycopg.Connection) -> None:
    good, bad = _seed(
        conn,
        _change(
            type="amend_text", number=2, nominees=[], field="co_filers", value="B Fund; C Trust"
        ),
        _change(type="amend_text", number=2, nominees=[], field="title", value="   x   "),
    )  # fmt: skip
    conn.execute(
        "update pending_changes set field = 'category', proposed = '\"merger\"' where id = %s",
        (bad,),
    )

    store.apply_change(conn, good, "quinn", "ok")
    with pytest.raises(ReviewError, match="not allowed"):
        store.apply_change(conn, bad, "quinn", "ok")

    row = conn.execute("select co_filers, category from ballot_items where number = '2'").fetchone()
    assert row == (["B Fund", "C Trust"], "shareholder_proposal")
    assert conn.execute("select status from pending_changes where id = %s", (bad,)).fetchone() == (
        "pending",
    )


def test_apply_added_item_creates_unpublished_item_in_queue(conn: psycopg.Connection) -> None:
    (change_id,) = _seed(
        conn, _change(type="add_item", number=2, part="b", nominees=[], item=ADDED)
    )

    store.apply_change(conn, change_id, "quinn", "new vote")

    row = conn.execute(
        "select number, title, category, published_at, status from ballot_items where number = '2b'"
    ).fetchone()
    assert row == ("2b", "Approval of Stock Split", "charter_bylaw_amendment", None, "active")
    texts = conn.execute(
        "select t.kind, t.body, t.review_status from item_texts t "
        "join ballot_items bi on bi.id = t.ballot_item_id where bi.number = '2b'"
    ).fetchall()
    assert texts == [("summary", "Shareholders are asked to approve a stock split.", "pending")]
    assert conn.execute("select stage from filings where accession = %s", (PROXY,)).fetchone() == (
        "checked",
    )
    assert PROXY in [q.accession for q in store.queue(conn)]


def test_reject_change_needs_reason(conn: psycopg.Connection) -> None:
    (change_id,) = _seed(conn, _change())
    with pytest.raises(ReviewError, match="reason"):
        store.reject_change(conn, change_id, "quinn", " ")

    store.reject_change(conn, change_id, "quinn", "the nominee list is unchanged")

    assert conn.execute("select status from pending_changes").fetchone() == ("rejected",)
    assert conn.execute("select name from nominees order by position").fetchall() == [
        ("Ann Lee",), ("Bo Chen",),
    ]  # fmt: skip
    assert _corrections(conn) == []


def test_decided_change_raises(conn: psycopg.Connection) -> None:
    (change_id,) = _seed(conn, _change())
    store.reject_change(conn, change_id, "quinn", "no")
    with pytest.raises(ReviewError, match="not pending"):
        store.apply_change(conn, change_id, "quinn", "yes")
    with pytest.raises(ReviewError, match="no change"):
        store.reject_change(conn, 999_999, "quinn", "no")


def test_queue_and_view_include_pending_changes(conn: psycopg.Connection) -> None:
    _seed(conn, _change())
    conn.execute("update item_texts set review_status = 'approved'")

    queued = store.queue(conn)
    view = store.load(conn, PROXY)

    assert [(q.accession, q.pending_changes) for q in queued] == [(PROXY, 1)]
    assert view is not None and len(view.changes) == 1
    change = view.changes[0]
    assert (change.item_number, change.field, change.proposed, change.supplement_accession) == (
        "1", "nominees", ["Ann Lee", "Cy Diaz"], SUPP,
    )  # fmt: skip


def test_render_shows_changes_with_supplement_link(conn: psycopg.Connection) -> None:
    _seed(conn, _change())
    view = store.load(conn, PROXY)
    assert view is not None

    text = render.filing(view)

    assert f"supplement {SUPP}" in text
    assert 'nominees → ["Ann Lee", "Cy Diaz"]' in text
    assert "https://www.sec.gov/Archives/s.htm#:~:text=the%20board%20has%20nominated" in text


def _asker(answers: list[str]) -> Callable[[str], str]:
    remaining = list(answers)

    def ask(prompt: str) -> str:
        del prompt
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    return ask


def test_session_applies_and_rejects_changes(conn: psycopg.Connection) -> None:
    first, second = _seed(
        conn,
        _change(),
        _change(type="amend_text", number=2, nominees=[], field="title", value="Water Report"),
    )  # fmt: skip
    conn.execute("update item_texts set review_status = 'approved'")

    made = session.run(
        conn, "quinn", _asker(["y", "confirmed", "n", "title is unchanged"]), io.StringIO()
    )

    assert made == 2
    statuses = conn.execute("select id, status from pending_changes order by id").fetchall()
    assert statuses == [(first, "applied"), (second, "rejected")]


def test_clear_accepts_supplement_without_items(conn: psycopg.Connection) -> None:
    _seed(conn)
    conn.execute("update filings set stage = 'in_review' where accession = %s", (SUPP,))

    store.clear(conn, SUPP, "quinn", "the quote was right after all")

    assert conn.execute("select stage from filings where accession = %s", (SUPP,)).fetchone() == (
        "checked",
    )


def test_load_supplement_in_review_shows_its_failed_checks(conn: psycopg.Connection) -> None:
    _seed(conn)
    run_id = conn.execute("select id from extraction_runs where kind = 'supplement'").fetchone()
    assert run_id is not None
    insert_check_results(conn, run_id[0], [CheckResult("quotes", False, "change 1: no quote")])

    view = store.load(conn, SUPP)

    assert view is not None and view.failed_checks == [("quotes", "change 1: no quote")]


def test_session_change_prompt_handles_skip_errors_and_quit(conn: psycopg.Connection) -> None:
    _seed(
        conn,
        _change(),
        _change(type="amend_text", number=2, nominees=[], field="title", value="Water Report"),
    )
    conn.execute("update item_texts set review_status = 'approved'")
    out = io.StringIO()

    made = session.run(conn, "quinn", _asker(["x", "y", " ", "s", "q"]), out)

    assert made == 0
    assert "unknown choice 'x'" in out.getvalue()
    assert "a reason is required" in out.getvalue()
    assert conn.execute("select distinct status from pending_changes").fetchall() == [("pending",)]


def test_requeue_refuses_proxy_with_added_item_change(conn: psycopg.Connection) -> None:
    # Final review I-2a: an added-item change names only the meeting.
    _seed(conn, _change(type="add_item", number=3, nominees=[], item=ADDED), stage="in_review")
    with pytest.raises(ReviewError, match="has changes from supplements"):
        store.requeue(conn, PROXY, "quinn", "re-extract")


def test_requeue_refuses_supplement_that_wrote_changes(conn: psycopg.Connection) -> None:
    # Final review I-2b: its runs back recorded changes.
    _seed(conn, _change())
    conn.execute("update filings set stage = 'checked' where accession = %s", (SUPP,))
    with pytest.raises(ReviewError, match="recorded changes"):
        store.requeue(conn, SUPP, "quinn", "read again")


def test_requeue_rereads_supplement_without_changes(conn: psycopg.Connection) -> None:
    _seed(conn)
    conn.execute("update filings set stage = 'in_review' where accession = %s", (SUPP,))
    store.requeue(conn, SUPP, "quinn", "quote typo; read again")
    assert conn.execute("select stage from filings where accession = %s", (SUPP,)).fetchone() == (
        "fetched",
    )


def test_apply_second_added_item_with_same_number_raises(conn: psycopg.Connection) -> None:
    # Final review I-3: two supplements may each propose item 3.
    first, second = _seed(
        conn,
        _change(type="add_item", number=3, nominees=[], item=ADDED),
        _change(type="add_item", number=3, nominees=[], item={**ADDED, "title": "Stock Split"}),
    )  # fmt: skip
    store.apply_change(conn, first, "quinn", "new vote")
    with pytest.raises(ReviewError, match="item 3 already exists"):
        store.apply_change(conn, second, "quinn", "same vote")


def test_applied_added_item_carries_its_source_quote(conn: psycopg.Connection) -> None:
    # Final review I-5: the item's facts trace to the supplement's words.
    (change_id,) = _seed(conn, _change(type="add_item", number=3, nominees=[], item=ADDED))
    store.apply_change(conn, change_id, "quinn", "new vote")
    assert conn.execute("select source_quote from ballot_items where number = '3'").fetchone() == (
        QUOTE,
    )


def test_apply_structured_vote_amendment_corrects(conn: psycopg.Connection) -> None:
    # Finding F25: UPS 2026 corrected Proposal 3 to shares present and entitled to vote.
    (change_id,) = _seed(
        conn,
        _change(
            type="amend_text", number=2, nominees=[], field="vote_base", value="shares_present"
        ),
    )

    store.apply_change(conn, change_id, "quinn", "UPS supplement")

    assert conn.execute("select vote_base from ballot_items where number = '2'").fetchone() == (
        "shares_present",
    )
    assert _corrections(conn)[0][:3] == ("vote_base", None, "shares_present")
