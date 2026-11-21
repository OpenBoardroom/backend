# OpenBoardroom backend

The data pipeline and database schema for OpenBoardroom, a neutral civic platform that explains corporate ballot items. The pipeline discovers SEC EDGAR proxy filings, extracts ballot items, and writes them to Postgres. The `obr` command runs its stages.

## Setup

- Python 3.13 with [uv](https://docs.astral.sh/uv/): `uv sync`
- [dbmate](https://github.com/amacneil/dbmate) applies the migrations in `db/`.
- Docker must be running: tests start a real Postgres with Testcontainers.
- Run the checks: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`.

See `CLAUDE.md` and the [engineering handbook](https://github.com/OpenBoardroom/architecture/blob/main/docs/handbook.md) for working rules.

## License

Code is licensed AGPL-3.0-only; see `LICENSE`. The license for generated content is set by ADR-0002 in [OpenBoardroom/architecture](https://github.com/OpenBoardroom/architecture).
