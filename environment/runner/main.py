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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger

from .coordinator.runtime import get_coordinator
from .data import router as data_router
from .gateway.gateway import shutdown_stateful_proxy
from .gateway.router import close_proxy_client
from .gateway.router import router as gateway_router
from .gateway.state import get_mcp_lifespan_manager
from .middleware import NormalizeMcpPathMiddleware
from .utils.logging import setup_logger, teardown_logger
from .utils.signing import signing_enabled, verify_request_signature


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

    # Validate the signing key early so a misconfigured API_SIGNING_PUBLIC_KEY
    # fails at startup (clear log message) rather than on every request (raw 500).
    try:
        signing_enabled()
    except RuntimeError as e:
        logger.error("Invalid API_SIGNING_PUBLIC_KEY — aborting startup: {}", e)
        raise

    # Noticeably get_coordinator().start() isn't called here because
    # the coordinator is started by the /apps endpoint which runs
    # the MCP swap.

    yield

    logger.info("Shutting down environment gateway server")
    await get_coordinator().stop()
    await close_proxy_client()
    # Disconnect the session-affine backend client (owner task + browser) if one
    # is active; exiting the MCP app lifespan below does not tear it down.
    await shutdown_stateful_proxy()

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

# Serve a bare ``/mcp`` directly (200) instead of 307-redirecting to ``/mcp/``;
# some MCP streamable-HTTP clients drop the streaming connection on the redirect.
app.add_middleware(NormalizeMcpPathMiddleware)


# Paths that do not require a signature even when API_SIGNING_PUBLIC_KEY is set.
# Health probes originate from the container orchestrator, not the studio server.
_SIGNING_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/"})
# Path prefixes that are also exempt: MCP and REST gateway endpoints are
# accessed by agent MCP clients that cannot add signing headers. They are
# still protected by the Modal sandbox Bearer token enforced by the gateway.
_SIGNING_EXEMPT_PREFIXES: tuple[str, ...] = ("/mcp", "/rest")


def _is_signing_exempt(path: str) -> bool:
    return path in _SIGNING_EXEMPT_PATHS or path.startswith(_SIGNING_EXEMPT_PREFIXES)


@app.middleware("http")
async def verify_signature_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Reject requests that lack a valid studio signature when signing is enabled."""
    if signing_enabled() and not _is_signing_exempt(request.url.path):
        ts = request.headers.get("X-Studio-Timestamp", "")
        nonce = request.headers.get("X-Studio-Nonce", "")
        sig = request.headers.get("X-Studio-Signature", "")

        # Include query string in the path so it matches the signed payload built
        # by sign_env_runner_request, which appends "?query" when one is present.
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"

        body = await request.body() or b""

        is_valid = verify_request_signature(
            method=request.method,
            path=path,
            body=body,
            timestamp_str=ts,
            nonce=nonce,
            signature_b64=sig,
        )

        if not is_valid:
            # Return directly — exceptions raised in @app.middleware("http") bypass
            # @app.exception_handler and produce a 500 via ServerErrorMiddleware.
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"Request signature verification failed for {request.method} {request.url.path}"
                },
            )

    return await call_next(request)


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
