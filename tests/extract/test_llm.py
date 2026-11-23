"""Tests for the Claude extraction client, replayed through the real SDK."""

import copy
import json
import re
from pathlib import Path
from typing import Any, cast

import anthropic
import httpx2
import pytest
from llm_replay import (
    FAKE_API_KEY,
    REQUEST_ID,
    Recorder,
    api_error,
    json_response,
    jsonl_response,
    mock_client,
    sse_body,
    stream_response,
)

from openboardroom.extract.llm import (
    FALLBACK_BETA,
    LlmSettings,
    Outcome,
    batch_outcomes,
    batch_status,
    outcome_from_message,
    request_params,
    run_direct,
    submit_batch,
)
from openboardroom.extract.regex import Candidate
from openboardroom.extract.spec import output_schema, system_prompt, user_message

LLM = Path(__file__).parent.parent / "fixtures" / "llm"
SETTINGS = LlmSettings(model="claude-opus-5-5", effort="high", max_tokens=64000)
TEXT = "PROXY STATEMENT\nProposal No. 1 Election of Directors\n"
CANDIDATES = [Candidate(number=1, heading="Proposal No. 1 Election of Directors", lines=(1,))]
# The batch API's documented custom_id pattern.
CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
BATCH_ID = "msgbatch_fixture_01"
BATCH_URL = f"https://api.anthropic.com/v1/messages/batches/{BATCH_ID}"


def _load(name: str) -> dict[str, Any]:
    return json.loads((LLM / name).read_text(encoding="utf-8"))


def _aapl() -> dict[str, Any]:
    return _load("aapl-2024.message.json")


def _with_text(text: str) -> dict[str, Any]:
    message = _aapl()
    message["content"] = [{"type": "text", "text": text}]
    return message


def _final_message(message: dict[str, Any]) -> dict[str, Any]:
    client = mock_client(Recorder(stream_response(message)))
    params = cast(Any, request_params(SETTINGS, TEXT, CANDIDATES))
    with client.beta.messages.stream(**params) as stream:
        return stream.get_final_message().to_dict()


def _direct(*responses: httpx2.Response) -> tuple[Outcome, Recorder]:
    recorder = Recorder(*responses)
    return run_direct(mock_client(recorder), SETTINGS, TEXT, CANDIDATES), recorder


# --- request_params ---------------------------------------------------------


def test_request_params_shape() -> None:
    params = request_params(SETTINGS, TEXT, CANDIDATES)

    assert params == {
        "model": "claude-opus-5-5",
        "max_tokens": 64000,
        "system": system_prompt(),
        "messages": [{"role": "user", "content": user_message(TEXT, CANDIDATES)}],
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": output_schema()},
        },
    }
    for banned in ("thinking", "temperature", "top_p", "top_k", "fallbacks", "stream"):
        assert banned not in params


# --- sse_body round trip ----------------------------------------------------


@pytest.mark.parametrize("name", ["aapl-2024.message.json", "refusal.json", "max-tokens.json"])
def test_sse_body_round_trips_through_the_sdk(name: str) -> None:
    recorded = _load(name)

    final = _final_message(recorded)

    assert final["content"] == recorded["content"]
    assert final["model"] == recorded["model"]
    assert final["stop_reason"] == recorded["stop_reason"]
    assert final.get("stop_details") == recorded["stop_details"]
    for key, value in recorded["usage"].items():
        assert final["usage"][key] == value


def test_sse_body_is_an_event_stream() -> None:
    body = sse_body(_aapl()).decode()

    names = re.findall(r"^event: (\w+)$", body, re.MULTILINE)
    assert names[0] == "message_start"
    assert names[-2:] == ["message_delta", "message_stop"]
    # The 15k-character text arrives in many deltas, not one.
    assert names.count("content_block_delta") > 10


# --- run_direct -------------------------------------------------------------


def test_run_direct_succeeds_on_the_aapl_fixture() -> None:
    recorded = _aapl()

    outcome, recorder = _direct(stream_response(recorded))

    assert outcome == Outcome(
        status="succeeded",
        output=json.loads(recorded["content"][1]["text"]),
        model="claude-opus-5-5",
        input_tokens=107179,
        output_tokens=5931,
        detail=None,
    )
    request = recorder.requests[0]
    assert request.url.path == "/v1/messages"
    assert FALLBACK_BETA in request.headers["anthropic-beta"].split(",")
    assert FALLBACK_BETA == "server-side-fallback-2026-07-01"
    body = recorder.body()
    assert body["fallbacks"] == "default"
    assert body["stream"] is True
    assert {k: v for k, v in body.items() if k not in ("fallbacks", "stream")} == request_params(
        SETTINGS, TEXT, CANDIDATES
    )


