# Review and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can review every extracted filing from the terminal and approve, edit, or reject its texts. They can fix or clear a filing that failed a check, or send it back to extraction. Facts that passed the checks publish, and every change to a public value leaves a correction record.

**Architecture:**
- A new package `openboardroom/review/` holds three modules:
  - queries and decisions (`store.py`)
  - plain-text rendering of a filing for the terminal (`render.py`)
  - the interactive walk (`session.py`)
- `openboardroom/pipeline/publish.py` is a stage command that publishes the facts of `checked` filings.
- `openboardroom/pipeline/regenerate.py` re-runs rejected texts with a text-only Claude call, one call per filing.
- `obr review …` and `obr publish` are registered like the existing stages. Every decision is its own transaction, so quitting mid-filing keeps what was decided.

**Tech Stack:** Python 3.13, argparse, psycopg 3, the `anthropic` SDK (direct streamed calls only), pytest with Testcontainers Postgres 17, ruff, pyright strict.

**Spec:** `OpenBoardroom/architecture` → `docs/specs/2026-11-21-ballot-item-pipeline-design.md` §7 (publication, review, corrections), §8.4 (neutrality), §6.4 (checks). Evidence for review needs: `docs/extraction-sample-findings.md` (F12, F16, F17: argument accuracy left to review; the five hand-counted filings). Ticket: BR-15 (#15), "Plan 3: review and publication".

## Global Constraints

- Branch `task/BR-15`; commits `[BR-15] TYPE: message`, each ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
- TDD; 90% line and branch coverage for `openboardroom/review/`, `openboardroom/pipeline/publish.py`, `openboardroom/pipeline/regenerate.py`.
- Database tests use real Postgres (Testcontainers); never mock queries. No live API calls in tests: LLM tests replay fixtures through `tests/llm_replay.py`.
- Only `obr review regenerate` spends money, and only when run by hand.
- A later run never overwrites a published value (spec §7.1). A change to a published fact or an approved text on a published item writes a `corrections` row in the same transaction.
- Neutrality (handbook; spec §8.4): the regeneration prompt keeps v4's rules, including attribution ("The proponent argues…", "The board argues…"), parallel form, and no OpenBoardroom view. Its PR gets a "Neutrality review" section.
- The reviewer's name comes from `OBR_REVIEWER`. Review commands need it and the database URL, not the EDGAR User-Agent.

## Scope rulings (for Quinn's approval)

- **Split Plan 3.** This plan covers review, publication, text regeneration, and corrections from review. **Plan 3b** gets:
  - DEFA14A extraction (moved here from Plan 2)
  - `pending_changes` from re-runs and supplements
  - the automatic withdrawal and recommendation-change rule (§7.1)

  Each half ships on its own: the 2026 season can be reviewed and published before any supplement is read. Cost if wrong: a withdrawal announced in a 2026 DEFA14A stays unseen until Plan 3b. Reconciliation (Plan 4) would catch it after the meeting.
- **Adding or removing an item is not a review action.** A filing with a missing or spurious item is requeued: reset to `fetched` and extracted again. This formalizes the SQL reset used on 2026-11-23 and 2026-11-24. Requeue refuses a filing with any published item. Cost if wrong: about $0.70 per re-extraction, against a hand-entry feature.
- **Regeneration is an explicit command, run direct, one call per filing** covering all of that filing's rejected texts. Batching can come later. Cost if wrong: full price, but one call per filing rather than one per text.
- **`obr publish` is a stage command, not part of `extract`**: facts still publish on passing checks with no human step (§7.1), but someone runs the stage, as with fetch and extract. Cost if wrong: one extra command per run.
- **Review order is oldest filing first** (§7.2), by `filed_at` then id. `in_review` filings and filings with pending texts share one queue.
- **Reviews get a `ballot_item_id` column** (nullable), so a fact fix names its item. This is an addition to a table outside the published views. It needs a dated spec update to §8 (Task 1).
- **Text decisions per item:** "approve item" approves all of the item's pending texts in one step, empty texts included. That makes routine items (no arguments) quick.

## Review Focus

1. **An approved text edited after its item is published.** A new reviewer version is published (the view takes the latest approved version), and a `corrections` row records before and after. Test in Task 3.
2. **Requeue of a filing with any published item** is refused and deletes nothing. Requeue of an unpublished filing deletes exactly what the 2026-11-24 SQL reset deleted, and refuses when corrections, vote results, or pending changes exist. Earlier review rows stay, and their links to deleted texts and items become null. Tests in Task 4.
3. **Regeneration output that fails its schema, or that leaves out an item it was asked for.** No new text versions are written, the rejected texts stay rejected, and the run is recorded `failed`. Test in Task 7.
4. **A second rejection in a row of the same item and kind** marks it `needs_manual`, and regenerate skips it. Test in Task 3.
5. **A fix with a value the schema forbids** (a category outside the vocabulary, a non-numeric threshold, an unknown field) prints an error and writes nothing. The interactive session also ends cleanly on EOF. Tests in Tasks 4 and 6.

---

### Task 1: Schema for review decisions, and reviewer settings

**Files:**
- Create: `db/migrations/20261124000002_review_decisions.sql`
- Modify: `db/schema.sql` (regenerated with `scripts/dump-schema.sh`), `openboardroom/config.py`, `.env.example` (add `OBR_REVIEWER=`). Claude can't edit `.env*`, so Quinn makes that one-line change.
- Test: `tests/db/test_schema.py`, `tests/test_config.py`
- Also: architecture spec §8 dated update (the reviews column and the decision vocabulary), committed on `task/BR-15-costs`.

**Interfaces:**
- Produces:
  - `reviews.decision` check with values `approve`, `edit`, `reject`, `fix`, `clear`, `requeue`
  - `reviews.ballot_item_id bigint null references ballot_items on delete set null`
  - `reviews.item_text_id` foreign key changed from `on delete restrict` to `on delete set null`, so requeue keeps earlier review rows
  - `extraction_runs.kind` check gains `texts`
  - `config.review_settings_from_env(env) -> ReviewSettings(database_url: str, reviewer: str)`

- [ ] **Step 1: Write failing tests**
  - `test_review_decision_rejects_unknown_value`: inserting decision `maybe` raises `CheckViolation`.
  - `test_review_may_name_an_item`: inserting a review with a `ballot_item_id` succeeds.
  - `test_deleting_a_reviewed_text_keeps_the_review`: the review's `item_text_id` becomes null.
  - `test_extraction_run_kind_accepts_texts`.
  - `test_review_settings_need_reviewer`: `review_settings_from_env({OBR_DATABASE_URL: …})` raises `ConfigError` naming `OBR_REVIEWER`. A blank value counts as missing.
  - `test_review_settings_skip_edgar`: no `OBR_EDGAR_USER_AGENT` is needed.
- [ ] **Step 2: Run them.** `uv run pytest tests/db/test_schema.py tests/test_config.py -q`. Expected: the 6 new tests fail.
- [ ] **Step 3: Implement.** Write the migration with a down section that reverses each change. Regenerate the schema and add the config function.
- [ ] **Step 4: Run.** The same command passes, and `uv run pytest -q` is green.
- [ ] **Step 5: Commit** `[BR-15] NEW: add review decision vocabulary and reviewer setting`.

### Task 2: Review queue and filing view

**Files:**
- Create: `openboardroom/review/__init__.py`, `openboardroom/review/store.py`
- Test: `tests/review/test_store.py`, with fixtures built through `openboardroom.extract.store`

**Interfaces:**
- Produces:
  - `queue(conn) -> list[QueuedFiling]`. `QueuedFiling(filing_id: int, accession: str, ticker: str, stage: str, pending_texts: int)` is ordered by `filed_at`, then id.
  - `load(conn, accession) -> FilingView | None`. It returns:
    - `FilingView(filing_id, accession, ticker, stage, source_url, meeting: dict | None, items: list[ItemView], failed_checks: list[tuple[str, str]])`
    - `ItemView(id, number, fields: dict, nominees: list[str], texts: list[TextView], published: bool)`
    - `TextView(id, kind, version, body, origin, review_status)`, the latest version per kind

- [ ] **Step 1: Write failing tests**
  - `test_queue_holds_in_review_and_pending_texts`: the queue holds an `in_review` filing and a `checked` filing with a pending text. It leaves out a `checked` filing whose texts are all approved, and a `fetched` one.
  - `test_queue_is_oldest_first`.
  - `test_load_returns_latest_text_versions_and_failed_checks`: with versions 1 and 2 of a summary, `texts` holds version 2. `failed_checks` lists the latest LLM run's failed checks with their detail.
  - `test_load_unknown_accession_is_none`.
- [ ] **Step 2: Run them.** `uv run pytest tests/review -q`. Expected: import error, then failures.
- [ ] **Step 3: Implement** with plain SQL.
- [ ] **Step 4: Run.** The tests pass.
- [ ] **Step 5: Commit** `[BR-15] NEW: add the review queue and filing view`.

### Task 3: Text decisions and corrections

**Files:**
- Modify: `openboardroom/review/store.py`
- Test: `tests/review/test_decisions.py`

**Interfaces:**
- Consumes: Task 2's views.
- Produces (each one commits its own transaction and returns the new `reviews.id`):
  - `approve_text(conn, text_id, reviewer) -> int`
  - `approve_item(conn, item_id, reviewer) -> list[int]` (approves every pending latest-version text)
  - `edit_text(conn, text_id, body, reviewer, reason) -> int`
  - `reject_text(conn, text_id, reviewer, reason) -> int`
  - `ReviewError(Exception)` for a text that is not pending, a blank reason, or a blank edit

- [ ] **Step 1: Write failing tests**
  - `test_approve_sets_status_and_records_review`: the review row has decision `approve`, `item_text_id`, and `reviewer`.
  - `test_approve_item_approves_all_pending_including_empty`.
  - `test_edit_adds_reviewer_version_and_keeps_model_text`: the new version is `max + 1`, with origin `reviewer` and status `approved`. The model version becomes `rejected`. The review holds `before` and `after` bodies. `published_item_texts` shows the edit once the item is published.
  - `test_edit_of_approved_text_on_published_item_writes_correction`: the correction has field `summary`, before, after, reason, and `review_id`.
  - `test_reject_requires_reason`.
  - `test_second_rejection_in_a_row_needs_manual`: reject v1, add model v2, reject v2. v2 becomes `needs_manual`.
  - `test_decision_on_non_pending_text_raises`.
- [ ] **Step 2: Run them.** `uv run pytest tests/review/test_decisions.py -q`. Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass. `uv run pytest -q` is green.
- [ ] **Step 5: Commit** `[BR-15] NEW: record text review decisions and corrections`.

### Task 4: Filing decisions: fix, clear, requeue

**Files:**
- Modify: `openboardroom/review/store.py`
- Test: `tests/review/test_filing_decisions.py`

**Interfaces:**
- Produces:
  - `FIXABLE_ITEM_FIELDS` (title, type, category, effect, lead_filer, representative, co_filers, board_recommendation, vote_base, vote_threshold_pct, abstentions_count_against, vote_standard_text, request, status)
  - `FIXABLE_MEETING_FIELDS` (kind, meeting_date, meeting_time, format, record_date, shares_outstanding)
  - `fix_item(conn, item_id, field, value: str, reviewer, reason) -> int`. It parses `value` by the column's type: `""` is null, `co_filers` takes a `;`-separated list, booleans take `true`/`false`.
  - `fix_meeting(conn, accession, field, value, reviewer, reason) -> int`
  - `clear(conn, accession, reviewer, reason) -> int`, which moves `in_review` to `checked`
  - `requeue(conn, accession, reviewer, reason) -> int`, which deletes the filing's unpublished extraction rows and sets the stage to `fetched`

- [ ] **Step 1: Write failing tests**
  - `test_fix_item_updates_and_records_before_after`.
  - `test_fix_published_item_writes_correction`.
  - `test_fix_rejects_unknown_field_and_bad_value`: an unknown field, `category=merger`, and `vote_threshold_pct=abc` each raise `ReviewError` and write nothing.
  - `test_fix_meeting_parses_dates_and_integers`.
  - `test_clear_moves_in_review_to_checked`. Clear is refused unless the stage is `in_review`, and refused when the filing has no items (send those to requeue).
  - `test_requeue_deletes_extraction_rows_and_keeps_its_review`: the stage becomes `fetched`, and meeting, items, nominees, texts, check results, and runs are gone. The requeue review row stays, so `reviews.filing_id` must not cascade.
  - `test_requeue_after_text_decisions_keeps_those_reviews`: an earlier `approve` review survives with `item_text_id` null.
  - `test_requeue_refuses_published_filing`: nothing is deleted.
  - `test_requeue_refuses_when_corrections_or_vote_results_exist`.
- [ ] **Step 2: Run them.** Expected: they fail.
- [ ] **Step 3: Implement.** Requeue reuses the delete order of the 2026-11-24 reset. Item reviews' `ballot_item_id` is set to null on delete.
- [ ] **Step 4: Run.** They pass. The suite is green.
- [ ] **Step 5: Commit** `[BR-15] NEW: fix, clear, and requeue filings in review`.

### Task 5: Publish stage

**Files:**
- Create: `openboardroom/pipeline/publish.py`
- Modify: `openboardroom/cli.py`
- Test: `tests/pipeline/test_publish.py`

**Interfaces:**
- Produces:
  - `publish(conn, now: datetime) -> PublishResult(filings: int, items: int)`. It sets `published_at` on every item of `checked` filings, and advances those filings to `published`.
  - `register(subparsers)` adds `obr publish`, which prints `publish: N filings, N items`.

- [ ] **Step 1: Write failing tests**
  - `test_publish_checked_filings_only`: `in_review`, `fetched`, and `out_of_scope` filings are untouched.
  - `test_published_views_show_facts_and_only_approved_texts`: `published_ballot_items` has the items. `published_item_texts` is empty until a text is approved.
  - `test_publish_is_idempotent`: a second run reports 0 and keeps the first `published_at`.
  - `test_cli_publish_prints_counts`.
- [ ] **Step 2: Run them.** Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass.
- [ ] **Step 5: Commit** `[BR-15] NEW: publish facts of checked filings`.

### Task 6: Rendering and the review CLI

**Files:**
- Create: `openboardroom/review/render.py`, `openboardroom/review/session.py`, `openboardroom/review/cli.py`
- Modify: `openboardroom/cli.py`
- Test: `tests/review/test_render.py`, `tests/review/test_session.py`, `tests/review/test_cli.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `render.filing(view: FilingView) -> str`
  - `render.quote_link(source_url, quote) -> str`. It returns `source_url + "#:~:text=" + start,end`, with the first and last 4 words of the quote percent-encoded (a URL text fragment).
  - `session.run(conn, reviewer, ask: Callable[[str], str], out: TextIO) -> int`. It returns the number of decisions made.
  - Subcommands of `obr review`:
    - `next`
    - `show ACCESSION`
    - `approve TEXT_ID`
    - `approve-item ITEM_ID`
    - `edit TEXT_ID --body TEXT --reason R`
    - `reject TEXT_ID --reason R`
    - `fix ITEM_ID FIELD VALUE --reason R`
    - `fix-meeting ACCESSION FIELD VALUE --reason R`
    - `clear ACCESSION --reason R`
    - `requeue ACCESSION --reason R`
    - `queue` (lists the queue)
  - Exit codes: 0 on success, 1 on `ReviewError` with its message on stderr.

The rendering shows, per item:
- the facts
- the nominees
- the three quotes, each with its link
- each text, with its id, version, and status
- the word count of the two arguments, flagged `UNEVEN` when they differ by more than a quarter (spec §8.4)

The filing header shows the stage and the failed checks.

The session walks the queue. For each item it prompts `[a]pprove item, approve [t]ext ID, [e]dit ID, [r]eject ID, [s]kip, [q]uit`. Edits and rejections ask for a body or reason on the next line. After the last item of an `in_review` filing, it prompts `[c]lear, re[q]ueue, [k]eep in review`. EOF from `ask` ends the session like quit.

- [ ] **Step 1: Write failing tests**
  - `test_render_shows_failed_checks_quotes_and_links`: snapshot of a small filing.
  - `test_quote_link_encodes_text_fragment`: a quote with spaces, a comma, and a curly quote.
  - `test_render_flags_uneven_arguments`: 40 words against 60 is flagged, 40 against 48 is not.
  - `test_session_approve_item_then_quit`: decisions are recorded and the return value is 1.
  - `test_session_reject_asks_for_reason`.
  - `test_session_eof_ends_cleanly`.
  - `test_session_in_review_filing_offers_clear`.
  - `test_cli_fix_bad_value_exits_1_with_message`.
  - `test_cli_queue_lists_oldest_first`.
- [ ] **Step 2: Run them.** Expected: they fail.
- [ ] **Step 3: Implement.** The CLI opens the connection with `review_settings_from_env`.
- [ ] **Step 4: Run.** They pass. The suite is green, and ruff and pyright are clean.
- [ ] **Step 5: Commit** `[BR-15] NEW: add the obr review command`.

### Task 7: Text regeneration after rejection

**Files:**
- Create: `openboardroom/extract/prompts/texts-v1.md`, `openboardroom/extract/prompts/texts-schema-v1.json`, `openboardroom/pipeline/regenerate.py`
- Modify: `openboardroom/extract/spec.py` (`TEXTS_PROMPT_VERSION = "1"`, `texts_prompt()`, `texts_schema()`, `texts_user_message(text, items, reasons)`), `openboardroom/extract/llm.py` (`run_texts(client, settings, params) -> Outcome`, which reuses `run_direct`'s streaming and fallback handling), `openboardroom/review/cli.py` (`regenerate [--limit N]`)
- Test: `tests/extract/test_spec.py`, `tests/pipeline/test_regenerate.py`, `tests/fixtures/llm/texts.message.json` (synthetic, marked as such in its first text)

**Interfaces:**
- Consumes: Task 3's statuses.
- Produces:
  - `regenerate(conn, client, settings, text_dir, limit: int | None) -> RegenerateResult(filings: int, texts: int, failed: list[str])`
  - Selection: items whose latest summary or argument is `rejected`, grouped by filing, oldest filing first. `needs_manual` is skipped.
  - Output schema: `{"items": [{"number", "part", "summary", "discussion": {"proponent", "board"}}]}`.
  - New versions are written with origin `model` and status `pending`, plus an `extraction_runs` row of kind `texts` that carries tokens and the prompt version. Only the rejected kinds get new versions.

The prompt reuses v4's neutrality and attribution rules word for word, plus: "Each item's facts and the reviewer's reason for rejecting its text are given. Write only the texts listed."

- [ ] **Step 1: Write failing tests**
  - `test_texts_prompt_carries_neutrality_rules`: the same phrases `test_prompt_neutrality_rules` and `test_prompt_hedge_and_attribution_rule` check in v4.
  - `test_texts_user_message_lists_items_reasons_and_filing_last`.
  - `test_regenerate_writes_new_pending_versions`: replay fixture. Only the rejected kind gets a new version.
  - `test_regenerate_skips_needs_manual`.
  - `test_regenerate_bad_output_writes_nothing`: the output fails the schema, or omits a requested item. The run is `failed`, the texts stay `rejected`, and the filing is listed in `failed`.
  - `test_regenerate_limit`.
  - `test_cli_regenerate_prints_counts`.
- [ ] **Step 2: Run them.** Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass. The suite is green, and coverage is at or above 90% for the three new modules.
- [ ] **Step 5: Commit** `[BR-15] NEW: regenerate rejected texts`.

### Task 8: Documentation and a live dry run

**Files:**
- Modify: `README.md` (a "Review and publish" section: the stages after extract, `OBR_REVIEWER`, each `obr review` subcommand, what publishes when, and that `regenerate` costs money), `docs/extraction-sample-findings.md` (link to this plan)

- [ ] **Step 1: Write the README section.**
- [ ] **Step 2: Dry run on the local database. No API calls.**
  1. `obr review queue` shows 5 `in_review` filings first, by `filed_at`, then `checked` filings.
  2. `obr review show 0001193125-26-106253` (Berkshire) renders.
  3. Quinn runs `obr review next` on one filing.
  4. `obr publish` runs on the local database.
  5. Read the output, and check `published_ballot_items` counts by SQL.
- [ ] **Step 3: Commit** `[BR-15] UPDATE: document review and publication`.
