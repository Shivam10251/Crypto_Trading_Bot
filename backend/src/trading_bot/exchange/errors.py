"""Exchange error taxonomy.

Callers need to distinguish "retry this", "slow down", "this venue cannot do
that" and "the data was malformed" - each demands a different response, and a
single generic exception would force string matching on messages.
"""

from __future__ import annotations


class ExchangeError(Exception):
    """Base class for every exchange-layer failure."""


class ExchangeConnectionError(ExchangeError):
    """Network failure or timeout. Retryable."""


class ExchangeRateLimitError(ExchangeError):
    """Venue rate limit hit (HTTP 429/418).

    ``retry_after_seconds`` carries the venue's own guidance when it sends any;
    ignoring it risks an IP ban.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ExchangeResponseError(ExchangeError):
    """Venue returned an error payload or an unexpected status code."""

    def __init__(self, message: str, status_code: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        # Binance's own numeric error code, e.g. -1121 "Invalid symbol".
        self.code = code


class ExchangeDataError(ExchangeError):
    """A payload was missing fields, unparseable, or economically impossible.

    Raised by the normalization layer. Data that fails here must never reach a
    strategy: acting on a crossed or zero-priced book is worse than not trading.
    """


class UnknownMarketError(ExchangeError):
    """The requested symbol is not listed on this venue."""


class ExecutionNotEnabledError(ExchangeError):
    """Order placement was attempted while execution is disabled.

    Phase 2 implements market data only. This is a deliberate guard, not a
    missing feature: the execution path is built in Phase 17 and stays off
    until explicitly armed.
    """


class NotSupportedError(ExchangeError):
    """This adapter does not implement the requested capability."""
