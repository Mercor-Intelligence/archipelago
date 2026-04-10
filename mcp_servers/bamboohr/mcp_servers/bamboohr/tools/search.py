"""Search tools for BambooHR MCP server.

Implements:
- bamboo.search.employees: Search employees with fuzzy matching
- bamboo.search.time_off: Search time-off requests
- bamboo.search.metadata: Search metadata entities

Per BUILD_PLAN sections 3.2.32-3.2.34.
"""

from datetime import date
from typing import Any

from constants import get_all_fields
from db import Employee, ListFieldOption, TimeOffRequest, TimeOffType, get_session
from mcp_auth import public_tool, require_scopes
from schemas.search import (
    DEFAULT_EMPLOYEE_SEARCH_FIELDS,
    VALID_EMPLOYEE_SEARCH_FIELDS,
    VALID_ENTITY_TYPES,
    VALID_TIME_OFF_STATUSES,
)
from sqlalchemy import select

from .auth_helpers import get_user_context
from .constants import FIELD_ALIAS_MAP, FIELD_ALIAS_MAP_REVERSE

# Valid filter keys for employee search
VALID_EMPLOYEE_FILTER_KEYS = {"department", "status", "location", "job_title"}

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def _parse_query_tokens(query: str) -> list[str]:
    """Parse query string into tokens.

    Args:
        query: Search query string

    Returns:
        List of lowercase tokens
    """
    if not query:
        return []
    # Truncate to 200 chars max
    query = query[:200]
    # Split on whitespace and filter empty
    return [token.lower().strip() for token in query.split() if token.strip()]


def _calculate_match_score(searchable_values: list[str | None], tokens: list[str]) -> float:
    """Calculate match score for an entity against query tokens.

    Match scoring per BUILD_PLAN:
    - Exact match = 1.0
    - Prefix match = 0.8
    - Contains = 0.6
    - Fuzzy (Levenshtein distance <= 2) = 0.5

    Args:
        searchable_values: List of field values to search
        tokens: List of query tokens

    Returns:
        Normalized match score (0-1)
    """
    if not tokens:
        return 1.0  # Empty query matches everything

    total_score = 0.0

    for token in tokens:
        field_scores = []
        for value in searchable_values:
            if not value:
                continue
            value_lower = value.lower()

            if token == value_lower:
                field_scores.append(1.0)  # Exact match
            elif value_lower.startswith(token):
                field_scores.append(0.8)  # Prefix match
            elif token in value_lower:
                field_scores.append(0.6)  # Contains
            else:
                # Simple fuzzy matching using Levenshtein-like distance
                distance = _levenshtein_distance(token, value_lower)
                if distance <= 2:
                    field_scores.append(0.5)

        if field_scores:
            total_score += max(field_scores)  # Best match for this token

    return total_score / len(tokens) if tokens else 1.0


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein distance between two strings.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Edit distance between strings
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    # Use dynamic programming for efficiency
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _normalize_field_name(field: str) -> str:
    """Convert camelCase field name to snake_case."""
    return FIELD_ALIAS_MAP.get(field, field)


def _to_camel_case(snake_str: str) -> str:
    """Convert snake_case to camelCase."""
    return FIELD_ALIAS_MAP_REVERSE.get(snake_str, snake_str)


# =============================================================================
# SEARCH EMPLOYEES
# =============================================================================


