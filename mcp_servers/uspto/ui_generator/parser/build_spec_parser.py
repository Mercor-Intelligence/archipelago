"""
Build Spec Parser - Read and validate mcp-build-spec.yaml files.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError


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
    requires_auth: bool | None = None  # None means inherit from server


class BuildSpec(BaseModel):
    """Complete build specification."""

    version: str
    project_name: str | None = None
    servers: list[ServerConfig]
    categories: list[CategoryConfig] = Field(default_factory=list)
    tool_overrides: list[ToolOverride] = Field(default_factory=list)


class BuildSpecParser:
    """Parse and validate mcp-build-spec.yaml files."""

    def parse(self, filepath: str | Path) -> BuildSpec:
        """
        Parse and validate build spec from YAML file.

        Args:
            filepath: Path to mcp-build-spec.yaml

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
