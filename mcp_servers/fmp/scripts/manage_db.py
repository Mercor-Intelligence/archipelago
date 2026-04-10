#!/usr/bin/env python3
"""Database management CLI for FMP MCP Server.

Usage:
    # Truncate all tables (keep schema, remove data)
    uv run python -m scripts.manage_db --clear

    # Delete DB file entirely
    uv run python -m scripts.manage_db --delete

    # Show DB stats (rows per table, size, last updated)
    uv run python -m scripts.manage_db --status
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text

# Add the parent directory to the path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_servers.fmp_server.config import get_config
from mcp_servers.fmp_server.db import (
    Company,
    FinancialStatement,
    GenericApiResponse,
    HistoricalPrice,
    StockQuote,
    TechnicalIndicator,
    close_engine,
    get_session,
)

# All model classes for iteration
ALL_MODELS = [
    Company,
    StockQuote,
    HistoricalPrice,
    FinancialStatement,
    TechnicalIndicator,
    GenericApiResponse,
]


def get_db_path() -> Path:
    """Extract database file path from config URL."""
    config = get_config()
    db_url = config.fmp_database_url

    # Extract path from URL like 'sqlite+aiosqlite:///./data/fmp.db'
    if ":///" in db_url:
        path_str = db_url.split("///")[-1]
        return Path(path_str)
    return Path("./data/fmp.db")


async def clear_tables() -> None:
    """Truncate all tables, keeping schema intact."""
    db_path = get_db_path()

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        print("Suggestion: Run 'uv run python -m scripts.download_fixtures --use-bundled' first")
        sys.exit(1)

    print(f"Clearing all tables in {db_path}...")

    try:
        async with get_session() as session:
            # Delete all rows from each table
            for model in ALL_MODELS:
                result = await session.execute(select(func.count()).select_from(model))
                count = result.scalar() or 0
                await session.execute(text(f"DELETE FROM {model.__tablename__}"))
                print(f"  Cleared {model.__tablename__}: {count} rows deleted")

        print("\nAll tables cleared successfully.")
    except Exception as e:
        print(f"Error clearing tables: {e}")
        sys.exit(1)
    finally:
        await close_engine()


async def delete_database() -> None:
    """Delete the database file entirely."""
    db_path = get_db_path()

    if not db_path.exists():
        print(f"Warning: Database file not found at {db_path} (nothing to delete)")
        return

    # Close any open connections first
    await close_engine()

    try:
        db_path.unlink()
        print(f"Deleted database file: {db_path}")
    except Exception as e:
        print(f"Error deleting database: {e}")
        sys.exit(1)


async def show_status() -> None:
    """Show database statistics."""
    db_path = get_db_path()

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        print("Suggestion: Run 'uv run python -m scripts.download_fixtures --use-bundled' first")
        sys.exit(1)

    # Get file stats
    file_size = db_path.stat().st_size
    modified_time = datetime.fromtimestamp(db_path.stat().st_mtime)

    print(f"Database: {db_path}")
    print(f"File size: {file_size / 1024:.1f} KB ({file_size:,} bytes)")
    print(f"Last modified: {modified_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("Table Statistics:")
    print("-" * 50)

    try:
        total_rows = 0
        async with get_session() as session:
            for model in ALL_MODELS:
                result = await session.execute(select(func.count()).select_from(model))
                count = result.scalar() or 0
                total_rows += count
                print(f"  {model.__tablename__:<30} {count:>10,} rows")

        print("-" * 50)
        print(f"  {'Total':<30} {total_rows:>10,} rows")

        # Show sample symbols if companies exist
        async with get_session() as session:
            result = await session.execute(select(Company.symbol).limit(10))
            symbols = [row[0] for row in result.fetchall()]

            if symbols:
                print()
                print(f"Sample symbols: {', '.join(symbols)}")
                if len(symbols) == 10:
                    print("  (showing first 10)")

    except Exception as e:
        print(f"Error reading database: {e}")
        sys.exit(1)
    finally:
        await close_engine()


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Database management CLI for FMP MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python -m scripts.manage_db --status    # Show database stats
  uv run python -m scripts.manage_db --clear     # Truncate all tables
  uv run python -m scripts.manage_db --delete    # Delete database file
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--clear",
        action="store_true",
        help="Truncate all tables (keep schema, remove data)",
    )
    group.add_argument(
        "--delete",
        action="store_true",
        help="Delete the database file entirely",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Show database statistics (rows per table, size, last updated)",
    )

    args = parser.parse_args()

    if args.clear:
        asyncio.run(clear_tables())
    elif args.delete:
        asyncio.run(delete_database())
    elif args.status:
        asyncio.run(show_status())


if __name__ == "__main__":
    main()
