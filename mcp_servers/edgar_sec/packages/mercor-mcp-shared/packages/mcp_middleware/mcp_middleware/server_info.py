"""Server information tool for MCP servers.

Provides a standardized server_info tool that returns server configuration
including authentication status. This tool is automatically registered by
run_server() and is always public (no auth required).

The UI uses this to determine whether to show the login button.

This replaces the per-app server.py files in greenhouse/eightfold with a
shared implementation that reads auth status from environment variables.
"""

from typing import TYPE_CHECKING, Any

from mcp_auth import is_auth_configured, public_tool
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from fastmcp import FastMCP


class ServerInfoInput(BaseModel):
    """Input for server info tool (no parameters required)."""

    model_config = ConfigDict(json_schema_extra={"type": "object", "properties": {}})


class ServerFeatures(BaseModel):
    """Server feature flags and capabilities."""

    authentication: bool = Field(default=False, description="Whether authentication is enabled")
    personas: list[str] | None = Field(default=None, description="Available user personas/roles")
    persistence: str | None = Field(default=None, description="Data persistence type")
    api_compatibility: str | None = Field(default=None, description="External API compatibility")


class ToolEntry(BaseModel):
    """A tool entry within a category.

    For individual tools: just the name
    For meta tools: name + list of available actions
    """

    name: str = Field(..., description="Tool name")
    actions: list[str] | None = Field(
        default=None, description="Available actions (for meta tools only)"
    )


class ToolCategory(BaseModel):
    """A category of tools with their available operations."""

    name: str = Field(..., description="Category name (e.g., 'candidates', 'applications')")
    tools: list[ToolEntry] = Field(..., description="Tools in this category")


class ServerInfoResponse(BaseModel):
    """Server information response following established patterns from greenhouse/eightfold."""

    name: str = Field(..., description="Server name")
    version: str = Field(..., description="Server version")
    description: str = Field(default="", description="Server description")
    status: str = Field(default="running", description="Server status (running/stopped)")
    features: ServerFeatures = Field(..., description="Server features and capabilities")
    tool_categories: list[ToolCategory] | None = Field(
        default=None, description="Available tool categories and their actions"
    )


def register_server_info_tool(
    mcp_instance: "FastMCP",
    *,
    config: Any = None,
) -> None:
    """Register the server_info tool with the MCP instance.

    This tool is always public (no auth required) and returns server
    configuration including whether authentication is enabled.

    Args:
        mcp_instance: The FastMCP instance to register the tool with
        config: Optional ServerConfig with name, version, description, and features.
               If provided, these values are used for the response.
               If None, metadata is read from the mcp_instance attributes.
    """
    # Get server metadata from config or fall back to MCP instance
    if config is not None:
        server_name = config.name
        server_version = config.version
        server_description = config.description
        extra_features = config.features or {}
    else:
        server_name = getattr(mcp_instance, "name", "mcp-server")
        server_version = getattr(mcp_instance, "version", "0.0.0")
        server_description = getattr(mcp_instance, "instructions", "") or ""
        extra_features = {}

    # Build ServerFeatures model
    features = ServerFeatures(
        authentication=is_auth_configured(),
        personas=extra_features.get("personas"),
        persistence=extra_features.get("persistence"),
        api_compatibility=extra_features.get("api_compatibility"),
    )

    # Build tool_categories by iterating through registered tools
    # registered_tools: list of registered tool names
    # tool_to_category: dict mapping tool name to category name
    # meta_tool_actions: dict mapping meta tool name to list of action names
    registered_tools = extra_features.get("registered_tools", [])
    tool_to_category = extra_features.get("tool_to_category", {})
    meta_tool_actions = extra_features.get("meta_tool_actions", {})

    tool_categories: list[ToolCategory] | None = None
    if registered_tools:
        # Group tools by category
        category_tools: dict[str, list[ToolEntry]] = {}

        for tool_name in registered_tools:
            # Get category: from build spec, or use tool name for meta tools
            category = tool_to_category.get(tool_name)
            if category is None and tool_name in meta_tool_actions:
                # Meta tool not in build spec: use tool name as category
                category = tool_name

            if category is None:
                # Tool not categorized, skip it
                continue

            # Get actions if it's a meta tool
            actions = meta_tool_actions.get(tool_name)

            # Create ToolEntry and add to category
            tool_entry = ToolEntry(name=tool_name, actions=actions)
            if category not in category_tools:
                category_tools[category] = []
            category_tools[category].append(tool_entry)

        # Build ToolCategory list
        if category_tools:
            tool_categories = [
                ToolCategory(name=name, tools=tools) for name, tools in category_tools.items()
            ]

    @mcp_instance.tool(name="server_info")
    @public_tool
    async def server_info(input: ServerInfoInput = ServerInfoInput()) -> ServerInfoResponse:
        """Get server information and configuration (public, no auth required).

        Returns server metadata and feature flags including whether
        authentication is enabled. The UI uses this to determine
        whether to show the login workflow.
        """
        return ServerInfoResponse(
            name=server_name,
            version=server_version,
            description=server_description,
            status="running",
            features=features,
            tool_categories=tool_categories,
        )


__all__ = [
    "ServerFeatures",
    "ServerInfoInput",
    "ServerInfoResponse",
    "ToolCategory",
    "ToolEntry",
    "register_server_info_tool",
]
