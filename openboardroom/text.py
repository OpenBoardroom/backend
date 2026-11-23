"""Convert a filing's HTML to plain text.

Filings are untrusted input: they are only decoded and parsed, never executed.
"""

import codecs
import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# A charset is declared in the head; stop looking at <body> or after 64 KB.
_HEAD_LIMIT = 64 * 1024
_BODY = re.compile(rb"<body", re.IGNORECASE)
# Matches both <meta charset="x"> and <meta http-equiv=... content="...; charset=x">.
_META_CHARSET = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._:-]+)", re.IGNORECASE)
# Browsers read these labels as windows-1252 (WHATWG Encoding Standard), and
# filings that declare them often use its curly quotes in 0x80-0x9F.
_READ_AS_CP1252 = {"ascii", "iso8859-1"}
# A document whose ASCII <meta> tag was readable can't be UTF-16/32, so browsers
# treat these labels as UTF-8 (WHATWG); honoring them would garble the text.
_UTF_16_32 = re.compile(r"utf[-_]?(16|32)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
# Elements a browser lays out as their own block. Everything else is inline and
# joins its neighbours exactly as rendered, with no added space (spec §6.2).
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
})  # fmt: skip
# Marks a block boundary while converting. Not whitespace, so the whitespace
# collapse can't merge it away; filings never contain private-use characters
# that matter to the text.
_BLOCK_BREAK = "\ue001"
# Marks a printed page break, on a line of its own, until furniture is dropped.
_PAGE_BREAK = "\ue000"
_PAGE_BREAK_STYLE = re.compile(
    r"(page-break-(?P<old>before|after)\s*:\s*always)|(break-(?P<new>before|after)\s*:\s*page)",
    re.IGNORECASE,
)
# Running headers and footers sit in the first or last lines of a page and repeat
# exactly on most pages. Measured on the 2026 AAPL, XOM, GOOGL, and CSCO proxies:
# furniture repeats at the edges of 47-100% of pages, while repeated content
# ("Required Vote", a vote-standard sentence) stays at or below 23%.
_EDGE_LINES = 12
_MIN_PAGES = 3
_MIN_PAGE_SHARE = 0.4
_PAGE_NUMBER = re.compile(r"\d+|[ivxlc]+")


def _declared_charset(html: bytes) -> str | None:
    """Return the Python codec name of the charset the document declares, if known."""
    head = html[:_HEAD_LIMIT]
    body = _BODY.search(head)
    if body:
        head = head[: body.start()]
    for pattern in (_META_CHARSET, _XML_ENCODING):
        match = pattern.search(head)
        if match:
            try:
                name = codecs.lookup(match.group(1).decode("ascii")).name
            except LookupError:
                continue
            if _UTF_16_32.match(name):
                return None
            return "cp1252" if name in _READ_AS_CP1252 else name
    return None


def decode(html: bytes) -> str:
    """Decode a document by its declared charset, else UTF-8, else cp1252.

    A leading byte-order mark is dropped. A utf-16 or utf-32 declaration is
    ignored (see ``_UTF_16_32``). A declared charset the bytes don't fit is
    ignored. cp1252 is the last resort: its five undefined bytes become U+FFFD
    rather than raising.

    Args:
        html: The raw document.

    Returns:
        The decoded document.
    """
    for charset in (_declared_charset(html), "utf-8-sig"):
        if charset is None:
            continue
        try:
            return html.decode(charset).removeprefix("\ufeff")
        except UnicodeDecodeError:
            pass
    return html.decode("cp1252", errors="replace").removeprefix("\ufeff")


def _furniture_key(line: str) -> str:
    """Page numbers all share one key; any other line is its own key."""
    return "#" if _PAGE_NUMBER.fullmatch(line) else line


def _drop_furniture(lines: list[str]) -> list[str]:
    """Drop running headers and footers, and the page-break markers.

    A line is furniture when it is among the first or last ``_EDGE_LINES`` lines
    of a page and its key (``_furniture_key``) appears at the edges of at least
    ``_MIN_PAGES`` pages and ``_MIN_PAGE_SHARE`` of all pages. Lines away from
    page edges are never dropped, and a document without page breaks loses nothing.

    Args:
        lines: Text lines, with ``_PAGE_BREAK`` lines between pages.

    Returns:
        The lines without furniture or page-break markers.
    """
    pages: list[list[str]] = [[]]
    for line in lines:
        if line == _PAGE_BREAK:
            pages.append([])
        else:
            pages[-1].append(line)
    edges = [
        {*range(min(_EDGE_LINES, len(page))), *range(max(0, len(page) - _EDGE_LINES), len(page))}
        for page in pages
    ]
    pages_with: dict[str, int] = {}
    for page, edge in zip(pages, edges, strict=True):
        for key in {_furniture_key(page[i]) for i in edge}:
            pages_with[key] = pages_with.get(key, 0) + 1
    needed = max(_MIN_PAGES, _MIN_PAGE_SHARE * len(pages))
    furniture = {key for key, count in pages_with.items() if count >= needed}
    return [
        line
        for page, edge in zip(pages, edges, strict=True)
        for i, line in enumerate(page)
        if i not in edge or _furniture_key(line) not in furniture
    ]


def to_text(html: bytes) -> str:
    """Convert a filing's HTML to plain text, one block of text per line.

    Text is taken with BeautifulSoup's lxml parser. Each block element (see
    ``_BLOCK_TAGS``) starts a new line; inline text joins as a browser renders it,
    with no added space. Script and style contents and the inline-XBRL header are
    left out. Runs of whitespace, including source newlines, become one space; empty
    lines are dropped. Running page headers and footers are dropped
    (``_drop_furniture``), so a sentence split by a page break reads on.

    Args:
        html: The raw document.

    Returns:
        The lines joined by newlines, with a trailing newline, or ``""`` if the
        document has no text.
    """
    with warnings.catch_warnings():
        # Inline XBRL filings are XHTML behind an XML declaration; the HTML
        # parser is the intended one (same method as the extraction spike).
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(decode(html), "lxml")
    # The hidden inline-XBRL header holds context IDs and dates, not filing text.
    for header in soup.find_all("ix:header"):
        header.decompose()
    for element in soup.find_all(_BLOCK_TAGS):
        element.insert_before(_BLOCK_BREAK)
        element.insert_after(_BLOCK_BREAK)
    marker = _BLOCK_BREAK + _PAGE_BREAK + _BLOCK_BREAK
    for element in soup.find_all(style=_PAGE_BREAK_STYLE):
        match = _PAGE_BREAK_STYLE.search(str(element.get("style", "")))
        if match and (match.group("old") or match.group("new")) == "before":
            element.insert_before(marker)
        else:
            element.insert_after(marker)
    text = _WHITESPACE.sub(" ", soup.get_text(""))
    lines = [line.strip() for line in text.split(_BLOCK_BREAK)]
    kept = _drop_furniture([line for line in lines if line])
    return "\n".join(kept) + "\n" if kept else ""
