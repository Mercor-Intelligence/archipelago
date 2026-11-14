#!/usr/bin/env python3
"""
Download missing Finance 1.5 symbols to a separate database, then merge.

Usage:
    export FMP_API_KEY=your_key
    uv run python scripts/download_missing.py
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime

import httpx

API_KEY = os.environ.get("FMP_API_KEY", "")
BASE_URL = "https://financialmodelingprep.com/stable"

# All symbols needed for Finance 1.5 tasks
FINANCE_1_5_SYMBOLS = [
    # High frequency (>10 refs)
    "SPY",
    "AAPL",
    "FSLR",
    "AMD",
    "MSCI",
    "TEF",
    "WMT",
    "LYFT",
    "KO",
    "LULU",
    "IWM",
    "ADP",
    "MMAT",
    # Medium frequency (3-10 refs)
    "PEP",
    "ASAN",
    "QQQ",
    "KOS",
    "JPM",
    "OXY",
    "APA",
    "LQD",
    "GILD",
    "MSFT",
    "BIIB",
    "TGT",
    "AMGN",
    "LBTY",
    "COST",
    # Low frequency (1-2 refs)
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
    # Additional common
    "AGG",
    "DIA",
    "FL",
    "LOW",
    "NKE",
    "NVO",
    "SKX",
    "UPS",
]

# Existing database
EXISTING_DB = "mcp_servers/fmp_server/data/fmp.db"
# New download database
NEW_DB = "mcp_servers/fmp_server/data/fmp_new.db"


def get_existing_symbols() -> set[str]:
    """Get symbols already in the existing database."""
    if not os.path.exists(EXISTING_DB):
        return set()
    conn = sqlite3.connect(EXISTING_DB)
    cursor = conn.execute("SELECT DISTINCT symbol FROM historical_prices")
    symbols = {row[0] for row in cursor.fetchall()}
    conn.close()
    return symbols


def create_new_db():
    """Create the new database with schema."""
    if os.path.exists(NEW_DB):
        os.remove(NEW_DB)

    conn = sqlite3.connect(NEW_DB)
    conn.executescript("""
        CREATE TABLE historical_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            date DATE NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            unadjusted_volume INTEGER,
            adj_open REAL,
            adj_high REAL,
            adj_low REAL,
            adj_close REAL,
            change REAL,
            change_percent REAL,
            vwap REAL,
            label TEXT,
            change_over_time REAL,
            price_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, date)
        );

        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT UNIQUE NOT NULL,
            company_name TEXT,
            exchange TEXT,
            currency TEXT,
            country TEXT,
            sector TEXT,
            industry TEXT,
            description TEXT,
            ceo TEXT,
            employees INTEGER,
            website TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            phone TEXT,
            ipo_date DATE,
            market_cap REAL,
            profile_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE stock_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT UNIQUE NOT NULL,
            price REAL,
            open REAL,
            high REAL,
            low REAL,
            previous_close REAL,
            change REAL,
            change_percent REAL,
            volume INTEGER,
            avg_volume INTEGER,
            market_cap REAL,
            pe_ratio REAL,
            eps REAL,
            fifty_two_week_high REAL,
            fifty_two_week_low REAL,
            shares_outstanding INTEGER,
            quote_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX idx_hp_symbol ON historical_prices(symbol);
        CREATE INDEX idx_hp_date ON historical_prices(date);
    """)
    conn.commit()
    conn.close()
    print(f"Created new database: {NEW_DB}")


async def fetch_symbol(
    client: httpx.AsyncClient, symbol: str, semaphore: asyncio.Semaphore
) -> dict:
    """Fetch all data for a symbol."""
    async with semaphore:
        print(f"  Fetching {symbol}...")
        params = {"symbol": symbol, "apikey": API_KEY}

        result = {"symbol": symbol, "full": None, "adj": None, "profile": None, "quote": None}

        try:
            # Full historical prices
            r = await client.get(f"{BASE_URL}/historical-price-eod/full", params=params)
            if r.status_code == 200:
                result["full"] = r.json()

            # Dividend-adjusted prices
            r = await client.get(
                f"{BASE_URL}/historical-price-eod/dividend-adjusted", params=params
            )
            if r.status_code == 200:
                result["adj"] = r.json()

            # Profile
            r = await client.get(f"{BASE_URL}/profile", params=params)
            if r.status_code == 200:
                result["profile"] = r.json()

            # Quote
            r = await client.get(f"{BASE_URL}/quote", params=params)
            if r.status_code == 200:
                result["quote"] = r.json()

        except Exception as e:
            print(f"    Error fetching {symbol}: {e}")

        return result


def store_data(data: dict):
    """Store downloaded data in the new database."""
    conn = sqlite3.connect(NEW_DB)
    symbol = data["symbol"]
    now = datetime.now().isoformat()

    # Store full historical prices
    if data["full"]:
        for p in data["full"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO historical_prices
                (symbol, date, open, high, low, close, volume,
                 change, change_percent, vwap, price_data, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    symbol,
                    p.get("date"),
                    p.get("open"),
                    p.get("high"),
                    p.get("low"),
                    p.get("close"),
                    p.get("volume"),
                    p.get("change"),
                    p.get("changePercent"),
                    p.get("vwap"),
                    json.dumps(p),
                    now,
                ),
            )

    # Update with adj prices
    if data["adj"]:
        for p in data["adj"]:
            conn.execute(
                """
                UPDATE historical_prices SET
                    adj_open = ?, adj_high = ?, adj_low = ?, adj_close = ?, updated_at = ?
                WHERE symbol = ? AND date = ?
            """,
                (
                    p.get("adjOpen"),
                    p.get("adjHigh"),
                    p.get("adjLow"),
                    p.get("adjClose"),
                    now,
                    symbol,
                    p.get("date"),
                ),
            )

    # Store profile
    if data["profile"] and len(data["profile"]) > 0:
        p = data["profile"][0]
        conn.execute(
            """
            INSERT OR REPLACE INTO companies
            (symbol, company_name, exchange, currency, country, sector, industry, description,
             ceo, employees, website, market_cap, profile_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                symbol,
                p.get("companyName"),
                p.get("exchange"),
                p.get("currency"),
                p.get("country"),
                p.get("sector"),
                p.get("industry"),
                p.get("description"),
                p.get("ceo"),
                p.get("fullTimeEmployees"),
                p.get("website"),
                p.get("mktCap"),
                json.dumps(p),
                now,
            ),
        )

    # Store quote
    if data["quote"] and len(data["quote"]) > 0:
        q = data["quote"][0]
        conn.execute(
            """
            INSERT OR REPLACE INTO stock_quotes
            (symbol, price, open, high, low, previous_close, change, change_percent,
             volume, market_cap, pe_ratio, eps, fifty_two_week_high, fifty_two_week_low,
             quote_data, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                symbol,
                q.get("price"),
                q.get("open"),
                q.get("dayHigh"),
                q.get("dayLow"),
                q.get("previousClose"),
                q.get("change"),
                q.get("changesPercentage"),
                q.get("volume"),
                q.get("marketCap"),
                q.get("pe"),
                q.get("eps"),
                q.get("yearHigh"),
                q.get("yearLow"),
                json.dumps(q),
                now,
            ),
        )

    conn.commit()
    conn.close()


def merge_databases():
    """Merge new database into existing database."""
    print("\nMerging databases...")

    new_conn = sqlite3.connect(NEW_DB)
    existing_conn = sqlite3.connect(EXISTING_DB)

    # Merge historical_prices
    cursor = new_conn.execute("SELECT * FROM historical_prices")
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    print(f"  Merging {len(rows)} historical price records...")
    now = datetime.now().isoformat()
    for row in rows:
        data = dict(zip(columns, row))
        existing_conn.execute(
            """
            INSERT OR REPLACE INTO historical_prices
            (symbol, date, open, high, low, close, volume, unadjusted_volume,
             adj_open, adj_high, adj_low, adj_close, change, change_percent, vwap,
             label, change_over_time, price_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data["symbol"],
                data["date"],
                data["open"],
                data["high"],
                data["low"],
                data["close"],
                data["volume"],
                data.get("unadjusted_volume"),
                data["adj_open"],
                data["adj_high"],
                data["adj_low"],
                data["adj_close"],
                data["change"],
                data["change_percent"],
                data["vwap"],
                data.get("label"),
                data.get("change_over_time"),
                data["price_data"],
                data.get("created_at") or now,
                data["updated_at"],
            ),
        )

    # Merge companies
    cursor = new_conn.execute("SELECT * FROM companies")
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    print(f"  Merging {len(rows)} company profiles...")
    for row in rows:
        data = dict(zip(columns, row))
        existing_conn.execute(
            """
            INSERT OR REPLACE INTO companies
            (symbol, company_name, exchange, currency, country, sector, industry,
             description, ceo, full_time_employees, website, profile_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data["symbol"],
                data["company_name"],
                data["exchange"],
                data["currency"],
                data["country"],
                data["sector"],
                data["industry"],
                data["description"],
                data["ceo"],
                data.get("employees"),
                data["website"],
                data["profile_data"],
                data.get("created_at") or now,
                data["updated_at"],
            ),
        )

    # Merge stock_quotes
    cursor = new_conn.execute("SELECT * FROM stock_quotes")
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    print(f"  Merging {len(rows)} stock quotes...")
    for row in rows:
        data = dict(zip(columns, row))
        existing_conn.execute(
            """
            INSERT OR REPLACE INTO stock_quotes
            (symbol, price, open, high, low, previous_close, change, change_percent,
             volume, market_cap, pe_ratio, eps, fifty_two_week_high, fifty_two_week_low,
             quote_data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data["symbol"],
                data["price"],
                data["open"],
                data["high"],
                data["low"],
                data["previous_close"],
                data["change"],
                data["change_percent"],
                data["volume"],
                data["market_cap"],
                data["pe_ratio"],
                data["eps"],
                data["fifty_two_week_high"],
                data["fifty_two_week_low"],
                data["quote_data"],
                data.get("created_at") or now,
                data["updated_at"],
            ),
        )

    existing_conn.commit()
    new_conn.close()
    existing_conn.close()
    print("  Merge complete!")


