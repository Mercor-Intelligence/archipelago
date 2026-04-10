#!/usr/bin/env python3
"""Seed the Greenhouse database from CSV files.

Usage:
    cd mcp_servers/greenhouse
    uv run python scripts/seed_db.py

Reads CSV files directly from STATE_LOCATION env var (default: /.apps_data/greenhouse).
Expects subdirectories: Activity/, Applications/, Candidates/, Jobs/, Scorecards/,
Sources/, Users-Departments-Offices/

This script:
1. Initializes the database (creates tables if needed)
2. Clears existing data
3. Loads all CSV files in dependency order
"""

import asyncio
import csv
import os
import re
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.models import (  # noqa: E402
    Activity,
    Application,
    ApplicationAnswer,
    Candidate,
    CandidateEmailAddress,
    CandidatePhoneNumber,
    Department,
    HiringTeam,
    InterviewKitQuestion,
    InterviewStep,
    Job,
    JobDepartment,
    JobOffice,
    JobStage,
    Note,
    Office,
    RejectionReason,
    Scorecard,
    ScorecardAttribute,
    ScorecardQuestion,
    Source,
    SourceType,
    User,
)
from db.session import dispose_engine, get_session, reset_db  # noqa: E402


def file_to_table(filename: str) -> str:
    """Convert filename to table name.

    Examples:
        Greenhouse - Job_Stages.csv -> job_stages
        Greenhouse__Applications.csv -> applications
        Greenhouse-Users.csv -> users
    """
    name = filename.split("/")[-1].replace(".csv", "")
    # Remove "Greenhouse" prefix with any separator (-, __, space-)
    name = re.sub(r"^Greenhouse[_\-\s]+", "", name)
    return name.lower()


# Map table names to SQLAlchemy models
TABLE_TO_MODEL = {
    "source_types": SourceType,
    "sources": Source,
    "rejection_reasons": RejectionReason,
    "users": User,
    "departments": Department,
    "offices": Office,
    "candidates": Candidate,
    "candidate_email_addresses": CandidateEmailAddress,
    "candidate_phone_numbers": CandidatePhoneNumber,
    "jobs": Job,
    "job_departments": JobDepartment,
    "job_offices": JobOffice,
    "job_stages": JobStage,
    "interview_steps": InterviewStep,
    "interview_kit_questions": InterviewKitQuestion,
    "hiring_team": HiringTeam,
    "applications": Application,
    "application_answers": ApplicationAnswer,
    "scorecards": Scorecard,
    "scorecard_attributes": ScorecardAttribute,
    "scorecard_questions": ScorecardQuestion,
    "activities": Activity,
    "notes": Note,
}

# Import order (respects foreign key dependencies)
IMPORT_ORDER = [
    "source_types",
    "sources",
    "rejection_reasons",
    "users",
    "departments",
    "offices",
    "candidates",
    "candidate_email_addresses",
    "candidate_phone_numbers",
    "jobs",
    "job_departments",
    "job_offices",
    "job_stages",
    "interview_steps",
    "interview_kit_questions",
    "hiring_team",
    "applications",
    "application_answers",
    "scorecards",
    "scorecard_attributes",
    "scorecard_questions",
    "activities",
    "notes",
]


def find_csv_files(data_dir: Path) -> dict[str, Path]:
    """Find all CSV files and map them to table names."""
    files = {}
    for csv_file in data_dir.rglob("*.csv"):
        table_name = file_to_table(str(csv_file))
        files[table_name] = csv_file
    return files


def parse_value(value: str, column_type) -> any:
    """Parse a CSV value based on the target column type.

    Args:
        value: The raw string value from CSV
        column_type: SQLAlchemy column type instance (e.g., Integer(), String())
    """
    from sqlalchemy import Boolean, Float, Integer

    if value == "" or value is None:
        return None

    # Check column type and parse accordingly
    if isinstance(column_type, Boolean):
        return value.lower() == "true"
    elif isinstance(column_type, Integer):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif isinstance(column_type, Float):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    else:
        # String and other types - preserve as-is (keeps leading zeros)
        return value


