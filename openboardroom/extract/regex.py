"""Regex pass that finds numbered proposal headings in filing text.

The result is a hint for the model and a cross-check on its output: if the
number of proposals found here differs from the number the model returns, the
filing goes to human review. Filings are untrusted input; this only matches text.
"""

import re
from dataclasses import dataclass

_NUMBER_WORDS = [
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty",
]  # fmt: skip
_WORD_NUMBERS = {word: number for number, word in enumerate(_NUMBER_WORDS, 1)}

MAX_PROPOSAL_NUMBER = 30

# A heading starts the line. The rest of the line is not capped: a proposal's
# title can run long (Microsoft's 13th did, and a length cap missed it).
_HEADING = re.compile(
    r"^\s*(?:Proposal|Item)\s+(?:No\.?\s*|Number\s+)?(\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Candidate:
    """A proposal number found by the regex pass.

    Attributes:
        number: 1-based proposal number.
        heading: The first matching line, as written (leading and trailing space removed).
        lines: Every 0-based line index whose text matched this number.
    """

    number: int
    heading: str
    lines: tuple[int, ...]


def _number(token: str) -> int | None:
    number = int(token) if token.isdigit() else _WORD_NUMBERS[token.lower()]
    return number if 1 <= number <= MAX_PROPOSAL_NUMBER else None


def find_candidates(text: str) -> list[Candidate]:
    """Find numbered proposal headings.

    Args:
        text: Plain text of a filing, one paragraph per line.

    Returns:
        One candidate per proposal number, sorted by number. A number that
        matches on several lines (a summary table, then the section) keeps
        the first line as its heading and lists every line.
    """
    headings: dict[int, str] = {}
    matched: dict[int, list[int]] = {}
    for index, line in enumerate(text.splitlines()):
        match = _HEADING.match(line)
        number = _number(match.group(1)) if match else None
        if number is None:
            continue
        headings.setdefault(number, line.strip())
        matched.setdefault(number, []).append(index)
    return [Candidate(n, headings[n], tuple(matched[n])) for n in sorted(headings)]


def candidates_hint(candidates: list[Candidate]) -> str:
    """Format candidates for the extraction prompt.

    Args:
        candidates: Candidates from `find_candidates`.

    Returns:
        One ``N: heading`` line per candidate.
    """
    return "\n".join(f"{c.number}: {c.heading}" for c in candidates)
