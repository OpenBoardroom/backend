"""Tests for the four extraction checks."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from openboardroom.extract.checks import (
    CheckResult,
    meeting_date_verified,
    normalize,
    run_checks,
)
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
    out["meeting"]["date_quote"] = "invented words that appear nowhere on February 28, 2024"
    results = _run(out)
    assert _failed(results) == ["quotes"]
    assert results[1].detail is not None and "meeting: date_quote" in results[1].detail


@pytest.mark.parametrize(
    ("filing", "quote"),
    [
        ("the board’s view of the matter is final", "the board's view of the matter is final"),
        ("the board's view of the matter is final", "the board’s view of the matter is final"),
        ("one — two – three − four five", "one - two - three - four five"),
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
    item["lead_filer"] = ""
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


def test_share_capital_authority_binding_passes() -> None:
    # Irish and UK share-capital authorities (finding F20); the resolution grants the authority.
    out = _output()
    item = _item(out, "auditor_ratification")
    item["category"], item["effect"] = "share_capital_authority", "binding"
    assert _failed(_run(out)) == []


def test_share_capital_authority_advisory_fails() -> None:
    out = _output()
    item = _item(out, "auditor_ratification")
    item["category"] = "share_capital_authority"
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "requires 'binding'" in (results[3].detail or "")


def test_shareholder_binding_passes_for_a_binding_bylaw_amendment() -> None:
    out = _output()
    _item(out, "shareholder_proposal")["effect"] = "binding"
    assert _run(out)[3].passed


def test_shareholder_advisory_fails() -> None:
    out = _output()
    _item(out, "shareholder_proposal")["effect"] = "advisory"
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 4: effect is 'advisory'" in (results[3].detail or "")


@pytest.mark.parametrize(
    "category", ["charter_bylaw_amendment", "say_on_pay", "other", "director_election"]
)
def test_shareholder_item_needs_the_shareholder_proposal_category(category: str) -> None:
    out = _output()
    _item(out, "shareholder_proposal")["category"] = category
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 4: a shareholder item needs category 'shareholder_proposal'" in (
        results[3].detail or ""
    )


def test_management_item_cannot_be_a_shareholder_proposal() -> None:
    out = _output()
    item = out["items"][1]
    item["category"] = "shareholder_proposal"
    item["effect"] = "precatory"
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 2: category 'shareholder_proposal' needs type 'shareholder'" in (
        results[3].detail or ""
    )


def test_relabelled_shareholder_proposal_fails() -> None:
    """The reviewer's probe: a shareholder proposal passed off as a management item."""
    out = _output()
    item = out["items"][3]  # NCPPR's proposal; no representative named
    item.update(type="management", effect="advisory", lead_filer="", representative="")
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 4: category 'shareholder_proposal' needs type 'shareholder'" in (
        results[3].detail or ""
    )


@pytest.mark.parametrize("blank", ["", "  "])
def test_shareholder_item_needs_request(blank: str) -> None:
    # The filer rule is covered by test_shareholder_item_without_filer_or_representative_fails.
    out = _output()
    out["items"][3]["request"] = blank
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "item 4: request is required for a shareholder proposal" in (results[3].detail or "")


@pytest.mark.parametrize(
    ("field", "value"),
    [("lead_filer", "Some Fund"), ("representative", "Some Advisor"), ("co_filers", ["A"])],
)
def test_management_item_has_no_proponent(field: str, value: object) -> None:
    out = _output()
    out["items"][0][field] = value
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert f"item 1: {field} must be empty for a management item" in (results[3].detail or "")


@pytest.mark.parametrize("date", ["28 February 2024", "2024-02-30", "2024-2-28"])
def test_meeting_date_must_be_an_iso_date(date: str) -> None:
    out = _output()
    out["meeting"]["date"] = date
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "meeting.date is not a YYYY-MM-DD date" in (results[3].detail or "")


