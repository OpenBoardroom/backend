"""The interactive review walk behind `obr review next` (spec §7.2)."""

from collections.abc import Callable
from typing import TextIO

import psycopg

from openboardroom.review import render, store
from openboardroom.review.store import ItemView, ReviewError

ITEM_PROMPT = "[a]pprove item, approve [t]ext ID, [e]dit ID, [r]eject ID, [s]kip, [q]uit > "
FILING_PROMPT = "[c]lear, [r]equeue, [k]eep in review, [q]uit > "


class _Quit(Exception):
    """The reviewer quit, or input ended."""


class _Tally:
    """Decisions recorded so far; kept outside the call stack so quitting can't lose it."""

    def __init__(self) -> None:
        self.made = 0


def _pending(item: ItemView) -> set[int]:
    return {t.id for t in item.texts if t.review_status == "pending"}


def _open(item: ItemView) -> set[int]:
    """Texts that still need a decision: pending, or rejected twice and waiting to be written."""
    return {t.id for t in item.texts if t.review_status in ("pending", "needs_manual")}


def _reload(conn: psycopg.Connection, accession: str, item_id: int) -> ItemView | None:
    view = store.load(conn, accession)
    return next((i for i in view.items if i.id == item_id), None) if view else None


def _ask(ask: Callable[[str], str], prompt: str) -> str:
    try:
        return ask(prompt).strip()
    except EOFError:
        raise _Quit from None


def _read_body(ask: Callable[[str], str]) -> str:
    """Read a text of one or more lines, ended by a line holding only "."."""
    lines: list[str] = []
    while (line := _ask(ask, "text (end with a line holding only '.') > ")) != ".":
        lines.append(line)
    return "\n".join(lines)


def _edit(
    conn: psycopg.Connection, reviewer: str, ask: Callable[[str], str], out: TextIO, text_id: int
) -> bool:
    """Read a new text and a reason, show them, and save only on confirmation."""
    body = _read_body(ask)
    if not body.strip():
        out.write("an empty text can't be saved; approve a text that should stay empty\n")
        return False
    reason = _ask(ask, "reason > ")
    out.write(f"new text:\n{body}\n")
    if _ask(ask, "save? [y/n] > ").lower() != "y":
        out.write("discarded\n")
        return False
    store.edit_text(conn, text_id, body, reviewer, reason)
    return True


def _review_item(
    conn: psycopg.Connection,
    reviewer: str,
    ask: Callable[[str], str],
    out: TextIO,
    view: tuple[str, str],
    item: ItemView | None,
    tally: _Tally,
) -> None:
    """Take decisions on one item until none of its texts is open, or the reviewer skips.

    ``view`` is the filing's ``(accession, source_url)``. The item is shown
    again before every prompt, so its current state is always on screen.
    """
    accession, source_url = view
    while item is not None and _open(item):
        out.write("\n" + render.item(item, source_url) + "\n")
        choice, _, arg = _ask(ask, ITEM_PROMPT).partition(" ")
        try:
            if choice == "q":
                raise _Quit
            if choice == "s":
                return
            if choice == "a":
                tally.made += len(store.approve_item(conn, item.id, reviewer))
            elif choice in ("t", "e", "r"):
                text_id = int(arg) if arg.strip().isdigit() else -1
                allowed = _open(item) if choice == "e" else _pending(item)
                if text_id not in allowed:
                    out.write(f"{arg.strip() or '?'} is not a pending text of this item\n")
                    continue
                if choice == "t":
                    store.approve_text(conn, text_id, reviewer)
                    tally.made += 1
                elif choice == "e":
                    tally.made += _edit(conn, reviewer, ask, out, text_id)
                else:
                    store.reject_text(conn, text_id, reviewer, _ask(ask, "reason > "))
                    tally.made += 1
            else:
                out.write(f"unknown choice {choice!r}\n")
                continue
        except ReviewError as error:
            out.write(f"error: {error}\n")
        item = _reload(conn, accession, item.id)


def _decide_filing(
    conn: psycopg.Connection, reviewer: str, ask: Callable[[str], str], out: TextIO, accession: str
) -> int:
    """Offer clear, requeue, or keep for a filing in review. Fact fixes use `obr review fix`."""
    while True:
        choice = _ask(ask, FILING_PROMPT)
        try:
            if choice == "q":
                raise _Quit
            if choice == "k":
                return 0
            if choice == "c":
                store.clear(conn, accession, reviewer, _ask(ask, "reason > "))
                return 1
            if choice == "r":
                store.requeue(conn, accession, reviewer, _ask(ask, "reason > "))
                return 1
            out.write(f"unknown choice {choice!r}\n")
        except ReviewError as error:
            out.write(f"error: {error}\n")


def run(conn: psycopg.Connection, reviewer: str, ask: Callable[[str], str], out: TextIO) -> int:
    """Walk the review queue, oldest filing first, until it ends or the reviewer quits.

    Each decision commits on its own, so quitting keeps every decision made.

    Args:
        conn: Database connection, in autocommit mode outside tests.
        reviewer: Who is deciding.
        ask: Shows a prompt and returns the reviewer's answer; raises
            ``EOFError`` when input ends, which ends the session.
        out: Where the filing and messages are written.

    Returns:
        How many decisions were recorded.
    """
    tally = _Tally()
    try:
        for queued in store.queue(conn):
            view = store.load(conn, queued.accession)
            if view is None:
                continue
            out.write(render.filing(view))
            for item in view.items:
                _review_item(
                    conn, reviewer, ask, out, (view.accession, view.source_url), item, tally
                )
            if view.stage == "in_review":
                out.write(
                    f"{view.accession} is in review. Fix facts with "
                    "`obr review fix ITEM_ID FIELD VALUE --reason R` before clearing.\n"
                )
                tally.made += _decide_filing(conn, reviewer, ask, out, view.accession)
    except _Quit:
        pass
    return tally.made
