"""Tests for regenerating rejected texts with a text-only call (spec §7.2)."""

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from llm_replay import Recorder, json_response, mock_client, stream_response
from review_seed import SETTINGS, seed_filing

from openboardroom.cli import main
from openboardroom.pipeline import regenerate as regenerate_stage
from openboardroom.pipeline.regenerate import RegenerateResult, regenerate
from openboardroom.review import store

ACCESSION = "0000320193-26-000001"


def _message(output: object) -> dict[str, Any]:
    """A synthetic Messages API response carrying ``output`` as its JSON text."""
    return {
        "id": "msg_fixture_texts", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(output)}],
        "stop_reason": "end_turn", "stop_sequence": None, "stop_details": None,
        "usage": {"input_tokens": 5000, "output_tokens": 300,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }  # fmt: skip


GOOD = {
    "items": [
        {
            "number": 2,
            "part": "",
            "summary": "Unchanged summary.",
            "discussion": {
                "proponent": "The proponent argues that water risk rose 12% since 2020.",
                "board": "The board argues that current reporting covers it.",
            },
        }
    ]
}


def _text_dir(tmp_path: Path, accessions: list[str] = [ACCESSION]) -> Path:  # noqa: B006
    for accession in accessions:
        (tmp_path / f"{accession}.txt").write_text("Proposal 2 water text", encoding="utf-8")
    return tmp_path


def _reject_proponent(conn: psycopg.Connection, item_id: int) -> int:
    row = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'proponent_argument'",
        (item_id,),
    ).fetchone()
    assert row is not None
    store.reject_text(conn, row[0], "quinn", "leaves out the 12% figure")
    return row[0]


def _latest(conn: psycopg.Connection, item_id: int) -> list[tuple[str, int, str | None, str]]:
    return conn.execute(
        "select distinct on (kind) kind, version, body, review_status from item_texts "
        "where ballot_item_id = %s order by kind, version desc",
        (item_id,),
    ).fetchall()


def test_regenerate_writes_new_pending_versions(conn: psycopg.Connection, tmp_path: Path) -> None:
    seeded = seed_filing(conn, stage="published")
    _reject_proponent(conn, seeded.item_ids[1])
    recorder = Recorder(stream_response(_message(GOOD)))

    result = regenerate(conn, mock_client(recorder), SETTINGS, _text_dir(tmp_path))

    assert result == RegenerateResult(filings=1, texts=1, failed=[])
    assert _latest(conn, seeded.item_ids[1]) == [
        ("board_argument", 1, "The board argues that current reporting covers it.", "pending"),
        ("proponent_argument", 2,
         "The proponent argues that water risk rose 12% since 2020.", "pending"),
        ("summary", 1, "A request for a report on water use.", "pending"),
    ]  # fmt: skip
    run = conn.execute(
        "select kind, status, prompt_version, input_tokens from extraction_runs "
        "where kind = 'texts'"
    ).fetchone()
    assert run == ("texts", "succeeded", "1", 5000)
    body = recorder.body()
    assert "leaves out the 12% figure" in body["messages"][0]["content"]
    assert body["output_config"]["format"]["schema"]["required"] == ["items"]


def test_regenerate_skips_needs_manual_and_unrejected(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    seeded = seed_filing(conn)
    text_id = _reject_proponent(conn, seeded.item_ids[1])
    conn.execute("update item_texts set review_status = 'needs_manual' where id = %s", (text_id,))
    recorder = Recorder()

    result = regenerate(conn, mock_client(recorder), SETTINGS, _text_dir(tmp_path))

    assert result == RegenerateResult(filings=0, texts=0, failed=[])
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("output", "detail"),
    [
        ({"items": [{"number": 2}]}, "items/0"),
        ({"items": [{**GOOD["items"][0], "number": 3}]}, "item 2 missing"),
    ],
)
def test_regenerate_bad_output_writes_nothing(
    conn: psycopg.Connection, tmp_path: Path, output: object, detail: str
) -> None:
    seeded = seed_filing(conn)
    _reject_proponent(conn, seeded.item_ids[1])
    recorder = Recorder(stream_response(_message(output)))

    result = regenerate(conn, mock_client(recorder), SETTINGS, _text_dir(tmp_path))

    assert result.texts == 0 and len(result.failed) == 1 and detail in result.failed[0]
    assert ("proponent_argument", 1, "The proponent argues that water risk is rising.",
            "rejected") in _latest(conn, seeded.item_ids[1])  # fmt: skip
    assert conn.execute("select status from extraction_runs where kind = 'texts'").fetchone() == (
        "failed",
    )


