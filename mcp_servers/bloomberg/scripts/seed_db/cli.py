"""CLI for managing the offline mode DuckDB database."""

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

from db import INTRADAY_INTERVALS, OfflineDatabase

from .fetchers import FMPFetcher
from .loaders import DuckDBLoader
from .pipeline import SeedPipeline
from .storage import RawStorage

logger = logging.getLogger(__name__)


def configure_logging(verbose: bool = False) -> None:
    """Configure logging level based on verbosity."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    # Silence httpx/httpcore unless verbose mode
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


# Paths
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

load_dotenv(".env.local")


def load_config() -> dict:
    """Load configuration from config.json."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_paths(config: dict) -> dict:
    """Get paths from config with defaults."""
    paths = config.get("paths", {})
    return {
        "raw_data": Path(paths.get("raw_data", "data/raw")),
        "duckdb": Path(paths.get("duckdb", "data/offline.duckdb")),
    }


def cmd_seed(args: argparse.Namespace) -> None:
    """Seed the database with data from FMP."""
    config = load_config()
    paths = get_paths(config)
    symbols = [s.upper() for s in config.get("symbols", [])]

    if not symbols:
        logger.error("No symbols configured in config.json")
        sys.exit(1)

    # Override from args if provided
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]

    historical_config = config.get("historical", {})
    intraday_config = config.get("intraday", {})
    profiles_config = config.get("profiles", {})

    # Override days/intervals from args
    if args.days:
        historical_config["days"] = args.days
    if args.intervals:
        intraday_config["intervals"] = [i.strip() for i in args.intervals.split(",")]

    # Handle --only flags (enable target, disable others)
    if args.historical_only:
        historical_config["enabled"] = True
        intraday_config["enabled"] = False
        profiles_config["enabled"] = False
    elif args.intraday_only:
        intraday_config["enabled"] = True
        historical_config["enabled"] = False
        profiles_config["enabled"] = False
    elif args.profiles_only:
        profiles_config["enabled"] = True
        historical_config["enabled"] = False
        intraday_config["enabled"] = False

    # Initialize pipeline components
    storage = RawStorage(paths["raw_data"])
    loader = DuckDBLoader(paths["duckdb"])

    # Only create fetcher if we need to make API calls
    fetcher = None if args.from_raw else FMPFetcher()

    try:
        pipeline = SeedPipeline(fetcher, storage, loader)

        logger.info(f"Database: {paths['duckdb'].absolute()}")
        logger.info(f"Raw data: {paths['raw_data'].absolute()}")
        logger.info(
            f"Symbols: {', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''} ({len(symbols)} total)"
        )
        if args.force:
            logger.info("Mode: FORCE (will re-fetch all data)")
        if args.from_raw:
            logger.info("Mode: FROM-RAW (loading from existing raw files, no API key required)")
        if args.no_save_raw:
            logger.info("Mode: NO-SAVE-RAW (not saving raw files)")
        if args.raw_only:
            logger.info("Mode: RAW-ONLY (fetch and save raw, no DB load)")

        # Run pipeline
        stats = pipeline.run(
            symbols=symbols,
            historical_config=historical_config,
            intraday_config=intraday_config,
            profiles_config=profiles_config,
            save_raw=not args.no_save_raw,
            from_raw=args.from_raw,
            raw_only=args.raw_only,
            force=args.force,
            verbose=True,
        )

        logger.info("=" * 40)
        logger.info("SUMMARY")
        logger.info("=" * 40)
        logger.info(f"Profiles:        {stats.profiles_count}")
        logger.info(f"Historical rows: {stats.historical_rows}")
        logger.info(f"Intraday rows:   {stats.intraday_rows}")
        logger.info(f"Total:           {stats.total_rows}")

    finally:
        loader.close()
        if fetcher is not None:
            fetcher.close()


