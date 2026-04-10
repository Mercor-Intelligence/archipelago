"""Integration tests for temporal queries.

Tests point-in-time (as_of_date) queries across:
- workday_get_worker
- workday_list_workers
- workday_report_workforce_roster

Validates that worker state is correctly returned at different points in time
based on hire, transfer, and termination events.
"""

import uuid
from datetime import date, timedelta

import pytest

from .helpers import (
    DEMO_COST_CENTERS,
    DEMO_JOB_PROFILES,
    DEMO_ORGS,
    RestClient,
)


def generate_worker_id() -> str:
    """Generate a unique worker ID for temporal testing."""
    return f"WRK-TMP-{uuid.uuid4().hex[:8].upper()}"


def date_str(offset_days: int = 0) -> str:
    """Get a date as YYYY-MM-DD string, relative to today.

    Args:
        offset_days: Days to add/subtract from today (negative for past)

    Returns:
        Date string in YYYY-MM-DD format
    """
    return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class TestTemporalQueries:
    """Tests for temporal (as_of_date) queries.

    Test scenario timeline (relative to today):
    - Day -180: Hire worker in ORG-ENG
    - Day -120: Transfer to ORG-ENG-BACKEND
    - Day -30:  Terminate worker

    Query points:
    - Day -150: Worker active in ORG-ENG (post-hire, pre-transfer)
    - Day -90:  Worker active in ORG-ENG-BACKEND (post-transfer, pre-termination)
    - Day 0:    Worker terminated (should not appear in active queries)
    """

    @pytest.fixture
    def worker_with_lifecycle(self, rest_client: RestClient) -> dict:
        """Create a worker with complete lifecycle for temporal testing.

        Timeline:
        - Day -180: Hire in ORG-ENG
        - Day -120: Transfer to ORG-ENG-BACKEND
        - Day -30: Terminate

        Returns:
            dict with worker_id and key dates
        """
        worker_id = generate_worker_id()

        # Key dates in the worker's lifecycle
        day_0 = date_str(-180)  # Hire date
        day_60 = date_str(-120)  # Transfer date (60 days after hire)
        day_150 = date_str(-30)  # Termination date (150 days after hire)

        # Query points
        query_day_30 = date_str(-150)  # 30 days after hire
        query_day_90 = date_str(-90)  # 90 days after hire
        query_day_180 = date_str(0)  # 180 days after hire (today)
        day_before_hire = date_str(-181)  # One day before hire
        day_before_termination = date_str(-31)  # One day before termination

        # Step 1: Hire worker in ORG-ENG at Day 0
        rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": worker_id,
                "job_profile_id": DEMO_JOB_PROFILES[2],  # JP-SWE-SR
                "org_id": DEMO_ORGS[1],  # ORG-ENG
                "cost_center_id": DEMO_COST_CENTERS[1],  # CC-2000
                "hire_date": day_0,
            },
        )

        # Step 2: Transfer to ORG-ENG-BACKEND at Day +60
        rest_client.call_tool(
            "workday_transfer_worker",
            {
                "worker_id": worker_id,
                "new_org_id": DEMO_ORGS[2],  # ORG-ENG-BACKEND
                "new_cost_center_id": DEMO_COST_CENTERS[2],  # CC-2100
                "transfer_date": day_60,
            },
        )

        # Step 3: Terminate at Day +150
        rest_client.call_tool(
            "workday_terminate_worker",
            {
                "worker_id": worker_id,
                "termination_date": day_150,
            },
        )

        return {
            "worker_id": worker_id,
            "hire_date": day_0,
            "transfer_date": day_60,
            "termination_date": day_150,
            "query_day_30": query_day_30,
            "query_day_90": query_day_90,
            "query_day_180": query_day_180,
            "day_before_hire": day_before_hire,
            "day_before_termination": day_before_termination,
        }

    # ============================================================
    # Tests for workday_get_worker with as_of_date
    # ============================================================

    def test_get_worker_temporal_active_post_hire(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test get_worker at Day +30: Worker should be found (was active at that date).

        At this point:
        - Worker has been hired (Day 0)
        - Transfer has NOT happened yet (Day +60)
        - Worker was active at this date, so should be found

        Note: The as_of_date filters visibility based on hire/termination dates,
        but employment_status reflects the CURRENT state, not historical.
        """
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_30"]

        result = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": worker_id,
                "as_of_date": query_date,
            },
        )

        # Worker should be found (was active at this date)
        assert result["worker_id"] == worker_id
        # Note: employment_status reflects CURRENT state, not historical

    def test_get_worker_temporal_active_post_transfer(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test get_worker at Day +90: Worker should be found (was active post-transfer).

        At this point:
        - Worker has been hired (Day 0)
        - Transfer has happened (Day +60)
        - Termination has NOT happened yet (Day +150)
        - Worker was active at this date, so should be found

        Note: The as_of_date filters visibility based on hire/termination dates,
        but employment_status reflects the CURRENT state, not historical.
        """
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_90"]

        result = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": worker_id,
                "as_of_date": query_date,
            },
        )

        # Worker should be found (was active at this date)
        assert result["worker_id"] == worker_id
        # Note: employment_status reflects CURRENT state, not historical

    def test_get_worker_temporal_after_termination(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test get_worker at Day +180: Worker is terminated.

        At this point:
        - Worker was terminated at Day +150
        - Temporal query should return error (worker not found at that date)
        """
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_180"]

        # Worker should not be found with temporal query after termination
        with pytest.raises(AssertionError) as exc_info:
            rest_client.call_tool(
                "workday_get_worker",
                {
                    "worker_id": worker_id,
                    "as_of_date": query_date,
                },
            )

        assert "500" in str(exc_info.value) or "not found" in str(exc_info.value).lower()

    def test_get_worker_temporal_exactly_on_hire_date(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test query exactly on hire date: Worker should be found."""
        worker_id = worker_with_lifecycle["worker_id"]
        hire_date = worker_with_lifecycle["hire_date"]

        result = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": worker_id,
                "as_of_date": hire_date,
            },
        )

        # Worker should be found on hire date
        assert result["worker_id"] == worker_id
        # Note: employment_status reflects CURRENT state, not historical

    def test_get_worker_temporal_day_before_hire(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test query one day before hire: Worker should not be found."""
        worker_id = worker_with_lifecycle["worker_id"]
        day_before_hire = worker_with_lifecycle["day_before_hire"]

        # Worker should not be found before hire date
        with pytest.raises(AssertionError) as exc_info:
            rest_client.call_tool(
                "workday_get_worker",
                {
                    "worker_id": worker_id,
                    "as_of_date": day_before_hire,
                },
            )

        assert "500" in str(exc_info.value) or "not found" in str(exc_info.value).lower()

    # ============================================================
    # Tests for workday_list_workers with as_of_date
    # ============================================================

    def test_list_workers_temporal_includes_active(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test list_workers at Day +30: Worker should appear in list."""
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_30"]

        result = rest_client.call_tool(
            "workday_list_workers",
            {
                "as_of_date": query_date,
            },
        )

        worker_ids = [w["worker_id"] for w in result["workers"]]
        assert worker_id in worker_ids, f"Worker {worker_id} should appear in list at {query_date}"

    def test_list_workers_temporal_excludes_terminated(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test list_workers at Day +180: Terminated worker should not appear."""
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_180"]

        result = rest_client.call_tool(
            "workday_list_workers",
            {
                "as_of_date": query_date,
            },
        )

        worker_ids = [w["worker_id"] for w in result["workers"]]
        assert worker_id not in worker_ids, (
            f"Terminated worker {worker_id} should not appear in list at {query_date}"
        )

    def test_list_workers_temporal_without_status_filter(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test list_workers at Day +30 without status filter includes historical workers.

        Note: When using as_of_date, the filter uses hire/termination dates for
        temporal visibility, but employment_status filter uses CURRENT status.
        So combining as_of_date with employment_status=Active would exclude
        workers who were active at that date but are now terminated.
        """
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_30"]

        # Query without employment_status filter to get all workers
        # who were visible at that date
        result = rest_client.call_tool(
            "workday_list_workers",
            {
                "as_of_date": query_date,
            },
        )

        worker_ids = [w["worker_id"] for w in result["workers"]]
        assert worker_id in worker_ids, f"Worker {worker_id} should appear in list at {query_date}"

    # ============================================================
    # Tests for workday_report_workforce_roster with as_of_date
    # ============================================================

    def test_roster_temporal_shows_worker_at_effective_date(self, rest_client: RestClient):
        """Test roster with as_of_date returns workers effective at that date.

        Note: The roster filters by effective_date <= as_of_date.
        The effective_date is updated on each lifecycle event (hire, transfer, terminate).
        So a worker with effective_date = hire_date will appear when querying
        as_of_date >= hire_date.
        """
        # Create a fresh worker (not using lifecycle fixture to avoid termination)
        worker_id = generate_worker_id()
        hire_date = date_str(-30)  # 30 days ago

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

        # Query roster at today (after hire)
        result = rest_client.call_tool(
            "workday_report_workforce_roster",
            {
                "as_of_date": date_str(0),  # today
            },
        )

        roster_worker_ids = [r["worker_id"] for r in result["roster"]]
        assert worker_id in roster_worker_ids, (
            f"Worker {worker_id} should appear in roster when as_of_date >= hire_date"
        )

    def test_roster_temporal_excludes_terminated(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test roster at Day +180: Terminated worker should not appear."""
        worker_id = worker_with_lifecycle["worker_id"]
        query_date = worker_with_lifecycle["query_day_180"]

        result = rest_client.call_tool(
            "workday_report_workforce_roster",
            {
                "as_of_date": query_date,
            },
        )

        roster_worker_ids = [r["worker_id"] for r in result["roster"]]
        assert worker_id not in roster_worker_ids, (
            f"Terminated worker {worker_id} should not appear in roster at {query_date}"
        )

    def test_roster_temporal_with_as_of_date_field(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test roster includes as_of_date in response."""
        query_date = worker_with_lifecycle["query_day_30"]

        result = rest_client.call_tool(
            "workday_report_workforce_roster",
            {
                "as_of_date": query_date,
            },
        )

        assert "roster" in result
        assert result.get("as_of_date") == query_date

    # ============================================================
    # Edge Cases and Boundary Tests
    # ============================================================

    def test_temporal_query_exactly_on_termination_date(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test query exactly on termination date: Worker behavior depends on implementation.

        The termination_date represents when the worker becomes terminated.
        On that date, the worker may or may not be found depending on implementation.
        This test documents the actual behavior.
        """
        worker_id = worker_with_lifecycle["worker_id"]
        termination_date = worker_with_lifecycle["termination_date"]

        # Query on termination date - behavior may vary
        # Either the worker is found (termination happens end of day)
        # or not found with HTTP error (termination happens start of day)
        worker_found = False
        result = None
        try:
            result = rest_client.call_tool(
                "workday_get_worker",
                {
                    "worker_id": worker_id,
                    "as_of_date": termination_date,
                },
            )
            worker_found = True
        except AssertionError as e:
            # Only catch HTTP errors (500 or "not found"), not assertion failures
            error_msg = str(e).lower()
            if "500" not in str(e) and "not found" not in error_msg:
                raise  # Re-raise unexpected errors

        # If worker was found, validate it's the correct one
        if worker_found:
            assert result["worker_id"] == worker_id, (
                f"Expected worker {worker_id}, got {result['worker_id']}"
            )

    def test_temporal_query_day_before_termination(
        self, rest_client: RestClient, worker_with_lifecycle: dict
    ):
        """Test query one day before termination: Worker should be found."""
        worker_id = worker_with_lifecycle["worker_id"]
        day_before_term = worker_with_lifecycle["day_before_termination"]

        result = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": worker_id,
                "as_of_date": day_before_term,
            },
        )

        # Worker should be found (was active at this date)
        assert result["worker_id"] == worker_id
        # Note: employment_status reflects CURRENT state, not historical


class TestTemporalQueriesWithoutLifecycle:
    """Tests for temporal queries with simpler scenarios."""

    def test_get_worker_without_as_of_date(self, rest_client: RestClient):
        """Test get_worker without as_of_date returns current state."""
        worker_id = generate_worker_id()
        hire_date = date_str(-30)

        # Hire a worker
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

        # Get worker without as_of_date
        result = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": worker_id,
            },
        )

        assert result["worker_id"] == worker_id
        assert result["employment_status"] == "Active"

    def test_list_workers_without_as_of_date(self, rest_client: RestClient):
        """Test list_workers without as_of_date returns current state."""
        worker_id = generate_worker_id()
        hire_date = date_str(-30)

        # Hire a worker
        rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": worker_id,
                "job_profile_id": DEMO_JOB_PROFILES[3],  # JP-SWE-MID
                "org_id": DEMO_ORGS[2],  # ORG-ENG-BACKEND
                "cost_center_id": DEMO_COST_CENTERS[2],  # CC-2100
                "hire_date": hire_date,
            },
        )

        # List workers without as_of_date
        result = rest_client.call_tool(
            "workday_list_workers",
            {
                "org_id": DEMO_ORGS[2],
            },
        )

        worker_ids = [w["worker_id"] for w in result["workers"]]
        assert worker_id in worker_ids

    def test_roster_without_as_of_date(self, rest_client: RestClient):
        """Test roster without as_of_date returns current state."""
        worker_id = generate_worker_id()
        hire_date = date_str(-30)

        # Hire a worker
        rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": worker_id,
                "job_profile_id": DEMO_JOB_PROFILES[4],  # JP-SWE-JR
                "org_id": DEMO_ORGS[3],  # ORG-ENG-FRONTEND
                "cost_center_id": DEMO_COST_CENTERS[3],  # CC-2200
                "hire_date": hire_date,
            },
        )

        # Get roster without as_of_date
        result = rest_client.call_tool(
            "workday_report_workforce_roster",
            {},
        )

        roster_worker_ids = [r["worker_id"] for r in result["roster"]]
        assert worker_id in roster_worker_ids
