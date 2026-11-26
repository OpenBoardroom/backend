"""Tests for the publish stage: facts of checked filings go public (spec §7.1)."""

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from review_seed import seed_filing

from openboardroom.cli import main
from openboardroom.pipeline import publish as publish_stage
from openboardroom.pipeline.publish import PublishResult, publish

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _stages(conn: psycopg.Connection) -> list[tuple[str, str, int]]:
    return conn.execute(
        "select f.accession, f.stage, count(bi.published_at) from filings f "
        "join meetings m on m.proxy_filing_id = f.id join ballot_items bi on bi.meeting_id = m.id "
        "group by f.accession, f.stage order by f.accession"
    ).fetchall()


def test_publish_checked_filings_only(conn: psycopg.Connection) -> None:
    for n, stage in enumerate(("checked", "in_review", "fetched", "out_of_scope"), 1):
        seed_filing(conn, f"000000000{n}-26-000001", stage=stage, cik=f"000000000{n}")

    result = publish(conn, NOW)

    assert result == PublishResult(filings=1, items=2)
    assert _stages(conn) == [
        ("0000000001-26-000001", "published", 2),
        ("0000000002-26-000001", "in_review", 0),
        ("0000000003-26-000001", "fetched", 0),
        ("0000000004-26-000001", "out_of_scope", 0),
    ]


def test_publish_is_idempotent(conn: psycopg.Connection) -> None:
    seed_filing(conn)
    publish(conn, NOW)

    assert publish(conn, datetime(2026, 12, 1, tzinfo=UTC)) == PublishResult(filings=0, items=0)
    assert conn.execute("select distinct published_at from ballot_items").fetchall() == [(NOW,)]


@pytest.fixture
def admin_conn(pg_url: str) -> Iterator[psycopg.Connection]:
    """A superuser transaction, rolled back after the test, so it can switch to ``web_ro``."""
    connection = psycopg.connect(pg_url)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def test_published_views_show_facts_and_only_approved_texts(
    admin_conn: psycopg.Connection,
) -> None:
    seeded = seed_filing(admin_conn)
    publish(admin_conn, NOW)
    admin_conn.execute(
        "update item_texts set review_status = 'approved' "
        "where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[0],),
    )

    admin_conn.execute("set local role web_ro")

    items = admin_conn.execute("select number from published_ballot_items order by id").fetchall()
    texts = admin_conn.execute("select ballot_item_id, kind from published_item_texts").fetchall()
    assert items == [("1",), ("2",)]
    assert texts == [(seeded.item_ids[0], "summary")]


def test_cli_publish_prints_counts(
    monkeypatch: pytest.MonkeyPatch, role_urls: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[bool] = []

    def fake(conn: psycopg.Connection, now: datetime) -> PublishResult:
        calls.append(now.tzinfo is not None)
        return PublishResult(filings=3, items=21)

    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.delenv("OBR_EDGAR_USER_AGENT", raising=False)
    monkeypatch.setattr(publish_stage, "publish", fake)

    assert main(["publish"]) == 0
    assert calls == [True]
    assert "publish: 3 filings, 21 items" in capsys.readouterr().out


def test_publish_leaves_supplements_alone(conn: psycopg.Connection) -> None:
    # Final review I-1: supplements are never published; only their changes are.
    from review_seed import add_supplement

    seed_filing(conn)
    add_supplement(conn, "0000320193-26-000101", "2026-01-20", stage="checked")

    assert publish(conn, NOW) == PublishResult(filings=1, items=2)
    assert conn.execute("select stage from filings where form = 'DEFA14A'").fetchone() == (
        "checked",
    )
