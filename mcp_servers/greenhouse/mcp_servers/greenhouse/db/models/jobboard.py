"""Job Board and Prospect Pool models for Greenhouse MCP Server.

API Reference:
- Job Board: GET /boards/{token}/jobs, GET /boards/{token}/jobs/{id}
- Prospect Pools: GET /prospect_pools
- Education Reference: GET /boards/{token}/education/degrees, disciplines, schools
"""

from typing import TYPE_CHECKING

from db.models.base import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.jobs import Job


class JobPost(Base, TimestampMixin):
    """Job posting for the public job board.

    Response: { id, internal_job_id, title, location, absolute_url, language,
                content, departments[], offices[], metadata[], questions[] }
    """

    __tablename__ = "job_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    location_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # HTML job description
    absolute_url: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="en")
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_published_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    job: Mapped["Job"] = relationship("Job")
    questions: Mapped[list["JobPostQuestion"]] = relationship(
        "JobPostQuestion", back_populates="job_post", cascade="all, delete-orphan"
    )


class JobPostQuestion(Base):
    """Custom application question on a job post.

    Supports: questions[] array when ?questions=true
    """

    __tablename__ = "job_post_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_posts.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    # Field name identifier (e.g., "first_name", "question_12345")
    field_name: Mapped[str] = mapped_column(String, nullable=False)
    # Field type: input_text, input_file, input_hidden, textarea,
    # multi_value_single_select, multi_value_multi_select
    field_type: Mapped[str] = mapped_column(String, nullable=False)

    # Relationships
    job_post: Mapped["JobPost"] = relationship("JobPost", back_populates="questions")
    options: Mapped[list["JobPostQuestionOption"]] = relationship(
        "JobPostQuestionOption", back_populates="question", cascade="all, delete-orphan"
    )


class JobPostQuestionOption(Base):
    """Option for a select/multi-select job post question."""

    __tablename__ = "job_post_question_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("job_post_questions.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)

    # Relationships
    question: Mapped["JobPostQuestion"] = relationship("JobPostQuestion", back_populates="options")


class ProspectPool(Base):
    """Prospect pool for nurturing candidates.

    Response: { id, name, stages[] }
    """

    __tablename__ = "prospect_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=True)

    # Relationships
    stages: Mapped[list["ProspectPoolStage"]] = relationship(
        "ProspectPoolStage", back_populates="pool", cascade="all, delete-orphan"
    )


class ProspectPoolStage(Base):
    """Stage within a prospect pool.

    Response in stages[]: { id, name, priority }
    """

    __tablename__ = "prospect_pool_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prospect_pool_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("prospect_pools.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    pool: Mapped["ProspectPool"] = relationship("ProspectPool", back_populates="stages")


# Education Reference Data (for Job Board API)


class Degree(Base):
    """Education degree reference data.

    Response: { id, text }
    """

    __tablename__ = "degrees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)


class Discipline(Base):
    """Education discipline/field of study reference data.

    Response: { id, text }
    """

    __tablename__ = "disciplines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)


class School(Base):
    """School/university reference data.

    Response: { id, text }
    """

    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
