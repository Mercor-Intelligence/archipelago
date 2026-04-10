"""Common Pydantic schemas shared across BambooHR API.

Includes pagination, error responses, and utility types.
"""

from datetime import datetime
from typing import Any

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field


class Pagination(BaseModel):
    """Pagination parameters for list operations."""

    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(20, ge=1, le=100, alias="pageSize", description="Items per page")

    @property
    def offset(self) -> int:
        """Calculate database offset from page number."""
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        """Alias for page_size for database queries."""
        return self.page_size

    model_config = {"populate_by_name": True}


class PaginatedResponse[T](BaseModel):
    """Generic paginated response wrapper."""

    data: list[T] = Field(default_factory=list)
    page: int = Field(1)
    page_size: int = Field(20, alias="pageSize")
    total: int = Field(0)
    total_pages: int = Field(0, alias="totalPages")

    model_config = {"populate_by_name": True}


class ErrorResponse(BaseModel):
    """Standard error response format."""

    error: str = Field(..., description="Error message")
    code: int = Field(..., description="HTTP status code")
    details: dict[str, Any] | None = Field(None, description="Additional error details")

    model_config = {"populate_by_name": True}


class ValidationErrorDetail(BaseModel):
    """Validation error detail for a specific field."""

    field: str = Field(..., description="Field name that failed validation")
    message: str = Field(..., description="Validation error message")
    value: Any | None = Field(None, description="The invalid value")


class ValidationErrorResponse(BaseModel):
    """Response for validation errors (422)."""

    error: str = Field("Validation Error")
    code: int = Field(422)
    errors: list[ValidationErrorDetail] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SuccessResponse(BaseModel):
    """Generic success response."""

    success: bool = Field(True)
    message: str | None = Field(None)
    timestamp: datetime | None = Field(None)

    model_config = {"populate_by_name": True}


class ResetStateInput(BaseModel):
    """Input for resetting database state."""

    confirm: bool = Field(..., description="Must be true to confirm reset")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for reset state."""
        return {
            "url_template": "/v1/admin/reset/",
            "method": "POST",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True


class ResetStateResponse(BaseModel):
    """Response for reset state operation."""

    success: bool = Field(...)
    message: str = Field(...)
    timestamp: datetime = Field(...)

    model_config = {"populate_by_name": True}


class FieldOption(BaseModel):
    """A single option in a list field."""

    id: str = Field(..., description="Option ID")
    value: str = Field(..., validation_alias="option_value", description="Option display value")
    archived: bool = Field(False, description="Whether the option is archived")

    model_config = {"populate_by_name": True, "from_attributes": True}


class ListFieldResponse(BaseModel):
    """Response for a list field with its options."""

    field_id: str = Field(..., alias="fieldId")
    field_name: str = Field(..., alias="fieldName")
    options: list[FieldOption] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class FieldDefinitionResponse(BaseModel):
    """Response for a field definition."""

    id: str = Field(..., validation_alias="field_id", description="Field ID")
    name: str = Field(..., validation_alias="field_name", description="Field display name")
    type: str = Field(
        ..., validation_alias="field_type", description="Field type (text, date, int, list, etc.)"
    )
    category: str | None = Field(None, description="Field category (personal, job, etc.)")
    alias: str | None = Field(None, description="Field alias")
    required: bool = Field(False)
    deprecated: bool = Field(False)

    model_config = {"populate_by_name": True, "from_attributes": True}


class FieldsResponse(BaseModel):
    """Response for listing field definitions."""

    fields: list[FieldDefinitionResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class CountryResponse(BaseModel):
    """Response for a country entry."""

    id: str = Field(..., description="Country code (ISO 3166-1 alpha-2)")
    name: str = Field(..., description="Country name")

    model_config = {"populate_by_name": True}


class StateResponse(BaseModel):
    """Response for a state/province entry."""

    id: str = Field(..., description="State code")
    name: str = Field(..., description="State name")
    country_id: str = Field(..., alias="countryId", description="Parent country code")

    model_config = {"populate_by_name": True}


class UserResponse(BaseModel):
    """Response for a BambooHR user (distinct from employee)."""

    id: str = Field(..., description="User ID")
    email: str = Field(..., description="User email")
    first_name: str | None = Field(None, alias="firstName")
    last_name: str | None = Field(None, alias="lastName")
    employee_id: str | None = Field(None, alias="employeeId")
    status: str = Field("active", description="User status")

    model_config = {"populate_by_name": True}


class UsersResponse(BaseModel):
    """Response for listing BambooHR users."""

    users: list[UserResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
