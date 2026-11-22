"""The versioned extraction prompt and output schema.

The prompt and schema live as files beside this module so they can be read and
audited without running code (handbook *Auditability*). A change to either
file gets a new version number, and the eval set runs before it ships.
"""

import json
from functools import cache
from importlib.resources import files
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from openboardroom.extract.regex import Candidate, candidates_hint

PROMPT_VERSION = "1"
SCHEMA_VERSION = "1"

_PROMPTS = files("openboardroom.extract") / "prompts"


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


def user_message(text: str, candidates: list[Candidate]) -> str:
    """Build the user message for one filing.

    Args:
        text: Plain text of the filing.
        candidates: Proposal candidates from the regex pass.

    Returns:
        The candidate hint, then the full filing text last, inside
        ``<filing>`` tags so the model can tell data from instructions.
    """
    hint = candidates_hint(candidates) or "(none found)"
    return (
        "Proposal candidates found by a regular expression. This is a hint only; "
        "it may be wrong or incomplete, and the filing decides.\n"
        f"{hint}\n\n"
        f"<filing>\n{text}\n</filing>"
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
