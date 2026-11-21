"""Shared fixtures: a migrated Postgres 17 database and per-test connections."""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from testcontainers.community.postgres import PostgresContainer

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

# Test-only credentials for a throwaway container. In production the
# `infrastructure` repo creates these roles; migrations only grant to them.
ROLE_PASSWORDS = {"pipeline_rw": "pipeline_rw_test", "web_ro": "web_ro_test"}

Dbmate = Callable[..., subprocess.CompletedProcess[str]]


def with_credentials(url: str, user: str, password: str, dbname: str | None = None) -> str:
    """Return a connection URL for ``url``'s server with other credentials or database.

    Args:
        url: A ``postgresql://`` URL.
        user: Role to connect as.
        password: That role's password.
        dbname: Database to connect to; defaults to the one in ``url``.

    Returns:
        A ``postgresql://`` URL.
    """
    parts = conninfo_to_dict(url)
    host, port = parts["host"], parts["port"]
    database = dbname or parts["dbname"]
    return f"postgresql://{user}:{password}@{host}:{port}/{database}?sslmode=disable"


@pytest.fixture(scope="session")
def dbmate() -> Dbmate:
    """Return a function that runs dbmate against a URL with this repo's migrations."""

    def run(url: str, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "dbmate",
                "--url",
                url,
                "--migrations-dir",
                str(MIGRATIONS_DIR),
                "--no-dump-schema",
                *args,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"dbmate {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    return run


@pytest.fixture(scope="session")
def pg_url(dbmate: Dbmate) -> Iterator[str]:
    """Start Postgres 17, create the application roles, and apply all migrations.

    Yields:
        A superuser ``postgresql://`` URL for the migrated database.
    """
    with PostgresContainer("postgres:17", driver=None) as container:
        raw = container.get_connection_url()
        parts = conninfo_to_dict(raw)
        url = with_credentials(
            raw, str(parts["user"]), str(parts["password"]), str(parts["dbname"])
        )
        with psycopg.connect(url, autocommit=True) as admin:
            for role, password in ROLE_PASSWORDS.items():
                admin.execute(
                    sql.SQL("create role {} login password {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        dbmate(url, "up")
        yield url


@pytest.fixture(scope="session")
def role_urls(pg_url: str) -> dict[str, str]:
    """Map each application role (``pipeline_rw``, ``web_ro``) to a URL that logs in as it."""
    return {
        role: with_credentials(pg_url, role, password) for role, password in ROLE_PASSWORDS.items()
    }


@pytest.fixture(scope="session")
def admin_url_for(pg_url: str) -> Callable[[str], str]:
    """Return a function giving a superuser URL for another database on the same server."""
    parts = conninfo_to_dict(pg_url)

    def url_for(dbname: str) -> str:
        return with_credentials(pg_url, str(parts["user"]), str(parts["password"]), dbname)

    return url_for


@pytest.fixture
def conn(role_urls: dict[str, str]) -> Iterator[psycopg.Connection]:
    """Connect as ``pipeline_rw`` inside a transaction that is rolled back after the test."""
    connection = psycopg.connect(role_urls["pipeline_rw"])
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
