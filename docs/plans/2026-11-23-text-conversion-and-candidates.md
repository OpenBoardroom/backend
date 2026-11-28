# Text Conversion and Proposal Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fetch's plain text match spec §6.2 ("one line per text block"), with page headers and footers removed, and extend the regex pass to the heading styles the 2026 sample scan found. This stops correct extractions from going to review because of how a label was laid out.

**Architecture:**
- `openboardroom/text.py` stops putting a newline between every HTML string. It joins inline text the way a browser renders it and breaks lines only at block elements. It marks page breaks while converting, then drops lines that repeat at page edges.
- A new `obr fetch --reconvert` re-runs the conversion on every cached raw document, with no download, and updates `text_sha256`.
- `openboardroom/extract/regex.py` also matches parenthesized labels anywhere in a line.

**Tech Stack:** Python 3.13, BeautifulSoup with lxml (already a dependency), psycopg 3, pytest with Testcontainers Postgres 17, ruff, pyright strict.

**Spec:** `OpenBoardroom/architecture` → `docs/specs/2026-11-21-ballot-item-pipeline-design.md` §6.2 (Fetch) and §6.3.1 (Regex pass). Evidence: `docs/extraction-sample-findings.md` F3 and F8. Ticket: BR-15 (#15). This plan sits between Plans 2 and 3.

## Global Constraints

- Branch `task/BR-15`; commits `[BR-15] TYPE: message`, each ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
- TDD; 90% line and branch coverage for `openboardroom/text.py`, `openboardroom/extract/regex.py`, `openboardroom/pipeline/fetch.py`.
- Parser bug fixes add a fixture (handbook). Every behavior below that came from a real filing gets a test built from that filing's HTML, cut to the smallest snippet that shows it.
- Filings are untrusted input: parse, never execute.
- No live EDGAR calls in tests. Task 4's fixture refresh makes at most 3 requests through `EdgarClient`, with `OBR_EDGAR_USER_AGENT` from `Settings`.
- `--reconvert` never downloads. It reads only cached raw documents whose sha256 matches the filing's `raw_sha256`.

## Scope rulings

- **Not attempted:** filings whose item numbers stand alone ("2" / "Ratification of…", "1." / "ELECTION OF DIRECTORS": IBM, JNJ, BRK-B, and probably TXN and V), and one-item special meetings with no number (NOW 2025). A bare-number rule would match page numbers and list items. These filings keep going to review, and the 8-K reconciliation (Plan 4) checks their counts after each meeting.
- **Extractions already run** (AAPL, XOM, GOOGL samples) had their quotes checked against the old text. They are re-run after this plan, together with the prompt changes from findings F2 and F9. Nothing has been published, so no correction records are needed.
- **The page-edge window and repeat thresholds below come from the 2026 sample** (XOM, GOOGL, CSCO). Task 5's scan checks them across all 105 cached proxies.

## Review Focus

1. **A heading repeated at the top of several pages with a different number each time** ("(ITEM 4 ON THE PROXY CARD)", "(ITEM 5 ON THE PROXY CARD)"). It must be kept, because only exact repeats are furniture. Test in Task 2.
2. **A document with no page-break markers.** Nothing is dropped. Test in Task 2.
3. **Adjacent inline elements with and without whitespace between them** ("Proposal Number 3: A" + "pproval…" joins to "Approval"; "Proposal" + " " + "2" joins to "Proposal 2"). Tests in Task 1.
4. **Table cells next to each other.** They become separate lines and are never glued together (no "Proposal2"). Test in Task 1.
5. **`--reconvert` for a filing whose raw document is missing or has the wrong hash.** No request is made, the filing is reported as failed, and its stage and hashes are unchanged. Test in Task 3.

---

### Task 1: One line per text block

**Files:**
- Modify: `openboardroom/text.py` (`to_text`)
- Test: `tests/test_text.py`

**Interfaces:**
- Produces: `to_text(html: bytes) -> str`. Same signature; lines are now text blocks. Task 2 extends it.

- [ ] **Step 1: Write the failing tests.** Each builds HTML inline and asserts on `to_text(...)`:
  - `test_inline_elements_join_without_added_space`: `<div><a>Proposal Number 3: A</a><a>pproval of the Plan</a></div>` gives `"Proposal Number 3: Approval of the Plan\n"` (from GOOGL 2026).
  - `test_whitespace_only_inline_element_keeps_one_space`: `<div><a>Proposal</a><a> </a><a>2</a><a>: Advisory Vote</a></div>` gives `"Proposal 2: Advisory Vote\n"` (from CSCO 2026).
  - `test_block_elements_start_new_lines`: `<p>One</p><div>Two<div>Three</div></div><h2>Four</h2><li>Five</li>` gives `"One\nTwo\nThree\nFour\nFive\n"`.
  - `test_br_breaks_a_line`: `<p>A<br/>B</p>` gives `"A\nB\n"`.
  - `test_table_cells_are_separate_lines`: `<table><tr><td>Proposal</td><td>2</td></tr></table>` gives `"Proposal\n2\n"`.
  - `test_nbsp_between_inline_elements_is_a_space`: `<p><span>Executive</span>&#160;<span>Compensation</span></p>` gives `"Executive Compensation\n"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_text.py -q`. Expected: the six new tests FAIL. The existing tests pass, except `test_golden_apple_2024`, which may now fail as well.
- [ ] **Step 3: Implement.** Before `get_text`, insert a newline string before and after every element in `_BLOCK_TAGS`, then call `soup.get_text("")` and keep the existing whitespace collapse and empty-line drop. `_BLOCK_TAGS = {"address","article","aside","blockquote","br","caption","dd","div","dl","dt","figcaption","figure","footer","form","h1","h2","h3","h4","h5","h6","header","hr","li","main","nav","ol","p","pre","section","table","tbody","td","tfoot","th","thead","tr","ul"}`. Update the docstring: lines are text blocks, inline text joins as rendered.
- [ ] **Step 4: Regenerate the golden file.** Run `to_text` on `tests/fixtures/filings/aapl-2024-def14a.htm` and write the output to `aapl-2024-def14a.expected.txt`. Read `git diff` on it: every change should either join a line that was split inside one block, or turn nothing into something new. Note anything else in the commit message.
- [ ] **Step 5: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 6: Commit** `[BR-15] FIX: convert filing HTML one line per text block, as spec §6.2 says`.

### Task 2: Drop page headers and footers

**Files:**
- Modify: `openboardroom/text.py`
- Test: `tests/test_text.py`

**Interfaces:**
- Consumes: Task 1's `to_text`.
- Produces: the same `to_text` signature; furniture lines removed.

Rule (decided here, from XOM, GOOGL and CSCO):
- A page break is any element whose `style` matches `page-break-(before|after)\s*:\s*always` or `break-(before|after)\s*:\s*page` (case-insensitive). Mark one with a sentinel line (`""`, a private-use character) before converting. It can't be whitespace: `"\f"` would be split by `splitlines()` and collapsed by the whitespace rule.
- Split the lines into pages at the sentinels. The **edge lines** of a page are its first 12 and last 12 lines.
- An edge line's key is the line itself, except that a line made only of digits (or a lowercase roman numeral) has the key `"#"`.
- A key is **furniture** when it appears among the edge lines of at least 3 pages, and of at least 20% of pages. Drop every edge line whose key is furniture. Lines away from the edges are never dropped. With no page breaks there is one page, and nothing is dropped.

- [ ] **Step 1: Write the failing tests:**
  - `test_running_header_and_page_numbers_are_dropped`: 5 pages, each `<div>2026 Proxy Statement</div><div>{n}</div><div>Table of Contents</div><p>Body {n}</p>`, separated by `<div style="page-break-after:always"></div>`. The output keeps only `Body 1` … `Body 5`.
  - `test_sentence_split_by_furniture_is_contiguous_after_drop`: XOM 2026 Item 6. Build 5 pages that each carry the furniture (`2026 Proxy Statement`, a page number, `Table of Contents`), so the rule fires. One page ends `…shareholders so`; the next page's body starts `that the retail voting program…`. Assert `normalize(to_text(html))` contains `"shareholders so that the retail voting program"` (`normalize` from `openboardroom.extract.checks`).
  - `test_numbered_headings_repeated_at_page_tops_are_kept`: 5 pages each starting `<div>(ITEM {n} ON THE PROXY CARD)</div>`. All 5 lines are kept.
  - `test_no_page_breaks_drops_nothing`: one page with a line repeated 10 times. All 10 are kept.
  - `test_repeat_below_threshold_is_kept`: a line at the edge of 2 of 5 pages is kept.
- [ ] **Step 2: Run** `uv run pytest tests/test_text.py -q`. Expected: the furniture tests FAIL. `test_numbered_headings…` and `test_no_page_breaks…` already pass; keep them as guards.
- [ ] **Step 3: Implement** `_drop_furniture(lines: list[str]) -> list[str]` in `openboardroom/text.py`, with the rule above. Constants: `_EDGE_LINES = 12`, `_MIN_PAGES = 3`, `_MIN_PAGE_SHARE = 0.2`. Call it inside `to_text` after the whitespace collapse; it removes the sentinels.
- [ ] **Step 4: Regenerate `aapl-2024-def14a.expected.txt`** and read the diff. Only page furniture should disappear.
- [ ] **Step 5: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 6: Commit** `[BR-15] NEW: drop running page headers and footers from filing text`.

### Task 3: `obr fetch --reconvert`

**Files:**
- Modify: `openboardroom/pipeline/fetch.py`
- Test: `tests/pipeline/test_fetch.py`

**Interfaces:**
- Produces: `reconvert(conn: psycopg.Connection, cache_dir: Path) -> ReconvertResult`, where `@dataclass(frozen=True) class ReconvertResult(changed: int, unchanged: int, failed: list[str])` (`failed` lists accessions). CLI: `obr fetch --reconvert` prints `fetch --reconvert: {changed} changed, {unchanged} unchanged, {failed} failed`, plus one line per failure, and exits 1 if any failed.
- Selection: every filing with `raw_sha256 is not null`, in id order, at any stage.

- [ ] **Step 1: Write the failing tests.** They use the existing Testcontainers `conn` fixture and `tmp_path`, with the cache populated by hand:
  - `test_reconvert_rewrites_text_and_hash`: a filing with cached raw whose stored text is stale. After `reconvert`, the text file equals `to_text(raw)`, `text_sha256` matches it, the stage is unchanged, and the result is `changed == 1`.
  - `test_reconvert_leaves_current_text_unchanged`: the text is already current, so the result is `unchanged == 1` and the file's mtime doesn't change.
  - `test_reconvert_missing_raw_fails_without_request`: no raw file; the filing is in `failed`, and the row is unchanged. Pass no `EdgarClient`: `reconvert` takes none, so it can't make a request.
  - `test_reconvert_hash_mismatch_fails`: the raw bytes don't match `raw_sha256`, so the filing is in `failed` and the row is unchanged.
  - `test_cli_reconvert_prints_counts_and_exits_nonzero_on_failure`.
- [ ] **Step 2: Run** `uv run pytest tests/pipeline/test_fetch.py -q`. Expected: the new tests FAIL (`reconvert` not defined).
- [ ] **Step 3: Implement** `reconvert`, reusing `_cached`, `_sha256` and `_write`. Write the text through the same `.part` rename. Commit per filing. Add `--reconvert` to the `fetch` subparser; it skips the EDGAR client entirely.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Document it in the README** under Stages: "After a change to text conversion, run `uv run obr fetch --reconvert` to rebuild every cached text from its raw document (no downloads)."
- [ ] **Step 6: Commit** `[BR-15] NEW: add obr fetch --reconvert to rebuild text from cached filings`.

### Task 4: Parenthesized labels in the regex pass, and refreshed fixtures

**Files:**
- Modify: `openboardroom/extract/regex.py`
- Modify: `tests/fixtures/filings/{aapl-2023,aapl-2024,msft-2023,googl-2026}-def14a.txt`, `tests/fixtures/regex/*.expected.json`, `tests/fixtures/filings/README.md`
- Test: `tests/extract/test_regex.py`

**Interfaces:**
- Produces: the same `find_candidates(text: str) -> list[Candidate]`. A line now also matches when it contains a parenthesized label anywhere.

- [ ] **Step 1: Refresh the text fixtures with Task 2's converter.**
  - Fetch the three raw documents not in the cache (AAPL 2023, AAPL 2024, MSFT 2023) through `EdgarClient`, from the URLs in the fixtures README. That's 3 requests.
  - Take GOOGL 2026 from `cache/raw`.
  - Write each `to_text` output over its fixture, and update the text sha256 column in the README.
- [ ] **Step 2: Regenerate the four `*.expected.json` files** with `find_candidates`. Confirm with `git diff` that only the `lines` indices changed: the numbers, the headings, and `BALLOT_COUNTS` are the same. Run `uv run pytest tests/extract/test_regex.py -q`; expected PASS. Commit `[BR-15] UPDATE: rebuild filing text fixtures with block-level conversion`.
- [ ] **Step 3: Write the failing tests** (add to `test_variants`, number expected):
  - `("Animals within Supply Chain (Proxy Item No. 6)", 6)` (PEP 2026)
  - `("(ITEM 1 ON THE PROXY CARD)", 1)` (HD 2026)
  - `("ELECTION OF DIRECTORS (ITEM 1)", 1)` (BA 2026)
  - `("Shareholder Proposal – Action by Written Consent (Item 5)", 5)` (BA 2026)

  Add to `test_non_headings_do_not_match`: `"Fee computed (Item 25(b))"`, `"(Item 5.07)"`, `"Each director nominee named in Proxy Item No. 1 must receive"`.
- [ ] **Step 4: Run** `uv run pytest tests/extract/test_regex.py -q`. Expected: the 4 new variants FAIL with `[] == [n]`; the non-headings pass.
- [ ] **Step 5: Implement.** Add `_PAREN_LABEL`, searched anywhere in a line when `_HEADING` doesn't match:
  `r"\((?:Proxy\s+)?(?:Proposal|Item)\s+(?:No\.?\s*|Number\s+)?(\d{1,2}|<words>)(?:\s+on\s+the\s+proxy\s+card)?\)"`, case-insensitive, with the same number words and the same 1–30 range.
- [ ] **Step 6: Run** `uv run pytest -q`. Expected: all pass. Commit `[BR-15] FIX: match parenthesized proposal labels in the regex pass`.

### Task 5: Rebuild the cache and re-scan

**Files:**
- Create: `scripts/scan_candidates.py` (prints candidate count, gaps and first heading for each filing; it takes a file of accessions)
- Modify: `docs/extraction-sample-findings.md`

- [ ] **Step 1: Run** `uv run obr fetch --reconvert` against the local database. Expected: `0 failed`, and most of the 416 filings `changed`.
- [ ] **Step 2: Run the scan over all 105 DEF 14As.** Expected: CSCO, JNJ, PEP, HD, BA and GOOGL have no gaps and no zero counts. The bare-number filings (IBM 1–4, BRK-B, TXN, V) and NOW 2025 remain. Spot-check XOM's Item 6 text: `…shareholders so` / `that the retail…`, with no furniture between them.
- [ ] **Step 3: Update findings F3 and F8** with the counts before and after, and commit `[BR-15] UPDATE: record the re-scan after block-level conversion`.
