"""Review queries and decisions (spec §7.2, §7.3).

Every decision runs in its own transaction, so a reviewer who quits halfway
keeps what they decided.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from openboardroom.extract.store import set_stage

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

FIXABLE_ITEM_FIELDS = (
    "title", "type", "category", "effect", "lead_filer", "representative", "co_filers",
    "board_recommendation", "vote_base", "vote_threshold_pct", "abstentions_count_against",
    "vote_standard_text", "request", "status",
)  # fmt: skip
FIXABLE_MEETING_FIELDS = (
    "kind", "meeting_date", "meeting_time", "format", "record_date", "shares_outstanding",
)  # fmt: skip

# The latest version of each text kind per item.
_LATEST_TEXTS = """
    select distinct on (t.ballot_item_id, t.kind) t.*
    from item_texts t
    order by t.ballot_item_id, t.kind, t.version desc
"""
_KIND_ORDER = {"summary": 0, "proponent_argument": 1, "board_argument": 2}
# Pending changes on the meeting of filing ``f`` (spec §7.1).
_PENDING_CHANGES = """
    select count(*) from pending_changes pc join meetings pm on pm.id = pc.meeting_id
    where pm.proxy_filing_id = f.id and pc.status = 'pending'
"""


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
        needs_manual: Latest-version texts rejected twice, waiting to be written by hand.
        pending_changes: Changes from supplements waiting for a decision.
    """

    filing_id: int
    accession: str
    ticker: str
    stage: str
    pending_texts: int
    needs_manual: int
    pending_changes: int = 0


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
class ChangeView:
    """A pending change to a meeting's ballot, from a supplement.

    Attributes:
        id: The pending change.
        item_id: The item it changes; None for an added item.
        item_number: That item's number as printed; None for an added item.
        field: ``status``, ``board_recommendation``, ``nominees``, an amended
            field, or ``item`` for an added item.
        proposed: The proposed value: a nominee list, a value, or the added item.
        source_quote: The supplement's words stating the change.
        supplement_accession: The supplement.
        supplement_url: Its EDGAR document, for quote links.
        status: ``pending``, ``applied``, or ``rejected``.
    """

    id: int
    item_id: int | None
    item_number: str | None
    field: str
    proposed: Any
    source_quote: str | None
    supplement_accession: str
    supplement_url: str
    status: str


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
        failed_checks: ``(check, detail)`` for each check the latest llm or
            supplement run failed.
        changes: Pending changes to the meeting's ballot, oldest first.
    """

    filing_id: int
    accession: str
    ticker: str
    stage: str
    source_url: str
    meeting: dict[str, Any] | None
    items: list[ItemView]
    failed_checks: list[tuple[str, str]]
    changes: list[ChangeView] = field(default_factory=lambda: [])


def queue(conn: psycopg.Connection) -> list[QueuedFiling]:
    """List filings waiting for review, oldest filing first.

    A filing waits when it is ``in_review``, or when it is ``checked`` or
    ``published`` and a latest-version text is pending or needs manual writing.

    Args:
        conn: Database connection.

    Returns:
        The queue, by ``filed_at`` and then id.
    """
    rows = conn.execute(
        f"""
        select f.id, f.accession, c.ticker, f.stage,
               count(t.id) filter (where t.review_status = 'pending') as pending,
               count(t.id) filter (where t.review_status = 'needs_manual') as manual,
               ({_PENDING_CHANGES}) as changes
        from filings f
        join companies c on c.id = f.company_id
        left join meetings m on m.proxy_filing_id = f.id
        left join ballot_items bi on bi.meeting_id = m.id
        left join ({_LATEST_TEXTS}) t on t.ballot_item_id = bi.id
        where f.stage in ('in_review', 'checked', 'published')
        group by f.id, c.ticker
        having f.stage = 'in_review'
            or count(t.id) filter (where t.review_status in ('pending', 'needs_manual')) > 0
            or ({_PENDING_CHANGES}) > 0
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
                select max(id) from extraction_runs
                where filing_id = %s and kind in ('llm', 'supplement')
            ) and not cr.passed
            order by cr.id
            """,
            (filing["id"],),
        ).fetchall()
        views = [_item_view(cursor, row) for row in items]
        changes = (
            cursor.execute(
                """
                select pc.id, pc.ballot_item_id, bi.number, pc.field, pc.proposed,
                       pc.source_quote, s.accession, s.source_url, pc.status
                from pending_changes pc
                left join ballot_items bi on bi.id = pc.ballot_item_id
                join extraction_runs r on r.id = pc.extraction_run_id
                join filings s on s.id = r.filing_id
                where pc.meeting_id = %s and pc.status = 'pending'
                order by pc.id
                """,
                (meeting["id"],),
            ).fetchall()
            if meeting
            else []
        )
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
        [ChangeView(*row.values()) for row in changes],
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
        review_id = _record(conn, text.filing_id, "approve", reviewer, item_id=text.item_id,
                            text_id=text_id)  # fmt: skip
        public = conn.execute(
            "select body from item_texts where ballot_item_id = %s and kind = %s "
            "and version < %s and review_status = 'approved' order by version desc limit 1",
            (text.item_id, text.kind, text.version),
        ).fetchone()
        if text.published and public is not None:
            # This version replaces a public text: that is a correction (spec §7.3).
            _correct(conn, text.item_id, text.kind, public[0], text.body, "approved a new version",
                     review_id)  # fmt: skip
        return review_id


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


def _parse(field: str, value: str) -> object:
    """Parse a reviewer's value for ``field``; ``""`` means null (an empty list for co-filers)."""
    value = value.strip()
    if field == "co_filers":
        return [name.strip() for name in value.split(";") if name.strip()]
    if not value:
        return None
    if field == "vote_threshold_pct":
        try:
            return Decimal(value)
        except InvalidOperation:
            raise ReviewError(f"{field}: {value!r} is not a number") from None
    if field == "abstentions_count_against":
        if value.lower() not in ("true", "false"):
            raise ReviewError(f"{field}: give true or false, not {value!r}")
        return value.lower() == "true"
    if field in ("meeting_date", "record_date"):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ReviewError(f"{field}: {value!r} is not a date (YYYY-MM-DD)") from None
    if field == "shares_outstanding":
        if not value.isdigit():
            raise ReviewError(f"{field}: {value!r} is not a whole number")
        return int(value)
    return value


