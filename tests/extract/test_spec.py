"""Tests for the versioned extraction prompt, output schema, and replay fixture."""

import copy
import json
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
    assert (PROMPT_VERSION, SCHEMA_VERSION) == ("1", "1")


def test_schema_is_valid_json_schema():
    Draft202012Validator.check_schema(output_schema())


def test_every_object_forbids_extra_properties_and_requires_all():
    objects = _objects(output_schema())
    assert len(objects) >= 4  # top level, meeting, item, discussion
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


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


def test_validate_output_reports_root_and_format_errors():
    assert validate_output([]) == ["<root>: [] is not of type 'object'"]
    output = copy.deepcopy(_fixture_output())
    output["meeting"]["date"] = "28 February 2024"
    assert [e.split(":")[0] for e in validate_output(output)] == ["meeting/date"]


def test_prompt_rules():
    prompt = system_prompt()
    assert "The proponent argues" in prompt
    assert "The board argues" in prompt
    assert "says" not in prompt
    assert "null" in prompt
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
    assert "null if the filing states neither" in prompt
    assert "whatever its subject, its proponent, or which side it favors" in prompt
    assert "self-descriptive or evaluative adjectives" in prompt


def test_fixture_arguments_carry_no_evaluative_adjectives():
    for item in _fixture_output()["items"]:
        discussion: dict[str, str] = item["discussion"] or {}
        for side in discussion.values():
            assert "deeply committed" not in side
