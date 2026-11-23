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


def to_text(html: bytes) -> str:
    """Convert a filing's HTML to plain text, one block of text per line.

    Text is taken with BeautifulSoup's lxml parser. Each block element (see
    ``_BLOCK_TAGS``) starts a new line; inline text joins as a browser renders it,
    with no added space. Script and style contents and the inline-XBRL header are
    left out. Runs of whitespace, including source newlines, become one space; empty
    lines are dropped.

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
    text = _WHITESPACE.sub(" ", soup.get_text(""))
    lines = [line.strip() for line in text.split(_BLOCK_BREAK)]
    kept = [line for line in lines if line]
    return "\n".join(kept) + "\n" if kept else ""
