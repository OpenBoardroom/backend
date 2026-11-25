"""Tests for the supplements stage: direct, submit, and collect (spec §6.3 item 3, §7.1)."""

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from llm_replay import Recorder, json_response, jsonl_response, mock_client, stream_response
from review_seed import SETTINGS, add_supplement, seed_filing
from test_extract import BATCH_ID, batch, result_line

from openboardroom.cli import main
from openboardroom.pipeline import supplements as stage
from openboardroom.pipeline.supplements import SupplementResult, run

FIXTURE = Path(__file__).parent.parent / "fixtures" / "filings" / "gs-2026-defa14a.txt"
SUPP = "0000320193-26-000101"
QUOTE = (
    "On April 1, 2026, the American Family Association withdrew "
    "“Item 5. Shareholder Proposal Regarding Charitable Giving Reporting.”"
)


def _message(output: object) -> dict[str, Any]:
    return {
        "id": "msg_fixture_supplement", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(output)}],
        "stop_reason": "end_turn", "stop_sequence": None, "stop_details": None,
        "usage": {"input_tokens": 3000, "output_tokens": 120,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }  # fmt: skip


def _withdraw(number: int = 2, quote: str = QUOTE) -> dict[str, Any]:
    return {
        "changes": [{
            "type": "withdraw", "number": number, "part": "", "recommendation": None,
            "nominees": [], "field": None, "value": "", "item": None, "source_quote": quote,
        }]
    }  # fmt: skip


def _setup(conn: psycopg.Connection, tmp_path: Path, filed_at: str = "2026-01-20") -> int:
    seed_filing(conn)
    supp_id = add_supplement(conn, SUPP, filed_at)
    (tmp_path / f"{SUPP}.txt").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return supp_id


def _state(conn: psycopg.Connection) -> tuple[str, list[str]]:
    stage_row = conn.execute("select stage from filings where accession = %s", (SUPP,)).fetchone()
    statuses = conn.execute("select status from ballot_items order by id").fetchall()
    assert stage_row is not None
    return stage_row[0], [s for (s,) in statuses]


