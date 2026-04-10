"""Scorecard (interview feedback) models for Greenhouse MCP Server.

API Reference:
- GET /scorecards, GET /applications/{id}/scorecards
- POST /scorecards
"""

from typing import TYPE_CHECKING

from db.models.base import Base, TimestampMixin
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.applications import Application
    from db.models.candidates import Candidate
    from db.models.jobs import InterviewStep
    from db.models.users import User


class Scorecard(Base, TimestampMixin):
    """Interview feedback scorecard.

    Response: { id, interview, interview_step, candidate_id, application_id,
                interviewed_at, submitted_by, interviewer, submitted_at,
                overall_recommendation, attributes[], ratings, questions[] }
    """

    __tablename__ = "scorecards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id"), nullable=False, index=True
    )
    interview_step_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("interview_steps.id"), nullable=True
    )
    interview_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Maps to "interview" field
    interviewer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    submitted_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    overall_recommendation: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        info={"enum": ["definitely_not", "no", "mixed", "yes", "strong_yes", "no_decision"]},
    )
    interviewed_at: Mapped[str | None] = mapped_column(
        String, nullable=True, info={"date_after": "applications.applied_at"}
    )
    submitted_at: Mapped[str | None] = mapped_column(
        String, nullable=True, info={"date_after": "interviewed_at"}
    )

    # Relationships
    application: Mapped["Application"] = relationship("Application")
    candidate: Mapped["Candidate"] = relationship("Candidate")
    interview_step: Mapped["InterviewStep | None"] = relationship("InterviewStep")
    interviewer: Mapped["User | None"] = relationship("User", foreign_keys=[interviewer_id])
    submitted_by: Mapped["User | None"] = relationship("User", foreign_keys=[submitted_by_id])
    attributes: Mapped[list["ScorecardAttribute"]] = relationship(
        "ScorecardAttribute", back_populates="scorecard", cascade="all, delete-orphan"
    )
    questions: Mapped[list["ScorecardQuestion"]] = relationship(
        "ScorecardQuestion", back_populates="scorecard", cascade="all, delete-orphan"
    )


class ScorecardAttribute(Base):
    """Scorecard attribute (rating for a specific skill/qualification).

    Response: { name, type, note, rating }
    Also used to build the ratings summary object.
    """

    __tablename__ = "scorecard_attributes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scorecard_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(
        String, default="Skills", info={"enum": ["Skills", "Qualifications"]}
    )
    rating: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        info={"enum": ["definitely_not", "no", "mixed", "yes", "strong_yes", "no_decision"]},
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    scorecard: Mapped["Scorecard"] = relationship("Scorecard", back_populates="attributes")


class ScorecardQuestion(Base):
    """Scorecard interview question and answer.

    Response: { id, question, answer }
    """

    __tablename__ = "scorecard_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scorecard_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("scorecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    scorecard: Mapped["Scorecard"] = relationship("Scorecard", back_populates="questions")
