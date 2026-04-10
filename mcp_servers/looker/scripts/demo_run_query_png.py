#!/usr/bin/env python
"""Demo script showing the run_query_png workflow.

This script demonstrates the exact workflow requested:
1. Create a Looker query on model=nyc_311
2. Run the query as a PNG visualization and save to file
3. The model can then read the PNG to verify it was created correctly

Usage:
    uv run python scripts/demo_run_query_png.py
"""

import asyncio
import base64
import sys
from pathlib import Path

# Add looker server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "looker"))

from models import CreateQueryRequest, RunQueryPngRequest
from tools.query_execution import create_query, run_query_png


async def main():
    print("=" * 60)
    print("Demo: run_query_png Workflow")
    print("=" * 60)

    # Step 1: Create a Looker query
    print("\n[Step 1] Creating query...")
    query_response = await create_query(
        CreateQueryRequest(
            model="nyc_311",
            view="service_requests",
            fields=["service_requests.borough", "service_requests.count"],
            limit=500,
            vis_config={"type": "looker_column"},
        )
    )
    query_id = query_response.query.id
    print(f"  Created query ID: {query_id}")
    print(f"  Model: {query_response.query.model}")
    print(f"  View: {query_response.query.view}")
    print(f"  Fields: {query_response.query.fields}")
    print(f"  Vis Config: {query_response.query.vis_config}")

    # Step 2: Run the query as PNG
    print("\n[Step 2] Running query as PNG...")
    png_response = await run_query_png(
        RunQueryPngRequest(
            query_id=query_id,
            width=800,
            height=600,
        )
    )
    print(f"  Query ID: {png_response.query_id}")
    print(f"  Chart Type: {png_response.chart_type}")
    print(f"  Dimensions: {png_response.width}x{png_response.height}")
    print(f"  Content Type: {png_response.content_type}")
    print(f"  Image Data Size: {len(png_response.image_data)} chars (base64)")

    # Step 3: Save to file
    outfile = Path("nyc311_chart.png")
    print(f"\n[Step 3] Saving to file: {outfile}")
    with open(outfile, "wb") as f:
        image_bytes = base64.b64decode(png_response.image_data)
        f.write(image_bytes)
    print(f"  File size: {outfile.stat().st_size} bytes")

    # Step 4: Verify the file
    print("\n[Step 4] Verifying PNG file...")
    with open(outfile, "rb") as f:
        header = f.read(8)
    if header == b"\x89PNG\r\n\x1a\n":
        print("  Valid PNG header detected")
    else:
        print(f"  ERROR: Invalid PNG header: {header}")

    print("\n" + "=" * 60)
    print(f"SUCCESS! Chart saved to: {outfile.absolute()}")
    print("=" * 60)

    # Also create different chart types
    print("\n\nBonus: Creating additional chart types...")
    chart_types = [
        ("looker_bar", "nyc311_bar_chart.png"),
        ("looker_pie", "nyc311_pie_chart.png"),
        ("looker_line", "nyc311_line_chart.png"),
    ]

    for chart_type, filename in chart_types:
        query_resp = await create_query(
            CreateQueryRequest(
                model="nyc_311",
                view="service_requests",
                fields=["service_requests.borough", "service_requests.count"],
                limit=10,
                vis_config={"type": chart_type},
            )
        )
        png_resp = await run_query_png(RunQueryPngRequest(query_id=query_resp.query.id))

        outfile = Path(filename)
        with open(outfile, "wb") as f:
            f.write(base64.b64decode(png_resp.image_data))
        print(f"  {chart_type}: {filename} ({outfile.stat().st_size} bytes)")

    print("\nAll charts created successfully!")


if __name__ == "__main__":
    asyncio.run(main())
