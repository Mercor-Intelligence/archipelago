#!/usr/bin/env python3
"""Local test script for Tableau verifier.

Run with: python -m runner.evals.tableau.test_local
"""

import asyncio
import io
import sqlite3
import tempfile
import zipfile
from typing import Any

from runner.evals.tableau.field_check import (
    FIELD_MAPPINGS,
    _compare_values,
    _execute_operation,
)
from runner.helpers.tableau_state.main import tableau_state_helper


def create_mock_tableau_db() -> bytes:
    """Create a mock Tableau SQLite database with sample data."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        conn = sqlite3.connect(f.name)
        cursor = conn.cursor()

        # Create tables
        cursor.execute("""
            CREATE TABLE sites (
                id TEXT PRIMARY KEY,
                name TEXT,
                content_url TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                name TEXT,
                email TEXT,
                site_role TEXT,
                site_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE workbooks (
                id TEXT PRIMARY KEY,
                name TEXT,
                site_id TEXT,
                project_id TEXT,
                owner_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT,
                site_id TEXT,
                description TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE datasources (
                id TEXT PRIMARY KEY,
                name TEXT,
                site_id TEXT,
                project_id TEXT,
                owner_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE views (
                id TEXT PRIMARY KEY,
                name TEXT,
                workbook_id TEXT,
                owner_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE groups (
                id TEXT PRIMARY KEY,
                name TEXT,
                site_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # Insert sample data (based on task ug5h1e7b)
        site_id = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

        cursor.execute(
            "INSERT INTO sites VALUES (?, ?, ?, ?, ?)",
            (site_id, "Default Site", "default", "2025-01-01", "2025-01-01"),
        )

        # Users (3 GP users as per task)
        users = [
            (
                "u1",
                "Dr. Alice Diaz_CareConnect Telehealth Hub",
                "alice@example.com",
                "Viewer",
                site_id,
            ),
            (
                "u2",
                "Dr. Bob Smith_Multilingual Family Telehealth",
                "bob@example.com",
                "Viewer",
                site_id,
            ),
            (
                "u3",
                "Dr. Carol Jones_SummitLink GP Network",
                "carol@example.com",
                "Viewer",
                site_id,
            ),
            ("u4", "admin", "admin@example.com", "Creator", site_id),
        ]
        for u in users:
            cursor.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
                (u[0], u[1], u[2], u[3], u[4], "2025-01-01", "2025-01-01"),
            )

        # Workbook
        cursor.execute(
            "INSERT INTO workbooks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "wb1",
                "virtual_GP_report",
                site_id,
                "proj1",
                "u4",
                "2025-01-01",
                "2025-01-01",
            ),
        )

        # Project
        cursor.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
            (
                "proj1",
                "Default Project",
                site_id,
                "Main project",
                "2025-01-01",
                "2025-01-01",
            ),
        )

        conn.commit()
        conn.close()

        with open(f.name, "rb") as db_file:
            return db_file.read()


def create_mock_snapshot(db_bytes: bytes) -> io.BytesIO:
    """Create a mock snapshot zip containing the database."""
    snapshot = io.BytesIO()
    with zipfile.ZipFile(snapshot, "w") as zf:
        zf.writestr(".apps_data/tableau/data.db", db_bytes)
    snapshot.seek(0)
    return snapshot


async def test_tableau_helper():
    """Test the Tableau state helper."""
    print("=" * 60)
    print("Testing Tableau State Helper")
    print("=" * 60)

    db_bytes = create_mock_tableau_db()
    snapshot = create_mock_snapshot(db_bytes)

    # Create mock trajectory (not used but required)
    state = await tableau_state_helper(
        initial_snapshot_bytes=io.BytesIO(),
        final_snapshot_bytes=snapshot,
        trajectory=None,  # pyright: ignore[reportArgumentType]
    )

    print("\nExtracted state:")
    for entity, records in state.items():
        print(f"  {entity}: {len(records)} records")
        if records:
            print(f"    Sample: {records[0]}")

    return state


def test_field_checks(state: dict[str, Any]) -> bool:
    """Test various field check operations."""
    print("\n" + "=" * 60)
    print("Testing Field Check Operations")
    print("=" * 60)

    test_cases = [
        # (field_to_check, verifier_values, expected_value, should_pass)
        ("User Count", {}, "4", True),
        ("User Count", {}, "3", False),  # Should fail - we have 4 users
        ("User Count by Role", {"site_role": "Viewer"}, "3", True),
        ("User Count by Role", {"site_role": "Creator"}, "1", True),
        ("Workbook Exists", {"workbook_name": "virtual_GP_report"}, "true", True),
        ("Workbook Exists", {"workbook_name": "nonexistent"}, "true", False),
        (
            "User Exists",
            {"user_name": "Dr. Alice Diaz_CareConnect Telehealth Hub"},
            "true",
            True,
        ),
        (
            "User Site Role",
            {"user_name": "Dr. Alice Diaz_CareConnect Telehealth Hub"},
            "Viewer",
            True,
        ),
        (
            "Site Exists by ID",
            {"site_id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"},
            "true",
            True,
        ),
    ]

    passed = 0
    failed = 0

    for field_to_check, verifier_values, expected_value, should_pass in test_cases:
        field_config = FIELD_MAPPINGS.get(field_to_check)
        if not field_config:
            print(f"  ❌ Unknown field: {field_to_check}")
            failed += 1
            continue

        data = state.get(field_config["data_source"], [])
        result = _execute_operation(data, field_config, verifier_values)
        matches = _compare_values(result, expected_value, None)

        status = "✅" if matches == should_pass else "❌"
        if matches != should_pass:
            failed += 1
        else:
            passed += 1

        print(f"  {status} {field_to_check}")
        print(f"      Params: {verifier_values}")
        print(f"      Result: {result} (expected: {expected_value})")
        print(f"      Matches: {matches} (should_pass: {should_pass})")

    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


async def main():
    """Run all tests."""
    print("\n🧪 Tableau Verifier Local Tests\n")

    state = await test_tableau_helper()
    success = test_field_checks(state)

    print("\n" + "=" * 60)
    if success:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
