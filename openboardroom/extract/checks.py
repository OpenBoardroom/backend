"""The four checks that gate publication of extracted ballot items.

Any failure sends the whole filing to human review. The checks are pure and
tolerant: model output is untrusted, so malformed shapes (wrong types, missing
keys) become failed checks with a detail rather than exceptions.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
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
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
# Words count only when they hold a letter or digit, so "- - - - -" is no quote (R17).
MIN_QUOTE_WORDS = 5


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


def _missing(value: object) -> bool:
    """True for None and for a blank string; blank text is not an answer."""
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: object) -> str | None:
    """The value when it is a string, else None, so only strings reach set lookups."""
    return value if isinstance(value, str) else None


def iso_date(value: object) -> date | None:
    """The value as a date when it is a ``YYYY-MM-DD`` string naming a real day; else None.

    Stricter than ``date.fromisoformat``, which also takes forms like ``20240228``.
    """
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _word_count(folded: str) -> int:
    return sum(1 for token in folded.split() if any(c.isalnum() for c in token))


def _quote_problem(field: str, quote: object, haystack: str) -> str | None:
    """Why a quote fails against the normalized filing text, or None when it passes."""
    if not isinstance(quote, str):
        return f"{field} is not a string"
    folded = normalize(quote)
    if _word_count(folded) < MIN_QUOTE_WORDS:
        return f"{field} has fewer than {MIN_QUOTE_WORDS} words"
    if folded not in haystack:
        return f"{field} not found in filing text"
    return None


def _quote_needed(field: str, item: dict[str, Any]) -> bool:
    """Whether the item states the fact this quote must support (mirrors the consistency rules)."""
    if field == "recommendation_quote":
        recommendation = item.get("board_recommendation")
        return not _missing(recommendation) and recommendation != "none"
    if field == "vote_standard_quote":
        return not _missing(item.get("vote_standard_text"))
    return True


def _written_dates(day: date) -> tuple[str, ...]:
    """How a filing writes the day: "February 28, 2024" and the zero-padded "February 08, 2024"."""
    month = _MONTHS[day.month - 1]
    return (f"{month} {day.day}, {day.year}", f"{month} {day.day:02d}, {day.year}")


def _date_in_quote(day: date, quote: str) -> bool:
    folded = normalize(quote).casefold()
    return any(written.casefold() in folded for written in _written_dates(day))


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
    return f"item {number if type(number) is int else f'at position {index + 1}'}"


def _check_count(output: object, candidates: list[Candidate]) -> CheckResult:
    items, reason = _items(output)
    if items is None:
        return _result("count", [f"cannot count: {reason}"])
    problems: list[str] = []
    if not items:
        problems.append("no proposals found")
    if len(items) != len(candidates):
        problems.append(f"{len(items)} items for {len(candidates)} candidates")
    numbers = [
        cast(dict[str, Any], i).get("number") if isinstance(i, dict) else None for i in items
    ]
    expected = list(range(1, len(items) + 1))
    if not all(type(n) is int for n in numbers) or numbers != expected:
        problems.append(f"item numbers are {numbers}, expected 1..{len(items)} in order")
    return _result("count", problems)


def _check_quotes(output: object, text: str) -> CheckResult:
    items, reason = _items(output)
    if items is None:
        return _result("quotes", [f"cannot check quotes: {reason}"])
    haystack = normalize(text)
    problems: list[str] = []

    def check(label: str, field: str, quote: object, needed: bool = True) -> None:
        if quote is None:
            return  # null is a schema finding, and a required quote the consistency check's
        if not needed and _missing(quote):
            return  # "" where the filing doesn't state the fact is the right answer
        problem = _quote_problem(field, quote, haystack)
        if problem is not None:
            problems.append(f"{label}: {problem}")

    meeting = _meeting(output)
    if meeting is not None:
        check("meeting", "date_quote", meeting.get("date_quote"))
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        fields = cast(dict[str, Any], item)
        for field in _QUOTE_FIELDS:
            check(_label(index, fields), field, fields.get(field), _quote_needed(field, fields))
    return _result("quotes", problems)


def _check_schema(output: object) -> CheckResult:
    return _result("schema", validate_output(output))


def _item_problems(label: str, item: dict[str, Any]) -> list[str]:
    problems = [f"{label}: {f} is required" for f in _ALWAYS_REQUIRED if _missing(item.get(f))]
    if (
        item.get("board_recommendation") != "none"
        and not _missing(item.get("board_recommendation"))
        and _missing(item.get("recommendation_quote"))
    ):
        problems.append(f"{label}: recommendation_quote is required")
    if not _missing(item.get("vote_standard_text")) and _missing(item.get("vote_standard_quote")):
        problems.append(f"{label}: vote_standard_quote is required")

    nominees = item.get("nominees")
    if item.get("category") == "director_election" and not (
        isinstance(nominees, list) and nominees
    ):
        problems.append(f"{label}: nominees needs at least one name for a director election")
    return problems + _type_problems(label, item)


def _type_problems(label: str, item: dict[str, Any]) -> list[str]:
    """Type, category, effect, and proponent must agree (ruling R16).

    A shareholder item is a ``shareholder_proposal`` and vice versa. Its effect is
    ``precatory``, or ``binding`` for a binding shareholder bylaw amendment, never
    ``advisory``; it names what it requests and who filed it. A management item
    names no filer, representative, or co-filers.
    """
    problems: list[str] = []
    kind, category, effect = item.get("type"), item.get("category"), item.get("effect")
    if kind == "shareholder":
        if category != "shareholder_proposal":
            problems.append(
                f"{label}: a shareholder item needs category 'shareholder_proposal', "
                f"not {category!r}"
            )
        if effect not in ("precatory", "binding"):
            problems.append(
                f"{label}: effect is {effect!r}, a shareholder proposal is 'precatory' or 'binding'"
            )
        for field in ("request", "lead_filer"):
            if _missing(item.get(field)):
                problems.append(f"{label}: {field} is required for a shareholder proposal")
        return problems

    if category == "shareholder_proposal":
        problems.append(f"{label}: category 'shareholder_proposal' needs type 'shareholder'")
    group = _text(category)  # only a string can be looked up in the category sets
    required = "binding" if group in _BINDING else "advisory" if group in _ADVISORY else None
    if required is not None and effect != required:
        problems.append(f"{label}: effect is {effect!r}, {category} requires {required!r}")
    if kind == "management":
        if effect == "precatory":
            problems.append(f"{label}: effect 'precatory' is not allowed for a management proposal")
        for field in ("lead_filer", "representative"):
            if not _missing(item.get(field)):
                problems.append(f"{label}: {field} must be empty for a management item")
        if item.get("co_filers"):
            problems.append(f"{label}: co_filers must be empty for a management item")
    return problems


def _meeting_problems(meeting: dict[str, Any]) -> list[str]:
    problems = [
        f"meeting.{field} is required"
        for field in ("date", "date_quote")
        if _missing(meeting.get(field))
    ]
    value, quote = meeting.get("date"), meeting.get("date_quote")
    if _missing(value):
        return problems
    day = iso_date(value)
    if day is None:
        problems.append(f"meeting.date is not a YYYY-MM-DD date: {value!r}")
    elif isinstance(quote, str) and not _missing(quote) and not _date_in_quote(day, quote):
        # The date decides whether the filing is in scope at all (ruling R15).
        problems.append(f"meeting.date {day} does not appear in date_quote")
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
        problems.extend(_meeting_problems(meeting))
    for index, item in enumerate(items):
        if isinstance(item, dict):
            fields = cast(dict[str, Any], item)
            problems.extend(_item_problems(_label(index, fields), fields))
        else:
            problems.append(f"{_label(index, item)}: not an object")
    return _result("consistency", problems)


def meeting_date_verified(output: object, text: str) -> bool:
    """Whether the meeting date is backed by its quote, so it can decide scope (ruling R15).

    Args:
        output: Parsed JSON from the model; may be malformed.
        text: Plain text of the filing.

    Returns:
        True only when ``meeting.date`` is a real ``YYYY-MM-DD`` date, written
        the way a filing writes it inside ``meeting.date_quote``, and that quote
        passes the quote check against ``text``.
    """
    meeting = _meeting(output)
    if meeting is None:
        return False
    day, quote = iso_date(meeting.get("date")), meeting.get("date_quote")
    if day is None or not isinstance(quote, str) or not _date_in_quote(day, quote):
        return False
    return _quote_problem("date_quote", quote, normalize(text)) is None


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
