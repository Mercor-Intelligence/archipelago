"""MCP tools for managing offline data (DuckDB).

Tools:
- list_symbols: List all symbols in the database
- data_status: Show date ranges and counts for each data type
- download_symbol: Download CSV for a single symbol
"""

import io
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi_app.models.data_management import (
    DataStatusResponse,
    DataType,
    DateRange,
    DownloadSymbolRequest,
    DownloadSymbolResponse,
    ListSymbolsResponse,
)

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    """Get database path from DUCKDB_PATH env var or default."""
    return Path(os.environ.get("DUCKDB_PATH", "data/offline.duckdb"))


async def list_symbols() -> ListSymbolsResponse:
    """List all symbols available in the offline database."""
    from db import OfflineDatabase

    db_path = _get_db_path()

    if not db_path.exists():
        return ListSymbolsResponse(symbols=[], count=0)

    with OfflineDatabase(db_path) as db:
        symbols = db.get_symbols()
        return ListSymbolsResponse(symbols=symbols, count=len(symbols))


async def data_status() -> DataStatusResponse:
    """Get database status showing date ranges and counts for each data type."""
    from db import OfflineDatabase
    from db.session import INTRADAY_INTERVALS

    db_path = _get_db_path()

    if not db_path.exists():
        return DataStatusResponse(
            db_path=str(db_path),
            db_size_mb=0.0,
        )

    with OfflineDatabase(db_path) as db:
        tables = db.get_tables()
        db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)

        # Historical data range
        historical = None
        if "historical_prices" in tables:
            stats = db.get_historical_stats()
            if stats:
                historical = DateRange(
                    first_date=str(min(s["first_date"] for s in stats)),
                    last_date=str(max(s["last_date"] for s in stats)),
                    row_count=db.get_row_count("historical_prices"),
                    symbol_count=len(stats),
                )

        # Intraday ranges
        intraday_ranges: dict[str, DateRange | None] = {}
        for interval in INTRADAY_INTERVALS:
            table_name = f"intraday_bars_{interval}"
            if table_name in tables:
                stats = db.get_intraday_stats(interval)
                if stats:
                    intraday_ranges[interval] = DateRange(
                        first_date=str(min(s["first_ts"] for s in stats)),
                        last_date=str(max(s["last_ts"] for s in stats)),
                        row_count=db.get_row_count(table_name),
                        symbol_count=len(stats),
                    )

        # Profiles count
        profiles_count = None
        if "company_profiles" in tables:
            profiles_count = db.get_row_count("company_profiles")

        return DataStatusResponse(
            db_path=str(db_path),
            db_size_mb=db_size_mb,
            historical=historical,
            intraday_1min=intraday_ranges.get("1min"),
            intraday_5min=intraday_ranges.get("5min"),
            intraday_15min=intraday_ranges.get("15min"),
            intraday_30min=intraday_ranges.get("30min"),
            intraday_1hour=intraday_ranges.get("1hour"),
            intraday_4hour=intraday_ranges.get("4hour"),
            profiles_count=profiles_count,
        )


async def download_symbol(request: DownloadSymbolRequest) -> DownloadSymbolResponse:
    """Download CSV data for a single symbol.

    Supports historical daily data and intraday bars at various intervals.

    Args:
        request.symbol: Stock ticker (e.g., "AAPL")
        request.data_type: "historical" for daily OHLCV, or intraday intervals:
            "intraday_1min", "intraday_5min", "intraday_15min",
            "intraday_30min", "intraday_1hour", "intraday_4hour"

    Returns CSV with columns: date/timestamp, open, high, low, close, volume
    """
    from db.service import DuckDBService

    db_path = _get_db_path()
    symbol = request.symbol.upper()
    data_type = request.data_type

    if not db_path.exists():
        return DownloadSymbolResponse(
            symbol=symbol,
            data_type=data_type,
            row_count=0,
            csv_content="",
        )

    service = DuckDBService(db_path)

    try:
        # Parse optional date filters
        start = datetime.fromisoformat(request.start_date) if request.start_date else None
        end = datetime.fromisoformat(request.end_date) if request.end_date else None

        if data_type == DataType.HISTORICAL:
            df = service.get_historical(symbol, start_date=start, end_date=end)
        else:
            # Extract interval from data_type (e.g., "intraday_5min" -> "5min")
            interval = data_type.value.replace("intraday_", "")
            df = service.get_intraday_bars(symbol, interval, start=start, end=end)

        if df.empty:
            return DownloadSymbolResponse(
                symbol=symbol,
                data_type=data_type,
                row_count=0,
                csv_content="",
            )

        # Convert DataFrame to CSV string
        buffer = io.StringIO()
        df.to_csv(buffer, index=True)
        csv_content = buffer.getvalue()

        return DownloadSymbolResponse(
            symbol=symbol,
            data_type=data_type,
            row_count=len(df),
            csv_content=csv_content,
        )

    finally:
        service.close()
