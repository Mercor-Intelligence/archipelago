"""Add additional indexes for help module query performance.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-01-09

Adds indexes for:
- Date range queries on help_cases.created_at
- Chronological ordering on help_timeline_events.created_at
- Direction filtering on help_messages
- Actor-based queries on help_audit_log
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add indexes for common query patterns
    op.create_index("ix_help_cases_created_at", "help_cases", ["created_at"])
    op.create_index("ix_help_timeline_events_created_at", "help_timeline_events", ["created_at"])
    op.create_index("ix_help_messages_direction", "help_messages", ["direction"])
    op.create_index("ix_help_audit_log_actor", "help_audit_log", ["actor"])


def downgrade() -> None:
    op.drop_index("ix_help_audit_log_actor", table_name="help_audit_log")
    op.drop_index("ix_help_messages_direction", table_name="help_messages")
    op.drop_index("ix_help_timeline_events_created_at", table_name="help_timeline_events")
    op.drop_index("ix_help_cases_created_at", table_name="help_cases")
