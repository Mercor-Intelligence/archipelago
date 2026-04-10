"""Seed data for BambooHR MCP server.

Provides test fixtures that can be loaded via --seed flag.
This creates a realistic dataset for testing and development.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Employee,
    EmployeePolicy,
    EmployeeStatus,
    ListFieldOption,
    TimeOffBalance,
    TimeOffPolicy,
    TimeOffRequest,
    TimeOffRequestStatus,
    TimeOffType,
)


async def seed_list_field_options(session: AsyncSession) -> None:
    """Seed list field options (departments, job titles, locations)."""
    options = [
        # Departments
        ListFieldOption(field_name="department", option_value="Engineering", sort_order=1),
        ListFieldOption(field_name="department", option_value="Product", sort_order=2),
        ListFieldOption(field_name="department", option_value="Design", sort_order=3),
        ListFieldOption(field_name="department", option_value="Marketing", sort_order=4),
        ListFieldOption(field_name="department", option_value="Sales", sort_order=5),
        ListFieldOption(field_name="department", option_value="Human Resources", sort_order=6),
        ListFieldOption(field_name="department", option_value="Finance", sort_order=7),
        ListFieldOption(field_name="department", option_value="Operations", sort_order=8),
        # Job Titles
        ListFieldOption(field_name="jobTitle", option_value="Software Engineer", sort_order=1),
        ListFieldOption(
            field_name="jobTitle", option_value="Senior Software Engineer", sort_order=2
        ),
        ListFieldOption(field_name="jobTitle", option_value="Engineering Manager", sort_order=3),
        ListFieldOption(field_name="jobTitle", option_value="Product Manager", sort_order=4),
        ListFieldOption(field_name="jobTitle", option_value="Designer", sort_order=5),
        ListFieldOption(field_name="jobTitle", option_value="HR Manager", sort_order=6),
        ListFieldOption(field_name="jobTitle", option_value="Recruiter", sort_order=7),
        ListFieldOption(field_name="jobTitle", option_value="Sales Representative", sort_order=8),
        # Locations
        ListFieldOption(field_name="location", option_value="San Francisco, CA", sort_order=1),
        ListFieldOption(field_name="location", option_value="New York, NY", sort_order=2),
        ListFieldOption(field_name="location", option_value="Austin, TX", sort_order=3),
        ListFieldOption(field_name="location", option_value="Seattle, WA", sort_order=4),
        ListFieldOption(field_name="location", option_value="Remote", sort_order=5),
        # Divisions
        ListFieldOption(field_name="division", option_value="North America", sort_order=1),
        ListFieldOption(field_name="division", option_value="Europe", sort_order=2),
        ListFieldOption(field_name="division", option_value="Asia Pacific", sort_order=3),
    ]
    session.add_all(options)
    await session.flush()


async def seed_time_off_types(session: AsyncSession) -> list[TimeOffType]:
    """Seed time-off types."""
    types = [
        TimeOffType(id=1, name="Vacation", color="#4CAF50", paid=True, units="days"),
        TimeOffType(id=2, name="Sick Leave", color="#F44336", paid=True, units="days"),
        TimeOffType(id=3, name="Personal", color="#2196F3", paid=True, units="days"),
        TimeOffType(id=4, name="Unpaid Leave", color="#9E9E9E", paid=False, units="days"),
        TimeOffType(id=5, name="Bereavement", color="#795548", paid=True, units="days"),
        TimeOffType(id=6, name="Jury Duty", color="#607D8B", paid=True, units="days"),
    ]
    session.add_all(types)
    await session.flush()
    return types


async def seed_time_off_policies(session: AsyncSession) -> list[TimeOffPolicy]:
    """Seed time-off policies."""
    policies = [
        TimeOffPolicy(
            id=1,
            name="Standard Vacation",
            type_id=1,
            accrual_type="per_pay_period",
            accrual_rate=Decimal("0.833"),  # ~20 days/year
            max_balance=Decimal("40"),
            carry_over=True,
            carry_over_max=Decimal("5"),
        ),
        TimeOffPolicy(
            id=2,
            name="Sick Leave",
            type_id=2,
            accrual_type="annual",
            accrual_rate=Decimal("10"),
            max_balance=Decimal("40"),
            carry_over=True,
            carry_over_max=Decimal("40"),
        ),
        TimeOffPolicy(
            id=3,
            name="Personal Days",
            type_id=3,
            accrual_type="annual",
            accrual_rate=Decimal("3"),
            max_balance=Decimal("3"),
            carry_over=False,
        ),
    ]
    session.add_all(policies)
    await session.flush()
    return policies


async def seed_employees(session: AsyncSession) -> list[Employee]:
    """Seed employee data matching the users.json personas."""
    employees = [
        # HR Admin (id=1) - matches users.json
        Employee(
            id=1,
            employee_number="EMP-001",
            first_name="Sarah",
            last_name="Johnson",
            display_name="Sarah Johnson",
            work_email="sarah.johnson@company.com",
            department="Human Resources",
            job_title="HR Manager",
            location="San Francisco, CA",
            status=EmployeeStatus.ACTIVE.value,
            hire_date=date(2020, 3, 15),
            salary=Decimal("120000"),
            pay_type="salary",
            pay_schedule="bi-weekly",
        ),
        # Manager (id=2) - matches users.json
        Employee(
            id=2,
            employee_number="EMP-002",
            first_name="Michael",
            last_name="Chen",
            display_name="Michael Chen",
            work_email="michael.chen@company.com",
            department="Engineering",
            job_title="Engineering Manager",
            location="San Francisco, CA",
            status=EmployeeStatus.ACTIVE.value,
            hire_date=date(2019, 6, 1),
            supervisor_id=1,
            salary=Decimal("180000"),
            pay_type="salary",
            pay_schedule="bi-weekly",
        ),
        # Employee (id=3) - matches users.json, reports to Manager
        Employee(
            id=3,
            employee_number="EMP-003",
            first_name="Emily",
            last_name="Davis",
            display_name="Emily Davis",
            work_email="emily.davis@company.com",
            department="Engineering",
            job_title="Senior Software Engineer",
            location="Remote",
            status=EmployeeStatus.ACTIVE.value,
            hire_date=date(2021, 1, 10),
            supervisor_id=2,
            salary=Decimal("150000"),
            pay_type="salary",
            pay_schedule="bi-weekly",
        ),
        # Additional employees for testing
        Employee(
            id=4,
            employee_number="EMP-004",
            first_name="James",
            last_name="Wilson",
            display_name="James Wilson",
            work_email="james.wilson@company.com",
            department="Engineering",
            job_title="Software Engineer",
            location="Austin, TX",
            status=EmployeeStatus.ACTIVE.value,
            hire_date=date(2022, 4, 15),
            supervisor_id=2,
            salary=Decimal("120000"),
            pay_type="salary",
            pay_schedule="bi-weekly",
        ),
        Employee(
            id=5,
            employee_number="EMP-005",
            first_name="Lisa",
            last_name="Martinez",
            display_name="Lisa Martinez",
            work_email="lisa.martinez@company.com",
            department="Product",
            job_title="Product Manager",
            location="New York, NY",
            status=EmployeeStatus.ACTIVE.value,
            hire_date=date(2021, 8, 20),
            supervisor_id=1,
            salary=Decimal("140000"),
            pay_type="salary",
            pay_schedule="bi-weekly",
        ),
    ]
    session.add_all(employees)
    await session.flush()
    return employees


async def seed_employee_policies(session: AsyncSession) -> None:
    """Assign time-off policies to employees."""
    assignments = [
        # All active employees get standard policies
        EmployeePolicy(employee_id=1, policy_id=1, effective_date=date(2020, 3, 15)),
        EmployeePolicy(employee_id=1, policy_id=2, effective_date=date(2020, 3, 15)),
        EmployeePolicy(employee_id=1, policy_id=3, effective_date=date(2020, 3, 15)),
        EmployeePolicy(employee_id=2, policy_id=1, effective_date=date(2019, 6, 1)),
        EmployeePolicy(employee_id=2, policy_id=2, effective_date=date(2019, 6, 1)),
        EmployeePolicy(employee_id=2, policy_id=3, effective_date=date(2019, 6, 1)),
        EmployeePolicy(employee_id=3, policy_id=1, effective_date=date(2021, 1, 10)),
        EmployeePolicy(employee_id=3, policy_id=2, effective_date=date(2021, 1, 10)),
        EmployeePolicy(employee_id=3, policy_id=3, effective_date=date(2021, 1, 10)),
        EmployeePolicy(employee_id=4, policy_id=1, effective_date=date(2022, 4, 15)),
        EmployeePolicy(employee_id=4, policy_id=2, effective_date=date(2022, 4, 15)),
        EmployeePolicy(employee_id=4, policy_id=3, effective_date=date(2022, 4, 15)),
        EmployeePolicy(employee_id=5, policy_id=1, effective_date=date(2021, 8, 20)),
        EmployeePolicy(employee_id=5, policy_id=2, effective_date=date(2021, 8, 20)),
        EmployeePolicy(employee_id=5, policy_id=3, effective_date=date(2021, 8, 20)),
    ]
    session.add_all(assignments)
    await session.flush()


async def seed_time_off_balances(session: AsyncSession, year: int | None = None) -> None:
    """Seed time-off balances for current year."""
    # Use current year if not specified (was hardcoded to 2024 which caused issues in 2026)
    if year is None:
        year = date.today().year
    balances = [
        # Employee 1 (HR Admin)
        TimeOffBalance(
            employee_id=1, policy_id=1, year=year, balance=Decimal("20"), used=Decimal("5")
        ),
        TimeOffBalance(
            employee_id=1, policy_id=2, year=year, balance=Decimal("10"), used=Decimal("2")
        ),
        TimeOffBalance(
            employee_id=1, policy_id=3, year=year, balance=Decimal("3"), used=Decimal("0")
        ),
        # Employee 2 (Manager)
        TimeOffBalance(
            employee_id=2, policy_id=1, year=year, balance=Decimal("25"), used=Decimal("10")
        ),
        TimeOffBalance(
            employee_id=2, policy_id=2, year=year, balance=Decimal("10"), used=Decimal("3")
        ),
        TimeOffBalance(
            employee_id=2, policy_id=3, year=year, balance=Decimal("3"), used=Decimal("1")
        ),
        # Employee 3 (Regular Employee)
        TimeOffBalance(
            employee_id=3, policy_id=1, year=year, balance=Decimal("15"), used=Decimal("8")
        ),
        TimeOffBalance(
            employee_id=3, policy_id=2, year=year, balance=Decimal("10"), used=Decimal("0")
        ),
        TimeOffBalance(
            employee_id=3, policy_id=3, year=year, balance=Decimal("3"), used=Decimal("2")
        ),
        # Employee 4
        TimeOffBalance(
            employee_id=4, policy_id=1, year=year, balance=Decimal("12"), used=Decimal("4")
        ),
        TimeOffBalance(
            employee_id=4, policy_id=2, year=year, balance=Decimal("10"), used=Decimal("1")
        ),
        TimeOffBalance(
            employee_id=4, policy_id=3, year=year, balance=Decimal("3"), used=Decimal("0")
        ),
        # Employee 5
        TimeOffBalance(
            employee_id=5, policy_id=1, year=year, balance=Decimal("18"), used=Decimal("6")
        ),
        TimeOffBalance(
            employee_id=5, policy_id=2, year=year, balance=Decimal("10"), used=Decimal("4")
        ),
        TimeOffBalance(
            employee_id=5, policy_id=3, year=year, balance=Decimal("3"), used=Decimal("1")
        ),
    ]
    session.add_all(balances)
    await session.flush()


async def seed_time_off_requests(session: AsyncSession) -> None:
    """Seed some time-off requests for testing."""
    requests = [
        # Approved vacation for Employee 3
        TimeOffRequest(
            employee_id=3,
            type_id=1,
            policy_id=1,
            start_date=date(2024, 12, 23),
            end_date=date(2024, 12, 27),
            amount=Decimal("5"),
            status=TimeOffRequestStatus.APPROVED.value,
            notes="Holiday vacation",
            approver_id=2,
            approved_at=datetime(2024, 11, 15, 10, 0, 0, tzinfo=UTC),
        ),
        # Pending request for Employee 4
        TimeOffRequest(
            employee_id=4,
            type_id=1,
            policy_id=1,
            start_date=date(2025, 1, 6),
            end_date=date(2025, 1, 10),
            amount=Decimal("5"),
            status=TimeOffRequestStatus.REQUESTED.value,
            notes="New Year vacation",
        ),
        # Sick leave for Employee 5
        TimeOffRequest(
            employee_id=5,
            type_id=2,
            policy_id=2,
            start_date=date(2024, 11, 20),
            end_date=date(2024, 11, 21),
            amount=Decimal("2"),
            status=TimeOffRequestStatus.APPROVED.value,
            approver_id=1,
            approved_at=datetime(2024, 11, 20, 8, 30, 0, tzinfo=UTC),
        ),
    ]
    session.add_all(requests)
    await session.flush()


async def seed_system_data(session: AsyncSession) -> None:
    """Seed system/schema data required for blank-slate functionality.

    Note: Field definitions are now constants in constants.py, not database records.

    Does NOT seed user data like:
    - Employees (use bamboo_employees_create)
    - Time-off types (use bamboo_time_off_create_type)
    - Time-off policies (use bamboo_time_off_create_policy)
    - List field option values (use bamboo_meta_update_field_options)
    """
    # Field definitions are now constants, no seeding needed
    await session.commit()


async def seed_database(session: AsyncSession) -> None:
    """Run all seed functions to populate the database with test data.

    Call this when --seed flag is provided on startup.
    This is for TESTING/DEVELOPMENT - creates sample employees, policies, etc.

    For production blank-slate, use seed_system_data() instead (auto-called on init).
    """
    # Seed in dependency order
    await seed_list_field_options(session)
    await seed_time_off_types(session)
    await seed_time_off_policies(session)
    await seed_employees(session)
    await seed_employee_policies(session)
    await seed_time_off_balances(session)
    await seed_time_off_requests(session)
    # Note: Field definitions are now constants in constants.py

    await session.commit()
