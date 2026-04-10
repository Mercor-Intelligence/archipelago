"""Dataset tools for BambooHR MCP server.

Implements:
- bamboo.datasets.list: List available datasets
- bamboo.datasets.get_fields: Get dataset field definitions
- bamboo.datasets.get_field_options: Get options for a dataset field
- bamboo.datasets.query: Query dataset rows with filtering, sorting, aggregations

Per BUILD_PLAN sections 3.2.28-3.2.31:
- datasets.list, get_fields, get_field_options: All personas can access
- datasets.query: HR Admin only
"""

from datetime import date, datetime, timedelta
from typing import Any

from db import Employee, TimeOffRequest, TimeOffType, get_session
from mcp_auth import user_has_role
from sqlalchemy import select

from .meta import LIST_FIELDS

# =============================================================================
# Dataset Definitions
# =============================================================================
DATASETS: list[dict[str, str]] = [
    {"name": "employees", "title": "Employee Data"},
    {"name": "timeOff", "title": "Time Off Requests"},
    {"name": "jobInfo", "title": "Job Information"},
    {"name": "compensation", "title": "Compensation Data"},
]

# Dataset field definitions
# Each dataset has a list of fields with id, name, and type
DATASET_FIELDS: dict[str, list[dict[str, str]]] = {
    "employees": [
        {"id": "id", "name": "Employee ID", "type": "text"},
        {"id": "firstName", "name": "First Name", "type": "text"},
        {"id": "lastName", "name": "Last Name", "type": "text"},
        {"id": "displayName", "name": "Display Name", "type": "text"},
        {"id": "preferredName", "name": "Preferred Name", "type": "text"},
        {"id": "workEmail", "name": "Work Email", "type": "email"},
        {"id": "department", "name": "Department", "type": "options"},
        {"id": "division", "name": "Division", "type": "options"},
        {"id": "location", "name": "Location", "type": "options"},
        {"id": "jobTitle", "name": "Job Title", "type": "options"},
        {"id": "employmentStatus", "name": "Employment Status", "type": "options"},
        {"id": "hireDate", "name": "Hire Date", "type": "date"},
        {"id": "terminationDate", "name": "Termination Date", "type": "date"},
        {"id": "supervisorId", "name": "Supervisor ID", "type": "text"},
        {"id": "status", "name": "Status", "type": "text"},
    ],
    "timeOff": [
        {"id": "id", "name": "Request ID", "type": "text"},
        {"id": "employeeId", "name": "Employee ID", "type": "text"},
        {"id": "status", "name": "Status", "type": "options"},
        {"id": "startDate", "name": "Start Date", "type": "date"},
        {"id": "endDate", "name": "End Date", "type": "date"},
        {"id": "type", "name": "Time Off Type", "type": "options"},
        {"id": "amount", "name": "Amount", "type": "int"},
        {"id": "notes", "name": "Notes", "type": "text"},
        {"id": "created", "name": "Created Date", "type": "date"},
    ],
    "jobInfo": [
        {"id": "id", "name": "Record ID", "type": "text"},
        {"id": "employeeId", "name": "Employee ID", "type": "text"},
        {"id": "effectiveDate", "name": "Effective Date", "type": "date"},
        {"id": "department", "name": "Department", "type": "options"},
        {"id": "division", "name": "Division", "type": "options"},
        {"id": "location", "name": "Location", "type": "options"},
        {"id": "jobTitle", "name": "Job Title", "type": "options"},
        {"id": "reportsTo", "name": "Reports To", "type": "text"},
    ],
    "compensation": [
        {"id": "id", "name": "Record ID", "type": "text"},
        {"id": "employeeId", "name": "Employee ID", "type": "text"},
        {"id": "effectiveDate", "name": "Effective Date", "type": "date"},
        {"id": "payRate", "name": "Pay Rate", "type": "int"},
        {"id": "payType", "name": "Pay Type", "type": "options"},
        {"id": "payPeriod", "name": "Pay Period", "type": "options"},
        {"id": "changeReason", "name": "Change Reason", "type": "text"},
    ],
}

