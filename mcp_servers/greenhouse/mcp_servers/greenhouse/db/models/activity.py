"""Activity feed models for Greenhouse MCP Server.

API Reference:
- GET /candidates/{id}/activity_feed
"""

from typing import TYPE_CHECKING

from db.models.base import Base
from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.candidates import Candidate
    from db.models.users import User


class Note(Base):
    """Note on a candidate.

    Response in notes[]: { id, created_at, body, user, private, visibility }
    """

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String, default="public", info={"enum": ["admin_only", "public", "private"]}
    )
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate")
    user: Mapped["User | None"] = relationship("User")


class Email(Base):
    """Email sent to/from a candidate.

    Response in emails[]: { id, created_at, subject, body, to, from, cc, user }
    """

    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_address: Mapped[str | None] = mapped_column(String, nullable=True)
    from_address: Mapped[str | None] = mapped_column(String, nullable=True)
    cc_address: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate")
    user: Mapped["User | None"] = relationship("User")


class Activity(Base):
    """Activity feed event for a candidate.

    Response in activities[]: { id, created_at, subject, body, user }
    """

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    application_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate")
    user: Mapped["User | None"] = relationship("User")
