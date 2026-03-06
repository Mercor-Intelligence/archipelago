"""
FastAPI gateway server for an RL environment.

This server provides endpoints for managing a headless RL environment:

- /health - Health check endpoint to verify server readiness
- /data/populate - Load data from S3-compatible storage into subsystems
- /data/snapshot - Create snapshots of all subsystems and upload to S3
- /apps - Configure MCP servers (hot-swap MCP gateway)
- /mcp - MCP gateway endpoint for LLM agents (mounted dynamically)

The server is designed to run inside a Docker container with a timeout,
allowing external systems to manage the environment lifecycle.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from loguru import logger

from .data import router as data_router
from .gateway.router import router as gateway_router
from .gateway.state import get_mcp_lifespan_manager
from .utils.logging import setup_logger, teardown_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - initialize and cleanup resources.

    This context manager handles startup and shutdown logic for the FastAPI application.
    Manages MCP gateway lifespan cleanup on shutdown.

    Args:
        app: The FastAPI application instance
    """
    setup_logger()
    logger.info("Starting environment gateway server")

    yield

    logger.info("Shutting down environment gateway server")

    # Clean up MCP app lifespan if exists
    mcp_lm = get_mcp_lifespan_manager()
    if mcp_lm is not None:
        try:
            _ = await mcp_lm.__aexit__(None, None, None)
            logger.info("Cleaned up MCP gateway lifespan")
        except Exception as e:
            logger.error(f"Error cleaning up MCP gateway lifespan: {e}")

    await teardown_logger()


app = FastAPI(
    title="Archipelago Environment Gateway",
    description="Environment Gateway",
    lifespan=lifespan,
)

app.include_router(data_router, prefix="/data")
app.include_router(gateway_router)


@app.get("/health")
async def health() -> PlainTextResponse:
    """Health check endpoint.

    Returns a simple "OK" response to indicate the server is running and ready
    to accept requests. This endpoint can be used by container orchestration
    systems (e.g., Kubernetes, ECS) for health checks.

    Returns:
        PlainTextResponse with "OK" content and 200 status code
    """
    logger.debug("Health check requested")
    return PlainTextResponse(content="OK", status_code=200)


@app.get("/")
async def root() -> PlainTextResponse:
    return PlainTextResponse(content="Mercor Archipelago Environment", status_code=200)


if __name__ == "__main__":
    import uvicorn  # import-check-ignore

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