def _json(value: object) -> object:
    """A column value as JSON: dates as ISO strings, decimals as numbers."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _update(conn: psycopg.Connection, table: str, field: str, value: object, row_id: int) -> None:
    query = sql.SQL("update {} set {} = %s where id = %s").format(
        sql.Identifier(table), sql.Identifier(field)
    )
    try:
        with conn.transaction():
            conn.execute(query, (value, row_id))
    except (psycopg.errors.IntegrityError, psycopg.errors.DataError):
        raise ReviewError(f"{field}: {value!r} is not allowed") from None


def fix_item(
    conn: psycopg.Connection, item_id: int, field: str, value: str, reviewer: str, reason: str
) -> int:
    """Change one fact of a ballot item.

    Args:
        conn: Database connection.
        item_id: The ballot item.
        field: One of ``FIXABLE_ITEM_FIELDS``.
        value: The new value as typed: ``""`` for null, ``;`` between co-filers,
            ``true`` or ``false`` for abstentions.
        reviewer: Who decided.
        reason: Why.

    Returns:
        The review id. A correction is also written when the item is public.

    Raises:
        ReviewError: For an unknown item or field, a value the column rejects,
            or a blank reason. Nothing is written.
    """
    if field not in FIXABLE_ITEM_FIELDS:
        raise ReviewError(f"{field} can't be fixed; fixable: {', '.join(FIXABLE_ITEM_FIELDS)}")
    reason, parsed = _required_text(reason, "reason"), _parse(field, value)
    with conn.transaction():
        row = conn.execute(
            f"select m.proxy_filing_id, bi.published_at is not null, bi.{field} "
            "from ballot_items bi join meetings m on m.id = bi.meeting_id where bi.id = %s "
            "for update of bi",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ReviewError(f"no item with id {item_id}")
        filing_id, published, old = row
        _update(conn, "ballot_items", field, parsed, item_id)
        before, after = _json(old), _json(parsed)
        review_id = _record(
            conn, filing_id, "fix", reviewer, item_id=item_id, reason=reason,
            before={"field": field, "value": before}, after={"field": field, "value": after},
        )  # fmt: skip
        if published:
            _correct(conn, item_id, field, before, after, reason, review_id)
        return review_id


def fix_meeting(
    conn: psycopg.Connection, accession: str, field: str, value: str, reviewer: str, reason: str
) -> int:
    """Change one fact of a filing's meeting.

    A public meeting fact belongs to every public item of the meeting, so a
    correction is written for each one, with field ``meeting.<field>``.

    Args:
        conn: Database connection.
        accession: The proxy filing.
        field: One of ``FIXABLE_MEETING_FIELDS``.
        value: The new value as typed: ``""`` for null, dates as ``YYYY-MM-DD``.
        reviewer: Who decided.
        reason: Why.

    Returns:
        The review id.

    Raises:
        ReviewError: For an unknown filing or field, a bad value, or a blank reason.
    """
    if field not in FIXABLE_MEETING_FIELDS:
        raise ReviewError(f"{field} can't be fixed; fixable: {', '.join(FIXABLE_MEETING_FIELDS)}")
    reason, parsed = _required_text(reason, "reason"), _parse(field, value)
    with conn.transaction():
        row = conn.execute(
            f"select m.id, f.id, m.{field} from meetings m "
            "join filings f on f.id = m.proxy_filing_id where f.accession = %s for update of m",
            (accession,),
        ).fetchone()
        if row is None:
            raise ReviewError(f"no meeting for filing {accession}")
        meeting_id, filing_id, old = row
        _update(conn, "meetings", field, parsed, meeting_id)
        before, after = _json(old), _json(parsed)
        review_id = _record(
            conn, filing_id, "fix", reviewer, reason=reason,
            before={"field": f"meeting.{field}", "value": before},
            after={"field": f"meeting.{field}", "value": after},
        )  # fmt: skip
        published = conn.execute(
            "select id from ballot_items where meeting_id = %s and published_at is not null "
            "order by id",
            (meeting_id,),
        ).fetchall()
        for (item_id,) in published:
            _correct(conn, item_id, f"meeting.{field}", before, after, reason, review_id)
        return review_id


def _filing(conn: psycopg.Connection, accession: str) -> tuple[int, str]:
    row = conn.execute(
        "select id, stage from filings where accession = %s for update", (accession,)
    ).fetchone()
    if row is None:
        raise ReviewError(f"no filing {accession}")
    return row[0], row[1]


def _form(conn: psycopg.Connection, filing_id: int) -> str:
    row = conn.execute("select form from filings where id = %s", (filing_id,)).fetchone()
    assert row is not None
    return row[0]


def _item_count(conn: psycopg.Connection, filing_id: int, published_only: bool = False) -> int:
    row = conn.execute(
        "select count(*) from ballot_items bi join meetings m on m.id = bi.meeting_id "
        "where m.proxy_filing_id = %s and (not %s or bi.published_at is not null)",
        (filing_id, published_only),
    ).fetchone()
    assert row is not None
    return row[0]


def clear(conn: psycopg.Connection, accession: str, reviewer: str, reason: str) -> int:
    """Clear a filing from review once its facts are right, moving it to ``checked``.

    Args:
        conn: Database connection.
        accession: The filing, at ``in_review``.
        reviewer: Who decided.
        reason: What the reviewer confirmed.

    Returns:
        The review id.

    Raises:
        ReviewError: For an unknown filing, one not in review, one with no
            items (requeue it instead), or a blank reason.
    """
    reason = _required_text(reason, "reason")
    with conn.transaction():
        filing_id, stage = _filing(conn, accession)
        if stage != "in_review":
            raise ReviewError(f"{accession} is {stage}, not in_review")
        # A supplement has no items of its own; a proxy without items needs requeueing.
        if _form(conn, filing_id) == "DEF 14A" and _item_count(conn, filing_id) == 0:
            raise ReviewError(f"{accession} has no items; requeue it to extract again")
        set_stage(conn, filing_id, "checked")
        return _record(conn, filing_id, "clear", reviewer, reason=reason)


_REQUEUE_STAGES = ("in_review", "checked", "out_of_scope")
# Child rows first; the 2026-11-24 reset deleted in this order.
_REQUEUE_DELETES = (
    "delete from item_texts where ballot_item_id in (select id from _items) "
    "or extraction_run_id in (select id from _runs)",
    "delete from nominees where ballot_item_id in (select id from _items)",
    "delete from ballot_items where id in (select id from _items)",
    "delete from meetings where proxy_filing_id = %(filing)s",
    "delete from check_results where extraction_run_id in (select id from _runs)",
    "delete from extraction_runs where id in (select id from _runs)",
)


def requeue(conn: psycopg.Connection, accession: str, reviewer: str, reason: str) -> int:
    """Send a filing back to extraction: delete its extraction rows and set it to ``fetched``.

    Reviews are kept; their links to deleted texts and items become null.

    Args:
        conn: Database connection.
        accession: The filing.
        reviewer: Who decided.
        reason: Why it must be extracted again.

    Returns:
        The review id.

    Raises:
        ReviewError: For an unknown filing, a blank reason, or a filing with a
            published item, vote results, or pending changes. Nothing is deleted.
    """
    reason = _required_text(reason, "reason")
    with conn.transaction():
        filing_id, stage = _filing(conn, accession)
        # A fetched filing may be waiting on a batch: requeueing it would bill it twice.
        if stage not in _REQUEUE_STAGES:
            raise ReviewError(
                f"{accession} is {stage}; only {', '.join(_REQUEUE_STAGES)} filings can be requeued"
            )
        if _item_count(conn, filing_id, published_only=True):
            raise ReviewError(f"{accession} has published items; it can't be requeued")
        params = {"filing": filing_id}
        conn.execute(
            "create temp table _items on commit drop as select bi.id from ballot_items bi "
            "join meetings m on m.id = bi.meeting_id where m.proxy_filing_id = %(filing)s",
            params,
        )
        conn.execute(
            "create temp table _runs on commit drop as "
            "select id from extraction_runs where filing_id = %(filing)s",
            params,
        )
        blockers = (
            # Results arrive after the meeting (Plan 4).
            ("select exists (select 1 from vote_results "
             "where ballot_item_id in (select id from _items))", "has vote results"),
            # Changes from supplements on this proxy's meeting, added items included.
            ("select exists (select 1 from pending_changes pc join meetings m "
             "on m.id = pc.meeting_id where m.proxy_filing_id = %(filing)s)",
             "has changes from supplements"),
            # A supplement whose runs recorded changes: re-reading it would orphan them.
            ("select exists (select 1 from pending_changes "
             "where extraction_run_id in (select id from _runs))",
             "recorded changes from supplements"),
        )  # fmt: skip
        for query, what in blockers:
            row = conn.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]
            if row is not None and row[0]:
                raise ReviewError(f"{accession} {what}; it can't be requeued")
        review_id = _record(conn, filing_id, "requeue", reviewer, reason=reason)
        for statement in _REQUEUE_DELETES:
            conn.execute(statement, params)  # pyright: ignore[reportArgumentType]
        conn.execute("drop table _items, _runs")
        set_stage(conn, filing_id, "fetched")
        return review_id


@dataclass(frozen=True)
class _Change:
    id: int
    meeting_id: int
    item_id: int | None
    field: str
    proposed: Any
    status: str
    run_id: int
    filing_id: int
    filing_stage: str
    source_quote: str | None
    supplement: str


def _change(conn: psycopg.Connection, change_id: int) -> _Change:
    row = conn.execute(
        """
        select pc.id, pc.meeting_id, pc.ballot_item_id, pc.field, pc.proposed, pc.status,
               pc.extraction_run_id, f.id, f.stage, pc.source_quote, s.accession
        from pending_changes pc
        join meetings m on m.id = pc.meeting_id
        join filings f on f.id = m.proxy_filing_id
        join extraction_runs r on r.id = pc.extraction_run_id
        join filings s on s.id = r.filing_id
        where pc.id = %s
        for update of pc
        """,
        (change_id,),
    ).fetchone()
    if row is None:
        raise ReviewError(f"no change with id {change_id}")
    change = _Change(*row)
    if change.status != "pending":
        raise ReviewError(f"change {change_id} is not pending ({change.status})")
    return change


def _decide(conn: psycopg.Connection, change: _Change, status: str, review_id: int) -> None:
    conn.execute(
        "update pending_changes set status = %s, decided_at = now(), review_id = %s where id = %s",
        (status, review_id, change.id),
    )


def _apply_nominees(conn: psycopg.Connection, item_id: int, names: list[str]) -> list[str]:
    before = [n for (n,) in conn.execute(
        "select name from nominees where ballot_item_id = %s order by position", (item_id,)
    ).fetchall()]  # fmt: skip
    conn.execute("delete from nominees where ballot_item_id = %s", (item_id,))
    with conn.cursor() as cursor:
        cursor.executemany(
            "insert into nominees (ballot_item_id, name, position) values (%s, %s, %s)",
            [(item_id, name, position) for position, name in enumerate(names, 1)],
        )
    return before


def _add_item(conn: psycopg.Connection, change: _Change) -> int:
    item: dict[str, Any] = change.proposed
    number = f"{item['number']}{item['part']}"
    taken = conn.execute(
        "select 1 from ballot_items where meeting_id = %s and number = %s",
        (change.meeting_id, number),
    ).fetchone()
    if taken:
        raise ReviewError(f"item {number} already exists; reject this change")
    columns = ("title", "type", "category", "effect", "board_recommendation", "lead_filer",
               "representative", "request")  # fmt: skip
    values = [item[c] if item[c] != "" else None for c in columns]
    row = conn.execute(
        """
        insert into ballot_items (meeting_id, number, title, type, category, effect,
                                  board_recommendation, lead_filer, representative, request,
                                  source_quote, extraction_run_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (change.meeting_id, number, *values, change.source_quote, change.run_id),
    ).fetchone()
    assert row is not None
    conn.execute(
        "insert into item_texts (ballot_item_id, kind, version, body, origin, extraction_run_id) "
        "values (%s, 'summary', 1, %s, 'model', %s)",
        (row[0], item["summary"].strip() or None, change.run_id),
    )
    if change.filing_stage == "published":
        # Publish only takes checked filings: this lets it publish the new item.
        set_stage(conn, change.filing_id, "checked")
    return row[0]


