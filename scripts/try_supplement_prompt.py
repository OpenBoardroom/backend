r"""Dry-run the current supplement prompt on supplements already read, and print what it finds.

Usage (from the repo root; costs money, about 2 cents per supplement):
    ANTHROPIC_API_KEY="$(security find-generic-password -s anthropic-api-key -w)" \
        uv run python scripts/try_supplement_prompt.py ACCESSION [ACCESSION ...]

For example, the three supplements behind finding F25 (Duke, Mondelez, UPS):
    ... scripts/try_supplement_prompt.py 0001104659-26-033897 0001628280-26-025322 \
        0001628280-26-021015

Needs OBR_DATABASE_URL (and optionally OBR_CACHE_DIR, OBR_EXTRACT_MODEL,
OBR_EXTRACT_EFFORT). Writes nothing: the database connection is read-only, so
Postgres refuses any write. Each supplement is checked against the meeting it
is linked to, through the proxy its earlier run recorded changes for, or else
through the linking rule; one with no meeting is skipped.

Prints, per supplement: the call's status, any problems the checks find, and
each change (type, item, field, value). Then the tokens and the cost at
standard rates.
"""

import os
import sys
from pathlib import Path
from typing import Any, cast

import anthropic
import psycopg

from openboardroom.config import cache_dir_from_env, extract_settings_from_env
from openboardroom.extract import supplement as supp
from openboardroom.extract.llm import run_supplement

# Claude Opus 5.5 standard rates, dollars per token.
_INPUT_RATE, _OUTPUT_RATE = 4e-6, 20e-6


def _meeting(conn: psycopg.Connection, accession: str) -> int | None:
    """The meeting this supplement's earlier changes named, else the linking rule's."""
    row = conn.execute(
        """
        select pc.meeting_id from pending_changes pc
        join extraction_runs r on r.id = pc.extraction_run_id
        join filings f on f.id = r.filing_id
        where f.accession = %s limit 1
        """,
        (accession,),
    ).fetchone()
    if row is not None:
        return row[0]
    linked = supp.linked(conn, accession)
    return linked.meeting_id if linked else None


def main(argv: list[str]) -> int:
    """Run the dry run for each accession in ``argv[1:]``."""
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    settings = extract_settings_from_env()
    text_dir = cache_dir_from_env() / "text"
    client = anthropic.Anthropic()
    tokens_in = tokens_out = 0
    with psycopg.connect(os.environ["OBR_DATABASE_URL"]) as conn:
        conn.read_only = True
        for accession in argv[1:]:
            meeting_id = _meeting(conn, accession)
            path = Path(text_dir / f"{accession}.txt")
            if meeting_id is None or not path.is_file():
                print(f"== {accession}: skipped (no linked meeting or no cached text)")
                continue
            items = supp.meeting_items(conn, meeting_id)
            text = path.read_text(encoding="utf-8")
            outcome = run_supplement(client, settings, text, items)
            tokens_in += outcome.input_tokens or 0
            tokens_out += outcome.output_tokens or 0
            print(f"== {accession}: {outcome.status}")
            if outcome.status != "succeeded":
                print(f"   {outcome.detail}")
                continue
            for problem in supp.problems(outcome.output, text, items):
                print(f"   PROBLEM: {problem}")
            changes = cast(list[dict[str, Any]], (outcome.output or {}).get("changes", []))
            if not changes:
                print("   no changes")
            for change in changes:
                item = f"{change['number']}{change['part']}"
                print(f"   {change['type']} item {item} {change['field'] or ''}: {change['value']}")
    cost = tokens_in * _INPUT_RATE + tokens_out * _OUTPUT_RATE
    print(f"\n{tokens_in} input and {tokens_out} output tokens, ${cost:.2f} at standard rates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
