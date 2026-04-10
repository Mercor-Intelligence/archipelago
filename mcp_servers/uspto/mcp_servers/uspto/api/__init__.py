"""USPTO API client helpers for the MCP server."""

from __future__ import annotations

from typing import Any

import httpx

from mcp_servers.uspto.api.client import RateLimiter, USPTOAPIClient
from mcp_servers.uspto.api.contracts import USPTOClient
from mcp_servers.uspto.api.factory import get_uspto_client

BASE_URL = "https://api.uspto.gov/patent/v1"


def build_uspto_headers(api_key: str | None) -> dict[str, str]:
    """Return headers for USPTO calls including the passthrough API key."""

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "USPTO-MCP-Server/0.1.0",
    }
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


async def fetch_from_uspto(path: str, api_key: str | None) -> dict[str, Any]:
    """Execute a GET request against the USPTO API with the passthrough key."""

    async with httpx.AsyncClient(base_url=BASE_URL, headers=build_uspto_headers(api_key)) as client:
        response = await client.get(path)
        response.raise_for_status()
        return response.json()


__all__ = [
    "build_uspto_headers",
    "fetch_from_uspto",
    "RateLimiter",
    "USPTOAPIClient",
    "USPTOClient",
    "get_uspto_client",
]
