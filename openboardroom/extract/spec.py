"""The versioned extraction prompt and output schema.

The prompt and schema live as files beside this module so they can be read and
audited without running code (handbook *Auditability*). A change to either
file gets a new version number, and the eval set runs before it ships.
"""

import json
import re
from functools import cache
from importlib.resources import files
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from openboardroom.extract.regex import Candidate, candidates_hint

PROMPT_VERSION = "4"
SCHEMA_VERSION = "3"
# Text-only regeneration after a review rejection (spec §7.2); prompt and schema share it.
TEXTS_PROMPT_VERSION = "1"
# Supplement (DEFA14A) pass: ballot changes only (spec §6.3 item 3); prompt and schema share it.
SUPPLEMENT_PROMPT_VERSION = "1"

_PROMPTS = files("openboardroom.extract") / "prompts"
# A closing tag inside the untrusted filing text would end the data block early.
_CLOSING_TAG = re.compile(r"</(filing)", re.IGNORECASE)


def system_prompt() -> str:
    """Return the system prompt for extraction.

    Returns:
        The contents of ``prompts/system-v{PROMPT_VERSION}.md``.
    """
    return (_PROMPTS / f"system-v{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def output_schema() -> dict[str, object]:
    """Return the JSON Schema for the model's structured output.

    Returns:
        A fresh copy of ``prompts/output-schema-v{SCHEMA_VERSION}.json``.
    """
    path = _PROMPTS / f"output-schema-v{SCHEMA_VERSION}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(output_schema(), format_checker=FormatChecker())


def texts_prompt() -> str:
    """Return the system prompt for regenerating rejected texts.

    Returns:
        The contents of ``prompts/texts-v{TEXTS_PROMPT_VERSION}.md``.
    """
    return (_PROMPTS / f"texts-v{TEXTS_PROMPT_VERSION}.md").read_text(encoding="utf-8")


def texts_schema() -> dict[str, object]:
    """Return the JSON Schema for regenerated texts.

    Returns:
        A fresh copy of ``prompts/texts-schema-v{TEXTS_PROMPT_VERSION}.json``.
    """
    path = _PROMPTS / f"texts-schema-v{TEXTS_PROMPT_VERSION}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def _texts_validator() -> Draft202012Validator:
    return Draft202012Validator(texts_schema())


def texts_user_message(text: str, items: list[dict[str, object]]) -> str:
    r"""Build the user message for regenerating one filing's rejected texts.

    Args:
        text: Plain text of the filing.
        items: One object per item: its settled facts, its current texts, and
            ``rewrite``, the texts to write with the reviewer's reason for each.

    Returns:
        The items as JSON, then the filing text last inside ``<filing>`` tags,
        with ``</filing`` escaped as in ``user_message``.
    """
    filing = _CLOSING_TAG.sub(r"<\\/\1", text)
    return (
        "Items whose texts were rejected in review. The facts are settled.\n"
        f"{json.dumps(items, indent=2, ensure_ascii=False)}\n\n"
        f"<filing>\n{filing}\n</filing>"
    )


def validate_texts(output: object) -> list[str]:
    """Check regenerated texts against their schema.

    Args:
        output: Parsed JSON from the model.

    Returns:
        One ``path: message`` string per schema error; empty when valid.
    """
    found: list[ValidationError] = list(_texts_validator().iter_errors(cast(Any, output)))  # pyright: ignore[reportUnknownMemberType]
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in sorted(found, key=lambda e: [str(part) for part in e.path])
    ]


def user_message(text: str, candidates: list[Candidate]) -> str:
    r"""Build the user message for one filing.

    Args:
        text: Plain text of the filing.
        candidates: Proposal candidates from the regex pass.

    Returns:
        The candidate hint, then the full filing text last, inside
        ``<filing>`` tags so the model can tell data from instructions. A
        ``</filing`` inside the text is escaped as ``<\/filing``.
    """
    hint = candidates_hint(candidates) or "(none found)"
    filing = _CLOSING_TAG.sub(r"<\\/\1", text)
    return (
        "Proposal candidates found by a regular expression. This is a hint only; "
        "it may be wrong or incomplete, and the filing decides.\n"
        f"{hint}\n\n"
        f"<filing>\n{filing}\n</filing>"
    )


def validate_output(output: object) -> list[str]:
    """Check model output against the output schema.

    Args:
        output: Parsed JSON from the model.

    Returns:
        One ``path: message`` string per schema error, with the path written
        like ``items/0/effect`` (``<root>`` for the top level); empty when valid.
    """
    # The output is arbitrary parsed JSON; jsonschema's stubs want a JSON type.
    found: list[ValidationError] = list(_validator().iter_errors(cast(Any, output)))  # pyright: ignore[reportUnknownMemberType]
    errors = sorted(found, key=lambda e: [str(part) for part in e.path])
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


def supplement_prompt() -> str:
    """Return the system prompt for reading a supplement's ballot changes.

    Returns:
        The contents of ``prompts/supplement-v{SUPPLEMENT_PROMPT_VERSION}.md``.
    """
    return (_PROMPTS / f"supplement-v{SUPPLEMENT_PROMPT_VERSION}.md").read_text(encoding="utf-8")


def supplement_schema() -> dict[str, object]:
    """Return the JSON Schema for a supplement's changes.

    Returns:
        A fresh copy of ``prompts/supplement-schema-v{SUPPLEMENT_PROMPT_VERSION}.json``.
    """
    path = _PROMPTS / f"supplement-schema-v{SUPPLEMENT_PROMPT_VERSION}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def _supplement_validator() -> Draft202012Validator:
    return Draft202012Validator(supplement_schema())


def supplement_user_message(text: str, items: list[dict[str, object]]) -> str:
    r"""Build the user message for one supplement.

    Args:
        text: Plain text of the supplement.
        items: The meeting's current items (``supplement.meeting_items``).

    Returns:
        The items as JSON, then the supplement last inside ``<filing>`` tags,
        with ``</filing`` escaped as in ``user_message``.
    """
    filing = _CLOSING_TAG.sub(r"<\\/\1", text)
    return (
        "The meeting's current items:\n"
        f"{json.dumps(items, indent=2, ensure_ascii=False)}\n\n"
        f"<filing>\n{filing}\n</filing>"
    )


def validate_supplement(output: object) -> list[str]:
    """Check a supplement's output against its schema.

    Args:
        output: Parsed JSON from the model.

    Returns:
        One ``path: message`` string per schema error; empty when valid.
    """
    found: list[ValidationError] = list(_supplement_validator().iter_errors(cast(Any, output)))  # pyright: ignore[reportUnknownMemberType]
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in sorted(found, key=lambda e: [str(part) for part in e.path])
    ]
