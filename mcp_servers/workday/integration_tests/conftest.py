"""Pytest fixtures for integration tests.

This module provides fixtures for testing against the REST bridge.
The REST bridge must be running before tests are executed.

Demo data is seeded via REST API tools at session start if not present.
"""

import pytest

from .helpers import (
    DEFAULT_PASSWORD,
    DEFAULT_USER,
    DEMO_COST_CENTERS,
    DEMO_JOB_PROFILES,
    DEMO_LOCATIONS,
    DEMO_ORGS,
    REST_BRIDGE_URL,
    RestClient,
    wait_for_server,
)

# Demo data definitions for seeding via REST tools
_JOB_PROFILES_DATA = [
    {
        "job_profile_id": "JP-CEO",
        "title": "Chief Executive Officer",
        "job_family": "Executive",
        "job_level": "C-Level",
    },
    {
        "job_profile_id": "JP-VP-ENG",
        "title": "VP of Engineering",
        "job_family": "Engineering Leadership",
        "job_level": "VP",
    },
    {
        "job_profile_id": "JP-SWE-SR",
        "title": "Senior Software Engineer",
        "job_family": "Engineering",
        "job_level": "Senior",
    },
    {
        "job_profile_id": "JP-SWE-MID",
        "title": "Software Engineer",
        "job_family": "Engineering",
        "job_level": "Mid",
    },
    {
        "job_profile_id": "JP-SWE-JR",
        "title": "Junior Software Engineer",
        "job_family": "Engineering",
        "job_level": "Junior",
    },
]

_LOCATIONS_DATA = [
    {
        "location_id": "LOC-SF",
        "location_name": "San Francisco HQ",
        "city": "San Francisco",
        "country": "USA",
    },
    {
        "location_id": "LOC-NYC",
        "location_name": "New York Office",
        "city": "New York",
        "country": "USA",
    },
    {
        "location_id": "LOC-REMOTE",
        "location_name": "Remote",
        "country": "GLOBAL",
    },
]

# Orgs must be created in parent-first order due to FK constraints
_ORGS_DATA = [
    {
        "org_id": "ORG-COMPANY",
        "org_name": "Acme Corporation",
        "org_type": "Supervisory",
    },
    {
        "org_id": "ORG-ENG",
        "org_name": "Engineering",
        "org_type": "Supervisory",
        "parent_org_id": "ORG-COMPANY",
    },
    {
        "org_id": "ORG-ENG-BACKEND",
        "org_name": "Backend Engineering",
        "org_type": "Supervisory",
        "parent_org_id": "ORG-ENG",
    },
    {
        "org_id": "ORG-ENG-FRONTEND",
        "org_name": "Frontend Engineering",
        "org_type": "Supervisory",
        "parent_org_id": "ORG-ENG",
    },
]

_COST_CENTERS_DATA = [
    {
        "cost_center_id": "CC-1000",
        "cost_center_name": "Executive",
        "org_id": "ORG-COMPANY",
    },
    {
        "cost_center_id": "CC-2000",
        "cost_center_name": "Engineering",
        "org_id": "ORG-ENG",
    },
    {
        "cost_center_id": "CC-2100",
        "cost_center_name": "Backend Engineering",
        "org_id": "ORG-ENG-BACKEND",
    },
    {
        "cost_center_id": "CC-2200",
        "cost_center_name": "Frontend Engineering",
        "org_id": "ORG-ENG-FRONTEND",
    },
]


def _seed_demo_data(client: RestClient) -> None:
    """Seed demo data via REST tools if not already present.

    Creates job profiles, locations, orgs, and cost centers.
    Silently ignores duplicates (already exists errors).
    """
    # 1. Create job profiles
    for jp in _JOB_PROFILES_DATA:
        try:
            client.call_tool("workday_create_job_profile", jp)
        except AssertionError as e:
            if "already exists" not in str(e):
                raise

    # 2. Create locations
    for loc in _LOCATIONS_DATA:
        try:
            client.call_tool("workday_create_location", loc)
        except AssertionError as e:
            if "already exists" not in str(e):
                raise

    # 3. Create orgs (order matters - parents first)
    for org in _ORGS_DATA:
        try:
            client.call_tool("workday_create_org", org)
        except AssertionError as e:
            if "already exists" not in str(e):
                raise

    # 4. Create cost centers
    for cc in _COST_CENTERS_DATA:
        try:
            client.call_tool("workday_create_cost_center", cc)
        except AssertionError as e:
            if "already exists" not in str(e):
                raise


@pytest.fixture(scope="session")
def base_url() -> str:
    """Get the base URL for the REST bridge."""
    return REST_BRIDGE_URL


