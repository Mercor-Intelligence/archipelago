"""Constants for the BambooHR MCP server.

Static configuration data that doesn't change per-deployment.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldDefinition:
    """Static metadata about an employee field."""

    field_id: str
    field_name: str
    field_type: str  # int, text, email, phone, list, date, ssn, currency, bool
    category: str | None = None  # core, personal, contact, job, restricted, compensation
    alias: str | None = None
    required: bool = False
    deprecated: bool = False


# All BambooHR field definitions - static metadata mirroring BambooHR's API
FIELD_DEFINITIONS: dict[str, FieldDefinition] = {
    # Core fields
    "id": FieldDefinition("id", "Employee ID", "int", "core", required=True),
    "employeeNumber": FieldDefinition("employeeNumber", "Employee Number", "text", "core"),
    # Personal fields
    "firstName": FieldDefinition("firstName", "First Name", "text", "personal", required=True),
    "lastName": FieldDefinition("lastName", "Last Name", "text", "personal", required=True),
    "preferredName": FieldDefinition("preferredName", "Preferred Name", "text", "personal"),
    "displayName": FieldDefinition("displayName", "Display Name", "text", "personal"),
    # Contact fields
    "workEmail": FieldDefinition("workEmail", "Work Email", "email", "contact"),
    "homeEmail": FieldDefinition("homeEmail", "Home Email", "email", "contact"),
    "workPhone": FieldDefinition("workPhone", "Work Phone", "phone", "contact"),
    "mobilePhone": FieldDefinition("mobilePhone", "Mobile Phone", "phone", "contact"),
    # Job fields
    "department": FieldDefinition("department", "Department", "list", "job"),
    "division": FieldDefinition("division", "Division", "list", "job"),
    "jobTitle": FieldDefinition("jobTitle", "Job Title", "list", "job"),
    "location": FieldDefinition("location", "Location", "list", "job"),
    "hireDate": FieldDefinition("hireDate", "Hire Date", "date", "job"),
    "status": FieldDefinition("status", "Status", "list", "job"),
    "supervisorId": FieldDefinition("supervisorId", "Reports To", "int", "job"),
    # Restricted fields
    "ssn": FieldDefinition("ssn", "SSN", "ssn", "restricted"),
    "dateOfBirth": FieldDefinition("dateOfBirth", "Date of Birth", "date", "restricted"),
    "gender": FieldDefinition("gender", "Gender", "list", "restricted"),
    # Compensation fields
    "salary": FieldDefinition("salary", "Salary", "currency", "compensation"),
    "payRate": FieldDefinition("payRate", "Pay Rate", "currency", "compensation"),
    "payType": FieldDefinition("payType", "Pay Type", "list", "compensation"),
    # Additional list fields
    "employmentStatus": FieldDefinition(
        "employmentStatus", "Employment Status", "list", "job", alias="employmentStatus"
    ),
    "customOptions": FieldDefinition(
        "customOptions", "Custom Options", "options", "custom", alias="customOptions"
    ),
}


def get_field_definition(field_id: str) -> FieldDefinition | None:
    """Get a field definition by ID or alias."""
    if field_id in FIELD_DEFINITIONS:
        return FIELD_DEFINITIONS[field_id]
    # Alias lookup
    for field in FIELD_DEFINITIONS.values():
        if field.alias == field_id:
            return field
    return None


def get_all_fields(include_deprecated: bool = False) -> list[FieldDefinition]:
    """Get all field definitions."""
    if include_deprecated:
        return list(FIELD_DEFINITIONS.values())
    return [f for f in FIELD_DEFINITIONS.values() if not f.deprecated]


def get_fields_by_categories(
    categories: set[str] | None = None,
    exclude_categories: set[str] | None = None,
    include_deprecated: bool = False,
) -> list[FieldDefinition]:
    """Get field definitions filtered by category."""
    results = []
    for field in FIELD_DEFINITIONS.values():
        if field.deprecated and not include_deprecated:
            continue
        if categories is not None and field.category not in categories:
            continue
        if exclude_categories is not None and field.category in exclude_categories:
            continue
        results.append(field)
    return results
