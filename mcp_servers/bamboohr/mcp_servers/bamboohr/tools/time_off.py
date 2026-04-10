"""Time-off tools for BambooHR MCP server.

Implements:
- bamboo.time_off.get_types: Get time-off types with default hours configuration
- bamboo.time_off.create_type: Create a new time-off type (HR Admin only)

Per BUILD_PLAN section 3.2.17:
- Get types endpoint is accessible to all personas (public)
- Returns timeOffTypes with icon mappings
- Returns static defaultHours configuration
"""

from datetime import UTC, datetime

from db import TimeOffType, get_session
from mcp_auth import require_roles, require_scopes
from schemas.time_off import (
    CreateTimeOffTypeRequest,
    CreateTimeOffTypeResponse,
    GetTypesResponse,
)
from sqlalchemy import select

# Icon mapping for common time-off types
# Maps type name (case-insensitive) to BambooHR icon identifier
ICON_MAP: dict[str, str] = {
    "vacation": "palm-trees",
    "sick": "medical",
    "sick leave": "medical",
    "personal": "calendar",
    "unpaid leave": "calendar-x",
    "bereavement": "heart",
    "jury duty": "gavel",
    "parental leave": "baby",
    "medical leave": "medical-cross",
    "maternity": "baby",
    "paternity": "baby",
}

# Default color for types without a color (gray)
DEFAULT_COLOR = "9E9E9E"

# Static default hours configuration
DEFAULT_HOURS: list[dict[str, str]] = [
    {"name": "Saturday", "amount": "0"},
    {"name": "Sunday", "amount": "0"},
    {"name": "default", "amount": "8"},
]


def _get_icon_for_type(type_name: str) -> str:
    """Get icon identifier for a time-off type.

    Maps common type names to BambooHR icon identifiers.
    Falls back to "calendar" for unknown types.

    Args:
        type_name: Name of the time-off type

    Returns:
        Icon identifier string
    """
    normalized_name = type_name.lower().strip()
    return ICON_MAP.get(normalized_name, "calendar")


def _strip_hash_from_color(color: str | None) -> str:
    """Remove # prefix from hex color code.

    BambooHR API returns colors without # prefix (e.g., "4CAF50" not "#4CAF50").

    Args:
        color: Hex color code, possibly with # prefix, or None

    Returns:
        Hex color without # prefix, or default color if None
    """
    if color is None:
        return DEFAULT_COLOR

    return color.lstrip("#")


async def get_types(mode: str | None = None) -> dict:
    """Get all time-off types with default hours configuration."""
    async with get_session() as session:
        # Query all time-off types from database
        query = select(TimeOffType).order_by(TimeOffType.id)
        result = await session.execute(query)
        db_types = list(result.scalars().all())

    # Convert database models to API format
    time_off_types = [
        {
            "id": str(t.id),  # BambooHR API uses string IDs
            "name": t.name,
            "units": t.units,
            "color": _strip_hash_from_color(t.color),
            "icon": _get_icon_for_type(t.name),
        }
        for t in db_types
    ]

    # Validate response with Pydantic schema
    response = GetTypesResponse(
        timeOffTypes=time_off_types,
        defaultHours=DEFAULT_HOURS,
    )
    return response.model_dump(by_alias=True)


@require_roles("hr_admin")
@require_scopes("write:time_off")
async def create_type(request: CreateTimeOffTypeRequest) -> CreateTimeOffTypeResponse:
    """Create a new time-off type."""
    async with get_session() as session:
        # Check for duplicate name
        existing = await session.execute(
            select(TimeOffType).where(TimeOffType.name == request.name)
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"Time-off type '{request.name}' already exists")

        # Normalize color (remove # prefix if present)
        color = request.color
        if color and color.startswith("#"):
            color = color[1:]

        # Create new type
        new_type = TimeOffType(
            name=request.name,
            color=color,
            paid=request.paid,
            units=request.units,
        )
        session.add(new_type)
        await session.flush()  # Get the ID
        await session.commit()

        # Return response
        created_timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return CreateTimeOffTypeResponse(
            id=str(new_type.id),
            name=new_type.name,
            created=created_timestamp,
        )


__all__ = ["get_types", "create_type"]
