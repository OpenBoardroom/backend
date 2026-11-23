"""Tests for the Claude extraction client, replayed through the real SDK."""

import copy
import json
import re
from collections.abc import Iterator
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
    BatchResult,
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
# The custom_ids submitted in the batch-results fixture.
IDS = [f"0000000000-24-00000{n}" for n in range(1, 6)]


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
        # Explicit, so a non-default OBR_EXTRACT_MODEL still thinks.
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": output_schema()},
        },
    }
    for banned in ("temperature", "top_p", "top_k", "fallbacks", "stream"):
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


def test_sse_body_starts_a_fallback_stream_on_the_requested_model() -> None:
    # The API names the requested model in message_start; the SDK relabels the
    # message from the fallback block, which is what the refusal round trip proves.
    body = sse_body(_load("refusal.json")).decode()
    start = json.loads(body.split("data: ", 1)[1].split("\n", 1)[0])

    assert start["message"]["model"] == "claude-opus-5-5"


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


class _BrokenStream(httpx2.SyncByteStream):
    """A response body that sends some bytes and then loses the connection."""

    def __init__(self, head: bytes) -> None:
        self._head = head

    def __iter__(self) -> Iterator[bytes]:
        yield self._head
        raise httpx2.ReadError("connection reset")


def test_run_direct_turns_a_dropped_stream_into_failed() -> None:
    body = sse_body(_aapl())
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": REQUEST_ID},
        stream=_BrokenStream(body[: len(body) // 2]),
    )

    outcome, _ = _direct(response)

    assert outcome.status == "failed"
    assert outcome.output is None
    assert outcome.detail is not None
    assert "ReadError" in outcome.detail
    assert REQUEST_ID in outcome.detail


def test_run_direct_turns_a_malformed_event_into_failed() -> None:
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": REQUEST_ID},
        content=b"event: message_start\ndata: {not json\n\n",
    )

    outcome, _ = _direct(response)

    assert outcome.status == "failed"
    assert outcome.detail is not None
    assert "JSONDecodeError" in outcome.detail
    assert REQUEST_ID in outcome.detail


def test_run_direct_fails_on_a_stream_cut_before_its_end() -> None:
    body = sse_body(_aapl())
    cut = body[: body.index(b"event: message_delta")]
    response = httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": REQUEST_ID},
        content=cut,
    )

    outcome, _ = _direct(response)

    assert outcome.status == "failed"
    assert outcome.output is None


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


def _ended(lines: list[dict[str, Any]]) -> Recorder:
    return Recorder(json_response(_batch("ended", f"{BATCH_URL}/results")), jsonl_response(lines))


def _lines() -> list[dict[str, Any]]:
    return json.loads((LLM / "batch-results.json").read_text(encoding="utf-8"))


def _errored(custom_id: str, kind: str, message: str) -> dict[str, Any]:
    error = {"type": "error", "error": {"type": kind, "message": message}, "request_id": "req_x"}
    return {"custom_id": custom_id, "result": {"type": "errored", "error": error}}


def test_batch_outcomes_maps_results_by_custom_id() -> None:
    recorder = _ended(_lines())

    outcomes = batch_outcomes(mock_client(recorder), BATCH_ID, IDS)

    assert [r.url.path for r in recorder.requests] == [
        f"/v1/messages/batches/{BATCH_ID}",
        f"/v1/messages/batches/{BATCH_ID}/results",
    ]
    assert outcomes["0000000000-24-000001"] == BatchResult(
        Outcome(
            status="succeeded",
            output={"meeting": None, "items": []},
            model="claude-opus-5-5",
            input_tokens=1000,
            output_tokens=50,
            detail=None,
        ),
        retry=False,
    )
    refused = outcomes["0000000000-24-000002"]
    assert (refused.outcome.status, refused.retry) == ("refused", True)
    assert outcomes["0000000000-24-000003"] == BatchResult(
        Outcome(
            "failed", None, None, None, None,
            "batch errored: api_error: Fixture internal error. (request req_fixture_errored)",
        ),
        retry=True,
    )  # fmt: skip
    assert outcomes["0000000000-24-000004"] == BatchResult(
        Outcome("failed", None, None, None, None, "batch result expired"), retry=True
    )
    assert outcomes["0000000000-24-000005"] == BatchResult(
        Outcome("failed", None, None, None, None, "batch result canceled"), retry=True
    )
    assert len(outcomes) == 5


def test_batch_outcomes_does_not_retry_an_invalid_request() -> None:
    """A malformed request fails the same way every time, so it isn't billed again."""
    line = _errored(IDS[0], "invalid_request_error", "output_config.format: too many unions")

    outcomes = batch_outcomes(mock_client(_ended([line])), BATCH_ID, IDS[:1])

    assert outcomes[IDS[0]] == BatchResult(
        Outcome(
            "failed", None, None, None, None,
            "batch errored: invalid_request_error: output_config.format: too many unions "
            "(request req_x)",
        ),
        retry=False,
    )  # fmt: skip


def test_batch_outcomes_caps_a_long_error_message() -> None:
    line = _errored(IDS[0], "api_error", "x" * 5000)

    detail = batch_outcomes(mock_client(_ended([line])), BATCH_ID, IDS[:1])[IDS[0]].outcome.detail

    assert detail is not None and len(detail) < 400


def test_batch_outcomes_is_independent_of_result_order() -> None:
    lines = _lines()

    def outcomes_for(order: list[dict[str, Any]]) -> dict[str, BatchResult]:
        return batch_outcomes(mock_client(_ended(order)), BATCH_ID, IDS)

    assert outcomes_for(lines) == outcomes_for(list(reversed(copy.deepcopy(lines))))


_NO_RESULT = BatchResult(Outcome("failed", None, None, None, None, "no batch result"), retry=True)


def test_batch_outcomes_includes_every_submitted_id() -> None:
    ok = next(line for line in _lines() if line["custom_id"] == IDS[0])

    outcomes = batch_outcomes(mock_client(_ended([ok])), BATCH_ID, IDS)

    assert set(outcomes) == set(IDS)
    assert outcomes[IDS[0]].outcome.status == "succeeded"
    assert [outcomes[i] for i in IDS[1:]] == [_NO_RESULT] * 4


def test_batch_outcomes_survives_a_malformed_results_line() -> None:
    lines = _lines()
    ok = next(line for line in lines if line["custom_id"] == IDS[0])
    body = (json.dumps(ok) + "\n{not json\n" + json.dumps(lines[0]) + "\n").encode()
    results = httpx2.Response(
        200,
        headers={"content-type": "application/binary", "request-id": REQUEST_ID},
        content=body,
    )
    recorder = Recorder(json_response(_batch("ended", f"{BATCH_URL}/results")), results)

    outcomes = batch_outcomes(mock_client(recorder), BATCH_ID, IDS)

    first = outcomes[IDS[0]]
    assert (first.outcome.status, first.retry) == ("succeeded", False)
    # Everything not yet collected is retried directly.
    assert [outcomes[i] for i in IDS[1:]] == [_NO_RESULT] * 4
