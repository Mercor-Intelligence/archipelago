"""Data layer initialization - builds mock data structures for Looker API responses.

DuckDB is the SINGLE SOURCE OF TRUTH for user data.

This module initializes the data layer at MCP server startup by:
1. Copying bundled DuckDB to STATE_LOCATION (if not already there)
2. Loading bundled LookML files (pre-built, shipped with repo)
3. Querying DuckDB for user tables (SHOW TABLES, DESCRIBE)
4. Generating fields from DuckDB schema (dimensions + measures)
5. Building in-memory mock data structures for Looker API responses

Data Ingestion Paths (all write to DuckDB):
- RL Studio: [tasks.populate] loads CSVs from S3 mount into DuckDB
- Import World: UI downloads zip, extracts CSVs, loads into DuckDB
- CSV Upload: UI uploads CSV, loads into DuckDB

Key design decisions:
- Bundled DuckDB is pre-built and committed to repo (data/offline.duckdb) - READ ONLY
- At startup, bundled DuckDB is copied to STATE_LOCATION for runtime use
- All reads/writes go to the STATE_LOCATION copy (ephemeral, user-modifiable)
- Bundled LookML is pre-built and shipped with repo (data/lookml/*.view.lkml)
- User tables are discovered by querying DuckDB (not by scanning CSV files)

This ensures:
- Fast startup (no CSV parsing, just DuckDB queries)
- No accidental commits of user data (bundled DB is never modified)
- User uploads work correctly (written to DuckDB)
- No dependency on CSV files at runtime (DuckDB is the source of truth)
"""

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger


def _quote_identifier(name: str) -> str:
    """Safely quote a SQL identifier by escaping embedded double quotes."""
    from scripts.build_duckdb import _quote_ident

    return _quote_ident(name)


# Module-level state
_initialized = False
_lookml_models: list = []  # LookMLModel objects for API responses
_lookml_explores: dict = {}  # (model, explore) -> ExploreResponse
_session_temp_dir: Path | None = None  # Temp dir for local dev (created fresh each run)


def get_state_location() -> Path | None:
    """Get STATE_LOCATION path if set and exists."""
    state_loc = os.environ.get("STATE_LOCATION")
    if state_loc:
        path = Path(state_loc)
        if path.exists():
            return path
    return None


def get_user_csv_dir() -> Path | None:
    """Get directory containing user-uploaded CSVs.

    Returns STATE_LOCATION if set, otherwise the session temp dir.
    For local dev, this returns a fresh temp dir each run (no persisted data).
    """
    state_loc = os.environ.get("STATE_LOCATION")
    if state_loc:
        path = Path(state_loc)
        if path.exists():
            return path
    # Fall back to session temp dir (always empty on fresh run)
    return _get_session_temp_dir()


def get_bundled_duckdb_path() -> Path:
    """Get path to the bundled (read-only) DuckDB shipped with the repo."""
    return Path(__file__).parent / "data" / "offline.duckdb"


def _get_session_temp_dir() -> Path:
    """Get or create a temp directory for this session (local dev only).

    This ensures each server run starts fresh with no persisted user data.
    The temp dir is created once per process and reused for the session.
    """
    global _session_temp_dir
    if _session_temp_dir is None:
        _session_temp_dir = Path(tempfile.mkdtemp(prefix="looker_session_"))
        logger.info(f"Created session temp dir: {_session_temp_dir}")
    return _session_temp_dir


def get_runtime_duckdb_path() -> Path:
    """Get path to the runtime (writable) DuckDB for this session.

    This is where all reads and writes should go at runtime.
    The bundled DuckDB is copied here on first startup.

    Returns:
        Path to runtime DuckDB in STATE_LOCATION or temp dir (local dev)
    """
    # Check STATE_LOCATION first (production)
    state_loc = os.environ.get("STATE_LOCATION")
    if state_loc:
        return Path(state_loc) / "offline.duckdb"

    # Fall back to session-specific temp dir (local development)
    # This ensures each server run starts fresh with no persisted data
    return _get_session_temp_dir() / "offline.duckdb"


