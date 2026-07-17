"""FastAPI router for MCP gateway endpoints.

This module defines the FastAPI router that handles the /apps endpoint
for configuring MCP servers.
"""

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastmcp import Client as FastMCPClient
from loguru import logger

from runner.coordinator.agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    TARGET_AGENT_ACTOR_ID_VALUE,
)
from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.runtime import get_coordinator

from .gateway import MCPReadinessError, swap_mcp_app
from .models import AppConfigRequest, AppConfigResult, MCPServerConfig
from .state import get_mcp_config, set_mcp_config

router = APIRouter()

# Hop-by-hop headers must not be forwarded across the proxy boundary.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# Shared client for the REST proxy: reused across requests so we don't pay a
# fresh TLS/TCP handshake per proxied call. Closed on app shutdown.
_proxy_client: httpx.AsyncClient | None = None


def get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None or _proxy_client.is_closed:
        _proxy_client = httpx.AsyncClient(timeout=300.0)
    return _proxy_client


async def close_proxy_client() -> None:
    global _proxy_client
    if _proxy_client is not None and not _proxy_client.is_closed:
        await _proxy_client.aclose()
    _proxy_client = None


def reset_proxy_client() -> None:
    """Drop the cached proxy client without closing it (test isolation helper)."""
    global _proxy_client
    _proxy_client = None


def _resolve_actor_id(authorization: str | None) -> str:
    """Resolve the upstream actor id for a proxied REST call.

    Mirrors the MCP path's auth rewrite (CoordinatorToolCallMiddleware): the
    incoming bearer is a Modal sandbox connect token (internal TA) or an actor
    id (VCA). Foundry backends expect ``Bearer <actor_id>`` for tenancy, so we
    keep a known actor id and otherwise default to the target agent.
    """
    token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer ") :].strip() or None
    if token in {TARGET_AGENT_ACTOR_ID_VALUE, COORDINATOR_ACTOR_ID_VALUE}:
        return token  # pyright: ignore[reportReturnType]
    try:
        if token and token in get_coordinator().store.config.read().agents:
            return token
    except Exception:
        pass
    return TARGET_AGENT_ACTOR_ID_VALUE


def _resolve_server(service: str) -> MCPServerConfig | None:
    """Return the registered server config for a service, else None."""
    config = get_mcp_config()
    if config is None:
        return None
    return config.mcpServers.get(service)


def _resolve_service_base(service: str) -> str | None:
    """Return http://host:port base for a registered HTTP service, else None."""
    server = _resolve_server(service)
    if server is None or not server.url:
        return None
    parts = urlsplit(server.url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

# Cached MCP route coverage per (service, mcp_url): (tool_count, {(method, path)}).
_mcp_coverage_cache: dict[tuple[str, str], tuple[int, set[tuple[str, str]]]] = {}


async def _list_mcp_tools(url: str) -> list[Any]:
    # One retry absorbs transient backend hiccups; persistent failure still 502s.
    try:
        async with FastMCPClient(url) as client:
            return list(await client.list_tools())
    except Exception:
        await asyncio.sleep(1.0)
        async with FastMCPClient(url) as client:
            return list(await client.list_tools())


def _route_annotation(tool: Any) -> str | None:
    """Read the `_route` annotation ("METHOD /path") from a tool, if present."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        route = annotations.get("_route")
    else:
        route = getattr(annotations, "_route", None)
        if route is None:
            route = (getattr(annotations, "model_extra", None) or {}).get("_route")
    return route if isinstance(route, str) else None


def _normalize_route(method: str, path: str) -> tuple[str, str]:
    return method.lower(), (path.rstrip("/") or "/")


async def _mcp_route_coverage(
    service: str, mcp_url: str
) -> tuple[int, set[tuple[str, str]]]:
    """Return (tool_count, covered routes) for a service's MCP backend, cached."""
    key = (service, mcp_url)
    cached = _mcp_coverage_cache.get(key)
    if cached is not None:
        return cached
    tools = await _list_mcp_tools(mcp_url)
    covered: set[tuple[str, str]] = set()
    for tool in tools:
        route = _route_annotation(tool)
        if not route or " " not in route:
            continue
        method, _, path = route.partition(" ")
        covered.add(_normalize_route(method, path.strip()))
    result = (len(tools), covered)
    _mcp_coverage_cache[key] = result
    return result


async def _filter_openapi_spec(
    spec: dict[str, Any], service: str, server: MCPServerConfig
) -> dict[str, Any]:
    """Drop MCP-covered operations from an OpenAPI spec (AUTO transport)."""
    if not server.exposes_mcp or not server.url:
        return spec
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return spec
    tool_count, covered = await _mcp_route_coverage(service, server.url)
    if tool_count > 0 and not covered:
        # Unlabeled MCP app: hide all REST operations to avoid double exposure.
        spec["paths"] = {}
        return spec
    filtered: dict[str, Any] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            filtered[path] = item
            continue
        norm_path = path.rstrip("/") or "/"
        kept = {k: v for k, v in item.items() if (k.lower(), norm_path) not in covered}
        # Keep $ref-only path items: their operations aren't visible here, so
        # nothing was subtracted and they stay exposed.
        if any(k.lower() in _HTTP_METHODS for k in kept) or "$ref" in kept:
            filtered[path] = kept
    spec["paths"] = filtered
    return spec


@router.api_route(
    "/rest/{service}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_rest(service: str, path: str, request: Request) -> Response:
    """Proxy an external request to a registered service's local REST API."""
    server = _resolve_server(service)
    base = _resolve_service_base(service)
    if server is None or base is None:
        raise HTTPException(status_code=404, detail=f"Unknown service {service!r}")

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "authorization"
    }
    # Rewrite auth to the actor id (same contract as the MCP path): upstream
    # Foundry backends key tenancy off Bearer <actor_id>, not the sandbox
    # connect token the caller arrives with.
    headers["Authorization"] = (
        f"Bearer {_resolve_actor_id(request.headers.get('authorization'))}"
    )
    body = await request.body()
    upstream = await get_proxy_client().request(
        request.method,
        f"{base}/{path}",
        params=request.query_params,
        headers=headers,
        content=body,
    )
    # httpx already decompressed upstream.content, so drop the upstream
    # Content-Encoding (Content-Length is dropped via _HOP_BY_HOP). Otherwise the
    # caller would try to decompress already-plaintext bytes and fail with
    # "incorrect header check".
    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
    }
    content = upstream.content
    if (
        server.openapi_mcp_filter
        and request.method == "GET"
        and path.lstrip("/") == "openapi.json"
        and upstream.status_code == 200
    ):
        try:
            spec = await _filter_openapi_spec(upstream.json(), service, server)
        except Exception as e:
            logger.error(f"REST proxy: openapi MCP-filter failed for '{service}': {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Failed to filter openapi.json for {service!r}",
            ) from e
        content = json.dumps(spec).encode()
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


