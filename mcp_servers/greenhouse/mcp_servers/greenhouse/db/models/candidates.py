"""Candidate models for Greenhouse MCP Server.

API Reference:
- GET /candidates, GET /candidates/{id}
- POST /candidates
"""

from typing import TYPE_CHECKING

from db.models.base import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from db.models.users import User


class Candidate(Base, TimestampMixin):
    """Candidate profile.

    Response: { id, first_name, last_name, company, title, is_private, can_email,
                photo_url, recruiter, coordinator, application_ids[], phone_numbers[],
                addresses[], email_addresses[], website_addresses[],
                social_media_addresses[], educations[], employments[], tags[],
                applications[], attachments[] }
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    last_name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    can_email: Mapped[bool] = mapped_column(Boolean, default=True)
    recruiter_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    coordinator_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    last_activity: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    recruiter: Mapped["User | None"] = relationship("User", foreign_keys=[recruiter_id])
    coordinator: Mapped["User | None"] = relationship("User", foreign_keys=[coordinator_id])
    phone_numbers: Mapped[list["CandidatePhoneNumber"]] = relationship(
        "CandidatePhoneNumber", back_populates="candidate", cascade="all, delete-orphan"
    )
    email_addresses: Mapped[list["CandidateEmailAddress"]] = relationship(
        "CandidateEmailAddress", back_populates="candidate", cascade="all, delete-orphan"
    )
    addresses: Mapped[list["CandidateAddress"]] = relationship(
        "CandidateAddress", back_populates="candidate", cascade="all, delete-orphan"
    )
    website_addresses: Mapped[list["CandidateWebsiteAddress"]] = relationship(
        "CandidateWebsiteAddress", back_populates="candidate", cascade="all, delete-orphan"
    )
    social_media_addresses: Mapped[list["CandidateSocialMediaAddress"]] = relationship(
        "CandidateSocialMediaAddress", back_populates="candidate", cascade="all, delete-orphan"
    )
    educations: Mapped[list["CandidateEducation"]] = relationship(
        "CandidateEducation", back_populates="candidate", cascade="all, delete-orphan"
    )
    employments: Mapped[list["CandidateEmployment"]] = relationship(
        "CandidateEmployment", back_populates="candidate", cascade="all, delete-orphan"
    )
    attachments: Mapped[list["CandidateAttachment"]] = relationship(
        "CandidateAttachment", back_populates="candidate", cascade="all, delete-orphan"
    )
    tags: Mapped[list["CandidateTag"]] = relationship(
        "CandidateTag", back_populates="candidate", cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        """Full name (first + last)."""
        return f"{self.first_name} {self.last_name}"


class CandidatePhoneNumber(Base):
    """Candidate phone number.

    Response: { value, type }
    """

    __tablename__ = "candidate_phone_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(
        String, default="mobile", info={"enum": ["home", "work", "mobile", "skype", "other"]}
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="phone_numbers")


class CandidateEmailAddress(Base):
    """Candidate email address.

    Response: { value, type }
    """

    __tablename__ = "candidate_email_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[str] = mapped_column(String, nullable=False, index=True)
    type: Mapped[str] = mapped_column(
        String, default="personal", info={"enum": ["personal", "work", "other"]}
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="email_addresses")


class CandidateAddress(Base):
    """Candidate physical address.

    Response: { value, type }
    """

    __tablename__ = "candidate_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(
        String, default="home", info={"enum": ["home", "work", "other"]}
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="addresses")


class CandidateWebsiteAddress(Base):
    """Candidate website address.

    Response: { value, type }
    """

    __tablename__ = "candidate_website_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(
        String,
        default="personal",
        info={"enum": ["personal", "company", "portfolio", "blog", "other"]},
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="website_addresses")


class CandidateSocialMediaAddress(Base):
    """Candidate social media address.

    Response: { value }
    """

    __tablename__ = "candidate_social_media_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str] = mapped_column(String, nullable=False)

    # Relationships
    candidate: Mapped["Candidate"] = relationship(
        "Candidate", back_populates="social_media_addresses"
    )


class CandidateEducation(Base):
    """Candidate education history.

    Response: { id, school_name, degree, discipline, start_date, end_date }
    """

    __tablename__ = "candidate_educations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    school_name: Mapped[str | None] = mapped_column(String, nullable=True)
    degree: Mapped[str | None] = mapped_column(String, nullable=True)
    discipline: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="educations")


class CandidateEmployment(Base):
    """Candidate employment history.

    Response: { id, company_name, title, start_date, end_date }
    """

    __tablename__ = "candidate_employments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    company_name: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="employments")


class CandidateAttachment(Base):
    """Candidate attachment (resume, cover letter, etc.).

    Response: { filename, url, type, created_at }
    """

    __tablename__ = "candidate_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        info={
            "enum": [
                "resume",
                "cover_letter",
                "admin_only",
                "offer_packet",
                "take_home_test",
                "other",
            ]
        },
    )
    created_at: Mapped[str] = mapped_column(String, nullable=True)

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="attachments")


class Tag(Base):
    """Tag for categorizing candidates.

    Response: { id, name }
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class CandidateTag(Base):
    """Candidate to Tag mapping (many-to-many).

    Supports: tags[] array in candidate response.
    """

    __tablename__ = "candidate_tags"

    candidate_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate", back_populates="tags")
    tag: Mapped["Tag"] = relationship("Tag")
