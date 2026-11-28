# Extraction and Checks (Plan 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each fetched DEF 14A into a meeting, its ballot items, nominees, and draft texts, via a regex pass, one Claude structured-output call, and the four §6.4 checks, leaving every filing `checked`, `in_review`, `out_of_scope`, or still `fetched` with a recorded reason.

**Architecture:** A new `openboardroom/extract/` package holds pure, separately tested parts: the regex candidate pass, the versioned prompt and JSON schema, the four checks, and the Claude client wrapper (direct streaming with server-side fallback, or Message Batches). A new pipeline stage, `openboardroom/pipeline/extract.py`, picks `fetched` DEF 14A filings, runs those parts, and writes everything for one filing in one transaction. `obr extract` drives it in direct or batch mode.

**Tech Stack:** Python 3.13, uv, psycopg 3, `anthropic` (Python SDK), `jsonschema`, pytest with Testcontainers Postgres 17, `httpx.MockTransport` for replaying recorded API responses, ruff, pyright strict, dbmate.

**Spec:** `OpenBoardroom/architecture` → `docs/specs/2026-11-21-ballot-item-pipeline-design.md` (§6.3 Extract, §6.4 Check, §8 Data model, §9, §10) and [ADR-0003](https://github.com/OpenBoardroom/architecture/blob/main/docs/adr/0003-ballot-item-schema-contract.md). Ticket: BR-15 (#15, Plan 2 of 2–5).

## Global Constraints

- Branch `task/BR-15`; commits `[BR-15] TYPE: message` (NEW, UPDATE, FIX, REVERT), each ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push; Quinn approves pushes and PRs.
- TDD for all code; 90% line and branch coverage for `openboardroom/extract/**` and `openboardroom/pipeline/extract.py` (handbook: parsing, extraction, database writes).
- Database tests use real Postgres via the existing Testcontainers fixtures in `tests/conftest.py`; query logic is never mocked.
- **No live Anthropic calls in tests or CI.** Tests replay recorded responses through `httpx.MockTransport` passed to the real SDK client. The only live calls are Task 9's smoke run, which Quinn runs.
- The Anthropic key comes from the environment only (`ANTHROPIC_API_KEY`, read by the SDK). Never read, print, or log it; never write it to a file. Claude Code must not have it in its environment (it bills the API instead of the subscription): Quinn passes it per command.
- EDGAR: User-Agent from `OBR_EDGAR_USER_AGENT` via `Settings`, at most 10 requests per second, through the existing `EdgarClient`. Raw filings are never committed; text fixtures are.
- Filings are untrusted input: parse, never execute. Model output is untrusted too: validate before writing.
- Model `claude-opus-5-5`, adaptive thinking (omit `thinking`; Opus 5.5 always thinks), `output_config.effort` `"high"` set explicitly (the model's default is `medium`), structured output via `output_config.format` with the versioned JSON schema. Settings `OBR_EXTRACT_MODEL` (default `claude-opus-5-5`), `OBR_EXTRACT_EFFORT` (default `high`), `OBR_EXTRACT_MAX_TOKENS` (default `64000`).
- Direct calls stream (`client.beta.messages.stream(...)`, `.get_final_message()`) with `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"`. Batch requests can't set `fallbacks`; a batched item that ends `errored`, `expired`, or in `refusal` is re-run once as a direct call. A final refusal sends the filing to review.
- Neutrality (handbook, spec §8.4): discussion attributed and parallel; **both sides use "argues"** ("The proponent argues…", "The board argues…") [Quinn, 2026-11-22, frontend]; never OpenBoardroom's view; null for routine items with no opposing argument; summary is one neutral sentence on what the vote decides.
- Null means "not stated", never a guess (spec §8.2).
- Run order of a filing's writes: one transaction per filing; a failure rolls back that filing only.

## Scope rulings (for Quinn's approval)

- **Regex pass finds proposal candidates only.** Spec §6.3.1 also lists the filing header and meeting fields; here the meeting's date, time, format, and record date come from the model and are quote-checked (`meeting.date_quote`). Cost if wrong: one fewer cross-check on the meeting date.
- **DEFA14A extraction (spec §6.3.3) moves to Plan 3**, because its output is typed changes to published items (pending changes), which #15 assigns to Plan 3. Plan 2 extracts DEF 14A only.
- **No prompt caching** yet: each filing's request shares only the system prompt and schema, which are small; Plan 5's backfill measures whether caching pays.

## Review Focus

1. **The model returns something unusable** (`max_tokens` stop, non-JSON text, JSON that fails the schema, a refusal from every fallback, an API error): the filing never ends up half-written. Unusable or failed output leaves it at `fetched` with a `failed` run; a refusal sends it to `in_review` with no items. Tests in Tasks 6 and 7.
2. **Quotes with typographic differences** (curly vs straight quotes, en/em dashes vs hyphens, non-breaking spaces, line breaks inside a quote): the quote check normalizes both sides before comparing and still fails on a real paraphrase. Tests in Task 5.
3. **Re-running `obr extract`** after a crash, or with a batch already submitted: no filing is submitted twice, and no second meeting is written for one proxy (unique key). Tests in Tasks 2 and 7.
4. **A meeting outside the index window** (the company joined the S&P 100 after the meeting date, or left before it): the filing ends `out_of_scope` with the reason recorded and nothing else written. Test in Task 7.
5. **Proposal numbering the regex can trip on** (13+ proposals, "Proposal No. 1", "Item 4", repeated headings in a table of contents, spelled-out numbers): candidates are de-duplicated by number with every heading position kept. Golden tests in Task 3.

---

### Task 1: Clean the plain text

Carry-overs from Plan 1's reviews: strip the inline-XBRL hidden header, ignore utf-16/32 charset declarations, drop a UTF-8 BOM.

**Files:**
- Modify: `openboardroom/text.py`
- Modify: `tests/fixtures/filings/aapl-2024-def14a.expected.txt` (regenerated)
- Test: `tests/test_text.py`

**Interfaces:**
- Produces: `to_text(html: bytes) -> str` (unchanged signature) with the header removed; `decode(html: bytes) -> str` handling BOMs and utf-16/32 labels.

- [ ] **Step 1: Write the failing tests.**
  - `test_drops_inline_xbrl_header`: HTML with `<div style="display:none"><ix:header>…<ix:hidden>2024-01-02</ix:hidden>…</ix:header></div><p>Proposal 1</p>` → `"Proposal 1\n"`. Also with `<ix:header>` not wrapped in a hidden div.
  - `test_ignores_utf16_declaration`: bytes `<meta charset="utf-16"><p>caf\xc3\xa9</p>` → text `"café\n"` (WHATWG treats a `utf-16` meta label as UTF-8).
  - `test_ignores_utf32_declaration`: the same with `utf-32`.
  - `test_drops_utf8_bom`: `b"\xef\xbb\xbf<p>x</p>"` → `"x\n"` with no U+FEFF.
  - `test_aapl_2024_golden`: unchanged test, now against the regenerated expected file; assert also that the first line of the expected text is not an XBRL context ID (doesn't match `^[a-z0-9-]+_\d+$|^\d{4}-\d{2}-\d{2}$` for the first 5 lines).
- [ ] **Step 2: Run them.** `uv run pytest tests/test_text.py -q`. Expected: the four new tests FAIL.
- [ ] **Step 3: Implement.** Remove every `ix:header` element (and its hidden wrapper) with BeautifulSoup before `get_text`. In `_declared_charset`, map any `utf-16*`/`utf-32*` label to `None`. In `decode`, try `utf-8-sig` instead of `utf-8`, and strip a leading U+FEFF from any decode result.
- [ ] **Step 4: Regenerate the golden file** with `to_text` on `aapl-2024-def14a.htm` and inspect the diff: only the hidden header lines disappear.
- [ ] **Step 5: Run.** `uv run pytest -q && uv run ruff check && uv run pyright`. Expected: PASS.
- [ ] **Step 6: Commit.** `[BR-15] FIX: drop the inline-XBRL header and BOMs from filing text`

### Task 2: Schema changes for extraction

**Files:**
- Create: `db/migrations/20261122000001_extraction.sql`
- Modify: `db/schema.sql` (regenerated with `dbmate dump`, pinned Postgres 17.12 as in CI)
- Test: `tests/db/test_extraction_schema.py`

**Interfaces:**
- Produces (columns and constraints later tasks write):
  - `meetings`: `unique (proxy_filing_id)`.
  - `extraction_runs.kind` in (`regex`, `llm`, `reconcile`); `extraction_runs.status` in (`submitted`, `succeeded`, `refused`, `failed`); new nullable columns `batch_id text`, `detail text`.
  - `filings.stage` adds `out_of_scope`.
  - Down migration restores the previous constraints and drops the new columns.

- [ ] **Step 1: Write the failing tests** (Testcontainers):
  - inserting two meetings with the same `proxy_filing_id` raises `UniqueViolation`;
  - `extraction_runs` rejects `kind='other'` and `status='done'`, and accepts each listed value;
  - `filings.stage = 'out_of_scope'` is accepted;
  - `dbmate up`, `down`, `up` round-trips (extend the existing round-trip test if it covers all migrations).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the migration.** Name constraints explicitly. `pipeline_rw` already has rights on these tables; no new grants.
- [ ] **Step 4: Regenerate `db/schema.sql`** the way CI does (see `.github/workflows/ci.yml`: `dbmate up && dbmate dump` against postgres:17.12) and check the diff holds only this migration's changes. Published views are untouched, so the frontend contract (ADR-0003) doesn't change.
- [ ] **Step 5: Run.** `uv run pytest tests/db -q`. Expected: PASS.
- [ ] **Step 6: Commit.** `[BR-15] NEW: add extraction run vocabularies and one meeting per proxy`

### Task 3: Text fixtures and the regex candidate pass

**Files:**
- Create: `tests/fixtures/filings/{aapl-2023,aapl-2024,msft-2023}-def14a.txt`, `tests/fixtures/filings/README.md` (source URL, accession, retrieval date, sha256 of the raw document and of the text)
- Create: `openboardroom/extract/__init__.py`, `openboardroom/extract/regex.py`
- Create: `tests/fixtures/regex/{aapl-2023,aapl-2024,msft-2023}.expected.json`
- Test: `tests/extract/test_regex.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class Candidate:
    number: int              # 1-based proposal number
    heading: str             # the first matching line, as written
    lines: tuple[int, ...]   # every 0-based line index whose text matched this number

def find_candidates(text: str) -> list[Candidate]   # sorted by number
def candidates_hint(candidates: list[Candidate]) -> str   # one "N: heading" line each, for the prompt
```

- [ ] **Step 1: Build the fixtures.** For the spike's three filings (accessions `0001308179-23-000019`, `0001308179-24-000010`, `0001193125-23-259247`), download the primary document with the existing `EdgarClient` (3 requests, `Settings.from_env()` User-Agent), convert with Task 1's `to_text`, and save only the text. Do not commit raw HTML. Record sources and hashes in the README.
- [ ] **Step 2: Write the failing tests.**
  - `test_golden[aapl-2023|aapl-2024|msft-2023]`: `find_candidates(text)` serialized equals the expected JSON. The expected files are written by hand from reading each filing's ballot: AAPL 2023 and 2024 each have the proposal numbers in the spike's `regex.json`/`llm.json`, and MSFT 2023 has **13**, including the 13th that the spike's 140-character cap missed.
  - `test_variants`: lines `"Proposal No. 3 — Advisory Vote"`, `"PROPOSAL 4: Shareholder Proposal"`, `"Item 2. Ratification"`, `"Proposal Three"`, `"Proposal 12 " + "x" * 300` all match with the right numbers; `"see Proposal 3 above"` (not at line start) does not.
  - `test_dedupes_by_number_keeping_all_lines`: the same number on lines 5 and 90 yields one candidate with `lines == (5, 90)` and the line-5 heading.
- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Implement** from the spike's `HEADING` pattern (`spikes/extraction/regex_pass.py`) without the length cap, numbers 1–30 in digits and one–twenty in words.
- [ ] **Step 5: Run.** `uv run pytest tests/extract/test_regex.py -q`. Expected: PASS; coverage for `regex.py` ≥ 90%.
- [ ] **Step 6: Commit.** `[BR-15] NEW: add text fixtures and the regex proposal pass`

### Task 4: Versioned prompt, output schema, and a replay fixture

**Files:**
- Create: `openboardroom/extract/prompts/system-v1.md`, `openboardroom/extract/prompts/output-schema-v1.json`
- Create: `openboardroom/extract/spec.py`
- Create: `tests/fixtures/llm/aapl-2024.message.json`
- Test: `tests/extract/test_spec.py`
- Modify: `pyproject.toml` (add `anthropic`, `jsonschema` at exact versions; include `prompts/*` as package data)

**Interfaces:**
- Produces:
```python
PROMPT_VERSION = "1"
SCHEMA_VERSION = "1"
def system_prompt() -> str                  # contents of system-v1.md
def output_schema() -> dict[str, object]    # contents of output-schema-v1.json
def user_message(text: str, candidates: list[Candidate]) -> str
def validate_output(output: object) -> list[str]   # jsonschema errors as "path: message"; [] when valid
```
- Output schema (spec §8.3 with §8.2 applied), every object `additionalProperties: false`, every property required (nullable via `anyOf` with `null`):
  - `meeting`: `kind` (`annual`|`special`), `date` (YYYY-MM-DD), `time`, `format` (`virtual`|`in_person`|`hybrid`), `record_date`, `shares_outstanding` (integer), `date_quote`.
  - `items[]`: `number` (integer), `title`, `summary`, `type` (`management`|`shareholder`), `category` (spec §8.2 set), `effect` (`binding`|`advisory`|`precatory`), `lead_filer`, `representative`, `co_filers` (string[]), `board_recommendation` (spec §8.2 set), `recommendation_quote`, `vote_base` (spec §8.2 set), `vote_threshold_pct` (number), `abstentions_count_against` (boolean), `vote_standard_text`, `vote_standard_quote`, `request`, `nominees` (string[]), `discussion` (object `{proponent, board}` or null), `source_quote`.
  - Use only features structured outputs support (no `minimum`/`maxLength`/recursion; see the Python SDK structured-outputs notes).

- [ ] **Step 1: Write the system prompt** from the spike's `SYSTEM` with: the §8.2 definitions of `effect`, `category`, `board_recommendation`, the vote standard fields, and lead filer / representative / co-filers; "argues" for both sides; quotes copied verbatim, 8 to 40 consecutive words; null when not stated. Keep the regex hint caveat ("may be wrong or incomplete").
- [ ] **Step 2: Write the schema** as above.
- [ ] **Step 3: Write the replay fixture** `aapl-2024.message.json`: a complete Messages API response object (`id`, `type: "message"`, `role`, `model: "claude-opus-5-5"`, `content: [{"type":"thinking","thinking":"","signature":"x"},{"type":"text","text":<JSON string>}]`, `stop_reason: "end_turn"`, `stop_details: null`, `usage` with the spike's token counts). Build the JSON payload from the spike's `out/AAPL-0001308179-24-000010/llm.json`, converted to schema v1, with **every quote copied verbatim from Task 3's `aapl-2024-def14a.txt`**.
- [ ] **Step 4: Write the failing tests.**
  - `test_schema_is_valid_json_schema` (Draft 2020-12 meta-schema).
  - `test_every_object_forbids_extra_properties_and_requires_all` (walk the schema).
  - `test_fixture_output_validates`: the fixture's text JSON passes `validate_output`.
  - `test_validate_output_reports_paths`: an item with `effect: "maybe"` returns an error mentioning `items/0/effect`.
  - `test_prompt_rules`: the prompt contains "The proponent argues" and "The board argues", does not contain "says", and mentions null for unstated fields.
  - `test_user_message`: contains the candidates hint and the full text, text last.
- [ ] **Step 5: Run and implement** until green. `uv run pytest tests/extract/test_spec.py -q`.
- [ ] **Step 6: Commit.** `[BR-15] NEW: add the versioned extraction prompt and output schema`

### Task 5: The four checks

**Files:**
- Create: `openboardroom/extract/checks.py`
- Test: `tests/extract/test_checks.py`

**Interfaces:**
- Consumes: `Candidate` (Task 3), `validate_output` (Task 4).
- Produces:
```python
@dataclass(frozen=True)
class CheckResult:
    name: Literal["count", "quotes", "schema", "consistency"]
    passed: bool
    detail: str | None      # what failed, item by item; None when passed

def normalize(text: str) -> str
def run_checks(output: object, text: str, candidates: list[Candidate]) -> list[CheckResult]  # always all four, in that order
```
- Rules (spec §6.4, §8.2):
  - **count**: `len(items) == len(candidates)`, and item numbers are exactly 1..n in order.
  - **quotes**: every non-null `source_quote`, `recommendation_quote`, `vote_standard_quote`, and `meeting.date_quote` occurs in `normalize(text)` after `normalize(quote)`. `normalize`: NFKC; curly quotes to straight; en/em dashes and minus to `-`; all whitespace runs (including NBSP and newlines) to one space; strip.
  - **schema**: `validate_output(output) == []`. If schema fails, the other checks still run where they can and report what they couldn't check.
  - **consistency**: `type == "shareholder"` ⇒ `lead_filer` not null; `category == "director_election"` ⇒ ≥1 nominee; `effect` matches: `director_election`, `equity_plan`, `charter_bylaw_amendment` ⇒ `binding`; `say_on_pay`, `say_on_pay_frequency`, `auditor_ratification` ⇒ `advisory`; `type == "shareholder"` ⇒ `precatory`, except `category == "charter_bylaw_amendment"` ⇒ `binding`; `type == "management"` ⇒ never `precatory`. Required non-null: `number`, `title`, `type`, `category`, `effect`, `board_recommendation`, `source_quote`, `meeting.date`, `meeting.date_quote`; `recommendation_quote` when `board_recommendation != "none"`; `vote_standard_quote` when `vote_standard_text` is not null.

- [ ] **Step 1: Write the failing tests.** The AAPL fixture output passes all four. Then one mutation per rule, each failing exactly its check with a detail naming the item: 7 items for 8 candidates; numbers `[1,2,4]`; a paraphrased quote; a quote differing only by `’` vs `'`, `—` vs `-`, NBSP, or a line break (passes); `effect: "maybe"`; shareholder item with null `lead_filer`; director election with no nominees; `say_on_pay` marked `binding`; management item `precatory`; shareholder `charter_bylaw_amendment` marked `binding` (passes); missing `meeting.date_quote`.
- [ ] **Step 2: Run them.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** Expected: PASS, coverage ≥ 90%.
- [ ] **Step 5: Commit.** `[BR-15] NEW: add the four extraction checks`

### Task 6: The Claude client wrapper

**Files:**
- Create: `openboardroom/extract/llm.py`
- Create: `tests/llm_replay.py` (test helper: `sse_body(message: dict) -> bytes` turns a recorded Message into a Messages streaming response; `mock_client(handler) -> anthropic.Anthropic` with `http_client=httpx.Client(transport=httpx.MockTransport(handler))`, a fake API key, and `max_retries=0`)
- Create: `tests/fixtures/llm/{refusal,max-tokens,batch-results}.json`
- Test: `tests/extract/test_llm.py`

**Interfaces:**
- Consumes: `system_prompt`, `output_schema`, `user_message`, versions (Task 4).
- Produces:
```python
@dataclass(frozen=True)
class LlmSettings:
    model: str
    effort: str
    max_tokens: int

@dataclass(frozen=True)
class Outcome:
    status: Literal["succeeded", "refused", "failed"]
    output: dict[str, object] | None   # parsed JSON when succeeded
    model: str | None                  # model that produced it (fallbacks may differ)
    input_tokens: int | None
    output_tokens: int | None
    detail: str | None                 # why refused or failed; request id when known

def request_params(settings: LlmSettings, text: str, candidates: list[Candidate]) -> dict[str, object]
def outcome_from_message(message: anthropic.types.Message | anthropic.types.beta.BetaMessage) -> Outcome
def run_direct(client: anthropic.Anthropic, settings: LlmSettings, text: str, candidates: list[Candidate]) -> Outcome
def submit_batch(client: anthropic.Anthropic, settings: LlmSettings, requests: list[tuple[str, str, list[Candidate]]]) -> str   # (custom_id, text, candidates) → batch id
def batch_status(client: anthropic.Anthropic, batch_id: str) -> Literal["in_progress", "ended"]
def batch_outcomes(client: anthropic.Anthropic, batch_id: str) -> dict[str, Outcome | None]   # None = errored/expired/canceled: retry direct
```
- `request_params`: `model`, `max_tokens`, `system` (prompt), one user message, `output_config={"effort": effort, "format": {"type": "json_schema", "schema": ...}}`; no `thinking` key; no `temperature`.
- `run_direct`: `client.beta.messages.stream(**params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")` then `.get_final_message()`. API errors become `failed` with the error class and request id; never raise for one filing.
- `outcome_from_message`: `refusal` → `refused` (category and explanation in `detail`); `max_tokens` → `failed`; `end_turn` with a text block parsing as a JSON object → `succeeded`; anything else → `failed`.
- `custom_id` is the filing accession (unique, ≤64 chars, matches the batch id pattern).

- [ ] **Step 1: Write the failing tests** against the real SDK with `MockTransport`:
  - `request_params` shape: no `thinking`, effort and schema present, model from settings.
  - `run_direct` on the AAPL fixture (SSE) → `succeeded`, output equals the fixture JSON, token counts from usage; the request the transport received has the fallback beta header and `"fallbacks": "default"`.
  - refusal fixture → `refused`; max-tokens fixture → `failed`; HTTP 500 and 400 → `failed` with detail; a text block that isn't JSON → `failed`.
  - `submit_batch` posts one request per filing with `custom_id` = accession and no `fallbacks`; `batch_status`; `batch_outcomes` maps succeeded / refusal / errored / expired results (fixture JSONL) to `Outcome`, `Outcome(refused)`, `None`, `None`, keyed by `custom_id` whatever the order.
- [ ] **Step 2: Run them.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** PASS, coverage ≥ 90%.
- [ ] **Step 5: Commit.** `[BR-15] NEW: add the Claude extraction client with batches and fallback`

### Task 7: The extract stage

**Files:**
- Create: `openboardroom/extract/store.py` (all SQL for this stage)
- Create: `openboardroom/pipeline/extract.py`
- Test: `tests/pipeline/test_extract.py`, `tests/extract/test_store.py`

**Interfaces:**
- Consumes: everything above; `filings`, `meetings`, `ballot_items`, `nominees`, `item_texts`, `extraction_runs`, `check_results`, `index_memberships`.
- Produces:
```python
@dataclass(frozen=True)
class ExtractResult:
    checked: int; in_review: int; out_of_scope: int; failed: int; submitted: int
    errors: list[str]

def extract_direct(conn: psycopg.Connection, client: anthropic.Anthropic, settings: LlmSettings, text_dir: Path, limit: int | None = None, accession: str | None = None) -> ExtractResult
def submit(conn: psycopg.Connection, client: anthropic.Anthropic, settings: LlmSettings, text_dir: Path, limit: int | None = None) -> ExtractResult
def collect(conn: psycopg.Connection, client: anthropic.Anthropic, settings: LlmSettings, text_dir: Path) -> ExtractResult
```
- Selection: filings with `form = 'DEF 14A'` and `stage = 'fetched'` that have no `extraction_runs` row with `status = 'submitted'`, in id order. DEFA14A extraction is not in this plan (it produces pending changes; Plan 3).
- Per filing, one transaction:
  1. read `text_dir/{accession}.txt`; run `find_candidates`; insert a `regex` run (`succeeded`, output = candidates JSON).
  2. LLM: insert an `llm` run with model, effort, `PROMPT_VERSION`, `SCHEMA_VERSION`, tokens, output, status, detail (`batch_id` for submitted runs).
  3. `failed` → filing stays `fetched` (retried next time). `refused` → stage `in_review`, nothing else written.
  4. `succeeded` → **membership check**: if the company has no `index_memberships` row covering `meeting.date` (start ≤ date ≤ end or end null), stage `out_of_scope` and write nothing else; a null or unparsable date fails the consistency check instead and goes on to step 5.
  5. run checks; insert four `check_results`; write the meeting (`kind`, `meeting_date`, `meeting_time`, `format`, `record_date`, `shares_outstanding`, `date_quote`), ballot items (`number` as text, every §8.1 field), nominees (1-based `position`), and three `item_texts` per item (`summary`, `proponent_argument`, `board_argument`; `version` 1, `origin` `model`, `review_status` `pending`; null body when absent). Items are written even when a check fails, so the reviewer sees them; `published_at` stays null.
  6. stage `checked` if all four checks passed, else `in_review`.
- `collect`: for each batch with `submitted` runs, if `ended`, apply results as above; a `None` or `refused` outcome is retried once with `run_direct`, and that result is final.

- [ ] **Step 1: Write the failing tests** (real Postgres; client via `mock_client` replaying fixtures; a company with a membership covering 2024-02-28 and a fetched AAPL 2024 filing whose text file is the Task 3 fixture):
  - direct happy path: stage `checked`; 1 meeting; 8 items; nominees for item 1 in order; 24 item texts all `pending`; 2 runs; 4 passing check rows.
  - check failure (fixture with a paraphrased quote): stage `in_review`, items written, the `quotes` check row failed with detail.
  - refusal: stage `in_review`, no meeting or items, `refused` run with detail.
  - API failure and `max_tokens`: stage stays `fetched`, `failed` run, nothing else.
  - out of scope: membership ends 2023-12-31 → stage `out_of_scope`, no meeting.
  - idempotence: a second `extract_direct` picks nothing; a filing with a `submitted` run is skipped by `extract_direct` and `submit`.
  - rollback: a forced DB error mid-write (e.g. an item number violating `unique (meeting_id, number)` from a duplicated item) leaves the filing `fetched` with no partial rows.
  - batch: `submit` records `submitted` runs with the batch id; `collect` before `ended` changes nothing; after `ended` applies succeeded results, retries an errored one directly, and marks a refused-again one `in_review`.
- [ ] **Step 2: Run them.** Expected: FAIL. **Step 3: Implement.** **Step 4: Run.** PASS, coverage ≥ 90% for `extract/store.py` and `pipeline/extract.py`.
- [ ] **Step 5: Commit.** `[BR-15] NEW: add the extract stage`

### Task 8: `obr extract`, configuration, and the runbook

**Files:**
- Modify: `openboardroom/config.py`, `openboardroom/cli.py`, `README.md` (pipeline runbook), `.env.example` (variable names only)
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `extract_settings_from_env(env) -> LlmSettings` (defaults in Global Constraints; effort must be one of `low|medium|high|xhigh|max`; max tokens a positive integer). CLI:
  - `obr extract` (direct mode), options `--limit N`, `--filing ACCESSION`;
  - `obr extract --submit [--limit N]` (batch);
  - `obr extract --collect`.
  - Exit code 1 if any filing failed. Prints one line of counts and one line per failure to stderr. Never prints the API key or request bodies.

- [ ] **Step 1: Write the failing tests.** Settings defaults and each invalid value; CLI parsing for the three modes; `--submit` and `--collect` are mutually exclusive; a missing `ANTHROPIC_API_KEY` gives a clear error (the SDK's) without a traceback.
- [ ] **Step 2: Run, implement, run.** PASS.
- [ ] **Step 3: Runbook.** README section "Extract": the commands, how Quinn passes the key per command (`ANTHROPIC_API_KEY="$(security find-generic-password -s <keychain-item> -w)" uv run obr extract --filing …`), what each stage means, and that batches return within 24 hours.
- [ ] **Step 4: Run the whole suite.** `uv run pytest -q --cov` with coverage gates, `uv run ruff format --check`, `uv run ruff check`, `uv run pyright`. All pass.
- [ ] **Step 5: Commit.** `[BR-15] NEW: add obr extract`

### Task 9: Live smoke run (Quinn) and handoff

- [ ] **Step 1: Ask Quinn to approve the spend** (two direct calls at effort high: about 110K and 160K input tokens; estimate from the cost log, about $1–2 total **(verify against `architecture/docs/planning/05-cost-tracking.md`)**), then Quinn runs, against a local database with the companies loaded and the two filings fetched:
  `ANTHROPIC_API_KEY="$(security find-generic-password -s <keychain-item> -w)" uv run obr extract --filing 0001308179-24-000010` and the same for `0001193125-23-259247`.
- [ ] **Step 2: Read the results:** each filing's stage, the check rows, item counts (8 and 13), token counts. A real check failure is a finding to fix or to record as a known limitation for the eval set (Plan 5), not to hide. Append the token counts and cost to the architecture cost log (separate commit there).
- [ ] **Step 3: Review.** `superpowers:requesting-code-review` on the branch; fix findings.
- [ ] **Step 4: PR draft** with Summary, Verification (suite, coverage, smoke results), and a **Neutrality review** of the prompt. Ask Quinn before pushing.
