"""Tests for the rate-limited EDGAR client (no real network: httpx.MockTransport only)."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from openboardroom.config import Settings
from openboardroom.edgar import EdgarClient, EdgarError, RateLimiter, document_url

USER_AGENT = "Test Org test@example.org"


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        """Start at time 0 with no recorded sleeps."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record the sleep and advance the clock by it."""
        self.sleeps.append(seconds)
        self.now += seconds


def make_settings(rps: float = 8.0) -> Settings:
    return Settings(
        database_url="postgresql://u:p@localhost/db",
        edgar_user_agent=USER_AGENT,
        edgar_max_rps=rps,
        cache_dir=Path("./cache"),
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock
) -> EdgarClient:
    settings = make_settings()
    limiter = RateLimiter(settings.edgar_max_rps, clock=clock, sleep=clock.sleep)
    return EdgarClient(
        settings, transport=httpx.MockTransport(handler), limiter=limiter, sleep=clock.sleep
    )


def test_sends_configured_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"cik": "320193"})

    with make_client(handler, FakeClock()) as client:
        body = client.submissions("0000320193")

    assert body == {"cik": "320193"}
    assert len(seen) == 1
    assert seen[0].headers["User-Agent"] == USER_AGENT
    assert str(seen[0].url) == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_submissions_page_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"filings": []})

    with make_client(handler, FakeClock()) as client:
        assert client.submissions_page("CIK0000320193-submissions-001.json") == {"filings": []}

    assert seen == ["https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"]


def test_company_tickers_url_and_body() -> None:
    seen: list[httpx.Request] = []
    body = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    with make_client(handler, FakeClock()) as client:
        assert client.company_tickers() == body

    assert [str(r.url) for r in seen] == ["https://www.sec.gov/files/company_tickers.json"]
    assert seen[0].headers["User-Agent"] == USER_AGENT


def test_company_tickers_non_dict_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with make_client(handler, FakeClock()) as client, pytest.raises(EdgarError, match="ticker"):
        client.company_tickers()


def test_document_url() -> None:
    assert (
        document_url("0000320193", "0001308179-24-000010", "laapl2024_def14a.htm")
        == "https://www.sec.gov/Archives/edgar/data/320193/000130817924000010/laapl2024_def14a.htm"
    )


def test_document_returns_raw_bytes() -> None:
    seen: list[str] = []
    raw = b"<html>\xe9 proxy</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=raw)

    with make_client(handler, FakeClock()) as client:
        body = client.document("0000320193", "0001308179-24-000010", "laapl2024_def14a.htm")

    assert body == raw
    assert seen == [document_url("0000320193", "0001308179-24-000010", "laapl2024_def14a.htm")]


def test_rate_limiter_spaces_requests() -> None:
    clock = FakeClock()
    times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        times.append(clock.now)
        return httpx.Response(200, json={})

    with make_client(handler, clock) as client:
        for _ in range(10):
            client.submissions("0000320193")

    assert len(times) == 10
    assert clock.now >= 1.125
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= 0.125 for gap in gaps)


def test_rate_limiter_does_not_wait_when_time_has_passed() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8, clock=clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 5
    limiter.wait()
    assert clock.sleeps == []


def test_rate_limiter_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_403_raises_without_retry() -> None:
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403)

    with make_client(handler, clock) as client, pytest.raises(EdgarError) as excinfo:
        client.submissions("0000320193")

    assert len(calls) == 1
    message = str(excinfo.value)
    assert "403" in message
    assert "https://data.sec.gov/submissions/CIK0000320193.json" in message
    assert USER_AGENT not in message


def test_429_retries_then_raises() -> None:
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    with make_client(handler, clock) as client, pytest.raises(EdgarError, match="429"):
        client.submissions("0000320193")

    assert len(calls) == 4
    backoffs = [s for s in clock.sleeps if s >= 1]
    assert backoffs == [1, 2, 4]


def test_5xx_then_success_returns_body() -> None:
    clock = FakeClock()
    statuses = iter([503, 500])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, 200)
        if status == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status)

    with make_client(handler, clock) as client:
        assert client.submissions("0000320193") == {"ok": True}

    assert [s for s in clock.sleeps if s >= 1] == [1, 2]


def test_transport_error_retries_then_raises() -> None:
    clock = FakeClock()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler, clock) as client, pytest.raises(EdgarError) as excinfo:
        client.submissions("0000320193")

    assert len(calls) == 4
    assert "https://data.sec.gov/submissions/CIK0000320193.json" in str(excinfo.value)
