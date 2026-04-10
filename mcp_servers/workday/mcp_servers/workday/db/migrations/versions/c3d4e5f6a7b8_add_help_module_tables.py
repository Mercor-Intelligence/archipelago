"""Add help module tables for workday_help integration.

Revision ID: c3d4e5f6a7b8
Revises: 117922ffbfc5
Create Date: 2026-01-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create help_cases table
    op.create_table(
        "help_cases",
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("case_type", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("candidate_identifier", sa.String(), nullable=False),
        sa.Column("due_date", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("case_id"),
        sa.UniqueConstraint("candidate_identifier"),
        sa.CheckConstraint(
            "case_type IN ('Pre-Onboarding')",
            name="help_check_case_type",
        ),
        sa.CheckConstraint(
            "status IN ('Open', 'Waiting', 'In Progress', 'Resolved', 'Closed')",
            name="help_check_status",
        ),
    )

    # Create help_timeline_events table
    op.create_table(
        "help_timeline_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["help_cases.case_id"],
            ondelete="RESTRICT",
        ),
    )

    # Create help_messages table
    op.create_table(
        "help_messages",
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("sender", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("message_id"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["help_cases.case_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "direction IN ('internal', 'inbound', 'outbound')",
            name="help_check_direction",
        ),
        sa.CheckConstraint(
            "audience IN ('candidate', 'hiring_manager', 'recruiter', 'internal_hr')",
            name="help_check_audience",
        ),
    )

    # Create help_attachments table
    op.create_table(
        "help_attachments",
        sa.Column("attachment_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("external_reference", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploader", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.String(), nullable=False),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("attachment_id"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["help_cases.case_id"],
            ondelete="CASCADE",
        ),
    )

    # Create help_audit_log table
    op.create_table(
        "help_audit_log",
        sa.Column("log_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("actor_persona", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("changes", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("log_id"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["help_cases.case_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "actor_persona IN ('Case Owner', 'HR Admin', 'Manager', 'HR Analyst')",
            name="help_check_actor_persona",
        ),
    )

    # Create indexes for query performance
    op.create_index("ix_help_cases_status", "help_cases", ["status"])
    op.create_index("ix_help_cases_owner", "help_cases", ["owner"])
    op.create_index("ix_help_timeline_events_case_id", "help_timeline_events", ["case_id"])
    op.create_index("ix_help_messages_case_id", "help_messages", ["case_id"])
    op.create_index("ix_help_attachments_case_id", "help_attachments", ["case_id"])
    op.create_index("ix_help_audit_log_case_id", "help_audit_log", ["case_id"])


def downgrade() -> None:
    # Drop indexes
    op.drop_index("ix_help_audit_log_case_id", table_name="help_audit_log")
    op.drop_index("ix_help_attachments_case_id", table_name="help_attachments")
    op.drop_index("ix_help_messages_case_id", table_name="help_messages")
    op.drop_index("ix_help_timeline_events_case_id", table_name="help_timeline_events")
    op.drop_index("ix_help_cases_owner", table_name="help_cases")
    op.drop_index("ix_help_cases_status", table_name="help_cases")

    # Drop tables in reverse order (due to foreign keys)
    op.drop_table("help_audit_log")
    op.drop_table("help_attachments")
    op.drop_table("help_messages")
    op.drop_table("help_timeline_events")
    op.drop_table("help_cases")
