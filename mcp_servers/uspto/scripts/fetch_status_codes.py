#!/usr/bin/env python3
"""Fetch USPTO status codes from the API and save to offline data file.

This script fetches all 241 USPTO patent application status codes from the
live API and saves them to a JSON file for offline mode usage.

Usage:
    uv run python scripts/fetch_status_codes.py

Requirements:
    - USPTO API key set in environment or passed via --api-key
    - Internet connectivity
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp_servers.uspto.api.client import USPTOAPIClient  # noqa: E402


async def fetch_status_codes(api_key: str | None = None) -> dict:
    """Fetch all status codes from USPTO API.

    Args:
        api_key: Optional API key. If not provided, uses environment variable.

    Returns:
        Raw USPTO response with statusCodeBag and metadata.
    """
    # Get API key from argument or environment
    key = api_key or os.environ.get("USPTO_API_KEY")
    if not key:
        print("Error: USPTO_API_KEY environment variable not set and no --api-key provided")
        sys.exit(1)

    # Create client in ONLINE mode (offline_mode=False)
    client = USPTOAPIClient(api_key=key, offline_mode=False)

    try:
        print("Fetching status codes from USPTO API...")
        result = await client.get_status_codes()

        if "error" in result:
            print(f"Error from API: {result['error']}")
            sys.exit(1)

        return result
    finally:
        await client.aclose()


def save_status_codes(data: dict, output_path: Path) -> None:
    """Save status codes to JSON file.

    Args:
        data: Status codes data from API.
        output_path: Path to save JSON file.
    """
    import datetime

    # Create directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Extract raw response if present (before transformation)
    raw_data = data.get("raw_uspto_response", data)

    # Ensure version is present (USPTO API doesn't always include it)
    if "version" not in raw_data:
        raw_data["version"] = datetime.date.today().isoformat()

    # Save with nice formatting
    with open(output_path, "w") as f:
        json.dump(raw_data, f, indent=2)

    print(f"Saved {len(raw_data.get('statusCodeBag', []))} status codes to {output_path}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch USPTO status codes for offline mode")
    parser.add_argument(
        "--api-key",
        help="USPTO API key (or set USPTO_API_KEY env var)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "data/uspto/status_codes.json",
        help="Output file path",
    )
    args = parser.parse_args()

    # Fetch from API
    data = await fetch_status_codes(args.api_key)

    # Save to file
    save_status_codes(data, args.output)

    # Print summary
    codes = data.get("statusCodes", data.get("statusCodeBag", []))
    print(f"\nTotal status codes: {len(codes)}")
    if codes:
        print(f"Sample: {codes[0]}")


if __name__ == "__main__":
    asyncio.run(main())
