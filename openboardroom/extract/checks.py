"""The four checks that gate publication of extracted ballot items.

Any failure sends the whole filing to human review. The checks are pure and
tolerant: model output is untrusted, so malformed shapes (wrong types, missing
keys) become failed checks with a detail rather than exceptions.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, cast

from openboardroom.extract.regex import Candidate
from openboardroom.extract.spec import validate_output

CheckName = Literal["count", "quotes", "schema", "consistency"]

_QUOTE_FIELDS = ("source_quote", "recommendation_quote", "vote_standard_quote")
_ALWAYS_REQUIRED = (
    "number", "title", "type", "category", "effect", "board_recommendation", "source_quote",
)  # fmt: skip
_BINDING = {"director_election", "equity_plan", "charter_bylaw_amendment"}
_ADVISORY = {"say_on_pay", "say_on_pay_frequency", "auditor_ratification"}
_TRANSLATE = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-",
        "—": "-", "―": "-", "−": "-",
    }
)  # fmt: skip
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check.

    Attributes:
        name: Which check ran.
        passed: True when the check found nothing wrong.
        detail: What failed, item by item, in a stable order; None when passed.
    """

    name: CheckName
    passed: bool
    detail: str | None


def normalize(text: str) -> str:
    """Normalize text so quotes compare despite typographic differences.

    Args:
        text: Filing text or a quote.

    Returns:
        NFKC-normalized text with curly quotes made straight, en/em dashes and
        minus signs made ``-``, whitespace runs collapsed to one space, and
        the ends stripped.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    return _SPACES.sub(" ", folded).strip()


def _result(name: CheckName, problems: list[str]) -> CheckResult:
    return CheckResult(name, not problems, "; ".join(problems) if problems else None)


def _items(output: object) -> tuple[list[object] | None, str | None]:
    """Return the items list, or None and the reason it cannot be read."""
    if not isinstance(output, dict):
        return None, "output is not an object"
    items = cast(dict[str, Any], output).get("items")
    if not isinstance(items, list):
        return None, "items is missing or not a list"
    return cast(list[object], items), None


def _meeting(output: object) -> dict[str, Any] | None:
    meeting = cast(dict[str, Any], output).get("meeting") if isinstance(output, dict) else None
    return cast(dict[str, Any], meeting) if isinstance(meeting, dict) else None


def _label(index: int, item: object) -> str:
    number = cast(dict[str, Any], item).get("number") if isinstance(item, dict) else None
    return f"item {number if isinstance(number, int) else f'at position {index + 1}'}"


def _check_count(output: object, candidates: list[Candidate]) -> CheckResult:
    items, reason = _items(output)
    if items is None:
        return _result("count", [f"cannot count: {reason}"])
    problems: list[str] = []
    if len(items) != len(candidates):
        problems.append(f"{len(items)} items for {len(candidates)} candidates")
    numbers = [
        cast(dict[str, Any], i).get("number") if isinstance(i, dict) else None for i in items
    ]
    if numbers != list(range(1, len(items) + 1)):
        problems.append(f"item numbers are {numbers}, expected 1..{len(items)} in order")
    return _result("count", problems)


def _check_quotes(output: object, text: str) -> CheckResult:
    items, reason = _items(output)
    if items is None:
        return _result("quotes", [f"cannot check quotes: {reason}"])
    haystack = normalize(text)
    problems: list[str] = []

    def check(label: str, field: str, quote: object) -> None:
        if isinstance(quote, str) and normalize(quote) not in haystack:
            problems.append(f"{label}: {field} not found in filing text")

    meeting = _meeting(output)
    if meeting is not None:
        check("meeting", "date_quote", meeting.get("date_quote"))
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fields = cast(dict[str, Any], item)
        for field in _QUOTE_FIELDS:
            check(_label(index, fields), field, fields.get(field))
    return _result("quotes", problems)


def _check_schema(output: object) -> CheckResult:
    return _result("schema", validate_output(output))


def _item_problems(label: str, item: dict[str, Any]) -> list[str]:
    problems = [f"{label}: {f} is required" for f in _ALWAYS_REQUIRED if item.get(f) is None]
    if (
        item.get("board_recommendation") not in (None, "none")
        and item.get("recommendation_quote") is None
    ):
        problems.append(f"{label}: recommendation_quote is required")
    if item.get("vote_standard_text") is not None and item.get("vote_standard_quote") is None:
        problems.append(f"{label}: vote_standard_quote is required")

    kind, category, effect = item.get("type"), item.get("category"), item.get("effect")
    if kind == "shareholder" and item.get("lead_filer") is None:
        problems.append(f"{label}: lead_filer is required for a shareholder proposal")
    nominees = item.get("nominees")
    if category == "director_election" and not (isinstance(nominees, list) and nominees):
        problems.append(f"{label}: nominees needs at least one name for a director election")

    if kind == "shareholder":
        # A shareholder proposal is precatory unless it amends the charter or bylaws.
        required = "binding" if category == "charter_bylaw_amendment" else "precatory"
        if effect != required:
            problems.append(
                f"{label}: effect is {effect!r}, shareholder {category} requires {required!r}"
            )
    else:
        required = (
            "binding" if category in _BINDING else "advisory" if category in _ADVISORY else None
        )
        if required is not None and effect != required:
            problems.append(f"{label}: effect is {effect!r}, {category} requires {required!r}")
        if kind == "management" and effect == "precatory":
            problems.append(f"{label}: effect 'precatory' is not allowed for a management proposal")
    return problems


def _check_consistency(output: object) -> CheckResult:
    items, reason = _items(output)
    if items is None:
        return _result("consistency", [f"cannot check consistency: {reason}"])
    problems: list[str] = []
    meeting = _meeting(output)
    if meeting is None:
        problems.append("meeting: missing or not an object")
    else:
        for field in ("date", "date_quote"):
            if meeting.get(field) is None:
                problems.append(f"meeting.{field} is required")
    for index, item in enumerate(items):
        if isinstance(item, dict):
            fields = cast(dict[str, Any], item)
            problems.extend(_item_problems(_label(index, fields), fields))
        else:
            problems.append(f"{_label(index, item)}: not an object")
    return _result("consistency", problems)


def run_checks(output: object, text: str, candidates: list[Candidate]) -> list[CheckResult]:
    """Run all four checks on one filing's extraction.

    Args:
        output: Parsed JSON from the model; may be malformed.
        text: Plain text of the filing the quotes must come from.
        candidates: Proposal candidates from the regex pass.

    Returns:
        Exactly four results, in the order count, quotes, schema, consistency.
        A check that cannot read the output fails with a detail saying so.
    """
    return [
        _check_count(output, candidates),
        _check_quotes(output, text),
        _check_schema(output),
        _check_consistency(output),
    ]
