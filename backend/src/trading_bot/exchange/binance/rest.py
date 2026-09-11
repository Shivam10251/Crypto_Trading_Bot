"""HTTP client for Binance public endpoints.

Responsibilities kept here so the adapter stays about market data:
retries with backoff, rate-limit handling, and translating transport failures
into the exchange error taxonomy.

Rate limits matter more than usual: Binance bans IPs that ignore 429s, and a
ban during a trading session is an outage. The client honours ``Retry-After``
and surfaces the weight header so usage can be monitored.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from trading_bot.core.logging import get_logger
from trading_bot.exchange.errors import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    ExchangeResponseError,
)

logger = get_logger(__name__)

# 418 means "you were already warned and kept going" - a temporary ban.
_RATE_LIMIT_STATUSES = frozenset({429, 418})
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 3


class BinanceRestClient:
    """Thin async HTTP client. One instance is shared per adapter."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 10,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_backoff_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff_seconds
        # Injectable so tests can supply a MockTransport instead of hitting the
        # network.
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._used_weight: int | None = None

    @property
    def used_weight(self) -> int | None:
        """Request weight consumed in the current minute, per the last response."""
        return self._used_weight

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET and decode JSON, retrying transient failures."""
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = ExchangeConnectionError(f"timeout calling {url}: {exc}")
            except httpx.HTTPError as exc:
                last_error = ExchangeConnectionError(f"transport error calling {url}: {exc}")
            else:
                self._record_weight(response)
                outcome = self._evaluate(response, url)
                if not isinstance(outcome, Exception):
                    return outcome
                last_error = outcome
                # A rate limit tells us how long to wait; obey it rather than
                # using our own backoff curve.
                if isinstance(outcome, ExchangeRateLimitError):
                    if attempt == self._max_attempts:
                        raise outcome
                    await asyncio.sleep(outcome.retry_after_seconds or self._backoff(attempt))
                    continue
                # 4xx and non-transient 5xx will not succeed on a retry.
                if (
                    isinstance(outcome, ExchangeResponseError)
                    and outcome.status_code not in _RETRYABLE_STATUSES
                ):
                    raise outcome

            if attempt < self._max_attempts:
                delay = self._backoff(attempt)
                logger.warning(
                    "binance.request_retry",
                    url=url,
                    attempt=attempt,
                    delay_seconds=delay,
                    error=str(last_error),
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff, capped."""
        return min(self._max_backoff, 0.5 * float(2 ** (attempt - 1)))

    def _record_weight(self, response: httpx.Response) -> None:
        raw = response.headers.get("x-mbx-used-weight-1m") or response.headers.get(
            "x-mbx-used-weight"
        )
        if raw and raw.isdigit():
            self._used_weight = int(raw)

    def _evaluate(self, response: httpx.Response, url: str) -> Any:
        """Return the decoded payload, or the error to raise/retry."""
        if response.status_code in _RATE_LIMIT_STATUSES:
            retry_after = response.headers.get("retry-after")
            return ExchangeRateLimitError(
                f"rate limited by binance ({response.status_code}) on {url}",
                retry_after_seconds=float(retry_after) if retry_after else None,
            )

        if response.status_code >= 400:
            code: int | None = None
            message = response.text[:200]
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code")
                message = str(payload.get("msg", message))
            return ExchangeResponseError(
                f"binance returned {response.status_code} for {url}: {message}",
                status_code=response.status_code,
                code=code,
            )

        try:
            return response.json()
        except ValueError as exc:
            return ExchangeResponseError(f"malformed JSON from {url}: {exc}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