def _ensure_runtime_duckdb() -> Path:
    """Ensure the runtime DuckDB exists, copying from bundled if needed.

    This is called at startup to copy the bundled DuckDB to the writable
    location (STATE_LOCATION or .apps_data/).

    Returns:
        Path to the runtime DuckDB
    """
    # Log STATE_LOCATION for debugging session isolation issues
    state_loc = os.environ.get("STATE_LOCATION")
    logger.info(f"STATE_LOCATION env var: {state_loc!r}")

    bundled_path = get_bundled_duckdb_path()
    runtime_path = get_runtime_duckdb_path()
    logger.info(f"Runtime DuckDB path: {runtime_path}")

    # If runtime DB already exists, use it (preserves user uploads)
    if runtime_path.exists():
        logger.info(f"Using existing runtime DuckDB: {runtime_path}")
        return runtime_path

    # Ensure parent directory exists
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy bundled DB to runtime location, or create empty DB if no bundled data
    if bundled_path.exists():
        logger.info(f"Copying bundled DuckDB to runtime location: {runtime_path}")
        start = time.time()
        shutil.copy2(bundled_path, runtime_path)
        elapsed = time.time() - start
        logger.info(f"Copied bundled DuckDB in {elapsed:.2f}s")
    else:
        logger.info("No bundled DuckDB found, creating empty database")
        import duckdb

        conn = duckdb.connect(str(runtime_path))
        conn.close()

    return runtime_path


def initialize_data_layer(force_reload: bool = False) -> bool:
    """Initialize the data layer at MCP server startup.

    DuckDB is the single source of truth for user data:
    1. Copy bundled DuckDB to STATE_LOCATION (if not already there)
    2. Load bundled LookML files (for bundled views)
    3. Query DuckDB for user tables and generate fields from schema

    Args:
        force_reload: If True, reload even if already initialized

    Returns:
        True if any data was loaded, False otherwise
    """
    global _initialized, _lookml_models, _lookml_explores

    if _initialized and not force_reload:
        return bool(_lookml_models)

    if force_reload:
        # Reset state before reloading to handle partial failures correctly
        _initialized = False
        _lookml_models = []
        _lookml_explores = {}
        # Clear seeded data cache
        try:
            from duckdb_query_executor import clear_cache

            clear_cache()
        except ImportError:
            pass

    # Step 0: Ensure runtime DuckDB exists (copy from bundled if needed)
    _ensure_runtime_duckdb()

    # Bundled LookML directory (read-only, shared safely)
    bundled_lookml_dir = Path(__file__).parent / "data" / "lookml"

    # Step 1: Collect view names from pre-built bundled LookML files
    bundled_views = []
    if bundled_lookml_dir.exists():
        for lkml_file in bundled_lookml_dir.glob("*.view.lkml"):
            view_name = lkml_file.stem.replace(".view", "")
            bundled_views.append(view_name)

    if bundled_views:
        logger.info(f"Found {len(bundled_views)} bundled LookML view(s)")

    # Step 2: Query DuckDB for user tables (DuckDB is the single source of truth)
    # This replaces CSV file scanning - we now discover user data from DuckDB directly
    bundled_view_set = set(bundled_views)
    user_views, override_views = _get_user_tables_from_duckdb(bundled_view_set)
    user_fields: dict = {}  # view_name -> ExploreFields

    # Generate fields from DuckDB schema for user tables AND override tables
    # Override tables are bundled tables where user uploaded data with same name
    # User data takes precedence, so we regenerate schema from DuckDB
    tables_to_generate = user_views + override_views
    for table_name in tables_to_generate:
        try:
            fields = _generate_fields_from_duckdb_schema(table_name)
            user_fields[table_name] = fields
        except Exception as e:
            logger.warning(f"Failed to generate fields for {table_name}: {e}")

    if user_views:
        logger.info(f"Discovered {len(user_views)} user table(s) from DuckDB: {user_views}")
    if override_views:
        logger.info(f"User overriding {len(override_views)} bundled table(s): {override_views}")

    # Step 3: Build mock data structures
    # Combine user + bundled views, user takes priority for duplicates
    all_views = list(dict.fromkeys(user_views + bundled_views))

    # Override tables have user data that should take precedence over bundled LookML,
    # so remove them from the bundled set so _build_mock_data uses user-generated fields.
    bundled_view_set -= set(override_views)

    if all_views:
        models, explores = _build_mock_data(
            bundled_lookml_dir, all_views, bundled_view_set, user_fields
        )
        _lookml_models = models
        _lookml_explores = explores
        logger.info(f"Built {len(models)} model(s), {len(explores)} explore(s)")

    _initialized = True
    return bool(_lookml_models)


