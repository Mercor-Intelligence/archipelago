"""User, Department, and Office models for Greenhouse MCP Server.

API Reference:
- GET /users, GET /users/{id}
- GET /departments, GET /departments/{id}
- GET /offices, GET /offices/{id}
"""

from db.models.base import Base, TimestampMixin
from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship


class User(Base, TimestampMixin):
    """Greenhouse system user (interviewer, recruiter, etc.).

    Response: { id, name, first_name, last_name, primary_email_address,
                emails[], employee_id, disabled, site_admin,
                linked_candidate_ids[], offices[], departments[] }
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False)
    last_name: Mapped[str] = mapped_column(String, nullable=False)
    primary_email_address: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    site_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    emails: Mapped[list["UserEmail"]] = relationship(
        "UserEmail", back_populates="user", cascade="all, delete-orphan"
    )
    departments: Mapped[list["UserDepartment"]] = relationship(
        "UserDepartment", back_populates="user", cascade="all, delete-orphan"
    )
    offices: Mapped[list["UserOffice"]] = relationship(
        "UserOffice", back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def name(self) -> str:
        """Full name (first + last)."""
        return f"{self.first_name} {self.last_name}"


class UserEmail(Base):
    """Additional email addresses for a user.

    Supports: emails[] array in user response.
    """

    __tablename__ = "user_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="emails")


class Department(Base, TimestampMixin):
    """Department in the organization hierarchy.

    Response: { id, name, parent_id, child_ids[], external_id }
    Note: child_ids[] is computed dynamically from parent_id relationships.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("departments.id"), nullable=True, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    parent: Mapped["Department | None"] = relationship(
        "Department", remote_side=[id], backref="children"
    )


class Office(Base, TimestampMixin):
    """Office location.

    Response: { id, name, location: { name }, primary_contact_user_id,
                parent_id, child_ids[], external_id }
    """

    __tablename__ = "offices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    location_name: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("offices.id"), nullable=True, index=True
    )
    primary_contact_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    parent: Mapped["Office | None"] = relationship("Office", remote_side=[id], backref="children")
    primary_contact: Mapped["User | None"] = relationship("User")


class UserDepartment(Base):
    """User to Department mapping (many-to-many).

    Supports: departments[] array in user response.
    """

    __tablename__ = "user_departments"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="departments")
    department: Mapped["Department"] = relationship("Department")


class UserOffice(Base):
    """User to Office mapping (many-to-many).

    Supports: offices[] array in user response.
    """

    __tablename__ = "user_offices"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    office_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("offices.id", ondelete="CASCADE"), primary_key=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="offices")
    office: Mapped["Office"] = relationship("Office")