def audit_database():
    """Audit the database to verify Finance 1.5 coverage."""
    print("\n" + "=" * 60)
    print("DATABASE AUDIT")
    print("=" * 60)

    conn = sqlite3.connect(EXISTING_DB)

    # Get all symbols in database
    cursor = conn.execute("SELECT DISTINCT symbol FROM historical_prices ORDER BY symbol")
    db_symbols = {row[0] for row in cursor.fetchall()}

    # Check coverage
    needed = set(FINANCE_1_5_SYMBOLS)
    covered = needed & db_symbols
    missing = needed - db_symbols
    extra = db_symbols - needed

    print(f"\nSymbols needed for Finance 1.5: {len(needed)}")
    print(f"Symbols in database: {len(db_symbols)}")
    print(f"Coverage: {len(covered)}/{len(needed)} ({100 * len(covered) / len(needed):.1f}%)")

    if missing:
        print(f"\n❌ MISSING ({len(missing)}):")
        for s in sorted(missing):
            print(f"   {s}")
    else:
        print("\n✅ All Finance 1.5 symbols are covered!")

    if extra:
        print(f"\n📦 Extra symbols ({len(extra)}): {', '.join(sorted(extra))}")

    # Check adj_* columns
    cursor = conn.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(adj_close) as has_adj_close,
            COUNT(adj_open) as has_adj_open
        FROM historical_prices
    """)
    row = cursor.fetchone()
    print("\nAdjusted price coverage:")
    print(f"  Total records: {row[0]}")
    print(f"  With adj_close: {row[1]} ({100 * row[1] / row[0]:.1f}%)")
    print(f"  With adj_open: {row[2]} ({100 * row[2] / row[0]:.1f}%)")

    # Sample a dividend payer
    cursor = conn.execute("""
        SELECT symbol, date, close, adj_close
        FROM historical_prices
        WHERE symbol = 'KO' AND adj_close IS NOT NULL
        ORDER BY date DESC LIMIT 3
    """)
    rows = cursor.fetchall()
    if rows:
        print("\nSample KO (Coca-Cola) dividend-adjusted data:")
        for r in rows:
            diff = ((r[2] - r[3]) / r[2] * 100) if r[2] and r[3] else 0
            print(f"  {r[1]}: close={r[2]:.2f}, adj_close={r[3]:.2f} (diff: {diff:.2f}%)")

    conn.close()
    return len(missing) == 0


async def main():
    if not API_KEY:
        print("ERROR: Set FMP_API_KEY environment variable")
        return

    # Check existing symbols
    existing = get_existing_symbols()
    print(f"Existing database has {len(existing)} symbols")

    # Find missing symbols
    needed = set(FINANCE_1_5_SYMBOLS)
    missing = needed - existing

    if not missing:
        print("All Finance 1.5 symbols already exist!")
        audit_database()
        return

    print(f"Missing {len(missing)} symbols: {', '.join(sorted(missing))}")

    # Create new database
    create_new_db()

    # Download missing symbols
    print(f"\nDownloading {len(missing)} symbols...")
    semaphore = asyncio.Semaphore(5)  # 5 concurrent requests

    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [fetch_symbol(client, symbol, semaphore) for symbol in missing]
        results = await asyncio.gather(*tasks)

    # Store data
    print("\nStoring data...")
    success = 0
    for data in results:
        if data["full"] or data["profile"]:
            store_data(data)
            count = len(data["full"]) if data["full"] else 0
            print(f"  ✓ {data['symbol']}: {count} price records")
            success += 1
        else:
            print(f"  ✗ {data['symbol']}: no data")

    print(f"\nDownloaded {success}/{len(missing)} symbols")

    # Merge databases
    merge_databases()

    # Audit
    audit_database()

    # Cleanup
    if os.path.exists(NEW_DB):
        os.remove(NEW_DB)
        print("\nCleaned up temporary database")


if __name__ == "__main__":
    asyncio.run(main())
