# OpenBoardroom backend

The data pipeline and database schema for OpenBoardroom, a neutral civic platform that explains corporate ballot items. The pipeline discovers SEC EDGAR proxy filings, extracts ballot items, and writes them to Postgres. The `obr` command runs its stages.

## Setup

- Python 3.13 with [uv](https://docs.astral.sh/uv/): `uv sync`
- [dbmate](https://github.com/amacneil/dbmate) applies the migrations in `db/`.
- Docker must be running: tests start a real Postgres with Testcontainers.
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
| `ANTHROPIC_API_KEY` | for `extract` | Anthropic API key. The Anthropic SDK reads it; `obr` never does. Don't keep it in your shell environment or a file; pass it per command (see [Extract](#extract)) |

### Database

The `pipeline_rw` and `web_ro` roles must exist before you apply the migrations: the migrations grant to them but don't create them. In production the `infrastructure` repo creates them; locally, create them yourself as a superuser. Then apply the migrations as the database owner:

```sh
DATABASE_URL=postgres://OWNER:PASSWORD@HOST:5432/DBNAME dbmate up
```

### Stages

Run them in order:

1. `uv run obr companies sync` loads `data/companies.csv` (companies and index memberships) and `data/company_ciks.csv` (the EDGAR filer CIKs each company has used).
2. `uv run obr discover --since YYYY-MM-DD --until YYYY-MM-DD` finds new proxy statements (DEF 14A), proxy supplements (DEFA14A), and vote-result 8-Ks (item 5.07) filed in that window, and records them at stage `discovered`. A proxy is filed up to about six months before its meeting, so to cover a season, start the window about six months before the season's first meeting.
3. `uv run obr fetch` downloads each discovered filing's primary document into the cache, converts it to plain text, and advances it to `fetched`. It prints each failed filing as it goes and exits non-zero if any failed. If EDGAR refuses requests (HTTP 403 or 429) or can't be reached, it stops the whole run early and says why; the remaining filings stay `discovered`, so run it again later.

4. `uv run obr extract` reads each fetched proxy statement with Claude, checks the result, and advances the filing to `checked`, `in_review`, or `out_of_scope` (see [Extract](#extract)).

All stages are safe to rerun: discovery skips filings it already has, fetch reuses cached documents, and extract only takes filings still at `fetched`.

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

- `checked`: all four checks passed. It is ready for review.
- `in_review`: a check failed, the model refused, or the output couldn't be written. A reviewer looks at the items it did produce; nothing is published.
- `out_of_scope`: the meeting date, confirmed by its verbatim quote, isn't covered by any index membership, so the filing is set aside. A date its quote doesn't back goes to `in_review` instead.
- still `fetched`: the call failed (API error, truncated or unparseable output) or the text file is missing. Run extract again. After 2 billed failures a filing is capped: later runs skip it and report it as an error until someone looks at it.

### Discovery notices

`obr discover` may print two kinds of notice. Neither changes the database; each asks a person to check something:

- **8-K12B** (`covered company … filed an 8-K12B`): a successor company has taken over as the filer, for example after a holding-company reorganization. The successor usually files under a new CIK.
- **CIK drift** (`EDGAR now maps this ticker to CIK …, which isn't linked`): EDGAR's ticker file maps a covered company's ticker to a CIK the company doesn't have yet. Discovery never polls an unlinked CIK, so that CIK's filings are missed until it is linked. If the notice says EDGAR's ticker file doesn't list the ticker at all, check whether the ticker changed.

To link a new CIK, confirm it from the 8-K12B or the company's filings on EDGAR, add a row to `data/company_ciks.csv` with the dates each CIK was in use and a source note, then run `uv run obr companies sync` again. Linking is always a manual edit.

## License

Code is licensed AGPL-3.0-only; see `LICENSE`. The license for generated content is set by ADR-0002 in [OpenBoardroom/architecture](https://github.com/OpenBoardroom/architecture).
