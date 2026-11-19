"""The gateway presents the per-boot MCP gateway token to every upstream app."""

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from runner.gateway import gateway as gw
from runner.gateway import upstream_token
from runner.gateway.models import MCPSchema, MCPServerConfig
from runner.gateway.upstream_token import (
    MCP_GATEWAY_TOKEN_ENV,
    MCP_GATEWAY_TOKEN_HEADER,
    mcp_gateway_token,
    with_gateway_token,
)

TOKEN = "a" * 64


@pytest.fixture
def no_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(MCP_GATEWAY_TOKEN_ENV, raising=False)
    monkeypatch.setattr(upstream_token, "MCP_GATEWAY_TOKEN_FILE", tmp_path / "absent")


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch, no_token: None) -> str:
    monkeypatch.setenv(MCP_GATEWAY_TOKEN_ENV, TOKEN)
    return TOKEN


def _schema(**servers: dict[str, Any]) -> MCPSchema:
    return MCPSchema(mcpServers={n: MCPServerConfig(**s) for n, s in servers.items()})


class TestTokenSource:
    def test_absent_is_none(self, no_token: None) -> None:
        assert mcp_gateway_token() is None

    def test_env_wins(self, token: str) -> None:
        assert mcp_gateway_token() == token

    def test_root_only_file(
        self, monkeypatch: pytest.MonkeyPatch, no_token: None, tmp_path: Path
    ) -> None:
        path = tmp_path / "token"
        path.write_text(f"{TOKEN}\n")
        monkeypatch.setattr(upstream_token, "MCP_GATEWAY_TOKEN_FILE", path)
        assert mcp_gateway_token() == TOKEN

    def test_empty_file_is_none(
        self, monkeypatch: pytest.MonkeyPatch, no_token: None, tmp_path: Path
    ) -> None:
        path = tmp_path / "token"
        path.write_text("\n")
        monkeypatch.setattr(upstream_token, "MCP_GATEWAY_TOKEN_FILE", path)
        assert mcp_gateway_token() is None


class TestWithGatewayToken:
    def test_no_token_returns_headers_untouched(self) -> None:
        headers = {"Authorization": "Bearer x"}
        assert with_gateway_token(headers, None) is headers
        assert with_gateway_token(None, None) is None

    def test_adds_header_and_preserves_existing(self) -> None:
        headers = {"Authorization": "Bearer x"}
        merged = with_gateway_token(headers, TOKEN)
        assert merged == {"Authorization": "Bearer x", MCP_GATEWAY_TOKEN_HEADER: TOKEN}
        assert headers == {"Authorization": "Bearer x"}

    def test_minted_token_replaces_same_name_header_case_insensitively(self) -> None:
        merged = with_gateway_token({"X-Mcp-Gateway-Token": "configured"}, TOKEN)
        assert merged == {MCP_GATEWAY_TOKEN_HEADER: TOKEN}


class TestConfigInjection:
    def test_serving_config_carries_header(self, token: str) -> None:
        config = gw._serving_config_dict(
            _schema(
                app={
                    "transport": "http",
                    "url": "http://a/mcp/",
                    "headers": {"X-Other": "1"},
                },
                stdio={"transport": "stdio", "command": "true"},
            )
        )
        assert config is not None
        assert config["mcpServers"]["app"]["headers"] == {
            "X-Other": "1",
            MCP_GATEWAY_TOKEN_HEADER: token,
        }
        assert "headers" not in config["mcpServers"]["stdio"]

    def test_stored_config_is_not_mutated(self, token: str) -> None:
        schema = _schema(app={"transport": "http", "url": "http://a/mcp/"})
        _ = gw._serving_config_dict(schema)
        assert schema.mcpServers["app"].headers is None

    def test_probe_config_carries_header(self, token: str) -> None:
        body = gw._probe_client_config(
            "app", MCPServerConfig(transport="http", url="http://a/mcp/")
        )["mcpServers"]["app"]
        assert body["headers"] == {MCP_GATEWAY_TOKEN_HEADER: token}

    def test_absent_token_is_byte_identical(self, no_token: None) -> None:
        config = gw._serving_config_dict(
            _schema(app={"transport": "http", "url": "http://a/mcp/"})
        )
        assert config == {
            "mcpServers": {"app": {"transport": "http", "url": "http://a/mcp/"}}
        }
        body = gw._probe_client_config(
            "app", MCPServerConfig(transport="http", url="http://a/mcp/")
        )
        assert body == {
            "mcpServers": {"app": {"transport": "http", "url": "http://a/mcp/"}}
        }


class _RequireToken:
    """What an adopting app does: 401 any /mcp request without the token."""

    def __init__(self, app: ASGIApp, expected: str) -> None:
        self.app = app
        self.expected = expected

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            got = Request(scope).headers.get(MCP_GATEWAY_TOKEN_HEADER)
            if got != self.expected:
                response: Response = JSONResponse({"error": "unauthorized"}, 401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _hidden_tool() -> str:
    return "ok"


@contextlib.asynccontextmanager
async def _guarded_upstream() -> AsyncIterator[str]:
    app = FastMCP("upstream")
    _ = app.tool(name="hidden_tool")(_hidden_tool)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            _RequireToken(app.http_app(path="/mcp"), TOKEN),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def _list_via_gateway(url: str) -> list[str]:
    config = gw._serving_config_dict(_schema(app={"transport": "http", "url": url}))
    _, proxy = gw._build_mcp_app_with_proxy(config, None, None)
    async with FastMCPClient(proxy) as client:
        return [t.name for t in await client.list_tools()]


async def _direct_call(url: str) -> list[Any]:
    async with FastMCPClient(url) as client:
        return list(await client.list_tools())


@pytest.mark.usefixtures("token")
async def test_gateway_reaches_guarded_app_and_direct_call_is_refused() -> None:
    async with _guarded_upstream() as url:
        assert await asyncio.wait_for(_list_via_gateway(url), 30) == ["hidden_tool"]
        # The model's path: same loopback, no token.
        with pytest.raises(httpx.HTTPStatusError, match="401"):
            _ = await asyncio.wait_for(_direct_call(url), 30)
