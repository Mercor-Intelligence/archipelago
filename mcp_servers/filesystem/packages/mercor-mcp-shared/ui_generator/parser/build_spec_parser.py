"""
Build Spec Parser - Read and validate mcp-build-spec.yaml files.

Supports merging a base build spec with application-specific specs.
The base spec provides common categories (e.g., Database) and tool overrides
that all applications inherit automatically.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

# Path to the base build spec (relative to this file)
BASE_SPEC_PATH = Path(__file__).parent.parent / "base-build-spec.yaml"


class ServerConfig(BaseModel):
    """Configuration for an MCP server."""

    name: str
    display_name: str
    description: str
    tools_module: str
    base_url: str
    requires_auth: bool = True
    auth_type: str = "api_key"
    icon: str = ""


class CategoryConfig(BaseModel):
    """Configuration for a tool category."""

    name: str
    description: str
    servers: list[str]


class ToolOverride(BaseModel):
    """Override configuration for specific tools."""

    tool: str
    category: str | None = None
    display_name: str | None = None
    icon: str | None = None
    priority: int | None = None
    hidden: bool = False


class BuildSpec(BaseModel):
    """Complete build specification."""

    version: str
    project_name: str | None = None
    servers: list[ServerConfig]
    categories: list[CategoryConfig] = Field(default_factory=list)
    tool_overrides: list[ToolOverride] = Field(default_factory=list)


def _load_base_spec() -> dict[str, Any] | None:
    """Load the base build spec if it exists."""
    if BASE_SPEC_PATH.exists():
        with open(BASE_SPEC_PATH) as f:
            return yaml.safe_load(f)
    return None


def _merge_specs(base: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    """
    Merge base spec with application spec.

    Application spec takes precedence for scalar values.
    Lists (categories, tool_overrides) are combined with app items first.

    For categories with servers: [], they inherit the first server from app spec.
    """
    result = app.copy()

    # Get the first server name from app spec for category inheritance
    first_server = None
    if app.get("servers") and len(app["servers"]) > 0:
        first_server = (
            app["servers"][0].get("name") if isinstance(app["servers"][0], dict) else None
        )

    # Merge categories - base categories come after app categories
    base_categories = base.get("categories", [])
    app_categories = app.get("categories", [])
    app_category_names = {c["name"] for c in app_categories}

    merged_categories = list(app_categories)
    for cat in base_categories:
        if cat["name"] not in app_category_names:
            # Inherit server if category has empty servers list
            if first_server and (not cat.get("servers") or cat["servers"] == []):
                cat = cat.copy()
                cat["servers"] = [first_server]
            merged_categories.append(cat)

    result["categories"] = merged_categories

    # Merge tool_overrides - base overrides come after app overrides
    base_overrides = base.get("tool_overrides", [])
    app_overrides = app.get("tool_overrides", [])
    app_override_tools = {o["tool"] for o in app_overrides}

    merged_overrides = list(app_overrides)
    for override in base_overrides:
        if override["tool"] not in app_override_tools:
            merged_overrides.append(override)

    result["tool_overrides"] = merged_overrides

    return result


class BuildSpecParser:
    """Parse and validate mcp-build-spec.yaml files."""

    def parse(self, filepath: str | Path, merge_base: bool = True) -> BuildSpec:
        """
        Parse and validate build spec from YAML file.

        Args:
            filepath: Path to mcp-build-spec.yaml
            merge_base: Whether to merge with base-build-spec.yaml (default: True)

        Returns:
            Validated BuildSpec object

        Raises:
            FileNotFoundError: If file doesn't exist
            ValidationError: If spec is invalid
            yaml.YAMLError: If YAML is malformed
        """
        filepath = Path(filepath)

        if not filepath.exists():
            raise FileNotFoundError(f"Build spec not found: {filepath}")

        with open(filepath) as f:
            data = yaml.safe_load(f)

        # Handle empty or commented-out YAML files
        if data is None:
            raise ValueError(f"Build spec file is empty or contains only comments: {filepath}")

        # Merge with base spec if requested
        if merge_base:
            base_data = _load_base_spec()
            if base_data:
                data = _merge_specs(base_data, data)

        try:
            spec = BuildSpec(**data)
            return spec
        except ValidationError:
            # Re-raise the original ValidationError with all validation details
            raise

    def parse_dict(self, data: dict[str, Any]) -> BuildSpec:
        """Parse build spec from dictionary."""
        return BuildSpec(**data)

    def validate_file(self, filepath: str | Path) -> tuple[bool, str]:
        """
        Validate build spec file without raising exceptions.

        Args:
            filepath: Path to build spec file

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            self.parse(filepath)
            return (True, "")
        except Exception as e:
            return (False, str(e))
