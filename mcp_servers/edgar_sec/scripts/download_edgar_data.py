#!/usr/bin/env python3
"""
Download SEC EDGAR data for a configurable list of companies.

Creates an offline dataset containing:
- Company tickers/CIK mapping
- Submissions (filing history) for each company
- Company facts (XBRL financial data) for each company

Usage:
    # Set your user agent (required by SEC)
    export EDGAR_USER_AGENT="YourCompany your@email.com"

    # Run the download (uses default tickers.txt next to this script)
    python scripts/download_edgar_data.py

    # Or specify a custom ticker list and output directory
    python scripts/download_edgar_data.py -t my_tickers.txt -o ./data/edgar_offline

Output structure:
    ./data/edgar_offline/
    ├── company_tickers.json          # Full ticker/CIK mapping
    ├── tickers.json                  # Requested subset with CIKs
    ├── submissions/
    │   ├── CIK0000320193.json        # Apple
    │   ├── CIK0000789019.json        # Microsoft
    │   └── ...
    └── companyfacts/
        ├── CIK0000320193.json        # Apple XBRL data
        ├── CIK0000789019.json        # Microsoft XBRL data
        └── ...
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

# Rate limiting: SEC allows 10 requests/second
RATE_LIMIT = 10
REQUEST_INTERVAL = 1.0 / RATE_LIMIT  # 0.1 seconds between requests

# SEC API endpoints
SEC_BASE_URL = "https://data.sec.gov"
SEC_WWW_URL = "https://www.sec.gov"
COMPANY_TICKERS_URL = f"{SEC_WWW_URL}/files/company_tickers.json"


def load_tickers(path: Path) -> list[str]:
    """Load ticker symbols from a text file.

    Blank lines and lines starting with ``#`` are ignored.
    Each remaining line is treated as a single ticker symbol.
    """
    tickers: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                tickers.append(stripped)
    if not tickers:
        print(f"ERROR: No tickers found in {path}")
        sys.exit(1)
    return tickers


class EdgarDownloader:
    """Download SEC EDGAR data with rate limiting."""

    def __init__(self, user_agent: str, output_dir: Path, tickers: list[str]):
        self.user_agent = user_agent
        self.output_dir = output_dir
        self.tickers = tickers
        self.last_request_time = 0
        self.client = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def _rate_limited_get(self, url: str) -> httpx.Response:
        """Make a rate-limited GET request."""
        # Enforce rate limit
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < REQUEST_INTERVAL:
            await asyncio.sleep(REQUEST_INTERVAL - elapsed)

        self.last_request_time = time.time()
        response = await self.client.get(url)
        response.raise_for_status()
        return response

    async def download_company_tickers(self) -> dict:
        """Download the full company tickers mapping."""
        print("Downloading company tickers...")

        tickers_file = self.output_dir / "company_tickers.json"

        response = await self._rate_limited_get(COMPANY_TICKERS_URL)
        data = response.json()

        # Save full tickers file
        with open(tickers_file, "w") as f:
            json.dump(data, f, indent=2)

        print(f"  Saved {len(data)} companies to {tickers_file}")
        return data

    def build_ticker_mapping(self, all_tickers: dict) -> dict:
        """Build CIK mapping for the requested tickers."""
        print("Building ticker -> CIK mapping...")

        # Create ticker -> company data lookup
        ticker_lookup = {}
        for entry in all_tickers.values():
            ticker = entry.get("ticker", "").upper()
            if ticker:
                ticker_lookup[ticker] = {
                    "cik": str(entry["cik_str"]).zfill(10),
                    "name": entry.get("title", ""),
                    "ticker": ticker,
                }

        # Map requested tickers to CIKs
        ticker_mapping = {}
        missing = []

        for ticker in self.tickers:
            # Handle tickers with dots (BRK.B -> BRK-B or BRKB)
            ticker_variants = [
                ticker.upper(),
                ticker.upper().replace(".", "-"),
                ticker.upper().replace(".", ""),
            ]

            found = False
            for variant in ticker_variants:
                if variant in ticker_lookup:
                    ticker_mapping[ticker] = ticker_lookup[variant]
                    found = True
                    break

            if not found:
                missing.append(ticker)

        # Save ticker mapping
        mapping_file = self.output_dir / "tickers.json"
        with open(mapping_file, "w") as f:
            json.dump(ticker_mapping, f, indent=2)

        print(f"  Mapped {len(ticker_mapping)} companies")
        if missing:
            print(f"  Warning: Could not find CIK for {len(missing)} tickers: {missing[:10]}...")

        return ticker_mapping

    async def download_submissions(self, cik: str, ticker: str) -> bool:
        """Download submissions for a single company."""
        submissions_dir = self.output_dir / "submissions"
        submissions_dir.mkdir(exist_ok=True)

        output_file = submissions_dir / f"CIK{cik}.json"

        # Skip if already downloaded
        if output_file.exists():
            return True

        try:
            url = f"{SEC_BASE_URL}/submissions/CIK{cik}.json"
            response = await self._rate_limited_get(url)
            data = response.json()

            with open(output_file, "w") as f:
                json.dump(data, f)

            return True

        except httpx.HTTPStatusError as e:
            print(f"  Error downloading submissions for {ticker} ({cik}): {e.response.status_code}")
            return False
        except Exception as e:
            print(f"  Error downloading submissions for {ticker} ({cik}): {e}")
            return False

    async def download_company_facts(self, cik: str, ticker: str) -> bool:
        """Download company facts (XBRL data) for a single company."""
        facts_dir = self.output_dir / "companyfacts"
        facts_dir.mkdir(exist_ok=True)

        output_file = facts_dir / f"CIK{cik}.json"

        # Skip if already downloaded
        if output_file.exists():
            return True

        try:
            url = f"{SEC_BASE_URL}/api/xbrl/companyfacts/CIK{cik}.json"
            response = await self._rate_limited_get(url)
            data = response.json()

            with open(output_file, "w") as f:
                json.dump(data, f)

            return True

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Some companies don't have XBRL data
                print(f"  No XBRL data for {ticker} ({cik})")
            else:
                print(f"  Error downloading facts for {ticker} ({cik}): {e.response.status_code}")
            return False
        except Exception as e:
            print(f"  Error downloading facts for {ticker} ({cik}): {e}")
            return False

    async def download_all(self):
        """Download all data for the configured tickers."""
        # Create output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Download company tickers
        all_tickers = await self.download_company_tickers()

        # Step 2: Build ticker mapping
        ticker_mapping = self.build_ticker_mapping(all_tickers)

        # Step 3: Download submissions and facts for each company
        companies = list(ticker_mapping.items())
        total = len(companies)

        print(f"\nDownloading data for {total} companies...")
        print("(This will take ~2-3 minutes due to SEC rate limits)\n")

        submissions_success = 0
        facts_success = 0

        for i, (ticker, info) in enumerate(companies, 1):
            cik = info["cik"]

            # Progress indicator
            if i % 50 == 0 or i == total:
                print(f"Progress: {i}/{total} companies...")

            # Download submissions
            if await self.download_submissions(cik, ticker):
                submissions_success += 1

            # Download company facts
            if await self.download_company_facts(cik, ticker):
                facts_success += 1

        # Summary
        print(f"\n{'=' * 50}")
        print("Download Complete!")
        print(f"{'=' * 50}")
        print(f"Output directory: {self.output_dir}")
        print(f"Companies processed: {total}")
        print(f"Submissions downloaded: {submissions_success}")
        print(f"Company facts downloaded: {facts_success}")

        # Calculate size
        total_size = sum(f.stat().st_size for f in self.output_dir.rglob("*.json"))
        print(f"Total size: {total_size / (1024 * 1024):.1f} MB")


async def main():
    parser = argparse.ArgumentParser(description="Download SEC EDGAR data for a list of tickers")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("./offline_data/edgar_offline"),
        help="Output directory (default: ./offline_data/edgar_offline)",
    )
    parser.add_argument(
        "--tickers-file",
        "-t",
        type=Path,
        default=Path(__file__).resolve().parent / "tickers.txt",
        help="Path to a file with one ticker per line (default: scripts/tickers.txt)",
    )
    args = parser.parse_args()

    # Check for user agent
    user_agent = os.environ.get("EDGAR_USER_AGENT")
    if not user_agent or "example" in user_agent.lower():
        print("ERROR: EDGAR_USER_AGENT environment variable must be set.")
        print("Example: export EDGAR_USER_AGENT='YourCompany your@email.com'")
        print("\nThe SEC requires a valid User-Agent header with contact info.")
        sys.exit(1)

    # Load tickers
    tickers = load_tickers(args.tickers_file)

    print("SEC EDGAR Data Downloader")
    print(f"{'=' * 50}")
    print(f"User-Agent: {user_agent}")
    print(f"Tickers file: {args.tickers_file}")
    print(f"Tickers loaded: {len(tickers)}")
    print(f"Output: {args.output}")
    print(f"Rate limit: {RATE_LIMIT} requests/second")
    print(f"{'=' * 50}\n")

    async with EdgarDownloader(user_agent, args.output, tickers) as downloader:
        await downloader.download_all()


if __name__ == "__main__":
    asyncio.run(main())
