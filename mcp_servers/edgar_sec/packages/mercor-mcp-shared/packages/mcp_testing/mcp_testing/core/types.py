"""Type definitions for the MCP testing framework."""

from typing import Any, Protocol

# Type aliases (Python 3.12+)
type JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type RequestData = dict[str, Any]
type ResponseData = dict[str, Any]


class MCPTool(Protocol):
    """Protocol for MCP tool functions."""

    async def __call__(self, request: Any) -> Any:
        """Execute the MCP tool with a request."""
        ...


class HTTPClient(Protocol):
    """Protocol for HTTP clients."""

    async def request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, JSONValue]:
        """Make an HTTP request."""
        ...
