"""seed_demo_data_with_weather_view

Revision ID: 94e839e9e74b
Revises: cb07cc928ad8
Create Date: 2025-11-26 00:02:54.695022

Seeds demo data including:
- A demo user (owner for project/workbook)
- A demo project
- A demo workbook
- A demo view with weekly weather sample data
"""

import json
from datetime import datetime, timezone

from alembic import op
from sqlalchemy import DateTime, String, column, table

# revision identifiers, used by Alembic.
revision = "94e839e9e74b"
down_revision = "cb07cc928ad8"
branch_labels = None
depends_on = None

# Reference the default site from previous migration
DEFAULT_SITE_ID = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"

# Fixed UUIDs for demo data (deterministic for reproducibility)
# Note: UUIDs are valid v4 format with 4th segment starting with [89ab]
DEMO_USER_ID = "b1c2d3e4-f5a6-4b5c-8d9e-0f1a2b3c4d5e"
DEMO_PROJECT_ID = "c2d3e4f5-a6b7-4c5d-9e0f-1a2b3c4d5e6f"
DEMO_WORKBOOK_ID = "d3e4f5a6-b7c8-4d5e-8f1a-2b3c4d5e6f7a"
DEMO_VIEW_ID = "e4f5a6b7-c8d9-4e5f-8a2b-3c4d5e6f7a8b"

# Weather sample data
WEATHER_DATA = [
    {
        "Day": "Sunday",
        "Snow (in)": 2.5,
        "Max Temp": 32,
        "Min Temp": 18,
        "Max Wind": 25,
        "Min Wind": 8,
    },
    {
        "Day": "Monday",
        "Snow (in)": 0.0,
        "Max Temp": 35,
        "Min Temp": 22,
        "Max Wind": 15,
        "Min Wind": 5,
    },
    {
        "Day": "Tuesday",
        "Snow (in)": 4.2,
        "Max Temp": 28,
        "Min Temp": 15,
        "Max Wind": 35,
        "Min Wind": 12,
    },
    {
        "Day": "Wednesday",
        "Snow (in)": 1.8,
        "Max Temp": 30,
        "Min Temp": 20,
        "Max Wind": 20,
        "Min Wind": 7,
    },
    {
        "Day": "Thursday",
        "Snow (in)": 0.0,
        "Max Temp": 38,
        "Min Temp": 25,
        "Max Wind": 12,
        "Min Wind": 3,
    },
    {
        "Day": "Friday",
        "Snow (in)": 0.5,
        "Max Temp": 33,
        "Min Temp": 21,
        "Max Wind": 18,
        "Min Wind": 6,
    },
    {
        "Day": "Saturday",
        "Snow (in)": 3.0,
        "Max Temp": 29,
        "Min Temp": 17,
        "Max Wind": 28,
        "Min Wind": 10,
    },
]


def upgrade() -> None:
    now = datetime.now(timezone.utc)

    # 1. Create demo user (required as owner for project and workbook)
    users_table = table(
        "users",
        column("id", String),
        column("site_id", String),
        column("name", String),
        column("email", String),
        column("site_role", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )

    op.bulk_insert(
        users_table,
        [
            {
                "id": DEMO_USER_ID,
                "site_id": DEFAULT_SITE_ID,
                "name": "Demo User",
                "email": "demo@example.com",
                "site_role": "Creator",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    # 2. Create demo project (required for workbook)
    projects_table = table(
        "projects",
        column("id", String),
        column("site_id", String),
        column("name", String),
        column("description", String),
        column("parent_project_id", String),
        column("owner_id", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )

    op.bulk_insert(
        projects_table,
        [
            {
                "id": DEMO_PROJECT_ID,
                "site_id": DEFAULT_SITE_ID,
                "name": "Weather Analytics",
                "description": "Demo project containing weather analysis dashboards",
                "parent_project_id": None,
                "owner_id": DEMO_USER_ID,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    # 3. Create demo workbook (required for view)
    workbooks_table = table(
        "workbooks",
        column("id", String),
        column("site_id", String),
        column("name", String),
        column("project_id", String),
        column("owner_id", String),
        column("file_reference", String),
        column("description", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )

    op.bulk_insert(
        workbooks_table,
        [
            {
                "id": DEMO_WORKBOOK_ID,
                "site_id": DEFAULT_SITE_ID,
                "name": "Weekly Weather Report",
                "project_id": DEMO_PROJECT_ID,
                "owner_id": DEMO_USER_ID,
                "file_reference": None,
                "description": "Weekly weather data visualization",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    # 4. Create demo view with weather sample data
    views_table = table(
        "views",
        column("id", String),
        column("site_id", String),
        column("workbook_id", String),
        column("name", String),
        column("content_url", String),
        column("sheet_type", String),
        column("sample_data_json", String),
        column("preview_image_path", String),
        column("created_at", DateTime),
        column("updated_at", DateTime),
    )

    op.bulk_insert(
        views_table,
        [
            {
                "id": DEMO_VIEW_ID,
                "site_id": DEFAULT_SITE_ID,
                "workbook_id": DEMO_WORKBOOK_ID,
                "name": "Weekly Weather Overview",
                "content_url": "weather-analytics/weekly-weather-report/weekly-weather-overview",
                "sheet_type": "dashboard",
                "sample_data_json": json.dumps(WEATHER_DATA),
                "preview_image_path": None,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    # Delete in reverse order to respect foreign key constraints
    op.execute(f"DELETE FROM views WHERE id = '{DEMO_VIEW_ID}'")
    op.execute(f"DELETE FROM workbooks WHERE id = '{DEMO_WORKBOOK_ID}'")
    op.execute(f"DELETE FROM projects WHERE id = '{DEMO_PROJECT_ID}'")
    op.execute(f"DELETE FROM users WHERE id = '{DEMO_USER_ID}'")