def test_direct_withdrawal_end_to_end(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    recorder = Recorder(stream_response(_message(_withdraw())))

    result = run(conn, mock_client(recorder), SETTINGS, tmp_path, mode="direct")

    assert result == SupplementResult(checked=1, applied=1)
    assert _state(conn) == ("checked", ["active", "withdrawn"])
    run_row = conn.execute(
        "select kind, status, prompt_version, schema_version from extraction_runs "
        "where kind = 'supplement'"
    ).fetchone()
    assert run_row == ("supplement", "succeeded", "1", "1")
    sent = recorder.body()
    assert '"number": "2"' in sent["messages"][0]["content"]
    assert sent["output_config"]["format"]["schema"]["required"] == ["changes"]


def test_no_changes_is_checked(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    recorder = Recorder(stream_response(_message({"changes": []})))

    result = run(conn, mock_client(recorder), SETTINGS, tmp_path, mode="direct")

    assert result == SupplementResult(checked=1)
    assert _state(conn) == ("checked", ["active", "active"])


def test_bad_quote_goes_to_review_and_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _setup(conn, tmp_path)
    bad = _withdraw(quote="the proponent has withdrawn this proposal from the annual meeting")
    recorder = Recorder(stream_response(_message(bad)))

    result = run(conn, mock_client(recorder), SETTINGS, tmp_path, mode="direct")

    assert result.in_review == 1
    assert _state(conn) == ("in_review", ["active", "active"])
    assert conn.execute("select count(*) from pending_changes").fetchone() == (0,)
    failed = conn.execute(
        "select check_name, detail from check_results where not passed"
    ).fetchall()
    assert failed == [("quotes", "change 1 (withdraw): source_quote not found in filing text")]


def test_unlinked_supplement_is_out_of_scope(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path, filed_at="2026-03-02")
    recorder = Recorder()

    result = run(conn, mock_client(recorder), SETTINGS, tmp_path, mode="direct")

    assert result == SupplementResult(out_of_scope=1)
    assert recorder.requests == []
    assert _state(conn)[0] == "out_of_scope"


def test_call_failure_stays_fetched_and_caps(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    truncated = {**_message({"changes": []}), "stop_reason": "max_tokens"}

    for _ in range(2):
        result = run(
            conn, mock_client(Recorder(stream_response(truncated))), SETTINGS, tmp_path,
            mode="direct",
        )  # fmt: skip
        assert result.failed == 1
    capped = run(conn, mock_client(Recorder()), SETTINGS, tmp_path, mode="direct")

    assert _state(conn)[0] == "fetched"
    assert capped.failed == 0 and "needs a person" in capped.errors[0]


def test_refusal_goes_to_review(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    details = {"type": "refusal", "category": "x", "explanation": "y"}
    refusal = {**_message({"changes": []}), "stop_reason": "refusal", "stop_details": details}

    result = run(
        conn, mock_client(Recorder(stream_response(refusal))), SETTINGS, tmp_path, mode="direct"
    )

    assert result.in_review == 1


def test_missing_text_fails(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    (tmp_path / f"{SUPP}.txt").unlink()

    result = run(conn, mock_client(Recorder()), SETTINGS, tmp_path, mode="direct")

    assert result.failed == 1 and "text file missing" in result.errors[0]


def test_submit_then_collect(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)

    submitted = run(
        conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit"
    )
    waiting = run(
        conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="collect"
    )
    collected = run(
        conn,
        mock_client(
            Recorder(
                batch("ended"), batch("ended"),
                jsonl_response([result_line(SUPP, _message(_withdraw()))]),
            )
        ),
        SETTINGS, tmp_path, mode="collect",
    )  # fmt: skip

    assert submitted == SupplementResult(submitted=1)
    assert waiting == SupplementResult(waiting=1)
    assert collected == SupplementResult(checked=1, applied=1)
    assert _state(conn) == ("checked", ["active", "withdrawn"])
    assert conn.execute(
        "select status, batch_id from extraction_runs where kind = 'supplement'"
    ).fetchall() == [("succeeded", BATCH_ID)]


def test_collect_retries_an_errored_result_directly(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _setup(conn, tmp_path)
    run(conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit")

    collected = run(
        conn,
        mock_client(Recorder(
            batch("ended"), batch("ended"), jsonl_response([result_line(SUPP, None)]),
            stream_response(_message({"changes": []})),
        )),
        SETTINGS, tmp_path, mode="collect",
    )  # fmt: skip

    assert collected == SupplementResult(checked=1)
    assert [s for (s,) in conn.execute(
        "select status from extraction_runs where kind = 'supplement' order by id"
    ).fetchall()] == ["failed", "succeeded"]  # fmt: skip


def test_extract_collect_ignores_supplement_runs(conn: psycopg.Connection, tmp_path: Path) -> None:
    from openboardroom.extract import store

    _setup(conn, tmp_path)
    run(conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit")

    assert store.submitted_runs(conn) == []


def test_cli_prints_counts(
    monkeypatch: pytest.MonkeyPatch,
    role_urls: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[str, int | None, str | None]] = []

    def fake(conn: object, client: object, settings: object, text_dir: Path, **kwargs: Any) -> Any:
        seen.append((kwargs["mode"], kwargs.get("limit"), kwargs.get("accession")))
        return SupplementResult(checked=3, in_review=1, out_of_scope=2, applied=1, pending=2,
                                failed=1, errors=["x: bad"])  # fmt: skip

    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.setenv("OBR_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setattr(stage, "run", fake)

    assert main(["supplements", "--filing", SUPP]) == 1

    captured = capsys.readouterr()
    assert seen == [("direct", None, SUPP)]
    assert (
        "supplements: 3 checked, 1 in review, 2 out of scope, 1 failed, 0 submitted, "
        "0 waiting; 1 changes applied, 2 pending" in captured.out
    )
    assert "x: bad" in captured.err
    assert main(["supplements", "--collect"]) == 1
    assert seen[-1] == ("collect", None, None)


def test_limit_and_empty_submit(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    second = "0000320193-26-000102"
    add_supplement(conn, second, "2026-01-21")
    (tmp_path / f"{second}.txt").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    recorder = Recorder(stream_response(_message({"changes": []})))

    assert run(conn, mock_client(recorder), SETTINGS, tmp_path, mode="direct", limit=1).checked == 1
    assert len(recorder.requests) == 1
    run(conn, mock_client(Recorder(stream_response(_message({"changes": []})))), SETTINGS,
        tmp_path, mode="direct")  # fmt: skip
    nothing = Recorder()
    assert run(conn, mock_client(nothing), SETTINGS, tmp_path, mode="submit") == SupplementResult()
    assert nothing.requests == []


def test_submit_api_error_records_nothing(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    error = {"type": "error", "error": {"type": "api_error", "message": "Fixture error."}}

    result = run(
        conn, mock_client(Recorder(json_response(error, status=500))), SETTINGS, tmp_path,
        mode="submit",
    )  # fmt: skip

    assert result.failed == 1 and "batch submit failed" in result.errors[0]
    assert conn.execute(
        "select count(*) from extraction_runs where kind = 'supplement'"
    ).fetchone() == (0,)


def test_collect_without_text_or_link_fails_the_run(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    _setup(conn, tmp_path)
    run(conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit")
    (tmp_path / f"{SUPP}.txt").unlink()

    result = run(
        conn,
        mock_client(Recorder(
            batch("ended"), batch("ended"),
            jsonl_response([result_line(SUPP, _message({"changes": []}))]),
        )),
        SETTINGS, tmp_path, mode="collect",
    )  # fmt: skip

    assert result.failed == 1 and "text file missing" in result.errors[0]
    assert conn.execute(
        "select status from extraction_runs where kind = 'supplement'"
    ).fetchone() == ("failed",)


def test_collect_when_proxy_lost_its_items(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    run(conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit")
    for table in ("item_texts", "nominees", "ballot_items"):
        conn.execute(f"delete from {table}")  # noqa: S608 - fixed names

    result = run(
        conn,
        mock_client(Recorder(
            batch("ended"), batch("ended"),
            jsonl_response([result_line(SUPP, _message({"changes": []}))]),
        )),
        SETTINGS, tmp_path, mode="collect",
    )  # fmt: skip

    assert "no longer linked to a meeting" in result.errors[0]


def test_collect_unreadable_batch_keeps_waiting(conn: psycopg.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    run(conn, mock_client(Recorder(batch("in_progress"))), SETTINGS, tmp_path, mode="submit")
    error = {"type": "error", "error": {"type": "api_error", "message": "Fixture error."}}

    result = run(
        conn, mock_client(Recorder(json_response(error, status=500))), SETTINGS, tmp_path,
        mode="collect",
    )  # fmt: skip

    assert result.waiting == 1 and result.errors[0].startswith(f"batch {BATCH_ID}")


def test_cli_rejects_bad_arguments_and_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["--collect", "--limit", "2"], ["--submit", "--filing", SUPP], ["--limit", "0"]):
        with pytest.raises(SystemExit):
            main(["supplements", *argv])
    monkeypatch.delenv("OBR_DATABASE_URL", raising=False)
    assert main(["supplements"]) == 1
    assert "OBR_DATABASE_URL" in capsys.readouterr().err