def cmd_inspect(_args: argparse.Namespace) -> None:
    """Inspect the database contents."""
    config = load_config()
    paths = get_paths(config)

    with OfflineDatabase(paths["duckdb"]) as db:
        print("=" * 60)
        print("OFFLINE DATABASE SUMMARY")
        print("=" * 60)
        print(f"Path: {db.db_path.absolute()}")
        print()

        tables = db.get_tables()
        print(f"Tables: {', '.join(tables)}")

        # Show metadata summary
        if "seed_metadata" in tables:
            metadata = db.get_all_metadata()
            if metadata:
                print("\n--- Seed Metadata ---")
                print(
                    f"{'Symbol':<8} {'Data Type':<16} {'Rows':>6} {'First':<12} {'Last':<12} {'Seeded':<20}"
                )
                for m in metadata:
                    first = str(m["first_date"])[:10] if m["first_date"] else "N/A"
                    last = str(m["last_date"])[:10] if m["last_date"] else "N/A"
                    seeded = str(m["last_seeded"])[:19] if m["last_seeded"] else "N/A"
                    rows = f"{m['row_count']:>6}" if m["row_count"] is not None else "   N/A"
                    print(
                        f"{m['symbol']:<8} {m['data_type']:<16} {rows} {first:<12} {last:<12} {seeded:<20}"
                    )

        # Historical prices
        if "historical_prices" in tables:
            print("\n--- historical_prices ---")
            count = db.get_row_count("historical_prices")
            print(f"Total rows: {count}")

            if count > 0:
                stats = db.get_historical_stats()
                print(
                    f"{'Symbol':<8} {'Rows':>6} {'First Date':<12} {'Last Date':<12} {'Min':>10} {'Max':>10}"
                )
                for s in stats:
                    min_p = (
                        f"{s['min_price']:>10.2f}" if s["min_price"] is not None else "       N/A"
                    )
                    max_p = (
                        f"{s['max_price']:>10.2f}" if s["max_price"] is not None else "       N/A"
                    )
                    print(
                        f"{s['symbol']:<8} {s['rows']:>6} {str(s['first_date']):<12} {str(s['last_date']):<12} {min_p} {max_p}"
                    )

        # Intraday tables
        for interval in INTRADAY_INTERVALS:
            table_name = f"intraday_bars_{interval}"
            if table_name in tables:
                count = db.get_row_count(table_name)
                if count > 0:
                    print(f"\n--- {table_name} ---")
                    print(f"Total rows: {count}")
                    stats = db.get_intraday_stats(interval)
                    print(
                        f"{'Symbol':<8} {'Rows':>6} {'First Timestamp':<20} {'Last Timestamp':<20}"
                    )
                    for s in stats:
                        print(
                            f"{s['symbol']:<8} {s['rows']:>6} {str(s['first_ts']):<20} {str(s['last_ts']):<20}"
                        )


def cmd_inspect_raw(args: argparse.Namespace) -> None:
    """Inspect raw data files."""
    config = load_config()
    paths = get_paths(config)
    storage = RawStorage(paths["raw_data"])

    print("=" * 60)
    print("RAW DATA FILES")
    print("=" * 60)
    print(f"Path: {paths['raw_data'].absolute()}")
    print()

    # Historical
    historical_symbols = storage.list_symbols("historical")
    print(f"Historical: {len(historical_symbols)} files")
    if historical_symbols and args.verbose:
        for sym in historical_symbols[:10]:
            meta = storage.get_metadata("historical", sym)
            fetched = ((meta.get("fetched_at") or "?")[:19]) if meta else "?"
            print(f"  {sym}: fetched {fetched}")
        if len(historical_symbols) > 10:
            print(f"  ... and {len(historical_symbols) - 10} more")

    # Intraday
    for interval in INTRADAY_INTERVALS:
        intraday_symbols = storage.list_symbols("intraday", interval)
        if intraday_symbols:
            print(f"Intraday {interval}: {len(intraday_symbols)} files")
            if args.verbose:
                for sym in intraday_symbols[:5]:
                    meta = storage.get_metadata("intraday", sym, interval)
                    fetched = ((meta.get("fetched_at") or "?")[:19]) if meta else "?"
                    print(f"  {sym}: fetched {fetched}")
                if len(intraday_symbols) > 5:
                    print(f"  ... and {len(intraday_symbols) - 5} more")

    # Profiles
    if storage.exists("profiles"):
        raw = storage.load("profiles")
        if raw:
            profiles = raw.get("data", {}).get("profiles", [])
            print(f"Profiles: {len(profiles)} in batch file")


def cmd_query(args: argparse.Namespace) -> None:
    """Run a SQL query against the database."""
    config = load_config()
    paths = get_paths(config)

    with OfflineDatabase(paths["duckdb"]) as db:
        try:
            result = db.session.execute(text(args.sql))
            rows = result.fetchall()
            if rows:
                columns = result.keys()
                # Print header
                print("\t".join(columns))
                # Print rows
                for row in rows:
                    print("\t".join(str(val) for val in row))
            else:
                print("No results")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)


