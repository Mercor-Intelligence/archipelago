"""Workbook publishing tool for uploading .twb/.twbx files.

This tool enables uploading complete workbook files to the Tableau system.
When a workbook is published, all views (worksheets, dashboards, stories)
contained in the workbook are automatically created.

Supports modern Tableau Hyper (.hyper) data extracts bundled within .twbx
packages, as well as legacy CSV and Excel formats.

Environment Variables:
- TABLEAU_TEST_MODE: "local" (default) or "http" for live Tableau Cloud
- TABLEAU_SERVER_URL: Tableau Server URL (required for HTTP mode)
- TABLEAU_SITE_ID: Site content URL (required for HTTP mode)
- TABLEAU_TOKEN_NAME: PAT name (required for HTTP mode)
- TABLEAU_TOKEN_SECRET: PAT secret (required for HTTP mode)
"""

import base64
import io
import json
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
from db.models import User, View, Workbook
from db.session import get_session
from models import (
    TableauPublishWorkbookInput,
    TableauPublishWorkbookOutput,
)
from sqlalchemy import select

# tableauhyperapi is only available on certain platforms (not Linux ARM64)
try:
    from tableauhyperapi import Connection, CreateMode, HyperException, HyperProcess, Telemetry

    HYPER_AVAILABLE = True
except ImportError:
    HYPER_AVAILABLE = False
    HyperException = Exception  # Fallback for exception handling

logger = logging.getLogger(__name__)

# Maximum rows to extract as sample data (None = unlimited)
_MAX_SAMPLE_ROWS = None

# Base directories for local file access (searched in order)
# 1. STATE_LOCATION: In production (RL Studio), points to /.apps_data/tableau/ where uploads land
# 2. APP_FS_ROOT: Alternative root for workbook files
# 3. Development fallback: mcp_servers/tableau/data/
_DATA_DIRS: list[Path] = []

_state_location = os.getenv("STATE_LOCATION")
if _state_location:
    _DATA_DIRS.append(Path(_state_location))

_app_fs_root = os.getenv("APP_FS_ROOT")
if _app_fs_root:
    _DATA_DIRS.append(Path(_app_fs_root))

# Fallback for development
if not _DATA_DIRS:
    _DATA_DIRS.append(Path(__file__).parent.parent / "data")


def _extract_view_names(file_content: bytes, file_name: str) -> list[tuple[str, str]]:
    """Extract worksheet/dashboard/story names from workbook file.

    Args:
        file_content: Binary content of .twb or .twbx file
        file_name: Filename with extension

    Returns:
        List of (view_name, sheet_type) tuples
    """
    try:
        if file_name.lower().endswith(".twbx"):
            # .twbx is a ZIP containing .twb
            with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
                for name in zf.namelist():
                    if name.endswith(".twb"):
                        return _parse_twb_xml(zf.read(name))
        else:
            # .twb is XML directly
            return _parse_twb_xml(file_content)
    except Exception:
        # If parsing fails, return default view
        pass

    return [("Sheet 1", "worksheet")]


def _parse_twb_xml(xml_content: bytes) -> list[tuple[str, str]]:
    """Parse .twb XML to extract worksheet/dashboard/story names.

    Args:
        xml_content: Binary XML content of .twb file

    Returns:
        List of (view_name, sheet_type) tuples (deduplicated by name)
    """
    seen_names: set[str] = set()
    views = []

    try:
        root = ET.fromstring(xml_content)

        # Find worksheets (deduplicate by name)
        for ws in root.findall(".//worksheet"):
            name = ws.get("name", "Untitled")
            if name not in seen_names:
                seen_names.add(name)
                views.append((name, "worksheet"))

        # Find dashboards (deduplicate by name)
        for db in root.findall(".//dashboard"):
            name = db.get("name", "Untitled Dashboard")
            if name not in seen_names:
                seen_names.add(name)
                views.append((name, "dashboard"))

        # Find stories (deduplicate by name)
        for story in root.findall(".//story"):
            name = story.get("name", "Untitled Story")
            if name not in seen_names:
                seen_names.add(name)
                views.append((name, "story"))

    except ET.ParseError:
        pass

    # Return at least one default view if none found
    return views if views else [("Sheet 1", "worksheet")]


