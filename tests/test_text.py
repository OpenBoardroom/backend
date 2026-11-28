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

from openboardroom.extract.checks import normalize
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

    # A source newline inside a block is whitespace, as in a browser.
    assert to_text(html) == "Proposal No. 1\nElection of Directors\n"


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


def test_inline_elements_join_without_added_space() -> None:
    # Alphabet 2026 splits one word across two links.
    html = b"<div><a>Proposal Number 3: A</a><a>pproval of the Plan</a></div>"

    assert to_text(html) == "Proposal Number 3: Approval of the Plan\n"


def test_whitespace_only_inline_element_keeps_one_space() -> None:
    # Cisco 2026 puts the space between "Proposal" and "2" in its own link.
    html = b"<div><a>Proposal</a><a> </a><a>2</a><a>: Advisory Vote</a></div>"

    assert to_text(html) == "Proposal 2: Advisory Vote\n"


def test_block_elements_start_new_lines() -> None:
    html = b"<p>One</p><div>Two<div>Three</div></div><h2>Four</h2><li>Five</li>"

    assert to_text(html) == "One\nTwo\nThree\nFour\nFive\n"


def test_br_breaks_a_line() -> None:
    assert to_text(b"<p>A<br/>B</p>") == "A\nB\n"


def test_table_cells_are_separate_lines() -> None:
    html = b"<table><tr><td>Proposal</td><td>2</td></tr></table>"

    assert to_text(html) == "Proposal\n2\n"


def test_nbsp_between_inline_elements_is_a_space() -> None:
    html = b"<p><span>Executive</span>&#160;<span>Compensation</span></p>"

    assert to_text(html) == "Executive Compensation\n"


_BREAK = b'<div style="page-break-after:always"></div>'


def _pages(bodies: list[bytes]) -> bytes:
    return _BREAK.join(bodies)


def test_running_header_and_page_numbers_are_dropped() -> None:
    html = _pages(
        [
            b"<div>2026 Proxy Statement</div><div>%d</div>" % n
            + b"<div>Table of Contents</div><p>Body %d</p>" % n
            for n in range(1, 13)
        ]
    )

    assert to_text(html) == "".join(f"Body {n}\n" for n in range(1, 13))


def test_sentence_split_by_furniture_is_contiguous_after_drop() -> None:
    # ExxonMobil 2026, Item 6: the resolution runs across a page break.
    furniture = b"<div>2026 Proxy Statement</div><div>%d</div><div>Table of Contents</div>"
    bodies = [furniture % n + b"<p>Other page %d</p>" % n for n in range(1, 13)]
    bodies[2] = furniture % 3 + b"<p>provides multiple independent options to shareholders so</p>"
    bodies[3] = (
        furniture % 4 + b"<p>that the retail voting program does not inordinately advantage</p>"
    )

    text = normalize(to_text(_pages(bodies)))

    assert "shareholders so that the retail voting program" in text


def test_numbered_headings_repeated_at_page_tops_are_kept() -> None:
    html = _pages([b"<div>(ITEM %d ON THE PROXY CARD)</div><p>Body</p>" % n for n in range(1, 6)])

    assert to_text(html).count("ON THE PROXY CARD") == 5


def test_no_page_breaks_drops_nothing() -> None:
    assert to_text(b"<p>Repeated</p>" * 10) == "Repeated\n" * 10


def test_repeat_below_threshold_is_kept() -> None:
    bodies = [b"<p>Body %d</p>" % n for n in range(1, 6)]
    bodies[0] = b"<div>Rare header</div>" + bodies[0]
    bodies[1] = b"<div>Rare header</div>" + bodies[1]

    assert to_text(_pages(bodies)).count("Rare header") == 2


def test_content_repeated_on_some_page_edges_is_kept() -> None:
    # Section labels like Alphabet's "Required Vote" repeat at page edges on about
    # 12% of pages; running headers repeat on nearly all. Here: 3 of 10 pages.
    bodies = [b"<p>Body %d</p>" % n for n in range(1, 11)]
    for n in (2, 5, 8):
        bodies[n] = b"<div>Vote Required</div>" + bodies[n]

    assert to_text(_pages(bodies)).count("Vote Required") == 3


def test_bare_numbers_inside_page_edges_are_kept() -> None:
    # Page numbers sit at the very edge; numbers a few lines in are content,
    # such as a table's year header or an 8-K's "Broker Non-Votes: 0".
    # Here the table is on 3 of 10 pages, every page numbered.
    furniture = b"<div>2026 Proxy Statement</div><div>%d</div>"
    table = (
        b"<div>Name</div><div>Year</div><div>Salary</div><div>2025</div><div>0</div>"
        + b"<p>More text</p>" * 4
    )
    html = _pages(
        [
            furniture % n + (table if n in (2, 5, 8) else b"") + b"<p>Body %d</p>" % n
            for n in range(1, 11)
        ]
    )

    text = to_text(html)

    assert text.count("\n2025\n") == 3
    assert text.count("\n0\n") == 3
    assert "2026 Proxy Statement" not in text
    assert "\n7\n" not in text


def test_page_number_followed_by_running_navigation_is_dropped() -> None:
    # Apple prints the page number, then its section menu, at the foot of each page.
    nav = b"".join(
        b"<div>%s</div>" % s for s in (b"Summary", b"Governance", b"Directors", b"Proposals")
    )
    html = _pages(
        [
            b"<p>Body %d</p>" % n + b"<p>More %d</p>" % n * 4 + b"<div>%d</div>" % (60 + n) + nav
            for n in range(1, 13)
        ]
    )

    text = to_text(html)

    assert "Governance" not in text
    assert not any(line.isdigit() for line in text.splitlines())


def test_short_document_keeps_lines_repeated_across_pages() -> None:
    # Review finding: in a short document every line sits at a page edge, so a
    # line repeated on 3 pages was dropped everywhere. Below 10 pages nothing is
    # treated as furniture.
    bodies = [b"<div>Vote by May 1</div><p>Notice %d</p>" % n for n in range(1, 8)]

    text = to_text(_pages(bodies))

    assert text.count("Vote by May 1") == 7
    assert "Notice 7" in text


def test_boeing_supplement_keeps_its_notice_text() -> None:
    # Boeing's 2026 DEFA14A (0001193125-26-096842): three versions of one notice
    # on 7 pages. The old rule dropped 21 lines, its voting instructions among them.
    html = (FILINGS / "boeing-2026-defa14a.htm").read_bytes()

    text = to_text(html)

    assert "Please note that if you own shares of The Boeing Company stock in mult" in text
    assert len(text.splitlines()) > 50