def _populate_duckdb(user_csv_dir: Path) -> int:
    """Add user-uploaded CSVs to the runtime DuckDB.

    Args:
        user_csv_dir: Directory containing user CSV files

    Returns:
        Number of tables added/updated
    """
    from scripts.build_duckdb import add_csvs_to_db

    db_path = get_runtime_duckdb_path()
    if not db_path.exists():
        logger.error(f"Runtime database not found at {db_path}")
        return 0

    max_retries = 3
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            return add_csvs_to_db(db_path, user_csv_dir)
        except Exception as e:
            error_msg = str(e).lower()
            if "lock" in error_msg or "busy" in error_msg or "concurrent" in error_msg:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"DuckDB write failed (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {retry_delay}s: {e}"
                    )
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
            raise


def _get_user_tables_from_duckdb(bundled_table_names: set[str]) -> tuple[list[str], list[str]]:
    """Get tables from DuckDB that need schema regeneration.

    This queries DuckDB directly with SHOW TABLES to discover what tables exist.
    Returns two lists:
    - user_tables: Tables not in bundled (new user uploads)
    - override_tables: Tables in both DuckDB and bundled (user overriding bundled)

    For override_tables, user data takes precedence over bundled LookML.

    Args:
        bundled_table_names: Set of table names that are bundled (pre-built)

    Returns:
        Tuple of (user_tables, override_tables)
    """
    import duckdb

    db_path = get_runtime_duckdb_path()
    if not db_path.exists():
        logger.warning(f"Runtime DuckDB not found at {db_path}")
        return [], []

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            result = conn.execute("SHOW TABLES").fetchall()
            all_tables = [row[0] for row in result]

            # Separate into new user tables and override tables
            user_tables = [t for t in all_tables if t not in bundled_table_names]

            # A bundled table is only an "override" if the user uploaded a CSV
            # with the same name. Without this check, ALL bundled tables appear
            # as overrides because the bundled DuckDB is copied to runtime.
            user_csv_dir = get_user_csv_dir()
            user_csv_names: set[str] = set()
            if user_csv_dir:
                user_csv_names = {f.stem for f in user_csv_dir.rglob("*.csv")}
            override_tables = [
                t for t in all_tables if t in bundled_table_names and t in user_csv_names
            ]

            logger.info(
                f"Found {len(all_tables)} total tables: "
                f"{len(user_tables)} user-only, {len(override_tables)} overriding bundled"
            )
            return user_tables, override_tables
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to query DuckDB tables: {e}")
        return [], []


