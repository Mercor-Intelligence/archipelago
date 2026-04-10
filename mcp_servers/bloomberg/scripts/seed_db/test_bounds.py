#!/usr/bin/env python
"""Test FMP data availability bounds for each interval.

This script queries FMP for one symbol to determine:
- How far back historical daily data goes
- How far back each intraday interval goes

Usage:
    uv run python -m scripts.seed_db.test_bounds
"""

from datetime import datetime

from .fetchers import BaseFetcher, FMPFetcher

# Test symbol
SYMBOL = "AAPL"

# All intervals to test
INTERVALS = ["1min", "5min", "15min", "30min", "1hour", "4hour"]


def test_historical_bounds(client: BaseFetcher) -> dict:
    """Test historical daily data bounds."""
    # Request a very large date range to see what we get back
    response = client.fetch_historical(SYMBOL, days=365 * 10)  # 10 years
    rows = response.get("data", {}).get("historical", [])

    if not rows:
        return {"error": "No data returned"}

    # FMP returns newest first
    dates = sorted([r["date"] for r in rows])
    first_date = dates[0]
    last_date = dates[-1]

    return {
        "first_date": first_date,
        "last_date": last_date,
        "row_count": len(rows),
        "years": round(
            (datetime.fromisoformat(last_date) - datetime.fromisoformat(first_date)).days / 365, 1
        ),
    }


def test_intraday_bounds(client: BaseFetcher, interval: str) -> dict:
    """Test intraday data bounds for a specific interval."""
    response = client.fetch_intraday(SYMBOL, interval)
    rows = response.get("data", {}).get("bars", [])

    if not rows:
        return {"error": "No data returned"}

    # FMP returns newest first
    timestamps = sorted([r["date"] for r in rows])
    first_ts = timestamps[0]
    last_ts = timestamps[-1]

    # Calculate span
    first_dt = datetime.fromisoformat(first_ts)
    last_dt = datetime.fromisoformat(last_ts)
    span_days = (last_dt - first_dt).days

    return {
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "row_count": len(rows),
        "span_days": span_days,
    }


def main():
    print("=" * 70)
    print(f"FMP DATA BOUNDS TEST - Symbol: {SYMBOL}")
    print("=" * 70)
    print()

    with FMPFetcher() as client:
        # Test historical
        print("Testing HISTORICAL (daily)...")
        hist = test_historical_bounds(client)
        print(f"  First date:  {hist.get('first_date', 'N/A')}")
        print(f"  Last date:   {hist.get('last_date', 'N/A')}")
        print(f"  Row count:   {hist.get('row_count', 'N/A')}")
        print(f"  Coverage:    ~{hist.get('years', 'N/A')} years")
        print()

        # Test each intraday interval
        results = {}
        for interval in INTERVALS:
            print(f"Testing INTRADAY @ {interval}...")
            result = test_intraday_bounds(client, interval)
            results[interval] = result
            print(f"  First:     {result.get('first_timestamp', 'N/A')}")
            print(f"  Last:      {result.get('last_timestamp', 'N/A')}")
            print(f"  Rows:      {result.get('row_count', 'N/A')}")
            print(f"  Span:      {result.get('span_days', 'N/A')} days")
            print()

    # Summary table
    print("=" * 70)
    print("SUMMARY - Data Availability by Interval")
    print("=" * 70)
    print(f"{'Data Type':<15} {'First':<20} {'Last':<20} {'Rows':>8} {'Span':>12}")
    print("-" * 70)
    years = hist.get("years", 0)
    print(
        f"{'historical':<15} {hist.get('first_date', 'N/A'):<20} {hist.get('last_date', 'N/A'):<20} {hist.get('row_count', 0):>8} {'~' + str(years) + ' years':>12}"
    )

    for interval in INTERVALS:
        r = results[interval]
        first = r.get("first_timestamp", "N/A")[:10] if r.get("first_timestamp") else "N/A"
        last = r.get("last_timestamp", "N/A")[:10] if r.get("last_timestamp") else "N/A"
        span = f"{r.get('span_days', 0)} days"
        print(f"{interval:<15} {first:<20} {last:<20} {r.get('row_count', 0):>8} {span:>12}")

    print()
    print("NOTE: These bounds are based on your FMP subscription level.")
    print("      If you need more history, you may need to upgrade your plan.")


if __name__ == "__main__":
    main()
