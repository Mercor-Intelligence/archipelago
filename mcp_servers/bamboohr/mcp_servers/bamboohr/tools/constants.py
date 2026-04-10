"""Field constants for BambooHR employee tools.

Centralizes all field-related constants used across employee tools:
- Field sets for persona-based access control
- Field alias mappings (camelCase API <-> snake_case DB)
- Update permission sets per persona
"""

# =============================================================================
# FIELD ALIAS MAPPING
# =============================================================================

# Mapping from camelCase API names to snake_case DB names
FIELD_ALIAS_MAP: dict[str, str] = {
    "employeeNumber": "employee_number",
    "firstName": "first_name",
    "lastName": "last_name",
    "preferredName": "preferred_name",
    "middleName": "middle_name",
    "displayName": "display_name",
    "workEmail": "work_email",
    "homeEmail": "home_email",
    "workPhone": "work_phone",
    "workPhoneExtension": "work_phone_extension",
    "mobilePhone": "mobile_phone",
    "jobTitle": "job_title",
    "hireDate": "hire_date",
    "terminationDate": "termination_date",
    "supervisorId": "supervisor_id",
    "photoUrl": "photo_url",
    "linkedIn": "linkedin",
    "dateOfBirth": "date_of_birth",
    "maritalStatus": "marital_status",
    "payRate": "pay_rate",
    "payPer": "pay_per",
    "payType": "pay_type",
    "paySchedule": "pay_schedule",
}

# Reverse mapping for API responses
FIELD_ALIAS_MAP_REVERSE: dict[str, str] = {v: k for k, v in FIELD_ALIAS_MAP.items()}

# =============================================================================
# ALL EMPLOYEE FIELDS
# =============================================================================

# All available employee fields (snake_case DB names)
ALL_FIELDS: set[str] = {
    "id",
    "employee_number",
    "first_name",
    "last_name",
    "preferred_name",
    "middle_name",
    "display_name",
    "work_email",
    "home_email",
    "work_phone",
    "work_phone_extension",
    "mobile_phone",
    "address1",
    "address2",
    "city",
    "state",
    "zipcode",
    "country",
    "department",
    "division",
    "job_title",
    "location",
    "status",
    "hire_date",
    "termination_date",
    "supervisor_id",
    "photo_url",
    "linkedin",
    # Restricted fields
    "ssn",
    "date_of_birth",
    "gender",
    "marital_status",
    "ethnicity",
    "salary",
    "pay_rate",
    "pay_per",
    "pay_type",
    "pay_schedule",
}

# =============================================================================
# PERSONA-BASED VIEW PERMISSIONS (per BUILD_PLAN section 3.2.2)
# =============================================================================

# Fields restricted to HR Admin only (Restricted + Compensation categories)
# Manager and Employee cannot see these fields
RESTRICTED_FIELDS: set[str] = {
    # Restricted category
    "ssn",
    "date_of_birth",
    "gender",
    "marital_status",
    "ethnicity",
    # Compensation category
    "salary",
    "pay_rate",
    "pay_per",
    "pay_type",
    "pay_schedule",
}

# Fields allowed for Employee persona (Core Identity + Job Info + Contact)
EMPLOYEE_ALLOWED_FIELDS: set[str] = {
    # Core Identity
    "id",
    "employee_number",
    "first_name",
    "last_name",
    "preferred_name",
    "middle_name",
    "display_name",
    "photo_url",
    # Job Info
    "department",
    "division",
    "job_title",
    "location",
    "status",
    "hire_date",
    "supervisor_id",
    # Contact
    "work_email",
    "home_email",
    "work_phone",
    "work_phone_extension",
    "mobile_phone",
    "linkedin",
    # Address (part of Contact)
    "address1",
    "address2",
    "city",
    "state",
    "zipcode",
    "country",
}

# =============================================================================
# UPDATE PERMISSIONS (per BUILD_PLAN section 3.2.4)
# =============================================================================

# Fields that cannot be updated after creation
IMMUTABLE_FIELDS: set[str] = {
    "id",
}

# Fields Manager can update (direct reports only)
MANAGER_UPDATABLE_FIELDS: set[str] = {
    # Job Information
    "job_title",
    "department",
    "division",
    "location",
    # Contact
    "work_email",
    "work_phone",
    "work_phone_extension",
    "mobile_phone",
    # Address
    "address1",
    "address2",
    "city",
    "state",
    "zipcode",
    "country",
    # Organization
    "supervisor_id",
    # Status
    "status",
}

# HR Admin can update all fields except immutable ones
HR_ADMIN_UPDATABLE_FIELDS: set[str] = MANAGER_UPDATABLE_FIELDS | {
    # Core Identity
    "first_name",
    "last_name",
    "employee_number",
    "preferred_name",
    "middle_name",
    "display_name",
    # Personal (Restricted)
    "ssn",
    "date_of_birth",
    "gender",
    "marital_status",
    "ethnicity",
    # Compensation
    "salary",
    "pay_rate",
    "pay_per",
    "pay_type",
    "pay_schedule",
    # Other
    "home_email",
    "termination_date",
    "photo_url",
    "linkedin",
    # Employment dates
    "hire_date",
}

# Fields with uniqueness constraints
UNIQUE_FIELDS: set[str] = {"work_email", "employee_number"}

# =============================================================================
# DIRECTORY FIELDS (per BUILD_PLAN section 3.2.5)
# =============================================================================

# Default fields shown in company directory
DEFAULT_DIRECTORY_FIELDS: list[str] = [
    "displayName",
    "firstName",
    "lastName",
    "department",
    "location",
]

# =============================================================================
# TYPE CONVERSION FIELDS
# =============================================================================

# Fields that require Decimal conversion
DECIMAL_FIELDS: set[str] = {"salary", "pay_rate"}

# Fields that require date conversion
DATE_FIELDS: set[str] = {"date_of_birth", "termination_date", "hire_date"}

# =============================================================================
# UPDATABLE FIELDS (camelCase for API/MCP interface)
# =============================================================================

# All updatable fields in camelCase (for update_employee function signature)
# Excludes employeeId (required param) and immutable fields (id)
UPDATABLE_FIELDS_CAMELCASE: tuple[str, ...] = (
    "firstName",
    "lastName",
    "preferredName",
    "middleName",
    "displayName",
    "workEmail",
    "homeEmail",
    "workPhone",
    "workPhoneExtension",
    "mobilePhone",
    "address1",
    "address2",
    "city",
    "state",
    "zipcode",
    "country",
    "department",
    "division",
    "jobTitle",
    "location",
    "status",
    "supervisorId",
    "photoUrl",
    "linkedIn",
    "ssn",
    "dateOfBirth",
    "gender",
    "maritalStatus",
    "ethnicity",
    "salary",
    "payRate",
    "payPer",
    "payType",
    "paySchedule",
    "employeeNumber",
    "terminationDate",
    "hireDate",
)


__all__ = [
    "ALL_FIELDS",
    "DATE_FIELDS",
    "DECIMAL_FIELDS",
    "DEFAULT_DIRECTORY_FIELDS",
    "EMPLOYEE_ALLOWED_FIELDS",
    "FIELD_ALIAS_MAP",
    "FIELD_ALIAS_MAP_REVERSE",
    "HR_ADMIN_UPDATABLE_FIELDS",
    "IMMUTABLE_FIELDS",
    "MANAGER_UPDATABLE_FIELDS",
    "RESTRICTED_FIELDS",
    "UNIQUE_FIELDS",
    "UPDATABLE_FIELDS_CAMELCASE",
]
