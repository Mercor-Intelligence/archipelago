"""MCP protocol client for testing MCP servers directly.

This module provides a client to invoke MCP tools via the MCP protocol,
enabling testing of MCP servers without requiring a live REST API.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class MCPClient:
    """Client for testing MCP servers via stdio protocol.

    Note: This client currently does not handle authentication flows.
    For servers requiring authentication (e.g., login tool), you must:
    1. Handle authentication externally before testing
    2. Use MCPValidator with pre-authenticated fixtures
    3. Or wait for auth support in a future release

    TODO: Add authentication support:
        - login() method to call authentication tools
        - Token storage and automatic passing to protected tools
        - Support for various auth patterns (Bearer, OAuth, etc.)

    Example:
        ```python
        # Test an MCP server directly (non-authenticated or pre-authenticated)
        client = MCPClient(server_script="mcp_servers/factset/main.py")

        result = await client.call_tool(
            "get_prices",
            {
                "ids": ["TSLA-US"],
                "startDate": "2025-01-01",
                "endDate": "2025-01-31"
            }
        )

        print(result)  # Returns tool output
        ```
    """

    def __init__(
        self,
        server_script: str | Path,
        python_executable: str | None = None,
    ):
        """Initialize MCP client.

        Args:
            server_script: Path to MCP server main.py
            python_executable: Python executable to use (defaults to sys.executable)
        """
        self.server_script = Path(server_script)
        self.python_executable = python_executable or sys.executable

        if not self.server_script.exists():
            raise FileNotFoundError(f"Server script not found: {server_script}")

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = 30.0,
    ) -> Any:
        """Call an MCP tool and return the result.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments as a dictionary
            timeout: Timeout in seconds

        Returns:
            Tool response data

        Raises:
            TimeoutError: If tool execution exceeds timeout
            RuntimeError: If tool execution fails

        Example:
            ```python
            result = await client.call_tool(
                "get_prices",
                {"ids": ["TSLA-US"], "startDate": "2025-01-01"}
            )
            ```
        """
        # Build MCP tool call request (simplified - would need full MCP protocol)
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        process = None
        try:
            # Start MCP server process
            process = await asyncio.create_subprocess_exec(
                self.python_executable,
                str(self.server_script),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.server_script.parent,
            )

            # Send request to server
            request_json = json.dumps(request) + "\n"
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request_json.encode()),
                timeout=timeout,
            )

            # Parse response
            # Note: stderr may contain debug/info logs, not just errors
            # We rely on JSON-RPC response structure for error detection
            response_text = stdout.decode().strip()
            if not response_text:
                # Only check stderr if stdout is empty (likely a crash)
                if stderr:
                    error_msg = stderr.decode().strip()
                    if error_msg:
                        raise RuntimeError(f"Server crashed: {error_msg}")
                raise RuntimeError("No response from server")

            # Parse JSON-RPC response (handle multiple lines)
            for line in response_text.split("\n"):
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                    if "result" in response:
                        return response["result"]
                    elif "error" in response:
                        raise RuntimeError(f"Tool error: {response['error']}")
                except json.JSONDecodeError:
                    continue

            raise RuntimeError(f"Invalid response format: {response_text}")

        except TimeoutError:
            if process:
                process.kill()
            raise TimeoutError(f"Tool '{tool_name}' timed out after {timeout}s")
        except RuntimeError:
            # Re-raise RuntimeErrors from tool execution without wrapping
            if process:
                process.kill()
            raise
        except Exception as e:
            # Wrap other exceptions with context
            if process:
                process.kill()
            raise RuntimeError(f"Failed to call tool '{tool_name}': {e}")

    async def list_tools(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        """List all available tools from the MCP server.

        Args:
            timeout: Timeout in seconds

        Returns:
            List of tool definitions with names and schemas

        Example:
            ```python
            tools = await client.list_tools()
            for tool in tools:
                print(f"Tool: {tool['name']}")
            ```
        """
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        }

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.python_executable,
                str(self.server_script),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.server_script.parent,
            )

            request_json = json.dumps(request) + "\n"
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request_json.encode()),
                timeout=timeout,
            )

            response_text = stdout.decode().strip()
            for line in response_text.split("\n"):
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                    if "result" in response and "tools" in response["result"]:
                        return response["result"]["tools"]
                except json.JSONDecodeError:
                    continue

            return []

        except TimeoutError:
            if process:
                process.kill()
            raise TimeoutError(f"list_tools timed out after {timeout}s")
        except Exception as e:
            if process:
                process.kill()
            raise RuntimeError(f"Failed to list tools: {e}")
