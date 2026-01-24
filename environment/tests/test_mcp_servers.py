"""Tests for MCP server configuration.

Verifies MCP servers can be configured and provide tools.
Replicates the functionality from archipelago/tests/services/smoke_test.py

These tests require RLS_GITHUB_READ_TOKEN to clone MCP server repos.
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client as FastMCPClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _substitute_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Replace all SECRET/VAR_NAME patterns with environment variable values.

    Recursively walks the config dict and replaces string values that start
    with "SECRET/" with the corresponding environment variable value.
    """

    def resolve_value(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("SECRET/"):
            env_var_name = value[7:]  # Remove "SECRET/" prefix
            return os.environ.get(env_var_name, "")
        elif isinstance(value, dict):
            return {k: resolve_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [resolve_value(item) for item in value]
        return value

    return resolve_value(config)


async def _configure_mcp_servers(
    base_url: str,
    mcp_config: dict[str, Any],
    timeout: int = 300,
) -> None:
    """Configure MCP servers via /apps endpoint."""
    resolved_config = _substitute_secrets(mcp_config)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/apps",
            json=resolved_config,
            timeout=timeout,
        )
        assert response.status_code == 200, (
            f"Failed to configure MCP servers: {response.status_code} {response.text}"
        )


class TestFilesystemServer:
    """Tests for filesystem MCP server.

    This is a simpler test that only requires RLS_GITHUB_READ_TOKEN.
    """

    @pytest.fixture
    def filesystem_config(self) -> dict[str, Any]:
        """Load filesystem server configuration from fixtures."""
        fixtures_file = FIXTURES_DIR / "filesystem_server.json"
        with open(fixtures_file) as f:
            return json.load(f)

    @pytest.mark.asyncio
    async def test_filesystem_server_provides_tools(
        self,
        base_url: str,
        filesystem_config: dict[str, Any],
    ) -> None:
        """Test that filesystem server can be configured and provides tools."""
        # Configure the server
        await _configure_mcp_servers(base_url, filesystem_config)

        # Verify tools are available
        mcp_client = FastMCPClient(f"{base_url}/mcp/")
        async with mcp_client:
            tools_result = await mcp_client.session.list_tools()

        # Filesystem server should provide at least some tools
        assert len(tools_result.tools) > 0, "Filesystem server should provide tools"

        # Check for expected filesystem tools
        tool_names = [t.name for t in tools_result.tools]
        # At minimum, filesystem server should have read/write capabilities
        assert any(
            "read" in name.lower() or "list" in name.lower() for name in tool_names
        ), f"Expected filesystem tools, got: {tool_names}"


class TestMultipleServers:
    """Tests for configuring multiple MCP servers.

    Replicates: archipelago/tests/services/smoke_test.py

    Uses fixtures from archipelago/tests/services/fixtures/mcp_configs.json
    """

    @pytest.fixture
    def all_servers_config(self) -> dict[str, Any]:
        """Load full MCP server configuration from local fixtures."""
        fixtures_file = FIXTURES_DIR / "mcp_configs.json"
        if not fixtures_file.exists():
            pytest.skip(f"Fixtures file not found: {fixtures_file}")

        with open(fixtures_file) as f:
            return json.load(f)

    @pytest.mark.asyncio
    async def test_all_servers_provide_tools(
        self,
        base_url: str,
        all_servers_config: dict[str, Any],
    ) -> None:
        """Test that all MCP servers can be configured and provide tools.

        This test mirrors the services smoke test:
        1. Configure all MCP servers
        2. Verify tools are available via FastMCPClient
        """
        # Configure all servers
        await _configure_mcp_servers(base_url, all_servers_config, timeout=300)

        # Verify tools are available
        mcp_client = FastMCPClient(f"{base_url}/mcp/")
        async with mcp_client:
            tools_result = await mcp_client.session.list_tools()

        # Should have tools from multiple servers
        tool_count = len(tools_result.tools)
        assert tool_count > 0, "Should have tools from configured servers"

        # Log tools for debugging
        tool_names = [t.name for t in tools_result.tools]
        print(f"Total tools available: {tool_count}")
        print(f"Sample tools: {tool_names[:10]}")
