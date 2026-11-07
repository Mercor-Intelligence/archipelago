"""Task-level tool filtering middleware.

Filters MCP tools by sub-app group based on a per-task configuration file.
When a tool_filter.json config exists with an ``enabled_groups`` allowlist,
only tools belonging to those groups are visible and callable. Tools not
assigned to any group (e.g., health/echo probes) are always available.

Configuration is read from a well-known path:
    /.apps_data/{app_name}/.config/tool_filter.json

Usage:
    In each Foundry app's main.py, define tool groups and call setup:

        TOOL_GROUPS = {
            "jira": {"createJiraIssue", "editJiraIssue", ...},
            "bitbucket": {"bitbucketRepository_get", ...},
            "confluence": {"space_list", "page_create", ...},
        }
        setup_tool_filter(mcp, tool_groups=TOOL_GROUPS)

    For apps with no sub-app groups (e.g. Zoho CRM), call without groups:

        setup_tool_filter(mcp)
"""

import logging
import os
from collections.abc import Sequence

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import ListToolsRequest
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ToolFilterConfig(BaseModel):
    """Tool filter configuration for a task.

    Attributes:
        enabled_groups: List of group names whose tools should be available.
                       If empty or config absent, all tools are available.
    """

    enabled_groups: list[str] = Field(
        default_factory=list,
        description="Sub-app groups to enable. Only tools in these groups are available.",
    )


class ToolFilterMiddleware(Middleware):
    """FastMCP middleware that filters tools by sub-app group.

    Tools belonging to a disabled group are:
    - Hidden from ``tools/list`` responses
    - Rejected with ``ToolError`` on ``tools/call`` attempts

    Tools NOT in any group (ungrouped tools like health probes) are
    always available regardless of the filter config.

    Args:
        config: The tool filter configuration
        tool_groups: Mapping of group name -> set of tool names in that group
    """

    def __init__(
        self,
        config: ToolFilterConfig,
        tool_groups: dict[str, set[str]],
    ):
        self.config = config
        self.tool_groups = tool_groups

        # Pre-compute the set of ALL grouped tool names
        self._all_grouped_tools: set[str] = set()
        for tools in tool_groups.values():
            self._all_grouped_tools |= tools

        # Pre-compute the set of allowed tool names from enabled groups
        self._allowed_tools: set[str] = set()
        for group_name in config.enabled_groups:
            if group_name in tool_groups:
                self._allowed_tools |= tool_groups[group_name]
            else:
                logger.warning(
                    f"Tool filter: unknown group '{group_name}' in enabled_groups. "
                    f"Valid groups: {sorted(tool_groups.keys())}"
                )

        enabled_count = len(self._allowed_tools)
        total_grouped = len(self._all_grouped_tools)
        logger.info(
            f"Tool filter active: {len(config.enabled_groups)} group(s) enabled, "
            f"{enabled_count}/{total_grouped} grouped tools allowed"
        )

    def _is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool should be available.

        Returns True if:
        - The tool is NOT in any defined group (ungrouped tools always pass)
        - The tool IS in a group that's in enabled_groups
        """
        if tool_name not in self._all_grouped_tools:
            return True  # Ungrouped tools are always available
        return tool_name in self._allowed_tools

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Filter disabled tools from ``tools/list`` responses."""
        tools = await call_next(context)
        return [t for t in tools if self._is_tool_allowed(getattr(t, "name", ""))]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Reject calls to tools in disabled groups."""
        tool_name = context.message.name
        if not self._is_tool_allowed(tool_name):
            logger.info(
                f"Tool filter: blocked call to '{tool_name}' (group not enabled)",
                extra={"tool": tool_name, "filtered": True},
            )
            raise ToolError(
                f"Tool '{tool_name}' is not available in the current configuration. "
                f"Enabled groups: {sorted(self.config.enabled_groups)}"
            )
        return await call_next(context)


class LazyToolFilterMiddleware(Middleware):
    """FastMCP middleware that defers config loading to the first tool interaction.

    MCP servers start before task data is populated from S3, so the config
    file may not exist at registration time. This middleware is always
    registered and attempts to load the config on the first ``tools/list``
    or ``tools/call``, when the file is guaranteed to exist.

    Args:
        config_path: Path to the tool_filter.json config file
        tool_groups: Mapping of group name -> set of tool names in that group
    """

    def __init__(
        self,
        config_path: str,
        tool_groups: dict[str, set[str]],
    ):
        self._config_path = config_path
        self._tool_groups = tool_groups
        self._inner: ToolFilterMiddleware | None = None
        self._loaded = False

    def _try_load(self) -> None:
        """Attempt to load the config file, retrying until it exists.

        If the file doesn't exist yet (S3 data not populated), returns
        without marking as loaded so the next tool interaction retries.
        Once the file is found, parsing is attempted once — errors are
        logged and loading is not retried.
        """
        if self._loaded:
            return

        if not os.path.exists(self._config_path):
            return

        self._loaded = True

        try:
            with open(self._config_path, encoding="utf-8") as f:
                config = ToolFilterConfig.model_validate_json(f.read())

            if not config.enabled_groups:
                return  # Empty allowlist = all tools available

            self._inner = ToolFilterMiddleware(config, self._tool_groups)
        except Exception:
            logger.exception("Failed to load tool filter config")

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Load config on first call, then delegate to inner middleware."""
        self._try_load()
        if self._inner is not None:
            return await self._inner.on_list_tools(context, call_next)
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Load config on first call, then delegate to inner middleware."""
        self._try_load()
        if self._inner is not None:
            return await self._inner.on_call_tool(context, call_next)
        return await call_next(context)


def setup_tool_filter(
    mcp,
    tool_groups: dict[str, set[str]] | None = None,
    apps_data_root: str = "/.apps_data",
) -> bool:
    """Register lazy tool filter middleware.

    Always registers a :class:`LazyToolFilterMiddleware` that defers config
    loading to the first tool interaction. This ensures tool filtering works
    even when the config file is populated after server startup (e.g. task
    data loaded from S3 via ``POST /data/populate/s3``).

    Args:
        mcp: The FastMCP server instance
        tool_groups: Mapping of group name -> set of tool names. If None or
                    empty, middleware is still registered (for infrastructure
                    consistency) but will be a no-op.
        apps_data_root: Root directory for app data (default: /.apps_data)

    Returns:
        True (middleware is always registered)
    """
    state_location = os.environ.get("STATE_LOCATION", "")
    if state_location:
        config_path = os.path.join(state_location, ".config", "tool_filter.json")
    else:
        config_path = os.path.join(apps_data_root, mcp.name, ".config", "tool_filter.json")

    mcp.add_middleware(LazyToolFilterMiddleware(config_path, tool_groups or {}))
    return True