# Field options for dataset options fields
# Maps dataset_name.field_id to list of options
DATASET_FIELD_OPTIONS: dict[str, list[dict[str, str]]] = {
    # Employee dataset options (referencing meta LIST_FIELDS)
    "employees.department": [],  # Will be populated from LIST_FIELDS
    "employees.division": [],
    "employees.location": [],
    "employees.jobTitle": [],
    "employees.employmentStatus": [],
    # JobInfo dataset options (same as employees - populated from LIST_FIELDS)
    "jobInfo.department": [],
    "jobInfo.division": [],
    "jobInfo.location": [],
    "jobInfo.jobTitle": [],
    # Time off status options - must match TimeOffRequestStatus enum values in db/models.py
    "timeOff.status": [
        {"optionId": "1", "label": "requested"},
        {"optionId": "2", "label": "approved"},
        {"optionId": "3", "label": "denied"},
        {"optionId": "4", "label": "canceled"},
        {"optionId": "5", "label": "superseded"},
    ],
    "timeOff.type": [
        {"optionId": "1", "label": "Vacation"},
        {"optionId": "2", "label": "Sick Leave"},
        {"optionId": "3", "label": "Personal"},
        {"optionId": "4", "label": "Bereavement"},
    ],
    # Compensation options
    "compensation.payType": [
        {"optionId": "1", "label": "Salary"},
        {"optionId": "2", "label": "Hourly"},
        {"optionId": "3", "label": "Commission"},
    ],
    "compensation.payPeriod": [
        {"optionId": "1", "label": "Weekly"},
        {"optionId": "2", "label": "Bi-Weekly"},
        {"optionId": "3", "label": "Monthly"},
        {"optionId": "4", "label": "Annually"},
    ],
}


def _init_dataset_field_options() -> None:
    """Initialize dataset field options from LIST_FIELDS.

    Populates options for employees and jobInfo datasets from shared LIST_FIELDS.
    """
    # Fields shared between employees and jobInfo datasets
    shared_fields = ["department", "division", "location", "jobTitle"]
    employee_only_fields = ["employmentStatus"]

    for list_field in LIST_FIELDS:
        alias = list_field.get("alias", "")
        options = []
        for opt in list_field.get("options", []):
            if opt.get("archived") != "yes":
                options.append({"optionId": str(opt["id"]), "label": opt["name"]})

        # Shared fields go to both employees and jobInfo datasets
        if alias in shared_fields:
            DATASET_FIELD_OPTIONS[f"employees.{alias}"] = options
            DATASET_FIELD_OPTIONS[f"jobInfo.{alias}"] = options
        # Employee-only fields
        elif alias in employee_only_fields:
            DATASET_FIELD_OPTIONS[f"employees.{alias}"] = options


# Initialize dataset options from LIST_FIELDS
_init_dataset_field_options()


async def _get_option_id_to_label_map(dataset_name: str, field_id: str) -> dict[str, str]:
    """Get mapping from option IDs to labels for an options field.

    Reads from database first, falls back to static DATASET_FIELD_OPTIONS.
    This ensures filter translation uses the same IDs returned by get_list_fields().

    Args:
        dataset_name: Name of the dataset
        field_id: ID of the options field

    Returns:
        Dictionary mapping optionId to label (e.g., {"1": "Engineering"})
    """
    from db import ListFieldOption

    # For list fields (department, location, jobTitle, division), read from DB
    list_field_aliases = {"department", "division", "location", "jobTitle"}
    if field_id in list_field_aliases:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(ListFieldOption)
                    .where(
                        ListFieldOption.field_name == field_id,
                        ListFieldOption.archived == False,  # noqa: E712
                    )
                    .order_by(ListFieldOption.sort_order, ListFieldOption.id)
                )
                db_options = result.scalars().all()
                if db_options:
                    # DB IDs are the option IDs, option_value is the label
                    return {str(opt.id): opt.option_value for opt in db_options}
        except Exception:
            # Fall back to static options if DB unavailable
            pass

    # Fall back to static DATASET_FIELD_OPTIONS
    key = f"{dataset_name}.{field_id}"
    options = DATASET_FIELD_OPTIONS.get(key, [])
    return {opt["optionId"]: opt["label"] for opt in options}


def _translate_option_ids_to_labels(filter_value: Any, option_map: dict[str, str]) -> Any:
    """Translate option IDs in a filter value to their labels.

    Args:
        filter_value: The filter value (list of option IDs or single ID)
        option_map: Mapping from option ID to label

    Returns:
        Filter value with option IDs translated to labels
    """
    if isinstance(filter_value, list):
        return [option_map.get(str(v), v) for v in filter_value]
    return option_map.get(str(filter_value), filter_value)