def cmd_delete(args: argparse.Namespace) -> None:
    """Delete data for specific symbol(s)."""
    config = load_config()
    paths = get_paths(config)
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    with OfflineDatabase(paths["duckdb"]) as db:
        existing = db.get_symbols()

        for symbol in symbols:
            if symbol not in existing:
                print(f"{symbol}: not found in database")
                continue

            if not args.force:
                confirm = input(f"Delete all data for {symbol}? [y/N] ")
                if confirm.lower() != "y":
                    print(f"{symbol}: skipped")
                    continue

            deleted = db.delete_symbol(symbol)
            total = sum(deleted.values())
            print(f"{symbol}: deleted {total} rows")
            for table, count in deleted.items():
                if count > 0:
                    print(f"  - {table}: {count}")


def cmd_clear_raw(args: argparse.Namespace) -> None:
    """Clear all raw data files."""
    config = load_config()
    paths = get_paths(config)
    storage = RawStorage(paths["raw_data"])

    if not args.force:
        confirm = input(f"Delete all raw files in {paths['raw_data']}? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled")
            return

    count = storage.clear_all()
    print(f"Deleted {count} raw files")


def main():
    parser = argparse.ArgumentParser(
        description="Manage the offline mode DuckDB database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # seed command
    seed_parser = subparsers.add_parser("seed", help="Seed the database with FMP data")
    seed_parser.add_argument(
        "--symbols", "-s", type=str, help="Comma-separated list of symbols (overrides config)"
    )
    seed_parser.add_argument(
        "--days", "-d", type=int, help="Days of historical data (overrides config)"
    )
    seed_parser.add_argument(
        "--intervals", "-i", type=str, help="Comma-separated list of intervals (overrides config)"
    )
    # Mutually exclusive data type selection
    data_type_group = seed_parser.add_mutually_exclusive_group()
    data_type_group.add_argument(
        "--historical-only", action="store_true", help="Only seed historical data"
    )
    data_type_group.add_argument(
        "--intraday-only", action="store_true", help="Only seed intraday data"
    )
    data_type_group.add_argument(
        "--profiles-only", action="store_true", help="Only seed company profiles"
    )
    seed_parser.add_argument(
        "--force", "-f", action="store_true", help="Force re-fetch even if data already exists"
    )
    seed_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show debug logs (including HTTP requests)"
    )
    # Raw data flags (mutually exclusive)
    raw_group = seed_parser.add_mutually_exclusive_group()
    raw_group.add_argument(
        "--no-save-raw",
        action="store_true",
        help="Don't save raw JSON files (only load to DB)",
    )
    raw_group.add_argument(
        "--from-raw",
        action="store_true",
        help="Load from existing raw files instead of fetching from API",
    )
    raw_group.add_argument(
        "--raw-only",
        action="store_true",
        help="Only fetch and save raw JSON files (don't load to DB)",
    )
    seed_parser.set_defaults(func=cmd_seed)

    # inspect command
    inspect_parser = subparsers.add_parser("inspect", help="Inspect database contents")
    inspect_parser.set_defaults(func=cmd_inspect)

    # inspect-raw command
    inspect_raw_parser = subparsers.add_parser("inspect-raw", help="Inspect raw data files")
    inspect_raw_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show file details"
    )
    inspect_raw_parser.set_defaults(func=cmd_inspect_raw)

    # query command
    query_parser = subparsers.add_parser("query", help="Run a SQL query")
    query_parser.add_argument("sql", type=str, help="SQL query to execute")
    query_parser.set_defaults(func=cmd_query)

    # delete command
    delete_parser = subparsers.add_parser("delete", help="Delete data for specific symbol(s)")
    delete_parser.add_argument(
        "symbols", type=str, help="Comma-separated list of symbols to delete"
    )
    delete_parser.add_argument(
        "--force", "-f", action="store_true", help="Skip confirmation prompt"
    )
    delete_parser.set_defaults(func=cmd_delete)

    # clear-raw command
    clear_raw_parser = subparsers.add_parser("clear-raw", help="Delete all raw data files")
    clear_raw_parser.add_argument(
        "--force", "-f", action="store_true", help="Skip confirmation prompt"
    )
    clear_raw_parser.set_defaults(func=cmd_clear_raw)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Configure logging (verbose flag only on seed command)
    verbose = getattr(args, "verbose", False)
    configure_logging(verbose=verbose)

    args.func(args)


if __name__ == "__main__":
    main()
