import os

# ============================================================================
# SQL_EXEC Storage Configuration
# ============================================================================

STATE_LOCATION = os.getenv("STATE_LOCATION", "/.apps_data/sql_execution")
DB_PATH = os.path.join(STATE_LOCATION, "database.db")
