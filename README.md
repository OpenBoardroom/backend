# OpenBoardroom backend

The data pipeline and database schema for OpenBoardroom, a neutral civic platform that explains corporate ballot items. The pipeline discovers SEC EDGAR proxy filings, extracts ballot items, and writes them to Postgres. The `obr` command runs its stages.

## Setup

- Python 3.13 with [uv](https://docs.astral.sh/uv/): `uv sync`
- [dbmate](https://github.com/amacneil/dbmate) applies the migrations in `db/`.
- Docker must be running: tests start a real Postgres with Testcontainers, and `compose.yaml` runs the local database (see [Database](#database)).
- Run the checks: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`.

See `CLAUDE.md` and the [engineering handbook](https://github.com/OpenBoardroom/architecture/blob/main/docs/handbook.md) for working rules.

## Running the pipeline

### Configuration

Set these environment variables (`.env.example` lists them):

| Variable | Required | Meaning |
|---|---|---|
| `OBR_DATABASE_URL` | yes | Postgres URL for the pipeline, connecting as the `pipeline_rw` role |
| `OBR_EDGAR_USER_AGENT` | yes | User-Agent sent to SEC EDGAR, naming your organization and a contact address, e.g. `Example Org contact@example.org`. SEC blocks requests without one |
| `OBR_EDGAR_MAX_RPS` | no | EDGAR requests per second; at most 10 (SEC's limit), default 8 |
| `OBR_CACHE_DIR` | no | Where raw filings and their text are cached; default `./cache`. Never commit it |
| `OBR_EXTRACT_MODEL` | no | Claude model that extracts ballot items; default `claude-opus-5-5` |
| `OBR_EXTRACT_EFFORT` | no | Output effort: `low`, `medium`, `high`, `xhigh`, or `max`; default `high` |
| `OBR_EXTRACT_MAX_TOKENS` | no | Output token ceiling per filing, thinking included; a positive integer, default `64000` |
| `OBR_REVIEWER` | for `review` | Your name, recorded on every review decision |
| `ANTHROPIC_API_KEY` | for `extract` | Anthropic API key. The Anthropic SDK reads it; `obr` never does. Don't keep it in your shell environment or a file; pass it per command (see [Extract](#extract)) |

### Database

The migrations grant to the `pipeline_rw` and `web_ro` roles but don't create them. In production the `infrastructure` repo creates them. Locally, `compose.yaml` runs Postgres 17.12 (the version CI uses) and creates both roles the first time it starts.

**Start a local database** (Docker must be running):

```sh
docker compose up -d --wait
```

It listens on `127.0.0.1:5432` only, with database `openboardroom`. Its passwords are for this local container and are not secrets:

| Role | Password | Used by |
|---|---|---|
| `obr_owner` | `obr_owner_local` | `dbmate` (owns the tables) |
| `pipeline_rw` | `pipeline_rw_local` | `obr` (`OBR_DATABASE_URL`) |
| `web_ro` | `web_ro_local` | the frontend, later |

**Apply the migrations** as the owner. dbmate reads `DATABASE_URL` (not `OBR_DATABASE_URL`):

```sh
DATABASE_URL="postgres://obr_owner:obr_owner_local@127.0.0.1:5432/openboardroom?sslmode=disable" dbmate --no-dump-schema up
```

`--no-dump-schema` stops dbmate from rewriting `db/schema.sql` with your local `pg_dump`; CI checks that file against its own dump, so regenerate it only with `scripts/dump-schema.sh`.

**Point `obr` at it** for the session:

```sh
export OBR_DATABASE_URL="postgres://pipeline_rw:pipeline_rw_local@127.0.0.1:5432/openboardroom?sslmode=disable"
export OBR_EDGAR_USER_AGENT="Example Org contact@example.org"   # your organization and contact
```

Then run the [stages](#stages) from `companies sync` onward. A fresh database has no record of earlier downloads, so fetch downloads each filing again (448 for the 2026 season, about a minute at 8 requests per second) and overwrites the cached copies.

**Stop it** with `docker compose stop` (data kept), or wipe it with `docker compose down -v` (deletes the data volume; the roles are created again on the next start).

If uv warns that `VIRTUAL_ENV` doesn't match the project environment, another virtual environment is active in your shell; run `deactivate` (or ignore the warning: uv uses the project's own `.venv`).

For a remote database, create the two roles as a superuser and apply the migrations as the database owner:

```sh
DATABASE_URL=postgres://OWNER:PASSWORD@HOST:5432/DBNAME dbmate --no-dump-schema up
```

### Stages

Run them in order:

1. `uv run obr companies sync` loads `data/companies.csv` (companies and index memberships) and `data/company_ciks.csv` (the EDGAR filer CIKs each company has used).
2. `uv run obr discover --since YYYY-MM-DD --until YYYY-MM-DD` finds new proxy statements (DEF 14A), proxy supplements (DEFA14A), and vote-result 8-Ks (item 5.07) filed in that window, and records them at stage `discovered`. A proxy is filed up to about six months before its meeting, so to cover a season, start the window about six months before the season's first meeting.
3. `uv run obr fetch` downloads each discovered filing's primary document into the cache, converts it to plain text, and advances it to `fetched`. It prints each failed filing as it goes and exits non-zero if any failed. If EDGAR refuses requests (HTTP 403 or 429) or can't be reached, it stops the whole run early and says why; the remaining filings stay `discovered`, so run it again later.

4. `uv run obr extract` reads each fetched proxy statement with Claude, checks the result, and advances the filing to `checked`, `in_review`, or `out_of_scope` (see [Extract](#extract)).
5. `uv run obr publish` makes the facts of every `checked` filing public and advances it to `published`. Texts become public only once a reviewer approves them (see [Review](#review)).

All stages are safe to rerun: discovery skips filings it already has, fetch reuses cached documents, extract only takes filings still at `fetched`, and publish only takes filings at `checked`.

After a change to text conversion, run `uv run obr fetch --reconvert` to rebuild every cached text from its raw document (no downloads). Stages don't change; a filing whose cached document is missing or doesn't match its recorded hash is reported and left as it was.

### Extract

`obr extract` sends each `fetched` proxy statement (DEF 14A) to Claude, validates the output with four checks, and writes the meeting, ballot items, and draft texts to the database. It costs money on every call. Choose a mode:

| Command | Use it for |
|---|---|
| `uv run obr extract --filing ACCESSION` | One filing now. Development, the eval set, and retries |
| `uv run obr extract --limit N` | The next N pending filings now, one call each. Small runs |
| `uv run obr extract --submit [--limit N]` | Scheduled runs. Sends pending filings as message batches (at half price) of up to 100 filings each; `--limit` caps the total across batches. Returns at once |
| `uv run obr extract --collect` | Later, to apply the results of batches that have ended. Batches finish within 24 hours; one still running is left alone, so run `--collect` again |

`--submit` and `--collect` can't be combined, and `--filing` works only in direct mode. A collect also retries, with one direct call, any batched filing whose result is missing, expired, canceled, errored, or refused. An `invalid_request_error` is not retried: the same request would fail again, so the filing stays `fetched` with the error recorded.

Pass the API key per command so it is never in your shell environment (`<keychain-item>` is the name of your Keychain item):

```sh
ANTHROPIC_API_KEY="$(security find-generic-password -s <keychain-item> -w)" uv run obr extract --filing ACCESSION
```

In `--collect` runs, the last count is filings still waiting on a batch that hasn't ended (or whose results couldn't be read). Each run commits filing by filing, so a crash keeps results already paid for. It prints one line of counts and one line per failure to stderr, and exits 1 if any filing failed. What a filing's stage means afterward:

- `checked`: all four checks passed. `obr publish` makes its facts public; its texts wait for review.
- `in_review`: a check failed, the model refused, or the output couldn't be written. Nothing from it is published until a reviewer fixes its facts and clears it, or requeues it for extraction (see [Review](#review)).
- `out_of_scope`: the meeting date, confirmed by its verbatim quote, isn't covered by any index membership, so the filing is set aside. A date its quote doesn't back goes to `in_review` instead.
- `published` (after `obr publish`): its facts are public. Each text becomes public when a reviewer approves it.
- still `fetched`: the call failed (API error, truncated or unparseable output) or the text file is missing. Run extract again. After 2 billed failures a filing is capped: later runs skip it and report it as an error until someone looks at it.

### Review

A person reviews every filing before its texts are public. Facts publish on passing checks; a filing that failed a check stays `in_review` until a reviewer clears it. Set `OBR_REVIEWER` to your name for these commands. They need the database URL but not the EDGAR User-Agent.

A typical pass:

1. `uv run obr publish` makes the facts of `checked` filings public.
2. `uv run obr review next` walks the queue. Approve, edit, or reject each text; fix facts on `in_review` filings with `obr review fix`, then clear them. Skipping an item leaves it for the next pass, and quitting keeps every decision made.
3. `uv run obr review regenerate` rewrites the texts you rejected (costs money). They come back to the queue as pending.
4. `uv run obr review next` again for the rewritten texts.
5. `uv run obr publish` again for filings you cleared, so their facts go public.

| Command | What it does |
|---|---|
| `uv run obr review queue` | List filings waiting for review, oldest filing first: `in_review` filings, and filings with pending texts or texts to write by hand |
| `uv run obr review next` | Walk the queue. Each item is shown before its prompt. Per item: approve all its texts, approve, edit, or reject one, skip, or quit. An edit reads lines until one holding only `.`, then a reason, then asks before saving; a text can't be edited to empty, so approve a text that should stay empty. Per `in_review` filing: clear, requeue, or keep. Every decision is saved as you make it |
| `uv run obr review show ACCESSION` | Print one filing: facts, failed checks, quotes with links that jump to them in the EDGAR document, texts with their ids, and each side's argument length (flagged `UNEVEN` when they differ by more than a quarter, `ONE-SIDED` when one is missing) |
| `uv run obr review approve TEXT_ID` / `approve-item ITEM_ID` | Approve one text, or all of an item's pending texts |
| `uv run obr review edit TEXT_ID --body TEXT --reason R` | Replace a text with your own, which is approved. The model's text is kept |
| `uv run obr review reject TEXT_ID --reason R` | Reject a text so `regenerate` rewrites it. A second rejection in a row marks it for writing by hand (use `edit`) |
| `uv run obr review fix ITEM_ID FIELD VALUE --reason R` | Change one fact of an item: `title`, `type`, `category`, `effect`, `lead_filer`, `representative`, `co_filers`, `board_recommendation`, `vote_base`, `vote_threshold_pct`, `abstentions_count_against`, `vote_standard_text`, `request`, or `status`. `""` means none; separate co-filers with `;`; abstentions take `true` or `false` |
| `uv run obr review fix-meeting ACCESSION FIELD VALUE --reason R` | Change one fact of a meeting: `kind`, `meeting_date`, `meeting_time`, `format`, `record_date`, or `shares_outstanding`. Dates are `YYYY-MM-DD` |
| `uv run obr review clear ACCESSION --reason R` | Move an `in_review` filing to `checked` once its facts are right |
| `uv run obr review requeue ACCESSION --reason R` | Send an `in_review`, `checked`, or `out_of_scope` filing back to extraction (for a missing or extra item). Deletes its extraction rows, including billed runs, so their token counts are lost and the two-failure cap starts over; keeps the review record. Refused once any of its items is published |
| `uv run obr review regenerate [--limit N]` | Rewrite rejected texts with Claude, one call per filing. **Costs money**; pass the API key as for `extract`. A text you edited or requeued while the call ran is left as you changed it and reported |

A change to a public value writes a correction record, which the public correction log shows: a fact fix on a published item, an edit of an approved text on a published item, or approving a new version that replaces a public text.

### Discovery notices

`obr discover` may print two kinds of notice. Neither changes the database; each asks a person to check something:

- **8-K12B** (`covered company … filed an 8-K12B`): a successor company has taken over as the filer, for example after a holding-company reorganization. The successor usually files under a new CIK.
- **CIK drift** (`EDGAR now maps this ticker to CIK …, which isn't linked`): EDGAR's ticker file maps a covered company's ticker to a CIK the company doesn't have yet. Discovery never polls an unlinked CIK, so that CIK's filings are missed until it is linked. If the notice says EDGAR's ticker file doesn't list the ticker at all, check whether the ticker changed.

To link a new CIK, confirm it from the 8-K12B or the company's filings on EDGAR, add a row to `data/company_ciks.csv` with the dates each CIK was in use and a source note, then run `uv run obr companies sync` again. Linking is always a manual edit.

## License

Code is licensed AGPL-3.0-only; see `LICENSE`. The license for generated content is set by ADR-0002 in [OpenBoardroom/architecture](https://github.com/OpenBoardroom/architecture).