def apply_change(conn: psycopg.Connection, change_id: int, reviewer: str, reason: str) -> int:
    """Apply a pending change from a supplement.

    Nominee changes replace the item's nominees. An amended field goes through
    the same parsing and checks as ``fix_item``. An added item is created
    unpublished with a pending summary, and its filing returns to ``checked``
    so ``obr publish`` publishes it. A change to a public item writes a correction.

    Args:
        conn: Database connection.
        change_id: The pending change.
        reviewer: Who decided.
        reason: Why it is right.

    Returns:
        The review id.

    Raises:
        ReviewError: For an unknown or decided change, a value the item rejects,
            or a blank reason. Nothing is written.
    """
    reason = _required_text(reason, "reason")
    with conn.transaction():
        change = _change(conn, change_id)
        published = False
        if change.item_id is not None:
            row = conn.execute(
                "select published_at is not null from ballot_items where id = %s",
                (change.item_id,),
            ).fetchone()
            published = bool(row and row[0])
        if change.field == "item":
            item_id = _add_item(conn, change)
            before, after = None, change.proposed
        elif change.field == "nominees":
            item_id = change.item_id
            assert item_id is not None
            before = _apply_nominees(conn, item_id, change.proposed)
            after = change.proposed
        else:
            item_id = change.item_id
            assert item_id is not None
            old = conn.execute(
                sql.SQL("select {} from ballot_items where id = %s").format(
                    sql.Identifier(change.field)
                ),
                (item_id,),
            ).fetchone()
            assert old is not None
            proposed: object = change.proposed
            text = (
                "; ".join(str(name) for name in cast(list[object], proposed))
                if isinstance(proposed, list)
                else str(proposed)
            )
            parsed = _parse(change.field, text)
            _update(conn, "ballot_items", change.field, parsed, item_id)
            before, after = _json(old[0]), _json(parsed)
        review_id = _record(
            conn, change.filing_id, "apply_change", reviewer, item_id=item_id, reason=reason,
            before={"field": change.field, "value": before},
            after={"field": change.field, "value": after},
        )  # fmt: skip
        if published:
            # The public correction log names the supplement behind the change.
            _correct(conn, item_id, change.field, before, after,
                     f"supplement {change.supplement}: {reason}", review_id)  # fmt: skip
        _decide(conn, change, "applied", review_id)
        return review_id


def reject_change(conn: psycopg.Connection, change_id: int, reviewer: str, reason: str) -> int:
    """Reject a pending change from a supplement; nothing on the ballot changes.

    Args:
        conn: Database connection.
        change_id: The pending change.
        reviewer: Who decided.
        reason: Why it is wrong.

    Returns:
        The review id.

    Raises:
        ReviewError: For an unknown or decided change, or a blank reason.
    """
    reason = _required_text(reason, "reason")
    with conn.transaction():
        change = _change(conn, change_id)
        review_id = _record(
            conn, change.filing_id, "reject_change", reviewer, item_id=change.item_id,
            reason=reason, before={"field": change.field, "value": change.proposed},
        )  # fmt: skip
        _decide(conn, change, "rejected", review_id)
        return review_id
