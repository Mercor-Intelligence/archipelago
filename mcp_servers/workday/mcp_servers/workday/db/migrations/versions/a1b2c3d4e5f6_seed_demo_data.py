"""seed_demo_data (DISABLED - Empty database for production)

Revision ID: a1b2c3d4e5f6
Revises: 117922ffbfc5
Create Date: 2025-12-13 10:30:00.000000

NOTE: This migration previously seeded demo data but has been disabled
to provide an empty database. Tables are created but no data is inserted.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "117922ffbfc5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-op: Database tables remain empty for production use
    pass


def downgrade() -> None:
    # No-op: Nothing to remove
    pass
