"""REST client behaviour: retries, rate limits and error translation.

All traffic goes through httpx.MockTransport - these tests never touch the
network, so they are deterministic and fast.
"""

from __future__ import annotations

import httpx
import pytest

from trading_bot.exchange.binance.rest import BinanceRestClient
from trading_bot.exchange.errors import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    ExchangeResponseError,
)

URL = "https://api.binance.com/api/v3/ticker/bookTicker"


def client_with(handler: object, **kwargs: object) -> BinanceRestClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return BinanceRestClient(
        client=httpx.AsyncClient(transport=transport),
        max_backoff_seconds=0.0,  # keep tests fast
        **kwargs,  # type: ignore[arg-type]
    )


class TestSuccess:
    async def test_returns_decoded_json(self) -> None:
        rest = client_with(lambda request: httpx.Response(200, json={"symbol": "BTCUSDT"}))
        assert await rest.get(URL) == {"symbol": "BTCUSDT"}

    async def test_passes_query_parameters(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={})

        await client_with(handler).get(URL, params={"symbol": "BTCUSDT", "limit": 5})
        assert seen == {"symbol": "BTCUSDT", "limit": "5"}

    async def test_records_rate_limit_weight(self) -> None:
        """Weight is tracked so usage can be monitored before a ban happens."""
        rest = client_with(
            lambda request: httpx.Response(200, json={}, headers={"x-mbx-used-weight-1m": "42"})
        )
        await rest.get(URL)
        assert rest.used_weight == 42


class TestRetries:
    async def test_retries_transient_server_error_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"msg": "service unavailable"})
            return httpx.Response(200, json={"ok": True})

        assert await client_with(handler).get(URL) == {"ok": True}
        assert calls["n"] == 2

    async def test_gives_up_after_max_attempts(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={"msg": "down"})

        with pytest.raises(ExchangeResponseError):
            await client_with(handler, max_attempts=3).get(URL)
        assert calls["n"] == 3

    async def test_client_error_is_not_retried(self) -> None:
        """A 400 will not become a 200; retrying only wastes rate-limit weight."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

        with pytest.raises(ExchangeResponseError) as exc_info:
            await client_with(handler).get(URL)
        assert calls["n"] == 1
        assert exc_info.value.code == -1121
        assert exc_info.value.status_code == 400

    async def test_timeout_is_retried_then_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow")

        with pytest.raises(ExchangeConnectionError, match="timeout"):
            await client_with(handler, max_attempts=2).get(URL)

    async def test_transport_error_becomes_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        with pytest.raises(ExchangeConnectionError, match="transport error"):
            await client_with(handler, max_attempts=1).get(URL)


class TestRateLimiting:
    @pytest.mark.parametrize("status", [429, 418])
    async def test_rate_limit_statuses_raise_after_retries(self, status: int) -> None:
        """418 means a temporary ban - Binance bans IPs that ignore 429s."""
        rest = client_with(lambda request: httpx.Response(status, json={"msg": "slow down"}))
        with pytest.raises(ExchangeRateLimitError):
            await rest.get(URL)

    async def test_retry_after_header_is_honoured(self) -> None:
        recorded: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "0.01"}, json={})

        import asyncio

        original_sleep = asyncio.sleep

        async def spy(delay: float) -> None:
            recorded.append(delay)
            await original_sleep(0)

        asyncio.sleep = spy  # type: ignore[assignment]
        try:
            with pytest.raises(ExchangeRateLimitError) as exc_info:
                await client_with(handler, max_attempts=2).get(URL)
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

        assert exc_info.value.retry_after_seconds == 0.01
        # The venue's guidance was used, not our own backoff curve.
        assert recorded == [0.01]

    async def test_rate_limit_recovers_when_venue_relents(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={})
            return httpx.Response(200, json={"ok": True})

        assert await client_with(handler).get(URL) == {"ok": True}


class TestMalformedResponses:
    async def test_non_json_body_raises(self) -> None:
        rest = client_with(lambda request: httpx.Response(200, text="<html>maintenance</html>"))
        with pytest.raises(ExchangeResponseError, match="malformed JSON"):
            await rest.get(URL)

    async def test_error_message_is_extracted_from_payload(self) -> None:
        rest = client_with(
            lambda request: httpx.Response(400, json={"code": -4021, "msg": "bad depth limit"})
        )
        with pytest.raises(ExchangeResponseError, match="bad depth limit"):
            await rest.get(URL)
