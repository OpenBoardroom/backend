"""Plain-text rendering of a filing for review in a terminal."""

from urllib.parse import quote

from openboardroom.review.store import FilingView, ItemView

# Spec §8.4: the two arguments stay within a quarter of each other's length.
_UNEVEN_SHARE = 0.25
_FRAGMENT_WORDS = 4


def quote_link(source_url: str, text: str) -> str:
    """Link to a quote inside the EDGAR document with a URL text fragment.

    Browsers that support text fragments scroll to and highlight the quote.
    Long quotes are matched by their first and last four words.

    Args:
        source_url: The EDGAR document.
        text: The verbatim quote.

    Returns:
        The link, or ``source_url`` alone for a blank quote.
    """
    words = text.split()
    if not words:
        return source_url

    def encode(part: list[str]) -> str:
        return quote(" ".join(part), safe="")

    if len(words) <= 2 * _FRAGMENT_WORDS:
        fragment = encode(words)
    else:
        fragment = f"{encode(words[:_FRAGMENT_WORDS])},{encode(words[-_FRAGMENT_WORDS:])}"
    return f"{source_url}#:~:text={fragment}"


def _quote(label: str, text: str | None, source_url: str) -> list[str]:
    if not text:
        return [f"  {label}: (none)"]
    return [f'  {label}: "{text}"', f"    {quote_link(source_url, text)}"]


def _arguments(item: ItemView) -> list[str]:
    counts = {t.kind: len((t.body or "").split()) for t in item.texts if t.kind != "summary"}
    proponent, board = counts.get("proponent_argument", 0), counts.get("board_argument", 0)
    if not proponent and not board:
        return []
    low, high = sorted((proponent, board))
    # One side missing is the most uneven case of all (spec §8.4).
    flag = " ONE-SIDED" if not low else " UNEVEN" if high - low > _UNEVEN_SHARE * high else ""
    return [f"  arguments: proponent {proponent}, board {board} words{flag}"]


def item(view: ItemView, source_url: str) -> str:
    """Render one ballot item: facts, quotes with links, and its texts.

    Args:
        view: The item.
        source_url: The EDGAR document, for quote links.

    Returns:
        Lines of text joined by newlines.
    """
    f = view.fields
    lines = [
        f"Item {view.number} (id {view.id}): {f['title']}"
        + ("  [published]" if view.published else ""),
        f"  {f['type']} · {f['category']} · {f['effect']} · board: {f['board_recommendation']}"
        + ("" if f["status"] == "active" else f" · {f['status']}"),
    ]
    if f["type"] == "shareholder":
        filers = [f"lead filer: {f['lead_filer'] or '(none named)'}"]
        if f["representative"]:
            filers.append(f"representative: {f['representative']}")
        if f["co_filers"]:
            filers.append(f"co-filers: {'; '.join(f['co_filers'])}")
        lines += [f"  {' · '.join(filers)}", f"  request: {f['request'] or '(none)'}"]
    threshold = "" if f["vote_threshold_pct"] is None else f" {f['vote_threshold_pct']}%,"
    against = f["abstentions_count_against"]
    lines.append(
        f"  vote: {f['vote_base']},{threshold} abstentions count against: "
        f"{'not stated' if against is None else against}"
    )
    if f["vote_standard_text"]:
        lines.append(f"  vote standard as written: {f['vote_standard_text']}")
    if view.nominees:
        lines.append(f"  nominees: {'; '.join(view.nominees)}")
    lines += _quote("source", f["source_quote"], source_url)
    lines += _quote("recommendation", f["recommendation_quote"], source_url)
    lines += _quote("vote standard", f["vote_standard_quote"], source_url)
    for text in view.texts:
        lines.append(
            f"  [{text.id}] {text.kind} v{text.version} {text.origin} {text.review_status}: "
            f"{text.body or '(none)'}"
        )
    lines += _arguments(view)
    return "\n".join(lines)


def filing(view: FilingView) -> str:
    """Render a whole filing: header, failed checks, meeting, and every item.

    Args:
        view: The filing.

    Returns:
        Text ending in a newline.
    """
    lines = [f"{view.ticker} {view.accession} [{view.stage}]", view.source_url]
    lines += [f"FAILED {name}: {detail}" for name, detail in view.failed_checks]
    m = view.meeting
    if m is None:
        lines.append("no meeting")
    else:
        lines.append(
            f"{m['kind']} meeting {m['meeting_date']} {m['meeting_time']}, {m['format']}; "
            f"record date {m['record_date'] or 'not stated'}; "
            + (
                "shares not stated"
                if m["shares_outstanding"] is None
                else f"{m['shares_outstanding']} shares"
            )
        )
        lines += _quote("date", m["date_quote"], view.source_url)
    if not view.items:
        lines.append("no items")
    for each in view.items:
        lines += ["", item(each, view.source_url)]
    return "\n".join(lines) + "\n"
