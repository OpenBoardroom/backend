"""Proxy supplements (DEFA14A): linking, checks, and the ballot changes they state.

Spec §6.3 item 3 and §7.1 with its update of 2026-11-25. Supplements and model
output are untrusted: every change carries a quote that must appear in the
supplement, or nothing from that supplement is written.
"""

from dataclasses import dataclass

import psycopg


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
      and not exists (
          select 1 from extraction_runs r where r.filing_id = s.id and r.status = 'submitted'
      )
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
        _LINKS, {"accession": accession}
    ).fetchall():
        if proxy_id is not None and (meeting_id is None or not items):
            continue
        ready.append(Supplement(filing_id, number, meeting_id))
        if limit is not None and len(ready) == limit:
            break
    return ready


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
