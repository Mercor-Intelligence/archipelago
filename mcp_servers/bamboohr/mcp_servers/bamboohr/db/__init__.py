"""Database package for BambooHR MCP server.

Provides:
- SQLAlchemy models for all BambooHR entities
- Async session management with StaticPool for in-memory SQLite
- Database initialization and seeding functions
"""

from .models import (
    AccrualType,
    AuditLog,
    BalanceAdjustment,
    Base,
    CustomFieldValue,
    CustomReport,
    Department,
    EmergencyContact,
    Employee,
    EmployeePolicy,
    EmployeeStatus,
    FieldType,
    ListFieldOption,
    TimeOffBalance,
    TimeOffPolicy,
    TimeOffRequest,
    TimeOffRequestStatus,
    TimeOffType,
)
from .seed import seed_database, seed_system_data
from .session import AsyncSessionLocal, engine, get_session, init_db, reset_db

__all__ = [
    # Models
    "Base",
    "Employee",
    "EmployeeStatus",
    "Department",
    "EmergencyContact",
    "CustomFieldValue",
    "ListFieldOption",
    "TimeOffType",
    "TimeOffPolicy",
    "TimeOffRequest",
    "TimeOffRequestStatus",
    "TimeOffBalance",
    "BalanceAdjustment",
    "FieldType",
    "CustomReport",
    "AuditLog",
    "EmployeePolicy",
    "AccrualType",
    # Session
    "engine",
    "AsyncSessionLocal",
    "get_session",
    "init_db",
    "reset_db",
    # Seed
    "seed_database",
    "seed_system_data",
]
