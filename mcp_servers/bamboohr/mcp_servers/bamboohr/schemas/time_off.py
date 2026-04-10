"""Time-off Pydantic schemas for BambooHR API.

These schemas match the BambooHR API structure for time-off operations.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import Field, computed_field, model_validator


class TimeOffTypeResponse(BaseModel):
    """Response for a time-off type."""

    id: str = Field(..., description="Time-off type ID")
    name: str = Field(..., description="Type name (e.g., Vacation, Sick)")
    color: str | None = Field(None, description="Hex color code")
    paid: bool = Field(True, description="Whether this is paid time off")
    units: str = Field("days", description="Units (days or hours)")

    model_config = {"populate_by_name": True, "from_attributes": True}


class TimeOffTypesResponse(BaseModel):
    """Response for listing time-off types."""

    types: list[TimeOffTypeResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class GetTypesTimeOffTypeResponse(BaseModel):
    """Response for a time-off type in get_types endpoint.

    BambooHR API: GET /v1/meta/time_off/types/
    Includes icon field which is not present in standard TimeOffTypeResponse.
    """

    id: str = Field(..., description="Time-off type ID")
    name: str = Field(..., description="Type name (e.g., Vacation, Sick)")
    units: str = Field("days", description="Units (days or hours)")
    color: str = Field(..., description="Hex color code without # prefix")
    icon: str = Field(..., description="Icon identifier (e.g., palm-trees, medical)")

    model_config = {"populate_by_name": True, "from_attributes": True}


class DefaultHoursResponse(BaseModel):
    """Default hours configuration for time-off.

    BambooHR API: GET /v1/meta/time_off/types/
    """

    name: str = Field(..., description="Day name (Saturday, Sunday, or default)")
    amount: str = Field(..., description="Hours as string (e.g., '0', '8')")

    model_config = {"populate_by_name": True}


class GetTypesResponse(BaseModel):
    """Response for get_types endpoint.

    BambooHR API: GET /v1/meta/time_off/types/
    """

    timeOffTypes: list[GetTypesTimeOffTypeResponse] = Field(  # noqa: N815
        default_factory=list, description="List of time-off types"
    )
    defaultHours: list[DefaultHoursResponse] = Field(  # noqa: N815
        default_factory=list, description="Default hours configuration"
    )

    model_config = {"populate_by_name": True}


class TimeOffPolicyResponse(BaseModel):
    """Response for a time-off policy."""

    id: str = Field(..., description="Policy ID")
    name: str = Field(..., description="Policy name")
    type_id: str = Field(..., alias="typeId", description="Associated time-off type ID")
    type_name: str | None = Field(None, alias="typeName")
    accrual_type: str = Field("manual", alias="accrualType")
    accrual_rate: Decimal | None = Field(None, alias="accrualRate")
    accrual_frequency: str | None = Field(None, alias="accrualFrequency")
    max_balance: Decimal | None = Field(None, alias="maxBalance")
    carry_over: bool = Field(False, alias="carryOver")
    carry_over_max: Decimal | None = Field(None, alias="carryOverMax")

    model_config = {"populate_by_name": True, "from_attributes": True}


class TimeOffPoliciesResponse(BaseModel):
    """Response for listing time-off policies."""

    policies: list[TimeOffPolicyResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class TimeOffRequestCreate(BaseModel):
    """Schema for creating a time-off request."""

    employee_id: int = Field(..., alias="employeeId")
    type_id: int = Field(..., alias="typeId", description="Time-off type ID")
    start_date: date = Field(..., alias="startDate")
    end_date: date = Field(..., alias="endDate")
    amount: Decimal = Field(..., description="Number of days/hours")
    notes: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def validate_date_range(self) -> "TimeOffRequestCreate":
        """Ensure end_date is not before start_date."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for creating a time-off request."""
        return {
            "url_template": "/v1/employees/{employeeId}/time_off/request/",
            "method": "PUT",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"employeeId": str(self.employee_id)}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True

    model_config = {"populate_by_name": True}


