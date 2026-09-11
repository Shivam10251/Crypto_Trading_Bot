"""API contract for the Phase 0 endpoints."""

from __future__ import annotations

from httpx import AsyncClient

from trading_bot import __version__
from trading_bot.api.router import API_PREFIX
from trading_bot.core.config import Settings
from trading_bot.main import create_app


class TestHealth:
    async def test_liveness(self, client: AsyncClient) -> None:
        response = await client.get(f"{API_PREFIX}/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["profile"] == "development"
        assert body["server_time"].endswith("Z") or "+00:00" in body["server_time"]

    async def test_liveness_works_without_a_database(self, unhealthy_client: AsyncClient) -> None:
        assert (await unhealthy_client.get(f"{API_PREFIX}/health")).status_code == 200


class TestReadiness:
    async def test_ready_when_database_reachable(self, client: AsyncClient) -> None:
        response = await client.get(f"{API_PREFIX}/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["components"][0] == {
            "name": "Database",
            "status": "HEALTHY",
            "detail": "connected",
        }

    async def test_503_when_database_unreachable(self, unhealthy_client: AsyncClient) -> None:
        response = await unhealthy_client.get(f"{API_PREFIX}/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert body["components"][0]["status"] == "OFFLINE"


class TestSystemStatus:
    async def test_reports_real_configuration(self, client: AsyncClient) -> None:
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        assert body["profile"] == "development"
        assert body["execution_mode"] == "paper"
        assert body["live_execution_armed"] is False
        assert body["exchange"] == "binance"
        assert body["monitored_spot_markets"] == 1
        assert body["monitored_perpetual_markets"] == 1

    async def test_unbuilt_components_report_offline_not_fake_health(
        self, client: AsyncClient
    ) -> None:
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        statuses = {c["name"]: c for c in body["components"]}
        assert statuses["API"]["status"] == "HEALTHY"
        assert statuses["Database"]["status"] == "HEALTHY"
        for pending in ("Market Data", "Strategy Engine", "Risk Engine", "Paper Execution"):
            assert statuses[pending]["status"] == "OFFLINE"
            assert "Phase" in statuses[pending]["detail"]

    async def test_exchange_is_offline_before_phase_2(self, client: AsyncClient) -> None:
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        exchange = next(c for c in body["components"] if c["name"] == "Exchange")
        assert exchange["status"] == "OFFLINE"


class TestAppWiring:
    async def test_docs_enabled_in_development(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200

    def test_docs_disabled_when_configured_off(self) -> None:
        from trading_bot.core.config import ApiConfig

        app = create_app(Settings(api=ApiConfig(docs_enabled=False)))
        assert app.docs_url is None
        assert app.openapi_url is None

    async def test_unknown_route_is_404(self, client: AsyncClient) -> None:
        assert (await client.get(f"{API_PREFIX}/orders")).status_code == 404

    def test_all_routes_are_versioned(self) -> None:
        """Every documented endpoint sits under /api/v1 so the contract can evolve."""
        paths = create_app(Settings()).openapi()["paths"]
        assert paths
        assert all(path.startswith(API_PREFIX) for path in paths), paths
        assert f"{API_PREFIX}/health" in paths
        assert f"{API_PREFIX}/system-status" in paths
