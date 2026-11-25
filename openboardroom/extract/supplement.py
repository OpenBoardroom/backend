"""Proxy supplements (DEFA14A): linking, checks, and the ballot changes they state.

Spec §6.3 item 3 and §7.1 with its update of 2026-11-25. Supplements and model
output are untrusted: every change carries a quote that must appear in the
supplement, or nothing from that supplement is written.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from openboardroom.extract.checks import quote_problem
from openboardroom.extract.spec import validate_supplement


@dataclass(frozen=True)
class Supplement:
    """A fetched supplement waiting for its LLM pass.

    Attributes:
        filing_id: The DEFA14A filing.
        accession: Its accession number.
        meeting_id: The meeting it belongs to, or None when no proxy links
            (the stage sets it ``out_of_scope``).
    """

    filing_id: int
    accession: str
    meeting_id: int | None


# For each fetched supplement not waiting on a batch, the proxy it belongs to:
# the latest DEF 14A of the company filed on or before it that is still a
# candidate. A candidate's meeting is on or after the supplement (or it isn't
# extracted yet, so the date is unknown); out-of-scope proxies never are.
_LINKS = """
    select s.id, s.accession, p.proxy_id, p.meeting_id, p.items
    from filings s
    left join lateral (
        select f.id as proxy_id, m.id as meeting_id,
               (select count(*) from ballot_items bi where bi.meeting_id = m.id) as items
        from filings f
        left join meetings m on m.proxy_filing_id = f.id
        where f.company_id = s.company_id and f.form = 'DEF 14A'
          and f.filed_at <= s.filed_at and f.stage <> 'out_of_scope'
          and (m.id is null or m.meeting_date is null or m.meeting_date >= s.filed_at::date)
        order by f.filed_at desc, f.id desc
        limit 1
    ) p on true
    where s.form = 'DEFA14A' and s.stage = 'fetched'
      and (%(accession)s::text is null or s.accession = %(accession)s)
      and (%(submitted)s or not exists (
          select 1 from extraction_runs r where r.filing_id = s.id and r.status = 'submitted'
      ))
    order by s.id
"""


def pending_supplements(
    conn: psycopg.Connection, limit: int | None = None, accession: str | None = None
) -> list[Supplement]:
    """Return fetched supplements ready for their LLM pass, in id order.

    A supplement waits (is left out) while its proxy isn't extracted yet, or
    its meeting has no items. One with no linked proxy is returned with
    ``meeting_id`` None.

    Args:
        conn: Database connection.
        limit: At most this many supplements; all when None.
        accession: Only this filing, when given.

    Returns:
        The supplements to process.
    """
    ready: list[Supplement] = []
    for filing_id, number, proxy_id, meeting_id, items in conn.execute(
        _LINKS, {"accession": accession, "submitted": False}
    ).fetchall():
        if proxy_id is not None and (meeting_id is None or not items):
            continue
        ready.append(Supplement(filing_id, number, meeting_id))
        if limit is not None and len(ready) == limit:
            break
    return ready


def linked(conn: psycopg.Connection, accession: str) -> Supplement | None:
    """Link one fetched supplement, including one waiting on a batch, for collect.

    Args:
        conn: Database connection.
        accession: The supplement.

    Returns:
        The supplement as ``pending_supplements`` would return it, or None
        when it now waits (its proxy was requeued, or has no items).
    """
    row = conn.execute(_LINKS, {"accession": accession, "submitted": True}).fetchone()
    if row is None:
        return None
    filing_id, number, proxy_id, meeting_id, items = row
    if proxy_id is not None and (meeting_id is None or not items):
        return None
    return Supplement(filing_id, number, meeting_id)


def meeting_items(conn: psycopg.Connection, meeting_id: int) -> list[dict[str, object]]:
    """The meeting's current items, as the supplement prompt and checks see them.

    Args:
        conn: Database connection.
        meeting_id: The meeting.

    Returns:
        Per item, in ballot order: ``number`` as printed, ``title``, ``type``,
        ``board_recommendation``, ``status``, and ``nominees``.
    """
    rows = conn.execute(
        """
        select bi.number, bi.title, bi.type, bi.board_recommendation, bi.status,
               array(select n.name from nominees n where n.ballot_item_id = bi.id
                     order by n.position)
        from ballot_items bi where bi.meeting_id = %s order by bi.id
        """,
        (meeting_id,),
    ).fetchall()
    keys = ("number", "title", "type", "board_recommendation", "status", "nominees")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _required(change: dict[str, Any]) -> str | None:
    kind = change["type"]
    if kind == "change_recommendation" and change["recommendation"] is None:
        return "recommendation is required"
    if kind == "change_nominees" and not change["nominees"]:
        return "nominees are required"
    if kind == "amend_text" and (change["field"] is None or not change["value"].strip()):
        return "field and value are required"
    if kind == "add_item" and change["item"] is None:
        return "item is required"
    return None


def problems(output: object, text: str, items: list[dict[str, object]]) -> list[str]:
    """Everything that stops a supplement's changes from being written.

    Args:
        output: Parsed JSON from the model.
        text: Plain text of the supplement.
        items: The meeting's current items (``meeting_items``).

    Returns:
        Schema errors, or per change: a quote not in the supplement, an item
        the meeting doesn't have (or, for ``add_item``, already has), or a
        field the change type needs left empty. Empty when all may be written.
    """
    errors = validate_supplement(output)
    if errors:
        return errors
    existing = {str(item["number"]) for item in items}
    found: list[str] = []
    for index, change in enumerate(cast(dict[str, Any], output)["changes"]):
        label = f"change {index + 1} ({change['type']})"
        target = None if change["number"] is None else f"{change['number']}{change['part']}"
        if change["type"] == "add_item":
            if target is None:
                found.append(f"{label}: a number is required")
            elif target in existing:
                found.append(f"{label}: item {target} already exists")
        elif target not in existing:
            found.append(f"{label}: {target} is not an item of this meeting")
        missing = _required(change)
        if missing:
            found.append(f"{label}: {missing}")
        quote = quote_problem("source_quote", change["source_quote"], text)
        if quote:
            found.append(f"{label}: {quote}")
    return found


@dataclass(frozen=True)
class ChangeCounts:
    """What writing one supplement's changes did.

    Attributes:
        applied: Automatic changes recorded as applied (withdrawals and
            recommendation changes), including ones that were already true.
        pending: Changes waiting for a reviewer.
        skipped: Changes already recorded by an earlier run.
    """

    applied: int
    pending: int
    skipped: int


# Changes that apply without review when their quote verifies (spec §7.1).
_AUTOMATIC = {"withdraw": "status", "change_recommendation": "board_recommendation"}


def _target(change: dict[str, Any]) -> tuple[str, object]:
    """The pending change's field and proposed value."""
    kind = change["type"]
    if kind == "withdraw":
        return "status", "withdrawn"
    if kind == "change_recommendation":
        return "board_recommendation", change["recommendation"]
    if kind == "change_nominees":
        return "nominees", change["nominees"]
    if kind == "amend_text":
        return change["field"], change["value"]
    return "item", {"number": change["number"], "part": change["part"], **change["item"]}