# Valid filter operators by field type
OPERATORS_BY_TYPE: dict[str, set[str]] = {
    "text": {"contains", "does_not_contain", "equal", "not_equal", "empty", "not_empty"},
    "date": {
        "lt",
        "lte",
        "gt",
        "gte",
        "last",
        "next",
        "range",
        "equal",
        "not_equal",
        "empty",
        "not_empty",
    },
    "int": {"equal", "not_equal", "gte", "gt", "lte", "lt", "empty", "not_empty"},
    "bool": {"checked", "not_checked"},
    "options": {"includes", "does_not_include", "empty", "not_empty"},
    "email": {"contains", "does_not_contain", "equal", "not_equal", "empty", "not_empty"},
    "phone": {"contains", "does_not_contain", "equal", "not_equal", "empty", "not_empty"},
}

# Valid aggregation functions by field type
AGGREGATIONS_BY_TYPE: dict[str, set[str]] = {
    "text": {"count"},
    "date": {"count"},
    "bool": {"count"},
    "options": {"count"},
    "int": {"count", "min", "max", "sum", "avg"},
    "email": {"count"},
    "phone": {"count"},
}


# =============================================================================
# Tool Functions
# =============================================================================
async def list_datasets() -> dict[str, Any]:
    """List all available datasets."""
    return {"datasets": list(DATASETS)}


async def get_dataset_fields(dataset_name: str) -> list[dict[str, str]]:
    """Get field definitions for a specific dataset."""
    if dataset_name not in DATASET_FIELDS:
        raise ValueError(f"Dataset '{dataset_name}' not found")
    return list(DATASET_FIELDS[dataset_name])


async def get_dataset_field_options(dataset_name: str, field_id: str) -> list[dict[str, str]]:
    """Get option values for a dataset field."""
    from db import ListFieldOption

    if dataset_name not in DATASET_FIELDS:
        raise ValueError(f"Dataset '{dataset_name}' not found")

    # Find the field definition
    field_def = None
    for field in DATASET_FIELDS[dataset_name]:
        if field["id"] == field_id:
            field_def = field
            break

    if field_def is None:
        raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

    if field_def["type"] != "options":
        raise ValueError(f"Field '{field_id}' is not an options field")

    # For list fields, read from database first to ensure consistency with filter translation
    list_field_aliases = {"department", "division", "location", "jobTitle"}
    if field_id in list_field_aliases:
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(ListFieldOption)
                    .where(
                        ListFieldOption.field_name == field_id,
                        ListFieldOption.archived == False,  # noqa: E712
                    )
                    .order_by(ListFieldOption.sort_order, ListFieldOption.id)
                )
                db_options = result.scalars().all()
                if db_options:
                    return [
                        {"optionId": str(opt.id), "label": opt.option_value} for opt in db_options
                    ]
        except Exception:
            # Fall back to static options if DB unavailable
            pass

    # Fall back to static DATASET_FIELD_OPTIONS
    key = f"{dataset_name}.{field_id}"
    return list(DATASET_FIELD_OPTIONS.get(key, []))


