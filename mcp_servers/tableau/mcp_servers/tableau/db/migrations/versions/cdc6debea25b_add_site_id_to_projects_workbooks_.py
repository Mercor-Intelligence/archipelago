"""add site_id to projects workbooks datasources

Revision ID: cdc6debea25b
Revises: 191f11dc3b4d
Create Date: 2025-11-24 10:00:00.000000

"""

from alembic import op
from sqlalchemy import Column, String

# revision identifiers, used by Alembic.
revision = "cdc6debea25b"
down_revision = "07c062e791ba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we use batch mode
    # which recreates the table with the new schema

    # Add site_id column to projects table
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(Column("site_id", String(length=36), nullable=True))
        batch_op.create_index("ix_projects_site_id", ["site_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_projects_site_id", "sites", ["site_id"], ["id"], ondelete="CASCADE"
        )

    # Add site_id column to workbooks table
    with op.batch_alter_table("workbooks", schema=None) as batch_op:
        batch_op.add_column(Column("site_id", String(length=36), nullable=True))
        batch_op.create_index("ix_workbooks_site_id", ["site_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_workbooks_site_id", "sites", ["site_id"], ["id"], ondelete="CASCADE"
        )

    # Add site_id column to datasources table
    with op.batch_alter_table("datasources", schema=None) as batch_op:
        batch_op.add_column(Column("site_id", String(length=36), nullable=True))
        batch_op.create_index("ix_datasources_site_id", ["site_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_datasources_site_id", "sites", ["site_id"], ["id"], ondelete="CASCADE"
        )

    # Note: Columns are initially nullable to allow existing data migration
    # In production, you would:
    # 1. Add column as nullable
    # 2. Populate site_id from related users or set a default
    # 3. Make column not nullable
    # For new databases, this can be done in one step


def downgrade() -> None:
    # Remove site_id from datasources
    with op.batch_alter_table("datasources", schema=None) as batch_op:
        batch_op.drop_index("ix_datasources_site_id")
        batch_op.drop_constraint("fk_datasources_site_id", type_="foreignkey")
        batch_op.drop_column("site_id")

    # Remove site_id from workbooks
    with op.batch_alter_table("workbooks", schema=None) as batch_op:
        batch_op.drop_index("ix_workbooks_site_id")
        batch_op.drop_constraint("fk_workbooks_site_id", type_="foreignkey")
        batch_op.drop_column("site_id")

    # Remove site_id from projects
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index("ix_projects_site_id")
        batch_op.drop_constraint("fk_projects_site_id", type_="foreignkey")
        batch_op.drop_column("site_id")
