#!/usr/bin/env python3
"""
Parallel download script for FMP data.

Enhancements over download_fixtures.py:
1. Parallel HTTP requests (5 concurrent to respect rate limits)
2. Sequential database writes (to avoid SQLite locking)
3. Additive - never wipes existing data

Usage:
    # Download missing symbols for Finance 1.5
    uv run python -m scripts.download_parallel --symbols FSLR,MSCI,TEF,WMT,LYFT

    # Download all Finance 1.5 symbols (additive)
    uv run python -m scripts.download_parallel --finance-1.5

    # Download specific symbols
    uv run python -m scripts.download_parallel --symbols AAPL,GOOGL,MSFT
"""

import argparse
import asyncio
import os
import sys

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_servers.fmp_server.db import (
    Company,
    HistoricalPrice,
    close_engine,
    get_session,
    init_db,
)
from scripts.download_fixtures import (
    store_financial_statement,
    store_historical_prices,
    store_profile,
    store_quote,
)

API_KEY = os.environ.get("FMP_API_KEY", "")
BASE_URL = "https://financialmodelingprep.com/stable"

# Finance 1.5 symbols (all symbols referenced in task prompts)
FINANCE_1_5_SYMBOLS = [
    # Currently in database
    "AAPL",
    "ADP",
    "AGG",
    "AMD",
    "AMGN",
    "APA",
    "ASAN",
    "BIIB",
    "DIA",
    "FL",
    "GILD",
    "KOS",
    "LOW",
    "LQD",
    "LULU",
    "NKE",
    "NVO",
    "QQQ",
    "SKX",
    "SPY",
    "TGT",
    "UPS",
    # Missing symbols (sorted by frequency in tasks)
    "FSLR",
    "MSCI",
    "TEF",
    "WMT",
    "LYFT",
    "KO",
    "IWM",
    "MMAT",
    "PEP",
    "JPM",
    "OXY",
    "MSFT",
    "COST",
    "LBTY",
    "LLY",
    "BRK-B",
    "MRK",
    "AMZN",
    "VZ",
    "JNJ",
    "SLB",
    "GOOGL",
    "UNH",
    "NFLX",
    "PG",
    "BMY",
    "EL",
]


async def fetch_endpoint(
    client: httpx.AsyncClient, endpoint: str, params: dict
) -> dict | list | None:
    """Fetch a single endpoint with error handling."""
    try:
        url = f"{BASE_URL}{endpoint}"
        response = await client.get(url, params={**params, "apikey": API_KEY})
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except Exception as e:
        print(f"    Error fetching {endpoint}: {e}")
        return None