async def query_dataset(
    dataset_name: str,
    fields: list[str],
    filters: list[dict[str, Any]] | None = None,
    sort_by: list[dict[str, str]] | None = None,
    group_by: list[str] | None = None,
    aggregations: list[dict[str, str]] | None = None,
    matches: str = "all",
) -> dict[str, Any]:
    """Query a dataset with filtering, sorting, grouping, and aggregations."""
    # Check HR Admin permission (user_has_role returns True if auth disabled)
    if not user_has_role("hr_admin"):
        raise PermissionError("Only HR Admin can query datasets")

    # Validate matches parameter
    if matches not in {"all", "any"}:
        raise ValueError(f"Invalid matches value '{matches}'. Must be 'all' or 'any'")

    # Validate dataset
    if dataset_name not in DATASET_FIELDS:
        raise ValueError(f"Dataset '{dataset_name}' not found")

    dataset_field_defs = DATASET_FIELDS[dataset_name]
    field_map = {f["id"]: f for f in dataset_field_defs}

    # Validate requested fields
    for field_id in fields:
        if field_id not in field_map:
            raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

    # Validate filters and translate option IDs to labels for options fields.
    # DB stores values (e.g., "Engineering") but filters use option IDs (e.g., "101")
    translated_filters: list[dict[str, Any]] | None = None
    if filters:
        translated_filters = []
        for f in filters:
            field_id = f.get("field")
            operator = f.get("operator")
            value = f.get("value")

            if field_id not in field_map:
                raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

            field_type = field_map[field_id]["type"]
            valid_ops = OPERATORS_BY_TYPE.get(field_type, set())

            if operator not in valid_ops:
                raise ValueError(f"Operator '{operator}' not valid for field type '{field_type}'")

            # Validate date range
            if operator == "range" and field_type == "date":
                if isinstance(value, dict):
                    start = value.get("start", "")
                    end = value.get("end", "")
                    if start and end and start > end:
                        raise ValueError("Invalid date range: start > end")

            # Translate option IDs to labels for options fields
            # DB stores labels (e.g., "Engineering") but API filters use IDs (e.g., "1")
            translated_value = value
            if field_type == "options" and operator in {"includes", "does_not_include"}:
                option_map = await _get_option_id_to_label_map(dataset_name, field_id)
                if option_map:  # Only translate if we have mappings
                    translated_value = _translate_option_ids_to_labels(value, option_map)

            translated_filters.append(
                {
                    "field": field_id,
                    "operator": operator,
                    "value": translated_value,
                }
            )

    # Validate sort_by fields
    if sort_by:
        for sort_spec in sort_by:
            field_id = sort_spec.get("field")
            if field_id not in field_map:
                raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

    # Validate group_by fields
    if group_by:
        for field_id in group_by:
            if field_id not in field_map:
                raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

    # Validate aggregations
    if aggregations:
        for agg in aggregations:
            field_id = agg.get("field")
            func = agg.get("function")

            if field_id not in field_map:
                raise ValueError(f"Field '{field_id}' does not exist in dataset '{dataset_name}'")

            field_type = field_map[field_id]["type"]
            valid_funcs = AGGREGATIONS_BY_TYPE.get(field_type, {"count"})

            if func not in valid_funcs:
                raise ValueError(
                    f"Aggregation function '{func}' not valid for field type '{field_type}'"
                )

    # Collect all fields needed for aggregations (in addition to requested fields)
    agg_fields = set()
    if aggregations:
        for agg in aggregations:
            agg_fields.add(agg.get("field"))

    # Fetch data from real database
    all_needed_fields = set(fields) | agg_fields
    if group_by:
        all_needed_fields.update(group_by)

    # Fetch raw data from database
    raw_data = await _fetch_dataset_data(dataset_name)

    # Get filtered/sorted data WITHOUT grouping (for aggregations)
    pre_group_data = _apply_filters_and_transforms(
        raw_data, list(all_needed_fields), translated_filters, sort_by, None, matches
    )

    # Calculate aggregations on PRE-GROUPED data (original rows)
    # Keys use format "{function}_{field}" to support multiple aggregations on different fields
    agg_results: dict[str, Any] = {}
    if aggregations:
        for agg in aggregations:
            field_id = agg.get("field")
            func = agg.get("function")
            agg_key = f"{func}_{field_id}"

            if func == "count":
                agg_results[agg_key] = len(pre_group_data)
            elif func in {"sum", "avg", "min", "max"}:
                # Extract numeric values for the field
                values = []
                for row in pre_group_data:
                    val = row.get(field_id)
                    if val is not None:
                        try:
                            values.append(float(val))
                        except (ValueError, TypeError):
                            pass

                if values:
                    if func == "sum":
                        agg_results[agg_key] = sum(values)
                    elif func == "avg":
                        agg_results[agg_key] = sum(values) / len(values)
                    elif func == "min":
                        agg_results[agg_key] = min(values)
                    elif func == "max":
                        agg_results[agg_key] = max(values)

    # Now apply grouping if needed (after aggregations)
    if group_by:
        data = _apply_filters_and_transforms(
            raw_data, list(all_needed_fields), translated_filters, sort_by, group_by, matches
        )
    else:
        data = pre_group_data

    # Store original row count before grouping
    total_rows = len(pre_group_data)

    # Project data to only requested fields (after aggregations)
    # For grouped data, also include _count if present
    projected_data = []
    for row in data:
        projected_row = {}
        for field in fields:
            if field in row:
                projected_row[field] = row[field]
        # Include _count for grouped results
        if "_count" in row:
            projected_row["_count"] = row["_count"]
        projected_data.append(projected_row)

    return {
        "data": projected_data,
        "aggregations": agg_results,
        "totalRows": total_rows,
    }


