# Backend Foundation Implementation Plan (Plan 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public-ready `backend` repo that creates the full database schema and published views, and discovers and fetches S&P 100 proxy filings and vote-result 8-Ks from EDGAR into it.

**Architecture:** A Python package `openboardroom` with a `obr` CLI. Each pipeline stage is a function taking a database connection and advancing filings by `filings.stage`; Plan 1 builds the schema, the view contract, and the first two stages (`discover`, `fetch`). Plain SQL with psycopg 3; dbmate migrations; no ORM.

**Tech Stack:** Python 3.13, uv, psycopg 3, httpx, beautifulsoup4 + lxml, dbmate, pytest + pytest-cov + testcontainers (Postgres 17), ruff, pyright strict, lefthook, gitleaks.

**Spec:** [architecture/docs/specs/2026-11-21-ballot-item-pipeline-design.md](https://github.com/OpenBoardroom/architecture/blob/main/docs/specs/2026-11-21-ballot-item-pipeline-design.md), with [ADR-0003](https://github.com/OpenBoardroom/architecture/blob/main/docs/adr/0003-ballot-item-schema-contract.md) for the view contract. Ticket: BR-11.

**Amended 2026-11-21:** Task 5b added and Task 7 amended after the spec made the company, not the EDGAR CIK, the unit [Quinn].

**Updated 2026-11-21 to match the implementation after review:** Review Focus #1 and #5, Task 6, Task 7 (rules and amendments), and Task 8 now describe the code as built; the original Task 7 rules are kept, struck through where superseded. Completed steps are ticked.

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

1. **EDGAR rejects the request (403 for a missing or blocked User-Agent, 429 for rate).** Expect a clear error after bounded retries, not an endless loop or a silent empty result. Test in Task 6. *(Updated 2026-11-21:)* `obr fetch` stops the whole run on 403, 429, 5xx, or a network error rather than trying every remaining filing; tests in Task 8.
2. **A covered company files nothing in the window** (meeting moved, company left the index). Expect zero new filings for it and no error. Test in Task 7.
3. **A primary document that isn't UTF-8** (EDGAR has windows-1252 documents). Expect correct text, not mojibake or a crash. Test in Task 8.
4. **A run killed mid-download.** Expect the rerun to fetch again rather than trust a partial cache file. Test in Task 8.
5. **A company in the index for only part of the window.** ~~Expect discovery only for filings dated inside its membership.~~ *(Updated 2026-11-21:)* Membership is judged at the meeting date, which discovery doesn't know yet, so expect discovery only for filings where one of the company's memberships overlaps [filed − 14 days, filed + 180 days]: a proxy filed shortly before a membership starts is kept, and so is a 5.07 8-K filed days after one ends. Test in Task 7.

---

### Task 1: Repository scaffold, tooling, and CI

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `openboardroom/__init__.py`, `openboardroom/cli.py`, `tests/test_cli.py`
- Create: `LICENSE`, `README.md`, `CLAUDE.md`, `.claude/settings.json`, `.gitignore`, `lefthook.yml`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: console script `obr` → `openboardroom.cli:main(argv: list[str] | None = None) -> int`, using `argparse` subcommands. Later tasks add subcommands with `register(subparsers)` functions.

- [x] **Step 1: Write the failing test** `tests/test_cli.py::test_version_prints_package_version`: `main(["--version"])` raises `SystemExit(0)` and stdout equals `f"obr {openboardroom.__version__}\n"`.
- [x] **Step 2:** Run `uv run pytest tests/test_cli.py -v`. Expected: FAIL (module not found).
- [x] **Step 3: Scaffold.**
  - `pyproject.toml`: `requires-python = ">=3.13"`, runtime deps `psycopg[binary]`, `httpx`, `beautifulsoup4`, `lxml`; dev group `pytest`, `pytest-cov`, `testcontainers[postgres]`, `ruff`, `pyright`. Ruff: line length 100, rules `E,F,I,B,UP,SIM,D` with Google docstring convention. Pyright `strict`. Coverage `fail_under = 90`, `branch = true`.
  - `openboardroom/__init__.py`: `__version__ = "0.1.0"`; `cli.py` with `--version`.
  - `LICENSE`: copy `../architecture/LICENSE`. `README.md`: purpose, setup (`uv sync`, dbmate, Docker for tests), and a License section (AGPL-3.0-only; generated content license per ADR-0002).
  - `CLAUDE.md` from the template: commands (`uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `dbmate`), style (Google docstrings, plain SQL), contracts (ADR-0003 views, `db/schema.sql`), pitfalls (EDGAR etiquette; never commit `cache/`).
  - `.gitignore`: `cache/`, `.venv/`, `.env*` except `.env.example`, `__pycache__/`, `.coverage`.
  - `lefthook.yml` pre-commit: ruff format check, ruff check, pyright, `gitleaks protect --staged`.
  - `ci.yml` on `pull_request` and `push` to `main`: setup uv; `uv sync`; ruff format check; ruff check; pyright; install dbmate (pinned release binary); `uv run pytest --cov=openboardroom`; `gitleaks/gitleaks-action` (pinned).
- [x] **Step 4:** Run `uv run pytest -v && uv run ruff check && uv run ruff format --check && uv run pyright`. Expected: all pass, 0 pyright errors.
- [x] **Step 5:** `shasum -a 256 LICENSE` matches the Global Constraints value; `cmp .claude/settings.json ../architecture/templates/claude-settings.json` exits 0.
- [x] **Step 6:** Commit `[BR-11] NEW: scaffold backend package, tooling, and CI`.

### Task 2: Configuration

**Files:**
- Create: `openboardroom/config.py`, `tests/test_config.py`, `.env.example`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Settings` with `database_url: str`, `edgar_user_agent: str`, `edgar_max_rps: float`, `cache_dir: Path`; `Settings.from_env(env: Mapping[str, str] = os.environ) -> Settings`; `class ConfigError(Exception)`.
- Env names: `OBR_DATABASE_URL` (required), `OBR_EDGAR_USER_AGENT` (required), `OBR_EDGAR_MAX_RPS` (default `8`), `OBR_CACHE_DIR` (default `./cache`).

- [x] **Step 1: Write the failing tests:** `test_missing_user_agent_raises` (`ConfigError` naming `OBR_EDGAR_USER_AGENT`); `test_rps_above_ten_raises` (`"10.5"` → `ConfigError`); `test_defaults` (`edgar_max_rps == 8.0`, `cache_dir == Path("cache")`).
- [x] **Step 2:** Run them. Expected: FAIL.
- [x] **Step 3:** Implement `Settings.from_env`. `.env.example` lists the four names with placeholder values (`OBR_EDGAR_USER_AGENT=Organization Name contact@example.org`).
- [x] **Step 4:** Run them. Expected: PASS.
- [x] **Step 5:** Commit `[BR-11] NEW: add environment configuration`.

### Task 3: Schema migrations and test database

**Files:**
- Create: `db/migrations/20261121000001_core_tables.sql`, `db/migrations/20261121000002_review_and_results.sql`, `tests/conftest.py`, `tests/db/test_schema.py`

**Interfaces:**
- Produces: pytest fixtures `pg_url: str` (session: Postgres 17 container, roles `pipeline_rw` and `web_ro` created with passwords, `dbmate up` run) and `conn: psycopg.Connection` (function scope, as `pipeline_rw`, rolled back after each test).
- Tables and columns exactly as spec §8.1, plus `vote_results.published_at timestamptz` (results publish only after reconciliation passes, spec §6.5).
- Vocabularies as `text` + `CHECK` (ADR-0003), values from spec §8.2: `type` (`management`, `shareholder`); `category` (`director_election`, `say_on_pay`, `say_on_pay_frequency`, `auditor_ratification`, `equity_plan`, `charter_bylaw_amendment`, `shareholder_proposal`, `other`); `effect` (`binding`, `advisory`, `precatory`); `board_recommendation` (`for`, `against`, `abstain`, `none`, `for_each_nominee`, `one_year`, `two_years`, `three_years`); `vote_base` (`votes_cast`, `shares_present_and_voting`, `shares_present`, `shares_outstanding`, `plurality`, `other`); `filings.form` (`DEF 14A`, `DEFA14A`, `8-K`); `filings.stage` (`discovered`, `fetched`, `extracted`, `checked`, `in_review`, `published`, `reconciled`); `meetings.kind` (`annual`, `special`); `meetings.format` (`virtual`, `in_person`, `hybrid`); `ballot_items.status` (`active`, `withdrawn`); `item_texts.kind` (`summary`, `proponent_argument`, `board_argument`); `item_texts.origin` (`model`, `reviewer`); `item_texts.review_status` (`pending`, `approved`, `rejected`, `needs_manual`); `pending_changes.reason` (`rerun`, `supplement`, `reconciliation`); `pending_changes.status` (`pending`, `applied`, `rejected`).
- Uniques: `companies.cik`, `filings.accession`, `ballot_items (meeting_id, number)`, `item_texts (ballot_item_id, kind, version)`.
- Grants: `pipeline_rw` gets `select, insert, update, delete` on all tables and sequence usage; `web_ro` gets nothing on tables.

- [x] **Step 1: Write the failing tests** in `tests/db/test_schema.py`: `test_category_rejects_unknown_value` (insert `category='merger'` → `psycopg.errors.CheckViolation`); `test_accession_is_unique` (`UniqueViolation`); `test_item_number_unique_per_meeting`; `test_migrations_round_trip` (`dbmate down` ×2 then `up` on a fresh database succeeds; table list matches before and after).
- [x] **Step 2:** Run `uv run pytest tests/db -v`. Expected: FAIL (no migrations).
- [x] **Step 3:** Write both migrations with `-- migrate:up` and `-- migrate:down`, and the fixtures in `conftest.py` (dbmate invoked via `subprocess` with `--url pg_url --migrations-dir db/migrations --no-dump-schema`).
- [x] **Step 4:** Run `uv run pytest tests/db -v`. Expected: PASS.
- [x] **Step 5:** Commit `[BR-11] NEW: add core schema migrations and test database`.

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

- [x] **Step 1: Write the failing tests** in `tests/db/test_views.py`:
  - `test_unpublished_item_hidden` (item with `published_at null` absent from `published_ballot_items`)
  - `test_only_latest_approved_text` (versions 1 approved, 2 approved, 3 pending → view returns version 2)
  - `test_pending_change_flagged_value_unchanged` (pending change on `board_recommendation` → `has_pending_change` true, value still the original)
  - `test_web_ro_cannot_read_tables` (as `web_ro`, `select` on `ballot_items` → `InsufficientPrivilege`; on `published_ballot_items` → succeeds)
- [x] **Step 2: Write the failing contract test** `tests/contract/test_contract.py::test_views_keep_contract_columns`: for each view in `views.json`, its columns from `information_schema.columns` are a superset of the listed ones (additions allowed, removals and renames fail, per ADR-0003 §6).
- [x] **Step 3:** Run `uv run pytest tests/db/test_views.py tests/contract -v`. Expected: FAIL.
- [x] **Step 4:** Write the views migration; generate `db/schema.sql` with `dbmate dump`; write `views.json` from the created views.
- [x] **Step 5:** Add a CI step that runs `dbmate up && dbmate dump` against a service Postgres and fails if `git diff --exit-code db/schema.sql` shows changes.
- [x] **Step 6:** Run `uv run pytest -v`. Expected: PASS.
- [x] **Step 7:** Commit `[BR-11] NEW: add published views and view contract test`.

### Task 5: Covered companies

**Files:**
- Create: `data/companies.csv`, `openboardroom/companies.py`, `tests/test_companies.py`, `tests/fixtures/companies_small.csv`

**Interfaces:**
- Consumes: `conn` fixture; tables `companies`, `index_memberships`.
- Produces: `sync_companies(conn: psycopg.Connection, csv_path: Path) -> SyncResult` (`@dataclass SyncResult(added: int, updated: int)`); `covered_companies(conn, index: str, start: date, end: date) -> list[CoveredCompany]` where `CoveredCompany(company_id: int, cik: str, ticker: str, membership_start: date, membership_end: date | None)`; CLI `obr companies sync [--csv PATH]`.
- CSV columns: `cik,ticker,name,index,start_date,end_date` (`cik` zero-padded to 10 digits; `end_date` empty when current). The file's first line is a comment `# source: <where the list came from>, as of <date>`.

- [x] **Step 1: Write the failing tests:** `test_sync_is_idempotent` (second run → `SyncResult(0, 0)`); `test_ticker_change_updates_row`; `test_partial_membership_overlap` (membership 2026-03-01..2026-06-30 included for window 2026-01-01..2026-12-31, excluded for 2026-07-01..2026-12-31).
- [x] **Step 2:** Run them. Expected: FAIL.
- [x] **Step 3:** Implement `sync_companies` (upsert on `cik`; memberships upsert on `(company_id, index, start_date)`) and `covered_companies` (overlap: `start_date <= end and (end_date is null or end_date >= start)`).
- [x] **Step 4:** Populate `data/companies.csv` with the S&P 100 as used for the 2026 season, CIKs checked against EDGAR's company tickers file; record the source and date in the comment line **(verify the constituent source with Quinn)**.
- [x] **Step 5:** Run them. Expected: PASS.
- [x] **Step 6:** Commit `[BR-11] NEW: add covered company list and sync`.

### Task 5b: Company filer IDs and approximate membership (added 2026-11-21)

Spec update 2026-11-21 [Quinn]: a company is the unit; a company can file under several EDGAR CIKs over time (ExxonMobil: 0000034088 through 2026-06-30, 0002115436 from 2026-07-01). Each filing keeps the CIK it was filed under. 2026 memberships are approximate.

**Files:**
- Create: `db/migrations/20261121000004_company_ciks.sql`, `data/company_ciks.csv`, `tests/db/test_company_ciks.py`
- Modify: `openboardroom/companies.py`, `tests/test_companies.py`, `db/schema.sql`, `tests/contract/views.json`, `db/migrations` views (via a new migration that recreates `published_ballot_items` with `filer_cik`, still `with (security_barrier)`), `tests/db/test_views.py`

**Interfaces:**
- Produces: table `company_ciks(id, company_id → companies, cik unique 10-digit, valid_from date not null, valid_to date null, note text)`; `filings.cik` (10-digit, not null, the CIK filed under); `index_memberships.dates_approximate boolean not null default false`; view column `published_ballot_items.filer_cik`.
- `CoveredCompany` gains `ciks: list[CikRange]` where `CikRange(cik: str, valid_from: date, valid_to: date | None)`; `covered_companies(...)` returns, for each company, the CIK ranges that overlap the window.
- `sync_companies` also loads `data/company_ciks.csv` (`ticker,cik,valid_from,valid_to,note`, `#` comments skipped) and ensures every company's current `companies.cik` has a `company_ciks` row with `valid_to` null; `--ciks PATH` option.
- `data/companies.csv` gains a `dates_approximate` column (`true` for all 2026 rows); XOM's current CIK is 0002115436, and `data/company_ciks.csv` lists `XOM,0000034088,<first filing date in EDGAR or 1900-01-01>,2026-06-30,"Exxon Mobil Corp; succeeded by ExxonMobil Holdings Corp (8-K12B 2026-07-01)"` and `XOM,0002115436,2026-07-01,,`.

- [x] **Step 1: Failing tests:** `test_company_has_multiple_ciks` (XOM fixture → two ranges); `test_cik_unique_across_companies` (`UniqueViolation`); `test_current_cik_has_open_range`; `test_covered_companies_returns_overlapping_ranges` (window 2026-01-01..2026-12-31 → both XOM ranges; window 2027 → new CIK only); `test_dates_approximate_loaded`; view test `test_filer_cik_is_filing_cik` (item on a filing under the old CIK shows `filer_cik=0000034088`, `cik=0002115436`); contract: `filer_cik` in `views.json`.
- [x] **Step 2:** Run them. Expected: FAIL.
- [x] **Step 3:** Migration (up/down), loader, data files; regenerate `db/schema.sql` with `scripts/dump-schema.sh`; update `views.json`; keep every view `security_barrier`; extend the leak test's probe list if a new joined column is added.
- [x] **Step 4:** Run `uv run pytest --cov=openboardroom -v`, ruff, pyright. Expected: all pass, coverage ≥ 90%.
- [x] **Step 5:** Commit `[BR-11] NEW: add company filer IDs and approximate membership`.

### Task 6: EDGAR client

**Files:**
- Create: `openboardroom/edgar/__init__.py`, `openboardroom/edgar/client.py`, `openboardroom/edgar/ratelimit.py`, `tests/edgar/test_client.py`

**Interfaces:**
- Consumes: `Settings` (Task 2).
- Produces:
  - `class RateLimiter(max_per_second: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)` with `wait() -> None`.
  - `class EdgarClient(settings: Settings, transport: httpx.BaseTransport | None = None, limiter: RateLimiter | None = None)` with `submissions(cik: str) -> dict[str, Any]` (`https://data.sec.gov/submissions/CIK{cik}.json`), `submissions_page(name: str) -> dict[str, Any]` (`https://data.sec.gov/submissions/{name}`), and `document(cik: str, accession: str, primary_document: str) -> bytes` (`https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession without dashes}/{primary_document}`).
  - `class EdgarError(Exception)`.
  - *(Updated 2026-11-21:)* also `company_tickers() -> dict[str, Any]` (`https://www.sec.gov/files/company_tickers.json`, used by Task 7) and a `sleep` keyword for backoff; `EdgarError(message, status: int | None = None)` carries the HTTP status (None for a network error or an unusable body); a body that isn't a JSON object raises `EdgarError`.
- Every request sends `User-Agent: settings.edgar_user_agent` and goes through the limiter. 429 and 5xx retry up to 3 times with exponential backoff (1, 2, 4 seconds via the injected `sleep`); 403 and other 4xx raise `EdgarError` at once with the status and URL.

- [x] **Step 1: Write the failing tests** (with `httpx.MockTransport` and a fake clock):
  - `test_sends_configured_user_agent`
  - `test_rate_limiter_spaces_requests` (10 calls at 8 rps → fake clock advanced ≥ 1.125 s)
  - `test_403_raises_without_retry` (one request made; `EdgarError` mentions 403)
  - `test_429_retries_then_raises` (4 requests made, then `EdgarError`)
  - `test_5xx_then_success_returns_body`
- [x] **Step 2:** Run `uv run pytest tests/edgar -v`. Expected: FAIL.
- [x] **Step 3:** Implement `ratelimit.py` and `client.py`.
- [x] **Step 4:** Run them. Expected: PASS.
- [x] **Step 5:** Commit `[BR-11] NEW: add rate-limited EDGAR client`.

### Task 7: Discover

**Files:**
- Create: `openboardroom/pipeline/__init__.py`, `openboardroom/pipeline/discover.py`, `tests/pipeline/test_discover.py`, `tests/fixtures/edgar/submissions_aapl.json` (trimmed copy of Apple's submissions JSON: the 2023–2026 DEF 14A, DEFA14A, and 8-K rows, including at least one 8-K without `5.07`), `tests/fixtures/edgar/submissions_page.json`

**Interfaces:**
- Consumes: `EdgarClient` (Task 6), `covered_companies` (Task 5).
- Produces: `discover(conn: psycopg.Connection, client: EdgarClient, start: date, end: date, index: str = "sp100") -> DiscoverResult` (`@dataclass DiscoverResult(new: int, existing: int)`); CLI `obr discover --since YYYY-MM-DD --until YYYY-MM-DD`.
- Rules (spec §6.1, §12):
  - Read `filings.recent` (parallel arrays: `form`, `filingDate`, `accessionNumber`, `primaryDocument`, `items`); if `start` is earlier than the oldest `filingDate` in `recent`, also read each page in `filings.files` whose `filingTo >= start`.
  - Keep `DEF 14A` and `DEFA14A`; keep `8-K` only when `"5.07"` is in its comma-separated `items`.
  - ~~Keep only filings dated inside both the window and the company's membership.~~ *(Updated 2026-11-21:)* Keep a filing dated inside the window when one of the company's memberships overlaps [filed − `MEMBERSHIP_LOOKBACK` (14 days), filed + `MEMBERSHIP_LOOKAHEAD` (180 days)], inclusive: membership is judged at the meeting date (spec §4), which a proxy precedes by under about six months and a 5.07 8-K follows by days. Each membership is checked on its own, not their merged span. Companies are selected with `covered_companies(conn, index, start − MEMBERSHIP_LOOKBACK, end + MEMBERSHIP_LOOKAHEAD)` so a membership starting after `end` or ending before `start` still counts.
  - Insert with `stage='discovered'`, `source_url` built as in `EdgarClient.document`; `on conflict (accession) do nothing` counts as `existing`.

- [x] **Step 1: Write the failing tests:**
  - `test_discovers_proxy_supplement_and_results` (Apple fixture, window 2026-01-01..2026-12-31 → the 2026-01-08 DEF 14A and DEFA14A and the 2026-02-24 8-K; `new == 3`)
  - `test_8k_without_507_ignored`
  - `test_rerun_is_noop` (`DiscoverResult(new=0, existing=3)`)
  - `test_company_with_no_filings_in_window` (`new == 0`, no error)
  - `test_reads_paged_file_for_older_window` (window in 2015 → `submissions_page` called)
  - `test_membership_limits_window`
- [x] **Step 2:** Run `uv run pytest tests/pipeline/test_discover.py -v`. Expected: FAIL.
- [x] **Step 3:** Implement `discover` and the CLI subcommand.
- [x] **Step 4:** Run them. Expected: PASS.
- [x] **Step 5:** Commit `[BR-11] NEW: add EDGAR filing discovery`.

**Amendments (2026-11-21, spec update: company is the unit):**
- Poll every `CikRange` of each covered company; keep a filing only if its date is inside both the window, ~~the membership,~~ and that CIK's range, and it passes the membership rule in the Rules above *(updated 2026-11-21)*; insert with `filings.cik` set to that CIK.
- Fixture: XOM submissions for both CIKs (from the real JSON, trimmed) → 2026 DEF 14A, DEFA14As, and results 8-K come from 0000034088; the new CIK's 2026-10/11 DEFA14As are kept (inside its range).
- A covered company's `8-K12B` is not stored (form CHECK stays 3 forms) but is returned in `DiscoverResult.successor_notices: list[SuccessorNotice(company_id, ticker, cik, accession, filed)]` and printed by `obr discover` for a person to link; test `test_8k12b_reported_not_stored`.

**Amendments (updated 2026-11-21 to match the implementation after review):**
- `SuccessorNotice` also carries `ticker` (above). An 8-K12B is filed under the successor's *new* CIK, which discovery doesn't poll until it is linked, so ticker drift is how a new filer is noticed: `discover` reads `company_tickers.json` once per run, and a covered company whose ticker EDGAR maps to a CIK not in its `company_ciks`, or that EDGAR doesn't list, is returned in `DiscoverResult.cik_drift: list[CikDriftNotice(company_id, ticker, edgar_cik: str | None)]` and printed. Linking stays a manual edit to `data/company_ciks.csv`. Tests `test_cik_drift_reported`, `test_no_drift_when_cik_linked`, `test_missing_ticker_reported`, `test_malformed_ticker_entry_raises`.
- Membership rule as in the Rules above. Tests `test_membership_gap_excludes_filings`, `test_proxy_filed_before_membership_start_is_kept`, `test_filing_long_before_membership_is_dropped`, `test_short_window_before_membership_start_finds_proxy`, `test_results_8k_shortly_after_membership_end_is_kept`, `test_results_8k_long_after_membership_end_is_dropped`, `test_membership_match_boundaries`.
- A paged file is also read when `start` equals the oldest date in `recent` (EDGAR splits by count); malformed submissions raise `EdgarError` naming the CIK. An empty or blank `primaryDocument` is stored as NULL.

### Task 8: Fetch and plain text

**Files:**
- Create: `openboardroom/pipeline/fetch.py`, `openboardroom/text.py`, `tests/pipeline/test_fetch.py`, `tests/test_text.py`, `tests/fixtures/filings/aapl-2024-def14a.htm` (trimmed to under 200 KB, keeping the cover page and Proposals 1, 4, and 8), `tests/fixtures/filings/aapl-2024-def14a.expected.txt`, `tests/fixtures/filings/cp1252.htm`

**Interfaces:**
- Consumes: `EdgarClient.document`, `Settings.cache_dir`.
- Produces: `to_text(html: bytes) -> str` in `openboardroom/text.py`; `fetch(conn: psycopg.Connection, client: EdgarClient, cache_dir: Path) -> FetchResult` (`@dataclass FetchResult(fetched: int, failed: int)`); CLI `obr fetch`. *(Updated 2026-11-21:)* `fetch(..., on_error: Callable[[str], None] | None = None)` reports each per-filing failure as it happens, and `FetchResult(fetched, failed, errors: list[str], stopped: str | None)`.
- `to_text`: decode by the document's declared charset, else UTF-8, else cp1252; parse with BeautifulSoup `lxml`; `get_text("\n")`; collapse runs of whitespace within each line; drop empty lines; join with `\n`; trailing newline. Same method as the spike.
- `fetch`: for each `stage='discovered'` filing: download to `cache_dir/raw/{accession}.part`, rename to `{accession}.htm` only after the full body is written; record `raw_sha256`; write `cache_dir/text/{accession}.txt`; record `text_sha256`; set `stage='fetched'`. A download error leaves the filing at `discovered` and counts as `failed`. *(Updated 2026-11-21:)* That holds for a per-filing error such as 404 or a missing primary document; on 403, 429, 5xx, or a network error the run stops at once with `stopped` set, leaving that filing and the rest at `discovered`, and the CLI exits non-zero. An existing `.htm` whose sha256 matches `raw_sha256` is reused without a request.

- [x] **Step 1: Write the failing tests:**
  - `tests/test_text.py::test_golden_apple_2024` (`to_text(fixture) == expected.txt`)
  - `test_cp1252_document_decodes` (a `’` encoded as `0x92` comes out as `’`)
  - `tests/pipeline/test_fetch.py::test_fetch_advances_stage_and_records_hashes`
  - `test_partial_download_is_refetched` (a leftover `.part` file is ignored; the document is requested again)
  - `test_cached_document_not_refetched` (second run makes no request)
  - `test_download_error_leaves_filing_discovered`
- [x] **Step 2:** Run `uv run pytest tests/test_text.py tests/pipeline/test_fetch.py -v`. Expected: FAIL.
- [x] **Step 3:** Implement `to_text`, `fetch`, and the CLI subcommand.
- [x] **Step 4:** Run `uv run pytest --cov=openboardroom -v && uv run pyright && uv run ruff check`. Expected: all pass; coverage ≥ 90%.
- [x] **Step 5:** Commit `[BR-11] NEW: add filing fetch and plain-text conversion`.

### Task 9: End-to-end check against EDGAR (manual)

- [x] **Step 1:** With `OBR_EDGAR_USER_AGENT="Example Org contact@example.org"` and a local Postgres (`dbmate up`), run `uv run obr companies sync && uv run obr discover --since 2026-01-01 --until 2026-12-31 && uv run obr fetch`. Expected: about 100 DEF 14A filings, their DEFA14As, and about 100 results 8-Ks at `stage='fetched'`; no `EdgarError`; no request rate above 8 per second.
- [x] **Step 2:** Record the counts per form, run time, and any failures in the PR description.
- [ ] **Step 3:** Ask Quinn before creating the GitHub repo (`gh repo create OpenBoardroom/backend --public`), pushing, and opening the PR.
