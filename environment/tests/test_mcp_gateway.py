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


class TestProxyReadTimeout:
    """The env-gated upstream read-timeout on the gateway proxy client.

    The proxy's `ProxyClient.timeout` becomes the ClientSession
    `read_timeout_seconds` — the only knob FastMCP 3.x applies to the upstream
    read. Unset env keeps the default (None). We assert the value the proxy's
    client_factory produces.
    """

    import datetime

    def _read_timeout(self, monkeypatch, env_value):
        import importlib

        gw = importlib.import_module("runner.gateway.gateway")
        if env_value is None:
            monkeypatch.delenv("MCP_GATEWAY_SSE_READ_TIMEOUT_SECONDS", raising=False)
        else:
            monkeypatch.setenv("MCP_GATEWAY_SSE_READ_TIMEOUT_SECONDS", env_value)

        config_dict = {
            "mcpServers": {"svc": {"transport": "http", "url": "http://a.local/mcp/"}}
        }
        proxy = gw._build_proxy(config_dict)
        client = proxy.client_factory()
        return client._session_kwargs.get("read_timeout_seconds")

    def test_env_sets_read_timeout(self, monkeypatch):
        assert self._read_timeout(monkeypatch, "900") == self.datetime.timedelta(
            seconds=900
        )

    def test_unset_env_leaves_default(self, monkeypatch):
        assert self._read_timeout(monkeypatch, None) is None

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
    def test_invalid_env_ignored(self, monkeypatch, bad):
        assert self._read_timeout(monkeypatch, bad) is None


class TestSessionAffinityGate:
    """The per-server `session_affinity` gate that selects the stateful proxy.

    Real behavior — no mocking of the unit under test. The gateway must (a) detect
    when a *serving* server requests session affinity (so it reuses one backend
    session instead of opening a fresh one per call, e.g. for the browser) and
    (b) strip the gateway-only `session_affinity` key before handing the config to
    FastMCP's parser (which rejects unknown server keys).
    """

    def _schema(self, **servers):
        from runner.gateway.models import MCPSchema, MCPServerConfig

        return MCPSchema(
            mcpServers={n: MCPServerConfig(**cfg) for n, cfg in servers.items()}
        )

    def test_default_is_stateless(self):
        from runner.gateway.gateway import _session_affinity_requested

        schema = self._schema(svc={"transport": "http", "url": "http://a/mcp/"})
        assert _session_affinity_requested(schema) is False

    def test_detects_affinity_on_serving_server(self):
        from runner.gateway.gateway import _session_affinity_requested

        schema = self._schema(
            browser={
                "transport": "http",
                "url": "http://b/mcp/",
                "session_affinity": True,
            },
            files={"transport": "http", "url": "http://f/mcp/"},
        )
        assert _session_affinity_requested(schema) is True

    def test_ignores_affinity_on_non_serving_server(self):
        from runner.gateway.gateway import _session_affinity_requested

        # serve_mcp_tools=False servers are /rest-only; their affinity is moot.
        schema = self._schema(
            rest={
                "transport": "rest",
                "url": "http://r/mcp/",
                "serve_mcp_tools": False,
                "session_affinity": True,
            },
        )
        assert _session_affinity_requested(schema) is False

    def test_serving_config_dict_strips_gateway_only_keys(self):
        from runner.gateway.gateway import _serving_config_dict

        schema = self._schema(
            browser={
                "transport": "http",
                "url": "http://b/mcp/",
                "session_affinity": True,
            },
        )
        config_dict = _serving_config_dict(schema)
        assert config_dict is not None
        browser_cfg = config_dict["mcpServers"]["browser"]
        # Gateway-only keys must NOT leak into FastMCP's per-server config.
        assert "session_affinity" not in browser_cfg
        assert "serve_mcp_tools" not in browser_cfg
        assert "openapi_mcp_filter" not in browser_cfg
        assert "exposes_mcp" not in browser_cfg
        assert browser_cfg["url"] == "http://b/mcp/"

    def test_serving_config_dict_excludes_non_serving(self):
        from runner.gateway.gateway import _serving_config_dict

        # Mixed config: a serving server and a /rest-only one — only the serving
        # server reaches the aggregated FastMCP config.
        schema = self._schema(
            browser={"transport": "http", "url": "http://b/mcp/"},
            rest={
                "transport": "rest",
                "url": "http://r/mcp/",
                "serve_mcp_tools": False,
            },
        )
        config_dict = _serving_config_dict(schema)
        assert config_dict is not None
        assert set(config_dict["mcpServers"]) == {"browser"}

    def test_serving_config_dict_none_when_no_serving(self):
        from runner.gateway.gateway import _serving_config_dict

        schema = self._schema(
            rest={
                "transport": "rest",
                "url": "http://r/mcp/",
                "serve_mcp_tools": False,
            },
        )
        assert _serving_config_dict(schema) is None


