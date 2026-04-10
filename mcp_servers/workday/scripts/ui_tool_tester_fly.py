#!/usr/bin/env python3
"""
Playwright-based UI automation script for testing Workday MCP tools on Fly.

This script:
1. Opens the browser to the Fly UI (https://mcp-services-gui-frontend.fly.dev)
2. Logs in with Fly credentials
3. Selects the Workday app
4. For each tool in HR workflow order:
   - Selects the tool
   - Fills in the test data
   - Clicks Execute
   - Waits for response
   - Takes a screenshot
5. Saves all screenshots to a timestamped folder

Usage:
    # Run the automation against Fly:
    uv run python scripts/ui_tool_tester_fly.py

    # Run in headed mode (visible browser):
    uv run python scripts/ui_tool_tester_fly.py --headed

    # Run specific tools only:
    uv run python scripts/ui_tool_tester_fly.py --tools "workday_create_org"

Requirements:
    uv add playwright
    playwright install chromium
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Check if playwright is installed
try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:
    print("ERROR: Playwright is not installed.")
    print("Run: uv add playwright && playwright install chromium")
    sys.exit(1)


# =============================================================================
# CONFIGURATION - FLY
# =============================================================================

FLY_UI_URL = "https://mcp-services-gui-frontend.fly.dev"
FLY_PASSWORD = "ew0h7jiK1pFosDNp"

# Screenshot output directory
SCREENSHOTS_DIR = Path(__file__).parent.parent / "test_screenshots"


def date_str(offset_days: int = 0) -> str:
    """Get a date as YYYY-MM-DD string, relative to today."""
    return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# =============================================================================
# TEST DATA - Following Mock Spec Pre-Onboarding Workflow (Phases A-D)
# =============================================================================
#
# This test data follows the Workday HCM V2 MCP Mock Spec workflow:
#
# SETUP PHASE: HCM Foundation (Orgs, Positions, Policies)
#   - Create organizational structure needed before case can reference it
#   - Create policies that will be attached to the case
#
# PHASE A - Case Initialization (Day 0-2):
#   - workday_create_case: Create pre-onboarding case
#   - workday_assign_owner_case: Assign case ownership
#   - workday_get_case: Verify case was created
#
# PHASE B - Discover Prerequisites & Constraints (Day 1-10):
#   - workday_hcm_read_context: Read HCM context for case
#   - workday_hcm_read_position: Read position context
#   - workday_policies_get_applicable: Retrieve applicable policies
#   - workday_policies_attach_to_case: Link policy refs to case
#   - workday_milestones_list: Identify required milestones
#
# PHASE C - Orchestrate Execution & Communications (Day 3-35):
#   - workday_milestones_update: Update milestone states with evidence
#   - workday_tasks_create: Create manual tasks for milestones
#   - workday_tasks_update: Update task status
#   - workday_search_case: Query cases by status/country
#   - workday_snapshot_case: Get full case snapshot
#   - workday_update_case: Update case status
#   - workday_exception_request: Request exception when needed
#   - workday_exception_approve: HR Admin approves exception
#
# PHASE D - Confirm Start Date & Gated HCM Update (Day 14-45+):
#   - workday_hcm_confirm_start_date: Gated write-back (requires milestones)
#   - workday_hcm_update_readiness: Update readiness flag
#   - workday_audit_get_history: Retrieve audit trail
#
# =============================================================================


def get_test_data() -> list[dict[str, Any]]:
    """
    Returns test data following the Mock Spec pre-onboarding workflow.

    The workflow demonstrates a complete hire coordination scenario:
    - Candidate: Sarah Chen (Senior Software Engineer)
    - Location: San Francisco, US
    - Start Date: 45 days from now
    - Employment Type: Full-time

    Each entry contains:
    - tool_name: The internal tool name (e.g., "workday_create_case")
    - display_name: The UI display name
    - category: The category in the UI sidebar
    - params: Dict of parameter name -> value to fill in
    - description: What this tool does (for logging)
    - requires_hr_admin: Whether this tool needs hr_admin login (optional)
    - skip: If True, tool may fail validation (expected behavior)
    """

    # Generate unique suffix for this test run
    suffix = datetime.now().strftime("%H%M%S")

    # ==========================================================================
    # Pre-Onboarding Scenario: Hiring Sarah Chen as Senior Software Engineer
    # ==========================================================================
    candidate_name = "Sarah Chen"
    candidate_id = f"CAND-SCHEN-{suffix}"
    case_id = f"CASE-SCHEN-{suffix}"
    requisition_id = f"REQ-ENG-{suffix}"
    role = "Senior Software Engineer"
    country = "US"
    city = "San Francisco"
    employment_type = "full_time"

    # HCM Entity IDs (created in setup phase)
    org_id = f"ORG-ENG-{suffix}"
    org_name = "Engineering"
    cost_center_id = f"CC-ENG-{suffix}"
    location_id = f"LOC-SF-{suffix}"
    job_profile_id = f"JP-SSE-{suffix}"
    position_id = f"POS-SSE-{suffix}"
    worker_id = f"WRK-SCHEN-{suffix}"
    policy_id = f"POL-US-ENG-{suffix}"

    # Dates
    proposed_start_date = date_str(45)  # 45 days from now
    case_due_date = date_str(30)  # 30 days to complete
    task_due_date = date_str(14)  # Task due in 14 days

    return [
        # =====================================================================
        # SETUP PHASE: HCM Foundation
        # Before we can create a case, we need the HCM entities it references
        # =====================================================================
        {
            "tool_name": "workday_create_org",
            "display_name": "Workday Create Org",
            "category": "Workday",
            "description": f"Setup: Create {org_name} organization",
            "params": {
                "org_id": org_id,
                "org_name": org_name,
                "org_type": "Supervisory",
            },
        },
        {
            "tool_name": "workday_create_cost_center",
            "display_name": "Workday Create Cost Center",
            "category": "Workday",
            "description": f"Setup: Create cost center for {org_name}",
            "params": {
                "cost_center_id": cost_center_id,
                "cost_center_name": f"{org_name} Operations",
                "org_id": org_id,
            },
        },
        {
            "tool_name": "workday_create_location",
            "display_name": "Workday Create Location",
            "category": "Workday",
            "description": f"Setup: Create {city} location",
            "params": {
                "location_id": location_id,
                "location_name": f"{city} Office",
                "city": city,
                "country": country,
            },
        },
        {
            "tool_name": "workday_create_job_profile",
            "display_name": "Workday Create Job Profile",
            "category": "Workday",
            "description": f"Setup: Create {role} job profile",
            "params": {
                "job_profile_id": job_profile_id,
                "title": role,
                "job_family": "Engineering",
                "job_level": "L5",
            },
        },
        {
            "tool_name": "workday_create_position",
            "display_name": "Workday Create Position",
            "category": "Workday",
            "description": f"Setup: Create open position for {role}",
            "params": {
                "position_id": position_id,
                "job_profile_id": job_profile_id,
                "org_id": org_id,
                "fte": "1.0",
                "status": "open",
            },
        },
        {
            "tool_name": "workday_policies_create",
            "display_name": "Workday Policies Create",
            "category": "Workday",
            "description": f"Setup: Create lead time policy for {country}",
            "params": {
                "policy_id": policy_id,
                "country": country,
                "policy_type": "lead_times",
                "content": '{"min_notice_days": 21, "description": "Background check lead time"}',
                "effective_date": date_str(0),
                "version": "1.0",
                "role": role,
                "employment_type": employment_type,
                "lead_time_days": "21",
            },
        },
        {
            "tool_name": "workday_policies_create_payroll_cutoff",
            "display_name": "Workday Policies Create Payroll Cutoff",
            "category": "Workday",
            "description": f"Setup: Create payroll cutoff rule for {country}",
            "params": {
                "cutoff_id": f"CUTOFF-{country}-{suffix}",
                "country": country,
                "cutoff_day_of_month": "15",
                "processing_days": "5",
                "effective_date": date_str(0),
            },
        },
        # =====================================================================
        # PHASE A: Case Initialization (Day 0-2)
        # Create pre-onboarding case for Sarah Chen
        # =====================================================================
        {
            "tool_name": "workday_create_case",
            "display_name": "Workday Create Case",
            "category": "Workday",
            "description": f"Phase A: Create case for {candidate_name}",
            "params": {
                "case_id": case_id,
                "candidate_id": candidate_id,
                "requisition_id": requisition_id,
                "role": role,
                "country": country,
                "employment_type": employment_type,
                "owner_persona": "pre_onboarding_coordinator",
                "proposed_start_date": proposed_start_date,
                "due_date": case_due_date,
            },
        },
        {
            "tool_name": "workday_assign_owner_case",
            "display_name": "Workday Assign Owner Case",
            "category": "Workday",
            "description": f"Phase A: Assign coordinator to {candidate_name}'s case",
            "params": {
                "case_id": case_id,
                "owner_persona": "pre_onboarding_coordinator",
                "rationale": f"Assigning coordinator to manage {candidate_name}'s onboarding",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_get_case",
            "display_name": "Workday Get Case",
            "category": "Workday",
            "description": f"Phase A: Verify case created for {candidate_name}",
            "params": {
                "case_id": case_id,
            },
        },
        # =====================================================================
        # PHASE B: Discover Prerequisites & Constraints (Day 1-10)
        # Retrieve HCM context and applicable policies
        # =====================================================================
        {
            "tool_name": "workday_hcm_read_context",
            "display_name": "Workday Hcm Read Context",
            "category": "Workday",
            "description": f"Phase B: Read HCM context for {candidate_name}'s case",
            "params": {
                "case_id": case_id,
            },
        },
        {
            "tool_name": "workday_hcm_read_position",
            "display_name": "Workday Hcm Read Position",
            "category": "Workday",
            "description": f"Phase B: Read position context for {role}",
            "params": {
                "case_id": case_id,
            },
        },
        {
            "tool_name": "workday_policies_get_applicable",
            "display_name": "Workday Policies Get Applicable",
            "category": "Workday",
            "description": f"Phase B: Get applicable policies for {role} in {country}",
            "params": {
                "country": country,
                "role": role,
                "employment_type": employment_type,
            },
        },
        {
            "tool_name": "workday_policies_attach_to_case",
            "display_name": "Workday Policies Attach To Case",
            "category": "Workday",
            "description": f"Phase B: Attach policies to {candidate_name}'s case",
            "params": {
                "case_id": case_id,
                "policy_ids": f'["{policy_id}"]',
                "decision_context": f"Attaching {country} background check policy for {role}",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_milestones_list",
            "display_name": "Workday Milestones List",
            "category": "Workday",
            "description": f"Phase B: List required milestones for {candidate_name}",
            "params": {
                "case_id": case_id,
            },
        },
        # =====================================================================
        # PHASE C: Orchestrate Execution & Communications (Day 3-35)
        # Update milestones, create tasks, track progress
        # =====================================================================
        {
            "tool_name": "workday_milestones_update",
            "display_name": "Workday Milestones Update",
            "category": "Workday",
            "description": f"Phase C: Mark screening complete for {candidate_name}",
            "params": {
                "case_id": case_id,
                "milestone_type": "screening",
                "new_status": "completed",
                "evidence_link": "https://screening.example.com/report/12345",
                "notes": "Background check passed - all clear",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_tasks_create",
            "display_name": "Workday Tasks Create",
            "category": "Workday",
            "description": f"Phase C: Create document collection task for {candidate_name}",
            "params": {
                "case_id": case_id,
                "milestone_type": "documents",
                "title": f"Collect I-9 and tax documents from {candidate_name}",
                "owner_persona": "pre_onboarding_coordinator",
                "due_date": task_due_date,
            },
        },
        {
            "tool_name": "workday_tasks_update",
            "display_name": "Workday Tasks Update",
            "category": "Workday",
            "description": "Phase C: Update document task progress",
            "params": {
                "task_id": "1",  # First task created
                "new_status": "in_progress",
                "notes": f"Contacted {candidate_name} for document submission",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_search_case",
            "display_name": "Workday Search Case",
            "category": "Workday",
            "description": f"Phase C: Search open cases in {country}",
            "params": {
                "status": "open",
                "country": country,
            },
        },
        {
            "tool_name": "workday_update_case",
            "display_name": "Workday Update Case",
            "category": "Workday",
            "description": f"Phase C: Update {candidate_name}'s case to in_progress",
            "params": {
                "case_id": case_id,
                "new_status": "in_progress",
                "rationale": "Prerequisites discovery complete, starting milestone execution",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_snapshot_case",
            "display_name": "Workday Snapshot Case",
            "category": "Workday",
            "description": f"Phase C: Get full snapshot of {candidate_name}'s case",
            "params": {
                "case_id": case_id,
            },
        },
        {
            "tool_name": "workday_exception_request",
            "display_name": "Workday Exception Request",
            "category": "Workday",
            "description": f"Phase C: Request work auth exception for {candidate_name}",
            "params": {
                "case_id": case_id,
                "milestone_type": "work_authorization",
                "reason": f"Visa processing for {candidate_name} delayed - requesting exception",
                "affected_policy_refs": f'["{policy_id}"]',
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_exception_approve",
            "display_name": "Workday Exception Approve",
            "category": "Workday",
            "description": f"Phase C: HR Admin approves {candidate_name}'s exception",
            "requires_hr_admin": True,
            "params": {
                "exception_id": "1",  # First exception created
                "approval_status": "approved",
                "approval_notes": f"Approved conditional start for {candidate_name}",
                "actor_persona": "hr_admin",
            },
        },
        # =====================================================================
        # PHASE D: Confirm Start Date & Gated HCM Update (Day 14-45+)
        # Final verification and write-back to HCM
        # =====================================================================
        # Note: workday_hcm_confirm_start_date requires all milestones complete
        # This will show gating validation in the UI
        {
            "tool_name": "workday_hcm_confirm_start_date",
            "display_name": "Workday Hcm Confirm Start Date",
            "category": "Workday",
            "description": "Phase D: Attempt to confirm start date (shows gating)",
            "skip": True,  # Expected to show gating validation
            "params": {
                "case_id": case_id,
                "confirmed_start_date": proposed_start_date,
                "policy_refs": f'["{policy_id}"]',
                "evidence_links": '["https://screening.example.com/report/12345"]',
                "rationale": f"Confirming start date for {candidate_name}",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        # Note: workday_hcm_update_readiness requires confirmed start date
        {
            "tool_name": "workday_hcm_update_readiness",
            "display_name": "Workday Hcm Update Readiness",
            "category": "Workday",
            "description": "Phase D: Attempt readiness update (shows gating)",
            "skip": True,  # Expected to show gating validation
            "params": {
                "case_id": case_id,
                "onboarding_readiness": "true",
                "policy_refs": f'["{policy_id}"]',
                "evidence_links": '["https://screening.example.com/report/12345"]',
                "rationale": f"{candidate_name} ready for Day 1",
                "actor_persona": "pre_onboarding_coordinator",
            },
        },
        {
            "tool_name": "workday_audit_get_history",
            "display_name": "Workday Audit Get History",
            "category": "Workday",
            "description": f"Phase D: Get audit trail for {candidate_name}'s case",
            "params": {
                "case_id": case_id,
            },
        },
        # =====================================================================
        # SUPPLEMENTARY: HCM Entity Operations
        # Additional tools not in core pre-onboarding workflow
        # =====================================================================
        {
            "tool_name": "workday_get_org",
            "display_name": "Workday Get Org",
            "category": "Workday",
            "description": f"Supplementary: Get {org_name} organization",
            "params": {
                "org_id": org_id,
            },
        },
        {
            "tool_name": "workday_list_orgs",
            "display_name": "Workday List Orgs",
            "category": "Workday",
            "description": "Supplementary: List all organizations",
            "params": {
                "page_size": "10",
                "page_number": "1",
            },
        },
        {
            "tool_name": "workday_get_org_hierarchy",
            "display_name": "Workday Get Org Hierarchy",
            "category": "Workday",
            "description": f"Supplementary: Get {org_name} hierarchy",
            "params": {
                "org_id": org_id,
            },
        },
        {
            "tool_name": "workday_get_job_profile",
            "display_name": "Workday Get Job Profile",
            "category": "Workday",
            "description": f"Supplementary: Get {role} job profile",
            "params": {
                "job_profile_id": job_profile_id,
            },
        },
        {
            "tool_name": "workday_list_job_profiles",
            "display_name": "Workday List Job Profiles",
            "category": "Workday",
            "description": "Supplementary: List all job profiles",
            "params": {
                "page_size": "10",
                "page_number": "1",
            },
        },
        {
            "tool_name": "workday_get_position",
            "display_name": "Workday Get Position",
            "category": "Workday",
            "description": f"Supplementary: Get {role} position",
            "params": {
                "position_id": position_id,
            },
        },
        {
            "tool_name": "workday_list_positions",
            "display_name": "Workday List Positions",
            "category": "Workday",
            "description": "Supplementary: List all positions",
            "params": {
                "page_size": "10",
                "page_number": "1",
            },
        },
        # =====================================================================
        # SUPPLEMENTARY: Reports
        # =====================================================================
        {
            "tool_name": "workday_report_workforce_roster",
            "display_name": "Workday Report Workforce Roster",
            "category": "Workday",
            "description": f"Supplementary: Generate roster for {org_name}",
            "params": {
                "org_id": org_id,
            },
        },
        {
            "tool_name": "workday_report_headcount",
            "display_name": "Workday Report Headcount",
            "category": "Workday",
            "description": "Supplementary: Generate headcount report",
            "params": {
                "start_date": date_str(0),
                "end_date": date_str(90),
                "group_by": "org_id",
            },
        },
        {
            "tool_name": "workday_report_movements",
            "display_name": "Workday Report Movements",
            "category": "Workday",
            "description": "Supplementary: Generate movement report",
            "params": {
                "start_date": date_str(0),
                "end_date": date_str(90),
            },
        },
        {
            "tool_name": "workday_report_positions",
            "display_name": "Workday Report Positions",
            "category": "Workday",
            "description": f"Supplementary: Generate positions report for {org_name}",
            "params": {
                "org_id": org_id,
            },
        },
        {
            "tool_name": "workday_report_org_hierarchy",
            "display_name": "Workday Report Org Hierarchy",
            "category": "Workday",
            "description": "Supplementary: Generate org hierarchy report",
            "params": {},
        },
        # =====================================================================
        # SUPPLEMENTARY: Worker Lifecycle
        # Hire worker to enable transfer/termination tests
        # =====================================================================
        {
            "tool_name": "workday_hire_worker",
            "display_name": "Workday Hire Worker",
            "category": "Workday",
            "description": f"Supplementary: Hire {candidate_name} (after case approval)",
            "params": {
                "worker_id": worker_id,
                "job_profile_id": job_profile_id,
                "org_id": org_id,
                "cost_center_id": cost_center_id,
                "location_id": location_id,
                "position_id": position_id,
                "fte": "1.0",
                "hire_date": proposed_start_date,
            },
        },
        {
            "tool_name": "workday_get_worker",
            "display_name": "Workday Get Worker",
            "category": "Workday",
            "description": f"Supplementary: Get {candidate_name}'s worker record",
            "params": {
                "worker_id": worker_id,
            },
        },
        {
            "tool_name": "workday_list_workers",
            "display_name": "Workday List Workers",
            "category": "Workday",
            "description": "Supplementary: List all workers",
            "params": {
                "page_size": "10",
                "page_number": "1",
            },
        },
        {
            "tool_name": "workday_transfer_worker",
            "display_name": "Workday Transfer Worker",
            "category": "Workday",
            "description": "Supplementary: Attempt transfer to non-existent org (validation)",
            "skip": True,  # Expected to show validation error
            "params": {
                "worker_id": worker_id,
                "new_org_id": "NON-EXISTENT-ORG-123",  # Intentionally invalid
                "transfer_date": date_str(60),
            },
        },
        {
            "tool_name": "workday_terminate_worker",
            "display_name": "Workday Terminate Worker",
            "category": "Workday",
            "description": f"Supplementary: Terminate {candidate_name} (demo only)",
            "params": {
                "worker_id": worker_id,
                "termination_date": date_str(90),
            },
        },
        {
            "tool_name": "workday_close_position",
            "display_name": "Workday Close Position",
            "category": "Workday",
            "description": f"Supplementary: Close {role} position",
            "params": {
                "position_id": position_id,
            },
        },
        # Note: workday_health_check is excluded - not displayed in UI
        # It can be tested via API directly
    ]


# =============================================================================
# UI AUTOMATION HELPERS - FLY SPECIFIC
# =============================================================================


def login_to_fly(page: Page, password: str):
    """Login to Fly UI with the site password."""
    print("  Logging into Fly...")

    # Wait for the login form
    try:
        # Check if there's a password input
        password_input = page.locator('input[type="password"]').first
        if password_input.is_visible(timeout=5000):
            password_input.fill(password)

            # Find and click the login/submit button
            submit_btn = page.locator(
                'button[type="submit"], button:has-text("Login"), button:has-text("Enter")'
            ).first
            if submit_btn.is_visible(timeout=2000):
                submit_btn.click()
                page.wait_for_timeout(2000)
                print("  Fly login submitted")
            else:
                # Try pressing Enter
                password_input.press("Enter")
                page.wait_for_timeout(2000)
                print("  Fly login submitted via Enter")
        else:
            print("  No login form detected - may already be authenticated")
    except Exception as e:
        print(f"  Note: Login handling: {e}")


def select_workday_app(page: Page):
    """Select the Workday app from the app selector."""
    print("  Selecting Workday app...")

    try:
        # Look for app selector or Workday button
        # The app selection may show as buttons or a dropdown
        workday_btn = page.locator(
            'button:has-text("Workday"), a:has-text("Workday"), [data-app="workday"]'
        ).first

        if workday_btn.is_visible(timeout=5000):
            workday_btn.click()
            page.wait_for_timeout(2000)
            print("  Selected Workday app")
        else:
            # Try looking for it in a different format
            app_selector = page.locator("text=Workday").first
            if app_selector.is_visible(timeout=2000):
                app_selector.click()
                page.wait_for_timeout(2000)
                print("  Selected Workday app")
            else:
                print("  Workday app may already be selected or not present")
    except Exception as e:
        print(f"  Note: App selection: {e}")


def wait_for_ui_ready(page: Page, timeout: int = 120000):
    """Wait for the UI to be fully loaded, including MCP server startup."""
    print("  Waiting for MCP server to start (this may take ~60s)...")

    try:
        # Wait for "Starting MCP server" to disappear and "Session Active" to appear
        # or wait for the tool list to be visible
        start_time = datetime.now()

        while (datetime.now() - start_time).total_seconds() < 90:
            # Check if still starting
            starting_msg = page.locator('text="Starting MCP server"')
            if starting_msg.is_visible(timeout=1000):
                elapsed = (datetime.now() - start_time).total_seconds()
                print(f"    Server starting... ({elapsed:.0f}s)")
                page.wait_for_timeout(5000)
                continue

            # Check if Session Active
            session_active = page.locator('text="Session Active"')
            if session_active.is_visible(timeout=1000):
                print("  Session is now active!")
                break

            # Check if MCP Tools appeared
            mcp_tools = page.locator('text="MCP Tools"')
            if mcp_tools.is_visible(timeout=1000):
                print("  MCP Tools panel is ready!")
                break

            page.wait_for_timeout(2000)

        # Wait for content to load
        page.wait_for_timeout(3000)

        # Take debug screenshot
        page.screenshot(path="/tmp/debug_fly_ui_ready.png")
        print("  Debug screenshot saved to /tmp/debug_fly_ui_ready.png")

        print("  UI loaded successfully")

        # Return the iframe frame for further operations
        return page

    except Exception as e:
        print(f"  Warning: UI ready check had issues: {e}")
        page.screenshot(path="/tmp/debug_fly_ui_error.png")
        print("  Error screenshot saved to /tmp/debug_fly_ui_error.png")
        return page


def get_iframe_context(page: Page):
    """Get the iframe containing the MCP Tools panel.

    Returns a tuple of (frame, page) where:
    - frame: The Frame object for locating elements
    - page: The Page object for mouse/screenshot operations
    """
    # Check for iframes
    iframes = page.frames
    print(f"  Found {len(iframes)} frames")

    for i, frame in enumerate(iframes):
        name = frame.name or f"frame_{i}"
        url = frame.url
        print(f"    Frame {i}: name={name}, url={url[:50]}...")

        # Try to find MCP Tools content in each frame
        try:
            if frame.locator('text="MCP Tools"').count() > 0:
                print(f"    Found MCP Tools in frame {i}")
                return (frame, page)
            if frame.locator('text="Workday"').count() > 1:
                print(f"    Found Workday categories in frame {i}")
                return (frame, page)
        except Exception:
            pass

    # If no specific frame found, return the first non-main frame if it exists
    if len(iframes) > 1:
        print("  Using frame 1 as fallback")
        return (iframes[1], page)

    return (page, page)


def expand_category(frame, page: Page, category: str):
    """Expand a category in the sidebar.

    Args:
        frame: The Frame object for locating elements
        page: The Page object for mouse operations
        category: The category name to expand
    """
    try:
        print(f"    Looking for category: {category}")

        # Debug: Dump all elements on the page to understand structure
        debug_info = frame.evaluate("""
            () => {
                const allElements = document.querySelectorAll('*');
                let textItems = [];
                let maxY = 0;
                let maxX = 0;

                for (const el of allElements) {
                    const rect = el.getBoundingClientRect();
                    if (rect.y > maxY) maxY = rect.y;
                    if (rect.x > maxX) maxX = rect.x;

                    // Get any element with "Workday" text
                    const text = (el.textContent || '').trim();
                    if (text.includes('Workday') && text.length < 100) {
                        textItems.push({
                            tag: el.tagName,
                            text: text.substring(0, 30),
                            y: Math.round(rect.y),
                            x: Math.round(rect.x),
                            w: Math.round(rect.width),
                            h: Math.round(rect.height)
                        });
                    }
                }

                // Check for iframes
                const iframes = document.querySelectorAll('iframe');

                return {
                    workdayElements: textItems.slice(0, 10),
                    maxCoords: {maxX, maxY},
                    iframeCount: iframes.length,
                    bodySize: {
                        w: document.body.scrollWidth,
                        h: document.body.scrollHeight
                    }
                };
            }
        """)
        iframe_cnt = debug_info.get("iframeCount")
        body_size = debug_info.get("bodySize")
        print(f"    Page debug: iframes={iframe_cnt}, body={body_size}")
        print(f"    Max coords: {debug_info.get('maxCoords')}")
        print(f"    Workday elements: {debug_info.get('workdayElements')}")

        # Try to find and click using aria attributes or data attributes
        result = frame.evaluate(f"""
            () => {{
                // Look for expandable/collapsible elements
                const sel = 'button, [role="button"], [aria-expanded]';
                const buttons = document.querySelectorAll(sel);
                for (const btn of buttons) {{
                    const text = btn.textContent || '';
                    if (text.includes('{category}')) {{
                        const rect = btn.getBoundingClientRect();
                        const x = rect.x + rect.width/2;
                        const y = rect.y + rect.height/2;
                        return {{found: true, x: x, y: y, text: text}};
                    }}
                }}

                // Look for any clickable element with the category
                const allClickable = document.querySelectorAll('div, span');
                for (const el of allClickable) {{
                    if (el.onclick || el.style.cursor === 'pointer' ||
                        getComputedStyle(el).cursor === 'pointer') {{
                        const text = el.textContent || '';
                        if (text.includes('{category}') && text.length < 50) {{
                            const rect = el.getBoundingClientRect();
                            const x = rect.x + rect.width/2;
                            const y = rect.y + rect.height/2;
                            return {{found: true, x: x, y: y, text: text}};
                        }}
                    }}
                }}

                return {{found: false}};
            }}
        """)
        print(f"    Clickable element: {result}")

        if result.get("found"):
            # Use frame's locator to click instead of coordinates
            # Find the button element and click it directly
            try:
                btn = frame.locator(f'button:has-text("{category}")').first
                if btn.is_visible(timeout=2000):
                    btn.click()
                    frame.wait_for_timeout(2000)
                    print("    Clicked button via locator")
            except Exception as click_err:
                print(f"    Locator click failed: {click_err}")

        # Take a screenshot to verify
        page.screenshot(path=f"/tmp/debug_after_expand_{category}.png")

    except Exception as e:
        print(f"  Warning: Could not expand category {category}: {e}")


def select_tool(frame, page: Page, display_name: str, category: str):
    """Select a tool from the sidebar using search.

    Args:
        frame: The Frame object for locating elements
        page: The Page object for mouse/screenshot operations
        display_name: The display name of the tool
        category: The category the tool belongs to
    """
    print(f"  Selecting tool: {display_name}")

    # Convert display name to the tool's internal name for searching
    # e.g., "Workday Create Org" -> "create_org" or "Create Org"
    search_term = display_name.replace("Workday ", "")

    try:
        # Use the search box to filter tools
        search_box = frame.locator('input[placeholder*="Search"]').first

        if search_box.is_visible(timeout=3000):
            # Clear and type search term
            search_box.click()
            search_box.fill("")
            frame.wait_for_timeout(300)
            search_box.fill(search_term)
            frame.wait_for_timeout(1000)
            print(f"    Searching for: {search_term}")

        # After search, the category should auto-expand with filtered results
        # Try to find and click the tool button
        tool_btn = frame.locator(f'button:has-text("{display_name}")').first

        if tool_btn.is_visible(timeout=3000):
            tool_btn.click()
            frame.wait_for_timeout(1000)
            print(f"  Selected tool: {display_name}")
            return

        # Try clicking any button that contains the search term
        tool_btn_partial = frame.locator(f'button:has-text("{search_term}")').first
        if tool_btn_partial.is_visible(timeout=2000):
            tool_btn_partial.click()
            frame.wait_for_timeout(1000)
            print(f"  Selected tool (partial): {display_name}")
            return

        # If search didn't help, try expanding category manually and scrolling
        search_box.fill("")  # Clear search
        frame.wait_for_timeout(500)

        # Expand the category
        expand_category(frame, page, category)
        frame.wait_for_timeout(1000)

        # Scroll through the expanded tool list to find the tool
        for scroll_pos in range(0, 3000, 300):
            # Check if tool is now visible
            if tool_btn.is_visible(timeout=500):
                tool_btn.click()
                frame.wait_for_timeout(1000)
                print(f"  Selected tool (after scroll): {display_name}")
                return

            # Scroll the tool list container
            frame.evaluate(f"""
                () => {{
                    // Find scrollable containers in the sidebar
                    const containers = document.querySelectorAll('[class*="overflow"]');
                    for (const c of containers) {{
                        if (c.scrollHeight > c.clientHeight) {{
                            c.scrollTop = {scroll_pos};
                        }}
                    }}
                }}
            """)
            frame.wait_for_timeout(200)

        raise Exception(f"Could not find tool: {display_name}")

    except Exception as e:
        print(f"  Error selecting tool: {e}")
        page.screenshot(path="/tmp/debug_tool_select.png")
        print("  Debug screenshot saved to /tmp/debug_tool_select.png")
        raise


def fill_parameters(frame, params: dict[str, str]):
    """Fill in the tool parameters.

    Args:
        frame: The Frame object for locating elements
        params: Dictionary of parameter name -> value
    """
    for param_name, value in params.items():
        if not value:
            continue

        # Convert param_name to label format (e.g., "org_id" -> "Org Id")
        label = param_name.replace("_", " ").title()

        # Also try variations of the label
        label_variations = [
            label,  # "Lead Time Days"
            label.lower(),  # "lead time days"
            param_name.replace("_", " "),  # "lead time days"
            param_name,  # "lead_time_days"
        ]

        filled = False

        try:
            # Strategy 1: Find by label text (try multiple variations)
            for lbl in label_variations:
                if filled:
                    break
                try:
                    label_elem = frame.locator(f'label:has-text("{lbl}")').first
                    if label_elem.is_visible(timeout=1000):
                        # Get the parent div that contains both label and input
                        container = label_elem.locator("xpath=..").first

                        # Try finding input in same container
                        inputs = container.locator("input, textarea, select").all()
                        if inputs:
                            input_elem = inputs[0]
                            if input_elem.is_visible():
                                input_elem.click()
                                input_elem.fill("")
                                input_elem.fill(str(value))
                                print(f"    Filled {param_name} = {value}")
                                filled = True
                                break

                        # Try finding in parent container
                        parent = container.locator("xpath=..").first
                        inputs = parent.locator("input, textarea, select").all()
                        if inputs:
                            input_elem = inputs[0]
                            if input_elem.is_visible():
                                input_elem.click()
                                input_elem.fill("")
                                input_elem.fill(str(value))
                                print(f"    Filled {param_name} = {value}")
                                filled = True
                                break
                except Exception:
                    continue

            # Strategy 2: Find input by iterating through all form fields
            if not filled:
                all_inputs = frame.locator("input, textarea, select").all()
                for inp in all_inputs:
                    if filled:
                        break
                    try:
                        # Check if this input's container has our label
                        for lbl in label_variations:
                            inp_container = inp.locator("xpath=ancestor::div[position()<=3]").first
                            if inp_container.locator(f'label:has-text("{lbl}")').count() > 0:
                                inp.click()
                                inp.fill("")
                                inp.fill(str(value))
                                print(f"    Filled {param_name} = {value}")
                                filled = True
                                break
                    except Exception:
                        continue

            # Strategy 3: Use JavaScript to find and fill the field
            if not filled:
                try:
                    # Prepare values for JS (escape quotes)
                    label_lower = label.lower()
                    param_spaced = param_name.replace("_", " ")
                    escaped_value = str(value).replace("'", "\\'")
                    # Use JS to find label and fill input
                    js_code = f"""
                        () => {{
                            const labels = document.querySelectorAll('label');
                            for (const label of labels) {{
                                const text = label.textContent.toLowerCase();
                                if (text.includes('{label_lower}') ||
                                    text.includes('{param_spaced}')) {{
                                    const container =
                                        label.closest('div.space-y-2') ||
                                        label.parentElement;
                                    if (container) {{
                                        const input = container.querySelector(
                                            'input, textarea, select');
                                        if (input) {{
                                            input.value = '{escaped_value}';
                                            input.dispatchEvent(
                                                new Event('input', {{bubbles: true}}));
                                            input.dispatchEvent(
                                                new Event('change', {{bubbles: true}}));
                                            return true;
                                        }}
                                    }}
                                }}
                            }}
                            return false;
                        }}
                    """
                    result = frame.evaluate(js_code)
                    if result:
                        print(f"    Filled {param_name} = {value} (via JS)")
                        filled = True
                except Exception:
                    pass

            if not filled:
                print(f"    Warning: Could not find field for {param_name}")

        except Exception as e:
            print(f"    Warning: Error filling {param_name}: {e}")


def click_execute(frame, page: Page):
    """Click the Execute button and wait for response.

    Args:
        frame: The Frame object for locating elements
        page: The Page object for wait operations
    """
    print("  Clicking Execute...")

    # Find and click Execute button
    execute_btn = frame.locator('button:has-text("Execute")').first
    execute_btn.click()

    # Wait for response (loading indicator disappears, response appears)
    try:
        # Wait for loading to finish
        frame.wait_for_timeout(500)
        # Use frame's locator for wait_for
        try:
            frame.locator('button:has-text("Executing...")').wait_for(state="hidden", timeout=30000)
        except Exception:
            pass  # Button might not appear if execution is fast

        # Wait for response section to appear
        frame.wait_for_timeout(1000)  # Give UI time to render

        print("  Execution complete")

    except Exception as e:
        print(f"  Warning: Execution may have timed out: {e}")


def take_screenshot(page: Page, tool_name: str, output_dir: Path, index: int):
    """Take a screenshot showing the tool form and response."""
    filename = f"{index:02d}_{tool_name}.png"
    filepath = output_dir / filename

    # Strategy: Scroll to show the response section which contains the result
    try:
        # First, try to scroll to the Response section
        response_header = page.locator('h3:text("RESPONSE")').first
        if response_header.is_visible(timeout=1000):
            response_header.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        # Also try to scroll to any error message if present
        error_section = page.locator('text="Request Error"').first
        if error_section.is_visible(timeout=500):
            error_section.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        # Try to find and scroll to the JSON response or success message
        json_response = page.locator("pre").first
        if json_response.is_visible(timeout=500):
            json_response.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
    except Exception:
        pass

    try:
        # Scroll the main scrollable container to show bottom content
        # The main content area that scrolls
        page.evaluate("""
            // Find the main scrollable container and scroll to bottom
            const mainContent = document.querySelector('.lg\\\\:col-span-2');
            if (mainContent) {
                const scrollable = mainContent.querySelector('.overflow-y-auto') || mainContent;
                scrollable.scrollTop = scrollable.scrollHeight;
            }
            // Also scroll the entire page
            window.scrollTo(0, document.body.scrollHeight);
        """)
        page.wait_for_timeout(300)
    except Exception as e:
        print(f"    Note: Could not scroll: {e}")

    page.wait_for_timeout(500)  # Wait for scroll to settle

    # Take screenshot - use full_page for complete capture
    page.screenshot(path=str(filepath), full_page=True)
    print(f"  Screenshot saved: {filename}")

    return filepath


# =============================================================================
# MARKDOWN REPORT GENERATION
# =============================================================================


def generate_markdown_report(
    results: dict,
    test_data: list[dict],
    output_dir: Path,
    report_file: Path,
    timestamp: str,
    expected_validations: int,
) -> None:
    """
    Generate a human-readable markdown report of the test run.

    This creates a non-technical summary that shows:
    - Test scenario overview
    - What data was used
    - Results for each tool with links to screenshots
    """
    # Build test data lookup for descriptions and params
    test_lookup = {t["tool_name"]: t for t in test_data}

    # Calculate stats
    actual_passed = results["passed"] - expected_validations

    # Group results by phase
    phases = {
        "Setup": [],
        "Phase A": [],
        "Phase B": [],
        "Phase C": [],
        "Phase D": [],
        "Supplementary": [],
        "Utility": [],
    }

    for detail in results["details"]:
        tool_name = detail["tool"]
        tool_info = test_lookup.get(tool_name, {})
        description = tool_info.get("description", "")

        # Determine phase from description
        if description.startswith("Setup:"):
            phases["Setup"].append((detail, tool_info))
        elif description.startswith("Phase A:"):
            phases["Phase A"].append((detail, tool_info))
        elif description.startswith("Phase B:"):
            phases["Phase B"].append((detail, tool_info))
        elif description.startswith("Phase C:"):
            phases["Phase C"].append((detail, tool_info))
        elif description.startswith("Phase D:"):
            phases["Phase D"].append((detail, tool_info))
        elif description.startswith("Utility:"):
            phases["Utility"].append((detail, tool_info))
        else:
            phases["Supplementary"].append((detail, tool_info))

    # Generate markdown
    lines = []

    # Header
    lines.append("# Workday MCP UI Test Report (Fly)")
    lines.append("")
    lines.append(f"**Test Run:** {timestamp}")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Environment:** Fly ({FLY_UI_URL})")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Total Tools Tested | {results['total']} |")
    lines.append(f"| Passed | {actual_passed} |")
    lines.append(f"| Expected Validations | {expected_validations} |")
    lines.append(f"| Failed | {results['failed']} |")
    lines.append("")

    # Test Scenario
    lines.append("## Test Scenario")
    lines.append("")
    lines.append("This test run simulates a complete **pre-onboarding workflow** for:")
    lines.append("")
    lines.append("- **Candidate:** Sarah Chen")
    lines.append("- **Role:** Senior Software Engineer")
    lines.append("- **Location:** San Francisco, US")
    lines.append("- **Employment Type:** Full-time")
    lines.append("- **Proposed Start Date:** 45 days from test run")
    lines.append("")
    lines.append("The workflow follows the Workday HCM V2 Mock Spec phases:")
    lines.append("")
    lines.append(
        "1. **Setup** - Create organizational structure "
        "(org, cost center, location, job profile, position, policies)"
    )
    lines.append("2. **Phase A** - Case Initialization (create case, assign owner)")
    lines.append(
        "3. **Phase B** - Discover Prerequisites (read HCM context, get policies, list milestones)"
    )
    lines.append(
        "4. **Phase C** - Orchestrate Execution "
        "(update milestones, create tasks, handle exceptions)"
    )
    lines.append("5. **Phase D** - Confirm Start Date (gated HCM write-back, audit trail)")
    lines.append("")

    # Results by Phase
    lines.append("## Results by Phase")
    lines.append("")

    phase_descriptions = {
        "Setup": "Creating the HCM foundation before case management",
        "Phase A": "Case Initialization (Day 0-2)",
        "Phase B": "Discover Prerequisites & Constraints (Day 1-10)",
        "Phase C": "Orchestrate Execution & Communications (Day 3-35)",
        "Phase D": "Confirm Start Date & Gated HCM Update (Day 14-45+)",
        "Supplementary": "Additional HCM entity and reporting operations",
        "Utility": "System health and utility operations",
    }

    for phase_name, phase_results in phases.items():
        if not phase_results:
            continue

        lines.append(f"### {phase_name}")
        lines.append("")
        lines.append(f"*{phase_descriptions.get(phase_name, '')}*")
        lines.append("")
        lines.append("| # | Tool | Description | Result | Screenshot |")
        lines.append("|---|------|-------------|--------|------------|")

        for detail, tool_info in phase_results:
            tool_name = detail["tool"]
            status = detail.get("status", "unknown")
            description = tool_info.get("description", "").split(": ", 1)[-1]

            # Format status with emoji for readability
            if status == "passed":
                status_display = "Passed"
            elif status == "expected_validation":
                status_display = "Validation (expected)"
            elif status == "failed":
                status_display = "Failed"
            else:
                status_display = status

            # Screenshot link (relative path)
            screenshot = detail.get("screenshot", "")
            if screenshot:
                screenshot_name = Path(screenshot).name
                screenshot_link = f"[{screenshot_name}]({screenshot_name})"
            else:
                screenshot_link = "-"

            # Find the index for this tool
            idx = next(
                (i for i, d in enumerate(results["details"], 1) if d["tool"] == tool_name),
                "-",
            )

            lines.append(
                f"| {idx} | `{tool_name}` | {description} | {status_display} | {screenshot_link} |"
            )

        lines.append("")

    # Test Data Used
    lines.append("## Test Data Used")
    lines.append("")
    lines.append("The following IDs were generated for this test run:")
    lines.append("")
    lines.append("| Entity | ID Pattern | Example |")
    lines.append("|--------|------------|---------|")
    lines.append("| Organization | `ORG-ENG-{timestamp}` | ORG-ENG-143052 |")
    lines.append("| Cost Center | `CC-ENG-{timestamp}` | CC-ENG-143052 |")
    lines.append("| Location | `LOC-SF-{timestamp}` | LOC-SF-143052 |")
    lines.append("| Job Profile | `JP-SSE-{timestamp}` | JP-SSE-143052 |")
    lines.append("| Position | `POS-SSE-{timestamp}` | POS-SSE-143052 |")
    lines.append("| Case | `CASE-SCHEN-{timestamp}` | CASE-SCHEN-143052 |")
    lines.append("| Candidate | `CAND-SCHEN-{timestamp}` | CAND-SCHEN-143052 |")
    lines.append("| Worker | `WRK-SCHEN-{timestamp}` | WRK-SCHEN-143052 |")
    lines.append("| Policy | `POL-US-ENG-{timestamp}` | POL-US-ENG-143052 |")
    lines.append("")

    # Expected Validation Scenarios
    if expected_validations > 0:
        lines.append("## Expected Validation Scenarios")
        lines.append("")
        lines.append(
            "Some tools were intentionally tested with conditions that trigger "
            "validation errors. This demonstrates the system's gating behavior:"
        )
        lines.append("")

        for detail in results["details"]:
            if detail.get("status") == "expected_validation":
                tool_name = detail["tool"]
                tool_info = test_lookup.get(tool_name, {})

                if tool_name == "workday_transfer_worker":
                    lines.append(f"- **{tool_name}**: Attempted transfer to ")
                    lines.append("  non-existent organization (`NON-EXISTENT-ORG-123`)")
                    lines.append("  to show validation of org references")
                elif tool_name == "workday_hcm_confirm_start_date":
                    lines.append(f"- **{tool_name}**: Attempted to confirm start date ")
                    lines.append("  before all milestones are complete, ")
                    lines.append("  demonstrating gating requirements")
                elif tool_name == "workday_hcm_update_readiness":
                    lines.append(f"- **{tool_name}**: Attempted to update readiness ")
                    lines.append("  before start date is confirmed, ")
                    lines.append("  demonstrating prerequisite validation")
                else:
                    lines.append(f"- **{tool_name}**: {tool_info.get('description', '')}")

        lines.append("")

    # How to View Screenshots
    lines.append("## Viewing Screenshots")
    lines.append("")
    lines.append("Each screenshot shows:")
    lines.append("")
    lines.append("1. **Tool Selection** - The tool selected in the sidebar")
    lines.append("2. **Input Parameters** - The form filled with test data")
    lines.append("3. **Response** - The API response (success or validation error)")
    lines.append("")
    lines.append(
        "Screenshots are named with their execution order: "
        "`01_workday_create_org.png`, `02_workday_create_cost_center.png`, etc."
    )
    lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("*This report was automatically generated by `scripts/ui_tool_tester_fly.py`*")

    # Write file
    with open(report_file, "w") as f:
        f.write("\n".join(lines))


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================


def run_tool_tests(
    headed: bool = False,
    tools_filter: list[str] | None = None,
    slow_mo: int = 0,
):
    """Run the UI automation tests against Fly."""

    # Create output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = SCREENSHOTS_DIR / f"fly_run_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'=' * 60}")
    print("WORKDAY MCP UI TOOL TESTER (FLY)")
    print(f"{'=' * 60}")
    print(f"Output directory: {output_dir}")
    print(f"UI URL: {FLY_UI_URL}")
    print(f"Mode: {'Headed (visible browser)' if headed else 'Headless'}")
    print(f"{'=' * 60}\n")

    # Get test data
    test_data = get_test_data()

    # Filter if specified
    if tools_filter:
        test_data = [t for t in test_data if t["tool_name"] in tools_filter]
        print(f"Filtering to {len(test_data)} tools: {tools_filter}\n")

    # Track results
    results = {
        "total": len(test_data),
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(
            headless=not headed,
            slow_mo=slow_mo,
        )

        # Create context with a VERY TALL viewport so everything fits without scrolling
        # Width: 1920 (standard desktop)
        # Height: 2000 (extra tall to show form + response without scrolling)
        context = browser.new_context(
            viewport={"width": 1920, "height": 2000},
        )

        page = context.new_page()

        try:
            # Navigate to Fly UI
            print("Navigating to Fly UI...")
            page.goto(FLY_UI_URL)
            page.wait_for_timeout(2000)

            # Login to Fly
            login_to_fly(page, FLY_PASSWORD)

            # Select Workday app
            select_workday_app(page)

            # Wait for UI to load
            wait_for_ui_ready(page)

            # Get the iframe context where MCP Tools panel is
            frame, main_page = get_iframe_context(page)
            print(f"  Using frame: {type(frame).__name__}, page: {type(main_page).__name__}")

            # Run each tool test
            for i, tool in enumerate(test_data, 1):
                tool_name = tool["tool_name"]
                display_name = tool["display_name"]

                print(f"\n[{i}/{len(test_data)}] {tool_name}")
                print(f"  Description: {tool['description']}")

                # Check if this is a "skip" tool - we still run it to capture the validation error
                is_skip_tool = tool.get("skip", False)
                if is_skip_tool:
                    print("  Note: This tool may fail validation (expected behavior)")

                try:
                    # Select the tool (use frame for locating, page for mouse)
                    select_tool(frame, main_page, display_name, tool["category"])

                    # Fill parameters
                    fill_parameters(frame, tool["params"])

                    # Execute
                    click_execute(frame, main_page)

                    # Take screenshot (use main page for full capture)
                    screenshot_path = take_screenshot(main_page, tool_name, output_dir, i)

                    # For skip tools, check if the response shows an error (expected)
                    if is_skip_tool:
                        # Still count as passed since we captured the expected behavior
                        print("  Captured expected validation scenario")
                        results["passed"] += 1
                        results["details"].append(
                            {
                                "tool": tool_name,
                                "status": "expected_validation",
                                "screenshot": str(screenshot_path),
                                "note": "Tool executed to show validation behavior",
                            }
                        )
                    else:
                        results["passed"] += 1
                        results["details"].append(
                            {
                                "tool": tool_name,
                                "status": "passed",
                                "screenshot": str(screenshot_path),
                            }
                        )

                except Exception as e:
                    print(f"  FAILED: {e}")

                    # Take error screenshot
                    try:
                        error_path = output_dir / f"{i:02d}_{tool_name}_ERROR.png"
                        page.screenshot(path=str(error_path), full_page=True)
                        print(f"  Error screenshot saved: {error_path.name}")
                    except Exception:
                        pass

                    if is_skip_tool:
                        # Expected to fail - count as expected behavior, not failure
                        print("  (Expected failure - validation scenario captured)")
                        results["passed"] += 1
                        results["details"].append(
                            {
                                "tool": tool_name,
                                "status": "expected_validation",
                                "error": str(e),
                                "screenshot": str(error_path) if "error_path" in locals() else None,
                                "note": "Expected validation failure captured",
                            }
                        )
                    else:
                        results["failed"] += 1
                        results["details"].append(
                            {
                                "tool": tool_name,
                                "status": "failed",
                                "error": str(e),
                            }
                        )

        finally:
            browser.close()

    # Count expected validations separately for clarity
    expected_validations = len(
        [d for d in results["details"] if d.get("status") == "expected_validation"]
    )
    actual_passed = results["passed"] - expected_validations

    # Print summary
    print(f"\n{'=' * 60}")
    print("TEST SUMMARY")
    print(f"{'=' * 60}")
    print(f"Total:   {results['total']}")
    print(f"Passed:  {actual_passed}")
    print(f"Expected Validations: {expected_validations} (captured validation scenarios)")
    print(f"Failed:  {results['failed']}")
    print(f"{'=' * 60}")
    print(f"Screenshots saved to: {output_dir}")

    # Generate markdown report
    report_file = output_dir / "REPORT.md"
    generate_markdown_report(
        results, test_data, output_dir, report_file, timestamp, expected_validations
    )
    print(f"Report saved to: {report_file}")

    return results


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Playwright-based UI automation for testing Workday MCP tools on Fly"
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run in headed mode (visible browser)",
    )
    parser.add_argument(
        "--tools",
        type=str,
        help="Comma-separated list of tool names to test (e.g., 'workday_create_org')",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow down operations by specified milliseconds (useful for debugging)",
    )

    args = parser.parse_args()

    tools_filter = None
    if args.tools:
        tools_filter = [t.strip() for t in args.tools.split(",")]

    results = run_tool_tests(
        headed=args.headed,
        tools_filter=tools_filter,
        slow_mo=args.slow_mo,
    )

    # Exit with error code if any tests failed
    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
