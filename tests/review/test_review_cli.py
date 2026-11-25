"""Tests for the `obr review` commands; store calls are replaced, the parsing is real."""

import psycopg
import pytest

from openboardroom.cli import main
from openboardroom.review import store
from openboardroom.review.store import FilingView, QueuedFiling, ReviewError


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, role_urls: dict[str, str]) -> pytest.MonkeyPatch:
    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.setenv("OBR_REVIEWER", "quinn")
    monkeypatch.delenv("OBR_EDGAR_USER_AGENT", raising=False)
    return monkeypatch


def test_cli_fix_bad_value_exits_1_with_message(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake(conn: psycopg.Connection, *args: object) -> int:
        calls.append((conn.autocommit, *args))
        raise ReviewError("category: 'merger' is not allowed")

    env.setattr(store, "fix_item", fake)

    code = main(["review", "fix", "7", "category", "merger", "--reason", "try"])

    assert code == 1
    assert calls == [(True, 7, "category", "merger", "quinn", "try")]
    assert "category: 'merger' is not allowed" in capsys.readouterr().err


def test_cli_queue_lists_oldest_first(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(conn: psycopg.Connection) -> list[QueuedFiling]:
        return [
            QueuedFiling(1, "0000000001-26-000001", "BRK-B", "in_review", 12),
            QueuedFiling(2, "0000000002-26-000001", "AAPL", "checked", 15),
        ]

    env.setattr(store, "queue", fake)

    assert main(["review", "queue"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["0000000001-26-000001", "BRK-B", "in_review", "12", "pending"]
    assert out[-1] == "2 filings in the queue"


@pytest.mark.parametrize(
    ("argv", "name", "expected"),
    [
        (["approve", "21"], "approve_text", (21, "quinn")),
        (["approve-item", "7"], "approve_item", (7, "quinn")),
        (["edit", "21", "--body", "New.", "--reason", "r"], "edit_text",
         (21, "New.", "quinn", "r")),
        (["reject", "21", "--reason", "r"], "reject_text", (21, "quinn", "r")),
        (["fix-meeting", "0000000001-26-000001", "kind", "special", "--reason", "r"],
         "fix_meeting", ("0000000001-26-000001", "kind", "special", "quinn", "r")),
        (["clear", "0000000001-26-000001", "--reason", "r"], "clear",
         ("0000000001-26-000001", "quinn", "r")),
        (["requeue", "0000000001-26-000001", "--reason", "r"], "requeue",
         ("0000000001-26-000001", "quinn", "r")),
    ],
)  # fmt: skip
def test_cli_decisions_call_the_store(
    env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    name: str,
    expected: tuple[object, ...],
) -> None:
    calls: list[tuple[object, ...]] = []

    def fake(conn: psycopg.Connection, *args: object) -> int:
        calls.append(args)
        return 99

    env.setattr(store, name, fake)

    assert main(["review", *argv]) == 0
    assert calls == [expected]
    assert "recorded" in capsys.readouterr().out


def test_cli_show_unknown_and_known(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(conn: psycopg.Connection, accession: str) -> FilingView | None:
        if accession == "0000000000-00-000000":
            return None
        return FilingView(1, accession, "AAPL", "checked", "https://x", None, [], [])

    env.setattr(store, "load", fake)
    assert main(["review", "show", "0000000000-00-000000"]) == 1
    assert "no filing 0000000000-00-000000" in capsys.readouterr().err
    assert main(["review", "show", "0000320193-26-000001"]) == 0
    assert "AAPL 0000320193-26-000001 [checked]" in capsys.readouterr().out


def test_cli_next_runs_the_session(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def fake(conn: psycopg.Connection, reviewer: str, ask: object, out: object) -> int:
        seen.append(reviewer)
        return 4

    env.setattr("openboardroom.review.session.run", fake)

    assert main(["review", "next"]) == 0
    assert seen == ["quinn"]
    assert "4 decisions recorded" in capsys.readouterr().out


def test_cli_needs_reviewer(env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env.delenv("OBR_REVIEWER")
    assert main(["review", "queue"]) == 1
    assert "OBR_REVIEWER" in capsys.readouterr().err
