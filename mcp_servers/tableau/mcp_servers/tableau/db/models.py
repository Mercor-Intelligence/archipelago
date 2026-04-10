"""SQLAlchemy database models for tableau.

These are your database/ORM models (separate from Pydantic API models).
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now():
    """Get current UTC time."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class Site(Base):
    """Site model for multi-tenancy support."""

    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    content_url: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class User(Base):
    """User model for authentication and ownership.

    Note: Username (name) must be unique per site. Email is optional and not validated.
    Site role must be one of 8 valid roles (see VALID_SITE_ROLES in models.py).
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Site role: Must be one of VALID_SITE_ROLES (8 roles in Tableau API v3.0+)
    site_role: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (UniqueConstraint("site_id", "name", name="unique_user_per_site"),)


class Group(Base):
    """Group model for organizing users."""

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class GroupUser(Base):
    """Join table for many-to-many relationship between groups and users."""

    __tablename__ = "group_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    __table_args__ = (UniqueConstraint("group_id", "user_id", name="unique_group_user"),)


class Project(Base):
    """Project model for organizing workbooks and datasources with hierarchical structure."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    parent_project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Workbook(Base):
    """Workbook model for Tableau workbooks."""

    __tablename__ = "workbooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    file_reference: Mapped[str] = mapped_column(String(500), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class Datasource(Base):
    """Datasource model for data connections."""

    __tablename__ = "datasources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Connection types: postgres, mysql, excel, csv, etc.
    connection_type: Mapped[str] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text, default="")
    # For CSV uploads: name of the raw data table in SQLite (e.g., "csv_weather_data")
    table_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


class WorkbookDatasource(Base):
    """Join table for many-to-many relationship between workbooks and datasources."""

    __tablename__ = "workbook_datasources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workbook_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workbooks.id", ondelete="CASCADE"), nullable=False
    )
    datasource_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    __table_args__ = (UniqueConstraint("workbook_id", "datasource_id", name="unique_wb_ds"),)


class View(Base):
    """View model for Tableau views (sheets/dashboards within workbooks).

    Views represent individual visualizations within a workbook. They can be:
    - Worksheets: Single chart/table visualizations
    - Dashboards: Collections of worksheets
    - Stories: Narrative sequences of dashboards/worksheets

    For offline mode, sample_data_json stores mock CSV data and
    preview_image_path stores path to a preview image.
    """

    __tablename__ = "views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workbook_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workbooks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # View type: worksheet, dashboard, story
    sheet_type: Mapped[str] = mapped_column(String(50), default="worksheet")
    # For offline mode: JSON array of objects for mock CSV data
    # e.g., [{"region": "West", "sales": 50000}, {"region": "East", "sales": 45000}]
    sample_data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For offline mode: path to preview image file
    preview_image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Shelf configuration for drag-and-drop visualization (JSON)
    shelf_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Datasource this view queries against
    datasource_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("idx_view_workbook", "workbook_id"),
        Index("idx_view_site", "site_id"),
    )


class Permission(Base):
    """Permission model for resource access control.

    Note: This table intentionally lacks foreign key constraints on resource_id
    and grantee_id due to polymorphic references (resource_id can point to
    projects/workbooks/datasources; grantee_id can point to users/groups).
    SQLite/SQLAlchemy don't support conditional FKs based on type columns.

    Validation is enforced at the application layer in PermissionRepository
    (_validate_resource_exists, _validate_grantee_exists). Orphaned permissions
    from deleted resources are tolerated and can be cleaned up periodically.

    Alternative approaches (separate tables per resource type, triggers) were
    considered but not implemented for this schema design.
    """

    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Resource type: project/workbook/datasource
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # NOTE: No FK constraint - see class docstring for explanation
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Grantee type: user/group
    grantee_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # NOTE: No FK constraint - see class docstring for explanation
    grantee_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Capability: Read/Write/ChangePermissions
    capability: Mapped[str] = mapped_column(String(50), nullable=False)
    # Mode: Allow/Deny
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    __table_args__ = (
        Index("idx_resource", "resource_type", "resource_id"),
        Index("idx_grantee", "grantee_type", "grantee_id"),
    )
