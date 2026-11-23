"""A thin client for the one Claude call per filing that extracts ballot items.

Two modes share one request shape. Direct mode (development, the eval set, and
retries) streams the request with server-side refusal fallback. Batch mode
(scheduled runs) sends many filings through the Message Batches API at half
price; anything a batch can't finish is retried directly.

Nothing here raises for a problem with one filing: API errors, refusals,
truncation, and unparseable output all become an ``Outcome``. Whether the
output matches the schema is the extraction checks' job, not this module's.
"""

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

import anthropic
from anthropic.types import Message
from anthropic.types.beta import BetaMessage
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from openboardroom.extract.regex import Candidate
from openboardroom.extract.spec import output_schema, system_prompt, user_message

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The Message Batches API's pattern for custom_id; a filing accession fits it.
_CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class LlmSettings:
    """Model settings for extraction.

    Attributes:
        model: Model ID, such as ``claude-opus-5-5``.
        effort: Output effort level (``low`` through ``max``).
        max_tokens: Output token ceiling, thinking included.
    """

    model: str
    effort: str
    max_tokens: int


@dataclass(frozen=True)
class Outcome:
    """The result of one extraction call.

    Attributes:
        status: ``succeeded`` when the output is a JSON object, ``refused`` when
            every model in the fallback chain declined, ``failed`` otherwise.
        output: The parsed JSON object when succeeded.
        model: The model that produced the response; a fallback may differ from
            the requested model.
        input_tokens: Input tokens from the response's usage.
        output_tokens: Output tokens from the response's usage.
        detail: Why it was refused or failed, with the request id when known.
            Never contains the API key or the request body.
    """

    status: Literal["succeeded", "refused", "failed"]
    output: dict[str, object] | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    detail: str | None


def request_params(
    settings: LlmSettings, text: str, candidates: list[Candidate]
) -> dict[str, object]:
    """Build the Messages API parameters for one filing.

    Thinking is always on for current models, so ``thinking`` is omitted, and
    they reject sampling parameters, so none are sent.

    Args:
        settings: Model settings.
        text: Plain text of the filing.
        candidates: Proposal candidates from the regex pass.

    Returns:
        Parameters shared by direct and batch calls.
    """
    return {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "system": system_prompt(),
        "messages": [{"role": "user", "content": user_message(text, candidates)}],
        "output_config": {
            "effort": settings.effort,
            "format": {"type": "json_schema", "schema": output_schema()},
        },
    }


def _outcome(
    message: Message | BetaMessage,
    status: Literal["succeeded", "refused", "failed"],
    detail: str | None,
    output: dict[str, object] | None = None,
) -> Outcome:
    return Outcome(
        status=status,
        output=output,
        model=message.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        detail=detail,
    )


def _final_text(message: Message | BetaMessage) -> str | None:
    # Content before the last fallback block came from a model that declined.
    start = 0
    for index, block in enumerate(message.content):
        if block.type == "fallback":
            start = index + 1
    texts = [block.text for block in message.content[start:] if block.type == "text"]
    return "".join(texts) if texts else None


def outcome_from_message(message: Message | BetaMessage) -> Outcome:
    """Classify a complete response.

    Args:
        message: A final message from a direct call or a batch result.

    Returns:
        ``refused`` on a refusal (category and explanation in ``detail``);
        ``succeeded`` when it ended normally and its text is a JSON object;
        ``failed`` otherwise.
    """
    if message.stop_reason == "refusal":
        details = message.stop_details
        category = details.category if details else None
        explanation = details.explanation if details else None
        return _outcome(message, "refused", f"refusal: category {category}; {explanation}")
    if message.stop_reason != "end_turn":
        return _outcome(message, "failed", f"stop_reason {message.stop_reason}")
    text = _final_text(message)
    if text is None:
        return _outcome(message, "failed", "no text block in the response")
    try:
        output = json.loads(text)
    except json.JSONDecodeError as error:
        # The decoder's message gives a position only, not the text.
        return _outcome(message, "failed", f"output is not JSON: {error.msg} at {error.pos}")
    if not isinstance(output, dict):
        return _outcome(
            message, "failed", f"output is JSON but not an object: {type(output).__name__}"
        )
    return _outcome(message, "succeeded", None, cast(dict[str, object], output))


def _error_outcome(error: anthropic.APIError) -> Outcome:
    detail = type(error).__name__
    if isinstance(error, anthropic.APIStatusError):
        detail += f": status {error.status_code}, request {error.request_id}"
    return Outcome("failed", None, None, None, None, detail)


def run_direct(
    client: anthropic.Anthropic, settings: LlmSettings, text: str, candidates: list[Candidate]
) -> Outcome:
    """Extract one filing with a streamed call and server-side refusal fallback.

    Args:
        client: An Anthropic client.
        settings: Model settings.
        text: Plain text of the filing.
        candidates: Proposal candidates from the regex pass.

    Returns:
        The outcome. API, connection, and timeout errors become ``failed``.
    """
    params = cast(Any, request_params(settings, text, candidates))
    try:
        with client.beta.messages.stream(
            **params, betas=[FALLBACK_BETA], fallbacks="default"
        ) as stream:
            message = stream.get_final_message()
            request_id = stream.request_id
    except anthropic.APIError as error:
        return _error_outcome(error)
    outcome = outcome_from_message(message)
    if outcome.detail is None:
        return outcome
    return replace(outcome, detail=f"{outcome.detail} (request {request_id})")


def submit_batch(
    client: anthropic.Anthropic,
    settings: LlmSettings,
    requests: list[tuple[str, str, list[Candidate]]],
) -> str:
    """Submit filings as one message batch.

    Batches don't take ``fallbacks``; refusals come back as results and are
    retried directly.

    Args:
        client: An Anthropic client.
        settings: Model settings.
        requests: ``(custom_id, text, candidates)`` per filing, where
            ``custom_id`` is the filing accession.

    Returns:
        The batch id.

    Raises:
        ValueError: A ``custom_id`` doesn't fit the batch API's pattern.
    """
    batch: list[Request] = []
    for custom_id, text, candidates in requests:
        if not _CUSTOM_ID.match(custom_id):
            raise ValueError(f"invalid batch custom_id: {custom_id!r}")
        params = cast(MessageCreateParamsNonStreaming, request_params(settings, text, candidates))
        batch.append(Request(custom_id=custom_id, params=params))
    return client.messages.batches.create(requests=batch).id


def batch_status(client: anthropic.Anthropic, batch_id: str) -> Literal["in_progress", "ended"]:
    """Return whether a batch has ended.

    Args:
        client: An Anthropic client.
        batch_id: The batch id.

    Returns:
        ``ended`` once results are ready; ``in_progress`` otherwise, including
        while a cancel is in progress.
    """
    status = client.messages.batches.retrieve(batch_id).processing_status
    return "ended" if status == "ended" else "in_progress"


def batch_outcomes(client: anthropic.Anthropic, batch_id: str) -> dict[str, Outcome | None]:
    """Read an ended batch's results.

    Args:
        client: An Anthropic client.
        batch_id: The batch id.

    Returns:
        Outcomes keyed by ``custom_id`` (results arrive in any order). ``None``
        means the request errored, expired, or was canceled: retry it directly.
    """
    outcomes: dict[str, Outcome | None] = {}
    for entry in client.messages.batches.results(batch_id):
        result = entry.result
        outcomes[entry.custom_id] = (
            outcome_from_message(result.message) if result.type == "succeeded" else None
        )
    return outcomes
