"""Regenerate texts a reviewer rejected, with one text-only call per filing (spec §7.2).

Only the rejected texts get new versions; facts are never re-extracted, so a
rejection never creates a fact change. Texts marked ``needs_manual`` (two
rejections in a row) are left for a person. This stage spends money: run it
by hand, with ``obr review regenerate``.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anthropic
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from openboardroom.extract.llm import LlmSettings, Outcome, run_texts
from openboardroom.extract.spec import TEXTS_PROMPT_VERSION, validate_texts

_LABEL = re.compile(r"(\d+)([a-z]?)")
_FACTS = (
    "title", "type", "category", "effect", "lead_filer", "representative",
    "board_recommendation", "request",
)  # fmt: skip


@dataclass(frozen=True)
class RegenerateResult:
    """What one regenerate run did.

    Attributes:
        filings: Filings sent to the model.
        texts: New text versions written.
        failed: ``accession: why`` for each filing that produced no new texts.
    """

    filings: int
    texts: int
    failed: list[str]


@dataclass(frozen=True)
class _Rewrite:
    item_id: int
    kind: str


def _filings(conn: psycopg.Connection, limit: int | None) -> list[tuple[int, str]]:
    """Filings with a latest-version text at ``rejected``, oldest filing first."""
    return conn.execute(
        """
        select f.id, f.accession from filings f
        where exists (
            select 1
            from (
                select distinct on (t.ballot_item_id, t.kind) t.ballot_item_id, t.review_status
                from item_texts t
                order by t.ballot_item_id, t.kind, t.version desc
            ) latest
            join ballot_items bi on bi.id = latest.ballot_item_id
            join meetings m on m.id = bi.meeting_id
            where m.proxy_filing_id = f.id and latest.review_status = 'rejected'
        )
        order by f.filed_at, f.id
        limit %s
        """,
        (limit,),
    ).fetchall()


def _payload(
    conn: psycopg.Connection, filing_id: int
) -> tuple[list[dict[str, object]], dict[tuple[int, str], list[_Rewrite]]]:
    """Build the items to send, and map each ``(number, part)`` to the texts to rewrite."""
    with conn.cursor(row_factory=dict_row) as cursor:
        texts = cursor.execute(
            f"""
            select distinct on (t.ballot_item_id, t.kind)
                   bi.id as item_id, bi.number, t.kind, t.body, t.review_status,
                   {", ".join(f"bi.{column}" for column in _FACTS)},
                   (select r.reason from reviews r where r.item_text_id = t.id
                    and r.decision = 'reject' order by r.id desc limit 1) as reason
            from item_texts t
            join ballot_items bi on bi.id = t.ballot_item_id
            join meetings m on m.id = bi.meeting_id
            where m.proxy_filing_id = %s
            order by t.ballot_item_id, t.kind, t.version desc
            """,  # noqa: S608 - fixed column names
            (filing_id,),
        ).fetchall()
    items: dict[int, dict[str, Any]] = {}
    rewrites: dict[tuple[int, str], list[_Rewrite]] = {}
    for row in texts:
        if row["item_id"] not in items:
            match = _LABEL.fullmatch(row["number"])
            number, part = (int(match[1]), match[2]) if match else (row["number"], "")
            items[row["item_id"]] = {
                "number": number, "part": part,
                **{column: row[column] for column in _FACTS},
                "current": {}, "rewrite": [],
            }  # fmt: skip
        item = items[row["item_id"]]
        item["current"][row["kind"]] = row["body"]
        if row["review_status"] == "rejected":
            item["rewrite"].append({"text": row["kind"], "reason": row["reason"]})
            key = (item["number"], item["part"])
            rewrites.setdefault(key, []).append(_Rewrite(row["item_id"], row["kind"]))
    wanted = [item for item in items.values() if item["rewrite"]]
    return wanted, rewrites


def _problems(output: dict[str, object] | None, wanted: set[tuple[int, str]]) -> list[str]:
    errors = validate_texts(output)
    if errors:
        return errors
    returned = {(i["number"], i["part"]) for i in cast(list[dict[str, Any]], output["items"])}  # type: ignore[index]
    return [f"item {n}{p} missing" for n, p in sorted(wanted - returned)]


def _body(item: dict[str, Any], kind: str) -> str | None:
    if kind == "summary":
        text = item["summary"]
    else:
        discussion = cast(dict[str, object], item["discussion"] or {})
        text = discussion.get("proponent" if kind == "proponent_argument" else "board")
    return text.strip() or None if isinstance(text, str) else None


def _insert_run(
    conn: psycopg.Connection,
    filing_id: int,
    settings: LlmSettings,
    outcome: Outcome,
    status: str,
    detail: str | None,
    started: datetime,
) -> int:
    row = conn.execute(
        """
        insert into extraction_runs (
            filing_id, kind, model, effort, prompt_version, schema_version, input_tokens,
            output_tokens, output, status, started_at, finished_at, detail
        )
        values (%s, 'texts', %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
        returning id
        """,
        (
            filing_id,
            outcome.model or settings.model,
            settings.effort,
            TEXTS_PROMPT_VERSION,
            TEXTS_PROMPT_VERSION,
            outcome.input_tokens,
            outcome.output_tokens,
            None if outcome.output is None else Jsonb(outcome.output),
            status,
            started,
            detail,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def regenerate(
    conn: psycopg.Connection,
    client: anthropic.Anthropic,
    settings: LlmSettings,
    text_dir: Path,
    limit: int | None = None,
) -> RegenerateResult:
    """Regenerate rejected texts, one call per filing, oldest filing first.

    Each filing is written in its own transaction after its call returns. A
    call that fails, is refused, or returns output that fails the schema or
    leaves out an item is recorded as a ``failed`` or ``refused`` run, and the
    texts stay ``rejected`` for the next run.

    Args:
        conn: Database connection, in autocommit mode outside tests.
        client: An Anthropic client.
        settings: Model settings.
        text_dir: Directory of cached plain texts, ``<accession>.txt``.
        limit: At most this many filings.

    Returns:
        The counts and failures.
    """
    filings, written = 0, 0
    failed: list[str] = []
    for filing_id, accession in _filings(conn, limit):
        path = text_dir / f"{accession}.txt"
        if not path.is_file():
            failed.append(f"{accession}: text file missing")
            continue
        items, rewrites = _payload(conn, filing_id)
        started = datetime.now(UTC)
        outcome = run_texts(client, settings, path.read_text(encoding="utf-8"), items)
        filings += 1
        problems = (
            _problems(outcome.output, set(rewrites))
            if outcome.status == "succeeded"
            else [outcome.detail or outcome.status]
        )
        with conn.transaction():
            if problems:
                status = "refused" if outcome.status == "refused" else "failed"
                _insert_run(
                    conn, filing_id, settings, outcome, status, "; ".join(problems), started
                )
                failed.append(f"{accession}: {'; '.join(problems)}")
                continue
            run_id = _insert_run(conn, filing_id, settings, outcome, "succeeded", None, started)
            output = cast(dict[str, Any], outcome.output)
            for item in output["items"]:
                for rewrite in rewrites.get((item["number"], item["part"]), []):
                    conn.execute(
                        """
                        insert into item_texts (ballot_item_id, kind, version, body, origin,
                                                extraction_run_id, review_status)
                        select %s, %s, max(version) + 1, %s, 'model', %s, 'pending'
                        from item_texts where ballot_item_id = %s and kind = %s
                        """,
                        (rewrite.item_id, rewrite.kind, _body(item, rewrite.kind), run_id,
                         rewrite.item_id, rewrite.kind),
                    )  # fmt: skip
                    written += 1
    return RegenerateResult(filings, written, failed)
