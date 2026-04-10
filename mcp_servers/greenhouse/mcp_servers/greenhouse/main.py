"""Greenhouse Recruiting MCP Server - Dual Registration Pattern.

Mock MCP server simulating Greenhouse Recruiting's applicant tracking system (ATS)
for AI agent training. Provides tools for managing candidates, applications, jobs,
feedback, and activity feeds.

Exposes both:
1. Meta-tools (9) - for LLM agents (consolidated, ~80% token reduction)
2. Individual tools (26) - for UI (clean forms)

Features:
- Empty database by default (user creates all data via MCP tools)
- Mock persona-based authentication with 4 personas
- Response schemas match Greenhouse Harvest API
- SQLite persistence across sessions
"""

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from db.session import init_db
from fastmcp import FastMCP
from mcp_auth import public_tool
from mcp_middleware import LoggingConfigurator, ServerConfig, apply_configurations, run_server
from mcp_middleware.db_tools import create_database_tools

# Server configuration
SERVER_CONFIG = ServerConfig(
    name="greenhouse-mcp",
    version="0.1.0",
    description="Mock MCP server simulating Greenhouse Recruiting's ATS for AI agent training",
    features={
        "personas": ["recruiter", "coordinator", "hiring_manager", "hr_analyst"],
        "persistence": "sqlite",
        "api_compatibility": "Greenhouse Harvest API",
    },
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Initialize database on server startup."""
    await init_db()
    yield {}


# Initialize FastMCP server
mcp = FastMCP(
    name=SERVER_CONFIG.name,
    instructions=SERVER_CONFIG.description,
    version=SERVER_CONFIG.version,
    lifespan=lifespan,
)


def _register_tools():
    """Register all tools with the MCP server.

    Called from main() to ensure tools are registered before auth setup.
    Supports dual registration pattern:
    - GUI_ENABLED=false: Register meta-tools (for LLMs, fewer tokens)
    - GUI_ENABLED not set or true: Register individual tools (for UI/humans) [default]
    """
    gui_enabled = os.getenv("GUI_ENABLED", "").lower()
    use_gui_tools = gui_enabled not in ("false", "0", "no")

    if use_gui_tools:
        from tools import (
            register_activity_tools,
            register_admin_tools,
            register_application_tools,
            register_candidate_tools,
            register_feedback_tools,
            register_job_tools,
            register_jobboard_tools,
            register_lookup_tools,
            register_user_tools,
        )

        # Register individual tools for UI mode (human operators)
        register_activity_tools(mcp)
        register_admin_tools(mcp)
        register_candidate_tools(mcp)
        register_application_tools(mcp)
        register_job_tools(mcp)
        register_feedback_tools(mcp)
        register_jobboard_tools(mcp)
        register_lookup_tools(mcp)
        register_user_tools(mcp)

        # Database Management Tools
        create_database_tools(mcp, "db.session", public_tool, server_name="greenhouse")
    else:
        from tools._meta_tools import register_meta_tools

        # Register consolidated meta-tools for LLM mode
        register_meta_tools(mcp)


def main():
    """Entry point for the Greenhouse MCP server when run directly.

    This function is called when the script is run as `python main.py` or via
    the greenhouse-mcp CLI command.
    """
    # Register all tools before auth setup
    _register_tools()

    # Configure logging using LoggingConfigurator
    # - Auto-discovery (REST bridge, fastmcp run): Uses environment variable defaults
    # - Direct execution (python main.py): Can use CLI args that override env vars
    parser = argparse.ArgumentParser(
        description="Greenhouse Recruiting MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    configurators = [LoggingConfigurator()]
    args, remaining = apply_configurations(parser, mcp, configurators)

    # Run the server (handles server_info and auth setup)
    run_server(mcp, config=SERVER_CONFIG, remaining_args=remaining)


if __name__ == "__main__":
    main()
