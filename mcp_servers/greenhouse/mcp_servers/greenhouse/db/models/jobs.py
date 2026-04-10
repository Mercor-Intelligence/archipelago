"""Job and Pipeline models for Greenhouse MCP Server.

API Reference:
- GET /jobs, GET /jobs/{id}
- GET /jobs/{id}/stages
"""

from typing import TYPE_CHECKING

from db.models.base import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.users import Department, Office, User


class Job(Base, TimestampMixin):
    """Job requisition.

    Response: { id, name, requisition_id, notes, confidential, status,
                opened_at, closed_at, is_template, copied_from_id,
                departments[], offices[], hiring_team, openings[] }
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    requisition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(
        String, default="draft", info={"enum": ["open", "closed", "draft"]}
    )
    opened_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    copied_from_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("jobs.id"), nullable=True
    )

    # Relationships
    departments: Mapped[list["JobDepartment"]] = relationship(
        "JobDepartment", back_populates="job", cascade="all, delete-orphan"
    )
    offices: Mapped[list["JobOffice"]] = relationship(
        "JobOffice", back_populates="job", cascade="all, delete-orphan"
    )
    hiring_team: Mapped[list["HiringTeam"]] = relationship(
        "HiringTeam", back_populates="job", cascade="all, delete-orphan"
    )
    stages: Mapped[list["JobStage"]] = relationship(
        "JobStage", back_populates="job", cascade="all, delete-orphan", order_by="JobStage.priority"
    )
    openings: Mapped[list["JobOpening"]] = relationship(
        "JobOpening", back_populates="job", cascade="all, delete-orphan"
    )
    copied_from: Mapped["Job | None"] = relationship("Job", remote_side=[id])


class JobDepartment(Base):
    """Job to Department mapping (many-to-many).

    Supports: departments[] array in job response.
    """

    __tablename__ = "job_departments"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="departments")
    department: Mapped["Department"] = relationship("Department")


class JobOffice(Base):
    """Job to Office mapping (many-to-many).

    Supports: offices[] array in job response.
    """

    __tablename__ = "job_offices"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    office_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("offices.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="offices")
    office: Mapped["Office"] = relationship("Office")


class HiringTeam(Base):
    """Hiring team member for a job.

    Supports: hiring_team.hiring_managers[], hiring_team.recruiters[],
              hiring_team.coordinators[]
    """

    __tablename__ = "hiring_team"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={"enum": ["hiring_manager", "recruiter", "coordinator", "sourcer"]},
    )
    responsible: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, nullable=True)

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="hiring_team")
    user: Mapped["User"] = relationship("User")


class JobStage(Base, TimestampMixin):
    """Pipeline stage for a job.

    Response: { id, name, job_id, priority, active, interviews[] }
    """

    __tablename__ = "job_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # Lower = earlier in pipeline
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="stages")
    interview_steps: Mapped[list["InterviewStep"]] = relationship(
        "InterviewStep", back_populates="stage", cascade="all, delete-orphan"
    )


class InterviewStep(Base):
    """Interview step within a stage.

    Response: { id, name, schedulable, estimated_minutes,
                default_interviewer_users[], interview_kit }
    """

    __tablename__ = "interview_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_stage_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_stages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    schedulable: Mapped[bool] = mapped_column(Boolean, default=True)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    interview_kit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interview_kit_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=True)

    # Relationships
    stage: Mapped["JobStage"] = relationship("JobStage", back_populates="interview_steps")
    kit_questions: Mapped[list["InterviewKitQuestion"]] = relationship(
        "InterviewKitQuestion", back_populates="interview_step", cascade="all, delete-orphan"
    )
    default_interviewers: Mapped[list["InterviewStepDefaultInterviewer"]] = relationship(
        "InterviewStepDefaultInterviewer",
        back_populates="interview_step",
        cascade="all, delete-orphan",
    )


class InterviewKitQuestion(Base):
    """Question in an interview kit.

    Supports: interview_kit.questions[] array.
    """

    __tablename__ = "interview_kit_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interview_step_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("interview_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    interview_step: Mapped["InterviewStep"] = relationship(
        "InterviewStep", back_populates="kit_questions"
    )


class InterviewStepDefaultInterviewer(Base):
    """Default interviewer for an interview step.

    Supports: default_interviewer_users[] array.
    """

    __tablename__ = "interview_step_default_interviewers"

    interview_step_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("interview_steps.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    interview_step: Mapped["InterviewStep"] = relationship(
        "InterviewStep", back_populates="default_interviewers"
    )
    user: Mapped["User"] = relationship("User")


class JobOpening(Base):
    """Job opening (headcount slot).

    Response: { id, opening_id, status, opened_at, closed_at,
                application_id, close_reason }
    """

    __tablename__ = "job_openings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opening_id: Mapped[str | None] = mapped_column(String, nullable=True)  # External ID
    status: Mapped[str] = mapped_column(String, default="open", info={"enum": ["open", "closed"]})
    opened_at: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    application_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("applications.id"), nullable=True
    )
    close_reason_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_reason_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="openings")
