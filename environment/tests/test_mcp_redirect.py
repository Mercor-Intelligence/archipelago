"""``NormalizeMcpPathMiddleware`` — bare ``/mcp`` served without a 307.

The gateway mounts the MCP app at ``/mcp`` (Starlette ``Mount``), which only
matches ``/mcp/...``; a bare ``/mcp`` would otherwise 307-redirect to ``/mcp/``
and break streamable-HTTP clients that don't follow the redirect mid-handshake.
These tests run the ASGI app in-process via ``httpx.ASGITransport`` (no sockets).
"""

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from runner.middleware import NormalizeMcpPathMiddleware

_METHODS = ("GET", "POST", "DELETE")


def _mounted_mcp_app() -> Starlette:
    """Stand-in for FastMCP's ``http_app(path="/")`` mounted at ``/mcp``: its
    route sits at the mount root, so it's reachable at ``/mcp/``."""

    async def root(_request: object) -> PlainTextResponse:
        return PlainTextResponse("mcp-ok")

    return Starlette(routes=[Route("/", root, methods=list(_METHODS))])


def _build_app(*, with_middleware: bool) -> FastAPI:
    app = FastAPI()
    if with_middleware:
        app.add_middleware(NormalizeMcpPathMiddleware)
    app.mount("/mcp", _mounted_mcp_app())
    return app


@pytest.mark.asyncio
async def test_bare_mcp_served_without_redirect() -> None:
    app = _build_app(with_middleware=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gw", follow_redirects=False
    ) as client:
        for method in _METHODS:
            resp = await client.request(method, "/mcp")
            assert resp.status_code == 200, (method, resp.status_code, resp.text)
            assert resp.text == "mcp-ok"
        # The trailing-slash form keeps working unchanged.
        resp = await client.get("/mcp/")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_other_paths_unaffected() -> None:
    app = _build_app(with_middleware=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gw", follow_redirects=False
    ) as client:
        # A different mount-prefixed path is untouched by the rewrite.
        resp = await client.get("/mcpx")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_without_middleware_bare_mcp_redirects() -> None:
    # Control: confirms the 307 the middleware exists to eliminate.
    app = _build_app(with_middleware=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gw", follow_redirects=False
    ) as client:
        resp = await client.post("/mcp")
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/mcp/")
