"""Startup validation for FMP MCP Server.

This module provides validation for offline mode:
- Checks that the database file exists
- If missing, creates an empty database with schema
"""

import asyncio
from pathlib import Path

from loguru import logger

# Support all execution methods:
# 1. python main.py (direct execution from server dir)
# 2. python -m mcp_servers.fmp_server (module execution)
try:
    from mcp_servers.fmp_server.config import Mode, get_config
except ImportError:
    # Direct execution - parent already added to path by main.py
    from config import Mode, get_config


def get_db_path_from_url(db_url: str) -> Path:
    """Extract database file path from SQLAlchemy URL."""
    if ":///" in db_url:
        path_str = db_url.split("///")[-1]
        return Path(path_str)
    return Path("./data/fmp.db")


async def _init_database() -> None:
    """Initialize database schema.

    Creates the database file and schema if they don't exist.
    """
    config = get_config()
    db_path = get_db_path_from_url(config.fmp_database_url)

    try:
        from mcp_servers.fmp_server.db import close_engine, init_db
    except ImportError:
        from db import close_engine, init_db

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize schema (creates db if not exists, idempotent)
    await init_db()
    logger.info(f"Database schema ready at {db_path}")

    await close_engine()


def startup_validation() -> None:
    """Perform startup validation based on current mode."""
    config = get_config()

    logger.info(f"FMP Server starting in {config.mode.value} mode")

    if config.mode == Mode.OFFLINE:
        # Initialize database schema
        asyncio.run(_init_database())