async def download_symbol_data(
    client: httpx.AsyncClient,
    symbol: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Download all data for a symbol using parallel HTTP requests."""
    async with semaphore:
        print(f"  Fetching {symbol}...")
        params = {"symbol": symbol}

        # Fetch all endpoints in parallel
        results = await asyncio.gather(
            fetch_endpoint(client, "/historical-price-eod/full", params),
            fetch_endpoint(client, "/historical-price-eod/dividend-adjusted", params),
            fetch_endpoint(client, "/profile", params),
            fetch_endpoint(client, "/quote", params),
            fetch_endpoint(
                client, "/income-statement", {**params, "period": "annual", "limit": 10}
            ),
            fetch_endpoint(
                client, "/balance-sheet-statement", {**params, "period": "annual", "limit": 10}
            ),
            fetch_endpoint(
                client, "/cash-flow-statement", {**params, "period": "annual", "limit": 10}
            ),
            return_exceptions=True,
        )

        return {
            "symbol": symbol,
            "full_prices": results[0] if not isinstance(results[0], Exception) else None,
            "adj_prices": results[1] if not isinstance(results[1], Exception) else None,
            "profile": results[2] if not isinstance(results[2], Exception) else None,
            "quote": results[3] if not isinstance(results[3], Exception) else None,
            "income": results[4] if not isinstance(results[4], Exception) else None,
            "balance": results[5] if not isinstance(results[5], Exception) else None,
            "cashflow": results[6] if not isinstance(results[6], Exception) else None,
        }


async def store_symbol_data(data: dict) -> dict:
    """Store all data for a symbol (sequential to avoid SQLite locking)."""
    symbol = data["symbol"]
    stats = {"symbol": symbol, "success": True, "counts": {}}

    try:
        # Store full historical prices
        if data["full_prices"]:
            async with get_session() as session:
                await store_historical_prices(session, symbol, {"historical": data["full_prices"]})
                await session.commit()
            stats["counts"]["full_prices"] = len(data["full_prices"])

        # Store dividend-adjusted prices (updates adj_* columns)
        if data["adj_prices"]:
            async with get_session() as session:
                await store_historical_prices(session, symbol, {"historical": data["adj_prices"]})
                await session.commit()
            stats["counts"]["adj_prices"] = len(data["adj_prices"])

        # Store profile
        if data["profile"]:
            async with get_session() as session:
                await store_profile(session, symbol, data["profile"])
                await session.commit()
            stats["counts"]["profile"] = 1

        # Store quote
        if data["quote"]:
            async with get_session() as session:
                await store_quote(session, symbol, data["quote"])
                await session.commit()
            stats["counts"]["quote"] = 1

        # Store financial statements
        # Map short names to full statement type names
        stmt_type_map = {
            "income": "income_statement",
            "balance": "balance_sheet",
            "cashflow": "cash_flow",
        }
        for stmt_type, stmt_data in [
            ("income", data["income"]),
            ("balance", data["balance"]),
            ("cashflow", data["cashflow"]),
        ]:
            if stmt_data:
                async with get_session() as session:
                    await store_financial_statement(
                        session, symbol, stmt_data, stmt_type_map[stmt_type], "annual"
                    )
                    await session.commit()
                stats["counts"][stmt_type] = len(stmt_data)

    except Exception as e:
        stats["success"] = False
        stats["error"] = str(e)

    return stats


async def get_existing_symbols() -> set[str]:
    """Get symbols already in the database."""
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(select(HistoricalPrice.symbol).distinct())
        return {row[0] for row in result.fetchall()}


async def main(symbols: list[str], skip_existing: bool = True):
    """Main download function."""
    if not API_KEY:
        print("ERROR: FMP_API_KEY environment variable not set")
        print("Set it with: export FMP_API_KEY=your_api_key")
        sys.exit(1)

    print("Initializing database...")
    await init_db()

    # Check existing symbols
    existing = await get_existing_symbols()
    print(f"Symbols already in database: {len(existing)}")

    if skip_existing:
        to_download = [s for s in symbols if s not in existing]
        skipped = len(symbols) - len(to_download)
        if skipped > 0:
            print(f"Skipping {skipped} symbols already in database")
    else:
        to_download = symbols

    if not to_download:
        print("Nothing to download - all symbols already exist!")
        await close_engine()
        return

    print(f"\n=== Downloading {len(to_download)} symbols ===")
    print(f"Symbols: {', '.join(to_download[:10])}{'...' if len(to_download) > 10 else ''}")

    # Semaphore for rate limiting (5 concurrent requests)
    semaphore = asyncio.Semaphore(5)

    # Phase 1: Parallel HTTP downloads
    print("\nPhase 1: Fetching data from FMP API...")
    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [download_symbol_data(client, symbol, semaphore) for symbol in to_download]
        all_data = await asyncio.gather(*tasks)

    # Phase 2: Sequential database writes
    print("\nPhase 2: Storing data in database...")
    results = []
    for data in all_data:
        result = await store_symbol_data(data)
        symbol = result["symbol"]
        if result["success"]:
            counts = result["counts"]
            print(f"  ✓ {symbol}: {sum(counts.values())} records")
        else:
            print(f"  ✗ {symbol}: {result.get('error', 'Unknown error')}")
        results.append(result)

    # Summary
    success_count = sum(1 for r in results if r["success"])
    print("\n=== Summary ===")
    print(f"Downloaded: {success_count}/{len(to_download)} symbols")

    # Final database stats
    from sqlalchemy import func, select

    async with get_session() as session:
        result = await session.execute(select(func.count(HistoricalPrice.id)))
        total_prices = result.scalar()

        result = await session.execute(select(func.count(Company.id)))
        total_companies = result.scalar()

        result = await session.execute(select(HistoricalPrice.symbol).distinct())
        total_symbols = len(result.fetchall())

    print("\nDatabase totals:")
    print(f"  Symbols: {total_symbols}")
    print(f"  Historical prices: {total_prices}")
    print(f"  Company profiles: {total_companies}")

    await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download FMP data in parallel")
    parser.add_argument(
        "--symbols",
        type=str,
        help="Comma-separated list of symbols to download",
    )
    parser.add_argument(
        "--finance-1.5",
        dest="finance_1_5",
        action="store_true",
        help="Download all Finance 1.5 symbols",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if symbol exists",
    )

    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    elif args.finance_1_5:
        symbols = FINANCE_1_5_SYMBOLS
    else:
        print("ERROR: Specify --symbols or --finance-1.5")
        sys.exit(1)

    asyncio.run(main(symbols, skip_existing=not args.force))