class TimeOffRequestStatusUpdate(BaseModel):
    """Schema for updating time-off request status (approve/deny/cancel)."""

    request_id: int = Field(..., alias="requestId")
    status: Literal["approved", "denied", "canceled"] = Field(...)
    notes: str | None = Field(None, alias="note", max_length=1000)

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for updating request status."""
        return {
            "url_template": "/v1/time_off/requests/{requestId}/status/",
            "method": "PUT",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"requestId": str(self.request_id)}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        if not lookup_key:
            return True
        lookup_id = lookup_key.get("requestId")
        return str(lookup_id) == str(self.request_id) if lookup_id is not None else False

    model_config = {"populate_by_name": True}


class TimeOffRequestResponse(BaseModel):
    """Response for a time-off request."""

    id: str = Field(..., description="Request ID")
    employee_id: str = Field(..., alias="employeeId")
    employee_name: str | None = Field(None, alias="employeeName")
    type_id: str = Field(..., alias="typeId")
    type_name: str | None = Field(None, alias="typeName")
    start_date: date = Field(..., alias="start")
    end_date: date = Field(..., alias="end")
    amount: Decimal = Field(...)
    units: str = Field("days")
    status: str = Field(...)
    notes: str | None = Field(None)
    approver_id: str | None = Field(None, alias="approverId")
    approver_name: str | None = Field(None, alias="approverName")
    approved_at: datetime | None = Field(None, alias="approvedAt")
    created_at: datetime = Field(..., alias="created")

    model_config = {"populate_by_name": True, "from_attributes": True}


class TimeOffRequestsInput(BaseModel):
    """Input for listing time-off requests."""

    employee_id: int | None = Field(None, alias="employeeId", description="Filter by employee ID")
    start_date: date | None = Field(None, alias="startDate", description="Filter by start date")
    end_date: date | None = Field(None, alias="endDate", description="Filter by end date")
    status: list[str] | None = Field(None, description="Filter by status")
    type_id: int | None = Field(None, alias="typeId", description="Filter by time-off type ID")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for listing time-off requests."""
        return {
            "url_template": "/v1/time_off/requests/",
            "method": "GET",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True

    model_config = {"populate_by_name": True}


class TimeOffRequestsResponse(BaseModel):
    """Response for listing time-off requests."""

    requests: list[TimeOffRequestResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class TimeOffBalanceResponse(BaseModel):
    """Response for an employee's time-off balance."""

    id: str = Field(...)
    employee_id: str = Field(..., alias="employeeId")
    policy_id: str = Field(..., alias="policyId")
    policy_name: str | None = Field(None, alias="policyName")
    type_name: str | None = Field(None, alias="typeName")
    year: int = Field(...)
    balance: Decimal = Field(...)
    used: Decimal = Field(...)
    scheduled: Decimal = Field(Decimal("0"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> Decimal:
        """Calculate available balance: balance - used - scheduled."""
        return self.balance - self.used - self.scheduled

    model_config = {"populate_by_name": True, "from_attributes": True}


class TimeOffBalancesInput(BaseModel):
    """Input for getting employee time-off balances."""

    employee_id: int = Field(..., alias="employeeId", description="Employee ID to get balances for")
    year: int | None = Field(None, description="Year (defaults to current year)")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for getting balances."""
        return {
            "url_template": "/v1/employees/{employeeId}/time_off/calculator/",
            "method": "GET",
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

    model_config = {"populate_by_name": True}


class TimeOffBalancesResponse(BaseModel):
    """Response for employee time-off balances."""

    employee_id: str = Field(..., alias="employeeId")
    balances: list[TimeOffBalanceResponse] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class TimeOffBalanceUpdate(BaseModel):
    """Schema for updating a time-off balance (HR Admin only)."""

    employee_id: int = Field(..., alias="employeeId")
    policy_id: int = Field(..., alias="policyId")
    year: int = Field(...)
    balance: Decimal = Field(..., description="New balance amount")
    reason: str | None = Field(None, max_length=500)

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for updating balance."""
        return {
            "url_template": "/v1/employees/{employeeId}/time_off/balance/",
            "method": "PUT",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"employeeId": str(self.employee_id)}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True

    model_config = {"populate_by_name": True}


class PolicyAssignment(BaseModel):
    """Schema for assigning a policy to an employee."""

    employee_id: int = Field(..., alias="employeeId")
    policy_id: int = Field(..., alias="policyId")
    effective_date: date = Field(..., alias="effectiveDate")

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for assigning policy."""
        return {
            "url_template": "/v1/employees/{employeeId}/time_off/policies/",
            "method": "PUT",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {"employeeId": str(self.employee_id)}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True

    model_config = {"populate_by_name": True}


class WhosOutInput(BaseModel):
    """Input for getting who's out on a given date range."""

    start_date: date = Field(..., alias="startDate", description="Start date of the date range")
    end_date: date = Field(..., alias="endDate", description="End date of the date range")

    @model_validator(mode="after")
    def validate_date_range(self) -> "WhosOutInput":
        """Ensure end_date is not before start_date."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self

    @staticmethod
    def get_api_config() -> dict:
        """API configuration for who's out."""
        return {
            "url_template": "/v1/time_off/whos_out/",
            "method": "GET",
        }

    def to_template_values(self) -> dict[str, str]:
        """Convert to URL template values."""
        return {}

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input matches lookup criteria."""
        return True

    model_config = {"populate_by_name": True}


class WhosOutEntry(BaseModel):
    """Entry for who's out response."""

    employee_id: str = Field(..., alias="employeeId")
    employee_name: str = Field(..., alias="employeeName")
    type_name: str = Field(..., alias="typeName")
    start_date: date = Field(..., alias="start")
    end_date: date = Field(..., alias="end")
    amount: Decimal = Field(...)

    model_config = {"populate_by_name": True}


class WhosOutResponse(BaseModel):
    """Response for who's out query."""

    entries: list[WhosOutEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ============================================================================
# Policy Tool Schemas (for get_policies, get_employee_policies, assign_policy)
# ============================================================================


class PolicyAssignmentInput(BaseModel):
    """Input for a single policy assignment in assign_policy request.

    Per BambooHR API: accrualStartDate of "0000-00-00" or null means removal.
    """

    time_off_policy_id: int = Field(..., alias="timeOffPolicyId", description="Policy ID to assign")
    accrual_start_date: date | None = Field(
        ...,
        alias="accrualStartDate",
        description="Date policy becomes effective, or '0000-00-00'/null to remove",
    )

    @model_validator(mode="before")
    @classmethod
    def handle_removal_date(cls, data: Any) -> Any:
        """Convert '0000-00-00' to None to signal removal."""
        if isinstance(data, dict):
            # Check both alias and field name
            for key in ("accrualStartDate", "accrual_start_date"):
                if key in data and data[key] == "0000-00-00":
                    data[key] = None
        return data

    @property
    def is_removal(self) -> bool:
        """Check if this is a removal request (accrualStartDate is null/0000-00-00)."""
        return self.accrual_start_date is None

    model_config = {"populate_by_name": True}


class PolicyListEntry(BaseModel):
    """Entry for get_policies response."""

    id: int = Field(..., description="Policy ID")
    time_off_type_id: int = Field(..., alias="timeOffTypeId")
    name: str = Field(..., description="Policy name")
    effective_date: str | None = Field(None, alias="effectiveDate")
    type: str = Field(..., description="Accrual type (accruing, manual, discretionary)")

    model_config = {"populate_by_name": True}


class EmployeePolicyEntry(BaseModel):
    """Entry for get_employee_policies response."""

    time_off_type_id: int = Field(..., alias="timeOffTypeId")
    time_off_type_name: str = Field(..., alias="timeOffTypeName")
    policy_id: int = Field(..., alias="policyId")
    policy_name: str = Field(..., alias="policyName")
    effective_date: str = Field(..., alias="effectiveDate")

    model_config = {"populate_by_name": True}


class AssignedPolicyEntry(BaseModel):
    """Entry for assigned policies in assign_policy response."""

    policy_id: int = Field(..., alias="policyId")
    policy_name: str | None = Field(None, alias="policyName")
    effective_date: str = Field(..., alias="effectiveDate")

    model_config = {"populate_by_name": True}


class RemovedPolicyEntry(BaseModel):
    """Entry for removed policies in assign_policy response."""

    policy_id: int = Field(..., alias="policyId")
    policy_name: str | None = Field(None, alias="policyName")

    model_config = {"populate_by_name": True}


class AssignPolicyResponse(BaseModel):
    """Response from assign_policy tool."""

    assigned: list[AssignedPolicyEntry] = Field(default_factory=list)
    removed: list[RemovedPolicyEntry] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ============================================================================
# Balance Tool Schemas (for get_balances and update_balance tools)
# ============================================================================


class BalanceEntry(BaseModel):
    """Individual balance entry for get_balances response."""

    time_off_type_id: str = Field(..., alias="timeOffTypeId")
    time_off_type_name: str = Field(..., alias="timeOffTypeName")
    policy_id: int = Field(..., alias="policyId")
    policy_name: str = Field(..., alias="policyName")
    balance: str = Field(..., description="Current available balance")
    used: str = Field(..., description="Amount used this year")
    scheduled: str = Field(..., description="Amount scheduled but not yet taken")
    accrued: str = Field(..., description="Total accrued (balance + used)")
    carry_over: str = Field(..., alias="carryOver", description="Carried over from previous year")
    unit: str = Field(..., description="Unit type (days or hours)")
    effective_date: str = Field(..., alias="effectiveDate", description="Policy effective date")

    model_config = {"populate_by_name": True}


class BalanceAdjustmentResponse(BaseModel):
    """Response from update_balance tool."""

    adjustment_id: str = Field(..., alias="adjustmentId")
    new_balance: float = Field(..., alias="newBalance")
    previous_balance: float = Field(..., alias="previousBalance")
    created: str = Field(..., description="ISO timestamp of adjustment")
    warning: str | None = Field(None, description="Warning if balance goes negative")

    model_config = {"populate_by_name": True}


# ============================================================================
# Create Type Tool Schemas
# ============================================================================


class CreateTimeOffTypeRequest(BaseModel):
    """Request to create a new time-off type."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Type name (e.g., 'Vacation', 'Sick Leave', 'Personal')",
    )
    color: str | None = Field(
        None,
        max_length=20,
        description="Hex color code (e.g., '#4CAF50' or '4CAF50')",
    )
    paid: bool = Field(
        True,
        description="Whether this is paid time off",
    )
    units: Literal["days", "hours"] = Field(
        "days",
        description="Units for tracking: 'days' or 'hours'",
    )

    model_config = {"populate_by_name": True}


class CreateTimeOffTypeResponse(BaseModel):
    """Response from create_type tool."""

    id: str = Field(..., description="New time-off type ID")
    name: str = Field(..., description="Type name")
    created: str = Field(..., description="ISO timestamp of creation")

    model_config = {"populate_by_name": True}


# ============================================================================
# Create Policy Tool Schemas
# ============================================================================


class CreateTimeOffPolicyRequest(BaseModel):
    """Request to create a new time-off policy."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Policy name (e.g., 'Standard Vacation', 'Sick Leave')",
    )
    type_id: int = Field(
        ...,
        alias="typeId",
        description="Time-off type ID this policy applies to",
    )
    accrual_type: Literal[
        "manual", "per_pay_period", "annual", "monthly", "hourly", "discretionary"
    ] = Field(
        "manual",
        alias="accrualType",
        description="Accrual type",
    )
    accrual_rate: Decimal | None = Field(
        None,
        alias="accrualRate",
        description="Rate of accrual per period (e.g., 0.833 for ~20 days/year biweekly)",
    )
    accrual_frequency: str | None = Field(
        None,
        alias="accrualFrequency",
        description="Frequency of accrual (e.g., 'biweekly', 'monthly')",
    )
    max_balance: Decimal | None = Field(
        None,
        alias="maxBalance",
        description="Maximum balance cap (e.g., 40 days)",
    )
    carry_over: bool = Field(
        False,
        alias="carryOver",
        description="Whether unused balance carries over to next year",
    )
    carry_over_max: Decimal | None = Field(
        None,
        alias="carryOverMax",
        description="Maximum amount that can carry over",
    )

    model_config = {"populate_by_name": True}


class CreateTimeOffPolicyResponse(BaseModel):
    """Response from create_policy tool."""

    id: str = Field(..., description="New policy ID")
    name: str = Field(..., description="Policy name")
    type_id: str = Field(..., alias="typeId", description="Associated time-off type ID")
    created: str = Field(..., description="ISO timestamp of creation")

    model_config = {"populate_by_name": True}


# ============================================================================
# Estimate Future Balances Tool Schemas
# ============================================================================


class EstimateFutureBalancesInput(BaseModel):
    """Input for estimating future time-off balances."""

    employee_id: str = Field(
        ...,
        alias="employeeId",
        description="Employee ID to estimate balances for",
    )
    date: str = Field(
        ...,
        description="Future date to estimate balances for (format: YYYY-MM-DD)",
    )

    model_config = {"populate_by_name": True}


class EstimateFutureBalanceEntry(BaseModel):
    """Single balance estimate entry."""

    time_off_type_id: str = Field(..., alias="timeOffTypeId", description="Time-off type ID")
    time_off_type_name: str = Field(..., alias="timeOffTypeName", description="Time-off type name")
    estimated_balance: str = Field(
        ..., alias="estimatedBalance", description="Projected balance as of the target date"
    )
    current_balance: str = Field(
        ..., alias="currentBalance", description="Current available balance"
    )
    projected_accrual: str = Field(
        ..., alias="projectedAccrual", description="Projected accrual between now and target date"
    )
    pending_requests: str = Field(
        ..., alias="pendingRequests", description="Amount in pending requests"
    )
    as_of_date: str = Field(..., alias="asOfDate", description="Target date for the estimate")

    model_config = {"populate_by_name": True}


class EstimateFutureBalancesOutput(BaseModel):
    """Output for estimate future balances tool."""

    estimates: list[EstimateFutureBalanceEntry] = Field(
        default_factory=list, description="List of balance estimates by time-off type"
    )

    model_config = {"populate_by_name": True}
