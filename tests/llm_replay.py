"""Replay recorded Claude API responses through the real ``anthropic`` SDK.

Tests never call the live API. ``mock_client`` builds a real client whose HTTP
layer is an ``httpx2.MockTransport`` (the SDK uses ``httpx2``, not ``httpx``), so
request building, SSE parsing, stream accumulation, and error mapping all run
the SDK's own code.
"""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2

# Fake credential: the transport never leaves the process.
FAKE_API_KEY = "sk-ant-test-not-a-real-key"
REQUEST_ID = "req_replay_0001"
# Small enough that the AAPL text arrives in many deltas, so accumulation is exercised.
_TEXT_CHUNK = 1000

Handler = Callable[[httpx2.Request], httpx2.Response]


def mock_client(handler: Handler) -> anthropic.Anthropic:
    """Return a real SDK client whose requests go to ``handler``.

    Args:
        handler: Called with each outgoing request; returns the response.

    Returns:
        A client with a fake API key and no retries, so failures surface at once.
    """
    return anthropic.Anthropic(
        api_key=FAKE_API_KEY,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


class Recorder:
    """A MockTransport handler that records requests and replies from a queue.

    Attributes:
        requests: Every request received, in order.
    """

    def __init__(self, *responses: httpx2.Response) -> None:
        """Queue the responses to return, one per request.

        Args:
            *responses: Responses in the order requests should receive them.
        """
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record ``request`` and return the next queued response.

        Args:
            request: The outgoing request.

        Returns:
            The next queued response.
        """
        self.requests.append(request)
        return self._responses.pop(0)

    def body(self, index: int = 0) -> dict[str, Any]:
        """Return a recorded request's JSON body.

        Args:
            index: Which request, in order received.

        Returns:
            The parsed body.
        """
        return json.loads(self.requests[index].content)


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _chunks(text: str) -> list[str]:
    return [text[i : i + _TEXT_CHUNK] for i in range(0, len(text), _TEXT_CHUNK)]


def _block_events(index: int, block: dict[str, Any]) -> list[str]:
    kind = block["type"]
    if kind == "text":
        events = [_event("content_block_start", _start(index, {"type": "text", "text": ""}))]
        events += [_delta(index, {"type": "text_delta", "text": c}) for c in _chunks(block["text"])]
    elif kind == "thinking":
        shell = {"type": "thinking", "thinking": "", "signature": ""}
        events = [_event("content_block_start", _start(index, shell))]
        for chunk in _chunks(block["thinking"]):
            events.append(_delta(index, {"type": "thinking_delta", "thinking": chunk}))
        events.append(_delta(index, {"type": "signature_delta", "signature": block["signature"]}))
    else:
        # Fallback and other server-side blocks arrive whole, with no deltas.
        events = [_event("content_block_start", _start(index, block))]
    events.append(_event("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def _start(index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": block}


def _delta(index: int, delta: dict[str, Any]) -> str:
    return _event(
        "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
    )


def sse_body(message: dict[str, Any]) -> bytes:
    """Turn a recorded Message into a Messages streaming response body.

    The stream follows the API's event order: ``message_start`` with empty
    content, no stop reason, and the requested model (the first fallback
    block's ``from`` model when there is one), then start/delta/stop events per content block,
    then ``message_delta`` carrying the stop reason, stop details, and final
    usage, then ``message_stop``.

    Args:
        message: A Messages API response as recorded JSON.

    Returns:
        The ``text/event-stream`` body.
    """
    # A fallback-served stream starts on the requested model; the SDK relabels
    # the message when the fallback block arrives.
    fallbacks = [block for block in message["content"] if block["type"] == "fallback"]
    model = fallbacks[0]["from"]["model"] if fallbacks else message["model"]
    start: dict[str, Any] = {
        **message,
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "stop_details": None,
        "usage": {**message["usage"], "output_tokens": 1},
    }
    events = [_event("message_start", {"type": "message_start", "message": start})]
    for index, block in enumerate(message["content"]):
        events += _block_events(index, block)
    delta = {
        "type": "message_delta",
        "delta": {
            "stop_reason": message["stop_reason"],
            "stop_sequence": message.get("stop_sequence"),
            "stop_details": message.get("stop_details"),
        },
        "usage": message["usage"],
    }
    events.append(_event("message_delta", delta))
    events.append(_event("message_stop", {"type": "message_stop"}))
    return "".join(events).encode()


def stream_response(message: dict[str, Any]) -> httpx2.Response:
    """Return a streaming HTTP response that replays ``message``.

    Args:
        message: A Messages API response as recorded JSON.

    Returns:
        A 200 ``text/event-stream`` response with a ``request-id`` header.
    """
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": REQUEST_ID},
        content=sse_body(message),
    )


def json_response(body: object, status: int = 200) -> httpx2.Response:
    """Return a JSON HTTP response with a ``request-id`` header.

    Args:
        body: The JSON body.
        status: HTTP status code.

    Returns:
        The response.
    """
    return httpx2.Response(
        status,
        headers={"content-type": "application/json", "request-id": REQUEST_ID},
        content=json.dumps(body).encode(),
    )


def jsonl_response(lines: list[dict[str, Any]]) -> httpx2.Response:
    """Return a batch results response: one JSON object per line.

    Args:
        lines: The result entries.

    Returns:
        A 200 ``application/binary`` response, as the API serves results files.
    """
    body = "".join(json.dumps(line) + "\n" for line in lines)
    return httpx2.Response(
        200,
        headers={"content-type": "application/binary", "request-id": REQUEST_ID},
        content=body.encode(),
    )


def api_error(status: int, error_type: str, message: str) -> httpx2.Response:
    """Return an API error response in the API's error shape.

    Args:
        status: HTTP status code.
        error_type: The API's error type, such as ``api_error``.
        message: The error message.

    Returns:
        The response.
    """
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return json_response(body, status)