def _get_twb_content(file_content: bytes, file_name: str) -> bytes | None:
    """Extract .twb XML content from .twb or .twbx file."""
    try:
        if file_name.lower().endswith(".twbx"):
            with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
                for name in zf.namelist():
                    if name.endswith(".twb"):
                        return zf.read(name)
        else:
            return file_content
    except Exception:
        pass
    return None


def _get_datasource_file_paths(datasource: ET.Element) -> list[str]:
    """Extract ALL data file paths from a datasource element.

    Handles federated datasources that combine multiple CSV/Excel files.
    Returns list of filenames (basename only, not full paths).
    """
    file_paths = []

    # Extract Hyper files
    for conn in datasource.findall(".//connection[@class='hyper']"):
        if dbname := conn.get("dbname"):
            file_paths.append(dbname)

    # Extract Excel files
    for conn in datasource.findall(".//connection[@class='excel-direct']"):
        if filename := conn.get("filename"):
            file_paths.append(Path(filename).name)

    # Extract CSV files (textscan)
    for conn in datasource.findall(".//connection[@class='textscan']"):
        if filename := conn.get("filename"):
            file_paths.append(Path(filename).name)

    return file_paths


def _parse_datasource_files(xml_content: bytes) -> dict[str, list[str]]:
    """Parse .twb XML to map datasource names to their data file paths.

    Returns dict mapping datasource name to list of file paths.
    Supports federated datasources with multiple files.
    """
    datasource_files: dict[str, list[str]] = {}

    try:
        root = ET.fromstring(xml_content)
        for datasource in root.findall(".//datasource"):
            if (ds_name := datasource.get("name")) and (
                file_paths := _get_datasource_file_paths(datasource)
            ):
                datasource_files[ds_name] = file_paths
    except ET.ParseError:
        pass

    return datasource_files


def _get_worksheet_datasources(worksheet: ET.Element) -> list[str]:
    """Extract datasource names from a worksheet element."""
    datasources = []
    for ds in worksheet.findall(".//table/view/datasources/datasource"):
        if (ds_name := ds.get("name")) and ds_name not in datasources:
            datasources.append(ds_name)

    if not datasources:
        for dep in worksheet.findall(".//datasource-dependencies"):
            if (ds_name := dep.get("datasource")) and ds_name not in datasources:
                datasources.append(ds_name)

    return datasources


def _parse_worksheet_datasources(xml_content: bytes) -> dict[str, list[str]]:
    """Parse .twb XML to map worksheet names to their datasource names."""
    worksheet_datasources: dict[str, list[str]] = {}

    try:
        root = ET.fromstring(xml_content)
        for worksheet in root.findall(".//worksheet"):
            if (ws_name := worksheet.get("name")) and (
                datasources := _get_worksheet_datasources(worksheet)
            ):
                worksheet_datasources[ws_name] = datasources
    except ET.ParseError:
        pass

    return worksheet_datasources


