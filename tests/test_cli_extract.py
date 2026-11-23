"""Tests for `obr extract`: argument parsing, mode dispatch, and exit codes.

The stages themselves are tested against Postgres in tests/pipeline/test_extract.py;
here they are replaced so no database or network is touched.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import pytest

from openboardroom.cli import main
from openboardroom.extract.llm import LlmSettings
from openboardroom.pipeline import extract_cli
from openboardroom.pipeline.extract import ExtractResult


class FakeConn:
    """Stands in for a psycopg connection used as a context manager."""

    def __enter__(self) -> "FakeConn":
        """Enter the context."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Leave the context."""
        return None


def returning(value: ExtractResult) -> Callable[..., ExtractResult]:
    """Make a stage stand-in that ignores its arguments and returns `value`."""

    def stage(*args: Any, **kwargs: Any) -> ExtractResult:
        return value

    return stage


def result(**kwargs: Any) -> ExtractResult:
    base: dict[str, Any] = {
        "checked": 0,
        "in_review": 0,
        "out_of_scope": 0,
        "failed": 0,
        "submitted": 0,
    }
    return ExtractResult(**{**base, **kwargs})


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Fake the database and the stage functions; record each stage call."""
    monkeypatch.setenv("OBR_DATABASE_URL", "postgres://x")
    monkeypatch.setenv("OBR_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        extract_cli.psycopg,
        "connect",
        lambda url, **kw: FakeConn(),  # pyright: ignore
    )
    log: list[tuple[str, dict[str, Any]]] = []

    def direct(conn: Any, client: Any, settings: LlmSettings, text_dir: Path, **kw: Any) -> Any:
        log.append(("direct", {"text_dir": text_dir, "settings": settings, **kw}))
        return result(checked=1)

    def submit(conn: Any, client: Any, settings: LlmSettings, text_dir: Path, **kw: Any) -> Any:
        log.append(("submit", kw))
        return result(submitted=0)

    def collect(conn: Any, client: Any, settings: LlmSettings, text_dir: Path) -> Any:
        log.append(("collect", {}))
        return result(checked=2)

    monkeypatch.setattr(extract_cli, "extract_direct", direct)
    monkeypatch.setattr(extract_cli, "submit", submit)
    monkeypatch.setattr(extract_cli, "collect", collect)
    return log


def test_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["extract", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--submit" in out and "--collect" in out and "--filing" in out


def test_direct_mode_passes_limit_filing_and_text_dir(
    calls: list[tuple[str, dict[str, Any]]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["extract", "--limit", "3", "--filing", "0001-24-000001"]) == 0
    [(mode, kw)] = calls
    assert mode == "direct"
    assert kw["limit"] == 3 and kw["accession"] == "0001-24-000001"
    assert kw["text_dir"] == tmp_path / "text"
    assert kw["settings"] == LlmSettings("claude-opus-5-5", "high", 64000)
    assert "1 checked" in capsys.readouterr().out


def test_direct_mode_defaults(calls: list[tuple[str, dict[str, Any]]]) -> None:
    assert main(["extract"]) == 0
    assert calls[0][1]["limit"] is None and calls[0][1]["accession"] is None


def test_collect_mode(calls: list[tuple[str, dict[str, Any]]]) -> None:
    assert main(["extract", "--collect"]) == 0
    assert [mode for mode, _ in calls] == ["collect"]


@pytest.mark.parametrize(
    "argv",
    [
        ["extract", "--submit", "--collect"],
        ["extract", "--collect", "--limit", "2"],
        ["extract", "--collect", "--filing", "a"],
        ["extract", "--submit", "--filing", "a"],
        ["extract", "--limit", "0"],
        ["extract", "--limit", "x"],
    ],
)
def test_invalid_combinations_exit_2(argv: list[str], calls: list[Any]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert calls == []


def test_failures_print_to_stderr_and_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        extract_cli,
        "extract_direct",
        returning(result(checked=1, failed=1, errors=["A-1: API error", "B-2: capped"])),
    )
    assert main(["extract"]) == 1
    captured = capsys.readouterr()
    assert "1 checked" in captured.out and "1 failed" in captured.out
    assert "A-1: API error" in captured.err and "B-2: capped" in captured.err


def test_in_review_alone_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any]
) -> None:
    monkeypatch.setattr(extract_cli, "extract_direct", returning(result(in_review=2)))
    assert main(["extract"]) == 0


def test_submit_sends_batches_of_at_most_100_until_limit(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    sizes: list[int | None] = []

    def submit(conn: Any, client: Any, settings: Any, text_dir: Any, limit: int | None) -> Any:
        sizes.append(limit)
        assert limit is not None
        return result(submitted=limit)

    monkeypatch.setattr(extract_cli, "submit", submit)
    assert main(["extract", "--submit", "--limit", "250"]) == 0
    assert sizes == [100, 100, 50]


def test_submit_without_limit_runs_until_nothing_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending = [100, 30]

    def submit(conn: Any, client: Any, settings: Any, text_dir: Any, limit: int | None) -> Any:
        assert limit == extract_cli.BATCH_SIZE
        return result(submitted=pending.pop(0) if pending else 0)

    monkeypatch.setattr(extract_cli, "submit", submit)
    assert main(["extract", "--submit"]) == 0
    assert "130 submitted" in capsys.readouterr().out


def test_submit_dedupes_repeated_errors_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    replies = [
        result(submitted=5, failed=1, errors=["A-1: no text"]),
        result(failed=1, errors=["A-1: no text"]),
    ]

    def submit(*args: Any, **kwargs: Any) -> ExtractResult:
        return replies.pop(0)

    monkeypatch.setattr(extract_cli, "submit", submit)
    assert main(["extract", "--submit"]) == 1
    captured = capsys.readouterr()
    assert captured.err.count("A-1: no text") == 1
    assert "1 failed, 5 submitted" in captured.out


def test_invalid_settings_exit_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OBR_EXTRACT_EFFORT", "extreme")
    assert main(["extract"]) == 1
    err = capsys.readouterr().err
    assert "OBR_EXTRACT_EFFORT" in err and "Traceback" not in err


def test_missing_api_key_is_one_line_error(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def direct(conn: Any, client: anthropic.Anthropic, *a: Any, **k: Any) -> Any:
        # The first real request is where the SDK notices there are no credentials.
        client.messages.count_tokens(model="m", messages=[{"role": "user", "content": "x"}])

    monkeypatch.setattr(extract_cli, "extract_direct", direct)
    assert main(["extract"]) == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert err.strip().count("\n") == 0 and "Traceback" not in err


def test_other_type_errors_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any]
) -> None:
    def boom(*args: Any, **kwargs: Any) -> ExtractResult:
        raise TypeError("a real bug")

    monkeypatch.setattr(extract_cli, "extract_direct", boom)
    with pytest.raises(TypeError, match="a real bug"):
        main(["extract"])