# --- Session-affine proxy lifecycle regression coverage -----------------------


def _isolate_swap_env(monkeypatch: pytest.MonkeyPatch, gw: object) -> dict[str, object]:
    """Stub readiness/sleep and isolate mount/lifespan/stateful state from globals."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(gw, "warm_and_check_gateway", AsyncMock(return_value=0))
    monkeypatch.setattr(gw, "warm_and_check_servers", AsyncMock())
    monkeypatch.setattr(gw.asyncio, "sleep", AsyncMock())  # pyright: ignore[reportAttributeAccessIssue]
    store: dict[str, object] = {"mount": None, "lm": None, "stateful": None}
    monkeypatch.setattr(gw, "get_mcp_mount", lambda: store["mount"])
    monkeypatch.setattr(gw, "set_mcp_mount", lambda m: store.update(mount=m))
    monkeypatch.setattr(gw, "get_mcp_lifespan_manager", lambda: store["lm"])
    monkeypatch.setattr(gw, "set_mcp_lifespan_manager", lambda m: store.update(lm=m))
    monkeypatch.setattr(gw, "get_current_stateful", lambda: store["stateful"])
    monkeypatch.setattr(gw, "set_current_stateful", lambda h: store.update(stateful=h))
    return store


@pytest.mark.asyncio
async def test_shutdown_stateful_none_is_noop() -> None:
    """_shutdown_stateful(None) is a no-op — a stateless gateway has no handle."""
    import importlib

    gw = importlib.import_module("runner.gateway.gateway")
    await gw._shutdown_stateful(None)  # must not raise


@pytest.mark.asyncio
async def test_shutdown_stateful_sets_stop_reaps_and_swallows_error() -> None:
    """_shutdown_stateful signals stop, awaits the owner, and swallows its error."""
    import asyncio
    import importlib

    gw = importlib.import_module("runner.gateway.gateway")
    stop = asyncio.Event()

    async def _owner() -> None:
        await stop.wait()
        raise RuntimeError("owner blew up on teardown")

    task = asyncio.create_task(_owner())
    await asyncio.sleep(0)  # let the owner reach `await stop.wait()`

    await gw._shutdown_stateful((stop, task))  # must not raise despite owner error

    assert stop.is_set()
    assert task.done()


@pytest.mark.asyncio
async def test_shutdown_stateful_proxy_clears_active_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown_stateful_proxy disconnects the active handle and clears it."""
    import importlib

    gw = importlib.import_module("runner.gateway.gateway")
    disconnected: list[object] = []
    store: dict[str, object] = {"stateful": ("stop", "task")}

    async def _fake_shutdown(handle: object) -> None:
        disconnected.append(handle)

    monkeypatch.setattr(gw, "_shutdown_stateful", _fake_shutdown)
    monkeypatch.setattr(gw, "get_current_stateful", lambda: store["stateful"])
    monkeypatch.setattr(gw, "set_current_stateful", lambda h: store.update(stateful=h))

    await gw.shutdown_stateful_proxy()

    assert disconnected == [("stop", "task")]
    assert store["stateful"] is None


