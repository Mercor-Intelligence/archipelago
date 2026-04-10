"""Integration tests for headcount reconciliation.

Tests the headcount reconciliation formula:
    beginning_hc + hires - terminations + transfers_in - transfers_out = ending_hc

Validates per-org and total company reconciliation using:
- workday_hire_worker
- workday_terminate_worker
- workday_transfer_worker
- workday_report_headcount
"""

import uuid
from datetime import date, timedelta

from .helpers import (
    DEMO_COST_CENTERS,
    DEMO_JOB_PROFILES,
    DEMO_ORGS,
    RestClient,
)


def generate_worker_id() -> str:
    """Generate a unique worker ID for testing."""
    return f"WRK-HC-{uuid.uuid4().hex[:8].upper()}"


def date_str(offset_days: int = 0) -> str:
    """Get a date as YYYY-MM-DD string, relative to today.

    Args:
        offset_days: Days to add/subtract from today (negative for past)

    Returns:
        Date string in YYYY-MM-DD format
    """
    return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class TestHeadcountReconciliation:
    """Tests for headcount reconciliation report.

    Tests the formula: beginning_hc + net_movement = ending_hc
    where: net_movement = hires - terminations + transfers_in - transfers_out
    """

    def test_headcount_reconciliation_by_org(self, rest_client: RestClient):
        """Test headcount reconciliation across multiple orgs with movements.

        Scenario:
        - Create 3 workers in ORG-ENG-BACKEND (before test period, for termination)
        - 5 hires in ORG-ENG (during period)
        - 3 terminations in ORG-ENG-BACKEND (during period)
        - 2 transfers from ORG-ENG to ORG-ENG-FRONTEND (during period)

        Validates: beginning_hc + net_movement = ending_hc for each org
        """
        # Define test period dates
        period_start = date_str(-30)  # 30 days ago
        period_end = date_str(0)  # today
        before_period = date_str(-60)  # 60 days ago (for initial hires)
        hire_date = date_str(-15)  # 15 days ago (within period)
        term_date = date_str(-10)  # 10 days ago (within period)
        transfer_date = date_str(-5)  # 5 days ago (within period)

        # Step 1: Create 3 workers in ORG-ENG-BACKEND (before test period)
        # These will be terminated during the test period
        backend_workers = []
        for _ in range(3):
            worker_id = generate_worker_id()
            rest_client.call_tool(
                "workday_hire_worker",
                {
                    "worker_id": worker_id,
                    "job_profile_id": DEMO_JOB_PROFILES[2],  # JP-SWE-SR
                    "org_id": DEMO_ORGS[2],  # ORG-ENG-BACKEND
                    "cost_center_id": DEMO_COST_CENTERS[2],  # CC-2100
                    "hire_date": before_period,
                },
            )
            backend_workers.append(worker_id)

        # Step 2: Hire 5 workers in ORG-ENG (during period)
        eng_workers = []
        for _ in range(5):
            worker_id = generate_worker_id()
            rest_client.call_tool(
                "workday_hire_worker",
                {
                    "worker_id": worker_id,
                    "job_profile_id": DEMO_JOB_PROFILES[3],  # JP-SWE-MID
                    "org_id": DEMO_ORGS[1],  # ORG-ENG
                    "cost_center_id": DEMO_COST_CENTERS[1],  # CC-2000
                    "hire_date": hire_date,
                },
            )
            eng_workers.append(worker_id)

        # Step 3: Terminate 3 workers in ORG-ENG-BACKEND
        for worker_id in backend_workers:
            rest_client.call_tool(
                "workday_terminate_worker",
                {
                    "worker_id": worker_id,
                    "termination_date": term_date,
                },
            )

        # Step 4: Transfer 2 workers from ORG-ENG to ORG-ENG-FRONTEND
        for worker_id in eng_workers[:2]:
            rest_client.call_tool(
                "workday_transfer_worker",
                {
                    "worker_id": worker_id,
                    "new_org_id": DEMO_ORGS[3],  # ORG-ENG-FRONTEND
                    "new_cost_center_id": DEMO_COST_CENTERS[3],  # CC-2200
                    "transfer_date": transfer_date,
                },
            )

        # Step 5: Run headcount report for the period
        report = rest_client.call_tool(
            "workday_report_headcount",
            {
                "start_date": period_start,
                "end_date": period_end,
                "group_by": "org_id",
            },
        )

        # Step 6: Validate report structure
        assert "report" in report
        assert "total_count" in report
        assert "start_date" in report
        assert "end_date" in report
        assert report["group_by"] == "org_id"

        # Step 7: Validate reconciliation formula for each org
        report_by_org = {row["group_id"]: row for row in report["report"]}

        # Verify all expected orgs appear in the report
        assert "ORG-ENG" in report_by_org, (
            "ORG-ENG should appear in headcount report after creating workers"
        )
        assert "ORG-ENG-BACKEND" in report_by_org, (
            "ORG-ENG-BACKEND should appear in headcount report after creating workers"
        )
        assert "ORG-ENG-FRONTEND" in report_by_org, (
            "ORG-ENG-FRONTEND should appear in headcount report after transfers"
        )

        # ORG-ENG: +5 hires, -2 transfers out
        eng_row = report_by_org["ORG-ENG"]
        assert eng_row["hires"] >= 5, "ORG-ENG should have at least 5 hires"
        assert eng_row["transfers_out"] >= 2, "ORG-ENG should have at least 2 transfers out"
        # Validate reconciliation formula
        expected_ending = eng_row["beginning_hc"] + eng_row["net_movement"]
        assert eng_row["ending_hc"] == expected_ending, (
            f"Reconciliation formula failed for ORG-ENG: "
            f"{eng_row['beginning_hc']} + {eng_row['net_movement']} != {eng_row['ending_hc']}"
        )
        # Validate net_movement calculation
        expected_net = (
            eng_row["hires"]
            - eng_row["terminations"]
            + eng_row["transfers_in"]
            - eng_row["transfers_out"]
        )
        assert eng_row["net_movement"] == expected_net, (
            f"Net movement calculation failed for ORG-ENG: "
            f"expected {expected_net}, got {eng_row['net_movement']}"
        )

        # ORG-ENG-BACKEND: -3 terminations
        backend_row = report_by_org["ORG-ENG-BACKEND"]
        assert backend_row["terminations"] >= 3, (
            "ORG-ENG-BACKEND should have at least 3 terminations"
        )
        # Validate reconciliation formula
        expected_ending = backend_row["beginning_hc"] + backend_row["net_movement"]
        assert backend_row["ending_hc"] == expected_ending, (
            f"Reconciliation failed for ORG-ENG-BACKEND: "
            f"{backend_row['beginning_hc']} + {backend_row['net_movement']} "
            f"!= {backend_row['ending_hc']}"
        )

        # ORG-ENG-FRONTEND: +2 transfers in
        frontend_row = report_by_org["ORG-ENG-FRONTEND"]
        assert frontend_row["transfers_in"] >= 2, (
            "ORG-ENG-FRONTEND should have at least 2 transfers in"
        )
        # Validate reconciliation formula
        expected_ending = frontend_row["beginning_hc"] + frontend_row["net_movement"]
        assert frontend_row["ending_hc"] == expected_ending, (
            f"Reconciliation failed for ORG-ENG-FRONTEND: "
            f"{frontend_row['beginning_hc']} + {frontend_row['net_movement']} "
            f"!= {frontend_row['ending_hc']}"
        )

    def test_headcount_reconciliation_by_cost_center(self, rest_client: RestClient):
        """Test headcount reconciliation grouped by cost center.

        Creates workers and movements, then validates the reconciliation
        formula holds for each cost center grouping.
        """
        # Define test period dates
        period_start = date_str(-30)
        period_end = date_str(0)
        hire_date = date_str(-15)

        # Create a worker in CC-2000 (Engineering)
        worker_id = generate_worker_id()
        rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": worker_id,
                "job_profile_id": DEMO_JOB_PROFILES[2],  # JP-SWE-SR
                "org_id": DEMO_ORGS[1],  # ORG-ENG
                "cost_center_id": DEMO_COST_CENTERS[1],  # CC-2000
                "hire_date": hire_date,
            },
        )

        # Run headcount report grouped by cost center
        report = rest_client.call_tool(
            "workday_report_headcount",
            {
                "start_date": period_start,
                "end_date": period_end,
                "group_by": "cost_center_id",
            },
        )

        # Validate report structure
        assert "report" in report
        assert report["group_by"] == "cost_center_id"

        # Validate reconciliation formula for each cost center
        for row in report["report"]:
            assert "group_id" in row
            assert "group_name" in row
            assert "beginning_hc" in row
            assert "hires" in row
            assert "terminations" in row
            assert "transfers_in" in row
            assert "transfers_out" in row
            assert "net_movement" in row
            assert "ending_hc" in row

            # Validate reconciliation formula
            expected_ending = row["beginning_hc"] + row["net_movement"]
            assert row["ending_hc"] == expected_ending, (
                f"Reconciliation formula failed for {row['group_id']}: "
                f"{row['beginning_hc']} + {row['net_movement']} != {row['ending_hc']}"
            )

            # Validate net_movement calculation
            expected_net = (
                row["hires"] - row["terminations"] + row["transfers_in"] - row["transfers_out"]
            )
            assert row["net_movement"] == expected_net, (
                f"Net movement calculation failed for {row['group_id']}: "
                f"expected {expected_net}, got {row['net_movement']}"
            )

    def test_total_company_reconciliation(self, rest_client: RestClient):
        """Test that sum of all org headcounts reconciles at company level.

        Validates:
        - Sum of all ending_hc values across orgs
        - Company-wide: total_hires - total_terms + net_transfers = net_change
        - Transfers should net to zero across the company (internal movements)
        """
        # Define test period dates
        period_start = date_str(-30)
        period_end = date_str(0)
        hire_date = date_str(-15)

        # Create a few workers to ensure we have data
        for _ in range(2):
            worker_id = generate_worker_id()
            rest_client.call_tool(
                "workday_hire_worker",
                {
                    "worker_id": worker_id,
                    "job_profile_id": DEMO_JOB_PROFILES[3],  # JP-SWE-MID
                    "org_id": DEMO_ORGS[1],  # ORG-ENG
                    "cost_center_id": DEMO_COST_CENTERS[1],  # CC-2000
                    "hire_date": hire_date,
                },
            )

        # Run headcount report by org
        report = rest_client.call_tool(
            "workday_report_headcount",
            {
                "start_date": period_start,
                "end_date": period_end,
                "group_by": "org_id",
            },
        )

        # Calculate company-wide totals
        total_beginning_hc = sum(row["beginning_hc"] for row in report["report"])
        total_ending_hc = sum(row["ending_hc"] for row in report["report"])
        total_hires = sum(row["hires"] for row in report["report"])
        total_terminations = sum(row["terminations"] for row in report["report"])
        total_transfers_in = sum(row["transfers_in"] for row in report["report"])
        total_transfers_out = sum(row["transfers_out"] for row in report["report"])
        total_net_movement = sum(row["net_movement"] for row in report["report"])

        # Validate company-wide reconciliation
        expected_total_ending = total_beginning_hc + total_net_movement
        assert total_ending_hc == expected_total_ending, (
            f"Company-wide reconciliation failed: "
            f"{total_beginning_hc} + {total_net_movement} != {total_ending_hc}"
        )

        # Validate that transfers net to zero company-wide (internal movements)
        assert total_transfers_in == total_transfers_out, (
            f"Company-wide transfers should net to zero: "
            f"transfers_in ({total_transfers_in}) != transfers_out ({total_transfers_out})"
        )

        # Validate net_movement equals hires - terminations (since transfers net to zero)
        expected_net = total_hires - total_terminations
        assert total_net_movement == expected_net, (
            f"Company-wide net movement should equal hires - terminations: "
            f"expected {expected_net}, got {total_net_movement}"
        )

    def test_headcount_with_org_filter(self, rest_client: RestClient):
        """Test headcount report filtered to a specific org."""
        # Define test period dates
        period_start = date_str(-30)
        period_end = date_str(0)
        hire_date = date_str(-15)

        # Create a worker in ORG-ENG
        worker_id = generate_worker_id()
        rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": worker_id,
                "job_profile_id": DEMO_JOB_PROFILES[2],  # JP-SWE-SR
                "org_id": DEMO_ORGS[1],  # ORG-ENG
                "cost_center_id": DEMO_COST_CENTERS[1],  # CC-2000
                "hire_date": hire_date,
            },
        )

        # Run headcount report filtered to ORG-ENG
        report = rest_client.call_tool(
            "workday_report_headcount",
            {
                "start_date": period_start,
                "end_date": period_end,
                "group_by": "org_id",
                "org_id": DEMO_ORGS[1],  # ORG-ENG
            },
        )

        # Validate report structure
        assert "report" in report
        assert len(report["report"]) >= 1

        # All results should be for ORG-ENG
        for row in report["report"]:
            assert row["group_id"] == DEMO_ORGS[1], (
                f"Expected all results for {DEMO_ORGS[1]}, got {row['group_id']}"
            )

            # Validate reconciliation formula
            expected_ending = row["beginning_hc"] + row["net_movement"]
            assert row["ending_hc"] == expected_ending