def _recorded(
    conn: psycopg.Connection, meeting_id: int, item_id: int | None, field: str, proposed: object
) -> bool:
    row = conn.execute(
        """
        select exists (
            select 1 from pending_changes
            where meeting_id = %s and ballot_item_id is not distinct from %s and field = %s
              and proposed = %s and status in ('pending', 'applied')
        )
        """,
        (meeting_id, item_id, field, Jsonb(proposed)),
    ).fetchone()
    return bool(row and row[0])


def write_changes(
    conn: psycopg.Connection,
    supplement: Supplement,
    run_id: int,
    changes: list[dict[str, Any]],
    now: datetime,
) -> ChangeCounts:
    """Record a supplement's checked changes, applying the automatic ones.

    Runs inside the caller's transaction. Withdrawals and recommendation
    changes update the item at once; when the item is public, a correction
    with no review is written. Other changes wait for a reviewer. A change
    already recorded (pending or applied) by an earlier run is skipped, so a
    supplement processed twice changes nothing twice.

    Args:
        conn: Database connection.
        supplement: The supplement, linked to a meeting.
        run_id: Its supplement run.
        changes: Its changes, already passed by ``problems``.
        now: When automatic changes are recorded as decided.

    Returns:
        The counts.
    """
    assert supplement.meeting_id is not None
    rows = conn.execute(
        "select number, id, published_at is not null, status, board_recommendation "
        "from ballot_items where meeting_id = %s",
        (supplement.meeting_id,),
    ).fetchall()
    items = {row[0]: row[1:] for row in rows}
    applied = pending = skipped = 0
    for change in changes:
        field, proposed = _target(change)
        label = f"{change['number']}{change['part']}"
        item_id = None if change["type"] == "add_item" else items[label][0]
        if _recorded(conn, supplement.meeting_id, item_id, field, proposed):
            skipped += 1
            continue
        automatic = change["type"] in _AUTOMATIC
        if automatic:
            _, published, status, recommendation = items[label]
            current = status if field == "status" else recommendation
            if current != proposed:
                conn.execute(
                    sql.SQL("update ballot_items set {} = %s where id = %s").format(
                        sql.Identifier(field)
                    ),
                    (proposed, item_id),
                )
                if published:
                    conn.execute(
                        "insert into corrections (ballot_item_id, field, before, after, reason) "
                        "values (%s, %s, %s, %s, %s)",
                        (item_id, field, Jsonb(current), Jsonb(proposed),
                         f"supplement {supplement.accession}"),
                    )  # fmt: skip
            applied += 1
        else:
            pending += 1
        conn.execute(
            """
            insert into pending_changes (meeting_id, ballot_item_id, field, proposed,
                                         source_quote, extraction_run_id, reason, status,
                                         decided_at)
            values (%s, %s, %s, %s, %s, %s, 'supplement', %s, %s)
            """,
            (supplement.meeting_id, item_id, field, Jsonb(proposed), change["source_quote"],
             run_id, "applied" if automatic else "pending", now if automatic else None),
        )  # fmt: skip
    return ChangeCounts(applied, pending, skipped)
