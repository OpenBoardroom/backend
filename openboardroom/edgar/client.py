"""HTTP client for SEC EDGAR, following SEC fair-access rules."""

import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, cast

import httpx

from openboardroom.config import Settings
from openboardroom.edgar.ratelimit import RateLimiter

SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TIMEOUT_SECONDS = 30.0
# Seconds to wait before each retry of a 429, 5xx, or network error.
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class EdgarError(Exception):
    """Raised when EDGAR returns an error or cannot be reached.

    Attributes:
        status: The HTTP status EDGAR answered with, or None if no response
            arrived (a network error) or the response itself was unusable.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """Create the error.

        Args:
            message: What failed, naming the URL.
            status: The HTTP status, if the failure was one.
        """
        super().__init__(message)
        self.status = status


def document_url(cik: str, accession: str, primary_document: str) -> str:
    """Build the Archives URL of a filing's primary document.

    Args:
        cik: Filer CIK, with or without zero padding.
        accession: Accession number, e.g. ``0001308179-24-000010``.
        primary_document: File name of the primary document.

    Returns:
        The document's URL on www.sec.gov.
    """
    return f"{ARCHIVES_BASE}/{int(cik)}/{accession.replace('-', '')}/{primary_document}"


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class EdgarClient:
    """Rate-limited EDGAR client that sends the configured User-Agent."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        limiter: RateLimiter | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a client.

        Args:
            settings: Supplies the User-Agent and the request rate.
            transport: httpx transport; tests pass an ``httpx.MockTransport``.
            limiter: Rate limiter; defaults to one at ``settings.edgar_max_rps``.
            sleep: Function used for retry backoff.
        """
        self._limiter = limiter or RateLimiter(settings.edgar_max_rps)
        self._sleep = sleep
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": settings.edgar_user_agent},
            timeout=TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> Self:
        """Return the client for use in a ``with`` block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        self.close()

    def submissions(self, cik: str) -> dict[str, Any]:
        """Fetch a filer's submissions JSON.

        Args:
            cik: CIK zero-padded to 10 digits.

        Returns:
            The decoded JSON object.

        Raises:
            EdgarError: On a non-retryable error, once retries run out, or if the
                body isn't a JSON object.
        """
        return self._get_object(f"{SUBMISSIONS_BASE}/CIK{cik}.json")

    def submissions_page(self, name: str) -> dict[str, Any]:
        """Fetch an additional submissions page listed under ``filings.files``.

        Args:
            name: Page file name, e.g. ``CIK0000320193-submissions-001.json``.

        Returns:
            The decoded JSON object.

        Raises:
            EdgarError: On a non-retryable error, once retries run out, or if the
                body isn't a JSON object.
        """
        return self._get_object(f"{SUBMISSIONS_BASE}/{name}")

    def company_tickers(self) -> dict[str, Any]:
        """Fetch EDGAR's current ticker-to-CIK map.

        Returns:
            The decoded JSON object: ``{"0": {"cik_str": 320193, "ticker": "AAPL",
            "title": "Apple Inc."}, ...}``.

        Raises:
            EdgarError: On a non-retryable error, once retries run out, or if the
                body isn't a JSON object.
        """
        return self._get_object(COMPANY_TICKERS_URL)

    def document(self, cik: str, accession: str, primary_document: str) -> bytes:
        """Fetch a filing document's raw bytes, undecoded.

        Args:
            cik: Filer CIK.
            accession: Accession number with dashes.
            primary_document: File name of the document.

        Returns:
            The response body.

        Raises:
            EdgarError: On a non-retryable error or once retries run out.
        """
        return self._get(document_url(cik, accession, primary_document)).content

    def _get_object(self, url: str) -> dict[str, Any]:
        """GET `url` and decode its body, which must be a JSON object."""
        response = self._get(url)
        try:
            body = response.json()
        except ValueError:
            raise EdgarError(
                f"EDGAR returned invalid JSON (HTTP {response.status_code}) for {url}"
            ) from None
        if not isinstance(body, dict):
            raise EdgarError(
                f"EDGAR response is not a JSON object (HTTP {response.status_code}) for {url}"
            )
        return cast(dict[str, Any], body)

    def _get(self, url: str) -> httpx.Response:
        attempts = len(BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            if attempt:
                self._sleep(BACKOFF_SECONDS[attempt - 1])
            self._limiter.wait()
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    # The exception text carries no headers, so the User-Agent stays out.
                    raise EdgarError(
                        f"EDGAR request failed ({type(exc).__name__}) for {url}"
                    ) from exc
                continue
            if response.is_success:
                return response
            if not _retryable(response.status_code) or attempt == attempts - 1:
                raise EdgarError(
                    f"EDGAR returned HTTP {response.status_code} for {url}",
                    status=response.status_code,
                )
        raise AssertionError("unreachable")  # pragma: no cover
