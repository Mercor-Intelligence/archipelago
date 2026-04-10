"""Bloomberg MCP Tools."""

import asyncio
import sys
from pathlib import Path

# Add src to path for accessing fastapi_app
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

# Add bloomberg directory to path for local imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import io
import os
from datetime import datetime

from models import (
    DataStatusOutput,
    DownloadSymbolInput,
    DownloadSymbolOutput,
    EquityScreeningInput,
    EquityScreeningOutput,
    HistoricalDataInput,
    HistoricalDataOutput,
    IntradayBarsInput,
    IntradayBarsOutput,
    IntradayTicksInput,
    IntradayTicksOutput,
    ListSymbolsOutput,
    ReferenceDataInput,
    ReferenceDataOutput,
)


def _classify_error(e: Exception) -> dict:
    from shared.models.error_models import is_connection_error, is_timeout_error

    error_str = str(e)
    if is_connection_error(e):
        return {
            "error": {
                "category": "CONNECTION_ERROR",
                "message": (
                    f"Network connectivity error: {error_str}. "
                    f"The Bloomberg emulator service may be unreachable. Please retry."
                ),
            }
        }
    if is_timeout_error(e):
        return {
            "error": {
                "category": "TIMEOUT",
                "message": (
                    f"Request timed out: {error_str}. "
                    f"Please retry with fewer securities or a smaller date range."
                ),
            }
        }
    return {"error": {"category": "UNKNOWN", "message": error_str}}


async def reference_data(input: ReferenceDataInput) -> ReferenceDataOutput:
    """Get current quotes and reference data for securities."""
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    request_data = {"requestType": "ReferenceDataRequest", **input.model_dump(exclude_none=True)}

    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return ReferenceDataOutput(responses=[_classify_error(e)], count=0)

    return ReferenceDataOutput(responses=responses, count=len(responses))


async def historical_data(input: HistoricalDataInput) -> HistoricalDataOutput:
    """Get historical OHLCV data for securities."""
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    request_data = {
        "requestType": "HistoricalDataRequest",
        "request_id": "mcp_request",
        **input.model_dump(exclude_none=True),
    }

    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return HistoricalDataOutput(responses=[_classify_error(e)], count=0)

    return HistoricalDataOutput(responses=responses, count=len(responses))


async def intraday_bars(input: IntradayBarsInput) -> IntradayBarsOutput:
    """Get intraday OHLCV bar data at various intervals."""
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    # Map snake_case to camelCase for the underlying Bloomberg model
    data = input.model_dump(exclude_none=True)
    request_data = {
        "requestType": "IntradayBarRequest",
        "security": data.get("security"),
        "interval": data.get("interval", 60),
        "startDateTime": data.get("start_datetime"),
        "endDateTime": data.get("end_datetime"),
    }

    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return IntradayBarsOutput(responses=[_classify_error(e)], count=0)

    return IntradayBarsOutput(responses=responses, count=len(responses))


async def intraday_ticks(input: IntradayTicksInput) -> IntradayTicksOutput:
    """Get intraday tick-level data."""
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    # Map snake_case to camelCase for the underlying Bloomberg model
    data = input.model_dump(exclude_none=True)
    request_data = {
        "requestType": "IntradayTickRequest",
        "security": data.get("security"),
        "startDateTime": data.get("start_datetime"),
        "endDateTime": data.get("end_datetime"),
        "eventTypes": data.get("event_types", ["TRADE"]),
    }

    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return IntradayTicksOutput(responses=[_classify_error(e)], count=0)

    return IntradayTicksOutput(responses=responses, count=len(responses))


async def equity_screening(input: EquityScreeningInput) -> EquityScreeningOutput:
    """Screen equities by criteria (sector, market cap, etc.)."""
    from fastapi_app.services.service_manager import get_service_manager

    manager = get_service_manager()
    manager.initialize()

    # Map snake_case to camelCase for the underlying Bloomberg model
    data = input.model_dump(exclude_none=True)

    # Build overrides object for screening criteria
    overrides = {}
    if data.get("sector"):
        overrides["sector"] = data["sector"]
    if data.get("market_cap_min") is not None:
        overrides["marketCapMin"] = data["market_cap_min"]
    if data.get("market_cap_max") is not None:
        overrides["marketCapMax"] = data["market_cap_max"]

    request_data = {
        "requestType": "BeqsRequest",
        "screenName": data.get("screen_name", "Custom Screen"),
        "screenType": "EQS",  # Valid enum value: "EQS" or "CUSTOM"
        "group": "General",  # Required by BeqsRequest
    }

    # Only add overrides if there are any
    if overrides:
        request_data["overrides"] = overrides

    responses = []
    stop_event = asyncio.Event()

    try:
        async for envelope in manager.dispatcher.dispatch_async(
            request_data, stop_event=stop_event
        ):
            responses.append(envelope.to_dict())
    except Exception as e:
        return EquityScreeningOutput(responses=[_classify_error(e)], count=0)

    return EquityScreeningOutput(responses=responses, count=len(responses))