def _extract_hyper_data(file_content: bytes) -> list[dict]:
    """Extract data from a Tableau Hyper (.hyper) data extract."""
    if not HYPER_AVAILABLE:
        return []

    try:
        # Write content to temporary file for Hyper API (requires file path)
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".hyper", delete=False) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        try:
            # Use HyperProcess to manage the Hyper database server instance
            with HyperProcess(
                Telemetry.SEND_USAGE_DATA_TO_TABLEAU, "hyper-data-extractor"
            ) as hyper:
                # Connect to the Hyper file
                with Connection(hyper.endpoint, tmp_path, CreateMode.NONE) as connection:
                    # Get all schema names in the Hyper file
                    schema_names = connection.catalog.get_schema_names()

                    if not schema_names:
                        return []

                    rows = []

                    # Iterate through schemas to find tables
                    for schema in schema_names:
                        table_names = connection.catalog.get_table_names(schema)

                        if not table_names:
                            continue

                        # Use the first table found
                        table_name = table_names[0]

                        # Query the table for sample data using the fully-qualified table_name object
                        limit_clause = f" LIMIT {_MAX_SAMPLE_ROWS}" if _MAX_SAMPLE_ROWS else ""
                        query = f"SELECT * FROM {table_name}{limit_clause}"

                        result = connection.execute_list_query(query)

                        # Get column names from table definition
                        table_def = connection.catalog.get_table_definition(table_name)
                        # Column names are Name objects; extract the name attribute and remove quotes
                        column_names = []
                        for col in table_def.columns:
                            col_name = col.name.name if hasattr(col.name, "name") else str(col.name)
                            # Remove surrounding quotes if present
                            col_name = col_name.strip('"')
                            column_names.append(col_name)

                        # Convert Row objects to dicts
                        for row in result:
                            row_dict = {}
                            for i, col_name in enumerate(column_names):
                                value = row[i] if i < len(row) else None
                                # Handle various data types for JSON serialization
                                if value is None:
                                    row_dict[col_name] = None
                                elif isinstance(value, datetime):
                                    row_dict[col_name] = value.isoformat()
                                else:
                                    # Try to convert to string for unknown types (like Timestamp objects)
                                    try:
                                        # If value has isoformat method, use it
                                        if hasattr(value, "isoformat"):
                                            row_dict[col_name] = value.isoformat()
                                        else:
                                            # Try JSON serializable conversion
                                            row_dict[col_name] = (
                                                str(value)
                                                if not isinstance(value, int | float | bool)
                                                else value
                                            )
                                    except Exception:
                                        row_dict[col_name] = str(value)

                            rows.append(row_dict)

                        # Return after extracting from first table
                        return rows

                    return []

        finally:
            # Clean up temporary Hyper file
            os.unlink(tmp_path)

    except HyperException:
        # Hyper API specific error - silently return empty list
        pass
    except Exception:
        # If extraction fails for any reason, silently return empty list
        pass

    return []


def _extract_embedded_data(file_content: bytes, file_name: str) -> list[dict]:
    """Extract data from embedded files in .twbx workbook packages.

    Supports multiple data formats bundled within .twbx, with priority order:
        1. .hyper: Tableau's modern columnar storage format (Tableau 2020+)
        2. .xlsx, .xls: Excel files
        3. .csv: CSV files

    Args:
        file_content: Binary content of .twbx file
        file_name: Filename with extension

    Returns:
        List of row dicts (all rows if _MAX_SAMPLE_ROWS is None), or empty list if extraction fails
    """
    if not file_name.lower().endswith(".twbx"):
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
            # Find data files (skip cache files, prioritize modern formats)
            data_files = [
                name
                for name in zf.namelist()
                if not name.endswith(".twb")
                and "Cache" not in name
                and (
                    name.lower().endswith(".hyper")
                    or name.lower().endswith(".xls")
                    or name.lower().endswith(".xlsx")
                    or name.lower().endswith(".csv")
                )
            ]

            if not data_files:
                return []

            # Prioritize .hyper files over legacy formats
            hyper_files = [f for f in data_files if f.lower().endswith(".hyper")]
            if hyper_files:
                data_file = hyper_files[0]
                data_bytes = zf.read(data_file)
                return _extract_hyper_data(data_bytes)

            # Fall back to legacy formats
            data_file = data_files[0]
            data_bytes = zf.read(data_file)

            # Parse based on file type
            if data_file.lower().endswith(".csv"):
                df = pd.read_csv(io.BytesIO(data_bytes), nrows=_MAX_SAMPLE_ROWS)
            elif data_file.lower().endswith(".xlsx"):
                df = pd.read_excel(
                    io.BytesIO(data_bytes),
                    engine="openpyxl",
                    nrows=_MAX_SAMPLE_ROWS,
                )
            else:  # .xls
                df = pd.read_excel(
                    io.BytesIO(data_bytes),
                    engine="xlrd",
                    nrows=_MAX_SAMPLE_ROWS,
                )

            # Convert to list of dicts, handling NaN and datetime
            records = df.to_dict(orient="records")

            # Clean up values for JSON serialization
            clean_records = []
            for record in records:
                clean_record = {}
                for k, v in record.items():
                    if pd.isna(v):
                        clean_record[k] = None
                    elif isinstance(v, pd.Timestamp | datetime):
                        clean_record[k] = v.isoformat()
                    else:
                        clean_record[k] = v
                clean_records.append(clean_record)

            return clean_records

    except Exception:
        # If extraction fails, return empty list (will use placeholder)
        pass

    return []


