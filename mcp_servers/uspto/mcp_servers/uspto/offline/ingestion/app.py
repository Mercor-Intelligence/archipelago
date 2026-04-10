"""USPTO Patent Data Ingestion Application Entry Point.

This file provides the run() function required by the data ingestion framework CLI.

Usage:
    # Ingest patent grants (default)
    uv run ingest --app-file mcp_servers/uspto/offline/ingestion/app.py --type grant

    # Ingest patent applications
    uv run ingest --app-file mcp_servers/uspto/offline/ingestion/app.py --type application
"""

import asyncio
import logging
from pathlib import Path

from data_ingestion import IngestionFramework
from data_ingestion.stats import IngestionStats

from mcp_servers.uspto.offline.db import init_db
from mcp_servers.uspto.offline.db.connection import current_db_path, get_sync_connection
from mcp_servers.uspto.offline.ingestion.factory import patent_grant_record_factory
from mcp_servers.uspto.offline.ingestion.persister import USPTOPatentPersister
from mcp_servers.uspto.offline.ingestion.sources import USPTOBulkFileSource
from mcp_servers.uspto.offline.repository import FTS5Repository

logger = logging.getLogger(__name__)


def run(type: str = "grant") -> IngestionStats:
    """Entry point called by framework CLI.

    Args:
        type: Type of data to ingest - "grant" or "application" (default: "grant")

    Returns:
        IngestionStats object with metrics

    Raises:
        ValueError: If type is not a string (e.g., if CLI flag provided without value)
    """
    # Validate type parameter
    if not isinstance(type, str):
        raise ValueError(
            f"Invalid type parameter: expected string, got {type.__class__.__name__}. "
            "Did you forget to provide a value for --type? "
            "Use: --type=application or --type application"
        )

    # Validate type value
    type_lower = type.lower()
    if type_lower not in ("grant", "application"):
        raise ValueError(
            f"Invalid type value: '{type}'. Must be either 'grant' or 'application'. "
            f"Use: --type=grant or --type=application"
        )

    # Initialize database (creates if doesn't exist, verifies schema)
    asyncio.run(init_db())

    # Get database path from connection module
    db_path = Path(current_db_path())

    # Configuration - choose based on type parameter
    if type_lower == "application":
        config_filename = "patent_application_ingestion.yaml"
        data_type_label = "patent applications"
    else:
        config_filename = "patent_grant_ingestion.yaml"
        data_type_label = "patent grants"

    config_file = Path(__file__).parent.parent / "config" / config_filename
    logger.info(f"Ingesting USPTO {data_type_label}")

    # Connect to database using connection module
    with get_sync_connection() as conn:
        # Capture counts BEFORE ingestion
        cursor = conn.cursor()
        before_grant_count = cursor.execute(
            "SELECT COUNT(*) FROM patents WHERE document_type = 'grant'"
        ).fetchone()[0]
        before_app_count = cursor.execute(
            "SELECT COUNT(*) FROM patents WHERE document_type = 'application'"
        ).fetchone()[0]
        before_total = before_grant_count + before_app_count

        # Create USPTO-specific persister
        persister = USPTOPatentPersister(conn, enable_fts=False)

        # Create USPTO-specific bulk file source
        # Read file path from config and resolve relative to config directory
        import yaml

        with open(config_file) as f:
            config = yaml.safe_load(f)

        file_path = config["source"]["path"]
        # Resolve relative paths relative to config file directory
        file_path_obj = Path(file_path)
        if not file_path_obj.is_absolute():
            file_path_obj = (config_file.parent / file_path_obj).resolve()

        source = USPTOBulkFileSource(file_path=file_path_obj)

        # Define callback to rebuild FTS index and report statistics
        def rebuild_fts_callback(stats: IngestionStats) -> None:
            """Rebuild FTS5 index and report database statistics."""
            logger.info("Rebuilding FTS5 index...")
            fts_repo = FTS5Repository(conn)
            indexed_count = fts_repo.rebuild_index()
            logger.info(f"FTS5 index rebuilt: {indexed_count} patents indexed")

            # Report database statistics
            logger.info("")
            logger.info("Database Statistics:")
            logger.info("=" * 50)

            cursor = conn.cursor()

            # Count patents by document type (AFTER ingestion)
            after_grant_count = cursor.execute(
                "SELECT COUNT(*) FROM patents WHERE document_type = 'grant'"
            ).fetchone()[0]
            after_app_count = cursor.execute(
                "SELECT COUNT(*) FROM patents WHERE document_type = 'application'"
            ).fetchone()[0]
            after_total = after_grant_count + after_app_count

            # Calculate new records added
            new_grants = after_grant_count - before_grant_count
            new_apps = after_app_count - before_app_count
            new_total = after_total - before_total

            # Show before/after comparison
            logger.info("BEFORE this ingestion:")
            logger.info(f"  Patent grants                 : {before_grant_count:,}")
            logger.info(f"  Patent applications           : {before_app_count:,}")
            logger.info(f"  Total patent records          : {before_total:,}")
            logger.info("")
            logger.info("AFTER this ingestion:")
            logger.info(
                f"  Patent grants                 : {after_grant_count:,} (+{new_grants:,} new)"
            )
            logger.info(
                f"  Patent applications           : {after_app_count:,} (+{new_apps:,} new)"
            )
            logger.info(f"  Total patent records          : {after_total:,} (+{new_total:,} new)")
            logger.info("")

            # Count unique application numbers
            unique_app_nums = cursor.execute(
                "SELECT COUNT(DISTINCT application_number) FROM patents"
            ).fetchone()[0]
            logger.info(f"  Unique application numbers    : {unique_app_nums:,}")

            # Count application numbers with both forms
            dual_form_count = cursor.execute("""
                SELECT COUNT(*) FROM (
                    SELECT application_number
                    FROM patents
                    GROUP BY application_number
                    HAVING COUNT(DISTINCT document_type) = 2
                )
            """).fetchone()
            dual_form_count = dual_form_count[0] if dual_form_count else 0
            logger.info(f"  Applications with BOTH forms  : {dual_form_count:,}")

            # Count standalone grants and applications
            grant_only = cursor.execute("""
                SELECT COUNT(DISTINCT application_number)
                FROM patents
                WHERE document_type = 'grant'
                  AND application_number NOT IN (
                    SELECT application_number FROM patents WHERE document_type = 'application'
                  )
            """).fetchone()[0]
            app_only = cursor.execute("""
                SELECT COUNT(DISTINCT application_number)
                FROM patents
                WHERE document_type = 'application'
                  AND application_number NOT IN (
                    SELECT application_number FROM patents WHERE document_type = 'grant'
                  )
            """).fetchone()[0]
            logger.info(f"  Grant-only (no application)   : {grant_only:,}")
            logger.info(f"  Application-only (not granted): {app_only:,}")

            logger.info("")

            # Count records in related tables
            tables = {
                "inventors": "Inventor records",
                "assignees": "Assignee/attorney records",
                "cpc_classifications": "CPC classification records",
                "patent_citations": "Citation records",
                "examiners": "Examiner records",
            }

            counts = {"patents": after_total}
            for table, description in tables.items():
                try:
                    result = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    count = result[0] if result else 0
                    counts[table] = count
                    logger.info(f"  {description:30s}: {count:,}")
                except Exception as e:
                    logger.warning(f"  {description:30s}: Error - {e}")

            # Calculate and report ratios
            if counts.get("patents", 0) > 0:
                logger.info("")
                logger.info("Averages per Patent:")
                logger.info("-" * 50)

                if counts.get("inventors", 0) > 0:
                    avg_inventors = counts["inventors"] / counts["patents"]
                    logger.info(f"  Inventors per patent         : {avg_inventors:.2f}")

                if counts.get("assignees", 0) > 0:
                    avg_assignees = counts["assignees"] / counts["patents"]
                    logger.info(f"  Assignees per patent         : {avg_assignees:.2f}")

                if counts.get("cpc_classifications", 0) > 0:
                    avg_cpc = counts["cpc_classifications"] / counts["patents"]
                    logger.info(f"  CPC classifications per patent: {avg_cpc:.2f}")

                if counts.get("patent_citations", 0) > 0:
                    avg_citations = counts["patent_citations"] / counts["patents"]
                    logger.info(f"  Citations per patent         : {avg_citations:.2f}")

            # Check for entirely NULL columns
            logger.info("")
            logger.info("Checking for entirely NULL columns:")
            logger.info("-" * 50)

            null_columns = []
            for table in tables.keys():
                if counts.get(table, 0) == 0:
                    continue  # Skip empty tables

                # Get all columns for this table
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]

                # Check each column
                for column in columns:
                    result = cursor.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL"
                    ).fetchone()
                    non_null_count = result[0] if result else 0

                    if non_null_count == 0:
                        null_columns.append(f"{table}.{column}")

            if null_columns:
                logger.warning("  Found entirely NULL columns:")
                for col in null_columns:
                    logger.warning(f"    ❌ {col}")
            else:
                logger.info("  ✓ No entirely NULL columns found")

            # Report database file size
            if db_path.exists():
                db_size_bytes = db_path.stat().st_size
                db_size_mb = db_size_bytes / (1024 * 1024)
                logger.info("")
                logger.info(f"Database file size               : {db_size_mb:.1f} MB")

            logger.info("=" * 50)

        # Create framework with config and USPTO-specific components
        framework = IngestionFramework(
            config_file=config_file,
            persister=persister,
            record_factory=patent_grant_record_factory,
            source=source,  # Custom USPTO bulk file source
            on_complete=rebuild_fts_callback,
        )

        logger.info(f"Config: {config_file}")
        logger.info(f"Database: {db_path}")

        # Run ingestion
        stats = framework.ingest(start_position=None)  # Resume not yet implemented

        return stats
