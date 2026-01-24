"""Tests for MCP gateway functionality.

Verifies the MCP gateway can be configured and accessed via FastMCPClient.
Replicates the functionality from archipelago/tests/no_servers/smoke_test.py
"""

import httpx
import pytest
from fastmcp import Client as FastMCPClient


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
