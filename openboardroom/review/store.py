"""Review queries and decisions (spec §7.2, §7.3).

Every decision runs in its own transaction, so a reviewer who quits halfway
keeps what they decided.
"""

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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


class ReviewError(Exception):
    """A decision that can't be made as asked; nothing was written."""


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


@dataclass(frozen=True)
class _Text:
    id: int
    item_id: int
    filing_id: int
    kind: str
    version: int
    body: str | None
    review_status: str
    latest: bool
    published: bool


def _text(conn: psycopg.Connection, text_id: int) -> _Text:
    row = conn.execute(
        """
        select t.id, t.ballot_item_id, m.proxy_filing_id, t.kind, t.version, t.body,
               t.review_status,
               t.version = (select max(version) from item_texts
                            where ballot_item_id = t.ballot_item_id and kind = t.kind),
               bi.published_at is not null
        from item_texts t
        join ballot_items bi on bi.id = t.ballot_item_id
        join meetings m on m.id = bi.meeting_id
        where t.id = %s
        for update of t
        """,
        (text_id,),
    ).fetchone()
    if row is None:
        raise ReviewError(f"no text with id {text_id}")
    return _Text(*row)


def _pending(text: _Text) -> None:
    if not text.latest:
        raise ReviewError(f"text {text.id} is not the latest version of its {text.kind}")
    if text.review_status != "pending":
        raise ReviewError(f"text {text.id} is not pending ({text.review_status})")


def _required_text(value: str, what: str) -> str:
    if not value.strip():
        raise ReviewError(f"a {what} is required")
    return value.strip()


def _record(
    conn: psycopg.Connection,
    filing_id: int,
    decision: str,
    reviewer: str,
    *,
    item_id: int | None = None,
    text_id: int | None = None,
    reason: str | None = None,
    before: object = None,
    after: object = None,
) -> int:
    row = conn.execute(
        """
        insert into reviews (filing_id, ballot_item_id, item_text_id, decision, reason,
                             reviewer, before, after)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            filing_id,
            item_id,
            text_id,
            decision,
            reason,
            reviewer,
            None if before is None else Jsonb(before),
            None if after is None else Jsonb(after),
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def _correct(
    conn: psycopg.Connection,
    item_id: int,
    field: str,
    before: object,
    after: object,
    reason: str,
    review_id: int,
) -> None:
    conn.execute(
        "insert into corrections (ballot_item_id, field, before, after, reason, review_id) "
        "values (%s, %s, %s, %s, %s, %s)",
        (item_id, field, Jsonb(before), Jsonb(after), reason, review_id),
    )


def approve_text(conn: psycopg.Connection, text_id: int, reviewer: str) -> int:
    """Approve the latest, pending version of a text.

    Args:
        conn: Database connection.
        text_id: The text.
        reviewer: Who decided.

    Returns:
        The review id.

    Raises:
        ReviewError: If there is no such text, or it isn't the latest pending version.
    """
    with conn.transaction():
        text = _text(conn, text_id)
        _pending(text)
        conn.execute("update item_texts set review_status = 'approved' where id = %s", (text_id,))
        return _record(conn, text.filing_id, "approve", reviewer, item_id=text.item_id,
                       text_id=text_id)  # fmt: skip


def approve_item(conn: psycopg.Connection, item_id: int, reviewer: str) -> list[int]:
    """Approve every pending latest-version text of an item, empty ones included.

    Args:
        conn: Database connection.
        item_id: The ballot item.
        reviewer: Who decided.

    Returns:
        One review id per text approved, in kind order (summary first).
    """
    rows = conn.execute(
        f"select id, kind from ({_LATEST_TEXTS}) t "
        "where t.ballot_item_id = %s and t.review_status = 'pending'",
        (item_id,),
    ).fetchall()
    ordered = sorted(rows, key=lambda row: _KIND_ORDER[row[1]])
    with conn.transaction():
        return [approve_text(conn, text_id, reviewer) for text_id, _ in ordered]


def edit_text(conn: psycopg.Connection, text_id: int, body: str, reviewer: str, reason: str) -> int:
    """Replace the latest version of a text with the reviewer's own, which is approved.

    The model's text is kept. A pending one becomes ``rejected``; an approved
    one stays approved as history. When the item is public and an approved
    text changes, a correction is recorded.

    Args:
        conn: Database connection.
        text_id: The latest version of the text.
        body: The new text.
        reviewer: Who decided.
        reason: Why it changed.

    Returns:
        The review id.

    Raises:
        ReviewError: If the text isn't the latest version, or the body or reason is blank.
    """
    body, reason = _required_text(body, "body"), _required_text(reason, "reason")
    with conn.transaction():
        text = _text(conn, text_id)
        if not text.latest:
            raise ReviewError(f"text {text.id} is not the latest version of its {text.kind}")
        if text.review_status != "approved":
            conn.execute(
                "update item_texts set review_status = 'rejected' where id = %s", (text_id,)
            )
        row = conn.execute(
            "insert into item_texts (ballot_item_id, kind, version, body, origin, review_status) "
            "values (%s, %s, %s, %s, 'reviewer', 'approved') returning id",
            (text.item_id, text.kind, text.version + 1, body),
        ).fetchone()
        assert row is not None
        review_id = _record(
            conn, text.filing_id, "edit", reviewer, item_id=text.item_id, text_id=row[0],
            reason=reason, before={"body": text.body}, after={"body": body},
        )  # fmt: skip
        if text.published and text.review_status == "approved":
            _correct(conn, text.item_id, text.kind, text.body, body, reason, review_id)
        return review_id


def reject_text(conn: psycopg.Connection, text_id: int, reviewer: str, reason: str) -> int:
    """Reject the latest, pending version of a text, so it is regenerated.

    A text whose previous version was also rejected becomes ``needs_manual``
    instead: two rejections in a row mean a person writes it (spec §7.2).

    Args:
        conn: Database connection.
        text_id: The text.
        reviewer: Who decided.
        reason: What is wrong; the regeneration call is given it.

    Returns:
        The review id.

    Raises:
        ReviewError: If the text isn't the latest pending version, or the reason is blank.
    """
    reason = _required_text(reason, "reason")
    with conn.transaction():
        text = _text(conn, text_id)
        _pending(text)
        previous = conn.execute(
            "select review_status from item_texts "
            "where ballot_item_id = %s and kind = %s and version = %s",
            (text.item_id, text.kind, text.version - 1),
        ).fetchone()
        status = "needs_manual" if previous == ("rejected",) else "rejected"
        conn.execute("update item_texts set review_status = %s where id = %s", (status, text_id))
        return _record(conn, text.filing_id, "reject", reviewer, item_id=text.item_id,
                       text_id=text_id, reason=reason)  # fmt: skip
