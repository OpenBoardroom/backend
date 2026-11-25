"""Tests for the regex proposal pass."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from openboardroom.extract.regex import Candidate, candidates_hint, find_candidates

FIXTURES = Path(__file__).parent.parent / "fixtures"
# Proposal counts read from each filing's ballot (see the fixtures README).
BALLOT_COUNTS = {"aapl-2023": 9, "aapl-2024": 8, "googl-2026": 14, "msft-2023": 13}


@pytest.mark.parametrize("name", sorted(BALLOT_COUNTS))
def test_golden(name: str):
    text = (FIXTURES / "filings" / f"{name}-def14a.txt").read_text(encoding="utf-8")
    expected = json.loads((FIXTURES / "regex" / f"{name}.expected.json").read_text())
    candidates = find_candidates(text)
    assert [{**asdict(c), "lines": list(c.lines)} for c in candidates] == expected
    assert [c.number for c in candidates] == list(range(1, BALLOT_COUNTS[name] + 1))


@pytest.mark.parametrize(
    ("line", "number"),
    [
        ("Proposal No. 3 — Advisory Vote", 3),
        ("Proposal Number 13: Shareholder Proposal", 13),
        ("PROPOSAL NUMBER 2", 2),
        ("PROPOSAL 4: Shareholder Proposal", 4),
        ("Item 2. Ratification", 2),
        ("Proposal Three", 3),
        ("Proposal 12 " + "x" * 300, 12),
        ("Proposal Twenty", 20),
        ("Animals within Supply Chain (Proxy Item No. 6)", 6),
        ("(ITEM 1 ON THE PROXY CARD)", 1),
        ("ELECTION OF DIRECTORS (ITEM 1)", 1),
        ("Shareholder Proposal – Action by Written Consent (Item 5)", 5),
        # Linde 2026 letters two votes under one number (finding F24).
        ("Proposal 2a: Non-Binding Ratification of the Appointment of the Auditor", 2),
        ("PROPOSAL 2B: Authorization of the Board", 2),
    ],
)
def test_variants(line: str, number: int):
    found = find_candidates(line)
    assert [c.number for c in found] == [number]
    assert found[0].heading == line


@pytest.mark.parametrize(
    "line",
    [
        "see Proposal 3 above",
        "Proposal 31",
        "Proposal 0",
        "Proposals",
        "Proposal Numbers 2",
        "Fee computed (Item 25(b))",
        "(Item 5.07)",
        "Each director nominee named in Proxy Item No. 1 must receive",
    ],
)
def test_non_headings_do_not_match(line: str):
    assert find_candidates(line) == []


def test_dedupes_by_number_keeping_all_lines():
    lines = ["intro"] * 100
    lines[5] = "Proposal 2: First"
    lines[90] = "Proposal 2: Again"
    lines[7] = "Proposal 1"
    found = find_candidates("\n".join(lines))
    assert found == [
        Candidate(number=1, heading="Proposal 1", lines=(7,)),
        Candidate(number=2, heading="Proposal 2: First", lines=(5, 90)),
    ]


def test_candidates_hint():
    found = [Candidate(1, "Proposal 1: A", (0,)), Candidate(2, "Proposal 2: B", (3,))]
    assert candidates_hint(found) == "1: Proposal 1: A\n2: Proposal 2: B"
    assert candidates_hint([]) == ""