@pytest.mark.asyncio
async def test_swap_uses_stateful_builder_only_when_affinity_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """swap_mcp_app picks the session-affine builder iff a serving server opts in."""
    import importlib
    from typing import Any

    from fastapi import FastAPI

    from runner.gateway.models import AppConfigRequest, MCPServerConfig

    gw = importlib.import_module("runner.gateway.gateway")
    _isolate_swap_env(monkeypatch, gw)

    calls: list[str] = []

    async def _fake_stateful(_config_dict: dict[str, Any], _affine: set[str]):
        calls.append("stateful")
        return (
            FastMCP(name="Gateway").http_app(path="/"),
            FastMCP(name="Gateway"),
            ("s", "t"),
        )

    def _fake_stateless(_config: Any):
        calls.append("stateless")
        return FastMCP(name="Gateway").http_app(path="/"), FastMCP(name="Gateway")

    from unittest.mock import AsyncMock

    monkeypatch.setattr(gw, "_build_stateful_mcp_app_with_proxy", _fake_stateful)
    monkeypatch.setattr(gw, "_build_mcp_app_with_proxy", _fake_stateless)
    monkeypatch.setattr(gw, "_shutdown_stateful", AsyncMock())

    affine = AppConfigRequest(
        mcpServers={
            "browser": MCPServerConfig(
                transport="http", url="http://b/mcp/", session_affinity=True
            )
        }
    )
    plain = AppConfigRequest(
        mcpServers={"svc": MCPServerConfig(transport="http", url="http://s/mcp/")}
    )

    await gw.swap_mcp_app(affine, FastAPI())
    assert calls == ["stateful"]
    calls.clear()
    await gw.swap_mcp_app(plain, FastAPI())
    assert calls == ["stateless"]


@pytest.mark.asyncio
async def test_swap_publishes_new_then_disconnects_prev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful session-affine swap publishes the new client active BEFORE
    disconnecting the previous one (no window where neither is active)."""
    import importlib
    from typing import Any

    from fastapi import FastAPI

    from runner.gateway.models import AppConfigRequest, MCPServerConfig

    gw = importlib.import_module("runner.gateway.gateway")
    store = _isolate_swap_env(monkeypatch, gw)

    new_handle = ("new_stop", "new_task")
    prev_handle = ("prev_stop", "prev_task")
    seen: dict[str, Any] = {}

    async def _fake_stateful(_config_dict: dict[str, Any], _affine: set[str]):
        return (
            FastMCP(name="Gateway").http_app(path="/"),
            FastMCP(name="Gateway"),
            new_handle,
        )

    async def _fake_shutdown(handle: Any) -> None:
        if handle == prev_handle:
            # The previous client is only disconnected after the new one is published.
            seen["current_at_prev_disconnect"] = store["stateful"]

    monkeypatch.setattr(gw, "_build_stateful_mcp_app_with_proxy", _fake_stateful)
    monkeypatch.setattr(gw, "_shutdown_stateful", _fake_shutdown)
    store["stateful"] = prev_handle

    config = AppConfigRequest(
        mcpServers={
            "browser": MCPServerConfig(
                transport="http", url="http://b/mcp/", session_affinity=True
            )
        }
    )
    await gw.swap_mcp_app(config, FastAPI())

    assert store["stateful"] == new_handle
    assert seen["current_at_prev_disconnect"] == new_handle


@pytest.mark.asyncio
async def test_swap_disconnects_new_on_pre_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the swap fails before publishing, the just-built session-affine client is
    disconnected and the previous one stays active (no leak, no live-gateway loss)."""
    import importlib
    from typing import Any

    from fastapi import FastAPI

    from runner.gateway.models import AppConfigRequest, MCPServerConfig

    gw = importlib.import_module("runner.gateway.gateway")
    store = _isolate_swap_env(monkeypatch, gw)

    new_handle = ("new_stop", "new_task")
    prev_handle = ("prev_stop", "prev_task")
    disconnected: list[Any] = []

    async def _fake_stateful(_config_dict: dict[str, Any], _affine: set[str]):
        return (
            FastMCP(name="Gateway").http_app(path="/"),
            FastMCP(name="Gateway"),
            new_handle,
        )

    class _FailingLifespan:
        def __init__(self, _app: Any) -> None: ...

        async def __aenter__(self) -> Any:
            raise RuntimeError("lifespan failed to start")

        async def __aexit__(self, *_: Any) -> None: ...

    async def _fake_shutdown(handle: Any) -> None:
        disconnected.append(handle)

    monkeypatch.setattr(gw, "_build_stateful_mcp_app_with_proxy", _fake_stateful)
    monkeypatch.setattr(gw, "LifespanManager", _FailingLifespan)
    monkeypatch.setattr(gw, "_shutdown_stateful", _fake_shutdown)
    store["stateful"] = prev_handle

    config = AppConfigRequest(
        mcpServers={
            "browser": MCPServerConfig(
                transport="http", url="http://b/mcp/", session_affinity=True
            )
        }
    )
    with pytest.raises(RuntimeError, match="Failed to swap MCP gateway"):
        await gw.swap_mcp_app(config, FastAPI())

    assert new_handle in disconnected
    assert prev_handle not in disconnected
    assert store["stateful"] == prev_handle


