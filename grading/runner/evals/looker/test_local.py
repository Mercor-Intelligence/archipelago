#!/usr/bin/env python3
"""Local test script for Looker verifier.

Run with: python -m runner.evals.looker.test_local
"""

import asyncio
import io
import sqlite3
import tempfile
import zipfile
from decimal import Decimal
from typing import Any

from runner.evals.looker.field_check import FIELD_MAPPINGS, _execute_operation
from runner.evals.looker.utils import compare_values
from runner.helpers.looker_state.main import cleanup_looker_state, looker_state_helper


def create_mock_looker_db() -> bytes:
    """Create a mock Looker SQLite database with sample data.

    Based on MI42 (Churn Prediction) task data.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        conn = sqlite3.connect(f.name)
        cursor = conn.cursor()

        # Create customer_transaction_data table (MI42 task)
        cursor.execute("""
            CREATE TABLE customer_transaction_data (
                transaction_id INTEGER PRIMARY KEY,
                customer_id TEXT,
                transaction_date TEXT,
                amount REAL,
                churned INTEGER
            )
        """)

        # Insert sample data to simulate:
        # - train_churn_rate: ~36% (based on time split)
        # - test_churn_rate: ~83%
        # - test_transaction_count: 2887

        # Training data (70% - earlier dates, ~36% churn)
        train_data = []
        for i in range(6737):  # ~70% of total
            churned = 1 if i % 3 == 0 else 0  # ~33% churn rate
            train_data.append(
                (
                    i,
                    f"cust_{i % 100}",
                    "2024-01-15",
                    100.0 + (i % 50),
                    churned,
                )
            )

        # Test data (30% - later dates, ~83% churn)
        test_data = []
        for i in range(2887):  # test_transaction_count = 2887
            churned = 1 if i % 6 != 0 else 0  # ~83% churn rate
            test_data.append(
                (
                    6737 + i,
                    f"cust_{i % 100}",
                    "2024-06-15",
                    150.0 + (i % 50),
                    churned,
                )
            )

        cursor.executemany(
            "INSERT INTO customer_transaction_data VALUES (?, ?, ?, ?, ?)",
            train_data + test_data,
        )

        # Create sensor_readings table (MI41 task)
        cursor.execute("""
            CREATE TABLE sensor_readings (
                reading_id INTEGER PRIMARY KEY,
                sensor_id TEXT,
                timestamp TEXT,
                temperature REAL,
                uptime_pct REAL
            )
        """)

        # Insert sample sensor data
        sensors = ["SENSOR-001", "SENSOR-002", "SENSOR-012", "SENSOR-015"]
        for i, sensor_id in enumerate(sensors):
            # SENSOR-012 has lowest uptime
            uptime = 85.0 if sensor_id != "SENSOR-012" else 82.5
            for j in range(100):
                cursor.execute(
                    "INSERT INTO sensor_readings VALUES (?, ?, ?, ?, ?)",
                    (
                        i * 100 + j,
                        sensor_id,
                        f"2024-01-{(j % 28) + 1:02d}",
                        20.0 + (j % 10) * 0.1,
                        uptime + (j % 5) * 0.5,
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
        zf.writestr(".apps_data/looker/data.db", db_bytes)
    snapshot.seek(0)
    return snapshot


async def test_looker_helper():
    """Test the Looker state helper."""
    print("=" * 60)
    print("Testing Looker State Helper")
    print("=" * 60)

    db_bytes = create_mock_looker_db()
    snapshot = create_mock_snapshot(db_bytes)

    # Create mock trajectory (not used but required by signature)
    state = await looker_state_helper(
        initial_snapshot_bytes=io.BytesIO(),
        final_snapshot_bytes=snapshot,
        trajectory=None,  # pyright: ignore[reportArgumentType]
    )

    print("\nExtracted state:")
    print(f"  Databases: {list(state['databases'].keys())}")
    for alias, tables in state["tables"].items():
        print(f"  Tables in '{alias}': {tables}")
        for table in tables:
            count = state["row_counts"][alias].get(table, 0)
            print(f"    {table}: {count} rows")

    return state


def test_field_checks(state: dict[str, Any]) -> bool:
    """Test various field check operations."""
    print("\n" + "=" * 60)
    print("Testing Field Check Operations")
    print("=" * 60)

    test_cases = [
        # (field_to_check, verifier_values, expected_value, tolerance, should_pass)
        (
            "Table Exists",
            {"table_name": "customer_transaction_data"},
            "true",
            None,
            True,
        ),
        (
            "Table Exists",
            {"table_name": "nonexistent_table"},
            "true",
            None,
            False,
        ),
        (
            "Row Count",
            {"table_name": "customer_transaction_data"},
            "9624",  # 6737 train + 2887 test
            None,
            True,
        ),
        (
            "Row Count with Condition",
            {
                "table_name": "customer_transaction_data",
                "condition": "transaction_date = '2024-06-15'",
            },
            "2887",  # test_transaction_count
            None,
            True,
        ),
        (
            "SQL Query Result",
            {
                "sql": "SELECT COUNT(*) FROM customer_transaction_data WHERE transaction_date = '2024-06-15'"
            },
            "2887",
            None,
            True,
        ),
        (
            "Average",
            {"table_name": "sensor_readings", "field": "uptime_pct"},
            "86.0",  # Approximate average
            Decimal("5.0"),  # With tolerance
            True,
        ),
        (
            "SQL Query Result",
            {
                "sql": """
                    SELECT sensor_id FROM sensor_readings
                    GROUP BY sensor_id
                    ORDER BY AVG(uptime_pct) ASC
                    LIMIT 1
                """
            },
            "SENSOR-012",  # Lowest uptime sensor
            None,
            True,
        ),
    ]

    passed = 0
    failed = 0

    for (
        field_to_check,
        verifier_values,
        expected_value,
        tolerance,
        should_pass,
    ) in test_cases:
        field_config = FIELD_MAPPINGS.get(field_to_check)
        if not field_config:
            print(f"  ❌ Unknown field: {field_to_check}")
            failed += 1
            continue

        result, error = _execute_operation(state, field_config, verifier_values)

        if error:
            matches = False
            result_str = f"ERROR: {error}"
        else:
            matches = compare_values(result, expected_value, tolerance)
            result_str = str(result)

        status = "✅" if matches == should_pass else "❌"
        if matches != should_pass:
            failed += 1
        else:
            passed += 1

        print(f"  {status} {field_to_check}")
        print(f"      Params: {verifier_values}")
        print(f"      Result: {result_str} (expected: {expected_value})")
        if tolerance:
            print(f"      Tolerance: {tolerance}")
        print(f"      Matches: {matches} (should_pass: {should_pass})")

    print(f"\nResults: {passed} passed, {failed} failed")
    return failed == 0


async def main():
    """Run all tests."""
    print("\n🧪 Looker Verifier Local Tests\n")

    state = await test_looker_helper()

    try:
        success = test_field_checks(state)
    finally:
        # Clean up resources
        cleanup_looker_state(state)

    print("\n" + "=" * 60)
    if success:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
