# Backend Foundation Implementation Plan (Plan 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public-ready `backend` repo that creates the full database schema and published views, and discovers and fetches S&P 100 proxy filings and vote-result 8-Ks from EDGAR into it.

**Architecture:** A Python package `openboardroom` with a `obr` CLI. Each pipeline stage is a function taking a database connection and advancing filings by `filings.stage`; Plan 1 builds the schema, the view contract, and the first two stages (`discover`, `fetch`). Plain SQL with psycopg 3; dbmate migrations; no ORM.

**Tech Stack:** Python 3.13, uv, psycopg 3, httpx, beautifulsoup4 + lxml, dbmate, pytest + pytest-cov + testcontainers (Postgres 17), ruff, pyright strict, lefthook, gitleaks.

**Spec:** [architecture/docs/specs/2026-11-21-ballot-item-pipeline-design.md](https://github.com/OpenBoardroom/architecture/blob/main/docs/specs/2026-11-21-ballot-item-pipeline-design.md), with [ADR-0003](https://github.com/OpenBoardroom/architecture/blob/main/docs/adr/0003-ballot-item-schema-contract.md) for the view contract. Ticket: BR-11.

**The five plans:** (1) foundation, this plan; (2) extraction and checks; (3) review CLI, publication, corrections; (4) 8-K reconciliation; (5) eval set and 2026 backfill. Each starts from the same spec.

## Global Constraints

- Repo is **public** and licensed **AGPL-3.0-only**: `LICENSE` byte-identical to `architecture/LICENSE` (sha256 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`).
- EDGAR: at most **10 requests per second** (default 8); User-Agent from configuration (`OBR_EDGAR_USER_AGENT`), never hard-coded, never defaulted; raw filings cached on disk, never committed.
- Filings are untrusted input: parsed as text only, never executed.
- SQL lowercase snake_case; every migration has a working `-- migrate:down`.
- Tests run against real Postgres 17 via Testcontainers; query logic is never mocked. HTTP may be faked with `httpx.MockTransport`.
- Coverage: 90% line and branch on `openboardroom/` (all of Plan 1 is "parsing, extraction, database writes").
- Commits `[BR-11] TYPE: message` with the `Co-Authored-By` trailer; branch `task/BR-11`.
- `.claude/settings.json` byte-identical to `architecture/templates/claude-settings.json`; `CLAUDE.md` from `architecture/templates/CLAUDE.md`.

## Review Focus

1. **EDGAR rejects the request (403 for a missing or blocked User-Agent, 429 for rate).** Expect a clear error after bounded retries, not an endless loop or a silent empty result. Test in Task 6.
2. **A covered company files nothing in the window** (meeting moved, company left the index). Expect zero new filings for it and no error. Test in Task 7.
3. **A primary document that isn't UTF-8** (EDGAR has windows-1252 documents). Expect correct text, not mojibake or a crash. Test in Task 8.
4. **A run killed mid-download.** Expect the rerun to fetch again rather than trust a partial cache file. Test in Task 8.
5. **A company in the index for only part of the window.** Expect discovery only for filings dated inside its membership. Test in Task 7.

---

### Task 1: Repository scaffold, tooling, and CI

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `openboardroom/__init__.py`, `openboardroom/cli.py`, `tests/test_cli.py`
- Create: `LICENSE`, `README.md`, `CLAUDE.md`, `.claude/settings.json`, `.gitignore`, `lefthook.yml`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: console script `obr` → `openboardroom.cli:main(argv: list[str] | None = None) -> int`, using `argparse` subcommands. Later tasks add subcommands with `register(subparsers)` functions.

- [ ] **Step 1: Write the failing test** `tests/test_cli.py::test_version_prints_package_version`: `main(["--version"])` raises `SystemExit(0)` and stdout equals `f"obr {openboardroom.__version__}\n"`.
- [ ] **Step 2:** Run `uv run pytest tests/test_cli.py -v`. Expected: FAIL (module not found).
- [ ] **Step 3: Scaffold.**
  - `pyproject.toml`: `requires-python = ">=3.13"`, runtime deps `psycopg[binary]`, `httpx`, `beautifulsoup4`, `lxml`; dev group `pytest`, `pytest-cov`, `testcontainers[postgres]`, `ruff`, `pyright`. Ruff: line length 100, rules `E,F,I,B,UP,SIM,D` with Google docstring convention. Pyright `strict`. Coverage `fail_under = 90`, `branch = true`.
  - `openboardroom/__init__.py`: `__version__ = "0.1.0"`; `cli.py` with `--version`.
  - `LICENSE`: copy `../architecture/LICENSE`. `README.md`: purpose, setup (`uv sync`, dbmate, Docker for tests), and a License section (AGPL-3.0-only; generated content license per ADR-0002).
  - `CLAUDE.md` from the template: commands (`uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `dbmate`), style (Google docstrings, plain SQL), contracts (ADR-0003 views, `db/schema.sql`), pitfalls (EDGAR etiquette; never commit `cache/`).
  - `.gitignore`: `cache/`, `.venv/`, `.env*` except `.env.example`, `__pycache__/`, `.coverage`.
  - `lefthook.yml` pre-commit: ruff format check, ruff check, pyright, `gitleaks protect --staged`.
  - `ci.yml` on `pull_request` and `push` to `main`: setup uv; `uv sync`; ruff format check; ruff check; pyright; install dbmate (pinned release binary); `uv run pytest --cov=openboardroom`; `gitleaks/gitleaks-action` (pinned).
- [ ] **Step 4:** Run `uv run pytest -v && uv run ruff check && uv run ruff format --check && uv run pyright`. Expected: all pass, 0 pyright errors.
- [ ] **Step 5:** `shasum -a 256 LICENSE` matches the Global Constraints value; `cmp .claude/settings.json ../architecture/templates/claude-settings.json` exits 0.
- [ ] **Step 6:** Commit `[BR-11] NEW: scaffold backend package, tooling, and CI`.

### Task 2: Configuration

**Files:**
- Create: `openboardroom/config.py`, `tests/test_config.py`, `.env.example`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Settings` with `database_url: str`, `edgar_user_agent: str`, `edgar_max_rps: float`, `cache_dir: Path`; `Settings.from_env(env: Mapping[str, str] = os.environ) -> Settings`; `class ConfigError(Exception)`.
- Env names: `OBR_DATABASE_URL` (required), `OBR_EDGAR_USER_AGENT` (required), `OBR_EDGAR_MAX_RPS` (default `8`), `OBR_CACHE_DIR` (default `./cache`).

- [ ] **Step 1: Write the failing tests:** `test_missing_user_agent_raises` (`ConfigError` naming `OBR_EDGAR_USER_AGENT`); `test_rps_above_ten_raises` (`"10.5"` → `ConfigError`); `test_defaults` (`edgar_max_rps == 8.0`, `cache_dir == Path("cache")`).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement `Settings.from_env`. `.env.example` lists the four names with placeholder values (`OBR_EDGAR_USER_AGENT=Organization Name contact@example.org`).
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `[BR-11] NEW: add environment configuration`.

### Task 3: Schema migrations and test database

**Files:**
- Create: `db/migrations/20261121000001_core_tables.sql`, `db/migrations/20261121000002_review_and_results.sql`, `tests/conftest.py`, `tests/db/test_schema.py`

**Interfaces:**
- Produces: pytest fixtures `pg_url: str` (session: Postgres 17 container, roles `pipeline_rw` and `web_ro` created with passwords, `dbmate up` run) and `conn: psycopg.Connection` (function scope, as `pipeline_rw`, rolled back after each test).
- Tables and columns exactly as spec §8.1, plus `vote_results.published_at timestamptz` (results publish only after reconciliation passes, spec §6.5).
- Vocabularies as `text` + `CHECK` (ADR-0003), values from spec §8.2: `type` (`management`, `shareholder`); `category` (`director_election`, `say_on_pay`, `say_on_pay_frequency`, `auditor_ratification`, `equity_plan`, `charter_bylaw_amendment`, `shareholder_proposal`, `other`); `effect` (`binding`, `advisory`, `precatory`); `board_recommendation` (`for`, `against`, `abstain`, `none`, `for_each_nominee`, `one_year`, `two_years`, `three_years`); `vote_base` (`votes_cast`, `shares_present_and_voting`, `shares_present`, `shares_outstanding`, `plurality`, `other`); `filings.form` (`DEF 14A`, `DEFA14A`, `8-K`); `filings.stage` (`discovered`, `fetched`, `extracted`, `checked`, `in_review`, `published`, `reconciled`); `meetings.kind` (`annual`, `special`); `meetings.format` (`virtual`, `in_person`, `hybrid`); `ballot_items.status` (`active`, `withdrawn`); `item_texts.kind` (`summary`, `proponent_argument`, `board_argument`); `item_texts.origin` (`model`, `reviewer`); `item_texts.review_status` (`pending`, `approved`, `rejected`, `needs_manual`); `pending_changes.reason` (`rerun`, `supplement`, `reconciliation`); `pending_changes.status` (`pending`, `applied`, `rejected`).
- Uniques: `companies.cik`, `filings.accession`, `ballot_items (meeting_id, number)`, `item_texts (ballot_item_id, kind, version)`.
- Grants: `pipeline_rw` gets `select, insert, update, delete` on all tables and sequence usage; `web_ro` gets nothing on tables.

- [ ] **Step 1: Write the failing tests** in `tests/db/test_schema.py`: `test_category_rejects_unknown_value` (insert `category='merger'` → `psycopg.errors.CheckViolation`); `test_accession_is_unique` (`UniqueViolation`); `test_item_number_unique_per_meeting`; `test_migrations_round_trip` (`dbmate down` ×2 then `up` on a fresh database succeeds; table list matches before and after).
- [ ] **Step 2:** Run `uv run pytest tests/db -v`. Expected: FAIL (no migrations).
- [ ] **Step 3:** Write both migrations with `-- migrate:up` and `-- migrate:down`, and the fixtures in `conftest.py` (dbmate invoked via `subprocess` with `--url pg_url --migrations-dir db/migrations --no-dump-schema`).
- [ ] **Step 4:** Run `uv run pytest tests/db -v`. Expected: PASS.
- [ ] **Step 5:** Commit `[BR-11] NEW: add core schema migrations and test database`.

### Task 4: Published views and the contract test

**Files:**
- Create: `db/migrations/20261121000003_published_views.sql`, `db/schema.sql`, `tests/contract/views.json`, `tests/db/test_views.py`, `tests/contract/test_contract.py`

**Interfaces:**
- Produces: views `published_meetings`, `published_ballot_items`, `published_item_texts`, `published_vote_results`, `public_corrections` with `select` granted to `web_ro` only (ADR-0003). `tests/contract/views.json` maps each view to its column list; it is the contract file `frontend` reads alongside `db/schema.sql`.
- Publication rules:
  - `published_ballot_items`: `ballot_items.published_at is not null`, joined to meeting, company (`cik`, `ticker`, `name`), and filing `source_url`; columns include every fact field from spec §8.1, the four quotes, `status`, `published_at`, and `has_pending_change` (`exists` a `pending_changes` row with `status='pending'`).
  - `published_item_texts`: for published items, the highest `version` per `(ballot_item_id, kind)` among rows with `review_status='approved'`.
  - `published_meetings`: meetings with at least one published item.
  - `published_vote_results`: rows with `published_at is not null` for published items.
  - `public_corrections`: all `corrections` rows, joined to item number and title, company ticker, and meeting date.

- [ ] **Step 1: Write the failing tests** in `tests/db/test_views.py`:
  - `test_unpublished_item_hidden` (item with `published_at null` absent from `published_ballot_items`)
  - `test_only_latest_approved_text` (versions 1 approved, 2 approved, 3 pending → view returns version 2)
  - `test_pending_change_flagged_value_unchanged` (pending change on `board_recommendation` → `has_pending_change` true, value still the original)
  - `test_web_ro_cannot_read_tables` (as `web_ro`, `select` on `ballot_items` → `InsufficientPrivilege`; on `published_ballot_items` → succeeds)
- [ ] **Step 2: Write the failing contract test** `tests/contract/test_contract.py::test_views_keep_contract_columns`: for each view in `views.json`, its columns from `information_schema.columns` are a superset of the listed ones (additions allowed, removals and renames fail, per ADR-0003 §6).
- [ ] **Step 3:** Run `uv run pytest tests/db/test_views.py tests/contract -v`. Expected: FAIL.
- [ ] **Step 4:** Write the views migration; generate `db/schema.sql` with `dbmate dump`; write `views.json` from the created views.
- [ ] **Step 5:** Add a CI step that runs `dbmate up && dbmate dump` against a service Postgres and fails if `git diff --exit-code db/schema.sql` shows changes.
- [ ] **Step 6:** Run `uv run pytest -v`. Expected: PASS.
- [ ] **Step 7:** Commit `[BR-11] NEW: add published views and view contract test`.

### Task 5: Covered companies

**Files:**
- Create: `data/companies.csv`, `openboardroom/companies.py`, `tests/test_companies.py`, `tests/fixtures/companies_small.csv`

**Interfaces:**
- Consumes: `conn` fixture; tables `companies`, `index_memberships`.
- Produces: `sync_companies(conn: psycopg.Connection, csv_path: Path) -> SyncResult` (`@dataclass SyncResult(added: int, updated: int)`); `covered_companies(conn, index: str, start: date, end: date) -> list[CoveredCompany]` where `CoveredCompany(company_id: int, cik: str, ticker: str, membership_start: date, membership_end: date | None)`; CLI `obr companies sync [--csv PATH]`.
- CSV columns: `cik,ticker,name,index,start_date,end_date` (`cik` zero-padded to 10 digits; `end_date` empty when current). The file's first line is a comment `# source: <where the list came from>, as of <date>`.

- [ ] **Step 1: Write the failing tests:** `test_sync_is_idempotent` (second run → `SyncResult(0, 0)`); `test_ticker_change_updates_row`; `test_partial_membership_overlap` (membership 2026-03-01..2026-06-30 included for window 2026-01-01..2026-12-31, excluded for 2026-07-01..2026-12-31).
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement `sync_companies` (upsert on `cik`; memberships upsert on `(company_id, index, start_date)`) and `covered_companies` (overlap: `start_date <= end and (end_date is null or end_date >= start)`).
- [ ] **Step 4:** Populate `data/companies.csv` with the S&P 100 as used for the 2026 season, CIKs checked against EDGAR's company tickers file; record the source and date in the comment line **(verify the constituent source with Quinn)**.
- [ ] **Step 5:** Run them. Expected: PASS.
- [ ] **Step 6:** Commit `[BR-11] NEW: add covered company list and sync`.

### Task 6: EDGAR client

**Files:**
- Create: `openboardroom/edgar/__init__.py`, `openboardroom/edgar/client.py`, `openboardroom/edgar/ratelimit.py`, `tests/edgar/test_client.py`

**Interfaces:**
- Consumes: `Settings` (Task 2).
- Produces:
  - `class RateLimiter(max_per_second: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)` with `wait() -> None`.
  - `class EdgarClient(settings: Settings, transport: httpx.BaseTransport | None = None, limiter: RateLimiter | None = None)` with `submissions(cik: str) -> dict[str, Any]` (`https://data.sec.gov/submissions/CIK{cik}.json`), `submissions_page(name: str) -> dict[str, Any]` (`https://data.sec.gov/submissions/{name}`), and `document(cik: str, accession: str, primary_document: str) -> bytes` (`https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession without dashes}/{primary_document}`).
  - `class EdgarError(Exception)`.
- Every request sends `User-Agent: settings.edgar_user_agent` and goes through the limiter. 429 and 5xx retry up to 3 times with exponential backoff (1, 2, 4 seconds via the injected `sleep`); 403 and other 4xx raise `EdgarError` at once with the status and URL.

- [ ] **Step 1: Write the failing tests** (with `httpx.MockTransport` and a fake clock):
  - `test_sends_configured_user_agent`
  - `test_rate_limiter_spaces_requests` (10 calls at 8 rps → fake clock advanced ≥ 1.125 s)
  - `test_403_raises_without_retry` (one request made; `EdgarError` mentions 403)
  - `test_429_retries_then_raises` (4 requests made, then `EdgarError`)
  - `test_5xx_then_success_returns_body`
- [ ] **Step 2:** Run `uv run pytest tests/edgar -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `ratelimit.py` and `client.py`.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `[BR-11] NEW: add rate-limited EDGAR client`.

### Task 7: Discover

**Files:**
- Create: `openboardroom/pipeline/__init__.py`, `openboardroom/pipeline/discover.py`, `tests/pipeline/test_discover.py`, `tests/fixtures/edgar/submissions_aapl.json` (trimmed copy of Apple's submissions JSON: the 2023–2026 DEF 14A, DEFA14A, and 8-K rows, including at least one 8-K without `5.07`), `tests/fixtures/edgar/submissions_page.json`

**Interfaces:**
- Consumes: `EdgarClient` (Task 6), `covered_companies` (Task 5).
- Produces: `discover(conn: psycopg.Connection, client: EdgarClient, start: date, end: date, index: str = "sp100") -> DiscoverResult` (`@dataclass DiscoverResult(new: int, existing: int)`); CLI `obr discover --since YYYY-MM-DD --until YYYY-MM-DD`.
- Rules (spec §6.1, §12):
  - Read `filings.recent` (parallel arrays: `form`, `filingDate`, `accessionNumber`, `primaryDocument`, `items`); if `start` is earlier than the oldest `filingDate` in `recent`, also read each page in `filings.files` whose `filingTo >= start`.
  - Keep `DEF 14A` and `DEFA14A`; keep `8-K` only when `"5.07"` is in its comma-separated `items`.
  - Keep only filings dated inside both the window and the company's membership.
  - Insert with `stage='discovered'`, `source_url` built as in `EdgarClient.document`; `on conflict (accession) do nothing` counts as `existing`.

- [ ] **Step 1: Write the failing tests:**
  - `test_discovers_proxy_supplement_and_results` (Apple fixture, window 2026-01-01..2026-12-31 → the 2026-01-08 DEF 14A and DEFA14A and the 2026-02-24 8-K; `new == 3`)
  - `test_8k_without_507_ignored`
  - `test_rerun_is_noop` (`DiscoverResult(new=0, existing=3)`)
  - `test_company_with_no_filings_in_window` (`new == 0`, no error)
  - `test_reads_paged_file_for_older_window` (window in 2015 → `submissions_page` called)
  - `test_membership_limits_window`
- [ ] **Step 2:** Run `uv run pytest tests/pipeline/test_discover.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `discover` and the CLI subcommand.
- [ ] **Step 4:** Run them. Expected: PASS.
- [ ] **Step 5:** Commit `[BR-11] NEW: add EDGAR filing discovery`.

### Task 8: Fetch and plain text

**Files:**
- Create: `openboardroom/pipeline/fetch.py`, `openboardroom/text.py`, `tests/pipeline/test_fetch.py`, `tests/test_text.py`, `tests/fixtures/filings/aapl-2024-def14a.htm` (trimmed to under 200 KB, keeping the cover page and Proposals 1, 4, and 8), `tests/fixtures/filings/aapl-2024-def14a.expected.txt`, `tests/fixtures/filings/cp1252.htm`

**Interfaces:**
- Consumes: `EdgarClient.document`, `Settings.cache_dir`.
- Produces: `to_text(html: bytes) -> str` in `openboardroom/text.py`; `fetch(conn: psycopg.Connection, client: EdgarClient, cache_dir: Path) -> FetchResult` (`@dataclass FetchResult(fetched: int, failed: int)`); CLI `obr fetch`.
- `to_text`: decode by the document's declared charset, else UTF-8, else cp1252; parse with BeautifulSoup `lxml`; `get_text("\n")`; collapse runs of whitespace within each line; drop empty lines; join with `\n`; trailing newline. Same method as the spike.
- `fetch`: for each `stage='discovered'` filing: download to `cache_dir/raw/{accession}.part`, rename to `{accession}.htm` only after the full body is written; record `raw_sha256`; write `cache_dir/text/{accession}.txt`; record `text_sha256`; set `stage='fetched'`. A download error leaves the filing at `discovered` and counts as `failed`. An existing `.htm` whose sha256 matches `raw_sha256` is reused without a request.

- [ ] **Step 1: Write the failing tests:**
  - `tests/test_text.py::test_golden_apple_2024` (`to_text(fixture) == expected.txt`)
  - `test_cp1252_document_decodes` (a `’` encoded as `0x92` comes out as `’`)
  - `tests/pipeline/test_fetch.py::test_fetch_advances_stage_and_records_hashes`
  - `test_partial_download_is_refetched` (a leftover `.part` file is ignored; the document is requested again)
  - `test_cached_document_not_refetched` (second run makes no request)
  - `test_download_error_leaves_filing_discovered`
- [ ] **Step 2:** Run `uv run pytest tests/test_text.py tests/pipeline/test_fetch.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `to_text`, `fetch`, and the CLI subcommand.
- [ ] **Step 4:** Run `uv run pytest --cov=openboardroom -v && uv run pyright && uv run ruff check`. Expected: all pass; coverage ≥ 90%.
- [ ] **Step 5:** Commit `[BR-11] NEW: add filing fetch and plain-text conversion`.

### Task 9: End-to-end check against EDGAR (manual)

- [ ] **Step 1:** With `OBR_EDGAR_USER_AGENT="Example Org contact@example.org"` and a local Postgres (`dbmate up`), run `uv run obr companies sync && uv run obr discover --since 2026-01-01 --until 2026-12-31 && uv run obr fetch`. Expected: about 100 DEF 14A filings, their DEFA14As, and about 100 results 8-Ks at `stage='fetched'`; no `EdgarError`; no request rate above 8 per second.
- [ ] **Step 2:** Record the counts per form, run time, and any failures in the PR description.
- [ ] **Step 3:** Ask Quinn before creating the GitHub repo (`gh repo create OpenBoardroom/backend --public`), pushing, and opening the PR.
