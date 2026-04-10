"""add indexes for query performance

Revision ID: 117922ffbfc5
Revises: fe25e6d2a7ad
Create Date: 2025-12-12 14:23:28.306429

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "117922ffbfc5"
down_revision: str | Sequence[str] | None = "fe25e6d2a7ad"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Workers indexes for filtering and temporal queries
    op.execute("CREATE INDEX idx_workers_org ON workers(org_id)")
    op.execute("CREATE INDEX idx_workers_cost_center ON workers(cost_center_id)")
    op.execute("CREATE INDEX idx_workers_status ON workers(employment_status)")
    op.execute("CREATE INDEX idx_workers_effective_date ON workers(effective_date)")

    # Positions indexes for filtering
    op.execute("CREATE INDEX idx_positions_org ON positions(org_id)")
    op.execute("CREATE INDEX idx_positions_status ON positions(status)")

    # Movements indexes for history and reports
    op.execute("CREATE INDEX idx_movements_worker ON movements(worker_id)")
    op.execute("CREATE INDEX idx_movements_date ON movements(event_date)")
    op.execute("CREATE INDEX idx_movements_type ON movements(event_type)")


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes in reverse order
    op.execute("DROP INDEX idx_movements_type")
    op.execute("DROP INDEX idx_movements_date")
    op.execute("DROP INDEX idx_movements_worker")
    op.execute("DROP INDEX idx_positions_status")
    op.execute("DROP INDEX idx_positions_org")
    op.execute("DROP INDEX idx_workers_effective_date")
    op.execute("DROP INDEX idx_workers_status")
    op.execute("DROP INDEX idx_workers_cost_center")
    op.execute("DROP INDEX idx_workers_org")