def test_regenerate_missing_text_file(conn: psycopg.Connection, tmp_path: Path) -> None:
    seeded = seed_filing(conn)
    _reject_proponent(conn, seeded.item_ids[1])
    recorder = Recorder()

    result = regenerate(conn, mock_client(recorder), SETTINGS, tmp_path)

    assert result.failed == [f"{ACCESSION}: text file missing"]
    assert recorder.requests == []


def test_regenerate_limit_takes_oldest(conn: psycopg.Connection, tmp_path: Path) -> None:
    newer = seed_filing(conn, filed_at="2026-03-01")
    older = seed_filing(conn, "0000000002-26-000001", cik="0000000002", filed_at="2026-01-01")
    _reject_proponent(conn, newer.item_ids[1])
    _reject_proponent(conn, older.item_ids[1])
    recorder = Recorder(stream_response(_message(GOOD)))
    text_dir = _text_dir(tmp_path, [ACCESSION, "0000000002-26-000001"])

    result = regenerate(conn, mock_client(recorder), SETTINGS, text_dir, limit=1)

    assert result.filings == 1
    assert ("proponent_argument", 2) in [k[:2] for k in _latest(conn, older.item_ids[1])]
    assert ("proponent_argument", 1) in [k[:2] for k in _latest(conn, newer.item_ids[1])]


def test_cli_regenerate_prints_counts(
    monkeypatch: pytest.MonkeyPatch,
    role_urls: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[Path, int | None]] = []

    def fake(
        conn: psycopg.Connection,
        client: object,
        settings: object,
        text_dir: Path,
        limit: int | None = None,
    ) -> RegenerateResult:
        seen.append((text_dir, limit))
        return RegenerateResult(filings=2, texts=3, failed=["0000000002-26-000001: bad"])

    monkeypatch.setenv("OBR_DATABASE_URL", role_urls["pipeline_rw"])
    monkeypatch.setenv("OBR_REVIEWER", "quinn")
    monkeypatch.setenv("OBR_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setattr(regenerate_stage, "regenerate", fake)

    assert main(["review", "regenerate", "--limit", "5"]) == 1

    captured = capsys.readouterr()
    assert seen == [(tmp_path / "text", 5)]
    assert "regenerate: 2 filings, 3 texts, 1 failed" in captured.out
    assert "0000000002-26-000001: bad" in captured.err


def test_regenerate_rewrites_a_summary(conn: psycopg.Connection, tmp_path: Path) -> None:
    seeded = seed_filing(conn)
    row = conn.execute(
        "select id from item_texts where ballot_item_id = %s and kind = 'summary'",
        (seeded.item_ids[1],),
    ).fetchone()
    assert row is not None
    store.reject_text(conn, row[0], "quinn", "say who asks")
    output = {"items": [{**GOOD["items"][0], "summary": "A Fund asks for a water report."}]}

    result = regenerate(
        conn, mock_client(Recorder(stream_response(_message(output)))), SETTINGS,
        _text_dir(tmp_path),
    )  # fmt: skip

    assert result.texts == 1
    assert ("summary", 2, "A Fund asks for a water report.", "pending") in _latest(
        conn, seeded.item_ids[1]
    )


def test_regenerate_api_error_is_recorded(conn: psycopg.Connection, tmp_path: Path) -> None:
    seeded = seed_filing(conn)
    _reject_proponent(conn, seeded.item_ids[1])
    error = {"type": "error", "error": {"type": "api_error", "message": "Fixture error."}}

    result = regenerate(
        conn, mock_client(Recorder(json_response(error, status=500))), SETTINGS,
        _text_dir(tmp_path),
    )  # fmt: skip

    assert result.texts == 0 and "InternalServerError" in result.failed[0]
    assert conn.execute("select status from extraction_runs where kind = 'texts'").fetchone() == (
        "failed",
    )
