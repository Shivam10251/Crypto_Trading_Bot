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
        # This SQLite database has no market tables, so the monitored markets are
        # unknown - reported as null rather than guessed from configuration.
        assert body["monitored_spot_markets"] is None
        assert body["monitored_perpetual_markets"] is None

    async def test_unbuilt_components_report_offline_not_fake_health(
        self, client: AsyncClient
    ) -> None:
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        statuses = {c["name"]: c for c in body["components"]}
        assert statuses["API"]["status"] == "HEALTHY"
        assert statuses["Database"]["status"] == "HEALTHY"
        for pending in ("Risk Engine",):
            assert statuses[pending]["status"] == "OFFLINE"
            assert "Phase" in statuses[pending]["detail"]

    async def test_execution_is_offline_while_it_is_switched_off(self, client: AsyncClient) -> None:
        """Built in Phase 8, but off by default - and it says which.

        "Not implemented" and "implemented and disarmed" are different facts,
        and the panel must not keep claiming the first once the second is true.
        """
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        execution = {c["name"]: c for c in body["components"]}["Paper Execution"]
        assert execution["status"] == "OFFLINE"
        assert "switched off" in execution["detail"]
        assert "Phase" not in execution["detail"]

    async def test_strategy_is_offline_without_recorded_opportunities(
        self, client: AsyncClient
    ) -> None:
        """Built, but nothing is running here - so it may not claim to be.

        This fixture has no opportunities table at all, which is the same
        answer as an empty one: the API has no evidence, so it says OFFLINE
        and why, rather than inventing a health.
        """
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        strategy = {c["name"]: c for c in body["components"]}["Strategy Engine"]
        assert strategy["status"] == "OFFLINE"
        assert "opportunities" in strategy["detail"]

    async def test_market_data_is_offline_without_live_quotes(self, client: AsyncClient) -> None:
        """Nothing streams in this test, so nothing may claim to."""
        body = (await client.get(f"{API_PREFIX}/system-status")).json()
        statuses = {c["name"]: c for c in body["components"]}
        for name in ("Exchange", "Market Data"):
            assert statuses[name]["status"] == "OFFLINE"


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


class TestServiceIndex:
    """Opening the bare host must explain the service, not return a bare 404."""

    async def test_root_returns_an_index(self, client: AsyncClient) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert body["profile"] == "development"

    async def test_root_points_at_the_real_endpoints(self, client: AsyncClient) -> None:
        body = (await client.get("/")).json()
        assert body["endpoints"] == {
            "health": f"{API_PREFIX}/health",
            "readiness": f"{API_PREFIX}/health/ready",
            "system_status": f"{API_PREFIX}/system-status",
        }
        assert body["docs"] == "/docs"

    async def test_advertised_links_actually_resolve(self, client: AsyncClient) -> None:
        """A broken link here is worse than no link."""
        body = (await client.get("/")).json()
        for path in body["endpoints"].values():
            assert (await client.get(path)).status_code == 200, path

    def test_docs_link_is_null_when_docs_disabled(self) -> None:
        from trading_bot.core.config import ApiConfig

        app = create_app(Settings(api=ApiConfig(docs_enabled=False)))
        assert app.docs_url is None

    def test_root_stays_out_of_the_versioned_contract(self) -> None:
        paths = create_app(Settings()).openapi()["paths"]
        assert "/" not in paths
        assert all(path.startswith(API_PREFIX) for path in paths)
