"""USPTO MCP Server - GUI Entrypoint.

Exposes individual tools for web UI and REST API consumption.
For LLM/Claude Desktop, use main.py instead.
"""

from __future__ import annotations

import argparse
import asyncio

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware, RetryMiddleware
from loguru import logger

from mcp_servers.uspto.config import configure
from mcp_servers.uspto.db import cleanup_db, current_db_path, init_db
from mcp_servers.uspto.middleware.logging import LoggingMiddleware
from mcp_servers.uspto.offline import db as offline_db
from mcp_servers.uspto.offline.db import init_db as init_offline_db
from mcp_servers.uspto.utils.logging import configure_logging

DEFAULT_LOG_LEVEL = "INFO"

# Create MCP instance at module level for import by UI generator and mcp_rest_bridge
mcp = FastMCP("uspto-patent-applications")

# Add middleware at module level
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())


def _register_workspace_tools() -> None:
    """Register workspace management tools."""
    register_workspace_tools(mcp)


def register_workspace_tools(mcp_instance) -> None:
    """Shared helper that registers workspace tools on any MCP instance."""
    from mcp_servers.uspto.tools.workspace import (
        uspto_workspaces_create,
        uspto_workspaces_get,
        uspto_workspaces_list,
    )

    mcp_instance.tool(uspto_workspaces_create)
    mcp_instance.tool(uspto_workspaces_get)
    mcp_instance.tool(uspto_workspaces_list)

    logger.info("Registered 3 workspace tools")


def _register_search_tools() -> None:
    """Register search tools."""
    register_search_tools(mcp)


def register_search_tools(mcp_instance) -> None:
    """Shared helper that registers search tools on any MCP instance."""
    from mcp_servers.uspto.tools.search import uspto_applications_search

    mcp_instance.tool(uspto_applications_search)

    logger.info("Registered 1 search tool")


def _register_patent_tools() -> None:
    """Register patent retrieval tools."""
    register_patent_tools(mcp)


def register_patent_tools(mcp_instance) -> None:
    """Shared helper that registers patent retrieval tools on any MCP instance."""
    from mcp_servers.uspto.tools.patent import uspto_patent_get

    mcp_instance.tool(uspto_patent_get)

    logger.info("Registered 1 patent retrieval tool")


def _register_query_tools() -> None:
    """Register saved query tools."""
    register_query_tools(mcp)


def register_query_tools(mcp_instance) -> None:
    """Shared helper that registers query tools on any MCP instance."""
    from mcp_servers.uspto.tools.queries import (
        uspto_queries_get,
        uspto_queries_run,
        uspto_queries_save,
    )

    mcp_instance.tool(uspto_queries_save)
    mcp_instance.tool(uspto_queries_get)
    mcp_instance.tool(uspto_queries_run)

    logger.info("Registered 3 query tools")


def _register_status_codes_tools() -> None:
    """Register status codes tools."""
    register_status_codes_tools(mcp)


def register_status_codes_tools(mcp_instance) -> None:
    """Shared helper that registers status codes tools on any MCP instance."""
    from mcp_servers.uspto.tools.status_codes import uspto_status_codes_list
    from mcp_servers.uspto.tools.status_normalize import uspto_status_normalize

    mcp_instance.tool(uspto_status_codes_list)
    mcp_instance.tool(uspto_status_normalize)

    logger.info("Registered 2 status tools")


def _register_document_tools() -> None:
    """Register document retrieval tools."""
    register_document_tools(mcp)


def register_document_tools(mcp_instance) -> None:
    """Shared helper that registers document tools on any MCP instance."""
    from mcp_servers.uspto.tools.documents import (
        uspto_documents_get_download_url,
        uspto_documents_list,
    )

    mcp_instance.tool(uspto_documents_list)
    mcp_instance.tool(uspto_documents_get_download_url)

    logger.info("Registered 2 document tools")


def _register_pdf_tools() -> None:
    """Register patent PDF generation tools."""
    register_pdf_tools(mcp)


def register_pdf_tools(mcp_instance) -> None:
    """Shared helper that registers PDF generation tools on any MCP instance."""
    from mcp_servers.uspto.tools.generate_pdf import uspto_patent_pdf_generate

    mcp_instance.tool(uspto_patent_pdf_generate)

    logger.info("Registered 1 PDF tool")


def _register_snapshot_tools() -> None:
    """Register application snapshot tools."""
    register_snapshot_tools(mcp)


def register_snapshot_tools(mcp_instance) -> None:
    """Shared helper that registers snapshot tools on any MCP instance."""
    from mcp_servers.uspto.tools.snapshots import (
        uspto_snapshots_create,
        uspto_snapshots_get,
        uspto_snapshots_list,
    )

    mcp_instance.tool(uspto_snapshots_create)
    mcp_instance.tool(uspto_snapshots_get)
    mcp_instance.tool(uspto_snapshots_list)

    logger.info("Registered 3 snapshot tools")


def _register_foreign_priority_tools() -> None:
    """Register foreign priority tools."""
    register_foreign_priority_tools(mcp)


def register_foreign_priority_tools(mcp_instance) -> None:
    """Shared helper that registers foreign priority tools on any MCP instance."""
    from mcp_servers.uspto.tools.foreign_priority import uspto_foreign_priority_get

    mcp_instance.tool(uspto_foreign_priority_get)

    logger.info("Registered 1 foreign priority tool")


def _register_bundle_tools() -> None:
    """Register bundle export tools."""
    register_bundle_tools(mcp)


def register_bundle_tools(mcp_instance) -> None:
    """Shared helper that registers bundle export tools on any MCP instance."""
    from mcp_servers.uspto.tools.bundles import uspto_bundles_export

    mcp_instance.tool(uspto_bundles_export)

    logger.info("Registered 1 bundle tool")


def _register_health_tools() -> None:
    """Register health check tools."""
    register_health_tools(mcp)


def register_health_tools(mcp_instance) -> None:
    """Shared helper that registers health check tools on any MCP instance."""
    from mcp_servers.uspto.tools.health_check import uspto_health_check

    mcp_instance.tool(uspto_health_check)

    logger.info("Registered 1 health check tool")


def _register_audit_tools() -> None:
    """Register audit history tools."""
    register_audit_tools(mcp)


def register_audit_tools(mcp_instance) -> None:
    """Shared helper that registers audit tools on any MCP instance."""
    from mcp_servers.uspto.tools.audit import uspto_audit_workspace_history

    mcp_instance.tool(uspto_audit_workspace_history)

    logger.info("Registered 1 audit tool")


# Register tools at module level (runs on import)
_register_workspace_tools()
_register_search_tools()
_register_patent_tools()
_register_query_tools()
_register_status_codes_tools()
_register_document_tools()
_register_pdf_tools()
_register_snapshot_tools()
_register_foreign_priority_tools()
_register_bundle_tools()
_register_health_tools()
_register_audit_tools()


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
        db_path=current_db_path(),
    ).info("Starting USPTO MCP server")

    try:
        mcp.run()
    finally:
        asyncio.run(cleanup_db())
        if not current_settings.online_mode:
            asyncio.run(offline_db.cleanup_db())


if __name__ == "__main__":
    main()
