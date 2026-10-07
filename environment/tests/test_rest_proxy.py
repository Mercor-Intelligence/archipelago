"""Tests for the path-based REST proxy on the gateway router.

Hermetic: both the gateway and the upstream service run in-process via
``httpx.ASGITransport`` (no real sockets), so it runs under network-isolated CI.
The proxy's own httpx client is patched to route to the in-process upstream.
"""

import gzip
import importlib
import json
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastmcp import FastMCP

from runner.gateway import gateway as gw
from runner.gateway.gateway import _build_mcp_app_with_proxy, _serving_config_dict
from runner.gateway.models import AppConfigRequest, MCPServerConfig
from runner.gateway.router import router
from runner.gateway.state import set_mcp_config

# Resolve the real module (the package re-exports ``router`` as the APIRouter,
# which shadows the submodule under attribute access).
router_module = importlib.import_module("runner.gateway.router")


def _build_upstream() -> FastAPI:
    app = FastAPI(title="upstream-service")

    @app.get("/pets")
    async def list_pets() -> list[str]:
        return []

    @app.post("/pets")
    async def create_pet() -> dict[str, object]:
        return {}

    @app.get("/pets/{pet_id}")
    async def get_pet(pet_id: str) -> dict[str, object]:
        return {}

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, object]:
        body = await request.json()
        return {
            "auth": request.headers.get("authorization"),
            "q": dict(request.query_params),
            "body": body,
        }

    @app.get("/gzipped")
    async def gzipped() -> Response:
        # Simulate an upstream (e.g. FastAPI GZipMiddleware) that gzip-encodes
        # its openapi.json. The proxy's httpx client auto-decompresses the body;
        # if it also forwarded Content-Encoding the caller would double-decompress.
        payload = json.dumps({"openapi": "3.1.0", "info": {"title": "gz"}}).encode()
        return Response(
            content=gzip.compress(payload),
            media_type="application/json",
            headers={"content-encoding": "gzip"},
        )

    return app


@pytest.fixture
def gateway_app() -> Generator[FastAPI]:
    upstream_transport = httpx.ASGITransport(app=_build_upstream())
    real_async_client = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        # The proxy constructs httpx.AsyncClient() with no transport -> route it
        # to the in-process upstream. Explicit-transport callers pass through.
        if "transport" in kwargs:
            return real_async_client(*args, **kwargs)
        return real_async_client(transport=upstream_transport, **kwargs)

    app = FastAPI()
    app.include_router(router)
    set_mcp_config(
        AppConfigRequest(
            mcpServers={
                "svc": MCPServerConfig(transport="http", url="http://svc.local/mcp/")
            }
        )
    )
    try:
        # Reset the proxy's module-level shared client so each test creates a
        # fresh one under this test's patched AsyncClient (no cross-test leak).
        router_module.reset_proxy_client()
        router_module._mcp_coverage_cache.clear()
        with patch.object(router_module.httpx, "AsyncClient", _client):
            yield app
    finally:
        set_mcp_config(None)
        router_module.reset_proxy_client()
        router_module._mcp_coverage_cache.clear()