def _generate_fields_from_duckdb_schema(table_name: str):
    """Generate ExploreFields from DuckDB table schema.

    This queries DuckDB with DESCRIBE to get column info, then generates
    dimensions and measures based on column types.

    Args:
        table_name: Name of the table in DuckDB

    Returns:
        ExploreFields with dimensions and measures
    """
    import duckdb
    from lookml_generator import (
        _ID_EXACT_MATCHES,
        _ID_SUFFIXES,
        _field_name_to_label,
        _generate_numeric_measures,
    )
    from models import ExploreFields, LookMLField

    db_path = get_runtime_duckdb_path()
    if not db_path.exists():
        logger.warning(f"Runtime DuckDB not found at {db_path}")
        return ExploreFields()

    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            # Get column info: column_name, column_type, null, key, default, extra
            result = conn.execute(f"DESCRIBE {_quote_identifier(table_name)}").fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to describe table {table_name}: {e}")
        return ExploreFields()

    dimensions = []
    measures = []

    # Map DuckDB types to LookML types
    def duckdb_type_to_lookml(dtype: str) -> str:
        dtype_upper = dtype.upper()
        if any(
            t in dtype_upper
            for t in (
                "INT",
                "BIGINT",
                "SMALLINT",
                "TINYINT",
                "DOUBLE",
                "FLOAT",
                "DECIMAL",
                "NUMERIC",
                "REAL",
            )
        ):
            return "number"
        if "BOOL" in dtype_upper:
            return "yesno"
        if "DATE" in dtype_upper and "TIME" not in dtype_upper:
            return "date"
        if "TIME" in dtype_upper or "TIMESTAMP" in dtype_upper:
            return "datetime"
        return "string"

    for row in result:
        col_name = row[0]
        col_type = row[1]

        lookml_type = duckdb_type_to_lookml(col_type)

        # Create dimension
        dim = LookMLField(
            name=f"{table_name}.{col_name}",
            label=_field_name_to_label(col_name),
            type=lookml_type,
            description=None,
            view=table_name,
            hidden=False,
        )
        dimensions.append(dim)

        # Generate measures for numeric fields (skip ID/count fields)
        col_lower = col_name.lower()
        is_id_field = col_lower in _ID_EXACT_MATCHES or col_lower.endswith(_ID_SUFFIXES)
        is_count_field = col_lower == "count" or col_lower.endswith("_count")

        if lookml_type == "number" and not is_id_field and not is_count_field:
            # Generate sum, avg, min, max measures
            for measure_info in _generate_numeric_measures(col_name, col_name):
                measures.append(
                    LookMLField(
                        name=f"{table_name}.{measure_info.name}",
                        label=measure_info.label,
                        type=measure_info.type,
                        description=measure_info.description,
                        view=table_name,
                        hidden=False,
                    )
                )

    # Always add a count measure
    measures.append(
        LookMLField(
            name=f"{table_name}.count",
            label="Count",
            type="count",
            description="Count of records",
            view=table_name,
            hidden=False,
        )
    )

    logger.info(
        f"Generated fields for {table_name}: {len(dimensions)} dimensions, {len(measures)} measures"
    )
    return ExploreFields(dimensions=dimensions, measures=measures)


def _build_mock_data(
    bundled_lookml_dir: Path,
    view_names: list[str],
    bundled_view_names: set[str],
    user_fields: dict | None = None,
) -> tuple[list, dict]:
    """Build mock data structures from bundled LookML and pre-parsed user fields.

    Args:
        bundled_lookml_dir: Directory containing bundled (read-only) LookML files
        view_names: List of view names to build
        bundled_view_names: Set of view names that are bundled (not user-uploaded)
        user_fields: Pre-parsed field metadata for user-uploaded CSVs (view_name -> ExploreFields)
    """
    from models import (
        ExploreFields,
        ExploreResponse,
        LookMLModel,
        LookmlModelNavExplore,
    )

    explores = {}
    explore_list = []
    user_fields = user_fields or {}

    for view_name in view_names:
        # For user views, use pre-parsed fields; for bundled views, read from LookML files
        if view_name in user_fields:
            fields = user_fields[view_name]
        elif bundled_lookml_dir.exists():
            bundled_view_file = bundled_lookml_dir / f"{view_name}.view.lkml"
            fields = (
                _parse_view_fields(bundled_view_file)
                if bundled_view_file.exists()
                else ExploreFields()
            )
        else:
            fields = ExploreFields()

        # Mark as bundled if this view is from bundled data (not overridden by user)
        is_bundled = view_name in bundled_view_names

        explore_nav = LookmlModelNavExplore(
            name=view_name,
            label=view_name.replace("_", " ").title(),
            description=f"Auto-generated from {view_name}.csv",
            hidden=False,
            group_label="User Data",
            is_bundled=is_bundled,
        )
        explore_list.append(explore_nav)

        explore = ExploreResponse(
            name=view_name,
            label=view_name.replace("_", " ").title(),
            description=f"Auto-generated from {view_name}.csv",
            model_name="user_data",
            view_name=view_name,
            fields=fields,
            joins=[],
        )
        explores[("user_data", view_name)] = explore

    model = LookMLModel(
        name="user_data",
        project_name="user_data_project",
        label="User Data",
        explores=explore_list,
        allowed_db_connection_names=["mercor"],
        unlimited_db_connections=False,
    )

    return [model], explores


