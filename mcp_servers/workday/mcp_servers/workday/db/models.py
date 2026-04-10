"""SQLAlchemy database models for Workday HCM.

These are ORM models that map to database tables.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now():
    """Get current UTC time."""
    return datetime.now(UTC)


def utc_now_str():
    """Get current UTC time as ISO 8601 string.

    Used by Help module models which store timestamps as strings
    for portability. Note: onupdate won't work with string columns,
    so updated_at must be set explicitly in repositories.
    """
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class SupervisoryOrg(Base):
    """Supervisory Organization model."""

    __tablename__ = "supervisory_orgs"

    org_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_name: Mapped[str] = mapped_column(String, nullable=False)
    org_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["supervisory", "division", "enterprise", "cost_center", "location"]},
    )
    parent_org_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=True
    )
    manager_worker_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("workers.worker_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class JobProfile(Base):
    """Job Profile model."""

    __tablename__ = "job_profiles"

    job_profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    job_family: Mapped[str] = mapped_column(String, nullable=False)
    job_level: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class CostCenter(Base):
    """Cost Center model."""

    __tablename__ = "cost_centers"

    cost_center_id: Mapped[str] = mapped_column(String, primary_key=True)
    cost_center_name: Mapped[str] = mapped_column(String, nullable=False)
    org_id: Mapped[str] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Location(Base):
    """Location model."""

    __tablename__ = "locations"

    location_id: Mapped[str] = mapped_column(String, primary_key=True)
    location_name: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Position(Base):
    """Position model."""

    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("job_profiles.job_profile_id"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=False
    )
    fte: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["open", "filled", "closed", "frozen"]},
    )
    worker_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("workers.worker_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Worker(Base):
    """Worker model."""

    __tablename__ = "workers"

    worker_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("job_profiles.job_profile_id"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=False
    )
    cost_center_id: Mapped[str] = mapped_column(
        String, ForeignKey("cost_centers.cost_center_id"), nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("locations.location_id"), nullable=True
    )
    position_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("positions.position_id"), nullable=True
    )
    employment_status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["active", "terminated", "on_leave"]},
    )
    fte: Mapped[float] = mapped_column(Float, nullable=False)
    hire_date: Mapped[str] = mapped_column(String, nullable=False)
    termination_date: Mapped[str | None] = mapped_column(String, nullable=True)
    effective_date: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Movement(Base):
    """Movement Event model for lifecycle tracking."""

    __tablename__ = "movements"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String, ForeignKey("workers.worker_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["hire", "termination", "transfer", "promotion"]},
    )
    event_date: Mapped[str] = mapped_column(String, nullable=False)
    from_org_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=True
    )
    to_org_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("supervisory_orgs.org_id"), nullable=True
    )
    from_cost_center_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cost_centers.cost_center_id"), nullable=True
    )
    to_cost_center_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cost_centers.cost_center_id"), nullable=True
    )
    from_job_profile_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("job_profiles.job_profile_id"), nullable=True
    )
    to_job_profile_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("job_profiles.job_profile_id"), nullable=True
    )
    from_position_id: Mapped[str | None] = mapped_column(String, nullable=True)
    to_position_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


# =============================================================================
# V2 PRE-ONBOARDING COORDINATION MODELS
# =============================================================================


class PolicyReference(Base):
    """Policy reference model for country/role-specific constraints."""

    __tablename__ = "policy_references"

    policy_id: Mapped[str] = mapped_column(String, primary_key=True)
    country: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "background_check",
                "benefits_waiting_period",
                "comp_band",
                "constraints",
                "credentialing",
                "drug_screen",
                "i9_verification",
                "lead_times",
                "payroll_cutoffs",
                "prerequisites",
                "visa",
            ]
        },
    )
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # JSON object
    effective_date: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    case_links: Mapped[list["CasePolicyLink"]] = relationship(
        "CasePolicyLink", back_populates="policy"
    )


class PayrollCutoff(Base):
    """Payroll cutoff rules by country."""

    __tablename__ = "payroll_cutoffs"

    cutoff_id: Mapped[str] = mapped_column(String, primary_key=True)
    country: Mapped[str] = mapped_column(String, nullable=False)
    cutoff_day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    processing_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    effective_date: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Case(Base):
    """Pre-onboarding case model."""

    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String, nullable=False)
    requisition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    country: Mapped[str] = mapped_column(String, nullable=False)
    employment_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="full_time",
        info={"enum": ["full_time", "part_time", "contractor", "intern"]},
    )
    owner_persona: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="open",
        info={
            "enum": [
                "closed",
                "in_progress",
                "new",
                "open",
                "pending_approval",
                "resolved",
                "waiting_on_candidate",
                "waiting_on_hcm",
            ]
        },
    )
    proposed_start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    due_date: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    milestones: Mapped[list["Milestone"]] = relationship("Milestone", back_populates="case")
    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="case")
    audit_entries: Mapped[list["AuditEntry"]] = relationship("AuditEntry", back_populates="case")
    exceptions: Mapped[list["CaseException"]] = relationship("CaseException", back_populates="case")
    policy_links: Mapped[list["CasePolicyLink"]] = relationship(
        "CasePolicyLink", back_populates="case"
    )
    hcm_worker_state: Mapped["HCMWorkerState | None"] = relationship(
        "HCMWorkerState", back_populates="case", uselist=False
    )
    hcm_write_logs: Mapped[list["HCMWriteLog"]] = relationship("HCMWriteLog", back_populates="case")


class Milestone(Base):
    """Case milestone tracking model."""

    __tablename__ = "milestones"

    milestone_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    milestone_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "approvals",
                "background_check",
                "benefits_setup",
                "credentialing",
                "documents",
                "drug_screen",
                "i9_verification",
                "offer_approval",
                "screening",
                "work_authorization",
            ]
        },
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
        info={"enum": ["blocked", "completed", "in_progress", "not_started", "pending", "waived"]},
    )
    evidence_link: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_date: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="milestones")
    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="milestone")

    __table_args__ = (UniqueConstraint("case_id", "milestone_type", name="uq_case_milestone_type"),)


class Task(Base):
    """Manual task model."""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    milestone_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("milestones.milestone_id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    owner_persona: Mapped[str] = mapped_column(String, nullable=False)
    due_date: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
        info={
            "enum": [
                "blocked",
                "canceled",
                "cancelled",
                "completed",
                "in_progress",
                "not_started",
                "pending",
            ]
        },
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="tasks")
    milestone: Mapped["Milestone | None"] = relationship("Milestone", back_populates="tasks")


class AuditEntry(Base):
    """Append-only audit trail model."""

    __tablename__ = "audit_entries"

    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    actor_persona: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_refs: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    evidence_links: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON object
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="audit_entries")


class CaseException(Base):
    """Exception request and approval model.

    Named CaseException to avoid conflict with Python's built-in Exception.
    """

    __tablename__ = "exceptions"

    exception_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    milestone_type: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    affected_policy_refs: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    approval_status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
        info={"enum": ["approved", "denied", "expired", "pending", "rejected", "requested"]},
    )
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="exceptions")


class CasePolicyLink(Base):
    """Case-to-policy association model."""

    __tablename__ = "case_policy_links"

    link_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String, ForeignKey("policy_references.policy_id"), nullable=False
    )
    attached_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    attached_by: Mapped[str] = mapped_column(String, nullable=False)
    decision_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="policy_links")
    policy: Mapped["PolicyReference"] = relationship("PolicyReference", back_populates="case_links")


class HCMWorkerState(Base):
    """Derived HCM worker state model (gated write-back target)."""

    __tablename__ = "hcm_worker_state"

    worker_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    onboarding_status: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        info={"enum": ["not_started", "in_progress", "ready", "finalized", "complete"]},
    )
    onboarding_readiness: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposed_start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    hire_finalized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_date: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="hcm_worker_state")

    __table_args__ = (UniqueConstraint("case_id", name="uq_hcm_worker_state_case"),)


class HCMWriteLog(Base):
    """Immutable HCM write log model."""

    __tablename__ = "hcm_write_log"

    log_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.case_id"), nullable=False)
    worker_id: Mapped[str] = mapped_column(String, nullable=False)
    write_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "confirm_start_date",
                "create_worker",
                "finalize_hire",
                "update_comp",
                "update_job",
                "update_org",
                "update_readiness",
            ]
        },
    )
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    new_value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    actor_persona: Mapped[str] = mapped_column(String, nullable=False)
    policy_refs: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    milestone_evidence: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    case: Mapped["Case"] = relationship("Case", back_populates="hcm_write_logs")


# =============================================================================
# HELP MODULE MODELS (from workday_help integration)
#
# Design Note: Help models use ISO 8601 strings for timestamps instead of
# DateTime for portability with the original workday_help implementation.
# Since SQLAlchemy's onupdate doesn't work with string columns, updated_at
# must be set explicitly in repository methods.
# =============================================================================


class HelpCase(Base):
    """Help desk case model for support tickets."""

    __tablename__ = "help_cases"

    case_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "Pre-Onboarding",
                "background_check",
                "benefits_enrollment",
                "candidate_query",
                "offer_letter_issue",
                "onboarding_support",
                "visa_support",
            ]
        },
    )
    owner: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "closed",
                "in_progress",
                "new",
                "open",
                "resolved",
                "waiting",
            ]
        },
    )
    candidate_identifier: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    due_date: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    # Use 'meta' as Python attr to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON

    # Relationships
    timeline_events: Mapped[list["HelpTimelineEvent"]] = relationship(
        "HelpTimelineEvent", back_populates="case"
    )
    messages: Mapped[list["HelpMessage"]] = relationship("HelpMessage", back_populates="case")
    attachments: Mapped[list["HelpAttachment"]] = relationship(
        "HelpAttachment", back_populates="case"
    )
    audit_logs: Mapped[list["HelpAuditLog"]] = relationship("HelpAuditLog", back_populates="case")


class HelpTimelineEvent(Base):
    """Timeline event model (append-only, immutable) for help cases."""

    __tablename__ = "help_timeline_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("help_cases.case_id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Use 'meta' as Python attr to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON

    # Relationships
    case: Mapped["HelpCase"] = relationship("HelpCase", back_populates="timeline_events")


class HelpMessage(Base):
    """Message model for help case communications."""

    __tablename__ = "help_messages"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("help_cases.case_id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["internal", "inbound", "outbound"]},
    )
    audience: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        info={"enum": ["candidate", "hiring_manager", "recruiter", "internal_hr"]},
    )
    sender: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    # Use 'meta' as Python attr to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON

    # Relationships
    case: Mapped["HelpCase"] = relationship("HelpCase", back_populates="messages")


class HelpAttachment(Base):
    """Attachment model for help case files."""

    __tablename__ = "help_attachments"

    attachment_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("help_cases.case_id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploader: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    # Use 'meta' as Python attr to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON

    # Relationships
    case: Mapped["HelpCase"] = relationship("HelpCase", back_populates="attachments")


class HelpAuditLog(Base):
    """Audit log model (append-only, immutable) for help cases."""

    __tablename__ = "help_audit_log"

    log_id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("help_cases.case_id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    actor_persona: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "Case Owner",
                "HR Admin",
                "HR Analyst",
                "Manager",
                "external",
                "hr_ops",
                "system",
                "ta",
            ]
        },
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=utc_now_str)
    changes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Use 'meta' as Python attr to avoid conflict with SQLAlchemy's reserved 'metadata'
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)  # JSON

    # Relationships
    case: Mapped["HelpCase"] = relationship("HelpCase", back_populates="audit_logs")
