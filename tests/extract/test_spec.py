"""Tests for the versioned extraction prompt, output schema, and replay fixture."""

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import anthropic.types
from jsonschema import Draft202012Validator

from openboardroom.extract.regex import Candidate, candidates_hint
from openboardroom.extract.spec import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    output_schema,
    system_prompt,
    user_message,
    validate_output,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
# Recorded Claude response for Apple's 2024 proxy, replayed instead of live calls.
# Its `abstentions_count_against: false` reflects only the present-and-voting prong
# of Apple's compound standard. The filing says abstentions can still block approval
# under the majority-of-the-quorum prong, so outcome tests must not assume
# abstentions are harmless for Apple.
MESSAGE = FIXTURES / "llm" / "aapl-2024.message.json"


def _fixture_output() -> dict[str, Any]:
    message = json.loads(MESSAGE.read_text(encoding="utf-8"))
    text = next(block["text"] for block in message["content"] if block["type"] == "text")
    return json.loads(text)


def _objects(node: object) -> list[dict[str, Any]]:
    """Every subschema with ``type: object``, found by walking the whole schema."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        if mapping.get("type") == "object":
            found.append(mapping)
        for value in mapping.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in cast(list[Any], node):
            found.extend(_objects(value))
    return found


def test_versions():
    assert (PROMPT_VERSION, SCHEMA_VERSION) == ("4", "3")


def test_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(output_schema())


def test_every_object_forbids_extra_properties_and_requires_all():
    objects = _objects(output_schema())
    assert len(objects) >= 4  # top level, meeting, item, discussion
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


# Text fields the filing may not state: a plain string, where "" means "not stated"
# (ruling R14), so they don't count toward the structured-output union limit.
_PLAIN_TEXT = {
    "meeting": ("date", "time", "record_date", "date_quote"),
    "item": (
        "lead_filer", "representative", "recommendation_quote",
        "vote_standard_text", "vote_standard_quote", "request",
    ),
    "discussion": ("proponent", "board"),
}  # fmt: skip
# The Claude structured-outputs limits on one request's schema.
MAX_UNION_PARAMETERS = 16
MAX_OPTIONAL_PARAMETERS = 24


def _parameters(node: object) -> list[dict[str, Any]]:
    """Every property schema of every object in the schema, $defs included."""
    return [
        cast(dict[str, Any], prop)
        for obj in _objects(node)
        for prop in cast(dict[str, Any], obj["properties"]).values()
    ]


def _is_union(prop: dict[str, Any]) -> bool:
    return "anyOf" in prop or isinstance(prop.get("type"), list)


def test_schema_fits_the_structured_output_limits():
    schema = output_schema()
    unions = [p for p in _parameters(schema) if _is_union(p)]
    assert len(unions) <= MAX_UNION_PARAMETERS
    assert len(unions) <= 10  # ruling R14's target, leaving room to grow
    optional = sum(len(set(o["properties"]) - set(o["required"])) for o in _objects(schema))
    assert optional == 0 <= MAX_OPTIONAL_PARAMETERS


def test_text_fields_are_plain_strings():
    defs = output_schema()["$defs"]
    assert isinstance(defs, dict)
    for name, fields in _PLAIN_TEXT.items():
        properties = cast(dict[str, Any], defs)[name]["properties"]
        for field in fields:
            assert properties[field]["type"] == "string", f"{name}.{field}"
            assert "anyOf" not in properties[field]
            assert "format" not in properties[field]


def test_fixture_uses_empty_strings_not_null_for_text():
    output = _fixture_output()
    for field in _PLAIN_TEXT["meeting"]:
        assert isinstance(output["meeting"][field], str)
    for item in output["items"]:
        for field in _PLAIN_TEXT["item"]:
            assert isinstance(item[field], str), (item["number"], field)


def test_schema_avoids_unsupported_keywords():
    unsupported = {"minimum", "maximum", "multipleOf", "minLength", "maxLength", "minItems"}
    text = json.dumps(output_schema())
    assert not [word for word in unsupported if f'"{word}"' in text]


def test_fixture_output_validates():
    output = _fixture_output()
    assert validate_output(output) == []
    assert [item["number"] for item in output["items"]] == list(range(1, 9))


def test_fixture_is_a_messages_api_response():
    message = anthropic.types.Message.model_validate(json.loads(MESSAGE.read_text("utf-8")))
    assert message.stop_reason == "end_turn"
    assert message.content[-1].type == "text"


def test_fixture_quotes_are_verbatim():
    text = (FIXTURES / "filings" / "aapl-2024-def14a.txt").read_text(encoding="utf-8")
    haystack = " ".join(text.split())
    output = _fixture_output()
    quotes = [output["meeting"]["date_quote"]]
    for item in output["items"]:
        quotes += [item["source_quote"], item["recommendation_quote"], item["vote_standard_quote"]]
    for quote in quotes:
        assert " ".join(quote.split()) in haystack, quote
        assert 8 <= len(quote.split()) <= 40, quote


def test_validate_output_reports_paths():
    output = copy.deepcopy(_fixture_output())
    output["items"][0]["effect"] = "maybe"
    errors = validate_output(output)
    assert len(errors) == 1
    assert errors[0].startswith("items/0/effect: ")


def test_validate_output_reports_root_errors():
    assert validate_output([]) == ["<root>: [] is not of type 'object'"]


def test_schema_leaves_the_date_format_to_the_checks():
    output = copy.deepcopy(_fixture_output())
    output["meeting"]["date"] = "28 February 2024"
    assert validate_output(output) == []
    output["meeting"]["date"] = None
    assert [e.split(":")[0] for e in validate_output(output)] == ["meeting/date"]


def test_prompt_rules():
    prompt = system_prompt()
    assert "The proponent argues" in prompt
    assert "The board argues" in prompt
    assert "says" not in prompt
    assert "null" in prompt
    assert 'empty string `""`' in prompt
    assert "use null" not in prompt.lower().replace("use `null`", "")
    assert "Never guess" in prompt
    assert "may be wrong or incomplete" in prompt
    assert "8 to 40 consecutive words" in prompt


def test_user_message():
    candidates = [Candidate(1, "Proposal No. 1 - Election", (3,))]
    text = "Proxy statement text.\nProposal No. 1 - Election\n"
    message = user_message(text, candidates)
    hint = candidates_hint(candidates)
    assert hint in message
    assert "may be wrong or incomplete" in message
    assert message.index(hint) < message.index(text)
    # Nothing but the closing delimiter follows the filing text.
    assert message.endswith(text + "\n</filing>")


def test_user_message_without_candidates():
    message = user_message("Filing.", [])
    assert "(none found)" in message
    assert message.endswith("Filing.\n</filing>")


def test_user_message_neutralizes_closing_tag_in_filing():
    message = user_message("Before </filing> middle </FILING > after", [])
    body = message.split("<filing>\n", 1)[1]
    assert body.lower().count("</filing") == 1
    assert body.endswith("\n</filing>")
    assert "Before <\\/filing> middle <\\/FILING > after" in message


def test_meeting_kind_may_be_null():
    output = copy.deepcopy(_fixture_output())
    output["meeting"]["kind"] = None
    assert validate_output(output) == []


def test_prompt_neutrality_rules():
    prompt = system_prompt()
    assert "`null` if the filing states neither" in prompt
    assert "whatever its subject, its proponent, or which side it favors" in prompt
    assert "self-descriptive or evaluative adjectives" in prompt
    assert "what shareholders are asked to vote on" in prompt
    assert "what a vote" not in prompt


def test_prompt_adjective_examples_cover_one_side_each():
    prompt = system_prompt()
    assert "a company-side" in prompt and "a proponent-side" in prompt
    assert '"deeply committed"' in prompt and '"reckless"' in prompt


def test_prompt_type_and_category_rules_match_the_checks():
    prompt = system_prompt()
    assert "never `advisory`" in prompt
    assert "binding shareholder bylaw amendment" in prompt
    assert "`charter_bylaw_amendment`: a management proposal" in prompt


def test_fixture_arguments_carry_no_evaluative_adjectives():
    for item in _fixture_output()["items"]:
        discussion: dict[str, str] = item["discussion"] or {}
        for side in discussion.values():
            assert "deeply committed" not in side


def test_prompt_title_rule():
    # Finding F2: company headings and proponent titles can carry an argument,
    # so every title is written to one neutral standard.
    prompt = system_prompt()
    assert "Write every `title` to the same standard" in prompt
    assert "not the company's heading or the proponent's title" in prompt
    assert "a proposal shareholders have rejected every year" in prompt
    assert "End Reckless Fleet Spending" in prompt


def test_prompt_examples_are_not_from_sample_filings():
    # Examples drawn from real filings would leak answers into the eval set.
    prompt = system_prompt()
    for phrase in (
        "overwhelmingly defeated",
        "Board-Aligned",
        "viewpoint diversity",
        "politicized",
    ):
        assert phrase not in prompt


def test_prompt_hedge_and_attribution_rule():
    # Finding F9: summaries kept one side's framing in OpenBoardroom's own voice.
    prompt = system_prompt()
    assert "Keep the resolution's hedges" in prompt
    assert "the perceived concentration of suppliers" in prompt
    assert "in quotation marks and attribute it" in prompt
    assert "what the proponent calls" in prompt
    assert "Apply this equally to the board's wording" in prompt


def test_earlier_prompt_versions_stay_readable():
    # Runs record the prompt version that produced them; the old text stays auditable.
    prompts = files("openboardroom.extract") / "prompts"
    for name in (
        "system-v1.md", "system-v2.md", "system-v3.md",
        "output-schema-v1.json", "output-schema-v2.json",
    ):  # fmt: skip
        assert (prompts / name).is_file()


def test_share_capital_authority_category():
    # Finding F20: Irish and UK companies put share-capital authorities on every ballot.
    schema = cast(dict[str, Any], output_schema())
    category = schema["$defs"]["item"]["properties"]["category"]
    assert "share_capital_authority" in category["enum"]
    prompt = system_prompt()
    assert "`share_capital_authority`: a management proposal" in prompt
    assert "pre-emption rights" in prompt
    assert "stays `charter_bylaw_amendment`" in prompt


def test_lettered_part_field():
    # Finding F24: Linde 2026 prints "Proposal 2a" and "Proposal 2b" as separate votes.
    schema = cast(dict[str, Any], output_schema())
    item = schema["$defs"]["item"]
    assert item["properties"]["part"]["type"] == "string"
    assert "part" in item["required"]
    prompt = system_prompt()
    assert "`part`:" in prompt and '"2a"' in prompt


def test_prompt_allows_an_unnamed_proponent():
    # Finding F23: some filings name no proponent at all.
    assert "names no proponent" in system_prompt()
