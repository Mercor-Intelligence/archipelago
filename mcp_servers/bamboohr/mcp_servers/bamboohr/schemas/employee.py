"""Employee Pydantic schemas for BambooHR API.

These schemas match the BambooHR API structure for employee operations.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import EmailStr, Field


class EmployeeBase(BaseModel):
    """Base employee fields shared across create/update/response."""

    first_name: str | None = Field(None, alias="firstName", max_length=100)
    last_name: str | None = Field(None, alias="lastName", max_length=100)
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
    department: str | None = Field(None, max_length=255)
    division: str | None = Field(None, max_length=255)
    job_title: str | None = Field(None, alias="jobTitle", max_length=255)
    location: str | None = Field(None, max_length=255)

    # Employment
    status: Literal["Active", "Inactive", "Terminated"] | None = Field(None)
    hire_date: date | None = Field(None, alias="hireDate")
    termination_date: date | None = Field(None, alias="terminationDate")

    # Organization
    supervisor_id: int | None = Field(None, alias="supervisorId")

    # Photo/Social
    photo_url: str | None = Field(None, alias="photoUrl", max_length=500)
    linkedin: str | None = Field(None, max_length=255)

    model_config = {"populate_by_name": True}


class EmployeeRestrictedFields(BaseModel):
    """Restricted fields only accessible by HR Admin."""

    ssn: str | None = Field(None, max_length=20)
    date_of_birth: date | None = Field(None, alias="dateOfBirth")
    gender: str | None = Field(None, max_length=50)
    marital_status: str | None = Field(None, alias="maritalStatus", max_length=50)
    ethnicity: str | None = Field(None, max_length=100)

    model_config = {"populate_by_name": True}


class EmployeeCompensation(BaseModel):
    """Compensation fields only accessible by HR Admin."""

    salary: Decimal | None = Field(None)
    pay_rate: Decimal | None = Field(None, alias="payRate")
    pay_per: str | None = Field(None, alias="payPer", max_length=50)
    pay_type: str | None = Field(None, alias="payType", max_length=50)
    pay_schedule: str | None = Field(None, alias="paySchedule", max_length=100)

    model_config = {"populate_by_name": True}


class EmployeeCreate(EmployeeBase, EmployeeRestrictedFields, EmployeeCompensation):
    """Schema for creating a new employee. HR Admin only.

    Required fields: firstName, lastName
    """

    first_name: str = Field(..., alias="firstName", max_length=100)
    last_name: str = Field(..., alias="lastName", max_length=100)
    employee_number: str | None = Field(None, alias="employeeNumber", max_length=50)

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for employee creation."""
        return {
            "url_template": "/v1/employees/",
            "method": "POST",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True


class EmployeeUpdate(EmployeeBase, EmployeeRestrictedFields, EmployeeCompensation):
    """Schema for updating an employee.

    All fields optional. Manager can only update non-restricted fields.
    HR Admin can update all fields.
    """

    employee_id: int = Field(..., alias="employeeId")
    employee_number: str | None = Field(None, alias="employeeNumber", max_length=50)

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for employee update."""
        return {
            "url_template": "/v1/employees/{employeeId}/",
            "method": "POST",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"employeeId": str(self.employee_id)}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        if not lookup_key:
            return True
        lookup_id = lookup_key.get("employeeId")
        return str(lookup_id) == str(self.employee_id) if lookup_id is not None else False


class EmployeeGetInput(BaseModel):
    """Input for getting a single employee."""

    employee_id: str = Field(..., alias="employeeId", description="Employee ID or '0' for self")
    fields: str | None = Field(
        None,
        description="Comma-separated list of fields to return",
    )

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for getting an employee."""
        return {
            "url_template": "/v1/employees/{employeeId}/",
            "method": "GET",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"employeeId": self.employee_id}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        if not lookup_key:
            return True
        lookup_id = lookup_key.get("employeeId")
        return str(lookup_id) == str(self.employee_id) if lookup_id is not None else False

    model_config = {"populate_by_name": True}


class EmployeeResponse(EmployeeBase):
    """Response schema for employee data.

    Fields returned depend on persona permissions:
    - HR Admin: All fields
    - Manager: Limited fields (no compensation/SSN)
    - Employee: Self only, limited fields
    """

    id: str = Field(..., description="Employee ID")
    employee_number: str | None = Field(None, alias="employeeNumber")

    # Restricted fields (HR Admin only, null for others)
    ssn: str | None = Field(None)
    date_of_birth: date | None = Field(None, alias="dateOfBirth")
    gender: str | None = Field(None)
    marital_status: str | None = Field(None, alias="maritalStatus")
    ethnicity: str | None = Field(None)

    # Compensation (HR Admin only)
    salary: Decimal | None = Field(None)
    pay_rate: Decimal | None = Field(None, alias="payRate")
    pay_per: str | None = Field(None, alias="payPer")
    pay_type: str | None = Field(None, alias="payType")
    pay_schedule: str | None = Field(None, alias="paySchedule")

    model_config = {"populate_by_name": True, "from_attributes": True}


class DirectoryEntry(BaseModel):
    """Employee entry in the company directory."""

    id: str = Field(..., description="Employee ID")
    display_name: str | None = Field(None, alias="displayName")
    first_name: str | None = Field(None, alias="firstName")
    last_name: str | None = Field(None, alias="lastName")
    preferred_name: str | None = Field(None, alias="preferredName")
    job_title: str | None = Field(None, alias="jobTitle")
    department: str | None = None
    division: str | None = None
    location: str | None = None
    work_phone: str | None = Field(None, alias="workPhone")
    work_phone_extension: str | None = Field(None, alias="workPhoneExtension")
    mobile_phone: str | None = Field(None, alias="mobilePhone")
    work_email: str | None = Field(None, alias="workEmail")
    photo_url: str | None = Field(None, alias="photoUrl")
    supervisor_id: str | None = Field(None, alias="supervisorId")

    model_config = {"populate_by_name": True, "from_attributes": True}


class DirectoryResponse(BaseModel):
    """Response for employee directory listing."""

    employees: list[DirectoryEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class CompanyInfoResponse(BaseModel):
    """Response for company information."""

    name: str = Field(..., description="Company name")
    subdomain: str | None = Field(None, description="BambooHR subdomain")
    employee_count: int = Field(0, alias="employeeCount")

    model_config = {"populate_by_name": True}


class DirectoryField(BaseModel):
    """Field definition in the directory response."""

    id: str = Field(..., description="Field ID (e.g., 'displayName', 'firstName')")
    type: str = Field(..., description="Field type (e.g., 'text', 'list', 'email')")
    name: str = Field(..., description="Human-readable field name")

    model_config = {"populate_by_name": True}


class DirectoryEmployee(BaseModel):
    """Employee entry in the directory response.

    Note: Uses camelCase field names to match BambooHR API format.
    Ignoring N815 (mixedCase) warnings as these match the external API.
    """

    id: str = Field(..., description="Employee ID")
    displayName: str | None = Field(None, alias="displayName", description="Employee display name")  # noqa: N815
    firstName: str | None = Field(None, alias="firstName", description="First name")  # noqa: N815
    lastName: str | None = Field(None, alias="lastName", description="Last name")  # noqa: N815
    preferredName: str | None = Field(None, alias="preferredName", description="Preferred name")  # noqa: N815
    jobTitle: str | None = Field(None, alias="jobTitle", description="Job title")  # noqa: N815
    workPhone: str | None = Field(None, alias="workPhone", description="Work phone")  # noqa: N815
    workPhoneExtension: str | None = Field(  # noqa: N815
        None, alias="workPhoneExtension", description="Work phone extension"
    )
    department: str | None = Field(None, description="Department")
    location: str | None = Field(None, description="Location")
    workEmail: str | None = Field(None, alias="workEmail", description="Work email")  # noqa: N815
    supervisorId: str | None = Field(  # noqa: N815
        None, alias="supervisorId", description="Supervisor employee ID"
    )
    photoUrl: str | None = Field(None, alias="photoUrl", description="Photo URL")  # noqa: N815
    division: str | None = Field(None, description="Division")
    status: str | None = Field(None, description="Employment status (Active, Inactive, Terminated)")
    hireDate: str | None = Field(None, alias="hireDate", description="Hire date")  # noqa: N815

    @classmethod
    def from_employee(cls, employee) -> "DirectoryEmployee":
        """Convert Employee model to DirectoryEmployee schema.

        Args:
            employee: Employee SQLAlchemy model instance

        Returns:
            DirectoryEmployee schema instance
        """
        return cls(
            id=str(employee.id),
            displayName=employee.display_name,
            firstName=employee.first_name,
            lastName=employee.last_name,
            preferredName=employee.preferred_name,
            jobTitle=employee.job_title,
            workPhone=employee.work_phone,
            workPhoneExtension=employee.work_phone_extension,
            department=employee.department,
            location=employee.location,
            workEmail=employee.work_email,
            supervisorId=str(employee.supervisor_id) if employee.supervisor_id else None,
            photoUrl=employee.photo_url,
            division=employee.division,
            status=employee.status,
            hireDate=str(employee.hire_date) if employee.hire_date else None,
        )

    model_config = {"populate_by_name": True}


class GetDirectoryOutput(BaseModel):
    """Response schema for bamboo.employees.get_directory."""

    fields: list[DirectoryField] = Field(
        default_factory=list, description="Field definitions for the directory"
    )
    employees: list[DirectoryEmployee] = Field(
        default_factory=list, description="List of employees in the directory"
    )

    model_config = {"populate_by_name": True}
