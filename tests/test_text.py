"""Tests for filing HTML to plain text.

``aapl-2024-def14a.htm`` is the primary document of Apple's 2024 DEF 14A
(accession 0001308179-24-000010), cut from the full submission downloaded from
EDGAR: the cover pages, table of contents, and meeting notice, then the pages
of Proposals 1, 4, and 8, joined at page breaks. ``expected.txt`` was produced
once by ``to_text`` and checked by eye. ``cp1252.htm`` is synthetic.
"""

import re
import warnings
from pathlib import Path

from openboardroom.text import decode, to_text

FILINGS = Path(__file__).resolve().parent / "fixtures" / "filings"


def test_golden_apple_2024() -> None:
    html = (FILINGS / "aapl-2024-def14a.htm").read_bytes()
    expected = (FILINGS / "aapl-2024-def14a.expected.txt").read_text(encoding="utf-8")

    assert to_text(html) == expected
    # The hidden inline-XBRL header (context IDs, dates) must not lead the text.
    hidden = re.compile(r"^[a-z0-9-]+_\d+$|^\d{4}-\d{2}-\d{2}$")
    assert not any(hidden.match(line) for line in expected.splitlines()[:5])


def test_cp1252_document_decodes() -> None:
    html = (FILINGS / "cp1252.htm").read_bytes()
    assert b"\x92" in html

    assert to_text(html) == "Notice\nThe Board’s recommendation\n"


def test_declared_http_equiv_charset_is_used() -> None:
    word = "Привет"
    html = (
        b'<html><head><meta http-equiv="Content-Type" content="text/html; charset=koi8-r">'
        b"</head><body><p>" + word.encode("koi8-r") + b"</p></body></html>"
    )

    assert to_text(html) == f"{word}\n"


def test_declared_meta_charset_is_used() -> None:
    html = b'<html><head><meta charset="utf-8"></head><body>caf\xc3\xa9</body></html>'

    assert to_text(html) == "café\n"


def test_declared_latin1_is_read_as_cp1252() -> None:
    # Browsers treat an ISO-8859-1 label as windows-1252, and filings rely on it.
    html = b'<meta charset="ISO-8859-1"><p>Board\x92s</p>'

    assert to_text(html) == "Board’s\n"


def test_unknown_declared_charset_is_ignored() -> None:
    html = b'<meta charset="no-such-charset"><p>caf\xc3\xa9</p>'

    assert to_text(html) == "café\n"


def test_wrong_declared_charset_falls_back() -> None:
    html = b'<meta charset="utf-8"><p>Board\x92s</p>'

    assert to_text(html) == "Board’s\n"


def test_undeclared_utf8_decodes() -> None:
    assert to_text("<p>Board’s</p>".encode()) == "Board’s\n"


def test_bytes_undefined_in_cp1252_are_replaced() -> None:
    assert to_text(b"<p>a\x81b\x92</p>") == "a�b’\n"


def test_whitespace_collapsed_and_empty_lines_dropped() -> None:
    html = b"<div>  Proposal\t No.\xc2\xa01 </div>\n\n<p> </p><p>Election\r\nof Directors</p>"

    assert to_text(html) == "Proposal No. 1\nElection\nof Directors\n"


def test_script_and_style_text_is_dropped() -> None:
    html = b"<style>p { color: red }</style><script>alert(1)</script><p>Notice</p>"

    assert to_text(html) == "Notice\n"


def test_inline_xbrl_parses_as_html_without_warning() -> None:
    # Inline XBRL filings are XHTML with an XML declaration; HTML parsing is intended.
    html = (
        b'<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        + b"<head><title></title></head><body><p>Notice</p>"
        + b"<p> </p>" * 100
        + b"</body></html>"
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert to_text(html) == "Notice\n"


def test_empty_document_is_empty_text() -> None:
    assert to_text(b"<html><body> </body></html>") == ""


def test_drops_inline_xbrl_header() -> None:
    hidden = (
        b'<div style="display:none"><ix:header><ix:hidden>2024-01-02</ix:hidden>'
        b"<ix:references>abc_1</ix:references></ix:header></div><p>Proposal 1</p>"
    )
    bare = b"<ix:header><ix:hidden>2024-01-02</ix:hidden></ix:header><p>Proposal 1</p>"

    assert to_text(hidden) == "Proposal 1\n"
    assert to_text(bare) == "Proposal 1\n"


def test_ignores_utf16_declaration() -> None:
    # WHATWG reads a utf-16 meta label as UTF-8: the bytes can't be UTF-16 if the tag is ASCII.
    # An even byte count, so a UTF-16 decode would succeed with garbage.
    assert to_text(b'<meta charset="utf-16"><p>caf\xc3\xa9</p> ') == "café\n"


def test_ignores_utf32_declaration() -> None:
    assert to_text(b'<meta charset="utf-32"><p>caf\xc3\xa9</p>  ') == "café\n"


def test_drops_utf8_bom() -> None:
    text = to_text(b"\xef\xbb\xbf<p>x</p>")

    assert text == "x\n"
    assert "\ufeff" not in text


def test_decode_drops_leading_bom() -> None:
    assert decode(b"\xef\xbb\xbf<p>x</p>") == "<p>x</p>"
