"""Tests for MCP gateway functionality.

Verifies the MCP gateway can be configured and accessed via FastMCPClient.
Replicates the functionality from archipelago/tests/no_servers/smoke_test.py
"""

import httpx
import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP

from runner.gateway.gateway import _AllowedToolsMiddleware


class TestMCPGatewayEmptyServers:
    """Tests for MCP gateway with zero servers configured.

    Replicates: archipelago/tests/no_servers/smoke_test.py

    Verifies:
    1. The /apps endpoint accepts an empty mcpServers configuration
    2. The /mcp/ gateway is mounted and accessible
    3. Clients can connect and list_tools() returns an empty list (not an error)
    """

    @pytest.mark.asyncio
    async def test_apps_endpoint_accepts_empty_config(self, base_url: str) -> None:
        """Test that /apps endpoint accepts empty mcpServers configuration."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{base_url}/apps",
                json={"mcpServers": {}},
                timeout=60,
            )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )

    @pytest.mark.asyncio
    async def test_mcp_client_connects_with_empty_servers(self, base_url: str) -> None:
        """Test that FastMCPClient can connect and list_tools returns empty."""
        # First configure empty servers
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"{base_url}/apps",
                json={"mcpServers": {}},
                timeout=60,
            )
            assert response.status_code == 200

        # Then verify MCP client can connect
        mcp_client = FastMCPClient(f"{base_url}/mcp/")
        async with mcp_client:
            tools_result = await mcp_client.session.list_tools()

        # Should return empty list, not error
        assert tools_result is not None
        assert len(tools_result.tools) == 0


@pytest.mark.asyncio
async def test_apps_endpoint_is_idempotent_for_identical_config(
    base_url: str,
) -> None:
    """A second /apps call with the same config short-circuits instead of re-swapping."""
    # Unique marker so the first call is a real swap regardless of which other
    # /apps tests ran first against this session-scoped container.
    body = {
        "mcpServers": {},
        "allowed_tool_names": ["__idempotent_marker__"],
    }
    async with httpx.AsyncClient() as client:
        first = await client.post(f"{base_url}/apps", json=body, timeout=60)
        second = await client.post(f"{base_url}/apps", json=body, timeout=60)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["duration_ms"] > 0.0
    assert second.json()["duration_ms"] == 0.0


@pytest.mark.asyncio
async def test_apps_endpoint_swaps_when_config_changes(base_url: str) -> None:
    """A /apps call with a different config must do a real swap, not short-circuit."""
    body_a = {"mcpServers": {}, "allowed_tool_names": ["__swap_marker_a__"]}
    body_b = {"mcpServers": {}, "allowed_tool_names": ["__swap_marker_b__"]}
    async with httpx.AsyncClient() as client:
        first = await client.post(f"{base_url}/apps", json=body_a, timeout=60)
        second = await client.post(f"{base_url}/apps", json=body_b, timeout=60)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["duration_ms"] > 0.0
    assert second.json()["duration_ms"] > 0.0


@pytest.mark.asyncio
async def test_apps_endpoint_idempotent_under_json_key_reordering(
    base_url: str,
) -> None:
    """Equality must be field-based, not JSON key-order based."""
    body_a = {
        "mcpServers": {},
        "allowed_tool_names": ["__key_order_marker__"],
    }
    body_b = {
        "allowed_tool_names": ["__key_order_marker__"],
        "mcpServers": {},
    }
    async with httpx.AsyncClient() as client:
        first = await client.post(f"{base_url}/apps", json=body_a, timeout=60)
        second = await client.post(f"{base_url}/apps", json=body_b, timeout=60)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["duration_ms"] > 0.0
    assert second.json()["duration_ms"] == 0.0


@pytest.mark.asyncio
async def test_apps_endpoint_invalidates_cache_on_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed readiness check must invalidate the cached config.

    swap_mcp_app mutates the FastAPI mount before running the readiness
    check, so when MCPReadinessError fires the gateway is in a partial
    state (new mount, broken servers) while the cached config still
    points to the previous successful swap. Without invalidation a
    subsequent /apps call matching that prior config would short-circuit
    and report success against a gateway that is in fact broken.

    Pure unit test against the router function — no testcontainer
    needed. ``monkeypatch.setattr`` on the module-level ``_mcp_config``
    auto-restores after the test so it doesn't pollute the
    session-scoped container's state.
    """
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException

    # ``runner.gateway/__init__.py`` does ``from .router import router``,
    # which binds the *APIRouter instance* (named ``router``) onto the
    # ``runner.gateway`` namespace and shadows the ``router.py`` submodule
    # attribute. Both ``import runner.gateway.router as M`` and
    # ``from runner.gateway import router as M`` therefore yield the
    # APIRouter, not the submodule we need to monkeypatch. ``import_module``
    # goes through ``sys.modules`` and returns the actual submodule.
    router_module = importlib.import_module("runner.gateway.router")
    state_module = importlib.import_module("runner.gateway.state")
    from runner.gateway.gateway import MCPReadinessError
    from runner.gateway.models import AppConfigRequest, ServerReadinessDetails

    config_a = AppConfigRequest(
        mcpServers={},
        allowed_tool_names=["__cache_invalidation_a__"],
    )
    config_b = AppConfigRequest(
        mcpServers={},
        allowed_tool_names=["__cache_invalidation_b__"],
    )

    monkeypatch.setattr(state_module, "_mcp_config", config_a)

    fake_request = MagicMock()
    fake_request.app = MagicMock()

    monkeypatch.setattr(
        router_module,
        "swap_mcp_app",
        AsyncMock(
            side_effect=MCPReadinessError(
                failed_servers={
                    "x": ServerReadinessDetails(error="timeout", attempts=1),
                },
            )
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        _ = await router_module.set_apps(config_b, fake_request)
    assert exc_info.value.status_code == 503
    assert state_module.get_mcp_config() is None, (
        "post-readiness-failure cache must be invalidated; otherwise a "
        "subsequent /apps {A} would short-circuit against a stale entry"
    )

    swap_calls = 0

    async def successful_swap(_req: object, _app: object) -> MagicMock:
        nonlocal swap_calls
        swap_calls += 1
        return MagicMock()

    monkeypatch.setattr(router_module, "swap_mcp_app", successful_swap)
    monkeypatch.setattr(
        router_module,
        "get_coordinator",
        lambda: MagicMock(start=AsyncMock()),
    )

    result = await router_module.set_apps(config_a, fake_request)
    assert swap_calls == 1, (
        "after a readiness failure the next /apps call must actually "
        "swap (not short-circuit against the old cache)"
    )
    assert result.duration_ms is not None and result.duration_ms > 0.0


@pytest.mark.asyncio
async def test_apps_endpoint_reconfigures_when_only_coordinator_config_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    from runner.coordinator.config.models import CoordinatorConfig
    from runner.gateway.models import AppConfigRequest

    router_module = importlib.import_module("runner.gateway.router")
    state_module = importlib.import_module("runner.gateway.state")

    config_a = AppConfigRequest(mcpServers={}, coordinator_config=None)
    config_b = AppConfigRequest(
        mcpServers={},
        coordinator_config=CoordinatorConfig(enabled=True),
    )
    monkeypatch.setattr(state_module, "_mcp_config", config_a)

    swap_calls = 0

    async def successful_swap(_req: object, _app: object) -> MagicMock:
        nonlocal swap_calls
        swap_calls += 1
        return MagicMock()

    coordinator = MagicMock(start=AsyncMock())
    fake_request = MagicMock()
    fake_request.app = MagicMock()
    monkeypatch.setattr(router_module, "swap_mcp_app", successful_swap)
    monkeypatch.setattr(router_module, "get_coordinator", lambda: coordinator)

    result = await router_module.set_apps(config_b, fake_request)

    assert swap_calls == 1
    coordinator.start.assert_awaited_once()
    assert state_module.get_mcp_config() == config_b
    assert result.duration_ms is not None and result.duration_ms > 0.0


@pytest.mark.asyncio
async def test_rest_only_server_in_readiness_but_not_aggregated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve_mcp_tools=False is waited on for readiness but excluded from /mcp."""
    import importlib
    from typing import Any
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from runner.gateway.models import AppConfigRequest, MCPServerConfig

    gw = importlib.import_module("runner.gateway.gateway")

    config = AppConfigRequest(
        mcpServers={
            "rest_only": MCPServerConfig(
                transport="http",
                url="http://rest.local/mcp/",
                serve_mcp_tools=False,
            ),
            "both_svc": MCPServerConfig(transport="http", url="http://both.local/mcp/"),
        }
    )

    aggregated: dict[str, Any] = {}

    def _fake_as_proxy(config_dict: dict[str, Any], **_: Any) -> FastMCP[Any]:
        aggregated["servers"] = set(config_dict["mcpServers"].keys())
        return FastMCP(name="Gateway")

    warm_gateway_servers: list[str] = []

    async def _fake_warm_gateway(_proxy: Any, servers: list[str], **_: Any) -> int:
        warm_gateway_servers.extend(servers)
        return 0

    readiness_urls: dict[str, str] = {}

    async def _fake_warm_servers(urls: dict[str, str], **_: Any) -> None:
        readiness_urls.update(urls)

    monkeypatch.setattr(gw, "warm_and_check_gateway", _fake_warm_gateway)
    monkeypatch.setattr(gw, "warm_and_check_servers", _fake_warm_servers)
    monkeypatch.setattr(gw.asyncio, "sleep", AsyncMock())

    # Isolate mount/lifespan state from the module globals so the test
    # doesn't pollute the session-scoped gateway state.
    store: dict[str, Any] = {"mount": None, "lm": None}
    monkeypatch.setattr(gw, "get_mcp_mount", lambda: store["mount"])
    monkeypatch.setattr(gw, "set_mcp_mount", lambda m: store.update(mount=m))
    monkeypatch.setattr(gw, "get_mcp_lifespan_manager", lambda: store["lm"])
    monkeypatch.setattr(gw, "set_mcp_lifespan_manager", lambda m: store.update(lm=m))

    real_as_proxy = FastMCP.as_proxy
    monkeypatch.setattr(FastMCP, "as_proxy", staticmethod(_fake_as_proxy))
    try:
        app = FastAPI()
        _ = await gw.swap_mcp_app(config, app)
    finally:
        monkeypatch.setattr(FastMCP, "as_proxy", staticmethod(real_as_proxy))

    assert aggregated["servers"] == {"both_svc"}
    assert warm_gateway_servers == ["both_svc"]
    assert readiness_urls == {"rest_only": "http://rest.local/mcp/"}


@pytest.mark.asyncio
async def test_allowlist_accepts_prefixed_name_for_single_server_tool() -> None:
    server = FastMCP("test", middleware=[_AllowedToolsMiddleware(["echo"])])

    @server.tool
    def insurance_echo(value: str) -> str:
        return value

    async with FastMCPClient(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool("insurance_echo", {"value": "hello"})

    assert [tool.name for tool in tools] == ["insurance_echo"]
    assert result.data == "hello"
