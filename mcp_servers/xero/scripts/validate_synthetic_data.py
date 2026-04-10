#!/usr/bin/env python3
"""
Validate synthetic data against live Xero API responses.

This script:
1. Authenticates with Xero using existing OAuth tokens
2. Fetches ONE sample response from each of the 7 endpoints
3. Compares field structure (keys) between live vs synthetic data
4. Outputs a comparison report showing matches/mismatches

Prerequisites:
- Valid .xero_tokens.json file (run test_oauth_flow.py first)
- XERO_CLIENT_ID environment variable set
- XERO_TENANT_ID environment variable set
- Real Xero account with test data

Usage:
    export XERO_CLIENT_ID="your-client-id"
    export XERO_TENANT_ID="your-tenant-id"
    python scripts/validate_synthetic_data.py
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_servers.xero.auth import OAuthManager, TokenStore
from mcp_servers.xero.config import config

# ============================================================================
# Utility Functions
# ============================================================================


def get_all_keys(obj: Any, prefix: str = "") -> set[str]:
    """
    Recursively extract all keys from nested dict/list structure.

    Args:
        obj: The object to extract keys from (dict, list, or primitive)
        prefix: Current key path (for nested structures)

    Returns:
        Set of all key paths found in the structure

    Examples:
        {"a": 1, "b": {"c": 2}} → {"a", "b", "b.c"}
        [{"x": 1}] → {"[0].x"}
    """
    keys = set()

    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{prefix}.{key}" if prefix else key
            keys.add(current_path)
            keys.update(get_all_keys(value, current_path))
    elif isinstance(obj, list) and obj:
        # For lists, analyze the first item as a representative sample
        keys.update(get_all_keys(obj[0], f"{prefix}[0]" if prefix else "[0]"))

    return keys


def compare_keys(
    live_keys: set[str], synthetic_keys: set[str], endpoint_name: str
) -> dict[str, Any]:
    """
    Compare two sets of keys and generate comparison report.

    Args:
        live_keys: Keys extracted from live API response
        synthetic_keys: Keys extracted from synthetic data
        endpoint_name: Name of the endpoint being compared

    Returns:
        Dict with comparison results including matches, missing, and extra keys
    """
    matched = live_keys & synthetic_keys
    missing_in_synthetic = live_keys - synthetic_keys
    extra_in_synthetic = synthetic_keys - live_keys

    return {
        "endpoint": endpoint_name,
        "matched_count": len(matched),
        "matched": sorted(matched),
        "missing_in_synthetic": sorted(missing_in_synthetic),
        "extra_in_synthetic": sorted(extra_in_synthetic),
        "is_valid": len(missing_in_synthetic) == 0 and len(extra_in_synthetic) == 0,
    }


def print_section(title: str, char: str = "=") -> None:
    """Print a formatted section header."""
    print(f"\n{char * 70}")
    print(f"  {title}")
    print(f"{char * 70}")


# ============================================================================
# Live Data Fetching
# ============================================================================


async def fetch_live_data(oauth_manager: OAuthManager) -> dict[str, Any]:
    """
    Fetch one sample response from each Xero API endpoint.

    Args:
        oauth_manager: Authenticated OAuth manager instance

    Returns:
        Dict containing live responses from all 7 endpoints

    Raises:
        httpx.HTTPError: If API calls fail
        ValueError: If tenant ID is not configured
    """
    # Get valid access token (auto-refreshes if needed)
    access_token = await oauth_manager.get_valid_access_token()

    if not access_token:
        raise ValueError("No valid access token available. Please run test_oauth_flow.py first.")

    # Verify tenant ID is set
    if not config.xero_tenant_id:
        raise ValueError(
            "XERO_TENANT_ID environment variable not set.\n"
            "Run test_oauth_flow.py to get your tenant ID, then:\n"
            "  export XERO_TENANT_ID='your-tenant-id'"
        )

    logger.info(f"Using tenant ID: {config.xero_tenant_id}")

    # Common headers for all requests
    headers = {
        "Authorization": f"Bearer {access_token}",
        "xero-tenant-id": config.xero_tenant_id,
        "Accept": "application/json",
    }

    base_url = config.xero_api_base_url
    live_data = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Fetch Accounts
        logger.info("Fetching Accounts...")
        response = await client.get(f"{base_url}/Accounts", headers=headers)
        response.raise_for_status()
        accounts_data = response.json()
        live_data["Accounts"] = accounts_data.get("Accounts", [])
        logger.info(f"  Fetched {len(live_data['Accounts'])} accounts")

        # 2. Fetch Contacts
        logger.info("Fetching Contacts...")
        response = await client.get(f"{base_url}/Contacts", headers=headers)
        response.raise_for_status()
        contacts_data = response.json()
        live_data["Contacts"] = contacts_data.get("Contacts", [])
        logger.info(f"  Fetched {len(live_data['Contacts'])} contacts")

        # 3. Fetch Invoices (with full details including line items)
        logger.info("Fetching Invoices...")
        response = await client.get(
            f"{base_url}/Invoices",
            headers=headers,
            params={"summaryOnly": "false"},  # Include line items and full details
        )
        response.raise_for_status()
        invoices_data = response.json()
        live_data["Invoices"] = invoices_data.get("Invoices", [])
        logger.info(f"  Fetched {len(live_data['Invoices'])} invoices with full details")

        # 4. Fetch BankTransactions (with full details including line items)
        logger.info("Fetching BankTransactions...")
        response = await client.get(
            f"{base_url}/BankTransactions",
            headers=headers,
            params={"summaryOnly": "false"},  # Include line items and full details
        )
        response.raise_for_status()
        bank_txns_data = response.json()
        live_data["BankTransactions"] = bank_txns_data.get("BankTransactions", [])
        logger.info(
            f"  Fetched {len(live_data['BankTransactions'])} bank transactions with full details"
        )

        # 5. Fetch Payments
        logger.info("Fetching Payments...")
        response = await client.get(f"{base_url}/Payments", headers=headers)
        response.raise_for_status()
        payments_data = response.json()
        live_data["Payments"] = payments_data.get("Payments", [])
        logger.info(f"  Fetched {len(live_data['Payments'])} payments")

        # 6. Fetch Balance Sheet Report
        logger.info("Fetching Balance Sheet Report...")
        response = await client.get(
            f"{base_url}/Reports/BalanceSheet",
            headers=headers,
            params={"date": "2024-10-31"},
        )
        response.raise_for_status()
        balance_sheet_data = response.json()
        live_data["BalanceSheet"] = (
            balance_sheet_data.get("Reports", [{}])[0] if balance_sheet_data.get("Reports") else {}
        )
        logger.info("  Fetched Balance Sheet report")

        # 7. Fetch Profit and Loss Report
        logger.info("Fetching Profit and Loss Report...")
        response = await client.get(
            f"{base_url}/Reports/ProfitAndLoss",
            headers=headers,
            params={"fromDate": "2024-01-01", "toDate": "2024-10-31"},
        )
        response.raise_for_status()
        pl_data = response.json()
        live_data["ProfitAndLoss"] = (
            pl_data.get("Reports", [{}])[0] if pl_data.get("Reports") else {}
        )
        logger.info("  Fetched Profit and Loss report")

    return live_data


def load_synthetic_data() -> dict[str, Any]:
    """
    Load synthetic data from JSON file.

    Returns:
        Dict containing synthetic data for all endpoints
    """
    synthetic_path = Path(__file__).parent.parent / "src/mcp_servers/xero/data/synthetic_data.json"

    logger.info(f"Loading synthetic data from: {synthetic_path}")

    with open(synthetic_path) as f:
        return json.load(f)


# ============================================================================
# Comparison and Reporting
# ============================================================================


def compare_endpoint(live_sample: Any, synthetic_sample: Any, endpoint_name: str) -> dict[str, Any]:
    """
    Compare structure of live vs synthetic data for a single endpoint.

    Args:
        live_sample: Sample from live API
        synthetic_sample: Sample from synthetic data
        endpoint_name: Name of the endpoint

    Returns:
        Comparison results dict
    """
    live_keys = get_all_keys(live_sample)
    synthetic_keys = get_all_keys(synthetic_sample)

    return compare_keys(live_keys, synthetic_keys, endpoint_name)


async def main():
    """Main validation workflow."""
    print_section("Xero Synthetic Data Validation", "=")

    # ========================================================================
    # Step 1: Validate Configuration
    # ========================================================================
    print_section("Step 1: Validate Configuration", "-")

    if not config.xero_client_id:
        print("\nXERO_CLIENT_ID environment variable not set!")
        print("\nPlease set your Xero client ID:")
        print("  export XERO_CLIENT_ID='your-client-id'")
        return 1

    if not config.xero_tenant_id:
        print("\nXERO_TENANT_ID environment variable not set!")
        print("\nPlease run test_oauth_flow.py first to get your tenant ID, then:")
        print("  export XERO_TENANT_ID='your-tenant-id'")
        return 1

    print(f"\nClient ID: {config.xero_client_id[:8]}...")
    print(f"Tenant ID: {config.xero_tenant_id[:8]}...")
    print(f"Token storage: {config.token_storage_path}")

    # ========================================================================
    # Step 2: Initialize OAuth Manager
    # ========================================================================
    print_section("Step 2: Initialize OAuth Manager", "-")

    token_store = TokenStore(config.token_storage_path)
    oauth_manager = OAuthManager(config, token_store)

    if not oauth_manager.has_valid_tokens():
        print("\nNo valid OAuth tokens found!")
        print("\nPlease run test_oauth_flow.py first to authenticate:")
        print("  python test_oauth_flow.py")
        return 1

    print("\nOAuth manager initialized")
    print("Valid tokens found")

    # ========================================================================
    # Step 3: Fetch Live Data from Xero API
    # ========================================================================
    print_section("Step 3: Fetch Live Data from Xero API", "-")

    try:
        live_data = await fetch_live_data(oauth_manager)
        print("\nSuccessfully fetched live data from all 7 endpoints")

        # Save live API responses to file for reference
        live_data_path = Path(__file__).parent.parent / "live_api_responses.json"
        with open(live_data_path, "w") as f:
            json.dump(live_data, f, indent=2)
        print(f"Live API responses saved to: {live_data_path}")
    except httpx.HTTPError as e:
        print("\nFailed to fetch live data from Xero API")
        print(f"HTTP Error: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Status: {e.response.status_code}")
            print(f"Response: {e.response.text[:500]}")
        return 1
    except Exception as e:
        print("\nUnexpected error while fetching live data")
        print(f"Error: {e}")
        return 1

    # ========================================================================
    # Step 4: Load Synthetic Data
    # ========================================================================
    print_section("Step 4: Load Synthetic Data", "-")

    try:
        synthetic_data = load_synthetic_data()
        print("\nSuccessfully loaded synthetic data")
    except Exception as e:
        print("\nFailed to load synthetic data")
        print(f"Error: {e}")
        return 1

    # ========================================================================
    # Step 5: Compare Structures
    # ========================================================================
    print_section("Step 5: Compare Structures", "-")

    print("\nComparing live API responses vs synthetic data...\n")

    comparisons = []

    # Compare each endpoint
    endpoints = [
        ("Accounts", "Accounts"),
        ("Contacts", "Contacts"),
        ("Invoices", "Invoices"),
        ("BankTransactions", "BankTransactions"),
        ("Payments", "Payments"),
        ("BalanceSheet", "Reports.BalanceSheet"),
        ("ProfitAndLoss", "Reports.ProfitAndLoss"),
    ]

    for live_key, synthetic_key in endpoints:
        # Get samples
        if "Reports." in synthetic_key:
            report_name = synthetic_key.split(".")[1]
            live_sample = live_data.get(report_name, {})
            synthetic_sample = synthetic_data.get("Reports", {}).get(report_name, {})
        else:
            live_sample = live_data.get(live_key, [])
            synthetic_sample = synthetic_data.get(synthetic_key, [])

        # Get first item for list endpoints
        if isinstance(live_sample, list) and live_sample:
            live_sample = live_sample[0]
        if isinstance(synthetic_sample, list) and synthetic_sample:
            synthetic_sample = synthetic_sample[0]

        # Compare
        comparison = compare_endpoint(live_sample, synthetic_sample, live_key)
        comparisons.append(comparison)

    # ========================================================================
    # Step 6: Generate Report
    # ========================================================================
    print_section("VALIDATION REPORT", "=")

    all_valid = True

    for comp in comparisons:
        endpoint = comp["endpoint"]
        is_valid = comp["is_valid"]
        matched_count = comp["matched_count"]
        missing = comp["missing_in_synthetic"]
        extra = comp["extra_in_synthetic"]

        status = "VALID" if is_valid else "MISMATCH"
        all_valid = all_valid and is_valid

        print(f"\n{'─' * 70}")
        print(f"Endpoint: {endpoint}")
        print(f"Status: {status}")
        print(f"{'─' * 70}")
        print(f"  Matched fields: {matched_count}")

        if missing:
            print(f"\n  Missing in synthetic ({len(missing)} fields):")
            for key in missing[:10]:  # Show first 10
                print(f"    - {key}")
            if len(missing) > 10:
                print(f"    ... and {len(missing) - 10} more")

        if extra:
            print(f"\n  Extra in synthetic ({len(extra)} fields):")
            for key in extra[:10]:  # Show first 10
                print(f"    - {key}")
            if len(extra) > 10:
                print(f"    ... and {len(extra) - 10} more")

        if is_valid:
            print("\n  Structure matches perfectly!")

    # ========================================================================
    # Summary
    # ========================================================================
    print_section("SUMMARY", "=")

    total_endpoints = len(comparisons)
    valid_endpoints = sum(1 for c in comparisons if c["is_valid"])
    invalid_endpoints = total_endpoints - valid_endpoints

    print(f"\nTotal endpoints validated: {total_endpoints}")
    print(f"Valid: {valid_endpoints}")
    print(f"Invalid: {invalid_endpoints}")

    if all_valid:
        print("\n" + "=" * 70)
        print("  All synthetic data structures match live API!")
        print("=" * 70)
        print("\nNext steps:")
        print("  1. Close the GitHub issue as validated")
        print("  2. Proceed with implementing OnlineProvider endpoints")
        return 0
    else:
        print("\n" + "=" * 70)
        print("  ATTENTION: Mismatches found in synthetic data")
        print("=" * 70)
        print("\nNext steps:")
        print("  1. Review the mismatches above")
        print("  2. Update src/mcp_servers/xero/data/synthetic_data.json")
        print("  3. Re-run this script to verify fixes")
        print("  4. Document changes in PR description")

        # Save detailed report to file
        report_path = Path(__file__).parent.parent / "validation_report.json"
        with open(report_path, "w") as f:
            json.dump(comparisons, f, indent=2)
        print(f"\nDetailed report saved to: {report_path}")

        return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nValidation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nValidation failed with error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
