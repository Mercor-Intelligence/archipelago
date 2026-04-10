"""USPTO MCP Server - LLM Entrypoint.

Exposes tools for LLM consumption via Claude Desktop or other AI clients.
For GUI/REST API, use ui.py instead.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware, RetryMiddleware
from loguru import logger

from mcp_servers.uspto.config import configure
from mcp_servers.uspto.db import cleanup_db, current_db_path, init_db
from mcp_servers.uspto.middleware.logging import LoggingMiddleware
from mcp_servers.uspto.middleware.validation_error_sanitizer import (
    ValidationErrorSanitizerMiddleware,
)
from mcp_servers.uspto.offline import db as offline_db
from mcp_servers.uspto.offline.db import init_db as init_offline_db
from mcp_servers.uspto.utils.logging import configure_logging

DEFAULT_LOG_LEVEL = "INFO"

# Create MCP instance at module level for import by UI generator and mcp_rest_bridge
mcp = FastMCP("uspto-patent-applications")

# Set up error injection middleware for Dynamic Friction testing
try:
    from mcp_middleware.injected_errors import setup_error_injection

    setup_error_injection(mcp)
except Exception:
    pass

# Add middleware at module level
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=False))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())


def _register_tools() -> None:
    """Register all tools for the LLM entrypoint."""
    from mcp_servers.uspto.ui import (
        register_audit_tools,
        register_bundle_tools,
        register_document_tools,
        register_foreign_priority_tools,
        register_health_tools,
        register_patent_tools,
        register_pdf_tools,
        register_query_tools,
        register_search_tools,
        register_snapshot_tools,
        register_status_codes_tools,
        register_workspace_tools,
    )

    register_workspace_tools(mcp)
    register_search_tools(mcp)
    register_patent_tools(mcp)  # Full text retrieval - critical for substantive analysis
    register_query_tools(mcp)
    register_status_codes_tools(mcp)
    register_document_tools(mcp)
    register_pdf_tools(mcp)
    register_snapshot_tools(mcp)
    register_foreign_priority_tools(mcp)
    register_bundle_tools(mcp)
    register_health_tools(mcp)
    register_audit_tools(mcp)


# Register tools at module level (runs on import)
_register_tools()


def parse_cli_args() -> argparse.Namespace:
    """Parse CLI arguments for the USPTO MCP server."""

    parser = argparse.ArgumentParser(description="USPTO patent applications MCP server")
    parser.add_argument(
        "--online",
        action="store_true",
        help="Enable live USPTO API calls (default offline/reactive)",
    )
    parser.add_argument(
        "--db-path",
        default=":memory:",
        help=(
            "SQLite database path. Use ':memory:' (default) for in-memory mode, "
            "'temp' to create a temporary file deleted at session end, or provide "
            "an explicit file path."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="USPTO API key (passthrough only, never stored or logged)",
    )
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help="Log verbosity level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Output structured JSON logs for production",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint for running the USPTO MCP server."""

    args = parse_cli_args()
    configure_logging(log_level=args.log_level, json_output=args.json_output)

    # Only override settings when CLI explicitly provides values
    # (preserve env var config when CLI uses defaults)
    if args.api_key is not None:
        configure(api_key=args.api_key)
    if args.online:
        configure(online_mode=True)

    from mcp_servers.uspto.config import get_settings

    current_settings = get_settings()

    # Log startup configuration
    logger.info("=" * 70)
    logger.info("USPTO MCP Server Startup Configuration")
    logger.info("=" * 70)
    logger.info(f"Mode: {'ONLINE' if current_settings.online_mode else 'OFFLINE'}")
    logger.info(f"Session DB Path (workspaces/queries): {args.db_path}")

    # Initialize session database (workspace, queries, snapshots)
    asyncio.run(init_db(args.db_path))
    logger.info(f"Session DB initialized: {current_db_path()}")

    # Initialize offline patents database if in offline mode
    # This runs after CLI args are parsed, so online_mode is correctly set
    if not current_settings.online_mode:
        logger.info(f"Offline Patents DB Path (config): {current_settings.offline_db}")
        asyncio.run(init_offline_db())
        logger.info(f"Offline Patents DB initialized: {offline_db.current_db_path()}")
    else:
        logger.info("Offline Patents DB: Not initialized (online mode)")

    logger.info("=" * 70)

    logger.bind(
        online=current_settings.online_mode,
        session_db=current_db_path(),
        offline_db=offline_db.current_db_path() if not current_settings.online_mode else None,
    ).info("Starting USPTO MCP server")

    try:
        transport = os.getenv("MCP_TRANSPORT", "http").lower()
        if transport == "http":
            port = int(os.getenv("MCP_PORT", "5000"))
            mcp.run(transport="http", host="0.0.0.0", port=port)
        else:
            mcp.run(transport="stdio")
    finally:
        asyncio.run(cleanup_db())
        if not current_settings.online_mode:
            asyncio.run(offline_db.cleanup_db())


if __name__ == "__main__":
    main()
