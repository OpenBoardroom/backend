"""Tests for the interactive review walk."""

import io
from collections.abc import Callable

import psycopg
from review_seed import seed_filing

from openboardroom.review import session


def _asker(answers: list[str]) -> Callable[[str], str]:
    remaining = list(answers)

    def ask(prompt: str) -> str:
        del prompt
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    return ask


def _statuses(conn: psycopg.Connection, item_id: int) -> list[tuple[str, str]]:
    return conn.execute(
        "select kind, review_status from item_texts where ballot_item_id = %s order by kind",
        (item_id,),
    ).fetchall()


def test_session_approve_item_then_quit(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    out = io.StringIO()

    made = session.run(conn, "quinn", _asker(["a", "q"]), out)

    assert made == 3
    assert {s for _, s in _statuses(conn, seeded.item_ids[0])} == {"approved"}
    assert {s for _, s in _statuses(conn, seeded.item_ids[1])} == {"pending"}
    assert "AAPL 0000320193-26-000001" in out.getvalue()


def test_session_reject_asks_for_reason_then_approves_rest(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    proposal = seeded.item_ids[1]
    text_id = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'proponent_argument'",
        (proposal,),
    ).fetchone()
    assert text_id is not None

    made = session.run(
        conn, "quinn", _asker(["s", f"r {text_id[0]}", "drops a figure", "a"]), io.StringIO()
    )

    assert made == 3
    assert _statuses(conn, proposal) == [
        ("board_argument", "approved"),
        ("proponent_argument", "rejected"),
        ("summary", "approved"),
    ]


def test_session_edit_and_bad_input(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    out = io.StringIO()
    summary = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[0],),
    ).fetchone()
    assert summary is not None

    answers = ["x", "t 999999", f"e {summary[0]}", "Elect directors.", "shorter", "q"]
    made = session.run(conn, "quinn", _asker(answers), out)

    assert made == 1
    assert "unknown choice" in out.getvalue()
    assert "999999 is not a pending text of this item" in out.getvalue()
    body = conn.execute(
        "select body from item_texts where ballot_item_id = %s and kind = 'summary' "
        "order by version desc limit 1",
        (seeded.item_ids[0],),
    ).fetchone()
    assert body == ("Elect directors.",)


def test_session_review_error_is_shown_and_reprompted(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    summary = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[0],),
    ).fetchone()
    assert summary is not None
    out = io.StringIO()

    session.run(conn, "quinn", _asker([f"r {summary[0]}", " ", "q"]), out)

    assert "a reason is required" in out.getvalue()


def test_session_eof_ends_cleanly(conn: psycopg.Connection) -> None:
    seed_filing(conn)
    assert session.run(conn, "quinn", _asker([]), io.StringIO()) == 0


def test_session_in_review_filing_offers_clear(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn, stage="in_review")

    made = session.run(conn, "quinn", _asker(["a", "a", "c", "counted by hand"]), io.StringIO())

    assert made == 7
    assert conn.execute(
        "select stage from filings where id = %s", (seeded.filing_id,)
    ).fetchone() == ("checked",)


def test_session_in_review_requeue_and_keep(conn: psycopg.Connection) -> None:
    first = seed_filing(conn, stage="in_review", item_list=[], filed_at="2026-01-01")
    second = seed_filing(
        conn, "0000000002-26-000001", stage="in_review", cik="0000000002", item_list=[]
    )

    session.run(conn, "quinn", _asker(["r", "dropped items", "k"]), io.StringIO())

    stages = conn.execute(
        "select id, stage from filings where id in (%s, %s) order by id",
        (first.filing_id, second.filing_id),
    ).fetchall()
    assert stages == [(first.filing_id, "fetched"), (second.filing_id, "in_review")]


def test_session_filing_prompt_handles_errors_and_quit(conn: psycopg.Connection) -> None:
    seed_filing(conn, stage="in_review", item_list=[])
    out = io.StringIO()

    made = session.run(conn, "quinn", _asker(["x", "c", "looks fine", "q"]), out)

    assert made == 0
    assert "unknown choice 'x'" in out.getvalue()
    assert "has no items; requeue it" in out.getvalue()


def test_session_approves_one_text(conn: psycopg.Connection) -> None:
    seeded = seed_filing(conn)
    summary = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[0],),
    ).fetchone()
    assert summary is not None

    assert session.run(conn, "quinn", _asker([f"t {summary[0]}", "q"]), io.StringIO()) == 1
    assert ("summary", "approved") in _statuses(conn, seeded.item_ids[0])
