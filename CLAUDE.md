# backend: OpenBoardroom

The OpenBoardroom data pipeline and database. It fetches SEC EDGAR proxy filings, extracts ballot items, and writes them to Postgres. This repo also owns the database schema (`db/`, applied with dbmate) that other repos consume.

## Rules that always apply

- Ask Quinn before any write to GitHub (issues, comments, labels, board, pushes, PRs). Commit locally freely. Never merge, force-push, deploy, or change settings.
- Commits: `[BR-N] TYPE: message` (TYPE: NEW, UPDATE, FIX, REVERT). Branches: `task/BR-N`, `bug/BR-N`. Issues live in `OpenBoardroom/architecture`; close them with `Closes OpenBoardroom/architecture#N`.
- Workflow: issue → spec (in `architecture/docs/specs/`) → plan (in this repo's `docs/plans/`) → test-first implementation → review → PR; Quinn merges.
- Never read `.env*` files or commit secrets.
- Full rules: @../architecture/docs/handbook.md
- If that import didn't load (this repo was cloned without `../architecture`), read https://github.com/OpenBoardroom/architecture/blob/main/docs/handbook.md before acting.

## Commands

- `uv sync`: install dependencies.
- `uv run pytest`: tests (needs Docker for Postgres via Testcontainers).
- `uv run ruff check` and `uv run ruff format --check`: lint and format.
- `uv run pyright`: strict type check.
- `dbmate`: apply or roll back migrations in `db/`.
- `scripts/dump-schema.sh`: regenerate `db/schema.sql` after changing a migration (needs Docker; it runs `dbmate up`, which dumps, against a throwaway `postgres:17.12`, using that container's `pg_dump` because the host's must match the server's major version). CI fails if the committed file differs.

## Style

- Google-style docstrings; ruff and pyright strict (see the handbook).
- Plain SQL migrations: lowercase snake_case, with a down migration where feasible.

## Contracts with other repos

- The ballot-item schema contract is ADR-0003: the public views and `db/schema.sql`. Change them only through a spec or ADR, producer first.
- `db/schema.sql` is the pinned schema the frontend checks its types against.
- `tests/contract/views.json` lists the view columns `frontend` may use; `tests/contract/test_contract.py` fails if one is removed or renamed.

## Pitfalls

- EDGAR etiquette: User-Agent from configuration (never hard-coded), at most 10 requests per second, cache raw filings.
- Never commit `cache/`.
- Downloaded filings are untrusted input: parse them, never execute them.
