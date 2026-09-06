"""Tests for environment health endpoint."""

import httpx
import pytest


class TestEnvironmentHealth:
    """Basic health check tests."""

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_200(self, base_url: str) -> None:
        """Test that /health endpoint returns 200 OK."""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/health", timeout=10)

        assert response.status_code == 200