# --- Multi-server session-affine composition (per-backend drop recovery) -------


@pytest.mark.asyncio
async def test_multi_server_composite_tool_names_match_native() -> None:
    """The per-server composite's aggregated tool names are byte-identical to
    FastMCP's native multi-server proxy, so naming can never silently diverge."""
    import importlib

    from fastmcp.utilities.tests import run_server_async

    gw = importlib.import_module("runner.gateway.gateway")

    backend_a = FastMCP("a")

    @backend_a.tool
    def navigate(url: str) -> str:
        return url

    backend_b = FastMCP("b")

    @backend_b.tool
    def read_cell(addr: str) -> str:
        return addr

    async with (
        run_server_async(backend_a) as url_a,
        run_server_async(backend_b) as url_b,
    ):
        config_dict = {
            "mcpServers": {
                "playwright": {"transport": "http", "url": url_a},
                "excel": {"transport": "http", "url": url_b},
            }
        }
        # Native reference: FastMCP's own multi-server prefixing (the stateless path).
        native = FastMCP.as_proxy(config_dict, name="Gateway")
        async with FastMCPClient(native) as c:
            native_names = sorted(t.name for t in await c.list_tools())

        # Ours: the real builder under test, in the mixed shape (one affine
        # backend, one stateless) — naming must still match native.
        _app, composite, handle = await gw._build_multi_stateful_mcp_app_with_proxy(
            config_dict, {"playwright"}
        )
        try:
            async with FastMCPClient(composite) as c:
                ours_names = sorted(t.name for t in await c.list_tools())
        finally:
            await gw._shutdown_stateful(handle)

    assert ours_names == native_names
    assert ours_names == ["excel_read_cell", "playwright_navigate"]


