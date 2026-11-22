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

    Text is taken with BeautifulSoup's lxml parser, one newline between strings;
    script and style contents and the inline-XBRL header are left out. Within each line, runs of
    whitespace become one space; empty lines are dropped.

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
    text = soup.get_text("\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
    kept = [line for line in lines if line]
    return "\n".join(kept) + "\n" if kept else ""
