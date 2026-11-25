# Supplements and Pending Changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read every proxy supplement (DEFA14A) for ballot changes:
- withdrawals and recommendation changes apply automatically when their quote verifies
- changed nominees, amended facts, and added items wait for a reviewer as pending changes
- every change to a public value leaves a correction

**Architecture:**
- New stage `obr supplements` (`openboardroom/pipeline/supplements.py`), with direct, `--submit`, and `--collect` modes like `obr extract`. It links each fetched DEFA14A to its meeting and sends the supplement with the meeting's current items to Claude. It checks the typed changes and writes them as `pending_changes` rows, applying the automatic ones.
- `openboardroom/review/` gains pending-change decisions: they appear in the queue, in `show`, in the `next` walk, and as subcommands.

**Tech Stack:** Python 3.13, psycopg 3, the `anthropic` SDK (streamed direct calls and Message Batches), pytest with Testcontainers Postgres 17, ruff, pyright strict.

**Spec:** `OpenBoardroom/architecture` → `docs/specs/2026-11-21-ballot-item-pipeline-design.md`:
- §6.3 item 3 (DEFA14A)
- §7.1 (changes to published facts) and its update of 2026-11-25 (`change nominees`, added items, linking)
- §7.3 (corrections)

Ticket: BR-15 (#15), Plan 3b. It follows Plan 3 (`docs/plans/2026-11-24-review-and-publication.md`).

## Global Constraints

- Branch `task/BR-15`; commits `[BR-15] TYPE: message`, each ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push.
- TDD; 90% line and branch coverage for `openboardroom/pipeline/supplements.py`, `openboardroom/extract/supplement.py`, and the changed parts of `openboardroom/review/`.
- No live API calls in tests; replay through `tests/llm_replay.py`. Only the stage commands spend money, and only when run by hand.
- Filings and model output are untrusted. Every change carries a `source_quote` that must appear verbatim (after `normalize`) in the supplement's text, or nothing from that supplement applies.
- A later run never overwrites a published value except through the two automatic change types. Each automatic change on a published item writes a `corrections` row with `review_id` null (spec §7.1).
- Neutrality (handbook, spec §8.4): the supplement prompt only extracts what changed. It writes no summaries or arguments, and an added item's summary follows v4's Neutrality section, copied byte for byte and tested as identical.
- Model and effort from `OBR_EXTRACT_MODEL` and `OBR_EXTRACT_EFFORT`, as for `extract`.

## Scope rulings (for Quinn's approval)

- **Supplement runs get their own `extraction_runs.kind`, `supplement`,** so `obr extract --collect` never picks them up, and `obr supplements --collect` never picks up proxy runs. Cost if wrong: none; one more vocabulary value.
- **Supplement stages reuse the filing stages:**
  - `checked`: valid output, whether or not it changed anything
  - `in_review`: a quote or reference failed, or the model refused
  - `out_of_scope`: no linked meeting

  Supplements are never "published"; their changes are. Cost if wrong: a supplement stage name reads oddly in `review queue`.
- **A supplement whose proxy isn't extracted yet stays `fetched`** and is skipped until a later run. A supplement whose linked meeting has no items (a refused proxy) waits the same way.
- **`amend text` covers only these fields:** `title`, `request`, `vote_standard_text`, `lead_filer`, `representative`, `co_filers`. They are the fields a supplement realistically corrects. Cost if wrong: a supplement correcting another field goes to `in_review`, where a person fixes it by hand.
- **An approved added item gets only a summary text, pending.** Its arguments, if any, come from review: a person edits them in. The supplement prompt asks for the added item's facts and summary only. Cost if wrong: one extra edit per added item; none seen in 204 supplements.
- **A supplement in review is handled with `clear` or `requeue`.** `clear` drops its "must have items" rule for DEFA14A filings. A supplement has no items of its own.

## Review Focus

1. **A change whose quote isn't in the supplement, or that names an item number the meeting doesn't have.** Nothing from that supplement is written, not even its valid changes, and it goes to `in_review` with the failed checks recorded. Test in Task 3.
2. **The same supplement processed twice** (a crash between submit and collect, or a requeue then a re-run). An automatic change that is already applied is not applied twice, and it writes no second correction. A pending change whose exact `(target, field, proposed)` is already pending is not duplicated. Test in Task 4.
3. **A withdrawal of an item that is already withdrawn, or a recommendation change to the value it already has.** Recorded as an `applied` pending change with no update and no correction. Test in Task 4.
4. **Linking at the edges:**
   - a supplement filed the same day as the proxy is linked
   - one filed after the meeting date is `out_of_scope`
   - with two proxies (an annual and a special meeting), it links to the latest one filed on or before it whose meeting hasn't happened

   Test in Task 2.
5. **Reviewer decisions on pending changes:**
   - approving a nominee change on a published election replaces the nominee rows and writes one correction (field `nominees`)
   - rejecting needs a reason and changes nothing
   - approving an added item creates it unpublished, numbered as printed (`"6"`, `"2b"`), and it then appears in the review queue

   Tests in Task 6.

---

### Task 1: Schema for supplements and pending changes

**Files:**
- Create: `db/migrations/20261125000002_supplements.sql`
- Modify: `db/schema.sql` (regenerate with `scripts/dump-schema.sh`)
- Test: `tests/db/test_schema.py`

**Interfaces:**
- Produces:
  - `pending_changes.ballot_item_id` becomes nullable.
  - New column `pending_changes.meeting_id bigint not null references meetings on delete restrict`. It is backfilled from the item for existing rows; there are none locally, but the migration doesn't assume that.
  - Check `pending_changes_target_check`: `(ballot_item_id is not null) <> (field = 'item')`. An added item has no item yet; every other change names one.
  - `pending_changes.decided_at timestamptz` (null until applied or rejected), and `pending_changes.review_id bigint references reviews on delete set null`. A null `review_id` on an applied change means it was applied automatically.
  - `extraction_runs.kind` gains `supplement`.
  - `reviews.decision` gains `apply_change` and `reject_change`.

- [ ] **Step 1: Write failing tests**
  - `test_pending_change_for_added_item_names_meeting_only`: an `item` change with a meeting and no item inserts. An `item` change with an item, and a `title` change with no item, each raise `CheckViolation`.
  - `test_extraction_run_kind_accepts_supplement`.
  - `test_review_decision_accepts_change_decisions`.
  - The existing migration up/down test covers reversibility.
- [ ] **Step 2: Run.** `uv run pytest tests/db -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement** the migration with a full down section. The down section fails while an `item` change exists, and its comment says so. Regenerate `schema.sql`.
- [ ] **Step 4: Run.** The tests pass, and `uv run pytest -q` is green.
- [ ] **Step 5: Commit** `[BR-15] NEW: schema for supplements and pending changes`.

### Task 2: Linking supplements to meetings

**Files:**
- Create: `openboardroom/extract/supplement.py` (store queries and checks for supplements; extraction keeps its own `store.py`)
- Test: `tests/extract/test_supplement_link.py`

**Interfaces:**
- Produces:
  - `Supplement(filing_id: int, accession: str, meeting_id: int | None)`.
  - `pending_supplements(conn, limit: int | None = None, accession: str | None = None) -> list[Supplement]`. It returns fetched DEFA14A filings with no `submitted` supplement run, in id order. `meeting_id` is the linked meeting (spec §7.1 update), or None when no proxy links.
  - `meeting_items(conn, meeting_id) -> list[dict[str, object]]`. Each entry holds `number` (as printed), `title`, `type`, `board_recommendation`, `status`, and `nominees`.

Linking: the company's DEF 14A with the latest `filed_at` at or before the supplement's `filed_at`.
- **Linked:** its meeting's `meeting_date` is on or after the supplement's `filed_at`. `meeting_id` is that meeting.
- **Waits** (left out of the list until a later run): the proxy isn't extracted yet (no meeting row), or its meeting has no items.
- **Unlinked:** no DEF 14A of the company is at or before the supplement, or the latest one's meeting is before the supplement. `meeting_id` is None, and the stage sets it `out_of_scope`.

- [ ] **Step 1: Write failing tests**
  - `test_links_same_day_supplement`.
  - `test_supplement_after_meeting_has_no_meeting`.
  - `test_links_latest_proxy_whose_meeting_is_ahead`: a special meeting's proxy filed after the annual proxy, and a supplement after both, link to the special meeting when its date is ahead.
  - `test_waits_for_unextracted_proxy`.
  - `test_skips_supplement_waiting_on_a_batch`.
  - `test_meeting_items_lists_numbers_and_nominees`.
- [ ] **Step 2: Run.** `uv run pytest tests/extract/test_supplement_link.py -q`. Expected: import error, then failures.
- [ ] **Step 3: Implement** with SQL.
- [ ] **Step 4: Run.** The tests pass.
- [ ] **Step 5: Commit** `[BR-15] NEW: link supplements to their meetings`.

### Task 3: Supplement prompt, schema, and checks

**Files:**
- Create: `openboardroom/extract/prompts/supplement-v1.md`, `openboardroom/extract/prompts/supplement-schema-v1.json`
- Modify: `openboardroom/extract/spec.py`, `openboardroom/extract/supplement.py`
- Test: `tests/extract/test_spec.py`, `tests/extract/test_supplement_checks.py`

**Interfaces:**
- Produces:
  - `spec.SUPPLEMENT_PROMPT_VERSION = "1"`, `supplement_prompt()`, `supplement_schema()`, `supplement_user_message(text, items) -> str`. The user message is the current items as JSON, then `<filing>`, with `</filing` escaped as in `user_message`.
  - `supplement.problems(output, text, items) -> list[str]`. It is empty when the changes may be written.
- Output schema (all fields required, strict):
  - `changes` is a list of objects with these fields:
    - `type`: one of `withdraw`, `change_recommendation`, `change_nominees`, `amend_text`, `add_item`
    - `number`: integer or null
    - `part`: string
    - `recommendation`: the board-recommendation vocabulary, or null
    - `nominees`: string list
    - `field`: one of the `amend_text` fields, or null
    - `value`: string
    - `item`: object or null, with the v3 item facts `title`, `type`, `category`, `effect`, `board_recommendation`, `lead_filer`, `representative`, `request`, `summary`
    - `source_quote`: string
  - An empty `changes` list means the supplement changes nothing.
- The prompt:
  - says to report only ballot changes the supplement states, against the items given, each with an 8–40 word verbatim quote
  - says to return no changes for supplements that only add materials, letters, or corrections that don't touch the ballot
  - copies v4's Neutrality section for the added item's summary
- `problems` returns, for each change:
  - a schema error
  - a quote not found in the normalized text (reuse `checks.normalize`)
  - a `number`/`part` that isn't an item of the meeting (except `add_item`, whose number must not already exist)
  - a type whose required field is empty: `change_recommendation` without `recommendation`, `change_nominees` with no nominees, `amend_text` without `field` and `value`, `add_item` without `item`

- [ ] **Step 1: Write failing tests**
  - `test_supplement_prompt_neutrality_matches_v4`.
  - `test_supplement_schema_is_valid_and_strict`: the `_objects` walk from `test_every_object_forbids_extra_properties_and_requires_all`.
  - `test_supplement_user_message_puts_items_before_filing`.
  - In `test_supplement_checks.py`: one test per problem above, plus `test_no_changes_has_no_problems`. The supplement text comes from the Goldman Sachs 2026 withdrawal ("On April 1, 2026, the American Family Association withdrew “Item 5. Shareholder Proposal Regarding Charitable Giving Reporting.”"), cut to its first paragraphs as fixture `tests/fixtures/filings/gs-2026-defa14a.txt`, plus a curly-quote variant to show `normalize` applies.
- [ ] **Step 2: Run.** Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass. `uv run pytest -q` is green.
- [ ] **Step 5: Commit** `[BR-15] NEW: supplement prompt, schema, and checks`.

### Task 4: Writing and applying changes

**Files:**
- Modify: `openboardroom/extract/supplement.py`
- Test: `tests/extract/test_supplement_apply.py`

**Interfaces:**
- Consumes: Task 1's columns, Task 3's output shape.
- Produces `write_changes(conn, supplement: Supplement, run_id: int, changes: list[dict], now: datetime) -> ChangeCounts(applied: int, pending: int, skipped: int)`, called inside the caller's transaction:
  - **`withdraw`**: `status` becomes `withdrawn`.
  - **`change_recommendation`**: sets `board_recommendation`.
  - Both of these are recorded as a `pending_changes` row with `status = 'applied'`, `decided_at = now`, `review_id` null, and `reason = 'supplement'`. When the item is published, they also write a `corrections` row with reason `supplement <accession>`.
  - An already-true change is recorded `applied` with no update and no correction.
  - **The other types** are written as `pending`, with `field` set to `nominees`, the amended field, or `item`. `proposed` holds the nominee list, the value, or the item object.
  - **A change already recorded** for the same target, field, and proposed value (pending, or applied from an earlier run) is counted `skipped`.

- [ ] **Step 1: Write failing tests**
  - `test_withdraw_applies_and_corrects_published_item`.
  - `test_withdraw_of_unpublished_item_writes_no_correction`.
  - `test_recommendation_change_applies`.
  - `test_already_true_change_is_recorded_without_update`.
  - `test_review_types_become_pending`: one each of nominees, amend, and add item. The added item has `meeting_id` and no item.
  - `test_rerun_skips_recorded_changes`: run twice, so applied is 1 then 0, skipped is 0 then 1, and there is one correction.
  - `test_published_view_flags_pending_change` (as `web_ro`, admin connection as in `tests/pipeline/test_publish.py`).
- [ ] **Step 2: Run.** Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass.
- [ ] **Step 5: Commit** `[BR-15] NEW: write and apply supplement changes`.

### Task 5: The supplements stage

**Files:**
- Create: `openboardroom/pipeline/supplements.py`
- Modify: `openboardroom/extract/llm.py` (a generic `submit_requests(client, requests: list[tuple[str, dict]]) -> str` that `submit_batch` and the supplement stage share; `supplement_request_params(settings, text, items)`; `run_supplement`), `openboardroom/extract/store.py` (`insert_llm_run` takes `kind` and the versions as keyword arguments, defaulting to today's values), `openboardroom/cli.py`
- Test: `tests/pipeline/test_supplements.py`, `tests/extract/test_llm.py` (the refactor keeps `submit_batch` behaviour)

**Interfaces:**
- Produces:
  - `run(conn, client, settings, text_dir, *, mode: Literal["direct", "submit", "collect"], limit=None, accession=None) -> SupplementResult(checked, in_review, out_of_scope, failed, submitted, waiting, applied, pending)`.
  - `register(subparsers)` adds `obr supplements [--filing A | --limit N] [--submit | --collect]`. It prints `supplements: N checked, N in review, N out of scope, N failed, N submitted, N waiting; N changes applied, N pending` and exits 1 on failures, like `extract`.

Per supplement (one transaction after its result arrives):
- no linked meeting → `out_of_scope`
- a call failure (API error, truncation) → the run is `failed` and the stage stays `fetched`. The same cap as extract applies: two billed failures at the current `SUPPLEMENT_PROMPT_VERSION`, counting only `supplement` runs. `pending_supplements` skips capped filings.
- a refusal → `in_review`
- `problems` → `in_review`, with check results recorded under the run (check name `quotes` or `schema` or `consistency`, as fits)
- otherwise `write_changes`, then `checked`

Collect retries errored, expired, or refused batch items once directly, as extract's collect does.

- [ ] **Step 1: Write failing tests**
  - `test_direct_withdrawal_end_to_end`: replay of a synthetic response for the Goldman Sachs fixture. The supplement becomes `checked`, item 5 is withdrawn, and the change is `applied`.
  - `test_no_changes_is_checked`.
  - `test_bad_quote_goes_to_review_and_writes_nothing`.
  - `test_unlinked_supplement_is_out_of_scope`.
  - `test_submit_then_collect` (`jsonl_response`, as `tests/pipeline/test_extract.py` does).
  - `test_extract_collect_ignores_supplement_runs`.
  - `test_cli_prints_counts`.
- [ ] **Step 2: Run.** Expected: they fail.
- [ ] **Step 3: Implement.** Run `uv run pytest tests/extract/test_llm.py -q` after the `llm.py` refactor, before the new code.
- [ ] **Step 4: Run.** They pass. The suite is green, and coverage is at or above 90% on the new modules.
- [ ] **Step 5: Commit** `[BR-15] NEW: add the obr supplements stage`.

### Task 6: Reviewing pending changes

**Files:**
- Modify: `openboardroom/review/store.py`, `render.py`, `session.py`, `cli.py`
- Test: `tests/review/test_review_changes.py`, plus additions to `tests/review/test_review_session.py` and `test_review_cli.py`

**Interfaces:**
- Produces:
  - `ChangeView(id, item_id: int | None, item_number: str | None, field, proposed, source_quote, supplement_accession, status)`.
  - `FilingView.changes: list[ChangeView]`, holding the pending changes on the filing's meeting.
  - `QueuedFiling.pending_changes: int`. Proxy filings with a pending change join the queue.
  - `apply_change(conn, change_id, reviewer, reason) -> int` and `reject_change(conn, change_id, reviewer, reason) -> int`.
  - `clear` accepts DEFA14A filings with no items.
  - Render shows each pending change with its quote and a link into the supplement's own EDGAR document.
  - The session asks per change: `[y] apply, [n] reject, [s]kip, [q]uit`, and a rejection asks for a reason.
  - CLI: `obr review apply-change ID --reason R` and `obr review reject-change ID --reason R`.

`apply_change`:
- `nominees`: replaces the nominee rows
- an amended field: goes through the same parsing and update as `fix_item`
- `item`: inserts an unpublished item numbered as printed, with a pending summary text

It marks the change `applied` with `decided_at` and `review_id`, records an `apply_change` review with before and after, and writes a correction when the target item is published. `reject_change` marks it `rejected` with the review.

- [ ] **Step 1: Write failing tests**
  - `test_apply_nominee_change_on_published_election_corrects`.
  - `test_apply_amend_text_uses_fix_rules`: a bad value raises `ReviewError` and nothing is written.
  - `test_apply_added_item_creates_unpublished_item_in_queue`.
  - `test_reject_change_needs_reason`.
  - `test_decided_change_raises`.
  - `test_queue_includes_filings_with_pending_changes`.
  - `test_render_shows_changes_with_supplement_link`.
  - `test_session_applies_and_rejects_changes`.
  - `test_clear_accepts_supplement_without_items`.
  - `test_cli_change_commands_call_the_store`.
- [ ] **Step 2: Run.** Expected: they fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** They pass. The suite is green, and ruff and pyright are clean.
- [ ] **Step 5: Commit** `[BR-15] NEW: review pending changes`.

### Task 7: Documentation and the first live run

**Files:**
- Modify: `README.md`:
  - stage 6 (`obr supplements` and its modes)
  - the stage meanings for supplements
  - the Review section's change commands
  - that automatic changes need no reviewer but leave corrections
- Modify: `docs/extraction-sample-findings.md`: a "Supplements" section with the live results.
- Modify: architecture `docs/planning/05-cost-tracking.md` with the measured cost.

- [ ] **Step 1: Write the README sections.**
- [ ] **Step 2: Live check, one at a time, run by Quinn or with his go-ahead.** The four `--filing` runs together cost under $0.20 **(estimate)**.
  - `obr supplements --filing 0001193125-26-141997` (Goldman Sachs). Expected: `checked`, item 5 `withdrawn`, one correction (item 5 is published).
  - Disney `0001744489-26-000028` and GE `0000040545-26-000022`: each withdraws a proposal.
  - GM `0001193125-26-239355`: one pending `nominees` change.
  - Philip Morris `0001628280-26-022319`: no changes.
  - Then `obr supplements --submit` for the rest (about $2–3 batched **(estimate)**), `--collect`, and `obr review queue` to see the pending changes.
- [ ] **Step 3: Record the results and cost; commit** `[BR-15] UPDATE: document supplements and record the first run`.