def test_meeting_date_must_appear_in_the_date_quote() -> None:
    out = _output()
    out["meeting"]["date"] = "2024-02-27"
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert "meeting.date 2024-02-27 does not appear in date_quote" in (results[3].detail or "")


@pytest.mark.parametrize("written", ["March 8, 2024", "March 08, 2024", "MARCH 8, 2024"])
def test_meeting_date_matches_the_quote_with_or_without_zero_pad(written: str) -> None:
    quote = f"The annual meeting will be held virtually on {written}, at 9:00 a.m."
    out = _output()
    out["meeting"]["date"] = "2024-03-08"
    out["meeting"]["date_quote"] = quote
    assert _run(out, text=quote + "\n" + FILING)[3].passed


@pytest.mark.parametrize(
    ("date", "quote", "expected"),
    [
        ("2024-02-28", None, True),
        ("2024-02-27", None, False),
        (
            "2024-02-28",
            "invented words that appear nowhere in the document February 28, 2024",
            False,
        ),
        ("", None, False),
        ("not a date", None, False),
    ],
)
def test_meeting_date_verified(date: str, quote: str | None, expected: bool) -> None:
    out = _output()
    out["meeting"]["date"] = date
    if quote is not None:
        out["meeting"]["date_quote"] = quote
    assert meeting_date_verified(out, FILING) is expected


@pytest.mark.parametrize("output", [[], {"meeting": None}, {"meeting": {"date": 5}}])
def test_meeting_date_verified_tolerates_malformed_output(output: object) -> None:
    assert meeting_date_verified(output, FILING) is False


def test_missing_date_quote_fails_consistency() -> None:
    out = _output()
    out["meeting"]["date_quote"] = ""
    results = _run(out)
    assert _failed(results) == ["quotes", "consistency"]
    assert "meeting.date_quote" in (results[3].detail or "")


def test_missing_meeting_date_fails_consistency() -> None:
    out = _output()
    out["meeting"]["date"] = ""
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
    item["recommendation_quote"] = ""
    assert "recommendation_quote" in (_run(out)[3].detail or "")
    item["board_recommendation"] = "none"
    assert _run(out)[3].passed


def test_vote_standard_quote_required_with_text() -> None:
    out = _output()
    item = copy.deepcopy(out["items"][1])
    out["items"][1]["vote_standard_quote"] = ""
    assert "vote_standard_quote" in (_run(out)[3].detail or "")
    out["items"][1]["vote_standard_text"] = ""
    assert _run(out)[3].passed
    assert item["vote_standard_text"]


@pytest.mark.parametrize("field", ["category", "type", "effect", "board_recommendation"])
@pytest.mark.parametrize("value", [["x"], {"a": 1}, 7, 1.5, True, False])
def test_odd_enum_values_never_raise(field: str, value: object) -> None:
    for index in (0, 1, 5):
        out = _output()
        out["items"][index][field] = value
        results = _run(out)
        assert [r.name for r in results] == NAMES
        assert not results[2].passed  # the schema rejects every one of these


@pytest.mark.parametrize("quote", ["", "   ", "\n "])
def test_blank_quote_fails_quotes(quote: str) -> None:
    out = _output()
    out["items"][0]["source_quote"] = quote
    out["meeting"]["date_quote"] = quote
    results = _run(out)
    assert "quotes" in _failed(results)
    detail = results[1].detail or ""
    assert "item 1: source_quote" in detail and "meeting: date_quote" in detail


@pytest.mark.parametrize("value", [5, True, ["a b c d e f"], {"q": "a b c d e f"}])
def test_non_string_quote_fails_quotes_itself(value: object) -> None:
    out = _output()
    out["items"][0]["source_quote"] = value
    results = _run(out)
    assert not results[1].passed
    assert "item 1: source_quote is not a string" in (results[1].detail or "")


@pytest.mark.parametrize("quote", ["- - - - -", "— – - . ,", "1 2 3 4 - - -"])
def test_punctuation_does_not_count_as_words(quote: str) -> None:
    out = _output()
    out["items"][0]["source_quote"] = quote
    results = _run(out, text=quote + "\n" + FILING)
    assert "item 1: source_quote has fewer than 5 words" in (results[1].detail or "")