def test_run_direct_reports_a_refusal() -> None:
    outcome, _ = _direct(stream_response(_load("refusal.json")))

    assert outcome.status == "refused"
    assert outcome.output is None
    # The fallback that also refused is the model of record.
    assert outcome.model == "claude-opus-5"
    assert (outcome.input_tokens, outcome.output_tokens) == (1200, 40)
    assert outcome.detail is not None
    assert "general_harms" in outcome.detail
    assert "Fixture refusal explanation." in outcome.detail
    assert REQUEST_ID in outcome.detail


def test_run_direct_fails_at_max_tokens() -> None:
    outcome, _ = _direct(stream_response(_load("max-tokens.json")))

    assert outcome.status == "failed"
    assert outcome.output is None
    assert (outcome.input_tokens, outcome.output_tokens) == (107179, 64000)
    assert outcome.detail is not None
    assert "max_tokens" in outcome.detail
    assert REQUEST_ID in outcome.detail


@pytest.mark.parametrize(
    ("status", "error_type", "exception"),
    [
        (500, "api_error", "InternalServerError"),
        (400, "invalid_request_error", "BadRequestError"),
        (429, "rate_limit_error", "RateLimitError"),
    ],
)
def test_run_direct_turns_http_errors_into_failed(
    status: int, error_type: str, exception: str
) -> None:
    outcome, recorder = _direct(api_error(status, error_type, "Fixture error message."))

    assert len(recorder.requests) == 1
    assert outcome.status == "failed"
    assert outcome.output is None
    assert outcome.input_tokens is None
    assert outcome.detail is not None
    assert exception in outcome.detail
    assert str(status) in outcome.detail
    assert REQUEST_ID in outcome.detail
    assert FAKE_API_KEY not in outcome.detail
    assert "PROXY STATEMENT" not in outcome.detail


@pytest.mark.parametrize(
    ("raised", "exception"),
    [
        (httpx2.ConnectError("connection refused"), "APIConnectionError"),
        (httpx2.ReadTimeout("timed out"), "APITimeoutError"),
    ],
)
def test_run_direct_turns_transport_errors_into_failed(raised: Exception, exception: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise raised

    outcome = run_direct(mock_client(handler), SETTINGS, TEXT, CANDIDATES)

    assert outcome.status == "failed"
    assert outcome.detail is not None
    assert exception in outcome.detail
    assert FAKE_API_KEY not in outcome.detail


def test_run_direct_turns_a_mid_stream_error_into_failed() -> None:
    recorded = _aapl()
    body = sse_body(recorded).decode().split("event: content_block_start")[0]
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    body += f"event: error\ndata: {json.dumps(error)}\n\n"
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": REQUEST_ID},
        content=body.encode(),
    )

    outcome, _ = _direct(response)

    assert outcome.status == "failed"
    assert outcome.detail is not None
    assert REQUEST_ID in outcome.detail


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Here are the ballot items: none.", "not JSON"),
        ("[1, 2, 3]", "not an object"),
        ('"just a string"', "not an object"),
    ],
)
def test_run_direct_fails_when_the_text_is_not_a_json_object(text: str, reason: str) -> None:
    outcome, _ = _direct(stream_response(_with_text(text)))

    assert outcome.status == "failed"
    assert outcome.output is None
    assert outcome.detail is not None
    assert reason in outcome.detail
    # The model's text is not echoed into the detail.
    assert text not in outcome.detail


def test_run_direct_succeeds_on_a_schema_invalid_object() -> None:
    # Schema checks belong to the extraction checks, not this module.
    outcome, _ = _direct(stream_response(_with_text('{"unexpected": true}')))

    assert outcome.status == "succeeded"
    assert outcome.output == {"unexpected": True}


def test_run_direct_reads_only_the_text_after_a_fallback() -> None:
    message = _aapl()
    output_text = message["content"][1]["text"]
    message["model"] = "claude-opus-5"
    message["content"] = [
        {"type": "thinking", "thinking": "", "signature": "fixture"},
        {"type": "text", "text": '{"meeting": {"kind": "ann'},
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-5"},
            "trigger": {"type": "refusal", "category": "general_harms"},
        },
        {"type": "thinking", "thinking": "", "signature": "fixture-2"},
        {"type": "text", "text": output_text},
    ]

    outcome, _ = _direct(stream_response(message))

    assert outcome.status == "succeeded"
    assert outcome.model == "claude-opus-5"
    assert outcome.output == json.loads(output_text)


# --- outcome_from_message ---------------------------------------------------


def _message(data: dict[str, Any]) -> anthropic.types.Message:
    return anthropic.types.Message.model_validate(data)


def test_outcome_from_message_fails_without_a_text_block() -> None:
    data = _aapl()
    data["content"] = [{"type": "thinking", "thinking": "", "signature": "fixture"}]

    outcome = outcome_from_message(_message(data))

    assert outcome.status == "failed"
    assert outcome.detail is not None
    assert "no text" in outcome.detail


