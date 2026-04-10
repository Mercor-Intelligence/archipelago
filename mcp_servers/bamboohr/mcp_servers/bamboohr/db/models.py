"""SQLAlchemy database models for BambooHR MCP server.

These models match the BambooHR API structure and support:
- Employee management with hierarchical relationships
- Time-off requests, policies, and balances
- Custom field definitions and metadata
- Audit logging for compliance
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    pass


def utc_now() -> datetime:
    """Get current UTC time."""
    return datetime.now(UTC)


class EmployeeStatus(str, Enum):
    """Employee employment status."""

    ACTIVE = "Active"
    INACTIVE = "Inactive"
    TERMINATED = "Terminated"


class TimeOffRequestStatus(str, Enum):
    """Time-off request status."""

    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class AccrualType(str, Enum):
    """Time-off policy accrual type."""

    MANUAL = "manual"
    PER_PAY_PERIOD = "per_pay_period"
    ANNUAL = "annual"
    MONTHLY = "monthly"
    HOURLY = "hourly"
    DISCRETIONARY = "discretionary"


class FieldType(str, Enum):
    """Custom field data types."""

    TEXT = "text"
    DATE = "date"
    INT = "int"
    BOOL = "bool"
    LIST = "list"
    CURRENCY = "currency"
    SSN = "ssn"
    EMAIL = "email"
    PHONE = "phone"


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class Employee(Base):
    """Employee model matching BambooHR employee schema.

    Supports hierarchical relationships (supervisor), and foreign keys
    to list field options for department, job title, and location.
    """

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_number: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)

    # Name fields
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    preferred_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    middle_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Contact info
    work_email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    home_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    work_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    work_phone_extension: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mobile_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Address
    address1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    zipcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Job info (references to list_field_options)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)
    division: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Employment status
    status: Mapped[str] = mapped_column(String(50), default=EmployeeStatus.ACTIVE.value)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    termination_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Supervisor relationship (self-referential)
    supervisor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    # Sensitive fields (HR Admin only)
    ssn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ethnicity: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Compensation (HR Admin only)
    salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pay_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    pay_per: Mapped[str | None] = mapped_column(String(50), nullable=True)  # hour, day, week, etc.
    pay_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # salary, hourly
    pay_schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Photo
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Social
    linkedin: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Relationships
    supervisor: Mapped["Employee | None"] = relationship(
        "Employee", remote_side=[id], back_populates="direct_reports"
    )
    direct_reports: Mapped[list["Employee"]] = relationship("Employee", back_populates="supervisor")
    time_off_requests: Mapped[list["TimeOffRequest"]] = relationship(
        "TimeOffRequest",
        foreign_keys="TimeOffRequest.employee_id",
        back_populates="employee",
        cascade="all, delete-orphan",
    )
    time_off_balances: Mapped[list["TimeOffBalance"]] = relationship(
        "TimeOffBalance", back_populates="employee", cascade="all, delete-orphan"
    )
    emergency_contacts: Mapped[list["EmergencyContact"]] = relationship(
        "EmergencyContact", back_populates="employee", cascade="all, delete-orphan"
    )
    custom_field_values: Mapped[list["CustomFieldValue"]] = relationship(
        "CustomFieldValue", back_populates="employee", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Employee(id={self.id}, name='{self.first_name} {self.last_name}')>"


class ListFieldOption(Base):
    """List field options for dropdown fields (department, job title, location, etc.).

    These are the options that appear in BambooHR's configurable list fields.
    """

    __tablename__ = "list_field_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    option_value: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))

    __table_args__ = (UniqueConstraint("field_name", "option_value", name="uq_field_option"),)

    def __repr__(self) -> str:
        return f"<ListFieldOption(field='{self.field_name}', value='{self.option_value}')>"


class TimeOffType(Base):
    """Time-off types (Vacation, Sick, Personal, etc.)."""

    __tablename__ = "time_off_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # hex color
    paid: Mapped[bool] = mapped_column(Boolean, default=True)
    units: Mapped[str] = mapped_column(String(20), default="days")  # days or hours

    # Relationships
    requests: Mapped[list["TimeOffRequest"]] = relationship(
        "TimeOffRequest", back_populates="time_off_type"
    )

    def __repr__(self) -> str:
        return f"<TimeOffType(id={self.id}, name='{self.name}')>"


class TimeOffPolicy(Base):
    """Time-off policies defining accrual rules and balances."""

    __tablename__ = "time_off_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    type_id: Mapped[int] = mapped_column(Integer, ForeignKey("time_off_types.id"), nullable=False)
    accrual_type: Mapped[str] = mapped_column(String(50), default=AccrualType.MANUAL.value)
    accrual_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    accrual_frequency: Mapped[str | None] = mapped_column(String(50), nullable=True)
    max_balance: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    carry_over: Mapped[bool] = mapped_column(Boolean, default=False)
    carry_over_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # Relationships
    time_off_type: Mapped["TimeOffType"] = relationship("TimeOffType")
    balances: Mapped[list["TimeOffBalance"]] = relationship(
        "TimeOffBalance", back_populates="policy"
    )

    def __repr__(self) -> str:
        return f"<TimeOffPolicy(id={self.id}, name='{self.name}')>"


class TimeOffRequest(Base):
    """Time-off requests from employees."""

    __tablename__ = "time_off_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type_id: Mapped[int] = mapped_column(Integer, ForeignKey("time_off_types.id"), nullable=False)
    policy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("time_off_policies.id", ondelete="SET NULL"), nullable=True
    )

    # Request details
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    units: Mapped[str] = mapped_column(String(20), default="days")
    status: Mapped[str] = mapped_column(
        String(50), default=TimeOffRequestStatus.REQUESTED.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Approval
    approver_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Relationships
    employee: Mapped["Employee"] = relationship(
        "Employee", foreign_keys=[employee_id], back_populates="time_off_requests"
    )
    time_off_type: Mapped["TimeOffType"] = relationship("TimeOffType", back_populates="requests")
    policy: Mapped["TimeOffPolicy | None"] = relationship("TimeOffPolicy")
    approver: Mapped["Employee | None"] = relationship("Employee", foreign_keys=[approver_id])

    def __repr__(self) -> str:
        return f"<TimeOffRequest(id={self.id}, status='{self.status}')>"


class TimeOffBalance(Base):
    """Employee time-off balances by policy and year."""

    __tablename__ = "time_off_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("time_off_policies.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    used: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    scheduled: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))

    __table_args__ = (
        UniqueConstraint("employee_id", "policy_id", "year", name="uq_employee_policy_year"),
    )

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee", back_populates="time_off_balances")
    policy: Mapped["TimeOffPolicy"] = relationship("TimeOffPolicy", back_populates="balances")

    def __repr__(self) -> str:
        return f"<TimeOffBalance(id={self.id}, balance={self.balance})>"


class CustomReport(Base):
    """Saved custom report definitions."""

    __tablename__ = "custom_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    field_ids: Mapped[dict | list] = mapped_column(JSON, default=list)
    filters: Mapped[dict | list] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Relationships
    creator: Mapped["Employee | None"] = relationship("Employee")

    def __repr__(self) -> str:
        return f"<CustomReport(id={self.id}, title='{self.title}')>"


class AuditLog(Base):
    """Audit trail for compliance and history tracking."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # create, update, delete
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    actor_persona: Mapped[str | None] = mapped_column(String(50), nullable=True)
    old_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP"), index=True
    )

    # Relationships
    actor: Mapped["Employee | None"] = relationship("Employee")

    def __repr__(self) -> str:
        return f"<AuditLog(action='{self.action}', entity='{self.entity_type}:{self.entity_id}')>"


