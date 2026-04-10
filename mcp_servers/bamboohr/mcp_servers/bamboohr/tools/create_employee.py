"""Tool: bamboo.employees.create

Creates a new employee record with validation of required fields,
uniqueness constraints, and referential integrity.

Business Logic:
- Validate persona is HR Admin (only HR Admin can create employees)
- Validate required fields: firstName, lastName
- Validate workEmail format and uniqueness
- Validate employeeNumber uniqueness
- Validate supervisor exists and is Active
- Set default status = "Active" if not provided
- Generate new employee ID and return with timestamp
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from db import Employee, EmployeeStatus, get_session
from mcp_auth import require_scopes
from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import EmailStr, Field
from sqlalchemy import select


def parse_date(date_str: str | None) -> date | None:
    """Parse date string in YYYY-MM-DD format to date object."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date format for {date_str}: {exc}") from exc


def parse_decimal(value: str | None) -> Decimal | None:
    """Parse string to Decimal for monetary values."""
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid value for salary/payRate: {value}") from exc


class CreateEmployeeInput(BaseModel):
    """Input schema for creating a new employee.

    Required fields: firstName, lastName
    HR Admin only operation.
    """

    first_name: str = Field(..., alias="firstName", max_length=100, min_length=1)
    last_name: str = Field(..., alias="lastName", max_length=100, min_length=1)
    preferred_name: str | None = Field(None, alias="preferredName", max_length=100)
    middle_name: str | None = Field(None, alias="middleName", max_length=100)
    display_name: str | None = Field(None, alias="displayName", max_length=200)

    # Contact
    work_email: EmailStr | None = Field(None, alias="workEmail")
    home_email: EmailStr | None = Field(None, alias="homeEmail")
    work_phone: str | None = Field(None, alias="workPhone", max_length=50)
    work_phone_extension: str | None = Field(None, alias="workPhoneExtension", max_length=20)
    mobile_phone: str | None = Field(None, alias="mobilePhone", max_length=50)

    # Address
    address1: str | None = Field(None, max_length=255)
    address2: str | None = Field(None, max_length=255)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    zipcode: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)

    # Job info
    employee_number: str | None = Field(None, alias="employeeNumber", max_length=50)
    department: str | None = Field(None, max_length=255)
    division: str | None = Field(None, max_length=255)
    job_title: str | None = Field(None, alias="jobTitle", max_length=255)
    location: str | None = Field(None, max_length=255)

    # Employment
    status: str | None = Field(None, pattern="^(Active|Inactive|Terminated)$")
    hire_date: str | None = Field(None, alias="hireDate", pattern=r"^\d{4}-\d{2}-\d{2}$")
    termination_date: str | None = Field(
        None, alias="terminationDate", pattern=r"^\d{4}-\d{2}-\d{2}$"
    )

    # Organization
    supervisor_id: int | None = Field(None, alias="supervisorId")

    # Photo
    photo_url: str | None = Field(None, alias="photoUrl", max_length=500)
    linkedin: str | None = Field(None, alias="linkedIn", max_length=255)

    # Restricted fields (HR Admin only)
    ssn: str | None = Field(None, max_length=20)
    date_of_birth: str | None = Field(None, alias="dateOfBirth", pattern=r"^\d{4}-\d{2}-\d{2}$")
    gender: str | None = Field(None, max_length=50)
    marital_status: str | None = Field(None, alias="maritalStatus", max_length=50)
    ethnicity: str | None = Field(None, max_length=100)

    # Compensation (HR Admin only)
    salary: str | None = Field(None)
    pay_rate: str | None = Field(None, alias="payRate")
    pay_per: str | None = Field(None, alias="payPer", max_length=50)
    pay_type: str | None = Field(None, alias="payType", max_length=50)
    pay_schedule: str | None = Field(None, alias="paySchedule", max_length=100)

    model_config = {"populate_by_name": True}


class CreateEmployeeOutput(BaseModel):
    """Output schema for employee creation.

    Returns the new employee ID and creation timestamp.
    """

    id: str = Field(..., description="New employee ID")
    created: str = Field(..., description="Creation timestamp (ISO 8601)")

    model_config = {"populate_by_name": True}


@require_scopes("write:employees")
async def create_employee(input_data: CreateEmployeeInput) -> CreateEmployeeOutput:
    """Create a new employee with validation."""
    async with get_session() as session:
        # 1. Validate email uniqueness (if provided)
        if input_data.work_email:
            result = await session.execute(
                select(Employee).where(Employee.work_email == str(input_data.work_email))
            )
            if result.scalar_one_or_none():
                raise ValueError("Employee with this email already exists")

        # 2. Validate employee number uniqueness (if provided)
        if input_data.employee_number:
            result = await session.execute(
                select(Employee).where(Employee.employee_number == input_data.employee_number)
            )
            if result.scalar_one_or_none():
                raise ValueError("Employee number already in use")

        # 3. Validate supervisor (if provided)
        if input_data.supervisor_id is not None:
            supervisor = await session.get(Employee, input_data.supervisor_id)
            if not supervisor:
                raise ValueError("Supervisor not found")
            if supervisor.status != EmployeeStatus.ACTIVE.value:
                raise ValueError("Supervisor must be an active employee")

        # 4. Set default status if not provided
        status = input_data.status or EmployeeStatus.ACTIVE.value

        # 5. Create employee record
        employee = Employee(
            first_name=input_data.first_name,
            last_name=input_data.last_name,
            preferred_name=input_data.preferred_name,
            middle_name=input_data.middle_name,
            display_name=input_data.display_name,
            work_email=str(input_data.work_email) if input_data.work_email else None,
            home_email=str(input_data.home_email) if input_data.home_email else None,
            work_phone=input_data.work_phone,
            work_phone_extension=input_data.work_phone_extension,
            mobile_phone=input_data.mobile_phone,
            address1=input_data.address1,
            address2=input_data.address2,
            city=input_data.city,
            state=input_data.state,
            zipcode=input_data.zipcode,
            country=input_data.country,
            employee_number=input_data.employee_number,
            department=input_data.department,
            division=input_data.division,
            job_title=input_data.job_title,
            location=input_data.location,
            status=status,
            hire_date=parse_date(input_data.hire_date),
            termination_date=parse_date(input_data.termination_date),
            supervisor_id=input_data.supervisor_id,
            photo_url=input_data.photo_url,
            linkedin=input_data.linkedin,
            ssn=input_data.ssn,
            date_of_birth=parse_date(input_data.date_of_birth),
            gender=input_data.gender,
            marital_status=input_data.marital_status,
            ethnicity=input_data.ethnicity,
            salary=parse_decimal(input_data.salary),
            pay_rate=parse_decimal(input_data.pay_rate),
            pay_per=input_data.pay_per,
            pay_type=input_data.pay_type,
            pay_schedule=input_data.pay_schedule,
        )

        session.add(employee)
        await session.flush()  # Get the ID
        await session.commit()

        # 6. Return result
        created_timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return CreateEmployeeOutput(id=str(employee.id), created=created_timestamp)
