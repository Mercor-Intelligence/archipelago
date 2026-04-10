"""Generic CLI for data ingestion framework.

Provides the 'ingest' command that dynamically loads and executes
application-specific ingestion entry points.
"""

import argparse
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from .stats import IngestionStats

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_app_module(app_file: Path) -> ModuleType:
    """Dynamically load application entry point module.

    Args:
        app_file: Path to application .py file

    Returns:
        Loaded module

    Raises:
        SystemExit: If module cannot be loaded
    """
    if not app_file.exists():
        logger.error(f"Application file not found: {app_file}")
        sys.exit(2)

    try:
        spec = importlib.util.spec_from_file_location("app", app_file)
        if spec is None or spec.loader is None:
            logger.error(f"Cannot load module from: {app_file}")
            sys.exit(2)

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        logger.error(f"Failed to load application module: {e}")
        sys.exit(2)


def ingest_command(args: argparse.Namespace) -> None:
    """Execute ingestion command.

    Args:
        args: Parsed command-line arguments
    """
    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # Load application module
    app_module = load_app_module(args.app_file)

    # Verify run() function exists
    if not hasattr(app_module, "run"):
        logger.error("Application file must export 'run() -> IngestionStats' function")
        sys.exit(2)

    run_func = getattr(app_module, "run")

    try:
        logger.info("Starting ingestion...")

        # Execute application's run function - pass extra args as kwargs if provided
        if hasattr(args, "extra_kwargs") and args.extra_kwargs:
            stats: IngestionStats = run_func(**args.extra_kwargs)
        else:
            stats: IngestionStats = run_func()

        # Display results
        logger.info("Ingestion complete!")
        logger.info("")
        logger.info("Statistics:")
        logger.info(f"  Records Processed:    {stats.records_processed:,}")
        logger.info(f"  Records Inserted:     {stats.records_inserted:,}")
        logger.info(f"  Parse Errors:         {stats.parse_errors:,}")
        logger.info(f"  Validation Errors:    {stats.validation_errors:,}")
        logger.info(f"  Persistence Errors:   {stats.persistence_errors:,}")
        logger.info(f"  Duration:             {stats.duration_seconds:.1f}s")
        logger.info(f"  Throughput:           {stats.records_per_second:.0f} rec/sec")

        # Write stats to file if requested
        if args.stats_file:
            import json

            stats_dict = {
                "records_processed": stats.records_processed,
                "records_inserted": stats.records_inserted,
                "records_skipped": stats.records_skipped,
                "parse_errors": stats.parse_errors,
                "validation_errors": stats.validation_errors,
                "persistence_errors": stats.persistence_errors,
                "batches_completed": stats.batches_completed,
                "duration_seconds": stats.duration_seconds,
                "records_per_second": stats.records_per_second,
                "success_rate": stats.success_rate,
                "error_rate": stats.error_rate,
            }
            args.stats_file.write_text(json.dumps(stats_dict, indent=2))
            logger.info(f"Statistics written to: {args.stats_file}")

    except KeyboardInterrupt:
        logger.warning("Ingestion interrupted by user")
        sys.exit(7)
    except Exception as e:
        logger.error(f"Ingestion failed: {e}", exc_info=args.verbose)
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="ingest",
        description="Data Ingestion Framework - Run ingestion pipeline",
    )

    parser.add_argument(
        "--app-file",
        type=Path,
        required=True,
        help="Path to application entry point file (must export run() function)",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose logging (DEBUG level)",
    )

    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Minimal output (WARNING level)",
    )

    parser.add_argument(
        "--stats-file",
        type=Path,
        help="Write statistics to JSON file",
    )

    # Parse known args and collect any extra args to pass through to run()
    args, extra_args = parser.parse_known_args()

    # Convert extra_args list to kwargs dict (--key value or --key=value -> {key: value})
    extra_kwargs = {}
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        if arg.startswith("--"):
            # Handle --key=value syntax
            if "=" in arg:
                key_part, value = arg[2:].split("=", 1)
                key = key_part.replace("-", "_")  # --batch-size=10 -> batch_size
                extra_kwargs[key] = value
                i += 1
            else:
                key = arg[2:].replace("-", "_")  # --batch-size -> batch_size
                # Check if next item is the value
                if i + 1 < len(extra_args) and not extra_args[i + 1].startswith("--"):
                    value = extra_args[i + 1]
                    extra_kwargs[key] = value
                    i += 2
                else:
                    # Flag without value (e.g., --verbose)
                    extra_kwargs[key] = True
                    i += 1
        else:
            i += 1

    args.extra_kwargs = extra_kwargs

    # Execute command
    ingest_command(args)


if __name__ == "__main__":
    main()