@router.post("/apps", response_model=AppConfigResult)
async def set_apps(request: AppConfigRequest, http_request: Request) -> AppConfigResult:
    """Set/update MCP servers configuration.

    This endpoint hot-swaps the MCP gateway with new configuration.
    Can be called multiple times to update the configuration.

    If the incoming configuration is byte-for-byte identical to the
    currently-mounted one, the swap is skipped entirely.

    Args:
        request: AppConfigRequest containing mcpServers configuration
        http_request: FastAPI Request object to access the app instance

    Returns:
        AppConfigResult with list of server names

    Raises:
        HTTPException: If configuration is invalid or swap fails
    """
    app: FastAPI = http_request.app
    server_names = list(request.mcpServers.keys())

    current_config = get_mcp_config()
    if current_config is not None and current_config == request:
        logger.info(
            f"MCP gateway config unchanged ({len(server_names)} server(s)), skipping swap"
        )
        return AppConfigResult(servers=server_names, duration_ms=0.0)

    logger.debug(f"Apps configuration request received: {len(server_names)} server(s)")
    for server_name in server_names:
        server_config = request.mcpServers[server_name]
        transport = server_config.transport
        logger.debug(f"  Server '{server_name}': transport={transport}")

    # Invalidate the cache *before* attempting the swap. swap_mcp_app
    # mutates the FastAPI mount (and rotates the lifespan manager)
    # before running the readiness check, so any post-mutation failure
    # — MCPReadinessError, or any exception from inside swap_mcp_app's
    # generic except block — leaves the gateway in a partial state
    # where the cached config no longer matches what is actually
    # mounted. Without this invalidation, a subsequent /apps request
    # matching the *prior* cached config would short-circuit and
    # report success even though the mounted gateway is in a broken
    # state. Worst case if swap_mcp_app raises *before* the mount
    # mutation (e.g. a ValueError from _build_mcp_app_with_proxy on
    # invalid config), this just costs one redundant full swap on the
    # next request — harmless.
    set_mcp_config(None)
    _mcp_coverage_cache.clear()

    start = time.perf_counter()
    try:
        mcp_proxy = await swap_mcp_app(request, app)
        await get_coordinator().start(
            mcp_proxy=mcp_proxy,
            config=request.coordinator_config or CoordinatorConfig(enabled=False),
        )
        duration_ms = (time.perf_counter() - start) * 1000

        set_mcp_config(request)

        logger.info(
            f"Configured MCP gateway with {len(server_names)} server(s): {', '.join(server_names)}"
        )

        return AppConfigResult(servers=server_names, duration_ms=duration_ms)
    except MCPReadinessError as e:
        logger.error(f"MCP servers not ready: {e.message}")
        error_detail = {
            "error": e.message,
            "failed_servers": list(e.failed_servers.keys()),
            "details": {
                name: details.model_dump() for name, details in e.failed_servers.items()
            },
        }
        raise HTTPException(status_code=503, detail=error_detail) from e
    except ValueError as e:
        logger.error(f"Invalid MCP configuration: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        logger.error(f"Failed to swap MCP gateway: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error setting MCP servers: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
