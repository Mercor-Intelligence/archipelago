"""Workday Field Check Verifier - Flexible dropdown + value interface.

This verifier provides a simple dropdown interface for domain experts to check
any Workday HCM field without writing SQL. The dropdown selection maps to
pre-defined SQL queries that have been verified against the actual Workday schema.

Supports verification of:
- Workers (employees, contractors)
- Positions (open, filled, closed)
- Supervisory Organizations
- Job Profiles
- Cost Centers
- Locations
- Movements (hire, transfer, termination events)
- Pre-onboarding Cases
- Milestones
- Tasks
"""

import os
import sqlite3
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# All queries are schema-accurate for Workday HCM
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # WORKER CHECKS
    # =========================================================================
    "Worker Count": {
        "query": "SELECT COUNT(*) FROM workers",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of workers",
    },
    "Worker Count by Status": {
        "query": "SELECT COUNT(*) FROM workers WHERE employment_status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count workers by employment status (Active, Terminated, Leave)",
    },
    "Active Worker Count": {
        "query": "SELECT COUNT(*) FROM workers WHERE employment_status = 'Active'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active workers",
    },
    "Terminated Worker Count": {
        "query": "SELECT COUNT(*) FROM workers WHERE employment_status = 'Terminated'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count terminated workers",
    },
    "Worker Exists by ID": {
        "query": "SELECT COUNT(*) FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if worker exists by worker_id",
    },
    "Worker Employment Status": {
        "query": "SELECT employment_status FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's employment status (Active, Terminated, Leave)",
    },
    "Worker Org ID": {
        "query": "SELECT org_id FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's supervisory org ID",
    },
    "Worker Job Profile ID": {
        "query": "SELECT job_profile_id FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's job profile ID",
    },
    "Worker Cost Center ID": {
        "query": "SELECT cost_center_id FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's cost center ID",
    },
    "Worker Location ID": {
        "query": "SELECT location_id FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's location ID",
    },
    "Worker Position ID": {
        "query": "SELECT position_id FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's position ID",
    },
    "Worker FTE": {
        "query": "SELECT fte FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "decimal",
        "description": "Get worker's FTE (0.0 to 1.0)",
    },
    "Worker Hire Date": {
        "query": "SELECT hire_date FROM workers WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get worker's hire date (YYYY-MM-DD)",
    },
    "Workers in Org": {
        "query": "SELECT COUNT(*) FROM workers WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "integer",
        "description": "Count workers in a specific organization",
    },
    "Active Workers in Org": {
        "query": "SELECT COUNT(*) FROM workers WHERE org_id = :org_id AND employment_status = 'Active'",
        "required_params": ["org_id"],
        "result_type": "integer",
        "description": "Count active workers in a specific organization",
    },
    "Workers with Job Profile": {
        "query": "SELECT COUNT(*) FROM workers WHERE job_profile_id = :job_profile_id",
        "required_params": ["job_profile_id"],
        "result_type": "integer",
        "description": "Count workers with a specific job profile",
    },
    "Workers in Cost Center": {
        "query": "SELECT COUNT(*) FROM workers WHERE cost_center_id = :cost_center_id",
        "required_params": ["cost_center_id"],
        "result_type": "integer",
        "description": "Count workers in a specific cost center",
    },
    "Workers at Location": {
        "query": "SELECT COUNT(*) FROM workers WHERE location_id = :location_id",
        "required_params": ["location_id"],
        "result_type": "integer",
        "description": "Count workers at a specific location",
    },
    # =========================================================================
    # POSITION CHECKS
    # =========================================================================
    "Position Count": {
        "query": "SELECT COUNT(*) FROM positions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of positions",
    },
    "Position Count by Status": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count positions by status (open, filled, closed)",
    },
    "Open Position Count": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = 'open'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count open positions",
    },
    "Filled Position Count": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = 'filled'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count filled positions",
    },
    "Closed Position Count": {
        "query": "SELECT COUNT(*) FROM positions WHERE status = 'closed'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count closed positions",
    },
    "Position Exists by ID": {
        "query": "SELECT COUNT(*) FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "boolean",
        "description": "Check if position exists by position_id",
    },
    "Position Status": {
        "query": "SELECT status FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position status (open, filled, closed)",
    },
    "Position Worker ID": {
        "query": "SELECT worker_id FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get worker assigned to position",
    },
    "Position Job Profile ID": {
        "query": "SELECT job_profile_id FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position's job profile ID",
    },
    "Position Org ID": {
        "query": "SELECT org_id FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "string",
        "description": "Get position's org ID",
    },
    "Position FTE": {
        "query": "SELECT fte FROM positions WHERE position_id = :position_id",
        "required_params": ["position_id"],
        "result_type": "decimal",
        "description": "Get position FTE (0.0 to 1.0)",
    },
    "Positions in Org": {
        "query": "SELECT COUNT(*) FROM positions WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "integer",
        "description": "Count positions in a specific organization",
    },
    "Open Positions in Org": {
        "query": "SELECT COUNT(*) FROM positions WHERE org_id = :org_id AND status = 'open'",
        "required_params": ["org_id"],
        "result_type": "integer",
        "description": "Count open positions in a specific organization",
    },
    # =========================================================================
    # SUPERVISORY ORG CHECKS
    # =========================================================================
    "Org Count": {
        "query": "SELECT COUNT(*) FROM supervisory_orgs",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of organizations",
    },
    "Org Count by Type": {
        "query": "SELECT COUNT(*) FROM supervisory_orgs WHERE org_type = :org_type",
        "required_params": ["org_type"],
        "result_type": "integer",
        "description": "Count orgs by type (Supervisory, Cost_Center, Location)",
    },
    "Org Exists by ID": {
        "query": "SELECT COUNT(*) FROM supervisory_orgs WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "boolean",
        "description": "Check if organization exists",
    },
    "Org Exists by Name": {
        "query": "SELECT COUNT(*) FROM supervisory_orgs WHERE LOWER(org_name) LIKE LOWER('%' || :org_name || '%')",
        "required_params": ["org_name"],
        "result_type": "boolean",
        "description": "Check if organization exists by name (fuzzy match)",
    },
    "Org Name": {
        "query": "SELECT org_name FROM supervisory_orgs WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "string",
        "description": "Get organization name by ID",
    },
    "Org Type": {
        "query": "SELECT org_type FROM supervisory_orgs WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "string",
        "description": "Get organization type (Supervisory, Cost_Center, Location)",
    },
    "Org Parent ID": {
        "query": "SELECT parent_org_id FROM supervisory_orgs WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "string",
        "description": "Get parent organization ID",
    },
    "Org Manager Worker ID": {
        "query": "SELECT manager_worker_id FROM supervisory_orgs WHERE org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "string",
        "description": "Get organization manager's worker ID",
    },
    "Child Org Count": {
        "query": "SELECT COUNT(*) FROM supervisory_orgs WHERE parent_org_id = :org_id",
        "required_params": ["org_id"],
        "result_type": "integer",
        "description": "Count child organizations",
    },
    # =========================================================================
    # JOB PROFILE CHECKS
    # =========================================================================
    "Job Profile Count": {
        "query": "SELECT COUNT(*) FROM job_profiles",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of job profiles",
    },
    "Job Profile Exists by ID": {
        "query": "SELECT COUNT(*) FROM job_profiles WHERE job_profile_id = :job_profile_id",
        "required_params": ["job_profile_id"],
        "result_type": "boolean",
        "description": "Check if job profile exists",
    },
    "Job Profile Exists by Title": {
        "query": "SELECT COUNT(*) FROM job_profiles WHERE LOWER(title) LIKE LOWER('%' || :title || '%')",
        "required_params": ["title"],
        "result_type": "boolean",
        "description": "Check if job profile exists by title (fuzzy match)",
    },
    "Job Profile Title": {
        "query": "SELECT title FROM job_profiles WHERE job_profile_id = :job_profile_id",
        "required_params": ["job_profile_id"],
        "result_type": "string",
        "description": "Get job profile title",
    },
    "Job Profile Family": {
        "query": "SELECT job_family FROM job_profiles WHERE job_profile_id = :job_profile_id",
        "required_params": ["job_profile_id"],
        "result_type": "string",
        "description": "Get job profile family",
    },
    "Job Profile Level": {
        "query": "SELECT job_level FROM job_profiles WHERE job_profile_id = :job_profile_id",
        "required_params": ["job_profile_id"],
        "result_type": "string",
        "description": "Get job profile level",
    },
    "Job Profiles in Family": {
        "query": "SELECT COUNT(*) FROM job_profiles WHERE job_family = :job_family",
        "required_params": ["job_family"],
        "result_type": "integer",
        "description": "Count job profiles in a family",
    },
    # =========================================================================
    # COST CENTER CHECKS
    # =========================================================================
    "Cost Center Count": {
        "query": "SELECT COUNT(*) FROM cost_centers",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of cost centers",
    },
    "Cost Center Exists by ID": {
        "query": "SELECT COUNT(*) FROM cost_centers WHERE cost_center_id = :cost_center_id",
        "required_params": ["cost_center_id"],
        "result_type": "boolean",
        "description": "Check if cost center exists",
    },
    "Cost Center Exists by Name": {
        "query": "SELECT COUNT(*) FROM cost_centers WHERE LOWER(cost_center_name) LIKE LOWER('%' || :cost_center_name || '%')",
        "required_params": ["cost_center_name"],
        "result_type": "boolean",
        "description": "Check if cost center exists by name (fuzzy match)",
    },
    "Cost Center Name": {
        "query": "SELECT cost_center_name FROM cost_centers WHERE cost_center_id = :cost_center_id",
        "required_params": ["cost_center_id"],
        "result_type": "string",
        "description": "Get cost center name",
    },
    "Cost Center Org ID": {
        "query": "SELECT org_id FROM cost_centers WHERE cost_center_id = :cost_center_id",
        "required_params": ["cost_center_id"],
        "result_type": "string",
        "description": "Get cost center's org ID",
    },
    # =========================================================================
    # LOCATION CHECKS
    # =========================================================================
    "Location Count": {
        "query": "SELECT COUNT(*) FROM locations",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of locations",
    },
    "Location Exists by ID": {
        "query": "SELECT COUNT(*) FROM locations WHERE location_id = :location_id",
        "required_params": ["location_id"],
        "result_type": "boolean",
        "description": "Check if location exists",
    },
    "Location Exists by Name": {
        "query": "SELECT COUNT(*) FROM locations WHERE LOWER(location_name) LIKE LOWER('%' || :location_name || '%')",
        "required_params": ["location_name"],
        "result_type": "boolean",
        "description": "Check if location exists by name (fuzzy match)",
    },
    "Location Name": {
        "query": "SELECT location_name FROM locations WHERE location_id = :location_id",
        "required_params": ["location_id"],
        "result_type": "string",
        "description": "Get location name",
    },
    "Location City": {
        "query": "SELECT city FROM locations WHERE location_id = :location_id",
        "required_params": ["location_id"],
        "result_type": "string",
        "description": "Get location city",
    },
    "Location Country": {
        "query": "SELECT country FROM locations WHERE location_id = :location_id",
        "required_params": ["location_id"],
        "result_type": "string",
        "description": "Get location country",
    },
    "Locations in Country": {
        "query": "SELECT COUNT(*) FROM locations WHERE LOWER(country) = LOWER(:country)",
        "required_params": ["country"],
        "result_type": "integer",
        "description": "Count locations in a country",
    },
    "Locations in City": {
        "query": "SELECT COUNT(*) FROM locations WHERE LOWER(city) = LOWER(:city)",
        "required_params": ["city"],
        "result_type": "integer",
        "description": "Count locations in a city",
    },
    # =========================================================================
    # MOVEMENT CHECKS (Hire, Transfer, Termination Events)
    # =========================================================================
    "Movement Count": {
        "query": "SELECT COUNT(*) FROM movements",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of movement events",
    },
    "Movement Count by Type": {
        "query": "SELECT COUNT(*) FROM movements WHERE event_type = :event_type",
        "required_params": ["event_type"],
        "result_type": "integer",
        "description": "Count movements by type (hire, termination, transfer)",
    },
    "Hire Count": {
        "query": "SELECT COUNT(*) FROM movements WHERE event_type = 'hire'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count hire events",
    },
    "Termination Count": {
        "query": "SELECT COUNT(*) FROM movements WHERE event_type = 'termination'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count termination events",
    },
    "Transfer Count": {
        "query": "SELECT COUNT(*) FROM movements WHERE event_type = 'transfer'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count transfer events",
    },
    "Worker Movement Count": {
        "query": "SELECT COUNT(*) FROM movements WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "integer",
        "description": "Count movements for a specific worker",
    },
    "Worker Has Hire Event": {
        "query": "SELECT COUNT(*) FROM movements WHERE worker_id = :worker_id AND event_type = 'hire'",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if worker has a hire event",
    },
    "Worker Has Termination Event": {
        "query": "SELECT COUNT(*) FROM movements WHERE worker_id = :worker_id AND event_type = 'termination'",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if worker has a termination event",
    },
    "Worker Has Transfer Event": {
        "query": "SELECT COUNT(*) FROM movements WHERE worker_id = :worker_id AND event_type = 'transfer'",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if worker has a transfer event",
    },
    # =========================================================================
    # CASE CHECKS (Pre-onboarding)
    # =========================================================================
    "Case Count": {
        "query": "SELECT COUNT(*) FROM cases",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of pre-onboarding cases",
    },
    "Case Count by Status": {
        "query": "SELECT COUNT(*) FROM cases WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count cases by status (open, in_progress, pending_approval, resolved, closed)",
    },
    "Open Case Count": {
        "query": "SELECT COUNT(*) FROM cases WHERE status = 'open'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count open cases",
    },
    "Case Exists by ID": {
        "query": "SELECT COUNT(*) FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "boolean",
        "description": "Check if case exists",
    },
    "Case Status": {
        "query": "SELECT status FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case status",
    },
    "Case Candidate ID": {
        "query": "SELECT candidate_id FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case candidate ID",
    },
    "Case Role": {
        "query": "SELECT role FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case role",
    },
    "Case Country": {
        "query": "SELECT country FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case country",
    },
    "Case Employment Type": {
        "query": "SELECT employment_type FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case employment type (full_time, part_time, contractor)",
    },
    "Case Owner Persona": {
        "query": "SELECT owner_persona FROM cases WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "string",
        "description": "Get case owner persona",
    },
    "Cases by Country": {
        "query": "SELECT COUNT(*) FROM cases WHERE LOWER(country) = LOWER(:country)",
        "required_params": ["country"],
        "result_type": "integer",
        "description": "Count cases by country",
    },
    "Cases by Owner": {
        "query": "SELECT COUNT(*) FROM cases WHERE owner_persona = :owner_persona",
        "required_params": ["owner_persona"],
        "result_type": "integer",
        "description": "Count cases by owner persona",
    },
    # =========================================================================
    # MILESTONE CHECKS
    # =========================================================================
    "Milestone Count": {
        "query": "SELECT COUNT(*) FROM milestones",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total milestones",
    },
    "Milestone Count for Case": {
        "query": "SELECT COUNT(*) FROM milestones WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count milestones for a case",
    },
    "Milestone Count by Status": {
        "query": "SELECT COUNT(*) FROM milestones WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count milestones by status (pending, in_progress, completed, waived, blocked)",
    },
    "Completed Milestones for Case": {
        "query": "SELECT COUNT(*) FROM milestones WHERE case_id = :case_id AND status = 'completed'",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count completed milestones for a case",
    },
    "Milestone Exists": {
        "query": "SELECT COUNT(*) FROM milestones WHERE case_id = :case_id AND milestone_type = :milestone_type",
        "required_params": ["case_id", "milestone_type"],
        "result_type": "boolean",
        "description": "Check if milestone exists for case (screening, work_authorization, documents, approvals)",
    },
    "Milestone Status": {
        "query": "SELECT status FROM milestones WHERE case_id = :case_id AND milestone_type = :milestone_type",
        "required_params": ["case_id", "milestone_type"],
        "result_type": "string",
        "description": "Get milestone status",
    },
    # =========================================================================
    # TASK CHECKS
    # =========================================================================
    "Task Count": {
        "query": "SELECT COUNT(*) FROM tasks",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total tasks",
    },
    "Task Count for Case": {
        "query": "SELECT COUNT(*) FROM tasks WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count tasks for a case",
    },
    "Task Count by Status": {
        "query": "SELECT COUNT(*) FROM tasks WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count tasks by status (pending, in_progress, completed, cancelled)",
    },
    "Completed Tasks for Case": {
        "query": "SELECT COUNT(*) FROM tasks WHERE case_id = :case_id AND status = 'completed'",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count completed tasks for a case",
    },
    "Pending Tasks for Case": {
        "query": "SELECT COUNT(*) FROM tasks WHERE case_id = :case_id AND status = 'pending'",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count pending tasks for a case",
    },
    "Task Exists by ID": {
        "query": "SELECT COUNT(*) FROM tasks WHERE task_id = :task_id",
        "required_params": ["task_id"],
        "result_type": "boolean",
        "description": "Check if task exists",
    },
    "Task Status": {
        "query": "SELECT status FROM tasks WHERE task_id = :task_id",
        "required_params": ["task_id"],
        "result_type": "string",
        "description": "Get task status",
    },
    "Task Title": {
        "query": "SELECT title FROM tasks WHERE task_id = :task_id",
        "required_params": ["task_id"],
        "result_type": "string",
        "description": "Get task title",
    },
    # =========================================================================
    # AUDIT CHECKS
    # =========================================================================
    "Audit Entry Count": {
        "query": "SELECT COUNT(*) FROM audit_entries",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total audit entries",
    },
    "Audit Entry Count for Case": {
        "query": "SELECT COUNT(*) FROM audit_entries WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count audit entries for a case",
    },
    # =========================================================================
    # EXCEPTION CHECKS
    # =========================================================================
    "Exception Count": {
        "query": "SELECT COUNT(*) FROM exceptions",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total exception requests",
    },
    "Exception Count for Case": {
        "query": "SELECT COUNT(*) FROM exceptions WHERE case_id = :case_id",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count exceptions for a case",
    },
    "Exception Count by Status": {
        "query": "SELECT COUNT(*) FROM exceptions WHERE approval_status = :approval_status",
        "required_params": ["approval_status"],
        "result_type": "integer",
        "description": "Count exceptions by approval status (pending, approved, denied)",
    },
    "Approved Exceptions for Case": {
        "query": "SELECT COUNT(*) FROM exceptions WHERE case_id = :case_id AND approval_status = 'approved'",
        "required_params": ["case_id"],
        "result_type": "integer",
        "description": "Count approved exceptions for a case",
    },
    # =========================================================================
    # POLICY CHECKS
    # =========================================================================
    "Policy Count": {
        "query": "SELECT COUNT(*) FROM policy_references",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total policy references",
    },
    "Policy Count by Country": {
        "query": "SELECT COUNT(*) FROM policy_references WHERE LOWER(country) = LOWER(:country)",
        "required_params": ["country"],
        "result_type": "integer",
        "description": "Count policies by country",
    },
    "Policy Count by Type": {
        "query": "SELECT COUNT(*) FROM policy_references WHERE policy_type = :policy_type",
        "required_params": ["policy_type"],
        "result_type": "integer",
        "description": "Count policies by type (prerequisites, lead_times, payroll_cutoffs, constraints)",
    },
    "Policy Exists by ID": {
        "query": "SELECT COUNT(*) FROM policy_references WHERE policy_id = :policy_id",
        "required_params": ["policy_id"],
        "result_type": "boolean",
        "description": "Check if policy exists",
    },
    # =========================================================================
    # PAYROLL CUTOFF CHECKS
    # =========================================================================
    "Payroll Cutoff Count": {
        "query": "SELECT COUNT(*) FROM payroll_cutoffs",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total payroll cutoffs",
    },
    "Payroll Cutoff Count by Country": {
        "query": "SELECT COUNT(*) FROM payroll_cutoffs WHERE LOWER(country) = LOWER(:country)",
        "required_params": ["country"],
        "result_type": "integer",
        "description": "Count payroll cutoffs by country",
    },
    "Payroll Cutoff Day": {
        "query": "SELECT cutoff_day_of_month FROM payroll_cutoffs WHERE cutoff_id = :cutoff_id",
        "required_params": ["cutoff_id"],
        "result_type": "integer",
        "description": "Get payroll cutoff day of month",
    },
    # =========================================================================
    # HCM STATE CHECKS
    # =========================================================================
    "HCM State Exists for Worker": {
        "query": "SELECT COUNT(*) FROM hcm_worker_state WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if HCM worker state exists",
    },
    "HCM Onboarding Status": {
        "query": "SELECT onboarding_status FROM hcm_worker_state WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "string",
        "description": "Get HCM onboarding status (not_started, in_progress, ready, finalized)",
    },
    "HCM Onboarding Readiness": {
        "query": "SELECT onboarding_readiness FROM hcm_worker_state WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "integer",
        "description": "Get HCM onboarding readiness percentage",
    },
    "HCM Hire Finalized": {
        "query": "SELECT hire_finalized FROM hcm_worker_state WHERE worker_id = :worker_id",
        "required_params": ["worker_id"],
        "result_type": "boolean",
        "description": "Check if hire is finalized",
    },
}