@pytest.mark.asyncio
async def test_proxy_get_openapi(gateway_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.get("/rest/svc/openapi.json")
    assert resp.status_code == 200, resp.text
    spec = resp.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "upstream-service"
    assert "/echo" in spec["paths"]


@pytest.mark.asyncio
async def test_proxy_gzipped_upstream_not_double_decompressed(
    gateway_app: FastAPI,
) -> None:
    # The proxy's httpx client decompresses the gzip body; it must not forward
    # the upstream Content-Encoding, or the caller double-decompresses and fails
    # with "incorrect header check".
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.get("/rest/svc/gzipped")
    assert resp.status_code == 200, resp.text
    assert "content-encoding" not in {k.lower() for k in resp.headers}
    assert resp.json()["info"]["title"] == "gz"


@pytest.mark.asyncio
async def test_proxy_post_with_auth_and_query(gateway_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/rest/svc/echo?k=v",
            json={"hello": "world"},
            headers={"Authorization": "Bearer connect-token"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # The proxy rewrites the caller's sandbox connect token to the actor id
    # (default target_agent) for upstream Foundry tenancy, mirroring MCP.
    assert data["auth"] == "Bearer target_agent"
    assert data["q"] == {"k": "v"}
    assert data["body"] == {"hello": "world"}


@pytest.mark.asyncio
async def test_proxy_preserves_known_actor_id(gateway_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.post(
            "/rest/svc/echo",
            json={},
            headers={"Authorization": "Bearer coordinator"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth"] == "Bearer coordinator"


@pytest.mark.asyncio
async def test_proxy_unknown_service_404(gateway_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        resp = await client.get("/rest/nope/openapi.json")
    assert resp.status_code == 404


class _FakeTool:
    def __init__(self, annotations: Any) -> None:
        self.annotations = annotations


def _set_auto_config(*, exposes_mcp: bool = True) -> None:
    set_mcp_config(
        AppConfigRequest(
            mcpServers={
                "svc": MCPServerConfig(
                    transport="http",
                    url="http://svc.local/mcp/",
                    openapi_mcp_filter=True,
                    exposes_mcp=exposes_mcp,
                )
            }
        )
    )


def _patch_tools(tools: list[Any]) -> Any:
    async def _fake_list(url: str) -> list[Any]:
        return tools

    return patch.object(router_module, "_list_mcp_tools", _fake_list)


@pytest.mark.asyncio
async def test_auto_filter_removes_mcp_covered_operations(
    gateway_app: FastAPI,
) -> None:
    _set_auto_config()
    tools = [
        # dict annotations, exact match
        _FakeTool({"_route": "GET /pets"}),
        # object annotations, case-insensitive method + trailing slash tolerated
        _FakeTool(SimpleNamespace(_route="get /pets/{pet_id}/")),
        _FakeTool(None),
    ]
    transport = httpx.ASGITransport(app=gateway_app)
    with _patch_tools(tools):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw"
        ) as client:
            resp = await client.get("/rest/svc/openapi.json")
    assert resp.status_code == 200, resp.text
    paths = resp.json()["paths"]
    # GET /pets covered by MCP; POST /pets survives.
    assert list(paths["/pets"].keys()) == ["post"]
    # All operations covered -> path item dropped entirely.
    assert "/pets/{pet_id}" not in paths
    # Uncovered routes untouched.
    assert "post" in paths["/echo"]


@pytest.mark.asyncio
async def test_list_mcp_tools_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    class _FlakyClient:
        def __init__(self, url: str) -> None:
            pass

        async def __aenter__(self) -> "_FlakyClient":
            attempts.append(1)
            if len(attempts) == 1:
                raise ConnectionError("backend not ready")
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def list_tools(self) -> list[object]:
            return []

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(router_module, "FastMCPClient", _FlakyClient)
    monkeypatch.setattr(router_module.asyncio, "sleep", _no_sleep)
    tools = await router_module._list_mcp_tools("http://svc.local/mcp/")
    assert tools == []
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_auto_filter_keeps_ref_only_path_items() -> None:
    spec = {
        "paths": {
            "/pets": {"get": {}, "post": {}},
            "/shared": {"$ref": "#/components/pathItems/shared"},
        }
    }
    server = MCPServerConfig(transport="http", url="http://svc.local/mcp/")
    with patch.object(
        router_module,
        "_mcp_route_coverage",
        return_value=(2, {("get", "/pets")}),
    ):
        out = await router_module._filter_openapi_spec(spec, "svc", server)
    assert list(out["paths"]["/pets"].keys()) == ["post"]
    # $ref-only path items have no visible operations to subtract; keep them.
    assert out["paths"]["/shared"] == {"$ref": "#/components/pathItems/shared"}


@pytest.mark.asyncio
async def test_auto_filter_unlabeled_tools_empties_paths(
    gateway_app: FastAPI,
) -> None:
    # Tools exist but none carry _route -> MCP-only fallback (no double exposure).
    _set_auto_config()
    transport = httpx.ASGITransport(app=gateway_app)
    with _patch_tools([_FakeTool(None), _FakeTool({"readOnlyHint": True})]):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw"
        ) as client:
            resp = await client.get("/rest/svc/openapi.json")
    assert resp.status_code == 200, resp.text
    assert resp.json()["paths"] == {}


@pytest.mark.asyncio
async def test_auto_filter_skipped_when_exposes_mcp_false(
    gateway_app: FastAPI,
) -> None:
    _set_auto_config(exposes_mcp=False)
    transport = httpx.ASGITransport(app=gateway_app)
    with _patch_tools([_FakeTool({"_route": "GET /pets"})]):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw"
        ) as client:
            resp = await client.get("/rest/svc/openapi.json")
    assert resp.status_code == 200, resp.text
    assert "get" in resp.json()["paths"]["/pets"]


@pytest.mark.asyncio
async def test_auto_filter_only_applies_to_openapi_json(gateway_app: FastAPI) -> None:
    _set_auto_config()
    transport = httpx.ASGITransport(app=gateway_app)
    with _patch_tools([_FakeTool({"_route": "POST /echo"})]):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw"
        ) as client:
            resp = await client.post("/rest/svc/echo", json={"hi": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"] == {"hi": 1}


@pytest.mark.asyncio
async def test_auto_filter_coverage_is_cached(gateway_app: FastAPI) -> None:
    _set_auto_config()
    calls = {"n": 0}

    async def _counting_list(url: str) -> list[Any]:
        calls["n"] += 1
        return [_FakeTool({"_route": "GET /pets"})]

    transport = httpx.ASGITransport(app=gateway_app)
    with patch.object(router_module, "_list_mcp_tools", _counting_list):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw"
        ) as client:
            _ = await client.get("/rest/svc/openapi.json")
            _ = await client.get("/rest/svc/openapi.json")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_rest_only_service_routes_but_not_aggregated() -> None:
    """serve_mcp_tools=False routes via /rest but is excluded from /mcp; both does both."""
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

    captured: dict[str, Any] = {}

    def _fake_as_proxy(config_dict: dict[str, Any], **_: Any) -> FastMCP[Any]:
        captured["servers"] = set(config_dict["mcpServers"].keys())
        return FastMCP(name="Gateway")

    with patch.object(gw, "FastMCP") as mock_fastmcp:
        mock_fastmcp.as_proxy.side_effect = _fake_as_proxy
        _build_mcp_app_with_proxy(_serving_config_dict(config))

    assert captured["servers"] == {"both_svc"}

    set_mcp_config(config)
    try:
        assert router_module._resolve_service_base("rest_only") == "http://rest.local"
        assert router_module._resolve_service_base("both_svc") == "http://both.local"
    finally:
        set_mcp_config(None)
