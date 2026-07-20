#!/usr/bin/env python3
"""Download fixtures CLI for FMP MCP Server.

Usage:
    # Download fresh data from API (preferred, requires API key)
    uv run python -m scripts.download_fixtures

    # Download specific symbols from API
    uv run python -m scripts.download_fixtures --symbols AAPL,MSFT,GOOGL

    # Download with custom date range
    uv run python -m scripts.download_fixtures --from-date 2024-01-01 --to-date 2025-01-01

    # Resume interrupted download (default behavior - skips existing data)
    uv run python -m scripts.download_fixtures

    # Force re-download all data (ignore existing)
    uv run python -m scripts.download_fixtures --force

    # Download from API and save compressed fixtures for distribution
    uv run python -m scripts.download_fixtures --compress

    # Export existing database data to compressed fixtures
    uv run python -m scripts.download_fixtures --export

    # Use pre-compiled fixtures as fallback (no API key needed)
    uv run python -m scripts.download_fixtures --use-bundled
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

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
    init_db,
)
from mcp_servers.fmp_server.db.models import FISCAL_DATE_SENTINEL
from scripts.endpoints import (
    DEFAULT_SYMBOLS,
    Endpoint,
    EndpointCategory,
    get_all_non_symbol_endpoints,
    get_all_symbol_endpoints,
)

# Rate limiting: 300 requests per minute = 5 requests per second
RATE_LIMIT_REQUESTS_PER_SECOND = 4.5  # Slightly under to be safe
REQUEST_DELAY = 1.0 / RATE_LIMIT_REQUESTS_PER_SECOND

# Bundled fixtures location
FIXTURES_DIR = Path(__file__).parent.parent / "mcp_servers" / "fmp_server" / "fixtures"


def get_fixture_filename(endpoint_path: str, params: dict | None = None) -> str:
    """Convert endpoint path to fixture filename.

    Maps endpoint paths to descriptive filenames for fixtures.
    """

    # Normalize period value for consistent filenames
    def normalize_period(period: str) -> str:
        # Map "quarter" to "quarterly" for consistent naming
        if period == "quarter":
            return "quarterly"
        return period

    # Map endpoints to fixture filenames
    if endpoint_path == "/quote":
        return "quote"
    elif endpoint_path == "/profile":
        return "profile"
    elif endpoint_path == "/income-statement":
        period = normalize_period(params.get("period", "annual") if params else "annual")
        return f"income_statement_{period}"
    elif endpoint_path == "/balance-sheet-statement":
        period = normalize_period(params.get("period", "annual") if params else "annual")
        return f"balance_sheet_{period}"
    elif endpoint_path == "/cash-flow-statement":
        period = normalize_period(params.get("period", "annual") if params else "annual")
        return f"cash_flow_{period}"
    elif endpoint_path.startswith("/historical-price"):
        return "historical_prices"
    elif endpoint_path.startswith("/technical-indicator") and "/" in endpoint_path[1:]:
        # Handle both /technical-indicator/sma and /technical-indicators/sma
        indicator = endpoint_path.split("/")[-1].lower()
        # Include period and interval to avoid filename collisions
        # FMP API uses periodLength and timeframe for /technical-indicators/
        period = (
            params.get("periodLength", params.get("timePeriod", params.get("period", 14)))
            if params
            else 14
        )
        interval = (
            params.get("timeframe", params.get("type", params.get("interval", "daily")))
            if params
            else "daily"
        )
        return f"{indicator}_{period}_{interval}"
    elif endpoint_path.startswith("/news/"):
        # Handle /news/stock, /news/press-releases, etc.
        news_type = endpoint_path.split("/")[-1]
        return f"news_{news_type}"
    elif endpoint_path == "/economic-indicators":
        # Economic indicators are differentiated by name param (GDP, unemploymentRate, etc.)
        name = params.get("name", "unknown") if params else "unknown"
        # Convert camelCase to snake_case for consistency
        snake_name = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
        return f"economic_indicators_{snake_name}"
    else:
        # Generic: convert /some-endpoint to some_endpoint
        return endpoint_path.strip("/").replace("-", "_").replace("/", "_")


def save_compressed_fixture(
    symbol: str | None,
    endpoint_path: str,
    params: dict | None,
    data: dict | list,
) -> Path:
    """Save data as a compressed JSON fixture file.

    Args:
        symbol: Stock symbol (None for non-symbol endpoints)
        endpoint_path: The API endpoint path
        params: Request parameters
        data: Response data to save

    Returns:
        Path to the created fixture file
    """
    # Determine directory
    if symbol:
        fixture_dir = FIXTURES_DIR / symbol
    else:
        fixture_dir = FIXTURES_DIR / "static"

    fixture_dir.mkdir(parents=True, exist_ok=True)

    # Get filename
    filename = get_fixture_filename(endpoint_path, params)
    fixture_path = fixture_dir / f"{filename}.json.gz"

    # Build fixture data with metadata
    fixture_data = {
        "_endpoint": endpoint_path,
        "_params": params or {},
        "_symbol": symbol,
        "_created_at": datetime.now(UTC).isoformat(),
        "data": data,
    }

    # Write compressed JSON
    with gzip.open(fixture_path, "wt", encoding="utf-8") as f:
        json.dump(fixture_data, f, indent=2, default=str)

    return fixture_path


class DownloadProgress:
    """Track download progress."""

    def __init__(self, total_requests: int):
        self.total = total_requests
        self.completed = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()

    def update(self, success: bool = True) -> None:
        """Update progress."""
        if success:
            self.completed += 1
        else:
            self.failed += 1

    def skip(self) -> None:
        """Mark an item as skipped (already exists in database)."""
        self.skipped += 1

    def get_eta(self) -> str:
        """Get estimated time remaining."""
        elapsed = time.time() - self.start_time
        processed = self.completed + self.failed
        if processed == 0:
            return "calculating..."
        rate = processed / elapsed
        remaining = self.total - processed - self.skipped
        if rate > 0:
            eta_seconds = remaining / rate
            if eta_seconds < 60:
                return f"{int(eta_seconds)}s"
            elif eta_seconds < 3600:
                return f"{int(eta_seconds / 60)}m {int(eta_seconds % 60)}s"
            else:
                return f"{int(eta_seconds / 3600)}h {int((eta_seconds % 3600) / 60)}m"
        return "unknown"

    def print_progress(self, current_item: str, is_skip: bool = False) -> None:
        """Print current progress."""
        processed = self.completed + self.failed + self.skipped
        pct = processed / self.total * 100
        eta = self.get_eta()
        status = "Skip" if is_skip else "DL"
        print(
            f"\r[{pct:5.1f}%] {self.completed}/{self.total} | "
            f"Skip: {self.skipped} | Fail: {self.failed} | "
            f"ETA: {eta} | [{status}] {current_item:<40}",
            end="",
            flush=True,
        )


def compute_params_hash(params: dict | None) -> str:
    """Compute SHA256 hash of parameters for unique identification."""
    if not params:
        return hashlib.sha256(b"").hexdigest()
    # Sort keys for consistent hashing
    sorted_params = json.dumps(params, sort_keys=True)
    return hashlib.sha256(sorted_params.encode()).hexdigest()


async def check_data_exists(
    endpoint: Endpoint,
    symbol: str | None,
    params: dict | None = None,
) -> bool:
    """Check if data already exists in the database for this endpoint/symbol.

    Returns True if data exists (should skip download), False otherwise.
    """
    from sqlalchemy import func, select

    try:
        async with get_session() as session:
            endpoint_path = endpoint.path

            # Check based on endpoint type
            if endpoint_path == "/quote" and symbol:
                result = await session.execute(
                    select(func.count()).select_from(StockQuote).where(StockQuote.symbol == symbol)
                )
                return (result.scalar() or 0) > 0

            elif endpoint_path == "/profile" and symbol:
                result = await session.execute(
                    select(func.count()).select_from(Company).where(Company.symbol == symbol)
                )
                return (result.scalar() or 0) > 0

            elif endpoint_path == "/income-statement" and symbol:
                period = params.get("period", "annual") if params else "annual"
                result = await session.execute(
                    select(func.count())
                    .select_from(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol,
                        FinancialStatement.statement_type == "income",
                        FinancialStatement.period == period,
                    )
                )
                return (result.scalar() or 0) > 0

            elif endpoint_path == "/balance-sheet-statement" and symbol:
                period = params.get("period", "annual") if params else "annual"
                result = await session.execute(
                    select(func.count())
                    .select_from(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol,
                        FinancialStatement.statement_type == "balance",
                        FinancialStatement.period == period,
                    )
                )
                return (result.scalar() or 0) > 0

            elif endpoint_path == "/cash-flow-statement" and symbol:
                period = params.get("period", "annual") if params else "annual"
                result = await session.execute(
                    select(func.count())
                    .select_from(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol,
                        FinancialStatement.statement_type == "cash_flow",
                        FinancialStatement.period == period,
                    )
                )
                return (result.scalar() or 0) > 0

            elif endpoint_path.startswith("/historical-price") and symbol:
                # For dividend-adjusted endpoint, check if adj_* columns are populated
                # This allows it to update existing records that only have OHLC data
                if "dividend-adjusted" in endpoint_path:
                    query = (
                        select(func.count())
                        .select_from(HistoricalPrice)
                        .where(
                            HistoricalPrice.symbol == symbol,
                            HistoricalPrice.adj_close.isnot(None),
                        )
                    )
                else:
                    # For other historical price endpoints, check if any data exists
                    query = (
                        select(func.count())
                        .select_from(HistoricalPrice)
                        .where(HistoricalPrice.symbol == symbol)
                    )
                if params:
                    if params.get("from"):
                        query = query.where(HistoricalPrice.date >= params["from"])
                    if params.get("to"):
                        query = query.where(HistoricalPrice.date <= params["to"])
                result = await session.execute(query)
                return (result.scalar() or 0) > 0

            elif (
                endpoint_path.startswith("/technical-indicator")
                and "/" in endpoint_path[1:]
                and symbol
            ):
                # Handle both /technical-indicator/ and /technical-indicators/ paths
                indicator = endpoint_path.split("/")[-1].upper()
                # Get period and interval from params
                # FMP API uses periodLength and timeframe for /technical-indicators/
                period = (
                    params.get("periodLength", params.get("timePeriod", params.get("period", 14)))
                    if params
                    else 14
                )
                interval = (
                    params.get("timeframe", params.get("type", params.get("interval", "daily")))
                    if params
                    else "daily"
                )
                # Build query with period, interval, and date range filters
                query = (
                    select(func.count())
                    .select_from(TechnicalIndicator)
                    .where(
                        TechnicalIndicator.symbol == symbol,
                        TechnicalIndicator.indicator_type == indicator,
                        TechnicalIndicator.period == period,
                        TechnicalIndicator.interval == interval,
                    )
                )
                if params:
                    if params.get("from"):
                        query = query.where(TechnicalIndicator.date >= params["from"])
                    if params.get("to"):
                        query = query.where(TechnicalIndicator.date <= params["to"])
                result = await session.execute(query)
                return (result.scalar() or 0) > 0

            else:
                # Check generic responses table
                params_hash = compute_params_hash(params)
                result = await session.execute(
                    select(func.count())
                    .select_from(GenericApiResponse)
                    .where(
                        GenericApiResponse.endpoint == endpoint_path,
                        GenericApiResponse.params_hash == params_hash,
                    )
                )
                return (result.scalar() or 0) > 0

    except Exception:
        # If we can't check (e.g., table doesn't exist), assume data doesn't exist
        return False


async def download_from_api(
    client: httpx.AsyncClient,
    endpoint: Endpoint,
    symbol: str | None,
    progress: DownloadProgress,
    from_date: str | None = None,
    to_date: str | None = None,
    year: int | None = None,
    quarter: int | None = None,
    snapshot_date: str | None = None,
) -> dict | None:
    """Download data from FMP API for a single endpoint."""
    # Build URL and params
    url = endpoint.path
    params = dict(endpoint.params) if endpoint.params else {}

    # Apply dynamic date defaults for endpoints that need current year/quarter/date
    # Use provided values or fall back to current date
    now = datetime.now()
    if endpoint.path in [
        "/earning-call-transcript",
        "/institutional-ownership/symbol-positions-summary",
    ]:
        params.setdefault("year", year if year is not None else now.year)
        params.setdefault("quarter", quarter if quarter is not None else (now.month - 1) // 3 + 1)
    if endpoint.path == "/sector-performance-snapshot":
        params.setdefault(
            "date", snapshot_date if snapshot_date is not None else now.strftime("%Y-%m-01")
        )

    # Add date filtering for historical endpoints
    if endpoint.category == EndpointCategory.HISTORICAL:
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

    if symbol:
        # Some endpoints have symbol in path, others in params
        if "{symbol}" in url:
            url = url.replace("{symbol}", symbol)
        else:
            params["symbol"] = symbol

    progress.print_progress(f"{url} {symbol or ''}")

    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        await asyncio.sleep(REQUEST_DELAY)  # Rate limiting

        data = response.json()
        progress.update(success=True)
        return {
            "endpoint": endpoint.path,
            "symbol": symbol,
            "params": params,
            "data": data,
            "http_status": response.status_code,
        }
    except httpx.HTTPStatusError as e:
        progress.update(success=False)
        print(f"\nHTTP error for {url}: {e.response.status_code}")
        return None
    except Exception as e:
        progress.update(success=False)
        print(f"\nError downloading {url}: {e}")
        return None


async def store_quote(session, symbol: str, data: dict) -> None:
    """Store quote data in the database."""
    from sqlalchemy.dialects.sqlite import insert

    if not data or not isinstance(data, list) or len(data) == 0:
        return

    quote_data = data[0]
    stmt = insert(StockQuote).values(
        symbol=symbol,
        price=quote_data.get("price"),
        open=quote_data.get("open"),
        high=quote_data.get("dayHigh"),
        low=quote_data.get("dayLow"),
        previous_close=quote_data.get("previousClose"),
        change=quote_data.get("change"),
        change_percent=quote_data.get("changesPercentage"),
        volume=quote_data.get("volume"),
        avg_volume=quote_data.get("avgVolume"),
        market_cap=quote_data.get("marketCap"),
        pe_ratio=quote_data.get("pe"),
        eps=quote_data.get("eps"),
        fifty_two_week_high=quote_data.get("yearHigh"),
        fifty_two_week_low=quote_data.get("yearLow"),
        shares_outstanding=quote_data.get("sharesOutstanding"),
        quote_data=json.dumps(quote_data),
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={
            "price": stmt.excluded.price,
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "previous_close": stmt.excluded.previous_close,
            "change": stmt.excluded.change,
            "change_percent": stmt.excluded.change_percent,
            "volume": stmt.excluded.volume,
            "avg_volume": stmt.excluded.avg_volume,
            "market_cap": stmt.excluded.market_cap,
            "pe_ratio": stmt.excluded.pe_ratio,
            "eps": stmt.excluded.eps,
            "fifty_two_week_high": stmt.excluded.fifty_two_week_high,
            "fifty_two_week_low": stmt.excluded.fifty_two_week_low,
            "shares_outstanding": stmt.excluded.shares_outstanding,
            "quote_data": stmt.excluded.quote_data,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)


async def store_profile(session, symbol: str, data: dict) -> None:
    """Store company profile data in the database."""
    from sqlalchemy.dialects.sqlite import insert

    if not data or not isinstance(data, list) or len(data) == 0:
        return

    profile = data[0]
    stmt = insert(Company).values(
        symbol=symbol,
        company_name=profile.get("companyName"),
        exchange=profile.get("exchange"),
        exchange_short_name=profile.get("exchangeShortName"),
        currency=profile.get("currency"),
        country=profile.get("country"),
        sector=profile.get("sector"),
        industry=profile.get("industry"),
        cik=profile.get("cik"),
        cusip=profile.get("cusip"),
        isin=profile.get("isin"),
        description=profile.get("description"),
        website=profile.get("website"),
        logo=profile.get("image"),
        ceo=profile.get("ceo"),
        full_time_employees=profile.get("fullTimeEmployees"),
        ipo_date=profile.get("ipoDate"),
        address=profile.get("address"),
        city=profile.get("city"),
        state=profile.get("state"),
        zip=profile.get("zip"),
        phone=profile.get("phone"),
        is_etf=profile.get("isEtf", False),
        is_actively_trading=profile.get("isActivelyTrading", True),
        profile_data=json.dumps(profile),
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol"],
        set_={
            "company_name": stmt.excluded.company_name,
            "exchange": stmt.excluded.exchange,
            "exchange_short_name": stmt.excluded.exchange_short_name,
            "currency": stmt.excluded.currency,
            "country": stmt.excluded.country,
            "sector": stmt.excluded.sector,
            "industry": stmt.excluded.industry,
            "cik": stmt.excluded.cik,
            "cusip": stmt.excluded.cusip,
            "isin": stmt.excluded.isin,
            "description": stmt.excluded.description,
            "website": stmt.excluded.website,
            "logo": stmt.excluded.logo,
            "ceo": stmt.excluded.ceo,
            "full_time_employees": stmt.excluded.full_time_employees,
            "ipo_date": stmt.excluded.ipo_date,
            "address": stmt.excluded.address,
            "city": stmt.excluded.city,
            "state": stmt.excluded.state,
            "zip": stmt.excluded.zip,
            "phone": stmt.excluded.phone,
            "is_etf": stmt.excluded.is_etf,
            "is_actively_trading": stmt.excluded.is_actively_trading,
            "profile_data": stmt.excluded.profile_data,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)


def parse_date(date_str: str | None) -> date | None:
    """Parse a date string in YYYY-MM-DD format to a Python date object."""
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


async def store_financial_statement(
    session, symbol: str, data: list, statement_type: str, period: str
) -> None:
    """Store financial statement data in the database."""
    from sqlalchemy.dialects.sqlite import insert

    if not data or not isinstance(data, list):
        return

    for stmt_data in data:
        # Use FISCAL_DATE_SENTINEL for missing dates
        fiscal_date_str = stmt_data.get("date") or stmt_data.get("fiscalDateEnding")
        fiscal_date = parse_date(fiscal_date_str) or FISCAL_DATE_SENTINEL

        stmt = insert(FinancialStatement).values(
            symbol=symbol,
            statement_type=statement_type,
            period=period,
            fiscal_date=fiscal_date,
            fiscal_year=stmt_data.get("calendarYear"),
            filing_date=parse_date(stmt_data.get("fillingDate")),
            accepted_date=stmt_data.get("acceptedDate"),
            cik=stmt_data.get("cik"),
            reported_currency=stmt_data.get("reportedCurrency"),
            statement_data=json.dumps(stmt_data),
            updated_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "statement_type", "period", "fiscal_date"],
            set_={
                "fiscal_year": stmt.excluded.fiscal_year,
                "filing_date": stmt.excluded.filing_date,
                "accepted_date": stmt.excluded.accepted_date,
                "cik": stmt.excluded.cik,
                "reported_currency": stmt.excluded.reported_currency,
                "statement_data": stmt.excluded.statement_data,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)


async def store_historical_prices(session, symbol: str, data: dict) -> None:
    """Store historical price data in the database.

    Handles both full price data and dividend-adjusted data.
    When updating, only overwrites fields that are present in the incoming data,
    preserving existing values for fields not in the update.
    """
    from sqlalchemy.dialects.sqlite import insert

    if not data:
        return

    # Data may be in "historical" key or be a list directly
    historical = data.get("historical", data) if isinstance(data, dict) else data
    if not isinstance(historical, list):
        return

    for price_data in historical:
        date_str = price_data.get("date")
        price_date = parse_date(date_str)
        if not price_date:
            continue

        # Build values dict with only non-None fields
        values = {
            "symbol": symbol,
            "date": price_date,
            "updated_at": datetime.now(UTC),
        }

        # Add fields only if present in the data
        field_mappings = [
            ("open", "open"),
            ("high", "high"),
            ("low", "low"),
            ("close", "close"),
            ("adjOpen", "adj_open"),
            ("adjHigh", "adj_high"),
            ("adjLow", "adj_low"),
            ("adjClose", "adj_close"),
            ("volume", "volume"),
            ("unadjustedVolume", "unadjusted_volume"),
            ("change", "change"),
            ("changePercent", "change_percent"),
            ("vwap", "vwap"),
        ]

        for json_key, db_key in field_mappings:
            if json_key in price_data:
                values[db_key] = price_data[json_key]

        # Always store the full JSON data
        values["price_data"] = json.dumps(price_data)

        stmt = insert(HistoricalPrice).values(**values)

        # Build update set - only update fields that are present in the incoming data
        # This allows dividend-adjusted data to update adj* fields without overwriting OHLC
        update_set = {"updated_at": stmt.excluded.updated_at}

        for json_key, db_key in field_mappings:
            if json_key in price_data and price_data[json_key] is not None:
                update_set[db_key] = stmt.excluded[db_key]

        # Determine if this is full price data or partial (dividend-adjusted only)
        # Full price data has 'open', 'high', 'low', 'close' fields
        is_full_price_data = all(k in price_data for k in ["open", "high", "low", "close"])

        if is_full_price_data:
            # Full data - store the complete JSON
            update_set["price_data"] = stmt.excluded.price_data
        # If partial data (dividend-adjusted), don't overwrite price_data JSON
        # The columns will still be updated with the adj* values

        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "date"],
            set_=update_set,
        )
        await session.execute(stmt)


async def store_technical_indicator(
    session, symbol: str, indicator: str, data: list, period: int = 14, interval: str = "daily"
) -> None:
    """Store technical indicator data in the database.

    Args:
        session: Database session
        symbol: Stock symbol
        indicator: Indicator type (sma, ema, rsi, etc.)
        data: List of indicator data points
        period: Indicator calculation period (default 14)
        interval: Data interval (default "daily")
    """
    from sqlalchemy.dialects.sqlite import insert

    if not data or not isinstance(data, list):
        return

    for ind_data in data:
        date_str = ind_data.get("date")
        ind_date = parse_date(date_str)
        if not ind_date:
            continue

        # Use proper None check to handle indicator values of 0
        indicator_val = ind_data.get(indicator.lower())
        value = indicator_val if indicator_val is not None else ind_data.get("value")

        stmt = insert(TechnicalIndicator).values(
            symbol=symbol,
            indicator_type=indicator.upper(),
            period=period,
            interval=interval,
            date=ind_date,
            value=value,
            open=ind_data.get("open"),
            high=ind_data.get("high"),
            low=ind_data.get("low"),
            close=ind_data.get("close"),
            volume=ind_data.get("volume"),
            indicator_data=json.dumps(ind_data),
            updated_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "indicator_type", "period", "interval", "date"],
            set_={
                "value": stmt.excluded.value,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "indicator_data": stmt.excluded.indicator_data,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await session.execute(stmt)


async def store_generic_response(
    session, endpoint: str, params: dict | None, symbol: str | None, data, http_status: int
) -> None:
    """Store generic API response in the database."""
    from sqlalchemy.dialects.sqlite import insert

    params_hash = compute_params_hash(params)

    stmt = insert(GenericApiResponse).values(
        endpoint=endpoint,
        params_hash=params_hash,
        params_json=json.dumps(params) if params else None,
        symbol=symbol,
        response_data=json.dumps(data),
        http_status=http_status,
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["endpoint", "params_hash"],
        set_={
            "params_json": stmt.excluded.params_json,
            "symbol": stmt.excluded.symbol,
            "response_data": stmt.excluded.response_data,
            "http_status": stmt.excluded.http_status,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await session.execute(stmt)


async def process_downloaded_data(result: dict) -> None:
    """Process and store downloaded data in the appropriate table."""
    endpoint = result["endpoint"]
    symbol = result["symbol"]
    data = result["data"]
    params = result["params"]
    http_status = result["http_status"]

    async with get_session() as session:
        # Route to appropriate storage based on endpoint
        if endpoint == "/quote":
            await store_quote(session, symbol, data)
        elif endpoint == "/profile":
            await store_profile(session, symbol, data)
        elif endpoint == "/income-statement":
            period = params.get("period", "annual")
            await store_financial_statement(session, symbol, data, "income", period)
        elif endpoint == "/balance-sheet-statement":
            period = params.get("period", "annual")
            await store_financial_statement(session, symbol, data, "balance", period)
        elif endpoint == "/cash-flow-statement":
            period = params.get("period", "annual")
            await store_financial_statement(session, symbol, data, "cash_flow", period)
        elif endpoint.startswith("/historical-price"):
            await store_historical_prices(session, symbol, data)
        elif endpoint.startswith("/technical-indicator") and "/" in endpoint[1:]:
            # Handle both /technical-indicator/ and /technical-indicators/ paths
            indicator = endpoint.split("/")[-1]
            # Get period and interval from params
            # FMP API uses periodLength and timeframe for /technical-indicators/
            period = params.get("periodLength", params.get("timePeriod", params.get("period", 14)))
            interval = params.get("timeframe", params.get("type", params.get("interval", "daily")))
            await store_technical_indicator(session, symbol, indicator, data, period, interval)
        else:
            # Store in generic table
            await store_generic_response(session, endpoint, params, symbol, data, http_status)


async def download_all_from_api(
    symbols: list[str],
    from_date: str | None = None,
    to_date: str | None = None,
    force: bool = False,
    compress: bool = False,
    year: int | None = None,
    quarter: int | None = None,
    snapshot_date: str | None = None,
) -> None:
    """Download all fixture data from FMP API.

    Args:
        symbols: List of stock symbols to download data for.
        from_date: Start date for historical data (YYYY-MM-DD).
        to_date: End date for historical data (YYYY-MM-DD).
        force: If True, re-download all data. If False (default), skip existing data.
        compress: If True, also save downloaded data as compressed fixtures.
        year: Year for earning call transcripts and institutional holdings.
        quarter: Quarter for earning call transcripts and institutional holdings.
        snapshot_date: Date for sector performance snapshot (YYYY-MM-DD).
    """
    config = get_config()

    if not config.fmp_api_key:
        print("Error: FMP_API_KEY environment variable not set.")
        print("Either set the API key or use --use-bundled flag.")
        sys.exit(1)

    # Initialize database
    await init_db()

    # Calculate total requests
    symbol_endpoints = get_all_symbol_endpoints()
    non_symbol_endpoints = get_all_non_symbol_endpoints()
    total_requests = len(symbols) * len(symbol_endpoints) + len(non_symbol_endpoints)

    print(f"Downloading fixtures for {len(symbols)} symbols...")
    print(f"Total endpoints per symbol: {len(symbol_endpoints)}")
    print(f"Non-symbol endpoints: {len(non_symbol_endpoints)}")
    print(f"Total API requests: {total_requests}")
    if not force:
        print("Resume mode: skipping existing data (use --force to re-download)")
    if compress:
        print(f"Compress mode: saving fixtures to {FIXTURES_DIR}")
    print()

    progress = DownloadProgress(total_requests)

    async with httpx.AsyncClient(
        base_url=config.fmp_api_base_url,
        params={"apikey": config.fmp_api_key},
        timeout=30.0,
    ) as client:
        # Compute default year/quarter/date for resume capability
        now = datetime.now()
        effective_year = year if year is not None else now.year
        effective_quarter = quarter if quarter is not None else (now.month - 1) // 3 + 1
        effective_snapshot_date = (
            snapshot_date if snapshot_date is not None else now.strftime("%Y-%m-01")
        )

        # Download symbol-specific endpoints
        for symbol in symbols:
            for endpoint in symbol_endpoints:
                # Build params for existence check
                params = dict(endpoint.params) if endpoint.params else {}
                if endpoint.category == EndpointCategory.HISTORICAL:
                    if from_date:
                        params["from"] = from_date
                    if to_date:
                        params["to"] = to_date

                # Add year/quarter/date params for endpoints that need them
                if endpoint.path in [
                    "/earning-call-transcript",
                    "/institutional-ownership/symbol-positions-summary",
                ]:
                    params.setdefault("year", effective_year)
                    params.setdefault("quarter", effective_quarter)
                if endpoint.path == "/sector-performance-snapshot":
                    params.setdefault("date", effective_snapshot_date)

                # Add symbol to params if not in path (must match download_from_api)
                # This ensures params_hash matches for resume capability
                if "{symbol}" not in endpoint.path:
                    params["symbol"] = symbol

                # Check if data already exists (resume capability)
                if not force and await check_data_exists(endpoint, symbol, params):
                    progress.print_progress(f"{endpoint.path} {symbol}", is_skip=True)
                    progress.skip()
                    continue

                result = await download_from_api(
                    client,
                    endpoint,
                    symbol,
                    progress,
                    from_date,
                    to_date,
                    effective_year,
                    effective_quarter,
                    effective_snapshot_date,
                )
                if result:
                    await process_downloaded_data(result)
                    if compress:
                        save_compressed_fixture(
                            symbol=symbol,
                            endpoint_path=endpoint.path,
                            params=result["params"],
                            data=result["data"],
                        )

        # Download non-symbol endpoints
        for endpoint in non_symbol_endpoints:
            params = dict(endpoint.params) if endpoint.params else {}

            # Add year/quarter/date params for endpoints that need them
            if endpoint.path in [
                "/earning-call-transcript",
                "/institutional-ownership/symbol-positions-summary",
            ]:
                params.setdefault("year", effective_year)
                params.setdefault("quarter", effective_quarter)
            if endpoint.path == "/sector-performance-snapshot":
                params.setdefault("date", effective_snapshot_date)

            # Check if data already exists (resume capability)
            if not force and await check_data_exists(endpoint, None, params):
                progress.print_progress(f"{endpoint.path}", is_skip=True)
                progress.skip()
                continue

            result = await download_from_api(
                client,
                endpoint,
                None,
                progress,
                from_date,
                to_date,
                effective_year,
                effective_quarter,
                effective_snapshot_date,
            )
            if result:
                await process_downloaded_data(result)
                if compress:
                    save_compressed_fixture(
                        symbol=None,
                        endpoint_path=endpoint.path,
                        params=result["params"],
                        data=result["data"],
                    )

    print("\n")
    print("Download complete!")
    print(f"  Downloaded: {progress.completed}")
    print(f"  Skipped (existing): {progress.skipped}")
    print(f"  Failed: {progress.failed}")
    if compress:
        print(f"  Fixtures saved to: {FIXTURES_DIR}")

    await close_engine()


async def load_bundled_fixtures(quiet: bool = False) -> None:
    """Load pre-compiled bundled fixtures into the database.

    Args:
        quiet: If True, use logger instead of print to avoid corrupting
               JSON-RPC protocol when called from background thread.
    """
    from loguru import logger

    def output(msg: str, is_error: bool = False) -> None:
        """Output message via print or logger based on quiet mode."""
        if quiet:
            if is_error:
                logger.warning(msg)
            else:
                logger.info(msg)
        else:
            print(msg)

    if not FIXTURES_DIR.exists():
        output(f"Error: Bundled fixtures directory not found at {FIXTURES_DIR}", is_error=True)
        output("Bundled fixtures may not be included in this distribution.", is_error=True)
        if quiet:
            raise RuntimeError(f"Bundled fixtures directory not found at {FIXTURES_DIR}")
        sys.exit(1)

    # Initialize database
    await init_db()

    output(f"Loading bundled fixtures from {FIXTURES_DIR}...")

    # Count files
    fixture_files = list(FIXTURES_DIR.rglob("*.json.gz"))
    if not fixture_files:
        output("Error: No fixture files found in bundled fixtures directory.", is_error=True)
        if quiet:
            await close_engine()  # Clean up before raising
            raise RuntimeError("No fixture files found in bundled fixtures directory.")
        sys.exit(1)

    output(f"Found {len(fixture_files)} fixture files")

    loaded = 0
    failed = 0

    for fixture_path in fixture_files:
        try:
            # Decompress and parse
            with gzip.open(fixture_path, "rt", encoding="utf-8") as f:
                fixture_data = json.load(f)

            # Extract metadata from fixture (preferred) or infer from path
            params = fixture_data.get("_params", {})
            data = fixture_data.get("data", fixture_data)

            # Get path info for fallback and period inference
            parts = fixture_path.relative_to(FIXTURES_DIR).parts
            endpoint_name = fixture_path.stem.replace(".json", "")

            # Use _endpoint from metadata if available (most accurate)
            endpoint = fixture_data.get("_endpoint")
            if not endpoint:
                # Map filename back to endpoint path
                endpoint_map = {
                    "quote": "/quote",
                    "profile": "/profile",
                    "income_statement_annual": "/income-statement",
                    "income_statement_quarterly": "/income-statement",
                    "balance_sheet_annual": "/balance-sheet-statement",
                    "balance_sheet_quarterly": "/balance-sheet-statement",
                    "cash_flow_annual": "/cash-flow-statement",
                    "cash_flow_quarterly": "/cash-flow-statement",
                    "historical_prices": "/historical-price-eod/full",
                }
                # Check for technical indicator pattern: {indicator}_{period}_{interval}
                # or legacy pattern: just {indicator}
                known_indicators = ["sma", "ema", "rsi", "wma", "dema", "tema", "williams", "adx"]
                indicator_base = endpoint_name.split("_")[0].lower()
                if indicator_base in known_indicators:
                    endpoint = f"/technical-indicator/{indicator_base}"
                else:
                    endpoint = endpoint_map.get(endpoint_name, f"/{endpoint_name}")

            # Use _symbol from metadata if available, else infer from path
            symbol = fixture_data.get("_symbol")
            if symbol is None and len(parts) >= 2:
                symbol = parts[0] if parts[0] != "static" else None

            # Infer period from filename for financial statements if not in _params
            # This ensures quarterly fixtures are stored correctly even without _params
            if "period" not in params:
                if "quarterly" in endpoint_name:
                    params["period"] = "quarter"
                elif "annual" in endpoint_name:
                    params["period"] = "annual"

            # Infer timePeriod and interval from technical indicator filename pattern
            # Pattern: {indicator}_{period}_{interval} e.g., sma_14_daily
            if endpoint and endpoint.startswith("/technical-indicator/"):
                name_parts = endpoint_name.split("_")
                if len(name_parts) >= 3:
                    if "timePeriod" not in params:
                        try:
                            params["timePeriod"] = int(name_parts[1])
                        except ValueError:
                            pass
                    if "type" not in params and len(name_parts) > 2:
                        params["type"] = name_parts[2]

            result = {
                "endpoint": endpoint,
                "symbol": symbol,
                "params": params,
                "data": data,
                "http_status": 200,
            }
            await process_downloaded_data(result)
            loaded += 1

            if not quiet:
                print(f"\r  Loaded: {loaded}/{len(fixture_files)}", end="", flush=True)

        except Exception as e:
            failed += 1
            output(f"Error loading {fixture_path}: {e}", is_error=True)

    if not quiet:
        print("\n")
    output("Bundled fixtures loaded!")
    output(f"  Successful: {loaded}")
    output(f"  Failed: {failed}")

    await close_engine()


async def export_database_to_fixtures() -> None:
    """Export existing database data to compressed fixture files.

    This allows exporting data that was previously downloaded into the database
    as compressed fixtures for distribution, without making any new API calls.
    """
    from sqlalchemy import select

    # Initialize database
    await init_db()

    print(f"Exporting database to compressed fixtures at {FIXTURES_DIR}...")
    print()

    exported = 0
    failed = 0

    async with get_session() as session:
        # Export stock quotes
        print("Exporting stock quotes...")
        result = await session.execute(select(StockQuote))
        quotes = result.scalars().all()
        for quote in quotes:
            try:
                data = json.loads(quote.quote_data) if quote.quote_data else {}
                save_compressed_fixture(
                    symbol=quote.symbol,
                    endpoint_path="/quote",
                    params={},
                    data=[data] if data else [],
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting quote for {quote.symbol}: {e}")
                failed += 1
        print(f"  Exported {len(quotes)} quotes")

        # Export company profiles
        print("Exporting company profiles...")
        result = await session.execute(select(Company))
        companies = result.scalars().all()
        for company in companies:
            try:
                data = json.loads(company.profile_data) if company.profile_data else {}
                save_compressed_fixture(
                    symbol=company.symbol,
                    endpoint_path="/profile",
                    params={},
                    data=[data] if data else [],
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting profile for {company.symbol}: {e}")
                failed += 1
        print(f"  Exported {len(companies)} profiles")

        # Export financial statements (grouped by symbol, type, period)
        print("Exporting financial statements...")
        result = await session.execute(select(FinancialStatement))
        statements = result.scalars().all()

        # Group statements by symbol, type, period
        stmt_groups: dict[tuple[str, str, str], list] = {}
        for stmt in statements:
            key = (stmt.symbol, stmt.statement_type, stmt.period)
            if key not in stmt_groups:
                stmt_groups[key] = []
            data = json.loads(stmt.statement_data) if stmt.statement_data else {}
            stmt_groups[key].append(data)

        # Map statement types to endpoint paths
        type_to_endpoint = {
            "income": "/income-statement",
            "balance": "/balance-sheet-statement",
            "cash_flow": "/cash-flow-statement",
        }

        for (symbol, stmt_type, period), data_list in stmt_groups.items():
            try:
                endpoint_path = type_to_endpoint.get(stmt_type, f"/{stmt_type}")
                save_compressed_fixture(
                    symbol=symbol,
                    endpoint_path=endpoint_path,
                    params={"period": period},
                    data=data_list,
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting {stmt_type} for {symbol}: {e}")
                failed += 1
        print(f"  Exported {len(stmt_groups)} financial statement groups")

        # Export historical prices (grouped by symbol)
        print("Exporting historical prices...")
        result = await session.execute(select(HistoricalPrice))
        prices = result.scalars().all()

        # Group by symbol
        price_groups: dict[str, list] = {}
        for price in prices:
            if price.symbol not in price_groups:
                price_groups[price.symbol] = []
            data = json.loads(price.price_data) if price.price_data else {}
            price_groups[price.symbol].append(data)

        for symbol, data_list in price_groups.items():
            try:
                # Sort by date descending (most recent first, like API returns)
                data_list.sort(key=lambda x: x.get("date", ""), reverse=True)
                save_compressed_fixture(
                    symbol=symbol,
                    endpoint_path="/historical-price-eod/full",
                    params={},
                    data={"symbol": symbol, "historical": data_list},
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting historical prices for {symbol}: {e}")
                failed += 1
        print(f"  Exported {len(price_groups)} historical price sets")

        # Export technical indicators (grouped by symbol, indicator type, period, interval)
        print("Exporting technical indicators...")
        result = await session.execute(select(TechnicalIndicator))
        indicators = result.scalars().all()

        # Group by symbol, indicator type, period, and interval
        indicator_groups: dict[tuple[str, str, int, str], list] = {}
        for ind in indicators:
            key = (ind.symbol, ind.indicator_type, ind.period, ind.interval)
            if key not in indicator_groups:
                indicator_groups[key] = []
            data = json.loads(ind.indicator_data) if ind.indicator_data else {}
            indicator_groups[key].append(data)

        for (symbol, ind_type, period, interval), data_list in indicator_groups.items():
            try:
                # Sort by date descending
                data_list.sort(key=lambda x: x.get("date", ""), reverse=True)
                save_compressed_fixture(
                    symbol=symbol,
                    endpoint_path=f"/technical-indicator/{ind_type.lower()}",
                    params={"timePeriod": period, "type": interval},
                    data=data_list,
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting {ind_type} for {symbol}: {e}")
                failed += 1
        print(f"  Exported {len(indicator_groups)} technical indicator sets")

        # Export generic API responses
        print("Exporting generic API responses...")
        result = await session.execute(select(GenericApiResponse))
        responses = result.scalars().all()
        for resp in responses:
            try:
                data = json.loads(resp.response_data) if resp.response_data else {}
                params = json.loads(resp.params_json) if resp.params_json else {}
                save_compressed_fixture(
                    symbol=resp.symbol,
                    endpoint_path=resp.endpoint,
                    params=params,
                    data=data,
                )
                exported += 1
            except Exception as e:
                print(f"  Error exporting {resp.endpoint}: {e}")
                failed += 1
        print(f"  Exported {len(responses)} generic responses")

    print()
    print("Export complete!")
    print(f"  Exported: {exported}")
    print(f"  Failed: {failed}")
    print(f"  Fixtures saved to: {FIXTURES_DIR}")

    await close_engine()


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Download fixtures for FMP MCP Server offline mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python -m scripts.download_fixtures                     # Download (resume if interrupted)
  uv run python -m scripts.download_fixtures --symbols AAPL,MSFT # Download specific symbols
  uv run python -m scripts.download_fixtures --force             # Re-download all data
  uv run python -m scripts.download_fixtures --use-bundled       # Use pre-compiled fixtures
        """,
    )

    parser.add_argument(
        "--symbols",
        type=str,
        help="Comma-separated list of symbols to download (default: 25 S&P 500 symbols)",
    )
    parser.add_argument(
        "--from-date",
        type=str,
        help="Start date for historical data (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to-date",
        type=str,
        help="End date for historical data (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download all data, ignoring existing data in database",
    )
    parser.add_argument(
        "--use-bundled",
        action="store_true",
        help="Use pre-compiled bundled fixtures instead of downloading from API",
    )
    parser.add_argument(
        "--compress",
        action="store_true",
        help="Save downloaded data as compressed fixtures for distribution",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Export existing database data to compressed fixtures (no API calls)",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Year for earning call transcripts and institutional holdings (default: current year)",
    )
    parser.add_argument(
        "--quarter",
        type=int,
        choices=[1, 2, 3, 4],
        help="Quarter for earning transcripts and institutional holdings (default: current)",
    )
    parser.add_argument(
        "--snapshot-date",
        type=str,
        help="Date for sector performance snapshot (YYYY-MM-DD, default: first of current month)",
    )

    args = parser.parse_args()

    # Handle mutually exclusive modes
    if args.use_bundled and args.export:
        print("Error: --use-bundled and --export cannot be used together.")
        sys.exit(1)

    if args.export:
        # Export mode: export existing database to fixtures (no API key needed)
        asyncio.run(export_database_to_fixtures())
    elif args.use_bundled:
        asyncio.run(load_bundled_fixtures())
    else:
        # Parse symbols (filter out empty strings from input like ",AAPL," or ",")
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if not symbols:
                print("Error: No valid symbols provided. Empty strings are not valid symbols.")
                sys.exit(1)
        else:
            symbols = DEFAULT_SYMBOLS

        # Validate that we have an API key
        config = get_config()
        if not config.fmp_api_key:
            print("Error: FMP_API_KEY environment variable not set.")
            print()
            print("Options:")
            print("  1. Set FMP_API_KEY and try again")
            print("  2. Use --use-bundled flag to load pre-compiled fixtures")
            print("  3. Use --export flag to export existing database data")
            sys.exit(1)

        asyncio.run(
            download_all_from_api(
                symbols,
                args.from_date,
                args.to_date,
                args.force,
                args.compress,
                args.year,
                args.quarter,
                args.snapshot_date,
            )
        )


if __name__ == "__main__":
    main()
