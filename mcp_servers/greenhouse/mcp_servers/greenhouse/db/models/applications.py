"""Application models for Greenhouse MCP Server.

API Reference:
- GET /applications, GET /applications/{id}
- POST /applications
"""

from typing import TYPE_CHECKING

from db.models.base import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.candidates import Candidate
    from db.models.jobboard import JobPost, ProspectPool, ProspectPoolStage
    from db.models.jobs import Job, JobStage
    from db.models.sources import Source
    from db.models.users import Department, Office, User


class RejectionReason(Base):
    """Reason for rejecting an application.

    Response: { id, name, type: { id, name } }
    """

    __tablename__ = "rejection_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # We rejected them, They rejected us, None


class Application(Base, TimestampMixin):
    """Job application linking a candidate to a job.

    Response: { id, candidate_id, prospect, applied_at, rejected_at, last_activity_at,
                location, source, credited_to, recruiter, coordinator,
                rejection_reason, rejection_details, jobs[], job_post_id, status,
                current_stage, answers[], prospect_detail }
    """

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("jobs.id"), nullable=True, index=True
    )  # NULL for prospects
    prospect: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(
        String,
        default="active",
        index=True,
        info={"enum": ["active", "rejected", "hired", "converted"]},
    )
    current_stage_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_stages.id"), nullable=True, index=True
    )
    source_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sources.id"), nullable=True)
    credited_to_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    recruiter_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    coordinator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    rejection_reason_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("rejection_reasons.id"), nullable=True
    )
    job_post_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_posts.id"), nullable=True
    )
    location_address: Mapped[str | None] = mapped_column(String, nullable=True)

    # Prospect-specific fields
    prospect_pool_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("prospect_pools.id"), nullable=True
    )
    prospect_stage_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("prospect_pool_stages.id"), nullable=True
    )
    prospect_owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    prospective_office_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("offices.id"), nullable=True
    )
    prospective_department_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("departments.id"), nullable=True
    )

    # Timestamps
    applied_at: Mapped[str | None] = mapped_column(
        String, nullable=True, info={"date_after": "jobs.opened_at"}
    )
    rejected_at: Mapped[str | None] = mapped_column(
        String, nullable=True, info={"date_after": "applied_at"}
    )
    hired_at: Mapped[str | None] = mapped_column(
        String, nullable=True, info={"date_after": "applied_at"}
    )
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate")
    job: Mapped["Job | None"] = relationship("Job")
    current_stage: Mapped["JobStage | None"] = relationship("JobStage")
    source: Mapped["Source | None"] = relationship("Source")
    credited_to: Mapped["User | None"] = relationship("User", foreign_keys=[credited_to_id])
    recruiter: Mapped["User | None"] = relationship("User", foreign_keys=[recruiter_id])
    coordinator: Mapped["User | None"] = relationship("User", foreign_keys=[coordinator_id])
    rejection_reason: Mapped["RejectionReason | None"] = relationship("RejectionReason")
    job_post: Mapped["JobPost | None"] = relationship("JobPost")
    prospect_pool: Mapped["ProspectPool | None"] = relationship("ProspectPool")
    prospect_stage: Mapped["ProspectPoolStage | None"] = relationship("ProspectPoolStage")
    prospect_owner: Mapped["User | None"] = relationship("User", foreign_keys=[prospect_owner_id])
    prospective_office: Mapped["Office | None"] = relationship("Office")
    prospective_department: Mapped["Department | None"] = relationship("Department")
    answers: Mapped[list["ApplicationAnswer"]] = relationship(
        "ApplicationAnswer", back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationAnswer(Base):
    """Answer to a job post application question.

    Response: { question, answer }
    """

    __tablename__ = "application_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    application: Mapped["Application"] = relationship("Application", back_populates="answers")