def _parse_view_fields(view_file: Path):
    """Parse LookML view file to extract field metadata."""
    from models import ExploreFields, LookMLField

    dimensions = []
    measures = []

    if not view_file.exists():
        return ExploreFields(dimensions=dimensions, measures=measures)

    content = view_file.read_text()
    view_name = view_file.stem.replace(".view", "")

    # Parse dimension blocks
    dim_pattern = r"dimension:\s+(\w+)\s*\{((?:[^{}]|\$\{[^}]*\})*)\}"
    for match in re.finditer(dim_pattern, content, re.DOTALL):
        field_name = match.group(1)
        block = match.group(2)

        field_type = _extract_value(block, "type") or "string"
        label = _extract_value(block, "label")
        description = _extract_value(block, "description")

        dimensions.append(
            LookMLField(
                name=f"{view_name}.{field_name}",
                label=label or field_name.replace("_", " ").title(),
                type=field_type,
                description=description,
                view=view_name,
                hidden=False,
            )
        )

    # Parse measure blocks
    measure_pattern = r"measure:\s+(\w+)\s*\{((?:[^{}]|\$\{[^}]*\})*)\}"
    for match in re.finditer(measure_pattern, content, re.DOTALL):
        field_name = match.group(1)
        block = match.group(2)

        field_type = _extract_value(block, "type") or "count"
        label = _extract_value(block, "label")
        description = _extract_value(block, "description")

        measures.append(
            LookMLField(
                name=f"{view_name}.{field_name}",
                label=label or field_name.replace("_", " ").title(),
                type=field_type,
                description=description,
                view=view_name,
                hidden=False,
            )
        )

    return ExploreFields(dimensions=dimensions, measures=measures)


def _extract_value(block: str, key: str) -> str | None:
    """Extract a value from a LookML block."""
    pattern = rf'{key}:\s*"?([^"\n]+)"?'
    match = re.search(pattern, block)
    if match:
        return match.group(1).strip()
    return None


# Public API for other modules
def get_lookml_models() -> list:
    """Get all LookML models (user + bundled)."""
    return _lookml_models


def get_lookml_explores() -> dict:
    """Get all explores as dict mapping (model_name, explore_name) to ExploreResponse."""
    return _lookml_explores


