"""Review queries and decisions (spec §7.2, §7.3).

Every decision runs in its own transaction, so a reviewer who quits halfway
keeps what they decided.
"""

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

ITEM_COLUMNS = (
    "title", "type", "category", "effect", "lead_filer", "representative", "co_filers",
    "board_recommendation", "recommendation_quote", "vote_base", "vote_threshold_pct",
    "abstentions_count_against", "vote_standard_text", "vote_standard_quote", "request",
    "status", "source_quote",
)  # fmt: skip
MEETING_COLUMNS = (
    "kind", "meeting_date", "meeting_time", "format", "record_date", "shares_outstanding",
    "date_quote",
)  # fmt: skip

# The latest version of each text kind per item.
_LATEST_TEXTS = """
    select distinct on (t.ballot_item_id, t.kind) t.*
    from item_texts t
    order by t.ballot_item_id, t.kind, t.version desc
"""
_KIND_ORDER = {"summary": 0, "proponent_argument": 1, "board_argument": 2}


@dataclass(frozen=True)
class QueuedFiling:
    """A filing waiting for review.

    Attributes:
        filing_id: The filing.
        accession: Its accession number.
        ticker: The company's ticker.
        stage: ``in_review``, ``checked``, or ``published``.
        pending_texts: Latest-version texts still pending.
    """

    filing_id: int
    accession: str
    ticker: str
    stage: str
    pending_texts: int


@dataclass(frozen=True)
class TextView:
    """The latest version of one item text."""

    id: int
    kind: str
    version: int
    body: str | None
    origin: str
    review_status: str


@dataclass(frozen=True)
class ItemView:
    """One ballot item as a reviewer sees it.

    Attributes:
        id: The item.
        number: The number as printed (``"2a"`` for a lettered vote).
        fields: The item's columns named in ``ITEM_COLUMNS``.
        nominees: Director nominees, in order.
        texts: The latest version of each text kind, summary first.
        published: True when its facts are public.
    """

    id: int
    number: str
    fields: dict[str, Any]
    nominees: list[str]
    texts: list[TextView]
    published: bool


@dataclass(frozen=True)
class FilingView:
    """A filing with everything a reviewer needs.

    Attributes:
        filing_id: The filing.
        accession: Its accession number.
        ticker: The company's ticker.
        stage: Its pipeline stage.
        source_url: The EDGAR document.
        meeting: The meeting's columns named in ``MEETING_COLUMNS``, or None.
        items: Its ballot items, in id order (the ballot's order).
        failed_checks: ``(check, detail)`` for each check the latest llm run failed.
    """

    filing_id: int
    accession: str
    ticker: str
    stage: str
    source_url: str
    meeting: dict[str, Any] | None
    items: list[ItemView]
    failed_checks: list[tuple[str, str]]


def queue(conn: psycopg.Connection) -> list[QueuedFiling]:
    """List filings waiting for review, oldest filing first.

    A filing waits when it is ``in_review``, or when it is ``checked`` or
    ``published`` and a latest-version text is still pending.

    Args:
        conn: Database connection.

    Returns:
        The queue, by ``filed_at`` and then id.
    """
    rows = conn.execute(
        f"""
        select f.id, f.accession, c.ticker, f.stage,
               count(t.id) filter (where t.review_status = 'pending') as pending
        from filings f
        join companies c on c.id = f.company_id
        left join meetings m on m.proxy_filing_id = f.id
        left join ballot_items bi on bi.meeting_id = m.id
        left join ({_LATEST_TEXTS}) t on t.ballot_item_id = bi.id
        where f.stage in ('in_review', 'checked', 'published')
        group by f.id, c.ticker
        having f.stage = 'in_review' or count(t.id) filter (where t.review_status = 'pending') > 0
        order by f.filed_at, f.id
        """
    ).fetchall()
    return [QueuedFiling(*row) for row in rows]


def load(conn: psycopg.Connection, accession: str) -> FilingView | None:
    """Load one filing for review.

    Args:
        conn: Database connection.
        accession: The filing's accession number.

    Returns:
        The filing, or None when no filing has that accession.
    """
    with conn.cursor(row_factory=dict_row) as cursor:
        filing = cursor.execute(
            "select f.id, f.accession, c.ticker, f.stage, f.source_url from filings f "
            "join companies c on c.id = f.company_id where f.accession = %s",
            (accession,),
        ).fetchone()
        if filing is None:
            return None
        meeting = cursor.execute(
            f"select id, {', '.join(MEETING_COLUMNS)} from meetings where proxy_filing_id = %s",
            (filing["id"],),
        ).fetchone()
        items = (
            cursor.execute(
                f"select id, number, published_at, {', '.join(ITEM_COLUMNS)} "
                "from ballot_items where meeting_id = %s order by id",
                (meeting["id"],),
            ).fetchall()
            if meeting
            else []
        )
        failed = cursor.execute(
            """
            select cr.check_name, cr.detail from check_results cr
            where cr.extraction_run_id = (
                select max(id) from extraction_runs where filing_id = %s and kind = 'llm'
            ) and not cr.passed
            order by cr.id
            """,
            (filing["id"],),
        ).fetchall()
        views = [_item_view(cursor, row) for row in items]
    if meeting is not None:
        del meeting["id"]
    return FilingView(
        filing["id"],
        filing["accession"],
        filing["ticker"],
        filing["stage"],
        filing["source_url"],
        meeting,
        views,
        [(row["check_name"], row["detail"] or "") for row in failed],
    )


def _item_view(cursor: psycopg.Cursor[dict[str, Any]], row: dict[str, Any]) -> ItemView:
    nominees = cursor.execute(
        "select name from nominees where ballot_item_id = %s order by position", (row["id"],)
    ).fetchall()
    texts = cursor.execute(
        f"select * from ({_LATEST_TEXTS}) t where t.ballot_item_id = %s", (row["id"],)
    ).fetchall()
    return ItemView(
        row["id"],
        row["number"],
        {column: row[column] for column in ITEM_COLUMNS},
        [n["name"] for n in nominees],
        [
            TextView(t["id"], t["kind"], t["version"], t["body"], t["origin"], t["review_status"])
            for t in sorted(texts, key=lambda t: _KIND_ORDER[t["kind"]])
        ],
        row["published_at"] is not None,
    )
