"""Tests for checking a supplement's stated ballot changes before anything is written."""

from pathlib import Path
from typing import Any

import pytest

from openboardroom.extract.supplement import problems

TEXT = (Path(__file__).parent.parent / "fixtures" / "filings" / "gs-2026-defa14a.txt").read_text(
    encoding="utf-8"
)
QUOTE = (
    "On April 1, 2026, the American Family Association withdrew "
    "“Item 5. Shareholder Proposal Regarding Charitable Giving Reporting.”"
)
ITEMS: list[dict[str, object]] = [
    {"number": str(n), "title": f"Item {n}", "type": "management",
     "board_recommendation": "for", "status": "active", "nominees": []}
    for n in range(1, 7)
]  # fmt: skip


def _change(**fields: Any) -> dict[str, Any]:
    change: dict[str, Any] = {
        "type": "withdraw", "number": 5, "part": "", "recommendation": None, "nominees": [],
        "field": None, "value": "", "item": None, "source_quote": QUOTE,
    }  # fmt: skip
    change.update(fields)
    return change


def _item(**fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "title": "New Item", "type": "management", "category": "other", "effect": "binding",
        "board_recommendation": "for", "lead_filer": "", "representative": "", "request": "",
        "summary": "Shareholders are asked to approve a new item.",
    }  # fmt: skip
    item.update(fields)
    return item


def test_no_changes_has_no_problems() -> None:
    assert problems({"changes": []}, TEXT, ITEMS) == []


def test_withdrawal_with_quote_has_no_problems() -> None:
    assert problems({"changes": [_change()]}, TEXT, ITEMS) == []


def test_curly_and_straight_quotes_compare_equal() -> None:
    straight = QUOTE.replace("“", '"').replace("”", '"')
    assert problems({"changes": [_change(source_quote=straight)]}, TEXT, ITEMS) == []


def test_schema_error() -> None:
    found = problems({"changes": [{"type": "withdraw"}]}, TEXT, ITEMS)
    assert found and found[0].startswith("changes/0")


def test_quote_not_in_supplement() -> None:
    change = _change(source_quote="the board now recommends a vote for this proposal instead")
    assert problems({"changes": [change]}, TEXT, ITEMS) == [
        "change 1 (withdraw): source_quote not found in filing text"
    ]


@pytest.mark.parametrize(("number", "part"), [(7, ""), (5, "a"), (None, "")])
def test_unknown_item(number: int | None, part: str) -> None:
    found = problems({"changes": [_change(number=number, part=part)]}, TEXT, ITEMS)
    assert len(found) == 1 and "is not an item of this meeting" in found[0]


def test_added_item_must_be_new() -> None:
    added = _change(type="add_item", number=6, item=_item())
    assert problems({"changes": [added]}, TEXT, ITEMS) == [
        "change 1 (add_item): item 6 already exists"
    ]
    assert problems({"changes": [{**added, "number": 7}]}, TEXT, ITEMS) == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"type": "change_recommendation"}, "recommendation is required"),
        ({"type": "change_nominees"}, "nominees are required"),
        ({"type": "amend_text"}, "field and value are required"),
        ({"type": "amend_text", "field": "title"}, "field and value are required"),
        ({"type": "add_item", "number": 7}, "item is required"),
    ],
)
def test_required_fields_per_type(fields: dict[str, Any], message: str) -> None:
    found = problems({"changes": [_change(**fields)]}, TEXT, ITEMS)
    assert len(found) == 1 and message in found[0]


def test_added_item_needs_a_number() -> None:
    added = _change(type="add_item", number=None, item=_item())
    assert problems({"changes": [added]}, TEXT, ITEMS) == [
        "change 1 (add_item): a number is required"
    ]
