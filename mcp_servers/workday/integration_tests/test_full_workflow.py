"""End-to-end integration test for the complete V1+V2 workflow.

This test covers the Maria Schmidt pre-onboarding scenario (35 steps):
- Phase 0: Foundation Setup (7 tools)
- Phase A: Case Initialization (1 tool)
- Phase B: Policy Discovery (6 tools)
- Phase C: Milestone Tracking (5 tools + negative test)
- Phase D-1: Exception Handling (4 tools)
- Phase D-2: Gated HCM Write-Back (5 tools)
- Phase E: V1 Hire Execution (3 tools)
- Phase F: V1 Reporting (4 tools)

Reference: integration_tests/V1_V2_WORKFLOW_EXAMPLE.md

Note: Test data uses unique IDs with a UUID suffix to allow repeated runs
without conflicts. All data is created via REST API calls (not hardcoded in DB).
"""

import uuid
from datetime import date, timedelta

import pytest

from .helpers import RestClient, get_user_credentials


def date_str(offset_days: int = 0) -> str:
    """Get a date as YYYY-MM-DD string, relative to today.

    Args:
        offset_days: Days to add/subtract from today (negative for past)

    Returns:
        Date string in YYYY-MM-DD format
    """
    return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# =============================================================================
# TEST CLASS
# =============================================================================