def load_csv(file_path: Path) -> list[dict]:
    """Load a CSV file and return list of row dicts (raw strings)."""
    rows = []
    with open(file_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Filter out None key (extra columns from trailing commas/inconsistent column counts)
            # and keep values as raw strings - parsing happens in seed_table
            cleaned_row = {k: v for k, v in row.items() if k is not None}
            rows.append(cleaned_row)
    return rows


async def seed_table(session, model, rows: list[dict], table_name: str) -> int:
    """Insert rows into a table."""
    if not rows:
        return 0

    # Build column name -> type mapping for type-aware parsing
    column_types = {c.key: c.type for c in model.__table__.columns}
    valid_columns = set(column_types.keys())

    # Handle self-referential FKs by nullifying them first, then updating
    self_ref_columns = {
        "jobs": "copied_from_id",
        "departments": "parent_id",
        "offices": "parent_id",
    }
    self_ref_col = self_ref_columns.get(table_name)
    deferred_updates = []

    count = 0
    for row in rows:
        # Filter to valid columns and parse values based on column type
        filtered_row = {}
        for k, v in row.items():
            if k in valid_columns:
                filtered_row[k] = parse_value(v, column_types[k])

        # Defer self-referential FK updates
        if self_ref_col and filtered_row.get(self_ref_col) is not None:
            deferred_updates.append((filtered_row["id"], filtered_row[self_ref_col]))
            filtered_row[self_ref_col] = None

        try:
            obj = model(**filtered_row)
            session.add(obj)
            count += 1
        except Exception as e:
            print(f"  Error inserting row into {table_name}: {e}")
            print(f"  Row: {filtered_row}")
            raise

    # Flush to ensure all rows exist before updating self-refs
    if deferred_updates:
        await session.flush()
        for row_id, ref_id in deferred_updates:
            from sqlalchemy import update

            stmt = update(model).where(model.id == row_id).values({self_ref_col: ref_id})
            await session.execute(stmt)

    return count


async def main():
    """Main entry point."""
    # Get data directory from STATE_LOCATION env var
    state_location = os.environ.get("STATE_LOCATION", "/.apps_data/greenhouse")
    data_path = Path(state_location)

    if not data_path.exists():
        print(f"No data directory found at: {data_path}")
        print("Skipping data seeding (no CSV files to import).")
        sys.exit(0)

    print(f"Looking for data in: {data_path}")
    print()

    # Find all CSV files
    csv_files = find_csv_files(data_path)

    if not csv_files:
        print("No CSV files found. Skipping data seeding.")
        sys.exit(0)

    print(f"Found {len(csv_files)} CSV files:")
    for table_name, file_path in sorted(csv_files.items()):
        print(f"  {table_name}: {file_path.name}")
    print()

    # Reset database (drop and recreate tables)
    print("Resetting database...")
    await reset_db()
    print("Database reset complete.")
    print()

    # Load tables in dependency order
    print("Loading data...")
    total_rows = 0

    for table_name in IMPORT_ORDER:
        if table_name not in csv_files:
            print(f"  {table_name}: SKIPPED (no source file)")
            continue

        if table_name not in TABLE_TO_MODEL:
            print(f"  {table_name}: SKIPPED (no model mapping)")
            continue

        file_path = csv_files[table_name]
        model = TABLE_TO_MODEL[table_name]

        rows = load_csv(file_path)

        # Use separate session per table to ensure commits happen in order
        async with get_session() as session:
            count = await seed_table(session, model, rows, table_name)
            await session.commit()

        total_rows += count
        print(f"  {table_name}: {count} rows")

    print()
    print(f"Done! Loaded {total_rows} total rows.")

    # Dispose engine to close all connections and allow clean exit
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
