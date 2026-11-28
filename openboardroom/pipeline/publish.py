"""Publish stage: the facts of every checked filing go public (spec §7.1).

Facts publish on passing checks with no human step; texts publish only once
approved, which the published views enforce. A filing whose facts are public
is at stage ``published``; its texts may still be in review.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from openboardroom.companies import SubParsers
from openboardroom.config import database_url_from_env


@dataclass(frozen=True)
class PublishResult:
    """What one publish run did.

    Attributes:
        filings: Filings moved from ``checked`` to ``published``.
        items: Ballot items whose facts became public.
    """

    filings: int
    items: int


def publish(conn: psycopg.Connection, now: datetime) -> PublishResult:
    """Publish the facts of every ``checked`` filing, in one transaction.

    Args:
        conn: Database connection.
        now: The publication time recorded on each item.

    Returns:
        The counts. A second run finds nothing to publish.
    """
    with conn.transaction():
        filings = conn.execute(
            # Supplements are never published; only their changes are (spec §7.1).
            "update filings set stage = 'published', stage_updated_at = now() "
            "where stage = 'checked' and form = 'DEF 14A' returning id"
        ).fetchall()
        ids = [filing_id for (filing_id,) in filings]
        items = conn.execute(
            "update ballot_items bi set published_at = %s from meetings m "
            "where m.id = bi.meeting_id and m.proxy_filing_id = any(%s) "
            "and bi.published_at is null",
            (now, ids),
        )
    return PublishResult(len(ids), items.rowcount)


def _run(args: argparse.Namespace) -> int:
    del args
    with psycopg.connect(database_url_from_env()) as conn:
        result = publish(conn, datetime.now(UTC))
    print(f"publish: {result.filings} filings, {result.items} items")
    return 0


def register(subparsers: SubParsers) -> None:
    """Add the `publish` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser("publish", help="Publish the facts of checked filings.")
    parser.set_defaults(func=_run)
