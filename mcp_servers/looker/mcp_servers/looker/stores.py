"""Runtime state stores for Looker content.

These in-memory stores hold user-created content (looks, dashboards, queries)
and dynamically generated schema metadata (models, explores from CSVs).

All stores start empty and are populated at runtime when:
- User uploads CSVs -> models/explores generated
- User calls create_query -> query stored
- User calls create_look -> look stored
- User calls create_dashboard -> dashboard stored
"""

from dataclasses import dataclass

from models import (
    ContentSearchResult,
    ExploreResponse,
    Folder,
    Look,
    LookMLModel,
)

# =============================================================================
# Schema Stores (populated from user-uploaded CSVs)
# =============================================================================

# LookML Models - populated dynamically via data_layer
MODELS: list[LookMLModel] = []

# Explores with field definitions - populated dynamically via data_layer
EXPLORES: dict[tuple[str, str], ExploreResponse] = {}

# =============================================================================
# Content Stores (populated by user actions)
# =============================================================================

# Default folder for saving content
DEFAULT_FOLDERS = [
    Folder(id="1", name="My Folder", parent_id=None, child_count=0),
]

# Looks created by users
LOOKS: list[Look] = []

# Content search results
SEARCH_RESULTS: dict[str, list[ContentSearchResult]] = {}


@dataclass
class DashboardTile:
    id: str
    title: str
    type: str  # "bar", "line", "table", "single_value"
    query: dict  # Full query definition


@dataclass
class Dashboard:
    id: int
    title: str
    description: str
    tiles: list[DashboardTile]
    filters: list[dict]  # Dashboard-level filters
    folder_id: str
    created_at: str
    updated_at: str


# Dashboards created by users
DASHBOARDS: dict[int, Dashboard] = {}


# =============================================================================
# Dynamic Getters (combine static stores with data_layer)
# =============================================================================


def get_all_models() -> list[LookMLModel]:
    """Get all LookML models including dynamically loaded user models.

    Combines the static MODELS store with any user-uploaded CSV data
    that has been converted to LookML models via data_layer.

    Returns:
        Combined list of LookMLModel objects
    """
    try:
        from data_layer import get_lookml_models

        user_models = get_lookml_models()
        if user_models:
            return MODELS + user_models
    except ImportError:
        pass

    return MODELS


def get_all_explores() -> dict[tuple[str, str], ExploreResponse]:
    """Get all explores including dynamically loaded user explores.

    Combines the static EXPLORES store with any user-uploaded CSV data
    that has been converted to explore definitions via data_layer.

    Returns:
        Combined dict mapping (model_name, explore_name) to ExploreResponse
    """
    try:
        from data_layer import get_lookml_explores

        user_explores = get_lookml_explores()
        if user_explores:
            result = dict(EXPLORES)
            result.update(user_explores)
            return result
    except ImportError:
        pass

    return EXPLORES