def add_single_view(view_name: str, csv_path: Path) -> None:
    """Add a single uploaded CSV incrementally without rebuilding everything.

    This is O(1) - it only processes the new CSV, not all existing ones.
    DuckDB is the single source of truth - we load the CSV first, then
    generate fields from the DuckDB schema.

    Args:
        view_name: Name of the view (e.g., 'ratings' from 'ratings.csv')
        csv_path: Path to the CSV file
    """
    global _lookml_models, _lookml_explores

    import duckdb
    from models import ExploreResponse, LookmlModelNavExplore
    from scripts.build_duckdb import load_csv_to_table

    # Ensure data layer is initialized (no-op if already done)
    if not _initialized:
        initialize_data_layer()

    # Ensure view_name matches csv_path.stem — load_csv_to_table uses
    # csv_path.stem as the DuckDB table name, so they must agree.
    table_name = csv_path.stem
    if view_name != table_name:
        logger.warning(
            f"view_name '{view_name}' differs from csv stem '{table_name}'; "
            f"using '{table_name}' for DuckDB consistency"
        )
        view_name = table_name

    # 1. Load CSV into DuckDB first (DuckDB is the source of truth)
    db_path = get_runtime_duckdb_path()
    conn = duckdb.connect(str(db_path))
    try:
        load_csv_to_table(conn, csv_path)
        logger.info(f"Loaded table {view_name} into DuckDB")
    finally:
        conn.close()

    # 2. Generate fields from DuckDB schema (not from CSV parsing)
    fields = _generate_fields_from_duckdb_schema(view_name)
    logger.info(
        f"Generated fields for {view_name} from DuckDB: "
        f"{len(fields.dimensions)} dims, {len(fields.measures)} measures"
    )

    # 3. Create explore objects for this view
    explore_nav = LookmlModelNavExplore(
        name=view_name,
        label=view_name.replace("_", " ").title(),
        description=f"Auto-generated from {view_name}.csv",
        hidden=False,
        group_label="User Data",
        is_bundled=False,
    )

    explore = ExploreResponse(
        name=view_name,
        label=view_name.replace("_", " ").title(),
        description=f"Auto-generated from {view_name}.csv",
        model_name="user_data",
        view_name=view_name,
        fields=fields,
        joins=[],
    )

    # 4. Merge into existing state
    # Update explores dict (overwrites if exists)
    _lookml_explores[("user_data", view_name)] = explore

    # Update model's explore list (create model if needed)
    if not _lookml_models:
        # Create user_data model if none exists (edge case: no bundled data)
        from models import LookMLModel

        _lookml_models.append(
            LookMLModel(
                name="user_data",
                project_name="user_data_project",
                label="User Data",
                explores=[],
                allowed_db_connection_names=["mercor"],
                unlimited_db_connections=False,
            )
        )
        logger.info("Created user_data model (no bundled data present)")

    model = _lookml_models[0]
    # Remove existing explore with same name (if re-uploading)
    model.explores = [e for e in model.explores if e.name != view_name]
    # Add new explore
    model.explores.append(explore_nav)
    logger.info(f"Added explore {view_name} to model (total: {len(model.explores)} explores)")


def clear_user_data() -> None:
    """Clear all user-uploaded CSV data.

    This removes all CSV files from the user data directory, drops the
    corresponding DuckDB tables, and reloads the data layer to remove
    them from the in-memory models/explores.
    """
    import duckdb

    user_dir = get_user_csv_dir()
    tables_to_drop = []

    if user_dir and user_dir.exists():
        # Collect table names and remove CSV files
        for csv_file in user_dir.glob("*.csv"):
            tables_to_drop.append(csv_file.stem)  # table name = filename without .csv
            try:
                csv_file.unlink()
                logger.info(f"Deleted user CSV: {csv_file}")
            except Exception as e:
                logger.warning(f"Failed to delete {csv_file}: {e}")

    # Drop corresponding DuckDB tables
    if tables_to_drop:
        db_path = get_runtime_duckdb_path()
        if db_path.exists():
            try:
                conn = duckdb.connect(str(db_path))
                for table_name in tables_to_drop:
                    try:
                        conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table_name)}")
                        logger.info(f"Dropped DuckDB table: {table_name}")
                    except Exception as e:
                        logger.warning(f"Failed to drop table {table_name}: {e}")
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to connect to DuckDB for cleanup: {e}")

    # Reload data layer to clear in-memory state
    initialize_data_layer(force_reload=True)


# Aliases for backwards compatibility
ensure_user_data_loaded = initialize_data_layer
get_user_models = get_lookml_models
get_user_explores = get_lookml_explores
get_user_data_dir = get_user_csv_dir
