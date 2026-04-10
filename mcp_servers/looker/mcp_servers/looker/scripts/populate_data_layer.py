#!/usr/bin/env python
"""
Populate the data layer for the Looker MCP server.

This script handles the file-based initialization:
1. Copies bundled DuckDB to STATE_LOCATION (if not already there)
2. Loads any user-uploaded CSVs from STATE_LOCATION into DuckDB

The in-memory data structures (models, explores) are still built at server
startup since they need to exist in the server's process memory.

Run this via mise: `mise run populate`
"""

import sys
from pathlib import Path

from loguru import logger

# Add parent directory to path so we can import from data_layer
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_layer import (
    _ensure_runtime_duckdb,
    _populate_duckdb,
    get_user_csv_dir,
)


def populate_data_layer():
    """
    Populate the runtime DuckDB with bundled and user data.

    This handles the file-based initialization:
    1. Copies bundled DuckDB to STATE_LOCATION (if not already there)
    2. Loads any user-uploaded CSVs from STATE_LOCATION into DuckDB

    The in-memory structures (LookML models/explores) are built when
    the server starts, since they need to exist in the server's process.
    """
    logger.info("Populating Looker data layer...")

    # Step 1: Ensure runtime DuckDB exists (copy from bundled if needed)
    try:
        runtime_path = _ensure_runtime_duckdb()
        logger.info(f"Runtime DuckDB ready at: {runtime_path}")
    except Exception as e:
        logger.error(f"Failed to ensure runtime DuckDB: {e}")
        raise

    # Step 2: Load user CSVs into DuckDB (if any exist in STATE_LOCATION)
    user_csv_dir = get_user_csv_dir()
    user_csvs = list(user_csv_dir.rglob("*.csv"))
    if user_csvs:
        logger.info(f"Found {len(user_csvs)} user CSV(s) in {user_csv_dir}")
        try:
            tables_added = _populate_duckdb(user_csv_dir)
            logger.info(f"Added {tables_added} user table(s) to DuckDB")
        except Exception as e:
            logger.error(f"Failed to load user CSVs: {e}")
            raise

    else:
        logger.info("No user CSVs found")

    logger.info("Data layer population complete")


if __name__ == "__main__":
    populate_data_layer()
