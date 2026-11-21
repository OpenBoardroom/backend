"""Discover stage: find new proxy and vote-result filings on EDGAR (spec §6.1)."""

import argparse
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, cast

import psycopg

from openboardroom.companies import SubParsers, covered_companies
from openboardroom.config import Settings
from openboardroom.edgar import EdgarClient, EdgarError, document_url

PROXY_FORMS = ("DEF 14A", "DEFA14A")
RESULTS_FORM = "8-K"
# Item 5.07 of Form 8-K: Submission of Matters to a Vote of Security Holders.
RESULTS_ITEM = "5.07"
# Filed by a successor issuer; a person links its CIK to the company (spec §4).
SUCCESSOR_FORM = "8-K12B"
# The parallel arrays read from `filings.recent` and from each paged file.
FIELDS = ("form", "filingDate", "accessionNumber", "primaryDocument", "items")


@dataclass(frozen=True)
class SuccessorNotice:
    """An 8-K12B filed under a covered company's CIK, for a person to review.

    Attributes:
        company_id: The covered company.
        cik: The CIK it was filed under.
        accession: Accession number.
        filed: EDGAR filing date.
    """

    company_id: int
    cik: str
    accession: str
    filed: date


@dataclass(frozen=True)
class DiscoverResult:
    """What a discover run found.

    Attributes:
        new: Filings inserted.
        existing: Filings found that were already stored.
        successor_notices: 8-K12B filings seen, which are reported, not stored.
    """

    new: int
    existing: int
    successor_notices: list[SuccessorNotice] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class _Row:
    form: str
    filed: date
    accession: str
    primary_document: str
    items: str


def _within(day: date, first: date, last: date | None) -> bool:
    return first <= day and (last is None or day <= last)


def _parse_date(raw: object, cik: str, source: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        raise EdgarError(f"CIK {cik} ({source}): unparseable date {raw!r}") from None


def _rows(table: Mapping[str, Any], cik: str, source: str) -> list[_Row]:
    """Zip EDGAR's parallel arrays into rows, refusing arrays that don't line up."""
    columns: dict[str, list[Any]] = {}
    for name in FIELDS:
        column = table.get(name)
        if not isinstance(column, list):
            raise EdgarError(f"CIK {cik} ({source}): missing array {name!r}")
        columns[name] = cast(list[Any], column)
    lengths = {name: len(column) for name, column in columns.items()}
    if len(set(lengths.values())) > 1:
        raise EdgarError(f"CIK {cik} ({source}): filing arrays differ in length {lengths}")
    return [
        _Row(
            form=str(form),
            filed=_parse_date(filed, cik, source),
            accession=str(accession),
            primary_document=str(primary_document),
            items=str(items or ""),
        )
        for form, filed, accession, primary_document, items in zip(
            *(columns[name] for name in FIELDS), strict=True
        )
    ]


def _object(value: object, cik: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EdgarError(f"CIK {cik}: submissions JSON has no {path!r} object")
    return cast(dict[str, Any], value)


def _submissions(client: EdgarClient, cik: str, start: date) -> list[_Row]:
    """Read a CIK's recent filings, plus any older pages that reach back to `start`."""
    filings = _object(client.submissions(cik).get("filings"), cik, "filings")
    rows = _rows(_object(filings.get("recent"), cik, "filings.recent"), cik, "filings.recent")
    oldest = min((row.filed for row in rows), default=None)
    if oldest is not None and start >= oldest:
        return rows
    for page in filings.get("files", []):
        info = _object(page, cik, "filings.files[]")
        name = str(info["name"])
        if _parse_date(info["filingTo"], cik, name) >= start:
            rows += _rows(client.submissions_page(name), cik, name)
    return rows


def _is_kept(row: _Row) -> bool:
    if row.form in PROXY_FORMS:
        return True
    items = {item.strip() for item in row.items.split(",")}
    return row.form == RESULTS_FORM and RESULTS_ITEM in items


def discover(
    conn: psycopg.Connection,
    client: EdgarClient,
    start: date,
    end: date,
    index: str = "sp100",
) -> DiscoverResult:
    """Record new filings of the companies covered during `start`..`end`.

    Every CIK range of each covered company is polled. A DEF 14A, DEFA14A, or
    8-K listing item 5.07 is kept only if its filing date is inside the window,
    inside one of the company's memberships (not just their merged span), and
    inside the range of the CIK it was filed under. Kept filings are inserted
    at stage `discovered` under that CIK; ones already stored are counted.
    8-K12B filings dated inside the window are returned as successor notices
    and never stored. The caller commits.

    Args:
        conn: Connection with write access.
        client: EDGAR client.
        start: First day of the window.
        end: Last day of the window.
        index: Index whose members are covered.

    Returns:
        Counts of new and already-stored filings, and any successor notices.

    Raises:
        EdgarError: If EDGAR fails or returns submissions that can't be read.
    """
    new = existing = 0
    notices: list[SuccessorNotice] = []
    for company in covered_companies(conn, index, start, end):
        for cik_range in company.ciks:
            for row in _submissions(client, cik_range.cik, start):
                if not _within(row.filed, start, end):
                    continue
                if row.form == SUCCESSOR_FORM:
                    # Only the window applies: a notice exists to fix CIK ranges
                    # and memberships, so it must not be hidden by them.
                    notices.append(
                        SuccessorNotice(company.company_id, cik_range.cik, row.accession, row.filed)
                    )
                    continue
                if not (
                    _is_kept(row)
                    and _within(row.filed, cik_range.valid_from, cik_range.valid_to)
                    and any(_within(row.filed, m.start, m.end) for m in company.memberships)
                ):
                    continue
                inserted = conn.execute(
                    """
                    insert into filings
                        (accession, company_id, cik, form, filed_at, source_url, primary_document)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (accession) do nothing
                    returning id
                    """,
                    (
                        row.accession,
                        company.company_id,
                        cik_range.cik,
                        row.form,
                        # EDGAR gives a date only; midnight UTC keeps filed_at::date
                        # equal to it whatever the session time zone.
                        datetime.combine(row.filed, time(), tzinfo=UTC),
                        document_url(cik_range.cik, row.accession, row.primary_document),
                        row.primary_document,
                    ),
                ).fetchone()
                if inserted is None:
                    existing += 1
                else:
                    new += 1
    return DiscoverResult(new=new, existing=existing, successor_notices=notices)


def _run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    try:
        with EdgarClient(settings) as client, psycopg.connect(settings.database_url) as conn:
            result = discover(conn, client, args.since, args.until)
    except EdgarError as exc:
        print(f"discover failed: {exc}", file=sys.stderr)
        return 1
    print(f"discover: {result.new} new, {result.existing} existing")
    for notice in result.successor_notices:
        print(
            f"successor notice: 8-K12B {notice.accession} filed {notice.filed} under CIK "
            f"{notice.cik} (company {notice.company_id}); link the successor's CIK in "
            "the company CIKs CSV"
        )
    return 0


def register(subparsers: SubParsers) -> None:
    """Add the `discover` command to the `obr` parser.

    Args:
        subparsers: The object returned by the top-level `add_subparsers`.
    """
    parser = subparsers.add_parser("discover", help="Find new filings on EDGAR.")
    parser.add_argument(
        "--since", type=date.fromisoformat, required=True, help="First day (YYYY-MM-DD)."
    )
    parser.add_argument(
        "--until", type=date.fromisoformat, required=True, help="Last day (YYYY-MM-DD)."
    )
    parser.set_defaults(func=_run)
