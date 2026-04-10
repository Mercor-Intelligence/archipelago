"""create a site

Revision ID: 07c062e791ba
Revises: 191f11dc3b4d
Create Date: 2025-11-20 22:00:30.446521

"""

from datetime import datetime, timezone

from alembic import op
from sqlalchemy import DateTime, String, column, table

# revision identifiers, used by Alembic.
revision = "07c062e791ba"
down_revision = "191f11dc3b4d"
branch_labels = None
depends_on = None

# Fixed UUID for the default site (deterministic for reproducibility)
DEFAULT_SITE_ID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"


def upgrade() -> None:
    # Insert a default site
    sites_table = table(
        "sites",
        column("id", String),
        column("name", String),
        column("content_url", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )

    op.bulk_insert(
        sites_table,
        [
            {
                "id": DEFAULT_SITE_ID,
                "name": "Default",
                "content_url": "https://somecontenturl.com",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        ],
    )


def downgrade() -> None:
    # Delete the default site by its deterministic ID
    op.execute(f"DELETE FROM sites WHERE id = '{DEFAULT_SITE_ID}'")