def _get_db_path():
    """Get database path from DUCKDB_PATH env var or default."""
    return Path(os.environ.get("DUCKDB_PATH", "data/offline.duckdb"))


async def list_symbols() -> ListSymbolsOutput:
    """List all symbols available in the offline database."""
    from db.database import OfflineDatabase

    db_path = _get_db_path()

    if not db_path.exists():
        return ListSymbolsOutput(symbols=[], count=0)

    with OfflineDatabase(db_path) as db:
        symbols = db.get_symbols()
        return ListSymbolsOutput(symbols=symbols, count=len(symbols))


async def data_status() -> DataStatusOutput:
    """Get database status showing date ranges and row counts for each data type."""
    from db.database import OfflineDatabase
    from db.session import INTRADAY_INTERVALS

    db_path = _get_db_path()

    if not db_path.exists():
        return DataStatusOutput(db_path=str(db_path), db_size_mb=0.0)

    with OfflineDatabase(db_path) as db:
        tables = db.get_tables()
        db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)

        # Historical data range
        historical = None
        if "historical_prices" in tables:
            stats = db.get_historical_stats()
            if stats:
                historical = {
                    "first_date": str(min(s["first_date"] for s in stats)),
                    "last_date": str(max(s["last_date"] for s in stats)),
                    "row_count": db.get_row_count("historical_prices"),
                    "symbol_count": len(stats),
                }

        # Intraday ranges
        intraday = {}
        for interval in INTRADAY_INTERVALS:
            table_name = f"intraday_bars_{interval}"
            if table_name in tables:
                stats = db.get_intraday_stats(interval)
                if stats:
                    intraday[interval] = {
                        "first_date": str(min(s["first_ts"] for s in stats)),
                        "last_date": str(max(s["last_ts"] for s in stats)),
                        "row_count": db.get_row_count(table_name),
                        "symbol_count": len(stats),
                    }

        # Profiles count
        profiles_count = None
        if "company_profiles" in tables:
            profiles_count = db.get_row_count("company_profiles")

        return DataStatusOutput(
            db_path=str(db_path),
            db_size_mb=db_size_mb,
            historical=historical,
            intraday=intraday if intraday else None,
            profiles_count=profiles_count,
        )


async def download_symbol(input: DownloadSymbolInput) -> DownloadSymbolOutput:
    """Download CSV data for a single symbol."""
    from db.service import DuckDBService

    db_path = _get_db_path()
    symbol = input.symbol.upper()
    data_type = input.data_type

    if not db_path.exists():
        return DownloadSymbolOutput(
            symbol=symbol,
            data_type=data_type,
            row_count=0,
            csv_content="",
        )

    service = DuckDBService(db_path)

    try:
        if data_type == "historical":
            df = service.get_historical(
                symbol,
                start_date=datetime.fromisoformat(input.start_date) if input.start_date else None,
                end_date=datetime.fromisoformat(input.end_date) if input.end_date else None,
            )
        else:
            # Extract interval from data_type (e.g., "intraday_5min" -> "5min")
            interval = data_type.replace("intraday_", "")
            df = service.get_intraday_bars(
                symbol,
                interval,
                start=datetime.fromisoformat(input.start_date) if input.start_date else None,
                end=datetime.fromisoformat(input.end_date) if input.end_date else None,
            )

        if df.empty:
            return DownloadSymbolOutput(
                symbol=symbol,
                data_type=data_type,
                row_count=0,
                csv_content="",
            )

        # Convert DataFrame to CSV string
        buffer = io.StringIO()
        df.to_csv(buffer, index=True)
        csv_content = buffer.getvalue()

        return DownloadSymbolOutput(
            symbol=symbol,
            data_type=data_type,
            row_count=len(df),
            csv_content=csv_content,
        )

    finally:
        service.close()
