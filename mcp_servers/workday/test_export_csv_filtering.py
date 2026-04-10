#!/usr/bin/env python3
"""Test script for enhanced export_csv with filtering.

Tests the new filtering capability by:
1. Creating sample organizations and cost centers
2. Exporting cost_centers table WITHOUT filters (all rows)
3. Exporting cost_centers table WITH filter by org_id
4. Verifying filtered results only contain matching rows
"""

import asyncio
import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent / "mcp_servers" / "workday"))

from db.models import CostCenter, SupervisoryOrg
from db.session import get_async_session, init_db
from mcp_middleware.db_tools import CSVExportRequest, export_db_to_csv


async def setup_test_data():
    """Create test organizations and cost centers."""
    print("📝 Setting up test data...")

    async with get_async_session() as session:
        # Create test organizations
        org1 = SupervisoryOrg(
            org_id="ORG001",
            org_name="Engineering Department",
            org_type="Supervisory",
        )
        org2 = SupervisoryOrg(
            org_id="ORG002",
            org_name="Sales Department",
            org_type="Supervisory",
        )

        # Create cost centers for org1
        cc1 = CostCenter(
            cost_center_id="CC001",
            cost_center_name="Backend Engineering",
            org_id="ORG001",
        )
        cc2 = CostCenter(
            cost_center_id="CC002",
            cost_center_name="Frontend Engineering",
            org_id="ORG001",
        )

        # Create cost centers for org2
        cc3 = CostCenter(
            cost_center_id="CC003",
            cost_center_name="Enterprise Sales",
            org_id="ORG002",
        )
        cc4 = CostCenter(
            cost_center_id="CC004",
            cost_center_name="SMB Sales",
            org_id="ORG002",
        )

        # Add orgs first and commit (cost_centers have FK to orgs)
        session.add_all([org1, org2])
        await session.commit()

        # Now add cost centers
        session.add_all([cc1, cc2, cc3, cc4])
        await session.commit()

    print("✅ Created 2 organizations and 4 cost centers")
    print("   - ORG001 (Engineering): CC001, CC002")
    print("   - ORG002 (Sales): CC003, CC004")


async def test_export_without_filter(engine):
    """Test exporting cost_centers without filters (all rows)."""
    print("\n🧪 Test 1: Export cost_centers WITHOUT filter")

    request = CSVExportRequest(
        table_name="cost_centers",
        include_headers=True,
    )

    result = await export_db_to_csv(request, engine)

    print(f"   Row count: {result.row_count}")
    print(f"   Message: {result.message}")

    # Parse CSV to count rows
    lines = result.csv_content.strip().split("\n")
    _header = lines[0]  # noqa: F841
    data_rows = lines[1:]

    print("   CSV preview:")
    for line in lines[:5]:  # Show first 5 lines
        print(f"     {line}")  # noqa: F541

    assert result.row_count == 4, f"Expected 4 rows, got {result.row_count}"
    assert len(data_rows) == 4, f"Expected 4 data rows in CSV, got {len(data_rows)}"

    print("   ✅ Test passed: Got all 4 cost centers")


async def test_export_with_org_filter(engine):
    """Test exporting cost_centers filtered by org_id."""
    print("\n🧪 Test 2: Export cost_centers WITH org_id filter")

    request = CSVExportRequest(
        table_name="cost_centers",
        include_headers=True,
        filters={"org_id": "ORG001"},  # Only Engineering cost centers
    )

    result = await export_db_to_csv(request, engine)

    print(f"   Row count: {result.row_count}")
    print(f"   Message: {result.message}")

    # Parse CSV to verify filtering
    lines = result.csv_content.strip().split("\n")
    _header = lines[0]  # noqa: F841
    data_rows = lines[1:]

    print("   CSV content:")
    for line in lines:
        print(f"     {line}")

    # Verify all rows have org_id=ORG001
    for row in data_rows:
        assert "ORG001" in row, f"Row should contain ORG001: {row}"
        assert "ORG002" not in row, f"Row should NOT contain ORG002: {row}"

    assert result.row_count == 2, f"Expected 2 rows for ORG001, got {result.row_count}"
    assert len(data_rows) == 2, f"Expected 2 data rows in CSV, got {len(data_rows)}"

    print("   ✅ Test passed: Got only 2 cost centers for ORG001")


async def test_export_with_limit(engine):
    """Test exporting cost_centers with a limit."""
    print("\n🧪 Test 3: Export cost_centers WITH limit=2")

    request = CSVExportRequest(
        table_name="cost_centers",
        include_headers=True,
        limit=2,
    )

    result = await export_db_to_csv(request, engine)

    print(f"   Row count: {result.row_count}")
    print(f"   Message: {result.message}")

    lines = result.csv_content.strip().split("\n")
    data_rows = lines[1:]

    assert result.row_count == 2, f"Expected 2 rows due to limit, got {result.row_count}"
    assert len(data_rows) == 2, f"Expected 2 data rows in CSV, got {len(data_rows)}"

    print("   ✅ Test passed: Limit correctly applied")


async def test_export_with_filter_and_limit(engine):
    """Test combining filters and limit."""
    print("\n🧪 Test 4: Export cost_centers WITH filter AND limit")

    request = CSVExportRequest(
        table_name="cost_centers",
        include_headers=True,
        filters={"org_id": "ORG001"},
        limit=1,
    )

    result = await export_db_to_csv(request, engine)

    print(f"   Row count: {result.row_count}")
    print(f"   Message: {result.message}")

    lines = result.csv_content.strip().split("\n")
    data_rows = lines[1:]

    print("   CSV content:")
    for line in lines:
        print(f"     {line}")

    assert result.row_count == 1, f"Expected 1 row, got {result.row_count}"
    assert "ORG001" in data_rows[0], "Row should contain ORG001"

    print("   ✅ Test passed: Filter + limit work together")


async def test_invalid_filter_column(engine):
    """Test that invalid filter columns are rejected."""
    print("\n🧪 Test 5: Test invalid filter column rejection")

    request = CSVExportRequest(
        table_name="cost_centers",
        filters={"invalid_column": "value"},  # This column doesn't exist
    )

    try:
        await export_db_to_csv(request, engine)
        assert False, "Should have raised ValueError for invalid column"
    except ValueError as e:
        print(f"   Expected error caught: {e}")
        assert "Invalid filter column" in str(e)
        print("   ✅ Test passed: Invalid columns rejected")


async def main():
    """Run all tests."""
    print("=" * 70)
    print("Testing Enhanced export_csv with Filtering")
    print("=" * 70)

    # Initialize database
    print("\n🔧 Initializing database...")
    init_db()

    # Get async engine from session
    from db.session import get_async_engine

    engine = get_async_engine()

    # Clean up existing data
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("DELETE FROM cost_centers"))
        await conn.execute(text("DELETE FROM supervisory_orgs"))

    # Setup test data
    await setup_test_data()

    # Run tests
    try:
        await test_export_without_filter(engine)
        await test_export_with_org_filter(engine)
        await test_export_with_limit(engine)
        await test_export_with_filter_and_limit(engine)
        await test_invalid_filter_column(engine)

        print("\n" + "=" * 70)
        print("✅ ALL TESTS PASSED!")
        print("=" * 70)
        print("\n📊 Summary:")
        print("   - Unfiltered export: ✅")
        print("   - Filtered by org_id: ✅")
        print("   - Limit parameter: ✅")
        print("   - Combined filter + limit: ✅")
        print("   - Invalid column rejection: ✅")
        print("\n🎉 The enhanced export_csv is ready for DynamicSelect!")

    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