@pytest.mark.asyncio
async def test_per_backend_drop_recovers_and_isolates() -> None:
    """A dropped backend session recovers via the reconnect override on the next
    call, and a sibling backend is unaffected (per-backend isolation)."""
    import importlib

    from fastmcp.server.server import create_proxy
    from fastmcp.utilities.tests import run_server_async
    from loguru import logger as loguru_logger

    gw = importlib.import_module("runner.gateway.gateway")

    a_calls: list[int] = []
    b_calls: list[int] = []
    backend_a = FastMCP("a")

    @backend_a.tool
    def echo_a(x: int) -> int:
        a_calls.append(x)
        return x

    backend_b = FastMCP("b")

    @backend_b.tool
    def echo_b(y: int) -> int:
        b_calls.append(y)
        return y

    async with (
        run_server_async(backend_a) as url_a,
        run_server_async(backend_b) as url_b,
    ):
        client_a = gw._ReconnectingStatefulProxyClient(
            {"mcpServers": {"a": {"transport": "http", "url": url_a}}}
        )
        client_b = gw._ReconnectingStatefulProxyClient(
            {"mcpServers": {"b": {"transport": "http", "url": url_b}}}
        )
        await client_a.__aenter__()
        await client_b.__aenter__()
        composite = FastMCP(name="Gateway")
        composite.mount(create_proxy(client_a, name="Proxy-a"), namespace="a")
        composite.mount(create_proxy(client_b, name="Proxy-b"), namespace="b")

        warnings: list[str] = []
        sink_id = loguru_logger.add(lambda m: warnings.append(str(m)), level="WARNING")
        try:
            async with FastMCPClient(composite) as gwc:
                r1 = await gwc.call_tool("a_echo_a", {"x": 1})
                assert r1.data == 1
                assert client_a._session_state.nesting_counter > 0

                # Force a clean session drop on A WITHOUT resetting its counter —
                # exactly the client.py nesting-counter invariant a real drop leaves.
                client_a._session_state.stop_event.set()
                await client_a._session_state.session_task

                r2 = await gwc.call_tool("a_echo_a", {"x": 2})  # override reconnects
                assert r2.data == 2
                rb = await gwc.call_tool("b_echo_b", {"y": 9})  # B never dropped
                assert rb.data == 9
        finally:
            await client_a._disconnect(force=True)
            await client_b._disconnect(force=True)
            loguru_logger.remove(sink_id)

    assert a_calls == [1, 2]
    assert b_calls == [9]
    # Proves the recovery came from the nesting-counter override, not luck:
    assert any("reconnecting fresh" in w for w in warnings)


@pytest.mark.asyncio
async def test_stateful_builder_dispatches_multi_for_2plus_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_stateful_mcp_app_with_proxy routes 2+ servers to the multi builder and
    keeps the verbatim single-server path for exactly one server."""
    import importlib
    from typing import Any

    gw = importlib.import_module("runner.gateway.gateway")

    seen: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    sentinel = (object(), object(), object())

    async def _fake_multi(cfg: dict[str, Any], affine: set[str]) -> Any:
        seen.append((tuple(cfg["mcpServers"]), tuple(sorted(affine))))
        return sentinel

    monkeypatch.setattr(gw, "_build_multi_stateful_mcp_app_with_proxy", _fake_multi)

    out = await gw._build_stateful_mcp_app_with_proxy(
        {
            "mcpServers": {
                "a": {"transport": "http", "url": "http://a/mcp/"},
                "b": {"transport": "http", "url": "http://b/mcp/"},
            }
        },
        {"a"},
    )
    assert out is sentinel
    assert seen == [(("a", "b"), ("a",))]

    # One server → single-server path, which must NOT call the multi builder. Stub
    # the client so the single-server path fails fast without a real connection.
    class _FailFast:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> Any:
            raise RuntimeError("no real connect in test")

        async def _disconnect(self, force: bool = False) -> None: ...

    monkeypatch.setattr(gw, "_ReconnectingStatefulProxyClient", _FailFast)
    seen.clear()
    with pytest.raises(RuntimeError):
        await gw._build_stateful_mcp_app_with_proxy(
            {"mcpServers": {"only": {"transport": "http", "url": "http://o/mcp/"}}},
            {"only"},
        )
    assert seen == []


@pytest.mark.asyncio
async def test_multi_owner_drains_connected_backends_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /apps request cancelled mid-connect must NOT orphan the backends that
    already connected — the owner task drains them on the cancel path."""
    import asyncio
    import importlib
    from typing import Any

    gw = importlib.import_module("runner.gateway.gateway")

    instances: list[Any] = []
    started_hang = asyncio.Event()

    class _FakeClient:
        def __init__(self, config_dict: dict[str, Any], timeout: Any = None) -> None:
            self.name = next(iter(config_dict["mcpServers"]))
            self.disconnected = False
            instances.append(self)

        async def __aenter__(self) -> "Any":
            if self.name == "hang":
                started_hang.set()
                await asyncio.Event().wait()  # connect never completes
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def _disconnect(self, force: bool = False) -> None:
            self.disconnected = True

    monkeypatch.setattr(gw, "_ReconnectingStatefulProxyClient", _FakeClient)

    cfg = {
        "mcpServers": {
            "good": {"transport": "http", "url": "http://a/mcp/"},
            "hang": {"transport": "http", "url": "http://h/mcp/"},
        }
    }
    task = asyncio.create_task(
        gw._build_multi_stateful_mcp_app_with_proxy(cfg, {"good", "hang"})
    )
    await asyncio.wait_for(
        started_hang.wait(), timeout=5
    )  # "good" connected, "hang" stuck
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    good = next(i for i in instances if i.name == "good")
    assert good.disconnected is True  # the already-connected backend was released


