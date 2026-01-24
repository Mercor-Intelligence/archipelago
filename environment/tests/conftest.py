"""Pytest configuration and fixtures for environment tests.

These tests use testcontainers to spin up the environment Docker container,
providing isolation and automatic cleanup.

Run with: uv run pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage

# Directory paths
TESTS_DIR = Path(__file__).parent
ENVIRONMENT_DIR = TESTS_DIR.parent


def _build_environment_image() -> DockerImage:
    """Build the environment Docker image using testcontainers."""
    image = DockerImage(
        path=str(ENVIRONMENT_DIR),
        tag="archipelago-environment:test",
    )
    image.build()
    return image


async def _wait_for_health(base_url: str, timeout: int = 120) -> bool:
    """Wait for environment to be healthy."""
    async with httpx.AsyncClient() as client:
        start = time.time()
        while time.time() - start < timeout:
            try:
                response = await client.get(f"{base_url}/health", timeout=5)
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False


@pytest.fixture(scope="session")
def environment_image() -> DockerImage:
    """Build the environment Docker image once per session."""
    return _build_environment_image()


@pytest.fixture(scope="session")
def environment_container(environment_image: DockerImage) -> Generator[DockerContainer]:
    """Start the archipelago environment container using testcontainers.

    Session-scoped so the container is reused across all tests.
    Uses context manager pattern for automatic cleanup.
    """
    with (
        DockerContainer(image=str(environment_image.tag))
        .with_exposed_ports(8080)
        .with_env("ENV", "local")
        .with_env("S3_ACCESS_KEY_ID", "test")
        .with_env("S3_SECRET_ACCESS_KEY", "test")
        .with_env("S3_REGION", "us-west-2")
        .with_env("S3_SNAPSHOTS_BUCKET", "test")
        .with_env("RLS_GITHUB_READ_TOKEN", os.environ.get("RLS_GITHUB_READ_TOKEN", ""))
    ) as container:
        # Get base URL from container
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8080)
        base_url = f"http://{host}:{port}"

        # Wait for health
        loop = asyncio.new_event_loop()
        try:
            healthy = loop.run_until_complete(_wait_for_health(base_url))
            if not healthy:
                logs = container.get_logs()
                pytest.fail(f"Environment failed to start:\n{logs}")
        finally:
            loop.close()

        yield container


@pytest.fixture(scope="session")
def base_url(environment_container: DockerContainer) -> str:
    """Get the base URL for the environment container."""
    host = environment_container.get_container_host_ip()
    port = environment_container.get_exposed_port(8080)
    return f"http://{host}:{port}"
