#!/usr/bin/env python3
"""
Generate guide.json from wiki SME Guide markdown file.

This script reads the SME Guide markdown file from the wiki directory
and generates a guide.json file for the UI to consume.

Usage:
    python scripts/generate_guide_json.py --server taxjar
    python scripts/generate_guide_json.py --server taxjar --wiki-dir wiki --output ui/taxjar/public
"""

import argparse
import json
import sys
from pathlib import Path


def find_guide_file(wiki_dir: Path, server_name: str) -> Path | None:
    """
    Find the SME Guide markdown file in the wiki directory.

    Tries multiple naming conventions:
    - {ServerName}-SME-Guide.md (TaxJar-SME-Guide.md)
    - {SERVER_NAME}-SME-Guide.md (TAXJAR-SME-Guide.md)
    - {server_name}-SME-Guide.md (taxjar-SME-Guide.md)

    Args:
        wiki_dir: Path to wiki directory
        server_name: Server name (e.g., "taxjar")

    Returns:
        Path to guide file if found, None otherwise
    """
    # Try different naming conventions
    variations = [
        f"{server_name.title()}-SME-Guide.md",  # TaxJar-SME-Guide.md
        f"{server_name.upper()}-SME-Guide.md",  # TAXJAR-SME-Guide.md
        f"{server_name.lower()}-SME-Guide.md",  # taxjar-SME-Guide.md
        f"{server_name}-SME-Guide.md",  # As-is
    ]

    for filename in variations:
        guide_path = wiki_dir / filename
        if guide_path.exists():
            return guide_path

    # Also try case-insensitive search
    for f in wiki_dir.iterdir():
        if f.name.lower() == f"{server_name.lower()}-sme-guide.md":
            return f

    return None


def generate_guide_json(guide_path: Path) -> str:
    """
    Generate guide.json content from markdown file.

    Args:
        guide_path: Path to the SME Guide markdown file

    Returns:
        JSON string with the guide content
    """
    content = guide_path.read_text(encoding="utf-8")
    return json.dumps({"content": content})


def main():
    parser = argparse.ArgumentParser(description="Generate guide.json from wiki SME Guide markdown")
    parser.add_argument(
        "--server",
        required=True,
        help="Server name (e.g., taxjar)",
    )
    parser.add_argument(
        "--wiki-dir",
        default="wiki",
        help="Path to wiki directory (default: wiki)",
    )
    parser.add_argument(
        "--output",
        help="Output directory for guide.json (default: ui/{server}/public)",
    )

    args = parser.parse_args()

    # Resolve paths
    repo_root = Path(__file__).parent.parent
    wiki_dir = repo_root / args.wiki_dir
    output_dir = Path(args.output) if args.output else repo_root / "ui" / args.server / "public"

    # Find guide file
    guide_path = find_guide_file(wiki_dir, args.server)
    if guide_path is None:
        print(f"Error: Could not find SME Guide for '{args.server}' in {wiki_dir}")
        print(f"  Expected: {args.server.title()}-SME-Guide.md or similar")
        sys.exit(1)

    print(f"Found guide file: {guide_path}")

    # Generate JSON
    guide_json = generate_guide_json(guide_path)

    # Write output
    output_path = output_dir / "guide.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(guide_json, encoding="utf-8")

    print(f"Generated: {output_path}")
    print("Done!")


if __name__ == "__main__":
    main()
