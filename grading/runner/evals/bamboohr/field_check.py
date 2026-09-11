"""BambooHR Field Check Verifier - Flexible dropdown + value interface for BambooHR.

This verifier provides a simple dropdown interface for domain experts to check
any BambooHR field without writing SQL. The dropdown selection maps to
pre-defined SQL queries verified against the actual BambooHR SQLAlchemy schema.

Schema Reference (from mercor-hr-apps/mcp_servers/bamboohr/db/models.py):
- employees: id, employee_number, first_name, last_name, status, department, job_title,
             location, hire_date, supervisor_id, work_email, salary, etc.
- time_off_requests: id, employee_id, type_id, policy_id, start_date, end_date, amount,
                     status, notes, approver_id
- time_off_balances: id, employee_id, policy_id, year, balance, used, scheduled
- time_off_policies: id, name, type_id, accrual_type, accrual_rate, max_balance, carry_over
- time_off_types: id, name, color, paid, units
- list_field_options: id, field_name, option_value, sort_order, archived
- employee_policies: id, employee_id, policy_id, effective_date, end_date
- departments: id, name, parent_id, archived
- audit_log: id, action, entity_type, entity_id, actor_id, timestamp
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus

# Field mappings for BambooHR database queries
# All queries verified against actual SQLAlchemy schema
FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    # =========================================================================
    # Employee Checks
    # =========================================================================
    "Employee Count": {
        "query": "SELECT COUNT(*) FROM employees",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total number of employees",
    },
    "Employee Count Active": {
        "query": "SELECT COUNT(*) FROM employees WHERE status = 'Active'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active employees",
    },
    "Employee Count Inactive": {
        "query": "SELECT COUNT(*) FROM employees WHERE status = 'Inactive'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count inactive employees",
    },
    "Employee Count Terminated": {
        "query": "SELECT COUNT(*) FROM employees WHERE status = 'Terminated'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count terminated employees",
    },
    "Employee Count by Status": {
        "query": "SELECT COUNT(*) FROM employees WHERE status = :status",
        "required_params": ["status"],
        "result_type": "integer",
        "description": "Count employees by status (Active, Inactive, Terminated)",
    },
    "Employee Count by Department": {
        "query": "SELECT COUNT(*) FROM employees WHERE LOWER(department) = LOWER(:department)",
        "required_params": ["department"],
        "result_type": "integer",
        "description": "Count employees in a specific department",
    },
    "Employee Count by Job Title": {
        "query": "SELECT COUNT(*) FROM employees WHERE LOWER(job_title) = LOWER(:job_title)",
        "required_params": ["job_title"],
        "result_type": "integer",
        "description": "Count employees with a specific job title",
    },
    "Employee Count by Location": {
        "query": "SELECT COUNT(*) FROM employees WHERE LOWER(location) = LOWER(:location)",
        "required_params": ["location"],
        "result_type": "integer",
        "description": "Count employees at a specific location",
    },
    "Employee Exists by ID": {
        "query": "SELECT COUNT(*) FROM employees WHERE id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "boolean",
        "description": "Check if an employee exists by ID",
    },
    "Employee Exists by Name": {
        "query": "SELECT COUNT(*) FROM employees WHERE LOWER(first_name) = LOWER(:first_name) AND LOWER(last_name) = LOWER(:last_name)",
        "required_params": ["first_name", "last_name"],
        "result_type": "boolean",
        "description": "Check if an employee exists by first and last name",
    },
    "Employee Exists by Email": {
        "query": "SELECT COUNT(*) FROM employees WHERE LOWER(work_email) = LOWER(:work_email)",
        "required_params": ["work_email"],
        "result_type": "boolean",
        "description": "Check if an employee exists by work email",
    },
    "Employee Exists by Employee Number": {
        "query": "SELECT COUNT(*) FROM employees WHERE employee_number = :employee_number",
        "required_params": ["employee_number"],
        "result_type": "boolean",
        "description": "Check if an employee exists by employee number",
    },
    "Employee Status": {
        "query": "SELECT status FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "string",
        "description": "Get status of an employee (Active, Inactive, Terminated)",
    },
    "Employee Status by Name": {
        "query": "SELECT status FROM employees WHERE LOWER(first_name) = LOWER(:first_name) AND LOWER(last_name) = LOWER(:last_name) LIMIT 1",
        "required_params": ["first_name", "last_name"],
        "result_type": "string",
        "description": "Get status of an employee by name",
    },
    "Employee Department": {
        "query": "SELECT department FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "string",
        "description": "Get department of an employee",
    },
    "Employee Department by Name": {
        "query": "SELECT department FROM employees WHERE LOWER(first_name) = LOWER(:first_name) AND LOWER(last_name) = LOWER(:last_name) LIMIT 1",
        "required_params": ["first_name", "last_name"],
        "result_type": "string",
        "description": "Get department of an employee by name",
    },
    "Employee Job Title": {
        "query": "SELECT job_title FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "string",
        "description": "Get job title of an employee",
    },
    "Employee Job Title by Name": {
        "query": "SELECT job_title FROM employees WHERE LOWER(first_name) = LOWER(:first_name) AND LOWER(last_name) = LOWER(:last_name) LIMIT 1",
        "required_params": ["first_name", "last_name"],
        "result_type": "string",
        "description": "Get job title of an employee by name",
    },
    "Employee Location": {
        "query": "SELECT location FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "string",
        "description": "Get location of an employee",
    },
    "Employee Email": {
        "query": "SELECT work_email FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "string",
        "description": "Get work email of an employee",
    },
    "Employee Email by Name": {
        "query": "SELECT work_email FROM employees WHERE LOWER(first_name) = LOWER(:first_name) AND LOWER(last_name) = LOWER(:last_name) LIMIT 1",
        "required_params": ["first_name", "last_name"],
        "result_type": "string",
        "description": "Get work email of an employee by name",
    },
    "Employee Salary": {
        "query": "SELECT salary FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "decimal",
        "description": "Get salary of an employee (HR Admin only)",
    },
    "Employee Has Supervisor": {
        "query": "SELECT COUNT(*) FROM employees WHERE id = :employee_id AND supervisor_id IS NOT NULL",
        "required_params": ["employee_id"],
        "result_type": "boolean",
        "description": "Check if an employee has a supervisor assigned",
    },
    "Employee Supervisor ID": {
        "query": "SELECT supervisor_id FROM employees WHERE id = :employee_id LIMIT 1",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Get supervisor ID of an employee",
    },
    "Employee Direct Reports Count": {
        "query": "SELECT COUNT(*) FROM employees WHERE supervisor_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Count direct reports of an employee",
    },
    # =========================================================================
    # Time-Off Request Checks
    # =========================================================================
    "Time Off Request Count": {
        "query": "SELECT COUNT(*) FROM time_off_requests",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total time-off requests",
    },
    "Time Off Request Count by Status": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE status = :request_status",
        "required_params": ["request_status"],
        "result_type": "integer",
        "description": "Count time-off requests by status (requested, approved, denied, canceled)",
    },
    "Time Off Request Count by Employee": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Count time-off requests for an employee",
    },
    "Time Off Request Count by Employee and Status": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE employee_id = :employee_id AND status = :request_status",
        "required_params": ["employee_id", "request_status"],
        "result_type": "integer",
        "description": "Count time-off requests for an employee by status",
    },
    "Time Off Request Count Approved": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE status = 'approved'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count approved time-off requests",
    },
    "Time Off Request Count Pending": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE status = 'requested'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count pending (requested) time-off requests",
    },
    "Time Off Request Count Denied": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE status = 'denied'",
        "required_params": [],
        "result_type": "integer",
        "description": "Count denied time-off requests",
    },
    "Time Off Request Exists": {
        "query": "SELECT COUNT(*) FROM time_off_requests WHERE id = :request_id",
        "required_params": ["request_id"],
        "result_type": "boolean",
        "description": "Check if a time-off request exists",
    },
    "Time Off Request Status": {
        "query": "SELECT status FROM time_off_requests WHERE id = :request_id LIMIT 1",
        "required_params": ["request_id"],
        "result_type": "string",
        "description": "Get status of a time-off request",
    },
    "Time Off Request Amount": {
        "query": "SELECT amount FROM time_off_requests WHERE id = :request_id LIMIT 1",
        "required_params": ["request_id"],
        "result_type": "decimal",
        "description": "Get amount (days/hours) of a time-off request",
    },
    "Time Off Request Total Days by Employee": {
        "query": "SELECT COALESCE(SUM(amount), 0) FROM time_off_requests WHERE employee_id = :employee_id AND status = 'approved'",
        "required_params": ["employee_id"],
        "result_type": "decimal",
        "description": "Sum of approved time-off days for an employee",
    },
    "Time Off Request Total Days by Employee and Year": {
        "query": """
            SELECT COALESCE(SUM(amount), 0) FROM time_off_requests
            WHERE employee_id = :employee_id
            AND status = 'approved'
            AND start_date LIKE :year || '-%'
        """,
        "required_params": ["employee_id", "year"],
        "result_type": "decimal",
        "description": "Sum of approved time-off days for an employee in a specific year (year format: YYYY)",
    },
    "Time Off Request by Date Range": {
        "query": """
            SELECT COUNT(*) FROM time_off_requests
            WHERE start_date >= :start_date AND end_date <= :end_date
        """,
        "required_params": ["start_date", "end_date"],
        "result_type": "integer",
        "description": "Count time-off requests within a date range",
    },
    # =========================================================================
    # Time-Off Balance Checks
    # =========================================================================
    "Time Off Balance Count": {
        "query": "SELECT COUNT(*) FROM time_off_balances",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total time-off balance records",
    },
    "Time Off Balance Exists": {
        "query": "SELECT COUNT(*) FROM time_off_balances WHERE employee_id = :employee_id AND policy_id = :policy_id AND year = :year",
        "required_params": ["employee_id", "policy_id", "year"],
        "result_type": "boolean",
        "description": "Check if a balance record exists for employee/policy/year",
    },
    "Time Off Balance Amount": {
        "query": "SELECT balance FROM time_off_balances WHERE employee_id = :employee_id AND policy_id = :policy_id AND year = :year LIMIT 1",
        "required_params": ["employee_id", "policy_id", "year"],
        "result_type": "decimal",
        "description": "Get balance amount for an employee/policy/year",
    },
    "Time Off Balance Used": {
        "query": "SELECT used FROM time_off_balances WHERE employee_id = :employee_id AND policy_id = :policy_id AND year = :year LIMIT 1",
        "required_params": ["employee_id", "policy_id", "year"],
        "result_type": "decimal",
        "description": "Get used amount for an employee/policy/year",
    },
    "Time Off Balance Scheduled": {
        "query": "SELECT scheduled FROM time_off_balances WHERE employee_id = :employee_id AND policy_id = :policy_id AND year = :year LIMIT 1",
        "required_params": ["employee_id", "policy_id", "year"],
        "result_type": "decimal",
        "description": "Get scheduled amount for an employee/policy/year",
    },
    "Time Off Balance Available": {
        "query": "SELECT (balance - used - scheduled) FROM time_off_balances WHERE employee_id = :employee_id AND policy_id = :policy_id AND year = :year LIMIT 1",
        "required_params": ["employee_id", "policy_id", "year"],
        "result_type": "decimal",
        "description": "Get available balance (balance - used - scheduled)",
    },
    "Time Off Total Balance by Employee": {
        "query": "SELECT COALESCE(SUM(balance), 0) FROM time_off_balances WHERE employee_id = :employee_id AND year = :year",
        "required_params": ["employee_id", "year"],
        "result_type": "decimal",
        "description": "Sum of all balances for an employee in a year",
    },
    "Time Off Total Used by Employee": {
        "query": "SELECT COALESCE(SUM(used), 0) FROM time_off_balances WHERE employee_id = :employee_id AND year = :year",
        "required_params": ["employee_id", "year"],
        "result_type": "decimal",
        "description": "Sum of all used time for an employee in a year",
    },
    # =========================================================================
    # Time-Off Policy Checks
    # =========================================================================
    "Time Off Policy Count": {
        "query": "SELECT COUNT(*) FROM time_off_policies",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total time-off policies",
    },
    "Time Off Policy Exists": {
        "query": "SELECT COUNT(*) FROM time_off_policies WHERE id = :policy_id",
        "required_params": ["policy_id"],
        "result_type": "boolean",
        "description": "Check if a policy exists",
    },
    "Time Off Policy Exists by Name": {
        "query": "SELECT COUNT(*) FROM time_off_policies WHERE LOWER(name) = LOWER(:policy_name)",
        "required_params": ["policy_name"],
        "result_type": "boolean",
        "description": "Check if a policy exists by name",
    },
    "Time Off Policy Name": {
        "query": "SELECT name FROM time_off_policies WHERE id = :policy_id LIMIT 1",
        "required_params": ["policy_id"],
        "result_type": "string",
        "description": "Get name of a policy",
    },
    "Time Off Policy Accrual Type": {
        "query": "SELECT accrual_type FROM time_off_policies WHERE id = :policy_id LIMIT 1",
        "required_params": ["policy_id"],
        "result_type": "string",
        "description": "Get accrual type of a policy (manual, per_pay_period, annual, hourly)",
    },
    "Time Off Policy Accrual Rate": {
        "query": "SELECT accrual_rate FROM time_off_policies WHERE id = :policy_id LIMIT 1",
        "required_params": ["policy_id"],
        "result_type": "decimal",
        "description": "Get accrual rate of a policy",
    },
    "Time Off Policy Max Balance": {
        "query": "SELECT max_balance FROM time_off_policies WHERE id = :policy_id LIMIT 1",
        "required_params": ["policy_id"],
        "result_type": "decimal",
        "description": "Get max balance of a policy",
    },
    "Time Off Policy Carry Over": {
        "query": "SELECT carry_over FROM time_off_policies WHERE id = :policy_id LIMIT 1",
        "required_params": ["policy_id"],
        "result_type": "boolean",
        "description": "Check if policy allows carry over",
    },
    # =========================================================================
    # Time-Off Type Checks
    # =========================================================================
    "Time Off Type Count": {
        "query": "SELECT COUNT(*) FROM time_off_types",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total time-off types",
    },
    "Time Off Type Exists": {
        "query": "SELECT COUNT(*) FROM time_off_types WHERE id = :type_id",
        "required_params": ["type_id"],
        "result_type": "boolean",
        "description": "Check if a time-off type exists",
    },
    "Time Off Type Exists by Name": {
        "query": "SELECT COUNT(*) FROM time_off_types WHERE LOWER(name) = LOWER(:type_name)",
        "required_params": ["type_name"],
        "result_type": "boolean",
        "description": "Check if a time-off type exists by name",
    },
    "Time Off Type Name": {
        "query": "SELECT name FROM time_off_types WHERE id = :type_id LIMIT 1",
        "required_params": ["type_id"],
        "result_type": "string",
        "description": "Get name of a time-off type",
    },
    "Time Off Type Is Paid": {
        "query": "SELECT paid FROM time_off_types WHERE id = :type_id LIMIT 1",
        "required_params": ["type_id"],
        "result_type": "boolean",
        "description": "Check if a time-off type is paid",
    },
    # =========================================================================
    # Employee Policy Assignment Checks
    # =========================================================================
    "Employee Policy Assignment Count": {
        "query": "SELECT COUNT(*) FROM employee_policies",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total employee-policy assignments",
    },
    "Employee Policy Assignment Count by Employee": {
        "query": "SELECT COUNT(*) FROM employee_policies WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Count policy assignments for an employee",
    },
    "Employee Policy Assignment Exists": {
        "query": "SELECT COUNT(*) FROM employee_policies WHERE employee_id = :employee_id AND policy_id = :policy_id",
        "required_params": ["employee_id", "policy_id"],
        "result_type": "boolean",
        "description": "Check if employee has a specific policy assigned",
    },
    "Employee Policy Active Assignment": {
        "query": """
            SELECT COUNT(*) FROM employee_policies
            WHERE employee_id = :employee_id
            AND policy_id = :policy_id
            AND effective_date <= :as_of_date
            AND (end_date IS NULL OR end_date >= :as_of_date)
        """,
        "required_params": ["employee_id", "policy_id", "as_of_date"],
        "result_type": "boolean",
        "description": "Check if employee has an active policy assignment as of a date (format: YYYY-MM-DD)",
    },
    # =========================================================================
    # Department Checks
    # =========================================================================
    "Department Count": {
        "query": "SELECT COUNT(*) FROM departments",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total departments",
    },
    "Department Count Active": {
        "query": "SELECT COUNT(*) FROM departments WHERE archived = 0",
        "required_params": [],
        "result_type": "integer",
        "description": "Count active (non-archived) departments",
    },
    "Department Exists": {
        "query": "SELECT COUNT(*) FROM departments WHERE id = :department_id",
        "required_params": ["department_id"],
        "result_type": "boolean",
        "description": "Check if a department exists by ID",
    },
    "Department Exists by Name": {
        "query": "SELECT COUNT(*) FROM departments WHERE LOWER(name) = LOWER(:department_name)",
        "required_params": ["department_name"],
        "result_type": "boolean",
        "description": "Check if a department exists by name",
    },
    "Department Name": {
        "query": "SELECT name FROM departments WHERE id = :department_id LIMIT 1",
        "required_params": ["department_id"],
        "result_type": "string",
        "description": "Get name of a department",
    },
    "Department Is Archived": {
        "query": "SELECT archived FROM departments WHERE id = :department_id LIMIT 1",
        "required_params": ["department_id"],
        "result_type": "boolean",
        "description": "Check if a department is archived",
    },
    "Department Has Parent": {
        "query": "SELECT COUNT(*) FROM departments WHERE id = :department_id AND parent_id IS NOT NULL",
        "required_params": ["department_id"],
        "result_type": "boolean",
        "description": "Check if a department has a parent department",
    },
    "Department Child Count": {
        "query": "SELECT COUNT(*) FROM departments WHERE parent_id = :department_id",
        "required_params": ["department_id"],
        "result_type": "integer",
        "description": "Count child departments",
    },
    # =========================================================================
    # List Field Option Checks (Metadata)
    # =========================================================================
    "List Field Option Count": {
        "query": "SELECT COUNT(*) FROM list_field_options",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total list field options",
    },
    "List Field Option Count by Field": {
        "query": "SELECT COUNT(*) FROM list_field_options WHERE LOWER(field_name) = LOWER(:field_name)",
        "required_params": ["field_name"],
        "result_type": "integer",
        "description": "Count options for a specific field (department, job_title, location, etc.)",
    },
    "List Field Option Count Active by Field": {
        "query": "SELECT COUNT(*) FROM list_field_options WHERE LOWER(field_name) = LOWER(:field_name) AND archived = 0",
        "required_params": ["field_name"],
        "result_type": "integer",
        "description": "Count active options for a specific field",
    },
    "List Field Option Exists": {
        "query": "SELECT COUNT(*) FROM list_field_options WHERE LOWER(field_name) = LOWER(:field_name) AND LOWER(option_value) = LOWER(:option_value)",
        "required_params": ["field_name", "option_value"],
        "result_type": "boolean",
        "description": "Check if a specific option exists for a field",
    },
    # =========================================================================
    # Audit Log Checks
    # =========================================================================
    "Audit Log Count": {
        "query": "SELECT COUNT(*) FROM audit_log",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total audit log entries",
    },
    "Audit Log Count by Action": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE action = :action",
        "required_params": ["action"],
        "result_type": "integer",
        "description": "Count audit log entries by action (create, update, delete)",
    },
    "Audit Log Count by Entity Type": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE entity_type = :entity_type",
        "required_params": ["entity_type"],
        "result_type": "integer",
        "description": "Count audit log entries by entity type (employee, time_off_request, etc.)",
    },
    "Audit Log Count by Entity": {
        "query": "SELECT COUNT(*) FROM audit_log WHERE entity_type = :entity_type AND entity_id = :entity_id",
        "required_params": ["entity_type", "entity_id"],
        "result_type": "integer",
        "description": "Count audit log entries for a specific entity",
    },
    # =========================================================================
    # Custom Report Checks
    # =========================================================================
    "Custom Report Count": {
        "query": "SELECT COUNT(*) FROM custom_reports",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total custom reports",
    },
    "Custom Report Exists": {
        "query": "SELECT COUNT(*) FROM custom_reports WHERE id = :report_id",
        "required_params": ["report_id"],
        "result_type": "boolean",
        "description": "Check if a custom report exists",
    },
    "Custom Report Exists by Title": {
        "query": "SELECT COUNT(*) FROM custom_reports WHERE LOWER(title) = LOWER(:report_title)",
        "required_params": ["report_title"],
        "result_type": "boolean",
        "description": "Check if a custom report exists by title",
    },
    "Custom Report Title": {
        "query": "SELECT title FROM custom_reports WHERE id = :report_id LIMIT 1",
        "required_params": ["report_id"],
        "result_type": "string",
        "description": "Get title of a custom report",
    },
    # =========================================================================
    # Emergency Contact Checks
    # =========================================================================
    "Emergency Contact Count": {
        "query": "SELECT COUNT(*) FROM emergency_contacts",
        "required_params": [],
        "result_type": "integer",
        "description": "Count total emergency contacts",
    },
    "Emergency Contact Count by Employee": {
        "query": "SELECT COUNT(*) FROM emergency_contacts WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "integer",
        "description": "Count emergency contacts for an employee",
    },
    "Emergency Contact Exists for Employee": {
        "query": "SELECT COUNT(*) FROM emergency_contacts WHERE employee_id = :employee_id",
        "required_params": ["employee_id"],
        "result_type": "boolean",
        "description": "Check if employee has emergency contacts",
    },
    "Emergency Contact Primary Exists": {
        "query": "SELECT COUNT(*) FROM emergency_contacts WHERE employee_id = :employee_id AND is_primary = 1",
        "required_params": ["employee_id"],
        "result_type": "boolean",
        "description": "Check if employee has a primary emergency contact",
    },
}


async def bamboohr_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify any BambooHR field using dropdown + value interface.

    Domain experts select what to check from a dropdown and provide the expected value.
    The system automatically generates and runs the appropriate SQL query.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    # Validate helper results available
    if not input.helper_results:
        raise ValueError("Helper results not available")

    # Get field selection (strip arrow notation if present)
    field_to_check_raw = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check_raw:
        raise ValueError("field_to_check is required")

    # Strip arrow notation (e.g., "Employee Count by Status → Status" -> "Employee Count by Status")
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
    params = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            raise ValueError(
                f"Required parameter '{param}' is missing for {field_to_check}"
            )
        params[param] = value

    # Validate year parameter to prevent SQL wildcard injection in LIKE patterns
    if "year" in params:
        year_value = str(params["year"])
        if "%" in year_value or "_" in year_value:
            raise ValueError(
                f"Invalid year value: '{year_value}' contains wildcard characters"
            )
        # Also validate it looks like a year (4 digits)
        if not year_value.isdigit() or len(year_value) != 4:
            raise ValueError(
                f"Invalid year format: '{year_value}' (expected 4-digit year like '2025')"
            )

    # Get database connection from snapshot_dbs helper
    dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS, {})
    if not dbs:
        raise ValueError("No databases found in snapshot")

    # Filter to BambooHR databases only (avoid picking up other HR apps)
    bamboo_dbs = [
        (alias, db_info) for alias, db_info in dbs.items() if "bamboo" in alias.lower()
    ]
    if len(bamboo_dbs) != 1:
        raise ValueError(
            f"Expected exactly 1 BambooHR database, found {len(bamboo_dbs)}. Available: {list(dbs.keys())}"
        )

    _alias, db_info = bamboo_dbs[0]
    conn = db_info.get("connection")
    if not conn:
        raise ValueError("BambooHR database found but connection is None")

    # Execute query
    try:
        cursor = conn.cursor()
        result_row = cursor.execute(field_config["query"], params).fetchone()

        if result_row is None:
            result = None
        else:
            result = result_row[0]

        logger.info(
            f"BambooHR field check: {field_to_check} = {result} (expected: {expected_value})"
        )

    except Exception as e:
        logger.error(f"Error executing query for {field_to_check}: {e}")
        raise ValueError(f"Query execution failed: {e!s}") from e

    # Compare based on result_type
    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "decimal":
        # Get tolerance with validation (only needed for decimal comparisons)
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
        # Convert various result types to boolean
        # Handles: COUNT results (int), direct boolean columns, string values
        try:
            if isinstance(result, bool):
                actual_bool = result
            elif isinstance(result, int | float):
                actual_bool = result > 0
            elif isinstance(result, str):
                actual_bool = result.lower() in ("true", "1", "yes", "t", "y")
            else:
                # Fallback: try truthiness
                actual_bool = bool(result)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid boolean value - result: {result}. Error: {e!s}"
            ) from e
        expected_str = str(expected_value).lower()
        expected_bool = expected_str in ("true", "1", "yes", "t", "y")
        matches = actual_bool == expected_bool
        # Use appropriate text based on the type of boolean check
        if "Exists" in field_to_check:
            bool_text = "Exists" if actual_bool else "Does not exist"
        elif "Has" in field_to_check:
            bool_text = "Yes" if actual_bool else "No"
        elif (
            "Is" in field_to_check
            or "Paid" in field_to_check
            or "Carry Over" in field_to_check
        ):
            bool_text = "Yes" if actual_bool else "No"
        else:
            bool_text = "Yes" if actual_bool else "No"
        message = f"{field_to_check}: {bool_text} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            actual_int = int(result)
            expected_int = int(expected_value)
            matches = actual_int == expected_int
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}"
            ) from e
        message = f"{field_to_check}: {actual_int} {'✓' if matches else f'✗ expected {expected_int}'}"
    elif result_type == "string":
        actual_str = str(result).strip().lower() if result is not None else ""
        expected_str = str(expected_value).strip().lower()
        matches = actual_str == expected_str
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"
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
        },
        message=message,
    )
