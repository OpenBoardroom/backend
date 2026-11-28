"""Run the regex pass over cached filing texts and report suspect results.

Usage (from the repo root):
    uv run python scripts/scan_candidates.py ACCESSIONS_FILE [CACHE_DIR]

ACCESSIONS_FILE lists one accession per line, e.g. every DEF 14A:
    docker compose exec -T postgres psql -U obr_owner -d openboardroom -At \
        -c "select accession from filings where form = 'DEF 14A'" > def14a.txt

Prints one line per filing (candidate count, missing numbers, first heading),
suspect filings first, then a summary. A filing is suspect when the regex
pass found no candidates or skipped a number. Missing numbers after the last
one found can't be seen this way; the 8-K reconciliation checks those.
"""

import sys
from pathlib import Path

from openboardroom.extract.regex import find_candidates


def scan(accession: str, cache_dir: Path) -> tuple[int, list[int], str]:
    """Return the candidate count, the skipped numbers, and the first heading."""
    text = (cache_dir / "text" / f"{accession}.txt").read_text(encoding="utf-8")
    candidates = find_candidates(text)
    numbers = {c.number for c in candidates}
    gaps = sorted(set(range(1, max(numbers, default=0) + 1)) - numbers)
    return len(candidates), gaps, candidates[0].heading if candidates else ""


def main(argv: list[str]) -> int:
    """Scan every accession in the file named by ``argv[1]``."""
    accessions = Path(argv[1]).read_text().split()
    cache_dir = Path(argv[2]) if len(argv) > 2 else Path("cache")
    rows = [(accession, *scan(accession, cache_dir)) for accession in accessions]
    suspect = [row for row in rows if row[1] == 0 or row[2]]
    for accession, count, gaps, heading in suspect + [r for r in rows if r not in suspect]:
        flag = "SUSPECT" if (accession, count, gaps, heading) in suspect else "ok"
        print(f"{flag:7} {accession} {count:2} gaps={gaps or '-'} | {heading[:70]}")
    print(f"\n{len(rows)} filings, {len(suspect)} suspect (no candidates or a skipped number)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
