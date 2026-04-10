#!/usr/bin/env python3
"""
Download SEC EDGAR data for S&P 500 companies.

Creates an offline dataset containing:
- Company tickers/CIK mapping
- Submissions (filing history) for each company
- Company facts (XBRL financial data) for each company

Usage:
    # Set your user agent (required by SEC)
    export EDGAR_USER_AGENT="YourCompany your@email.com"

    # Run the download
    python scripts/download_sp500_data.py

    # Or specify output directory
    python scripts/download_sp500_data.py --output ./data/edgar_offline

Output structure:
    ./data/edgar_offline/
    ├── company_tickers.json          # Full ticker/CIK mapping
    ├── sp500_tickers.json            # S&P 500 subset with CIKs
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

# Top ~300 S&P 500 companies by market cap (as of late 2024)
# Reduced from full S&P 500 to keep offline data under 100MB
SP500_TICKERS = [
    "AAPL",
    "ABBV",
    "ABT",
    "ACN",
    "ADBE",
    "ADI",
    "ADP",
    "ADSK",
    "AEP",
    "AFL",
    "AIG",
    "AJG",
    "AMAT",
    "AMD",
    "AMGN",
    "AMP",
    "AMT",
    "AMZN",
    "ANET",
    "AON",
    "APD",
    "APH",
    "AVGO",
    "AXP",
    "AZO",
    "BA",
    "BAC",
    "BDX",
    "BIIB",
    "BK",
    "BKNG",
    "BLK",
    "BMY",
    "BRK.B",
    "BSX",
    "BX",
    "C",
    "CAT",
    "CB",
    "CDNS",
    "CEG",
    "CHTR",
    "CI",
    "CL",
    "CMCSA",
    "CME",
    "CMG",
    "COF",
    "COP",
    "COST",
    "CRM",
    "CSCO",
    "CSX",
    "CTAS",
    "CTSH",
    "CVS",
    "CVX",
    "D",
    "DE",
    "DHR",
    "DIS",
    "DLR",
    "DUK",
    "EA",
    "ECL",
    "EL",
    "ELV",
    "EMR",
    "EOG",
    "EQIX",
    "ETN",
    "EW",
    "EXC",
    "F",
    "FDX",
    "FI",
    "FICO",
    "FIS",
    "FTNT",
    "GD",
    "GE",
    "GEHC",
    "GILD",
    "GIS",
    "GM",
    "GOOG",
    "GOOGL",
    "GPN",
    "GS",
    "GWW",
    "HCA",
    "HD",
    "HLT",
    "HON",
    "IBM",
    "ICE",
    "IDXX",
    "INTC",
    "INTU",
    "ISRG",
    "IT",
    "ITW",
    "JNJ",
    "JPM",
    "KDP",
    "KEYS",
    "KHC",
    "KKR",
    "KLAC",
    "KMB",
    "KO",
    "LHX",
    "LIN",
    "LLY",
    "LMT",
    "LOW",
    "LRCX",
    "LULU",
    "MA",
    "MAR",
    "MCD",
    "MCHP",
    "MCK",
    "MCO",
    "MDLZ",
    "MDT",
    "MET",
    "META",
    "MMC",
    "MMM",
    "MO",
    "MPC",
    "MRK",
    "MRNA",
    "MS",
    "MSCI",
    "MSFT",
    "MSI",
    "MU",
    "NEE",
    "NFLX",
    "NKE",
    "NOC",
    "NOW",
    "NSC",
    "NVDA",
    "NXPI",
    "O",
    "ODFL",
    "OKE",
    "ORCL",
    "ORLY",
    "OXY",
    "PANW",
    "PAYX",
    "PCAR",
    "PEG",
    "PEP",
    "PFE",
    "PG",
    "PGR",
    "PH",
    "PLD",
    "PM",
    "PNC",
    "PSA",
    "PSX",
    "PYPL",
    "QCOM",
    "REGN",
    "ROP",
    "ROST",
    "RSG",
    "RTX",
    "SBUX",
    "SCHW",
    "SHW",
    "SLB",
    "SMCI",
    "SNPS",
    "SO",
    "SPGI",
    "SRE",
    "SYK",
    "SYY",
    "T",
    "TDG",
    "TGT",
    "TJX",
    "TMO",
    "TMUS",
    "TRV",
    "TSLA",
    "TT",
    "TXN",
    "TYL",
    "UBER",
    "UNH",
    "UNP",
    "UPS",
    "URI",
    "USB",
    "V",
    "VLO",
    "VRTX",
    "VZ",
    "WBA",
    "WDC",
    "WEC",
    "WELL",
    "WFC",
    "WM",
    "WMB",
    "WMT",
    "WST",
    "XOM",
    "YUM",
    "ZTS",
    # Additional companies for sector coverage
    "AES",
    "ALB",
    "ALL",
    "ARE",
    "AVB",
    "AWK",
    "BEN",
    "BIO",
    "BKR",
    "CARR",
    "CBRE",
    "CCI",
    "CF",
    "CHD",
    "CMS",
    "CNC",
    "DAL",
    "DD",
    "DFS",
    "DG",
    "DHI",
    "DLTR",
    "DOV",
    "DOW",
    "DRI",
    "DTE",
    "DVN",
    "EBAY",
    "ED",
    "EFX",
    "EIX",
    "EMN",
    "ENPH",
    "EQR",
    "EQT",
    "ES",
    "ESS",
    "ETR",
    "EXR",
    "FANG",
    "FAST",
    "FCX",
    "FTV",
    "GEV",
    "GLW",
    "GNRC",
    "GPC",
    "HAL",
    "HES",
    "HIG",
    "HPE",
    "HPQ",
    "HSY",
    "HUM",
    "IFF",
    "ILMN",
    "INVH",
    "IP",
    "IQV",
    "IR",
    "IRM",
    "JBHT",
    "JCI",
    "K",
    "KEY",
    "KIM",
    "KMI",
    "KR",
    "L",
    "LDOS",
    "LEN",
    "LH",
    "LNT",
    "LUV",
    "LVS",
    "LYB",
    "MAA",
    "MAS",
    "MGM",
    "MKC",
]


class EdgarDownloader:
    """Download SEC EDGAR data with rate limiting."""

    def __init__(self, user_agent: str, output_dir: Path):
        self.user_agent = user_agent
        self.output_dir = output_dir
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

    def build_sp500_mapping(self, all_tickers: dict) -> dict:
        """Build CIK mapping for S&P 500 companies."""
        print("Building S&P 500 ticker -> CIK mapping...")

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

        # Map S&P 500 tickers to CIKs
        sp500_mapping = {}
        missing = []

        for ticker in SP500_TICKERS:
            # Handle tickers with dots (BRK.B -> BRK-B or BRKB)
            ticker_variants = [
                ticker.upper(),
                ticker.upper().replace(".", "-"),
                ticker.upper().replace(".", ""),
            ]

            found = False
            for variant in ticker_variants:
                if variant in ticker_lookup:
                    sp500_mapping[ticker] = ticker_lookup[variant]
                    found = True
                    break

            if not found:
                missing.append(ticker)

        # Save S&P 500 mapping
        sp500_file = self.output_dir / "sp500_tickers.json"
        with open(sp500_file, "w") as f:
            json.dump(sp500_mapping, f, indent=2)

        print(f"  Mapped {len(sp500_mapping)} S&P 500 companies")
        if missing:
            print(f"  Warning: Could not find CIK for {len(missing)} tickers: {missing[:10]}...")

        return sp500_mapping

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
        """Download all S&P 500 data."""
        # Create output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: Download company tickers
        all_tickers = await self.download_company_tickers()

        # Step 2: Build S&P 500 mapping
        sp500_mapping = self.build_sp500_mapping(all_tickers)

        # Step 3: Download submissions and facts for each company
        companies = list(sp500_mapping.items())
        total = len(companies)

        print(f"\nDownloading data for {total} S&P 500 companies...")
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
    parser = argparse.ArgumentParser(description="Download SEC EDGAR data for S&P 500 companies")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("./offline_data/edgar_offline"),
        help="Output directory (default: ./offline_data/edgar_offline)",
    )
    args = parser.parse_args()

    # Check for user agent
    user_agent = os.environ.get("EDGAR_USER_AGENT")
    if not user_agent or "example" in user_agent.lower():
        print("ERROR: EDGAR_USER_AGENT environment variable must be set.")
        print("Example: export EDGAR_USER_AGENT='YourCompany your@email.com'")
        print("\nThe SEC requires a valid User-Agent header with contact info.")
        sys.exit(1)

    print("SEC EDGAR S&P 500 Data Downloader")
    print(f"{'=' * 50}")
    print(f"User-Agent: {user_agent}")
    print(f"Output: {args.output}")
    print(f"Rate limit: {RATE_LIMIT} requests/second")
    print(f"{'=' * 50}\n")

    async with EdgarDownloader(user_agent, args.output) as downloader:
        await downloader.download_all()


if __name__ == "__main__":
    asyncio.run(main())