def _read_tabular_file(data_bytes: bytes, file_name: str) -> pd.DataFrame | None:
    """Read CSV or Excel file into a DataFrame."""
    stream = io.BytesIO(data_bytes)
    match Path(file_name).suffix.lower():
        case ".csv":
            return pd.read_csv(stream, nrows=_MAX_SAMPLE_ROWS)
        case ".xlsx":
            return pd.read_excel(stream, engine="openpyxl", nrows=_MAX_SAMPLE_ROWS)
        case ".xls":
            return pd.read_excel(stream, engine="xlrd", nrows=_MAX_SAMPLE_ROWS)
        case _:
            return None


def _clean_value(v):
    """Convert a value to be JSON-serializable."""
    match v:
        case _ if pd.isna(v):
            return None
        case pd.Timestamp() | datetime():
            return v.isoformat()
        case _:
            return v


def _dataframe_to_clean_records(df: pd.DataFrame) -> list[dict]:
    """Convert DataFrame to list of dicts with JSON-safe values."""
    return [
        {k: _clean_value(v) for k, v in record.items()} for record in df.to_dict(orient="records")
    ]


def _extract_single_data_file(zf: zipfile.ZipFile, data_file: str) -> list[dict]:
    """Extract data from a single data file within a ZIP archive."""
    try:
        data_bytes = zf.read(data_file)

        if data_file.lower().endswith(".hyper"):
            return _extract_hyper_data(data_bytes)

        df = _read_tabular_file(data_bytes, data_file)
        if df is not None:
            return _dataframe_to_clean_records(df)

    except Exception:
        pass

    return []


def _extract_all_embedded_data(file_content: bytes, file_name: str) -> dict[str, list[dict]]:
    """Extract data from ALL embedded files in .twbx workbook packages.

    Unlike _extract_embedded_data which returns only the first data file,
    this function extracts all data files and maps them by their path.
    """
    all_data: dict[str, list[dict]] = {}

    if not file_name.lower().endswith(".twbx"):
        return all_data

    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
            data_files = [
                name
                for name in zf.namelist()
                if not name.endswith(".twb")
                and "Cache" not in name
                and (
                    name.lower().endswith(".hyper")
                    or name.lower().endswith(".xls")
                    or name.lower().endswith(".xlsx")
                    or name.lower().endswith(".csv")
                )
            ]

            for data_file in data_files:
                rows = _extract_single_data_file(zf, data_file)
                if rows:
                    all_data[data_file] = rows
                    basename = Path(data_file).name
                    if basename not in all_data:
                        all_data[basename] = rows

    except Exception:
        pass

    return all_data


