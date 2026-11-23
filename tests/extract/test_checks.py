"""Tests for the four extraction checks."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from openboardroom.extract.checks import CheckResult, normalize, run_checks
from openboardroom.extract.regex import Candidate, find_candidates

FIXTURES = Path(__file__).parent.parent / "fixtures"
NAMES = ["count", "quotes", "schema", "consistency"]


def _output() -> dict[str, Any]:
    message = json.loads((FIXTURES / "llm" / "aapl-2024.message.json").read_text("utf-8"))
    text = next(block["text"] for block in message["content"] if block["type"] == "text")
    return json.loads(text)


FILING = (FIXTURES / "filings" / "aapl-2024-def14a.txt").read_text("utf-8")
CANDIDATES = find_candidates(FILING)


def _run(output: object, text: str = FILING, candidates: list[Candidate] | None = None):
    return run_checks(output, text, CANDIDATES if candidates is None else candidates)


def _failed(results: list[CheckResult]) -> list[str]:
    return [r.name for r in results if not r.passed]


def _item(output: dict[str, Any], category: str) -> dict[str, Any]:
    return next(i for i in output["items"] if i["category"] == category)


def test_fixture_passes_all_four_in_order() -> None:
    results = _run(_output())
    assert [r.name for r in results] == NAMES
    assert all(r.passed and r.detail is None for r in results)


def test_eight_candidates() -> None:
    assert len(CANDIDATES) == 8


def test_seven_items_for_eight_candidates_fails_count() -> None:
    out = _output()
    out["items"].pop()
    results = _run(out)
    assert _failed(results) == ["count"]
    assert results[0].detail is not None and "7" in results[0].detail


def test_wrong_numbers_fail_count() -> None:
    out = _output()
    out["items"] = out["items"][:3]
    out["items"][2]["number"] = 4
    results = _run(out, candidates=CANDIDATES[:3])
    assert _failed(results) == ["count"]
    assert results[0].detail is not None and "[1, 2, 4]" in results[0].detail


def test_paraphrased_quote_fails_quotes_naming_item() -> None:
    out = _output()
    out["items"][2]["source_quote"] = "Something the filing never says in these words at all"
    results = _run(out)
    assert _failed(results) == ["quotes"]
    assert results[1].detail is not None and "item 3: source_quote" in results[1].detail


@pytest.mark.parametrize("field", ["recommendation_quote", "vote_standard_quote"])
def test_other_item_quotes_are_checked(field: str) -> None:
    out = _output()
    out["items"][1][field] = "invented words that appear nowhere in the document"
    results = _run(out)
    assert _failed(results) == ["quotes"]
    assert results[1].detail is not None and f"item 2: {field}" in results[1].detail


def test_meeting_date_quote_is_checked() -> None:
    out = _output()
    out["meeting"]["date_quote"] = "invented words that appear nowhere in the document"
    results = _run(out)
    assert _failed(results) == ["quotes"]
    assert results[1].detail is not None and "meeting: date_quote" in results[1].detail


@pytest.mark.parametrize(
    ("filing", "quote"),
    [
        ("the board’s view of the matter is final", "the board's view of the matter is final"),
        ("the board's view of the matter is final", "the board’s view of the matter is final"),
        ("one — two – three − four", "one - two - three - four"),
        ("a quote with a non-breaking space", "a quote with a non-breaking space"),
        ("a quote split\nacross  a line\n break", "a quote split across a line break"),
    ],
)
def test_typographic_differences_pass(filing: str, quote: str) -> None:
    out = _output()
    out["items"][0]["source_quote"] = quote
    results = _run(out, text=filing + "\n" + FILING)
    assert results[1].passed


def test_normalize() -> None:
    assert normalize("  “A”’s —\n b  ") == '"A"\'s - b'


def test_schema_failure_reported_and_others_still_run() -> None:
    out = _output()
    out["items"][0]["effect"] = "maybe"
    results = _run(out)
    assert "schema" in _failed(results)
    schema = results[2]
    assert schema.detail is not None and "effect" in schema.detail
    assert results[0].passed and results[1].passed


def test_non_dict_output_fails_without_crashing() -> None:
    results = _run(["not", "a", "dict"])
    assert [r.name for r in results] == NAMES
    assert not any(r.passed for r in results)
    assert all(r.detail for r in results)


def test_items_not_a_list_fails_without_crashing() -> None:
    out = _output()
    out["items"] = "nope"
    results = _run(out)
    assert [r.name for r in results] == NAMES
    assert not any(r.passed for r in results[:3])


def test_item_missing_keys_does_not_crash() -> None:
    out = _output()
    out["items"][0] = {"number": 1}
    out["items"][1] = "not an object"
    results = _run(out)
    assert _failed(results) == ["count", "quotes", "schema", "consistency"] or (
        "schema" in _failed(results) and "consistency" in _failed(results)
    )
    consistency = results[3].detail
    assert consistency is not None and "item 1" in consistency


def test_missing_meeting_does_not_crash() -> None:
    out = _output()
    del out["meeting"]
    results = _run(out)
    assert "schema" in _failed(results)
    assert not results[3].passed


def test_shareholder_without_lead_filer_fails_consistency() -> None:
    out = _output()
    item = _item(out, "shareholder_proposal")
    item["lead_filer"] = None
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert f"item {item['number']}: lead_filer" in (results[3].detail or "")


def test_director_election_without_nominees_fails() -> None:
    out = _output()
    _item(out, "director_election")["nominees"] = []
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 1: nominees" in (results[3].detail or "")


def test_say_on_pay_binding_fails() -> None:
    out = _output()
    _item(out, "say_on_pay")["effect"] = "binding"
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "advisory" in (results[3].detail or "")


def test_management_precatory_fails() -> None:
    out = _output()
    _item(out, "auditor_ratification")["effect"] = "precatory"
    assert _failed(_run(out)) == ["consistency"]


def test_equity_plan_advisory_fails() -> None:
    out = _output()
    _item(out, "director_election")["effect"] = "advisory"
    assert _failed(_run(out)) == ["consistency"]


def test_shareholder_binding_fails_unless_charter_amendment() -> None:
    out = _output()
    item = _item(out, "shareholder_proposal")
    item["effect"] = "binding"
    assert _failed(_run(out)) == ["consistency"]
    item["category"] = "charter_bylaw_amendment"
    assert _run(out)[3].passed


def test_shareholder_advisory_fails() -> None:
    out = _output()
    _item(out, "shareholder_proposal")["effect"] = "advisory"
    assert _failed(_run(out)) == ["consistency"]


def test_missing_date_quote_fails_consistency() -> None:
    out = _output()
    out["meeting"]["date_quote"] = None
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "meeting.date_quote" in (results[3].detail or "")


def test_missing_meeting_date_fails_consistency() -> None:
    out = _output()
    out["meeting"]["date"] = None
    assert _failed(_run(out)) == ["consistency"]


def test_required_item_fields_non_null() -> None:
    out = _output()
    out["items"][0]["board_recommendation"] = None
    out["items"][0]["source_quote"] = None
    results = _run(out)
    detail = results[3].detail or ""
    assert "item 1: board_recommendation" in detail
    assert "item 1: source_quote" in detail


def test_recommendation_quote_required_unless_none() -> None:
    out = _output()
    item = out["items"][1]
    item["recommendation_quote"] = None
    assert "recommendation_quote" in (_run(out)[3].detail or "")
    item["board_recommendation"] = "none"
    assert _run(out)[3].passed


def test_vote_standard_quote_required_with_text() -> None:
    out = _output()
    item = copy.deepcopy(out["items"][1])
    out["items"][1]["vote_standard_quote"] = None
    assert "vote_standard_quote" in (_run(out)[3].detail or "")
    out["items"][1]["vote_standard_text"] = None
    assert _run(out)[3].passed
    assert item["vote_standard_text"] is not None