@require_scopes("read:employees")
async def search_employees(
    query: str = "",
    fields: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Search employees using flexible query criteria with fuzzy matching."""
    # Validate pagination
    if page_size < 1 or page_size > 100:
        return {"error": {"code": 422, "message": "pageSize must be between 1 and 100"}}
    if page < 1:
        return {"error": {"code": 422, "message": "page must be >= 1"}}

    # Normalize incoming field names to snake_case before validation
    if fields:
        fields = [_normalize_field_name(f) for f in fields]
        for field in fields:
            if field not in VALID_EMPLOYEE_SEARCH_FIELDS:
                return {"error": {"code": 422, "message": f"Invalid field: {field}"}}

    # Normalize filter keys to snake_case before validation
    filters = {_normalize_field_name(k): v for k, v in (filters or {}).items()}
    for filter_key in filters:
        if filter_key not in VALID_EMPLOYEE_FILTER_KEYS:
            return {"error": {"code": 422, "message": f"Invalid filter: {filter_key}"}}

    # Get user context
    user_employee_id, persona = get_user_context()

    # Parse query tokens
    tokens = _parse_query_tokens(query)

    async with get_session() as session:
        # Build base query with persona filtering
        base_query = select(Employee)

        if persona == "hr_admin":
            # HR Admin sees all employees
            pass
        elif persona == "manager":
            # Manager sees direct reports only
            if user_employee_id is not None:
                base_query = base_query.where(Employee.supervisor_id == user_employee_id)
            else:
                return {"employees": [], "page": page, "pageSize": page_size, "total": 0}
        elif persona == "employee":
            # Employee sees self only
            if user_employee_id is not None:
                base_query = base_query.where(Employee.id == user_employee_id)
            else:
                return {"employees": [], "page": page, "pageSize": page_size, "total": 0}
        else:
            return {"error": {"code": 403, "message": "Insufficient permissions"}}

        # Apply filters (exact match, AND logic)
        if filters.get("department"):
            base_query = base_query.where(Employee.department == filters["department"])
        if filters.get("status"):
            base_query = base_query.where(Employee.status == filters["status"])
        if filters.get("location"):
            base_query = base_query.where(Employee.location == filters["location"])
        if filters.get("job_title"):
            base_query = base_query.where(Employee.job_title == filters["job_title"])

        # Execute query
        result = await session.execute(base_query)
        all_employees = list(result.scalars().all())

        # Calculate match scores and filter
        scored_employees = []
        for emp in all_employees:
            searchable = [
                emp.first_name,
                emp.last_name,
                emp.work_email,
                emp.department,
                emp.job_title,
            ]
            score = _calculate_match_score(searchable, tokens)

            # Include if score > 0 or no tokens (empty query)
            if score > 0 or not tokens:
                scored_employees.append((emp, score))

        # Sort by match score descending
        scored_employees.sort(key=lambda x: x[1], reverse=True)

        # Calculate total before pagination
        total = len(scored_employees)

        # Apply pagination
        offset = (page - 1) * page_size
        paginated = scored_employees[offset : offset + page_size]

        # Determine fields to return
        return_fields = fields or DEFAULT_EMPLOYEE_SEARCH_FIELDS

        # Build response
        employees = []
        for emp, score in paginated:
            emp_dict: dict[str, Any] = {
                "id": str(emp.id),
                "matchScore": round(score, 2),
            }

            # Add requested fields (all keys are snake_case since inputs are normalized)
            field_mapping = {
                "first_name": emp.first_name,
                "last_name": emp.last_name,
                "preferred_name": emp.preferred_name or emp.first_name,
                "department": emp.department,
                "email": emp.work_email,
                "work_email": emp.work_email,
                "job_title": emp.job_title,
                "location": emp.location,
                "status": emp.status,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
            }

            for field in return_fields:
                if field in field_mapping:
                    # Use camelCase for output
                    camel_field = _to_camel_case(_normalize_field_name(field))
                    # Handle special case for email
                    if field in ("email", "workEmail", "work_email"):
                        emp_dict["email"] = field_mapping[field]
                    else:
                        emp_dict[camel_field] = field_mapping[field]

            employees.append(emp_dict)

        return {
            "employees": employees,
            "page": page,
            "pageSize": page_size,
            "total": total,
        }


# =============================================================================
# SEARCH TIME-OFF
# =============================================================================


@require_scopes("read:time_off")
async def search_time_off(
    query: str = "",
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Search time-off requests using flexible criteria."""
    # Validate pagination
    if page_size < 1 or page_size > 100:
        return {"error": {"code": 422, "message": "pageSize must be between 1 and 100"}}
    if page < 1:
        return {"error": {"code": 422, "message": "page must be >= 1"}}

    # Get user context
    user_employee_id, persona = get_user_context()

    # Parse query tokens
    tokens = _parse_query_tokens(query)

    # Default filters
    filters = filters or {}

    # Validate and normalize status filter
    if filters.get("status"):
        statuses = filters["status"]
        if isinstance(statuses, str):
            statuses = [s.strip().lower() for s in statuses.split(",")]
        else:
            # Normalize list values to lowercase
            statuses = [s.lower() if isinstance(s, str) else s for s in statuses]
        for status in statuses:
            if status not in VALID_TIME_OFF_STATUSES:
                return {"error": {"code": 422, "message": f"Invalid status: {status}"}}
        # Store normalized list back to filters
        filters["status"] = statuses

    # Validate date filters
    start_date_filter = None
    end_date_filter = None
    if filters.get("startDate"):
        try:
            start_date_filter = date.fromisoformat(filters["startDate"])
        except ValueError:
            return {"error": {"code": 422, "message": "Invalid date format (expected YYYY-MM-DD)"}}

    if filters.get("endDate"):
        try:
            end_date_filter = date.fromisoformat(filters["endDate"])
        except ValueError:
            return {"error": {"code": 422, "message": "Invalid date format (expected YYYY-MM-DD)"}}

    if start_date_filter and end_date_filter and start_date_filter > end_date_filter:
        return {"error": {"code": 422, "message": "startDate must be <= endDate"}}

    async with get_session() as session:
        # Build base query with JOINs for employee and type info
        base_query = (
            select(TimeOffRequest, Employee, TimeOffType)
            .join(Employee, TimeOffRequest.employee_id == Employee.id)
            .join(TimeOffType, TimeOffRequest.type_id == TimeOffType.id)
        )

        # Apply persona filtering
        if persona == "hr_admin":
            # HR Admin sees all requests
            pass
        elif persona == "manager":
            # Manager sees direct reports only
            if user_employee_id is not None:
                base_query = base_query.where(Employee.supervisor_id == user_employee_id)
            else:
                return {"requests": [], "page": page, "pageSize": page_size, "total": 0}
        elif persona == "employee":
            # Employee sees own requests only
            if user_employee_id is not None:
                base_query = base_query.where(TimeOffRequest.employee_id == user_employee_id)
            else:
                return {"requests": [], "page": page, "pageSize": page_size, "total": 0}
        else:
            return {"error": {"code": 403, "message": "Insufficient permissions"}}

        # Apply status filter (already normalized above)
        if filters.get("status"):
            base_query = base_query.where(TimeOffRequest.status.in_(filters["status"]))

        # Apply type filter
        if filters.get("type"):
            try:
                type_id = int(filters["type"])
                base_query = base_query.where(TimeOffRequest.type_id == type_id)
            except ValueError:
                return {"error": {"code": 422, "message": "Invalid time-off type ID"}}

        # Apply date range filter (overlapping logic)
        if start_date_filter:
            base_query = base_query.where(TimeOffRequest.end_date >= start_date_filter)
        if end_date_filter:
            base_query = base_query.where(TimeOffRequest.start_date <= end_date_filter)

        # Execute query
        result = await session.execute(base_query)
        all_rows = list(result.all())

        # Calculate match scores
        scored_requests = []
        for row in all_rows:
            request, employee, time_off_type = row
            employee_name = f"{employee.first_name} {employee.last_name}"

            searchable = [
                employee_name,
                time_off_type.name,
                request.notes,
            ]
            score = _calculate_match_score(searchable, tokens)

            if score > 0 or not tokens:
                scored_requests.append((request, employee, time_off_type, score))

        # Sort by match score descending, then by start date descending
        scored_requests.sort(key=lambda x: (-x[3], -x[0].start_date.toordinal()))

        # Calculate total before pagination
        total = len(scored_requests)

        # Apply pagination
        offset = (page - 1) * page_size
        paginated = scored_requests[offset : offset + page_size]

        # Build response
        requests = []
        for request, employee, time_off_type, score in paginated:
            amount_str = f"{request.amount} {request.units}"
            requests.append(
                {
                    "id": str(request.id),
                    "employeeId": str(request.employee_id),
                    "employeeName": f"{employee.first_name} {employee.last_name}",
                    "type": time_off_type.name,
                    "start": request.start_date.isoformat(),
                    "end": request.end_date.isoformat(),
                    "status": request.status,
                    "amount": amount_str,
                    "matchScore": round(score, 2),
                }
            )

        return {
            "requests": requests,
            "page": page,
            "pageSize": page_size,
            "total": total,
        }


# =============================================================================
# SEARCH METADATA
# =============================================================================


@public_tool
async def search_metadata(
    query: str = "",
    entity_types: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Search across metadata entities with fuzzy matching."""
    # Validate pagination
    if page_size < 1 or page_size > 100:
        return {"error": {"code": 422, "message": "pageSize must be between 1 and 100"}}
    if page < 1:
        return {"error": {"code": 422, "message": "page must be >= 1"}}

    # Validate query length
    if len(query) > 200:
        return {"error": {"code": 422, "message": "Query exceeds maximum length of 200 characters"}}

    # Validate entity types
    if entity_types:
        for et in entity_types:
            if et not in VALID_ENTITY_TYPES:
                return {
                    "error": {
                        "code": 422,
                        "message": f"Invalid entityType: {et}. Allowed: fields, listOptions, users",
                    }
                }
    else:
        # Default: search all types
        entity_types = list(VALID_ENTITY_TYPES)

    # Parse query tokens
    tokens = _parse_query_tokens(query)

    all_results: list[tuple[dict[str, Any], float]] = []

    # Search fields (no DB query needed - static data)
    if "fields" in entity_types:
        for field in get_all_fields():
            searchable = [field.field_id, field.field_name, field.alias]
            score = _calculate_match_score(searchable, tokens)
            if score > 0 or not tokens:
                all_results.append(
                    (
                        {
                            "entityType": "field",
                            "id": field.field_id,
                            "name": field.field_name,
                            "type": field.field_type,
                            "matchScore": round(score, 2),
                        },
                        score,
                    )
                )

    async with get_session() as session:
        # Search list options
        if "listOptions" in entity_types:
            option_results = await session.execute(select(ListFieldOption))
            options = list(option_results.scalars().all())

            for option in options:
                searchable = [option.option_value]
                score = _calculate_match_score(searchable, tokens)
                if score > 0 or not tokens:
                    all_results.append(
                        (
                            {
                                "entityType": "listOption",
                                "id": str(option.id),
                                "fieldId": option.field_name,
                                "optionId": str(option.id),
                                "label": option.option_value,
                                "matchScore": round(score, 2),
                            },
                            score,
                        )
                    )

        # Search users (employees)
        if "users" in entity_types:
            user_results = await session.execute(select(Employee))
            users = list(user_results.scalars().all())

            for user in users:
                searchable = [user.first_name, user.last_name, user.work_email]
                score = _calculate_match_score(searchable, tokens)
                if score > 0 or not tokens:
                    all_results.append(
                        (
                            {
                                "entityType": "user",
                                "id": str(user.id),
                                "name": f"{user.first_name} {user.last_name}",
                                "email": user.work_email,
                                "firstName": user.first_name,
                                "lastName": user.last_name,
                                "matchScore": round(score, 2),
                            },
                            score,
                        )
                    )

    # Sort by match score descending
    all_results.sort(key=lambda x: x[1], reverse=True)

    # Calculate total before pagination
    total = len(all_results)

    # Apply pagination
    offset = (page - 1) * page_size
    paginated = all_results[offset : offset + page_size]

    # Extract just the result dicts
    results = [r[0] for r in paginated]

    return {
        "results": results,
        "page": page,
        "pageSize": page_size,
        "total": total,
    }


__all__ = ["search_employees", "search_metadata", "search_time_off"]