async def _fetch_dataset_data(dataset_name: str) -> list[dict[str, Any]]:
    """Fetch raw data from database for a dataset.

    Args:
        dataset_name: Name of the dataset to query

    Returns:
        List of row dictionaries with camelCase field names
    """
    async with get_session() as session:
        if dataset_name == "employees":
            result = await session.execute(select(Employee))
            employees = result.scalars().all()
            return [
                {
                    "id": str(emp.id),
                    "firstName": emp.first_name,
                    "lastName": emp.last_name,
                    "displayName": emp.display_name,
                    "preferredName": emp.preferred_name,
                    "workEmail": emp.work_email,
                    "department": emp.department,
                    "division": emp.division,
                    "location": emp.location,
                    "jobTitle": emp.job_title,
                    # employmentStatus (Full-Time/Part-Time) not tracked in Employee model
                    # emp.status holds Active/Inactive/Terminated - different concept
                    "employmentStatus": None,
                    "hireDate": emp.hire_date.isoformat() if emp.hire_date else None,
                    "terminationDate": (
                        emp.termination_date.isoformat() if emp.termination_date else None
                    ),
                    "supervisorId": str(emp.supervisor_id) if emp.supervisor_id else None,
                    "status": emp.status,
                }
                for emp in employees
            ]

        elif dataset_name == "timeOff":
            result = await session.execute(
                select(TimeOffRequest, TimeOffType).join(
                    TimeOffType, TimeOffRequest.type_id == TimeOffType.id
                )
            )
            rows = result.all()
            return [
                {
                    "id": str(req.id),
                    "employeeId": str(req.employee_id),
                    "status": req.status,
                    "startDate": req.start_date.isoformat() if req.start_date else None,
                    "endDate": req.end_date.isoformat() if req.end_date else None,
                    "type": time_off_type.name,
                    "amount": float(req.amount) if req.amount is not None else None,
                    "notes": req.notes,
                    "created": req.created_at.date().isoformat() if req.created_at else None,
                }
                for req, time_off_type in rows
            ]

        elif dataset_name == "jobInfo":
            # jobInfo uses current employee job data (no historical table)
            result = await session.execute(select(Employee))
            employees = result.scalars().all()
            return [
                {
                    "id": str(emp.id),
                    "employeeId": str(emp.id),
                    "effectiveDate": emp.hire_date.isoformat() if emp.hire_date else None,
                    "department": emp.department,
                    "division": emp.division,
                    "location": emp.location,
                    "jobTitle": emp.job_title,
                    "reportsTo": str(emp.supervisor_id) if emp.supervisor_id else None,
                }
                for emp in employees
            ]

        elif dataset_name == "compensation":
            # compensation uses current employee compensation data (no historical table)
            result = await session.execute(select(Employee))
            employees = result.scalars().all()
            return [
                {
                    "id": str(emp.id),
                    "employeeId": str(emp.id),
                    "effectiveDate": emp.hire_date.isoformat() if emp.hire_date else None,
                    "payRate": float(emp.pay_rate) if emp.pay_rate is not None else None,
                    "payType": emp.pay_type,
                    "payPeriod": emp.pay_per,
                    "changeReason": None,  # Not tracked in current schema
                }
                for emp in employees
            ]

        return []


def _apply_filters_and_transforms(
    data: list[dict[str, Any]],
    fields: list[str],
    filters: list[dict[str, Any]] | None,
    sort_by: list[dict[str, str]] | None,
    group_by: list[str] | None,
    matches: str,
) -> list[dict[str, Any]]:
    """Apply filters, grouping, sorting to dataset data.

    Args:
        data: Raw data rows from database
        fields: Fields to include in output
        filters: Filter conditions
        sort_by: Sort specifications
        group_by: Group by fields
        matches: Filter match logic ("all" or "any")

    Returns:
        Processed data rows
    """
    # Apply filters
    if filters:
        filtered = []
        for row in data:
            results = []
            for f in filters:
                field_id = f.get("field")
                operator = f.get("operator")
                value = f.get("value")
                row_value = row.get(field_id)

                match = _evaluate_filter(row_value, operator, value)
                results.append(match)

            if matches == "all":
                if all(results):
                    filtered.append(row)
            else:  # matches == "any"
                if any(results):
                    filtered.append(row)
        data = filtered

    # Apply grouping (before sorting per BUILD_PLAN order: filter -> group -> aggregate -> sort)
    if group_by:
        groups: dict[tuple, list[dict]] = {}
        for row in data:
            key = tuple(row.get(f) for f in group_by)
            if key not in groups:
                groups[key] = []
            groups[key].append(row)

        # Convert groups to result rows with group fields
        grouped_data = []
        for key, group_rows in groups.items():
            group_row = {group_by[i]: key[i] for i in range(len(group_by))}
            group_row["_count"] = len(group_rows)
            grouped_data.append(group_row)
        data = grouped_data

    # Apply sorting (after grouping per BUILD_PLAN)
    if sort_by:
        for sort_spec in reversed(sort_by):
            field_id = sort_spec.get("field")
            sort_dir = str(sort_spec.get("sort", "asc")).lower()
            # Validate sort direction
            if sort_dir not in {"asc", "desc"}:
                raise ValueError(f"Invalid sort direction '{sort_dir}'. Must be 'asc' or 'desc'")
            reverse = sort_dir == "desc"
            # Use tuple key to handle None values safely with any type
            # (1, placeholder) for None sorts after (0, value) for non-None
            # This avoids TypeError when comparing None with numbers
            data = sorted(
                data,
                key=lambda x, fid=field_id: ((1, "") if x.get(fid) is None else (0, x.get(fid))),
                reverse=reverse,
            )

    # Filter to requested fields only (preserve _count for grouped results)
    result = []
    for row in data:
        filtered_row = {k: v for k, v in row.items() if k in fields or k == "_count"}
        result.append(filtered_row)

    return result