def test_outcome_from_message_fails_on_an_unexpected_stop_reason() -> None:
    data = _aapl()
    data["stop_reason"] = "pause_turn"

    outcome = outcome_from_message(_message(data))

    assert outcome.status == "failed"
    assert outcome.detail is not None
    assert "pause_turn" in outcome.detail


def test_outcome_from_message_reports_a_refusal_without_details() -> None:
    data = _load("refusal.json")
    data["content"] = []
    data["stop_details"] = None

    outcome = outcome_from_message(_message(data))

    assert outcome.status == "refused"
    assert outcome.detail is not None
    assert "refusal" in outcome.detail


def test_outcome_from_message_accepts_a_plain_message() -> None:
    outcome = outcome_from_message(_message(_aapl()))

    assert outcome.status == "succeeded"
    assert outcome.model == "claude-opus-5-5"


# --- batches ----------------------------------------------------------------


def _batch(status: str, results_url: str | None = None) -> dict[str, Any]:
    return {
        "id": BATCH_ID,
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0,
            "succeeded": 2,
            "errored": 1,
            "canceled": 1,
            "expired": 1,
        },
        "created_at": "2026-11-22T00:00:00Z",
        "expires_at": "2026-11-23T00:00:00Z",
        "ended_at": None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": results_url,
    }


def test_submit_batch_posts_one_request_per_filing() -> None:
    accessions = ["0001308179-24-000010", "0000320193-24-000006"]
    recorder = Recorder(json_response(_batch("in_progress")))

    batch_id = submit_batch(
        mock_client(recorder),
        SETTINGS,
        [(accessions[0], TEXT, CANDIDATES), (accessions[1], "Other filing", [])],
    )

    assert batch_id == BATCH_ID
    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/messages/batches"
    requests = recorder.body()["requests"]
    assert [r["custom_id"] for r in requests] == accessions
    for accession in accessions:
        assert CUSTOM_ID.match(accession)
    assert requests[0]["params"] == request_params(SETTINGS, TEXT, CANDIDATES)
    assert requests[1]["params"] == request_params(SETTINGS, "Other filing", [])
    for r in requests:
        assert "fallbacks" not in r["params"]
        assert "stream" not in r["params"]
    assert "anthropic-beta" not in request.headers


def test_submit_batch_rejects_an_invalid_custom_id() -> None:
    recorder = Recorder()

    with pytest.raises(ValueError, match="custom_id"):
        submit_batch(mock_client(recorder), SETTINGS, [("bad id/with slash", TEXT, [])])
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("api_status", "status"),
    [("in_progress", "in_progress"), ("canceling", "in_progress"), ("ended", "ended")],
)
def test_batch_status(api_status: str, status: str) -> None:
    recorder = Recorder(json_response(_batch(api_status)))

    assert batch_status(mock_client(recorder), BATCH_ID) == status
    assert recorder.requests[0].method == "GET"
    assert recorder.requests[0].url.path == f"/v1/messages/batches/{BATCH_ID}"


def test_batch_outcomes_maps_results_by_custom_id() -> None:
    lines = json.loads((LLM / "batch-results.json").read_text(encoding="utf-8"))
    recorder = Recorder(
        json_response(_batch("ended", f"{BATCH_URL}/results")),
        jsonl_response(lines),
    )

    outcomes = batch_outcomes(mock_client(recorder), BATCH_ID)

    assert [r.url.path for r in recorder.requests] == [
        f"/v1/messages/batches/{BATCH_ID}",
        f"/v1/messages/batches/{BATCH_ID}/results",
    ]
    assert outcomes["0000000000-24-000001"] == Outcome(
        status="succeeded",
        output={"meeting": None, "items": []},
        model="claude-opus-5-5",
        input_tokens=1000,
        output_tokens=50,
        detail=None,
    )
    refused = outcomes["0000000000-24-000002"]
    assert refused is not None
    assert refused.status == "refused"
    assert outcomes["0000000000-24-000003"] is None
    assert outcomes["0000000000-24-000004"] is None
    assert outcomes["0000000000-24-000005"] is None
    assert len(outcomes) == 5


def test_batch_outcomes_is_independent_of_result_order() -> None:
    lines = json.loads((LLM / "batch-results.json").read_text(encoding="utf-8"))

    def outcomes_for(order: list[dict[str, Any]]) -> dict[str, Outcome | None]:
        recorder = Recorder(
            json_response(_batch("ended", f"{BATCH_URL}/results")),
            jsonl_response(order),
        )
        return batch_outcomes(mock_client(recorder), BATCH_ID)

    assert outcomes_for(lines) == outcomes_for(list(reversed(copy.deepcopy(lines))))