@pytest.fixture(scope="session")
def rest_client(base_url: str) -> RestClient:
    """Create a REST client for testing.

    Verifies the server is running before tests start.
    Logs in as coordinator and seeds demo data via REST tools.
    """
    if not wait_for_server(base_url, timeout=5):
        pytest.skip(
            f"REST bridge not running at {base_url}. Start with: uv run mcp-ui -s workday --no-open"
        )

    client = RestClient(base_url)

    # Login as coordinator (has write permissions for seeding)
    login_response = client.login(DEFAULT_USER, DEFAULT_PASSWORD)
    assert "token" in login_response, f"Login failed: {login_response}"

    # Seed demo data for tests
    _seed_demo_data(client)

    return client


@pytest.fixture
def demo_job_profiles() -> list[str]:
    """Get list of demo job profile IDs."""
    return DEMO_JOB_PROFILES


@pytest.fixture
def demo_locations() -> list[str]:
    """Get list of demo location IDs."""
    return DEMO_LOCATIONS


@pytest.fixture
def demo_orgs() -> list[str]:
    """Get list of demo organization IDs."""
    return DEMO_ORGS


@pytest.fixture
def demo_cost_centers() -> list[str]:
    """Get list of demo cost center IDs."""
    return DEMO_COST_CENTERS


# =============================================================================
# POLICY SEEDING FIXTURES
# =============================================================================

# Policy definitions for Germany workflow test
_GERMANY_POLICIES_DATA = [
    {
        "policy_id": "POLICY-DE-LEAD-TIME",
        "country": "DE",
        "role": "Senior Software Engineer",
        "employment_type": "full_time",
        "policy_type": "lead_times",
        "lead_time_days": 21,
        "content": {"description": "Germany requires 21-day minimum notice"},
        "effective_date": "2025-01-01",
        "version": "1.0",
    },
    {
        "policy_id": "POLICY-DE-VISA",
        "country": "DE",
        # role and employment_type omitted = applies to all
        "policy_type": "constraints",
        "content": {"requirement": "Work authorization required for non-EU nationals"},
        "effective_date": "2025-01-01",
        "version": "1.0",
    },
    {
        "policy_id": "POLICY-DE-PAYROLL",
        "country": "DE",
        "policy_type": "payroll_cutoffs",
        "content": {"cutoff_day": 15, "processing_days": 5},
        "effective_date": "2025-01-01",
        "version": "1.0",
    },
]

# Payroll cutoff definition for Germany (used by HCM gating checks)
_GERMANY_PAYROLL_CUTOFF_DATA = {
    "cutoff_id": "CUTOFF-DE-001",
    "country": "DE",
    "cutoff_day_of_month": 15,
    "processing_days": 5,
    "effective_date": "2025-01-01",
}


def _seed_policies_via_rest(client: RestClient) -> list[str]:
    """Seed Germany policies via the REST API using workday_policies_create tool.

    Uses the new workday_policies_create MCP tool to create policies.
    Also creates payroll cutoff entries using workday_policies_create_payroll_cutoff.

    Args:
        client: REST client for calling tools

    Returns:
        List of created policy IDs
    """
    policy_ids = []

    # Create policy references
    for policy_data in _GERMANY_POLICIES_DATA:
        policy_id = policy_data["policy_id"]
        try:
            client.call_tool("workday_policies_create", policy_data)
            policy_ids.append(policy_id)
        except AssertionError as e:
            # Ignore "already exists" errors (idempotent)
            if "already exists" in str(e).lower() or "unique constraint" in str(e).lower():
                policy_ids.append(policy_id)
            else:
                raise

    # Create payroll cutoff entry (used by HCM gating checks)
    try:
        client.call_tool("workday_policies_create_payroll_cutoff", _GERMANY_PAYROLL_CUTOFF_DATA)
    except AssertionError as e:
        # Ignore "already exists" errors (idempotent)
        if "already exists" not in str(e).lower() and "unique constraint" not in str(e).lower():
            raise

    return policy_ids


@pytest.fixture(scope="session")
def seed_germany_policies(rest_client: RestClient) -> list[str]:
    """Seed Germany policies for the workflow test.

    Creates three policies using the workday_policies_create MCP tool:
    - POLICY-DE-LEAD-TIME: 21-day lead time requirement
    - POLICY-DE-VISA: Work authorization requirement
    - POLICY-DE-PAYROLL: Payroll cutoff rules

    Also creates payroll cutoff entry using workday_policies_create_payroll_cutoff:
    - CUTOFF-DE-001: Day 15, 5 processing days

    Args:
        rest_client: REST client fixture

    Returns:
        List of created policy IDs

    Note:
        Uses session scope to seed once per test session.
        Policies and payroll cutoffs are created via MCP tools.
    """
    return _seed_policies_via_rest(rest_client)