def _evaluate_filter(row_value: Any, operator: str, filter_value: Any) -> bool:
    """Evaluate a single filter condition."""
    if operator == "contains":
        if row_value is None:
            return False
        return str(filter_value).lower() in str(row_value).lower()

    if operator == "does_not_contain":
        if row_value is None:
            return True
        return str(filter_value).lower() not in str(row_value).lower()

    if operator == "equal":
        return row_value == filter_value

    if operator == "not_equal":
        return row_value != filter_value

    if operator == "empty":
        return row_value is None or row_value == ""

    if operator == "not_empty":
        return row_value is not None and row_value != ""

    if operator == "includes":
        # For options fields, value is list of option IDs
        if row_value is None:
            return False
        if isinstance(filter_value, list):
            return str(row_value) in filter_value or row_value in filter_value
        return str(row_value) == str(filter_value)

    if operator == "does_not_include":
        if row_value is None:
            return True
        if isinstance(filter_value, list):
            return str(row_value) not in filter_value and row_value not in filter_value
        return str(row_value) != str(filter_value)

    if operator == "range":
        if row_value is None:
            return False
        if isinstance(filter_value, dict):
            start = filter_value.get("start")
            end = filter_value.get("end")
            row_str = str(row_value)
            # Only apply bounds that are specified (missing bound = unbounded)
            if start and end:
                return start <= row_str <= end
            elif start:
                return start <= row_str
            elif end:
                return row_str <= end
            else:
                # No bounds specified, match all
                return True
        return False

    if operator in {"lt", "lte", "gt", "gte"}:
        if row_value is None:
            return False
        # Coerce types to prevent TypeError on mixed type comparison
        # Try numeric comparison first, fall back to string comparison
        try:
            row_num = float(row_value)
            filter_num = float(filter_value)
            if operator == "lt":
                return row_num < filter_num
            if operator == "lte":
                return row_num <= filter_num
            if operator == "gt":
                return row_num > filter_num
            if operator == "gte":
                return row_num >= filter_num
        except (ValueError, TypeError):
            # Fall back to string comparison
            row_str = str(row_value)
            filter_str = str(filter_value)
            if operator == "lt":
                return row_str < filter_str
            if operator == "lte":
                return row_str <= filter_str
            if operator == "gt":
                return row_str > filter_str
            if operator == "gte":
                return row_str >= filter_str

    if operator == "checked":
        return row_value is True

    if operator == "not_checked":
        return row_value is not True

    if operator == "last":
        # "last N days" - date is within the past N days
        if row_value is None:
            return False
        try:
            days = int(filter_value) if filter_value else 0
            today = date.today()
            cutoff = today - timedelta(days=days)
            row_date = datetime.strptime(str(row_value)[:10], "%Y-%m-%d").date()
            return cutoff <= row_date <= today
        except (ValueError, TypeError):
            return False

    if operator == "next":
        # "next N days" - date is within the next N days
        if row_value is None:
            return False
        try:
            days = int(filter_value) if filter_value else 0
            today = date.today()
            cutoff = today + timedelta(days=days)
            row_date = datetime.strptime(str(row_value)[:10], "%Y-%m-%d").date()
            return today <= row_date <= cutoff
        except (ValueError, TypeError):
            return False

    return False


__all__ = [
    "list_datasets",
    "get_dataset_fields",
    "get_dataset_field_options",
    "query_dataset",
]
