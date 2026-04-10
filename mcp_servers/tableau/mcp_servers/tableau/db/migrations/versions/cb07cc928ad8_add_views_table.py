"""add_views_table

Revision ID: cb07cc928ad8
Revises: cdc6debea25b
Create Date: 2025-11-25 15:43:41.470804

"""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, Text

# revision identifiers, used by Alembic.
revision = "cb07cc928ad8"
down_revision = "cdc6debea25b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "views",
        Column("id", String(36), primary_key=True),
        Column(
            "site_id",
            String(36),
            ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column(
            "workbook_id",
            String(36),
            ForeignKey("workbooks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        Column("name", String(255), nullable=False),
        Column("content_url", String(500), nullable=True),
        Column("sheet_type", String(50), default="worksheet"),
        Column("sample_data_json", Text, nullable=True),
        Column("preview_image_path", String(500), nullable=True),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
    )
    op.create_index("idx_view_workbook", "views", ["workbook_id"])
    op.create_index("idx_view_site", "views", ["site_id"])


def downgrade() -> None:
    op.drop_index("idx_view_site", table_name="views")
    op.drop_index("idx_view_workbook", table_name="views")
    op.drop_table("views")