# Database path options to try in the snapshot
DB_PATHS_TO_TRY = [
    "mcp_servers/workday/data.db",
    "var/lib/workday/data.db",
    "workday/data.db",
    ".apps_data/workday/data.db",
]


def _read_file_from_snapshot_zip(
    zf: zipfile.ZipFile, path: str, base_dir: str = "filesystem"
) -> bytes | None:
    """Read a file from a snapshot zip, trying with and without base_dir prefix."""
    paths_to_try = []
    if base_dir:
        paths_to_try.append(f"{base_dir}/{path}")
    paths_to_try.append(path)

    for try_path in paths_to_try:
        try:
            return zf.read(try_path)
        except KeyError:
            continue
    return None


async def workday_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any Workday HCM field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Get field selection and strip arrow notation if present
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        raise ValueError("field_to_check is required")

    # Strip arrow notation (e.g., "Worker Count by Status → Status" -> "Worker Count by Status")
    field_to_check = field_to_check_raw.split(" → ")[0].strip()

    # Get expected value
    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        raise ValueError("expected_value is required")

    # Get field configuration
    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        raise ValueError(f"Unknown field_to_check: {field_to_check}")

    # Extract required parameters from verifier_values
    params: dict[str, Any] = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for '{field_to_check}'"
            )
        params[param] = value

    # Get database connection from helper_results first (preferred method)
    # This filters by "workday" to avoid picking up other services' databases
    conn: sqlite3.Connection | None = None
    db_path_found: str | None = None

    if input.helper_results:
        dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
        if dbs:
            # Filter to Workday databases only (avoid picking up QuickBooks, Greenhouse, etc.)
            # Exclude workday_help which is a separate service
            workday_dbs = [
                (alias, db_info)
                for alias, db_info in dbs.items()
                if "workday" in alias.lower() and "workday_help" not in alias.lower()
            ]
            if len(workday_dbs) == 1:
                alias, db_info = workday_dbs[0]
                conn = db_info.get("connection")
                if conn:
                    db_path_found = alias
                    logger.info(
                        f"Found Workday database via SNAPSHOT_DBS helper: {alias}"
                    )
            elif len(workday_dbs) > 1:
                raise ValueError(
                    f"Expected exactly 1 Workday database, found {len(workday_dbs)}. Available: {list(dbs.keys())}"
                )

    # Fallback: Extract database directly from snapshot (for backwards compatibility)
    db_bytes: bytes | None = None
    if not conn:
        try:
            # Reset stream position in case it was read by another component
            input.final_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.final_snapshot_bytes, "r") as zf:
                # Try multiple possible paths
                for db_path in DB_PATHS_TO_TRY:
                    db_bytes = _read_file_from_snapshot_zip(zf, db_path)
                    if db_bytes:
                        db_path_found = db_path
                        logger.info(f"Found Workday database at: {db_path}")
                        break

                    # Also try without filesystem/ prefix (base_dir="")
                    db_bytes = _read_file_from_snapshot_zip(zf, db_path, base_dir="")
                    if db_bytes:
                        db_path_found = db_path
                        logger.info(
                            f"Found Workday database at: {db_path} (no base_dir)"
                        )
                        break
        except Exception as e:
            raise ValueError(f"Failed to read snapshot: {e!s}") from e

    if not conn and not db_bytes:
        raise ValueError(
            f"Workday database not found in snapshot. Tried paths: {DB_PATHS_TO_TRY}"
        )

    # If we didn't get a connection from the helper, create one from db_bytes
    tmp_path: str | None = None
    tmp_file = None
    owns_connection = False  # Track if we created the connection (need to close it)

    try:
        if not conn and db_bytes:
            # Write to temp file and create connection
            tmp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp_path = tmp_file.name
            tmp_file.write(db_bytes)
            tmp_file.flush()
            tmp_file.close()
            tmp_file = None  # Mark as closed

            conn = sqlite3.connect(tmp_path)
            owns_connection = True

        if not conn:
            raise ValueError("Failed to establish database connection")

        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"Workday field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        raise ValueError(f"Query execution failed: {e!s}") from e
    finally:
        # Close file handle if still open
        if tmp_file is not None:
            try:
                tmp_file.close()
            except Exception:
                pass
        # Only close connection if we own it (created it from db_bytes)
        if owns_connection and conn:
            conn.close()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed to delete temp db file")

    # Compare based on result_type
    result_type = field_config["result_type"]
    truthy_values = ("true", "1", "yes", "t", "y")

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation
        try:
            tolerance_value = input.verifier.verifier_values.get("tolerance", 0.01)
            if tolerance_value is None:
                tolerance_value = 0.01
            tolerance = Decimal(str(tolerance_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            raise ValueError(f"Invalid tolerance value: {e!s}") from e

        # Ensure tolerance is non-negative
        if tolerance < 0:
            raise ValueError(f"Tolerance must be non-negative, got: {tolerance}")

        try:
            result_decimal = Decimal(str(result))
            expected_decimal = Decimal(str(expected_value))
        except (ValueError, TypeError, InvalidOperation) as e:
            raise ValueError(
                f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        matches = abs(result_decimal - expected_decimal) <= tolerance
        message = f"{field_to_check}: {float(result_decimal):.2f} {'✓' if matches else f'✗ expected {float(expected_decimal):.2f}'}"
    elif result_type == "boolean":
        # Robust boolean conversion
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in truthy_values
            else:
                actual_bool = float(result) > 0
        except (TypeError, ValueError):
            actual_bool = bool(result)

        expected_bool = str(expected_value).lower() in truthy_values
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            matches = int(result) == int(expected_value)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
    elif result_type == "string":
        # Case-insensitive string comparison, consistent None handling
        result_str = str(result).lower().strip() if result is not None else ""
        expected_str = (
            str(expected_value).lower().strip() if expected_value is not None else ""
        )
        matches = result_str == expected_str
        message = f"{field_to_check}: '{result}' {'✓' if matches else f'✗ expected {expected_value}'}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
            "db_path": db_path_found,
        },
        message=message,
    )
