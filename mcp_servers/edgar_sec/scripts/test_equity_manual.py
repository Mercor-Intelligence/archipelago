#!/usr/bin/env python3
"""Manual test script for equity compensation extraction.

This is a simple script to test equity compensation extraction with real filings.
No pytest required - just run it directly!

Usage:
    export EDGAR_USER_AGENT='Your Name your.email@example.com'
    uv run python scripts/test_equity_manual.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "edgar_sec"))

from tools.equity import get_equity_compensation


async def test_filing(cik: str, accession: str):
    """Test a specific filing and print results."""
    print(f"\n{'=' * 80}")
    print("Testing Filing")
    print(f"{'=' * 80}")
    print(f"CIK:       {cik}")
    print(f"Accession: {accession}")
    print(f"{'=' * 80}\n")

    try:
        # Extract equity compensation
        result = await get_equity_compensation(cik, accession)
        result_data = json.loads(result)

        # Print full JSON
        print("FULL RESULT (JSON):")
        print(json.dumps(result_data, indent=2))

        # Print summary
        print(f"\n{'=' * 80}")
        print("SUMMARY")
        print(f"{'=' * 80}")
        print(f"Data Source:  {result_data['data_source']}")
        print(f"Confidence:   {result_data['confidence']}")
        print(f"Report Date:  {result_data.get('report_date', 'N/A')}")

        # Print each equity type
        if result_data.get("stock_options"):
            print(f"\n{'─' * 80}")
            print("STOCK OPTIONS")
            print(f"{'─' * 80}")
            opts = result_data["stock_options"]
            for key, value in opts.items():
                if value is not None:
                    formatted_value = f"{value:,}" if isinstance(value, int) else value
                    print(f"  {key:45s}: {formatted_value}")

        if result_data.get("rsus"):
            print(f"\n{'─' * 80}")
            print("RSUs (Restricted Stock Units)")
            print(f"{'─' * 80}")
            rsus = result_data["rsus"]
            for key, value in rsus.items():
                if value is not None:
                    formatted_value = f"{value:,}" if isinstance(value, int) else value
                    print(f"  {key:45s}: {formatted_value}")

        if result_data.get("psus"):
            print(f"\n{'─' * 80}")
            print("PSUs (Performance Share Units)")
            print(f"{'─' * 80}")
            psus = result_data["psus"]
            for key, value in psus.items():
                if value is not None:
                    formatted_value = f"{value:,}" if isinstance(value, int) else value
                    print(f"  {key:45s}: {formatted_value}")

        if result_data.get("espp"):
            print(f"\n{'─' * 80}")
            print("ESPP (Employee Stock Purchase Plan)")
            print(f"{'─' * 80}")
            espp = result_data["espp"]
            for key, value in espp.items():
                if value is not None:
                    formatted_value = f"{value:,}" if isinstance(value, int) else value
                    print(f"  {key:45s}: {formatted_value}")

        if not any(
            [
                result_data.get("stock_options"),
                result_data.get("rsus"),
                result_data.get("psus"),
                result_data.get("espp"),
            ]
        ):
            print("\n⚠️  No equity compensation data found in filing")

        print(f"\n{'=' * 80}")
        print("✅ TEST COMPLETED SUCCESSFULLY")
        print(f"{'=' * 80}\n")

    except Exception as e:
        print(f"\n{'=' * 80}")
        print("❌ ERROR")
        print(f"{'=' * 80}")
        print(f"{e}")
        print(f"{'=' * 80}\n")
        raise


def print_instructions():
    """Print usage instructions."""
    print("""
    ╔════════════════════════════════════════════════════════════════════════════╗
    ║                  EQUITY COMPENSATION EXTRACTION TEST                       ║
    ╚════════════════════════════════════════════════════════════════════════════╝

    HOW TO USE THIS SCRIPT:

    1. Set your EDGAR user agent (required by SEC):
       export EDGAR_USER_AGENT='Your Name your.email@example.com'

    2. Modify the CIK and accession number in the main() function below

    3. Run this script:
       uv run python scripts/test_equity_manual.py

    ────────────────────────────────────────────────────────────────────────────

    HOW TO FIND ACCESSION NUMBERS:

    Option 1 - SEC Website:
      1. Go to: https://www.sec.gov/edgar/searchedgar/companysearch
      2. Search for company name (e.g., "Asana")
      3. Click on the company
      4. Find a recent 10-Q or 10-K filing
      5. Copy the accession number (format: 0001477720-24-000042)

    Option 2 - Direct URL:
      Replace CIK in: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=XXXXXXXXXX&type=10-Q

    ────────────────────────────────────────────────────────────────────────────

    EXAMPLE COMPANIES TO TEST:

    Asana (has RSUs, PSUs):
      CIK: 0001477720
      URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001477720&type=10-Q

    NVIDIA (has stock options, RSUs):
      CIK: 0001045810
      URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810&type=10-K

    Snowflake (has RSUs):
      CIK: 0001640147
      URL: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001640147&type=10-Q

    ════════════════════════════════════════════════════════════════════════════
    """)


async def main():
    """Main function - modify CIK and accession here."""

    # Check if EDGAR_USER_AGENT is set
    if not os.environ.get("EDGAR_USER_AGENT"):
        print("\n❌ ERROR: EDGAR_USER_AGENT not set!")
        print("\nPlease set it to your email:")
        print("  export EDGAR_USER_AGENT='Your Name your.email@example.com'")
        print("\nThis is required by SEC to identify your requests.\n")
        return

    print_instructions()

    # ========================================================================
    # MODIFY THESE VALUES TO TEST DIFFERENT FILINGS
    # ========================================================================

    # Example 1: Asana 10-Q (expected to have RSUs and PSUs)
    cik = "0001477720"
    accession = "0001477720-24-000042"

    # Example 2: NVIDIA 10-K (uncomment to test)
    # cik = "0001045810"
    # accession = "0001045810-24-000029"

    # Example 3: Your custom filing (uncomment and modify)
    # cik = "XXXXXXXXXX"
    # accession = "XXXXXXXXXX-XX-XXXXXX"

    # ========================================================================

    await test_filing(cik, accession)


if __name__ == "__main__":
    asyncio.run(main())
