"""add_visualization_columns

Revision ID: de67dc05a15b
Revises: 94e839e9e74b
Create Date: 2026-02-04 12:25:58.671395

Adds columns needed for drag-and-drop visualization:
- datasources.table_name: SQLite table name for CSV uploads
- views.shelf_config_json: Shelf layout configuration (JSON)
- views.datasource_id: FK linking view to its datasource
"""

from alembic import op
from sqlalchemy import Column, String, Text

# revision identifiers, used by Alembic.
revision = "de67dc05a15b"
down_revision = "94e839e9e74b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("datasources", Column("table_name", String(255), nullable=True))
    op.add_column("views", Column("shelf_config_json", Text, nullable=True))
    # Use batch mode for SQLite FK constraint support
    with op.batch_alter_table("views") as batch_op:
        batch_op.add_column(Column("datasource_id", String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_views_datasource_id",
            "datasources",
            ["datasource_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("views") as batch_op:
        batch_op.drop_constraint("fk_views_datasource_id", type_="foreignkey")
        batch_op.drop_column("datasource_id")
    op.drop_column("views", "shelf_config_json")
    op.drop_column("datasources", "table_name")
