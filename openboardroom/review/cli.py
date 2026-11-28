"""The `obr review` command (spec §7.2)."""

import argparse
import sys
from collections.abc import Callable

import anthropic
import psycopg

from openboardroom.companies import SubParsers
from openboardroom.config import (
    ConfigError,
    cache_dir_from_env,
    extract_settings_from_env,
    review_settings_from_env,
)
from openboardroom.pipeline import regenerate as regenerate_stage
from openboardroom.review import render, session, store
from openboardroom.review.store import ReviewError

Action = Callable[[psycopg.Connection, argparse.Namespace, str], int]


def _queue(conn: psycopg.Connection, args: argparse.Namespace, reviewer: str) -> int:
    del args, reviewer
    queued = store.queue(conn)
    for q in queued:
        manual = f", {q.needs_manual} to write by hand" if q.needs_manual else ""
        print(f"{q.accession}  {q.ticker:6} {q.stage:9} {q.pending_texts:3} pending{manual}")
    print(f"{len(queued)} filings in the queue")
    return 0


def _show(conn: psycopg.Connection, args: argparse.Namespace, reviewer: str) -> int:
    del reviewer
    view = store.load(conn, args.accession)
    if view is None:
        raise ReviewError(f"no filing {args.accession}")
    print(render.filing(view), end="")
    return 0


def _next(conn: psycopg.Connection, args: argparse.Namespace, reviewer: str) -> int:
    del args
    made = session.run(conn, reviewer, input, sys.stdout)
    print(f"\n{made} decisions recorded")
    return 0


def _regenerate(conn: psycopg.Connection, args: argparse.Namespace, reviewer: str) -> int:
    del reviewer
    # The Anthropic SDK reads ANTHROPIC_API_KEY; this costs money (one call per filing).
    result = regenerate_stage.regenerate(
        conn,
        anthropic.Anthropic(),
        extract_settings_from_env(),
        cache_dir_from_env() / "text",
        limit=args.limit,
    )
    for failure in result.failed:
        print(f"regenerate failed: {failure}", file=sys.stderr)
    print(
        f"regenerate: {result.filings} filings, {result.texts} texts, {len(result.failed)} failed"
    )
    return 1 if result.failed else 0


def _recorded(review_id: int | list[int]) -> int:
    ids = review_id if isinstance(review_id, list) else [review_id]
    print(f"recorded review {', '.join(map(str, ids)) or '(nothing pending)'}")
    return 0


_DECISIONS: dict[str, Action] = {
    "approve": lambda c, a, r: _recorded(store.approve_text(c, a.text_id, r)),
    "approve-item": lambda c, a, r: _recorded(store.approve_item(c, a.item_id, r)),
    "edit": lambda c, a, r: _recorded(store.edit_text(c, a.text_id, a.body, r, a.reason)),
    "reject": lambda c, a, r: _recorded(store.reject_text(c, a.text_id, r, a.reason)),
    "fix": lambda c, a, r: _recorded(store.fix_item(c, a.item_id, a.field, a.value, r, a.reason)),
    "fix-meeting": lambda c, a, r: _recorded(
        store.fix_meeting(c, a.accession, a.field, a.value, r, a.reason)
    ),
    "clear": lambda c, a, r: _recorded(store.clear(c, a.accession, r, a.reason)),
    "requeue": lambda c, a, r: _recorded(store.requeue(c, a.accession, r, a.reason)),
    "apply-change": lambda c, a, r: _recorded(store.apply_change(c, a.change_id, r, a.reason)),
    "reject-change": lambda c, a, r: _recorded(store.reject_change(c, a.change_id, r, a.reason)),
}


def _runner(action: Action) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            settings = review_settings_from_env()
            # Autocommit: each decision's transaction commits on its own, so
            # quitting a session keeps every decision already made.
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                return action(conn, args, settings.reviewer)
        except (ConfigError, ReviewError) as error:
            print(f"review: {error}", file=sys.stderr)
            return 1

    return run


def register(subparsers: SubParsers) -> None:
    """Add the `review` command and its subcommands to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser("review", help="Review extracted filings (needs OBR_REVIEWER).")
    commands = parser.add_subparsers(dest="review_command", metavar="SUBCOMMAND", required=True)

    def add(name: str, help_text: str, action: Action) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(func=_runner(action))
        return sub

    add("queue", "List filings waiting for review, oldest first.", _queue)
    add("next", "Walk the queue interactively.", _next)
    add("show", "Show one filing.", _show).add_argument("accession")
    add("approve", "Approve a pending text.", _DECISIONS["approve"]).add_argument(
        "text_id", type=int
    )
    add(
        "approve-item", "Approve all of an item's pending texts.", _DECISIONS["approve-item"]
    ).add_argument("item_id", type=int)
    edit = add("edit", "Replace a text with your own; it is approved.", _DECISIONS["edit"])
    edit.add_argument("text_id", type=int)
    edit.add_argument("--body", required=True)
    reject = add("reject", "Reject a text so it is regenerated.", _DECISIONS["reject"])
    reject.add_argument("text_id", type=int)
    fix = add("fix", "Change one fact of an item.", _DECISIONS["fix"])
    fix.add_argument("item_id", type=int)
    fix_meeting = add("fix-meeting", "Change one fact of a meeting.", _DECISIONS["fix-meeting"])
    fix_meeting.add_argument("accession")
    for sub in (fix, fix_meeting):
        sub.add_argument("field")
        sub.add_argument("value", help='"" for none; ";" between co-filers')
    clear = add("clear", "Move a filing out of review to checked.", _DECISIONS["clear"])
    requeue = add("requeue", "Send a filing back to extraction.", _DECISIONS["requeue"])
    for sub in (clear, requeue):
        sub.add_argument("accession")
    regenerate = add("regenerate", "Rewrite rejected texts with Claude (costs money).", _regenerate)
    regenerate.add_argument("--limit", type=int, help="At most this many filings.")
    apply_change = add(
        "apply-change", "Apply a pending change from a supplement.", _DECISIONS["apply-change"]
    )
    reject_change = add(
        "reject-change", "Reject a pending change from a supplement.", _DECISIONS["reject-change"]
    )
    for sub in (apply_change, reject_change):
        sub.add_argument("change_id", type=int)
    for sub in (edit, reject, fix, fix_meeting, clear, requeue, apply_change, reject_change):
        sub.add_argument("--reason", required=True)