async def _publish_workbook_http(
    request: TableauPublishWorkbookInput,
    file_name: str,
    file_content: bytes,
) -> TableauPublishWorkbookOutput:
    """Publish workbook via Tableau REST API.

    Args:
        request: Publish workbook request
        file_name: Name of the workbook file
        file_content: Binary content of the workbook file

    Returns:
        Published workbook details
    """
    import httpx
    from tableau_http.tableau_client import TableauHTTPClient

    # Get credentials from environment
    server_url = os.environ.get("TABLEAU_SERVER_URL")
    site_content_url = os.environ.get("TABLEAU_SITE_ID")
    token_name = os.environ.get("TABLEAU_TOKEN_NAME")
    token_secret = os.environ.get("TABLEAU_TOKEN_SECRET")

    if not all([server_url, site_content_url, token_name, token_secret]):
        raise ValueError(
            "HTTP mode requires TABLEAU_SERVER_URL, TABLEAU_SITE_ID, "
            "TABLEAU_TOKEN_NAME, and TABLEAU_TOKEN_SECRET environment variables"
        )

    # Create client and sign in
    client = TableauHTTPClient(
        base_url=server_url,
        site_id=site_content_url,
        personal_access_token=f"{token_name}:{token_secret}",
    )
    await client.sign_in()

    # Build publish URL with overwrite parameter
    overwrite_param = "true" if request.overwrite else "false"
    publish_url = f"{client.api_base}/sites/{client.site_id}/workbooks?overwrite={overwrite_param}"

    # Create request JSON for workbook metadata
    # json.dumps() automatically handles escaping of special characters
    request_payload = {
        "workbook": {
            "name": request.name,
            "showTabs": True,
            "project": {"id": request.project_id},
        }
    }
    request_json = json.dumps(request_payload)

    # Build multipart body manually for proper boundary handling
    boundary = "----TableauBoundary"

    # Build multipart body manually
    body_parts = []

    # Request payload part (JSON format)
    body_parts.append(f"--{boundary}")
    body_parts.append('Content-Disposition: name="request_payload"')
    body_parts.append("Content-Type: application/json")
    body_parts.append("")
    body_parts.append(request_json)

    # File part
    # Escape filename for Content-Disposition header (RFC 6266)
    # Escape backslashes first, then double quotes
    escaped_file_name = file_name.replace("\\", "\\\\").replace('"', '\\"')
    body_parts.append(f"--{boundary}")
    body_parts.append(
        f'Content-Disposition: name="tableau_workbook"; filename="{escaped_file_name}"'
    )
    body_parts.append("Content-Type: application/octet-stream")
    body_parts.append("")

    # Join text parts
    body_text = "\r\n".join(body_parts) + "\r\n"
    body_bytes = body_text.encode("utf-8") + file_content + f"\r\n--{boundary}--\r\n".encode()

    headers = {
        "X-Tableau-Auth": client.auth_token,
        "Content-Type": f"multipart/mixed; boundary={boundary}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=120.0) as http_client:
        response = await http_client.post(publish_url, content=body_bytes, headers=headers)
        response.raise_for_status()

    # Parse JSON response to get workbook details
    response_data = response.json()
    workbook_data = response_data.get("workbook")

    if workbook_data is None:
        raise ValueError("Failed to parse workbook response from Tableau API")

    workbook_id = workbook_data.get("id")
    content_url = workbook_data.get("contentUrl", "")
    owner_data = workbook_data.get("owner", {})
    owner_id = owner_data.get("id") if owner_data else None
    project_data = workbook_data.get("project", {})
    project_id = project_data.get("id") if project_data else request.project_id

    # Extract view IDs from response
    view_ids = []
    views_data = workbook_data.get("views", {}).get("view", [])
    for view in views_data:
        view_id = view.get("id")
        if view_id:
            view_ids.append(view_id)

    now = datetime.now(timezone.utc).isoformat()

    return TableauPublishWorkbookOutput(
        id=workbook_id,
        name=request.name,
        project_id=project_id,
        owner_id=owner_id,
        description=request.description,
        content_url=content_url,
        created_at=now,
        updated_at=now,
        views=view_ids,
    )


def _resolve_file_input(request: TableauPublishWorkbookInput) -> tuple[str, bytes]:
    """Resolve file_path or file_content_base64 to (file_name, file_content)."""
    if request.file_path:
        # Read from local file
        # Support both absolute paths and relative paths within _DATA_DIRS
        # _DATA_DIRS includes STATE_LOCATION, APP_FS_ROOT, or dev fallback
        if os.path.isabs(request.file_path):
            file_path = Path(request.file_path)
            if not file_path.exists():
                raise ValueError(f"File not found: {request.file_path}")
        else:
            # Search through all configured data directories
            file_path = None
            for data_dir in _DATA_DIRS:
                candidate = data_dir / request.file_path
                if candidate.exists():
                    file_path = candidate
                    break

            if file_path is None:
                searched_dirs = ", ".join(str(d) for d in _DATA_DIRS)
                raise ValueError(
                    f"File not found: {request.file_path}. Searched in: {searched_dirs}"
                )

        # Auto-detect file_name from path
        file_name = request.file_name or file_path.name

        # Read file content
        try:
            return file_name, file_path.read_bytes()
        except Exception as e:
            raise ValueError(f"Failed to read file {request.file_path}: {e}")

    elif request.file_content_base64:
        # Use base64 content
        if not request.file_name:
            raise ValueError("file_name is required when using file_content_base64")
        try:
            return request.file_name, base64.b64decode(request.file_content_base64)
        except Exception as e:
            raise ValueError(f"Invalid base64 encoding in file_content_base64: {e}")

    raise ValueError("Either file_path or file_content_base64 must be provided")


_PLACEHOLDER_DATA = json.dumps([{"column1": "sample_value", "column2": 100}])


def _find_view_data(
    view_name: str,
    sheet_type: str,
    worksheet_datasources: dict[str, list[str]],
    datasource_files: dict[str, list[str]],
    all_data: dict[str, list[dict]],
) -> str | None:
    """Find sample data for a single view, returning JSON string or None.

    For federated datasources with multiple files, tries to match the view name
    to a filename (e.g., "Executive Summary" -> "Executive Summary.csv").
    Returns None if no match is found (better to have no data than wrong data).
    """
    if sheet_type == "worksheet":
        # Get datasources for this worksheet, or fall back to all datasources if not mapped
        # (some workbooks don't have explicit worksheet-datasource mappings in the XML)
        datasources = worksheet_datasources.get(view_name, [])
        if not datasources and len(datasource_files) == 1:
            # Only one datasource in the workbook - use it for unmapped worksheets
            datasources = list(datasource_files.keys())

        if datasources:
            # Get all files for this datasource (may be multiple for federated datasources)
            if file_paths := datasource_files.get(datasources[0], []):
                # Strategy 1: Try to match view name to filename (for federated datasources)
                # This handles cases like "Executive Summary" -> "Executive Summary.csv"
                view_name_lower = (
                    view_name.lower().replace(" ", "").replace("-", "").replace("_", "")
                )

                for file_path in file_paths:
                    # Try exact basename match first
                    file_basename = Path(file_path).stem.lower()
                    file_basename_normalized = (
                        file_basename.replace(" ", "").replace("-", "").replace("_", "")
                    )

                    if file_basename_normalized == view_name_lower:
                        if data := all_data.get(file_path) or all_data.get(Path(file_path).name):
                            return json.dumps(data)

                # Strategy 2: Try partial match (view name contained in filename or vice versa)
                for file_path in file_paths:
                    file_basename = Path(file_path).stem.lower()
                    file_basename_normalized = (
                        file_basename.replace(" ", "").replace("-", "").replace("_", "")
                    )

                    if (
                        view_name_lower in file_basename_normalized
                        or file_basename_normalized in view_name_lower
                    ):
                        if data := all_data.get(file_path) or all_data.get(Path(file_path).name):
                            return json.dumps(data)

                # If only one file in datasource, use it (common case for non-federated)
                if len(file_paths) == 1:
                    first_file = file_paths[0]
                    if data := all_data.get(first_file) or all_data.get(Path(first_file).name):
                        return json.dumps(data)

                # Strategy 3: If we have extracted data, use the first available data
                # This handles cases where file paths don't match but we have data
                for file_path in file_paths:
                    if data := all_data.get(file_path) or all_data.get(Path(file_path).name):
                        return json.dumps(data)

    # No match found - return None rather than wrong data
    return None


def _build_view_sample_data(
    file_content: bytes,
    file_name: str,
    view_names: list[tuple[str, str]],
) -> dict[str, str]:
    """Build per-view sample data mapping from workbook content."""
    if twb_content := _get_twb_content(file_content, file_name):
        worksheet_datasources = _parse_worksheet_datasources(twb_content)
        datasource_files = _parse_datasource_files(twb_content)
        all_data = _extract_all_embedded_data(file_content, file_name)

        return {
            view_name: _find_view_data(
                view_name, sheet_type, worksheet_datasources, datasource_files, all_data
            )
            or _PLACEHOLDER_DATA
            for view_name, sheet_type in view_names
        }

    embedded_data = _extract_embedded_data(file_content, file_name)
    sample_json = json.dumps(embedded_data) if embedded_data else _PLACEHOLDER_DATA
    return {view_name: sample_json for view_name, _ in view_names}


async def _publish_workbook_local(
    request: TableauPublishWorkbookInput,
    file_name: str,
    file_content: bytes,
) -> TableauPublishWorkbookOutput:
    """Publish workbook to local database."""
    view_names = _extract_view_names(file_content, file_name)
    view_sample_data = _build_view_sample_data(file_content, file_name, view_names)

    async with get_session() as session:
        # Check for existing workbook with same name in project if not overwriting
        if not request.overwrite:
            existing = await session.execute(
                select(Workbook).where(
                    Workbook.site_id == request.site_id,
                    Workbook.project_id == request.project_id,
                    Workbook.name == request.name,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(
                    f"Workbook '{request.name}' already exists in project. "
                    "Set overwrite=True to replace it."
                )

        # Handle overwrite: delete existing workbook (views cascade delete)
        if request.overwrite:
            existing = await session.execute(
                select(Workbook).where(
                    Workbook.site_id == request.site_id,
                    Workbook.project_id == request.project_id,
                    Workbook.name == request.name,
                )
            )
            existing_wb = existing.scalar_one_or_none()
            if existing_wb:
                await session.delete(existing_wb)
                await session.flush()

        # Get owner_id - use provided or find first user in site
        owner_id = request.owner_id
        if not owner_id:
            result = await session.execute(
                select(User).where(User.site_id == request.site_id).limit(1)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise ValueError(f"No users found in site {request.site_id}")
            owner_id = user.id

        # Create workbook record
        workbook_id = str(uuid4())
        now = datetime.now(timezone.utc)

        workbook = Workbook(
            id=workbook_id,
            site_id=request.site_id,
            name=request.name,
            project_id=request.project_id,
            owner_id=owner_id,
            description=request.description,
            file_reference=f"local://{file_name}",
            created_at=now,
            updated_at=now,
        )
        session.add(workbook)
        await session.flush()  # Flush workbook first so FK constraint succeeds for views

        # Create view records for each extracted view
        view_ids = []
        for view_name, sheet_type in view_names:
            view_id = str(uuid4())
            view = View(
                id=view_id,
                site_id=request.site_id,
                workbook_id=workbook_id,
                name=view_name,
                content_url=f"{request.name}/{view_name}".replace(" ", ""),
                sheet_type=sheet_type,
                sample_data_json=view_sample_data.get(view_name, _PLACEHOLDER_DATA),
                preview_image_path=None,
                created_at=now,
                updated_at=now,
            )
            session.add(view)
            view_ids.append(view_id)

        await session.commit()

        return TableauPublishWorkbookOutput(
            id=workbook_id,
            name=workbook.name,
            project_id=workbook.project_id,
            owner_id=workbook.owner_id,
            description=workbook.description,
            content_url=f"workbooks/{workbook_id}",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            views=view_ids,
        )


async def tableau_publish_workbook(
    request: TableauPublishWorkbookInput,
) -> TableauPublishWorkbookOutput:
    """Publish a workbook file (.twb or .twbx) to Tableau."""
    file_name, file_content = _resolve_file_input(request)

    if not (file_name.lower().endswith(".twb") or file_name.lower().endswith(".twbx")):
        raise ValueError(f"Invalid file format: {file_name}. Must be .twb or .twbx")

    if os.environ.get("TABLEAU_TEST_MODE", "local").lower() == "http":
        return await _publish_workbook_http(request, file_name, file_content)

    return await _publish_workbook_local(request, file_name, file_content)