def test_blank_optional_quotes_are_skipped_where_the_fact_is_absent() -> None:
    out = _output()
    item = out["items"][1]
    item.update(board_recommendation="none", recommendation_quote="")
    item.update(vote_standard_text="", vote_standard_quote="")
    assert all(r.passed for r in _run(out))


@pytest.mark.parametrize("field", ["recommendation_quote", "vote_standard_quote"])
def test_blank_required_quote_fails_quotes_and_consistency(field: str) -> None:
    out = _output()
    out["items"][1][field] = ""
    results = _run(out)
    assert _failed(results) == ["quotes", "consistency"]
    assert f"item 2: {field} has fewer than 5 words" in (results[1].detail or "")


def test_short_quote_fails_even_when_present_in_text() -> None:
    out = _output()
    out["items"][0]["source_quote"] = "Apple Inc."  # occurs in the filing, but too short
    results = _run(out)
    assert not results[1].passed
    assert "item 1: source_quote has fewer than 5 words" in (results[1].detail or "")


def test_five_word_quote_is_enough() -> None:
    out = _output()
    words = normalize(out["items"][0]["source_quote"]).split()[:5]
    out["items"][0]["source_quote"] = " ".join(words)
    assert _run(out)[1].passed


def test_shortest_fixture_quote_has_at_least_five_words() -> None:
    out = _output()
    quotes = [out["meeting"]["date_quote"]]
    for item in out["items"]:
        quotes += [
            q
            for q in (
                item["source_quote"],
                item["recommendation_quote"],
                item["vote_standard_quote"],
            )
            if q
        ]
    assert min(len(normalize(q).split()) for q in quotes) >= 5


@pytest.mark.parametrize("bad", [True, 1.0])
def test_bool_or_float_item_number_fails_count(bad: object) -> None:
    out = _output()
    out["items"][0]["number"] = bad
    results = _run(out)
    assert not results[0].passed
    assert "expected 1..8" in (results[0].detail or "")


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_blank_required_strings_fail_consistency(bad: str) -> None:
    out = _output()
    out["items"][0]["title"] = bad
    out["items"][0]["source_quote"] = bad
    out["items"][1]["recommendation_quote"] = bad
    out["items"][1]["vote_standard_quote"] = bad
    out["meeting"]["date"] = bad
    out["meeting"]["date_quote"] = bad
    detail = _run(out)[3].detail or ""
    for expected in (
        "item 1: title",
        "item 1: source_quote",
        "item 2: recommendation_quote",
        "item 2: vote_standard_quote",
        "meeting.date is",
        "meeting.date_quote",
    ):
        assert expected in detail


def test_blank_lead_filer_fails_consistency() -> None:
    out = _output()
    _item(out, "shareholder_proposal")["lead_filer"] = "  "
    assert "lead_filer" in (_run(out)[3].detail or "")


def test_zero_items_and_zero_candidates_fails_count() -> None:
    results = _run({**_output(), "items": []}, candidates=[])
    assert not results[0].passed
    assert "no proposals found" in (results[0].detail or "")


def test_shareholder_item_with_only_a_representative_passes() -> None:
    # Alphabet 2026 item 6: "Inspire Investing, LLC, on behalf of its client" names
    # the representative but not the filer (sample finding F15).
    out = _output()
    item = _item(out, "shareholder_proposal")
    item.update(lead_filer="", representative="Inspire Investing, LLC")
    assert _run(out)[3].passed


@pytest.mark.parametrize("blank", ["", "  "])
def test_shareholder_item_without_filer_or_representative_fails(blank: str) -> None:
    out = _output()
    item = _item(out, "shareholder_proposal")
    item.update(lead_filer=blank, representative=blank)
    results = _run(out)
    assert _failed(results) == ["consistency"]
    assert f"item {item['number']}: lead_filer or representative is required" in (
        results[3].detail or ""
    )