class TestFullWorkflowIntegration:
    """End-to-end integration test for the complete V1+V2 workflow.

    This test class implements the Maria Schmidt pre-onboarding scenario
    as documented in integration_tests/V1_V2_WORKFLOW_EXAMPLE.md.

    The scenario covers:
    - Hiring Maria Schmidt as Senior Software Engineer in Berlin, Germany
    - Relocating from US with Blue Card visa requirements
    - Encountering a visa delay
    - Requesting and approving an exception
    - Successfully onboarding with adjusted start date
    """

    @pytest.fixture
    def workflow_ids(self) -> dict:
        """Generate unique IDs for all entities in this workflow test.

        Returns a dict with all the IDs needed for the test, allowing
        the test to run multiple times without ID conflicts.
        """
        suffix = uuid.uuid4().hex[:8].upper()
        return {
            # Organization IDs (3-level hierarchy)
            "parent_org_id": f"ORG-GLOBAL-{suffix}",
            "eu_org_id": f"ORG-ENG-EU-{suffix}",
            "child_org_id": f"ORG-ENG-BERLIN-{suffix}",
            # Job Profile
            "job_profile_id": f"JP-SWE-SR-{suffix}",
            # Cost Center
            "cost_center_id": f"CC-ENG-DE-{suffix}",
            # Location
            "location_id": f"LOC-BERLIN-{suffix}",
            # Position (for V1 hire)
            "position_id": f"POS-2025-{suffix}",
            # Case
            "case_id": f"CASE-2025-{suffix}",
            "candidate_id": f"CAND-MS-{suffix}",
            "requisition_id": f"REQ-2025-ENG-{suffix}",
            # Worker (created in Phase E)
            "worker_id": f"EMP-DE-2025-{suffix}",
            # Dates - must be in the future to pass lead time validation
            # We use dates 30+ days in the future to satisfy the 21-day Germany lead time
            "proposed_start_date": date_str(45),  # 45 days from today
            "confirmed_start_date": date_str(60),  # 60 days from today
            "due_date": date_str(30),  # 30 days from today
        }

    # =========================================================================
    # PHASE 0: FOUNDATION SETUP (7 steps)
    # =========================================================================

    def _phase_0_foundation_setup(self, rest_client: RestClient, ids: dict) -> dict:
        """Create organizational entities before any hiring.

        Steps:
        1. Create parent org (Global Operations)
        2. Create EU region org (Engineering EU)
        3. Create Berlin org (Engineering Berlin)
        4. Create job profile (Senior Software Engineer)
        5. Create cost center (Engineering Germany)
        6. Create location (Berlin Office)
        7. Create position (open position for hire)
        """
        results = {}

        # Step 1: Create parent org (Global Operations)
        parent_org = rest_client.call_tool(
            "workday_create_org",
            {
                "org_id": ids["parent_org_id"],
                "org_name": "Global Operations",
                "org_type": "Supervisory",
            },
        )
        assert parent_org["org_id"] == ids["parent_org_id"]
        results["parent_org"] = parent_org

        # Step 2: Create EU region org (Engineering EU)
        eu_org = rest_client.call_tool(
            "workday_create_org",
            {
                "org_id": ids["eu_org_id"],
                "org_name": "Engineering EU",
                "org_type": "Supervisory",
                "parent_org_id": ids["parent_org_id"],
            },
        )
        assert eu_org["org_id"] == ids["eu_org_id"]
        results["eu_org"] = eu_org

        # Step 3: Create Berlin org (Engineering Berlin)
        child_org = rest_client.call_tool(
            "workday_create_org",
            {
                "org_id": ids["child_org_id"],
                "org_name": "Engineering Berlin",
                "org_type": "Supervisory",
                "parent_org_id": ids["eu_org_id"],
            },
        )
        assert child_org["org_id"] == ids["child_org_id"]
        results["child_org"] = child_org

        # Step 4: Create job profile (Senior Software Engineer)
        job_profile = rest_client.call_tool(
            "workday_create_job_profile",
            {
                "job_profile_id": ids["job_profile_id"],
                "title": "Senior Software Engineer",
                "job_family": "Engineering",
                "job_level": "Senior",
            },
        )
        assert job_profile["job_profile_id"] == ids["job_profile_id"]
        results["job_profile"] = job_profile

        # Step 5: Create cost center (Engineering Germany)
        cost_center = rest_client.call_tool(
            "workday_create_cost_center",
            {
                "cost_center_id": ids["cost_center_id"],
                "cost_center_name": "Engineering Germany",
                "org_id": ids["child_org_id"],
            },
        )
        assert cost_center["cost_center_id"] == ids["cost_center_id"]
        results["cost_center"] = cost_center

        # Step 6: Create location (Berlin Office)
        location = rest_client.call_tool(
            "workday_create_location",
            {
                "location_id": ids["location_id"],
                "location_name": "Berlin Office",
                "city": "Berlin",
                "country": "DE",
            },
        )
        assert location["location_id"] == ids["location_id"]
        results["location"] = location

        # Step 7: Create position (V1 tool - open position for hire)
        position = rest_client.call_tool(
            "workday_create_position",
            {
                "position_id": ids["position_id"],
                "job_profile_id": ids["job_profile_id"],
                "org_id": ids["child_org_id"],
                "fte": 1.0,
                "status": "open",
            },
        )
        assert position["position_id"] == ids["position_id"]
        assert position["status"] == "open"
        results["position"] = position

        return results

    # =========================================================================
    # PHASE A: CASE INITIALIZATION (1 step)
    # =========================================================================

    def _phase_a_case_initialization(self, rest_client: RestClient, ids: dict) -> dict:
        """Create pre-onboarding case for Maria Schmidt.

        Steps:
        8. Create case (auto-creates 4 milestones + audit entry)

        Verifications:
        - Case status is "open"
        - 4 milestones auto-initialized (screening, work_authorization, documents, approvals)
        - All milestones have status "pending"
        """
        # Step 8: Create case
        case = rest_client.call_tool(
            "workday_create_case",
            {
                "case_id": ids["case_id"],
                "candidate_id": ids["candidate_id"],
                "requisition_id": ids["requisition_id"],
                "role": "Senior Software Engineer",
                "country": "DE",
                "employment_type": "full_time",
                "owner_persona": "pre_onboarding_coordinator",
                "proposed_start_date": ids["proposed_start_date"],
                "due_date": ids["due_date"],
            },
        )

        # Verify case created with correct status
        assert case["case_id"] == ids["case_id"]
        assert case["status"] == "open"
        assert case["candidate_id"] == ids["candidate_id"]
        assert case["country"] == "DE"

        # Verify 4 milestones auto-initialized
        assert len(case["milestones"]) == 4
        milestone_types = {m["milestone_type"] for m in case["milestones"]}
        expected_types = {"screening", "work_authorization", "documents", "approvals"}
        assert milestone_types == expected_types

        # All milestones should be pending
        for milestone in case["milestones"]:
            assert milestone["status"] == "pending"

        return case

    # =========================================================================
    # PHASE B: POLICY DISCOVERY (6 steps)
    # =========================================================================

    def _phase_b_policy_discovery(
        self, rest_client: RestClient, ids: dict, policy_ids: list[str]
    ) -> dict:
        """Find applicable policies and create tasks.

        Steps:
        9. Get applicable policies for Germany/Engineer
        10. Attach policies to case
        11. Read position context
        12. Create visa task
        13. Create background check task
        14. Update case status to in_progress
        """
        results = {}

        # Step 9: Get applicable policies
        policies = rest_client.call_tool(
            "workday_policies_get_applicable",
            {
                "country": "DE",
                "role": "Senior Software Engineer",
                "employment_type": "full_time",
            },
        )
        assert len(policies["policies"]) >= 1
        results["policies"] = policies

        # Step 10: Attach policies to case
        # Use the policy IDs from the seeded policies
        attached = rest_client.call_tool(
            "workday_policies_attach_to_case",
            {
                "case_id": ids["case_id"],
                "policy_ids": policy_ids,
                "decision_context": (
                    "Candidate relocating from US, requires Blue Card visa. German policies apply."
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert len(attached) >= 1
        results["attached_policies"] = attached

        # Step 11: Read position context
        position_ctx = rest_client.call_tool(
            "workday_hcm_read_position",
            {
                "case_id": ids["case_id"],
            },
        )
        assert position_ctx["country"] == "DE"
        assert position_ctx["role"] == "Senior Software Engineer"
        results["position_context"] = position_ctx

        # Step 12: Create visa task
        visa_task = rest_client.call_tool(
            "workday_tasks_create",
            {
                "case_id": ids["case_id"],
                "milestone_type": "work_authorization",
                "title": "Submit Blue Card application to immigration attorney",
                "owner_persona": "pre_onboarding_coordinator",
                "due_date": date_str(10),  # 10 days from now
            },
        )
        assert visa_task["task_id"] is not None
        results["visa_task"] = visa_task

        # Step 13: Create background check task
        bg_task = rest_client.call_tool(
            "workday_tasks_create",
            {
                "case_id": ids["case_id"],
                "milestone_type": "screening",
                "title": "Schedule background check with vendor",
                "owner_persona": "pre_onboarding_coordinator",
                "due_date": date_str(8),  # 8 days from now
            },
        )
        assert bg_task["task_id"] is not None
        results["bg_task"] = bg_task

        # Step 14: Update case status to in_progress
        updated_case = rest_client.call_tool(
            "workday_update_case",
            {
                "case_id": ids["case_id"],
                "new_status": "in_progress",
                "rationale": "All prerequisites identified, coordination work beginning",
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert updated_case["status"] == "in_progress"
        results["updated_case"] = updated_case

        return results

    # =========================================================================
    # PHASE C: MILESTONE TRACKING (5 steps + negative test)
    # =========================================================================

    def _phase_c_milestone_tracking(
        self, rest_client: RestClient, ids: dict, policy_ids: list[str]
    ) -> dict:
        """Update progress on prerequisites.

        Steps:
        15. Mark screening -> completed (with evidence)
        16. Mark documents -> completed (with evidence)
        17. Mark approvals -> completed (with evidence)
        18. Mark work_authorization -> blocked (visa delay!)
        19. Search for at-risk German cases

        Negative Test:
        - Attempt gated write-back while milestone is blocked (should FAIL)
        """
        results = {}

        # Step 15: Mark screening completed
        screening = rest_client.call_tool(
            "workday_milestones_update",
            {
                "case_id": ids["case_id"],
                "milestone_type": "screening",
                "new_status": "completed",
                "evidence_link": "https://vendor.com/reports/MS-001-clear",
                "notes": "Background check cleared, no issues found",
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert screening["status"] == "completed"
        results["screening"] = screening

        # Step 16: Mark documents completed
        documents = rest_client.call_tool(
            "workday_milestones_update",
            {
                "case_id": ids["case_id"],
                "milestone_type": "documents",
                "new_status": "completed",
                "evidence_link": "https://docs.acme.com/onboarding/MS-001",
                "notes": "All required documents received and verified",
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert documents["status"] == "completed"
        results["documents"] = documents

        # Step 17: Mark approvals completed
        approvals = rest_client.call_tool(
            "workday_milestones_update",
            {
                "case_id": ids["case_id"],
                "milestone_type": "approvals",
                "new_status": "completed",
                "evidence_link": "https://approvals.acme.com/CASE-2025-0042",
                "notes": "Hiring manager and VP Engineering approved",
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert approvals["status"] == "completed"
        results["approvals"] = approvals

        # Step 18: Mark work_authorization BLOCKED (visa delay!)
        work_auth = rest_client.call_tool(
            "workday_milestones_update",
            {
                "case_id": ids["case_id"],
                "milestone_type": "work_authorization",
                "new_status": "blocked",
                "notes": (
                    "Blue Card processing delayed by German immigration. "
                    "Expected ready date: Feb 20, 2025"
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert work_auth["status"] == "blocked"
        results["work_auth"] = work_auth

        # Step 19: Search for at-risk German cases
        search_results = rest_client.call_tool(
            "workday_search_case",
            {
                "status": "in_progress",
                "country": "DE",
            },
        )
        case_ids_found = [c["case_id"] for c in search_results["cases"]]
        assert ids["case_id"] in case_ids_found
        results["search_results"] = search_results

        # NEGATIVE TEST: Attempt gated write-back while milestone is blocked
        with pytest.raises(AssertionError) as exc_info:
            rest_client.call_tool(
                "workday_hcm_confirm_start_date",
                {
                    "case_id": ids["case_id"],
                    "confirmed_start_date": ids["confirmed_start_date"],
                    "policy_refs": policy_ids[:1] if policy_ids else [],
                    "evidence_links": ["https://vendor.com/reports/MS-001-clear"],
                    "rationale": "Attempting before milestone complete",
                    "actor_persona": "pre_onboarding_coordinator",
                },
            )
        assert "E_GATE_001" in str(exc_info.value) or "500" in str(exc_info.value)

        return results

    # =========================================================================
    # PHASE D-1: EXCEPTION HANDLING (4 steps)
    # =========================================================================

    def _phase_d1_exception_handling(
        self, rest_client: RestClient, ids: dict, policy_ids: list[str]
    ) -> dict:
        """Request and approve exception for blocked milestone.

        Steps:
        20. Request exception for work_authorization
        21. HR Admin reviews audit history
        22. HR Admin approves exception
        23. Update milestone from blocked -> waived (requires approved exception)
        """
        results = {}

        # Get the visa policy ID (second in list) or use first if available
        visa_policy_id = policy_ids[1] if len(policy_ids) > 1 else policy_ids[0]

        # Step 20: Request exception
        exception = rest_client.call_tool(
            "workday_exception_request",
            {
                "case_id": ids["case_id"],
                "milestone_type": "work_authorization",
                "reason": (
                    "Blue Card processing delayed by German immigration authorities. "
                    "Immigration attorney confirms approval expected by Feb 20. "
                    "Requesting exception to proceed with conditional start."
                ),
                "affected_policy_refs": [visa_policy_id],
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert exception["exception_id"] is not None
        assert exception["approval_status"] == "pending"
        exception_id = exception["exception_id"]
        results["exception"] = exception

        # NEGATIVE TEST: Attempt to waive milestone BEFORE exception is approved
        with pytest.raises(AssertionError) as exc_info:
            rest_client.call_tool(
                "workday_milestones_update",
                {
                    "case_id": ids["case_id"],
                    "milestone_type": "work_authorization",
                    "new_status": "waived",
                    "evidence_link": f"https://exceptions.acme.com/{exception_id}",
                    "notes": "Attempting to waive before exception is approved",
                    "actor_persona": "pre_onboarding_coordinator",
                },
            )
        assert "E_MILE_003" in str(exc_info.value) or "500" in str(exc_info.value)

        # Step 21: HR Admin reviews audit history
        audit = rest_client.call_tool(
            "workday_audit_get_history",
            {
                "case_id": ids["case_id"],
            },
        )
        assert len(audit["entries"]) >= 5
        results["audit_review"] = audit

        # NEGATIVE TEST: Attempt to approve exception as coordinator (should FAIL)
        # This proves role-based access control is working
        with pytest.raises(AssertionError) as exc_info:
            rest_client.call_tool(
                "workday_exception_approve",
                {
                    "exception_id": exception_id,
                    "approval_status": "approved",
                    "approval_notes": "Attempting approval as coordinator",
                    "actor_persona": "hr_admin",
                },
            )
        # Should fail with 403 Forbidden (insufficient permissions)
        assert "403" in str(exc_info.value) or "Forbidden" in str(exc_info.value)
        results["auth_negative_test"] = "coordinator correctly denied exception approval"
        print("    [AUTH] ✓ Coordinator denied (403) - RBAC working!")

        # Step 22: Switch to HR Admin and approve exception
        print("    [AUTH] Switching user: coordinator → hr_admin")
        rest_client.switch_user(*get_user_credentials("hr_admin"))
        assert rest_client.current_user == "hr_admin"
        print(f"    [AUTH] ✓ Now logged in as: {rest_client.current_user}")
        print(f"    [AUTH] ✓ New token: {rest_client._auth_token[:20]}...")

        approved = rest_client.call_tool(
            "workday_exception_approve",
            {
                "exception_id": exception_id,
                "approval_status": "approved",
                "approval_notes": (
                    "Approved conditional start. Conditions: "
                    "(1) If Blue Card not received by Feb 25, escalate to Legal. "
                    "(2) Candidate may begin orientation remotely from US if needed. "
                    "(3) No Germany-based work until visa confirmed."
                ),
                "actor_persona": "hr_admin",
            },
        )
        assert approved["approval_status"] == "approved"
        assert approved["approved_by"] == "hr_admin"
        results["approved_exception"] = approved
        print("    [AUTH] ✓ Exception approved by hr_admin - elevated permissions worked!")

        # Switch back to coordinator for remaining steps
        print("    [AUTH] Switching user: hr_admin → coordinator")
        rest_client.switch_user(*get_user_credentials("coordinator"))
        assert rest_client.current_user == "coordinator"
        print(f"    [AUTH] ✓ Back to: {rest_client.current_user}")

        # Step 23: Waive milestone (blocked -> waived) - now allowed after approval
        waived = rest_client.call_tool(
            "workday_milestones_update",
            {
                "case_id": ids["case_id"],
                "milestone_type": "work_authorization",
                "new_status": "waived",
                "evidence_link": f"https://exceptions.acme.com/{exception_id}",
                "notes": (
                    f"Waived via exception approval {exception_id} - "
                    "conditional start authorized by HR Admin"
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert waived["status"] == "waived"
        results["waived_milestone"] = waived

        return results

    # =========================================================================
    # PHASE D-2: GATED HCM WRITE-BACK (5 steps)
    # =========================================================================

    def _phase_d2_gated_writeback(
        self, rest_client: RestClient, ids: dict, policy_ids: list[str]
    ) -> dict:
        """Confirm start date - THE KEY GATE.

        Steps:
        24. Read HCM context
        25. Get full case snapshot
        26. CONFIRM START DATE (Core gated operation)
        27. Update readiness
        28. Update case status to resolved
        """
        results = {}

        # Step 24: Read HCM context
        hcm_ctx = rest_client.call_tool(
            "workday_hcm_read_context",
            {
                "case_id": ids["case_id"],
            },
        )
        assert hcm_ctx["confirmed_start_date"] is None  # Not yet confirmed
        results["hcm_context"] = hcm_ctx

        # Step 25: Get full case snapshot
        snapshot = rest_client.call_tool(
            "workday_snapshot_case",
            {
                "case_id": ids["case_id"],
            },
        )
        assert snapshot["case"] is not None
        # Verify all milestones are completed or waived
        for m in snapshot["case"]["case"]["milestones"]:
            assert m["status"] in ("completed", "waived"), (
                f"Milestone {m['milestone_type']} has unexpected status: {m['status']}"
            )
        results["snapshot"] = snapshot

        # Step 26: CONFIRM START DATE (Core gated operation)
        confirmed = rest_client.call_tool(
            "workday_hcm_confirm_start_date",
            {
                "case_id": ids["case_id"],
                "confirmed_start_date": ids["confirmed_start_date"],
                "policy_refs": policy_ids[:2] if len(policy_ids) >= 2 else policy_ids,
                "evidence_links": [
                    "https://vendor.com/reports/MS-001-clear",
                    "https://docs.acme.com/onboarding/MS-001",
                    "https://approvals.acme.com/CASE-2025-0042",
                ],
                "rationale": (
                    "All milestones complete or waived. "
                    "Work authorization waived per approved exception. "
                    "Start date complies with 21-day lead time requirement."
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert confirmed["success"] is True
        assert confirmed["confirmed_start_date"] == ids["confirmed_start_date"]
        for check in confirmed["gating_checks"]:
            assert check["passed"] is True
        results["confirmed"] = confirmed

        # Step 27: Update readiness
        readiness = rest_client.call_tool(
            "workday_hcm_update_readiness",
            {
                "case_id": ids["case_id"],
                "onboarding_readiness": True,
                "policy_refs": policy_ids[:1] if policy_ids else [],
                "evidence_links": [],
                "rationale": (
                    "Candidate ready for Day 1 onboarding activities. "
                    "All prerequisites satisfied or approved."
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert readiness["success"] is True
        results["readiness"] = readiness

        # Step 28: Update case status to resolved
        resolved = rest_client.call_tool(
            "workday_update_case",
            {
                "case_id": ids["case_id"],
                "new_status": "resolved",
                "rationale": (
                    f"Start date confirmed ({ids['confirmed_start_date']}), "
                    "HCM updated, ready for hire execution in V1"
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert resolved["status"] == "resolved"
        results["resolved_case"] = resolved

        return results

    # =========================================================================
    # PHASE E: V1 HIRE EXECUTION (3 steps)
    # =========================================================================

    def _phase_e_hire_execution(self, rest_client: RestClient, ids: dict) -> dict:
        """Execute actual hire in HCM.

        Steps:
        29. Hire worker
        30. Verify worker created
        31. Close case
        """
        results = {}

        # Step 29: Hire worker (fills the position created in Phase 0)
        worker = rest_client.call_tool(
            "workday_hire_worker",
            {
                "worker_id": ids["worker_id"],
                "job_profile_id": ids["job_profile_id"],
                "org_id": ids["child_org_id"],
                "cost_center_id": ids["cost_center_id"],
                "location_id": ids["location_id"],
                "position_id": ids["position_id"],
                "fte": 1.0,
                "hire_date": ids["confirmed_start_date"],
            },
        )
        assert worker["worker_id"] == ids["worker_id"]
        assert worker["employment_status"] == "Active"
        results["worker"] = worker

        # Step 30: Verify worker
        verified = rest_client.call_tool(
            "workday_get_worker",
            {
                "worker_id": ids["worker_id"],
            },
        )
        assert verified["worker_id"] == ids["worker_id"]
        assert verified["job_profile_id"] == ids["job_profile_id"]
        assert verified["org_id"] == ids["child_org_id"]
        assert verified["cost_center_id"] == ids["cost_center_id"]
        assert verified["location_id"] == ids["location_id"]
        results["verified_worker"] = verified

        # Step 31: Close case
        closed = rest_client.call_tool(
            "workday_update_case",
            {
                "case_id": ids["case_id"],
                "new_status": "closed",
                "rationale": (
                    f"Hire executed in HCM. Worker ID: {ids['worker_id']}. Case complete."
                ),
                "actor_persona": "pre_onboarding_coordinator",
            },
        )
        assert closed["status"] == "closed"
        results["closed_case"] = closed

        return results

    # =========================================================================
    # PHASE F: V1 REPORTING (4 steps)
    # =========================================================================

    def _phase_f_reporting(self, rest_client: RestClient, ids: dict) -> dict:
        """Verify data in reports and audit trail.

        Steps:
        32. Headcount report
        33. Movements report
        34. Positions report
        35. Full audit trail verification
        """
        results = {}

        # Step 32: Headcount report
        # Use dynamic date range that includes the hire date
        report_start = date_str(0)  # today
        report_end = date_str(90)  # 90 days from now

        headcount = rest_client.call_tool(
            "workday_report_headcount",
            {
                "start_date": report_start,
                "end_date": report_end,
                "group_by": "org_id",
                "org_id": ids["child_org_id"],
            },
        )
        assert headcount["report"] is not None
        results["headcount"] = headcount

        # Step 33: Movements report
        movements = rest_client.call_tool(
            "workday_report_movements",
            {
                "start_date": ids["confirmed_start_date"],
                "end_date": report_end,
                "event_type": "hire",
                "org_id": ids["child_org_id"],
            },
        )
        # Verify Maria's hire event exists
        hire_events = [m for m in movements["movements"] if m["worker_id"] == ids["worker_id"]]
        assert len(hire_events) >= 1, f"Expected hire event for {ids['worker_id']}"
        results["movements"] = movements

        # Step 34: Positions report
        positions = rest_client.call_tool(
            "workday_report_positions",
            {
                "org_id": ids["child_org_id"],
            },
        )
        results["positions"] = positions

        # Step 35: Full audit trail verification
        audit = rest_client.call_tool(
            "workday_audit_get_history",
            {
                "case_id": ids["case_id"],
            },
        )
        # Should have 10+ audit entries covering all phases
        assert audit["total_count"] >= 10, f"Expected 10+ audit entries, got {audit['total_count']}"

        # Verify key actions in audit trail
        action_types = {e["action_type"] for e in audit["entries"]}
        expected_actions = {
            "case_created",
            "status_updated",
            "milestone_updated",
            "exception_requested",
            "exception_approved",
        }
        missing_actions = expected_actions - action_types
        assert not missing_actions, f"Missing expected audit actions: {missing_actions}"
        results["audit"] = audit

        return results

    # =========================================================================
    # MAIN TEST METHOD
    # =========================================================================

    def test_maria_schmidt_full_workflow(
        self, rest_client: RestClient, workflow_ids: dict, seed_germany_policies: list
    ):
        """Complete 35-step workflow test for Maria Schmidt scenario.

        This is the comprehensive end-to-end integration test covering:
        - Phase 0: Foundation Setup (7 steps)
        - Phase A: Case Initialization (1 step)
        - Phase B: Policy Discovery (6 steps)
        - Phase C: Milestone Tracking (5 steps + negative test)
        - Phase D-1: Exception Handling (4 steps)
        - Phase D-2: Gated HCM Write-Back (5 steps)
        - Phase E: V1 Hire Execution (3 steps)
        - Phase F: V1 Reporting (4 steps)

        Test Data:
        - Candidate: Maria Schmidt
        - Role: Senior Software Engineer
        - Location: Berlin, Germany
        - Situation: Relocating from US, requires Blue Card visa
        - Start dates are dynamically calculated to be in the future
        """
        ids = workflow_ids
        policy_ids = seed_germany_policies

        print("\n" + "=" * 60)
        print("MARIA SCHMIDT PRE-ONBOARDING WORKFLOW TEST")
        print("=" * 60)

        # =====================================================================
        # VERIFY AUTHENTICATION IS ACTIVE
        # =====================================================================
        # This assertion ensures the test is running with auth enabled.
        # If this fails, the REST bridge may not have auth configured.
        print("\n[Auth Check] Verifying authentication is enabled...")
        assert rest_client.is_authenticated, (
            "REST client is not authenticated! Auth must be enabled for this test to be valid."
        )
        assert rest_client.current_user == "coordinator", (
            f"Expected to be logged in as 'coordinator', got '{rest_client.current_user}'"
        )
        print(f"  ✓ Authentication ENABLED - logged in as: {rest_client.current_user}")
        print(f"  ✓ Bearer token present: {rest_client._auth_token[:20]}...")

        print("\n" + "-" * 60)
        print(f"Test Run ID: {ids['case_id'].split('-')[-1]}")
        print(f"Proposed Start Date: {ids['proposed_start_date']}")
        print(f"Confirmed Start Date: {ids['confirmed_start_date']}")
        print("-" * 60)

        # Phase 0: Foundation Setup (7 steps)
        print("\n[Phase 0] Foundation Setup - Creating organizational entities...")
        foundation = self._phase_0_foundation_setup(rest_client, ids)
        print("  ✓ workday_create_org (x3) - Created parent org, EU org, Berlin org")
        print("  ✓ workday_create_job_profile - Created Senior Software Engineer")
        print("  ✓ workday_create_cost_center - Created Engineering Germany")
        print("  ✓ workday_create_location - Created Berlin Office")
        print("  ✓ workday_create_position - Created open position")
        assert "parent_org" in foundation
        assert "eu_org" in foundation
        assert "child_org" in foundation
        assert "job_profile" in foundation
        assert "cost_center" in foundation
        assert "location" in foundation
        assert "position" in foundation

        # Phase A: Case Initialization
        print("\n[Phase A] Case Initialization - Creating pre-onboarding case...")
        case = self._phase_a_case_initialization(rest_client, ids)
        print(f"  ✓ workday_create_case - Created case: {ids['case_id']}")
        print("    → Auto-created 4 milestones (screening, work_auth, documents, approvals)")
        assert case["case_id"] == ids["case_id"]

        # Phase B: Policy Discovery
        print("\n[Phase B] Policy Discovery - Finding applicable German policies...")
        policies = self._phase_b_policy_discovery(rest_client, ids, policy_ids)
        print("  ✓ workday_policies_get_applicable - Found German policies")
        print("  ✓ workday_policies_attach_to_case - Attached policies to case")
        print("  ✓ workday_hcm_read_position - Read position context")
        print("  ✓ workday_tasks_create (x2) - Created visa task and background check task")
        print("  ✓ workday_update_case - Updated status to in_progress")
        assert "policies" in policies
        assert "visa_task" in policies
        assert "bg_task" in policies

        # Phase C: Milestone Tracking (includes negative test for gated write-back)
        print("\n[Phase C] Milestone Tracking - Updating milestone progress...")
        milestones = self._phase_c_milestone_tracking(rest_client, ids, policy_ids)
        print("  ✓ workday_milestones_update - screening → completed")
        print("  ✓ workday_milestones_update - documents → completed")
        print("  ✓ workday_milestones_update - approvals → completed")
        print("  ⚠ workday_milestones_update - work_authorization → BLOCKED (visa delay!)")
        print("  ✓ workday_search_case - Found at-risk German cases")
        print("  ✓ workday_hcm_confirm_start_date - Correctly REJECTED (negative test passed)")
        assert milestones["screening"]["status"] == "completed"
        assert milestones["documents"]["status"] == "completed"
        assert milestones["approvals"]["status"] == "completed"
        assert milestones["work_auth"]["status"] == "blocked"

        # Phase D-1: Exception Handling
        print("\n[Phase D-1] Exception Handling - Requesting approval for blocked milestone...")
        exception_result = self._phase_d1_exception_handling(rest_client, ids, policy_ids)
        print("  ✓ workday_exception_request - Requested exception for work_authorization")
        print("  ✓ workday_milestones_update - REJECTED waive before approval (negative test)")
        print("  ✓ workday_audit_get_history - HR Admin reviewed audit history")
        print("  ✓ workday_exception_approve - REJECTED as coordinator (auth negative test)")
        print("  ✓ switch_user - Switched to hr_admin")
        print("  ✓ workday_exception_approve - HR Admin approved exception")
        print("  ✓ switch_user - Switched back to coordinator")
        print("  ✓ workday_milestones_update - work_authorization → waived")
        assert exception_result["approved_exception"]["approval_status"] == "approved"
        assert exception_result["waived_milestone"]["status"] == "waived"
        assert "auth_negative_test" in exception_result

        # Phase D-2: Gated HCM Write-Back
        print("\n[Phase D-2] Gated HCM Write-Back - Confirming start date...")
        writeback = self._phase_d2_gated_writeback(rest_client, ids, policy_ids)
        print("  ✓ workday_hcm_read_context - Read HCM context")
        print("  ✓ workday_snapshot_case - Got full case snapshot")
        print(f"  ✓ workday_hcm_confirm_start_date - Confirmed: {ids['confirmed_start_date']}")
        print("    → All gating checks passed (milestones complete, lead time satisfied)")
        print("  ✓ workday_hcm_update_readiness - Marked ready for onboarding")
        print("  ✓ workday_update_case - Status → resolved")
        assert writeback["confirmed"]["success"] is True
        assert writeback["readiness"]["success"] is True
        assert writeback["resolved_case"]["status"] == "resolved"

        # Phase E: V1 Hire Execution
        print("\n[Phase E] V1 Hire Execution - Executing hire in HCM...")
        hire = self._phase_e_hire_execution(rest_client, ids)
        print(f"  ✓ workday_hire_worker - Hired: {ids['worker_id']}")
        print("  ✓ workday_get_worker - Verified worker status: Active")
        print("  ✓ workday_update_case - Status → closed")
        assert hire["worker"]["worker_id"] == ids["worker_id"]
        assert hire["worker"]["employment_status"] == "Active"
        assert hire["closed_case"]["status"] == "closed"

        # Phase F: V1 Reporting
        print("\n[Phase F] V1 Reporting - Verifying data in reports...")
        reports = self._phase_f_reporting(rest_client, ids)
        print("  ✓ workday_report_headcount - Generated headcount report")
        print("  ✓ workday_report_movements - Found hire event")
        print("  ✓ workday_report_positions - Generated positions report")
        audit_count = reports["audit"]["total_count"]
        print(f"  ✓ workday_audit_get_history - Verified {audit_count} audit entries")
        assert reports["audit"]["total_count"] >= 10

        # =================================================================
        # FINAL ASSERTIONS
        # =================================================================
        print("\n[Final] Verifying workflow completion...")

        # Verify case is closed
        final_case = rest_client.call_tool(
            "workday_get_case",
            {"case_id": ids["case_id"]},
        )
        assert final_case["case"]["status"] == "closed"

        # Verify worker exists and is active
        final_worker = rest_client.call_tool(
            "workday_get_worker",
            {"worker_id": ids["worker_id"]},
        )
        assert final_worker["employment_status"] == "Active"

        # Verify all milestones are complete or waived
        for m in final_case["case"]["milestones"]:
            assert m["status"] in ("completed", "waived"), (
                f"Milestone {m['milestone_type']} not finalized"
            )

        print("  ✓ workday_get_case - Case is closed")
        print("  ✓ workday_get_worker - Worker is active")
        print("  ✓ All milestones finalized (completed or waived)")
        print("\n" + "=" * 60)
        print("✅ WORKFLOW TEST PASSED - All 35 steps completed successfully!")
        print("=" * 60 + "\n")