@pytest.mark.asyncio
async def test_single_server_client_reuses_one_session() -> None:
    """The single-server affine client reuses ONE backend session across calls —
    the about:blank-defeating behavior. A regression here would silently break the
    core fix, so assert the same session object survives two tool calls."""
    import importlib

    from fastmcp.server.server import create_proxy
    from fastmcp.utilities.tests import run_server_async

    gw = importlib.import_module("runner.gateway.gateway")

    calls: list[int] = []
    backend = FastMCP("b")

    @backend.tool
    def ping() -> str:
        calls.append(1)
        return "pong"

    async with run_server_async(backend) as url:
        client = gw._ReconnectingStatefulProxyClient(
            {"mcpServers": {"b": {"transport": "http", "url": url}}}
        )
        await client.__aenter__()
        proxy = create_proxy(client, name="Gateway")
        try:
            async with FastMCPClient(proxy) as c:
                await c.call_tool("ping", {})
                sess1 = client._session_state.session_task
                await c.call_tool("ping", {})
                sess2 = client._session_state.session_task
        finally:
            await client._disconnect(force=True)

    assert len(calls) == 2
    assert (
        sess1 is not None and sess1 is sess2
    )  # one reused session, not fresh-per-call


@pytest.mark.asyncio
async def test_mixed_affinity_forwards_auth_header_to_stateless_backend() -> None:
    """Only servers that opt into session_affinity get the shared connect-time
    session; every other backend must keep opening a fresh session inside the
    request so the per-call `Authorization: Bearer <actor_id>` rewrite reaches
    it. Regression: sweeping every server in the world into the affine path
    starved tenancy-enforcing backends (email) of the header — every call failed
    with "Missing Authorization: Bearer <user_id> header."."""
    import importlib

    from fastmcp.server.dependencies import get_http_headers
    from fastmcp.utilities.tests import run_server_async

    gw = importlib.import_module("runner.gateway.gateway")

    affine_backend = FastMCP("affine")

    @affine_backend.tool
    def ping() -> str:
        return "pong"

    plain_backend = FastMCP("plain")

    @plain_backend.tool
    def whoami() -> str:
        # Echo the Authorization header this backend actually received, the way
        # a tenancy-enforcing Foundry app reads it.
        return get_http_headers(include={"authorization"}).get(
            "authorization", "missing"
        )

    async with (
        run_server_async(affine_backend) as url_a,
        run_server_async(plain_backend) as url_p,
    ):
        config_dict = {
            "mcpServers": {
                "affine": {"transport": "http", "url": url_a},
                "plain": {"transport": "http", "url": url_p},
            }
        }
        _app, composite, handle = await gw._build_multi_stateful_mcp_app_with_proxy(
            config_dict, {"affine"}
        )
        try:
            # Serve the composite over HTTP: the header rewrite and forwarding
            # are request-scoped, so an in-memory client would not exercise them.
            async with run_server_async(composite) as gw_url:
                async with FastMCPClient(gw_url, auth="target_agent") as c:
                    for _ in range(2):
                        r = await c.call_tool("plain_whoami", {})
                        assert r.data == "Bearer target_agent"
                    r = await c.call_tool("affine_ping", {})
                    assert r.data == "pong"
        finally:
            await gw._shutdown_stateful(handle)
