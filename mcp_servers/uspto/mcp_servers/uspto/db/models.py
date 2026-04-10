"""SQLAlchemy models for the USPTO session-scoped SQLite database."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now():
    """Return the current timestamp for SQLite defaults."""

    return func.datetime("now")


class Base(DeclarativeBase):
    """Declarative base for all USPTO session tables."""

    pass


class Workspace(Base):
    """Session-scoped attorney research workspace."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())
    updated_at: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=_now(),
        onupdate=_now(),
    )

    saved_queries = relationship(
        "SavedQuery",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    snapshots = relationship(
        "ApplicationSnapshot",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    documents = relationship(
        "DocumentRecord",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    foreign_priority = relationship(
        "ForeignPriorityRecord",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    audit_logs = relationship(
        "AuditLog",
        back_populates="workspace",
        cascade="save-update",
    )

    __table_args__ = (
        Index("idx_workspaces_created", "created_at"),
        UniqueConstraint("name", name="uq_workspace_name"),
    )


class SavedQuery(Base):
    """Repeatable USPTO searches saved inside the workspace."""

    __tablename__ = "saved_queries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinned_results: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())
    last_run_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    workspace = relationship("Workspace", back_populates="saved_queries")

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_saved_query_workspace_name"),
        Index("idx_saved_queries_workspace", "workspace_id"),
    )


class ApplicationSnapshot(Base):
    """Versioned, immutable captures of USPTO application data."""

    __tablename__ = "application_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_number_text: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    invention_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    patent_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    patent_issue_date: Mapped[str | None] = mapped_column(Text, nullable=True)

    application_status_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_status_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_normalized_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_inventor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_applicant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_entity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    examiner_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_art_unit_number: Mapped[str | None] = mapped_column(Text, nullable=True)

    uspc_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    uspc_subclass: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpc_classifications: Mapped[str | None] = mapped_column(Text, nullable=True)

    entity_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")

    raw_uspto_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_claims_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())

    workspace = relationship("Workspace", back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "application_number_text",
            "version",
            name="uq_workspace_app_number_version",
        ),
        Index("idx_snapshots_workspace", "workspace_id"),
        Index("idx_snapshots_app_number", "application_number_text"),
        Index("idx_snapshots_created", "created_at"),
        Index("idx_snapshots_workspace_created", "workspace_id", "created_at"),
        Index(
            "idx_snapshots_version",
            "workspace_id",
            "application_number_text",
            "version",
        ),
    )


class DocumentRecord(Base):
    """Prosecution history documents stored per workspace."""

    __tablename__ = "document_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_number_text: Mapped[str] = mapped_column(String(64), nullable=False)

    document_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    document_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_code_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    official_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_options: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())

    workspace = relationship("Workspace", back_populates="documents")

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "application_number_text",
            "document_identifier",
            name="uq_workspace_app_document",
        ),
        CheckConstraint(
            "direction_category IN ('INCOMING', 'OUTGOING', 'INTERNAL')",
            name="ck_document_direction_category",
        ),
        Index("idx_documents_workspace", "workspace_id"),
        Index("idx_documents_app_number", "application_number_text"),
        Index("idx_documents_official_date", "official_date"),
    )


class ForeignPriorityRecord(Base):
    """Foreign priority claims captured for an application."""

    __tablename__ = "foreign_priority_records"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_number_text: Mapped[str] = mapped_column(String(64), nullable=False)
    foreign_application_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    foreign_filing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_office_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_office_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())

    workspace = relationship("Workspace", back_populates="foreign_priority")

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "application_number_text",
            "foreign_application_number",
            name="uq_workspace_app_foreign",
        ),
        Index("idx_foreign_priority_workspace", "workspace_id"),
        Index("idx_foreign_priority_app_number", "application_number_text"),
    )


class StatusCode(Base):
    """Cached USPTO status code reference data."""

    __tablename__ = "status_codes"

    status_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    status_description_text: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())
    version: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_status_codes_retrieved", "retrieved_at"),)


class SearchCache(Base):
    """Session-scoped search results cache."""

    __tablename__ = "search_cache"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[str | None] = mapped_column(Text, nullable=True)
    results: Mapped[str] = mapped_column(Text, nullable=False)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())

    __table_args__ = (
        Index("idx_search_cache_query", "query_text"),
        Index("idx_search_cache_cached_at", "cached_at"),
    )


class AuditLog(Base):
    """Append-only audit trail for the session."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_now())

    workspace = relationship("Workspace", back_populates="audit_logs")

    __table_args__ = (
        Index("idx_audit_workspace", "workspace_id"),
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_action", "action"),
    )


__all__ = [
    "AuditLog",
    "ApplicationSnapshot",
    "DocumentRecord",
    "ForeignPriorityRecord",
    "Base",
    "SavedQuery",
    "SearchCache",
    "StatusCode",
    "Workspace",
]