class EmployeePolicy(Base):
    """Junction table for employee-policy assignments."""

    __tablename__ = "employee_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("time_off_policies.id", ondelete="CASCADE"), nullable=False
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint("employee_id", "policy_id", "effective_date", name="uq_emp_policy_date"),
    )

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee")
    policy: Mapped["TimeOffPolicy"] = relationship("TimeOffPolicy")

    def __repr__(self) -> str:
        return f"<EmployeePolicy(employee_id={self.employee_id}, policy_id={self.policy_id})>"


class Department(Base):
    """Department model with hierarchical support.

    Supports org chart hierarchy via parent_id for nested departments.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Self-referential relationship for hierarchy
    parent: Mapped["Department | None"] = relationship(
        "Department", remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Department"]] = relationship("Department", back_populates="parent")

    def __repr__(self) -> str:
        return f"<Department(id={self.id}, name='{self.name}')>"


class EmergencyContact(Base):
    """Employee emergency contact information."""

    __tablename__ = "emergency_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Relationship to employee: spouse, parent, sibling, etc.
    relation_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee", back_populates="emergency_contacts")

    def __repr__(self) -> str:
        return f"<EmergencyContact(id={self.id}, name='{self.name}')>"


class BalanceAdjustment(Base):
    """Audit trail for manual balance adjustments.

    Records all manual adjustments to time-off balances with full audit info.
    """

    __tablename__ = "balance_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("time_off_policies.id", ondelete="RESTRICT"), nullable=False
    )
    adjustment_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    previous_balance: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    new_balance: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    adjusted_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee", foreign_keys=[employee_id])
    policy: Mapped["TimeOffPolicy"] = relationship("TimeOffPolicy")
    adjusted_by: Mapped["Employee | None"] = relationship("Employee", foreign_keys=[adjusted_by_id])

    def __repr__(self) -> str:
        return f"<BalanceAdjustment(id={self.id}, amount={self.amount})>"


class CustomFieldValue(Base):
    """Stores custom field values for employees.

    Links employees to FieldDefinition via field_id and stores the actual value.
    """

    __tablename__ = "custom_field_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,  # References constants.FIELD_DEFINITIONS
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now, server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (UniqueConstraint("employee_id", "field_id", name="uq_employee_field"),)

    # Relationships
    employee: Mapped["Employee"] = relationship("Employee", back_populates="custom_field_values")

    def __repr__(self) -> str:
        return f"<CustomFieldValue(employee_id={self.employee_id}, field='{self.field_id}')>"
