"""Pydantic models for MCP gateway endpoints.

This module defines request and response models for the /apps endpoint,
as well as MCP server configuration models.
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runner.coordinator.config.models import CoordinatorConfig

# Duplicated from packages/tasks (this module is vendored into delivered worlds
# and can't import across packages): tool names/aliases must stay routable
# through fastmcp and safe as .apps_data path segments.
TOOL_ALIAS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class MCPServerConfig(BaseModel):
    """Configuration model for a single MCP server.

    Supports both remote HTTP/SSE servers and local stdio servers.
    """

    transport: str

    # Remote server config
    url: str | None = None
    headers: dict[str, str] | None = None
    auth: Any | None = None

    # Local server config
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None

    # When False the gateway routes /rest/<name> but excludes it from /mcp.
    serve_mcp_tools: bool = True

    # When True (transport "auto") the proxied openapi.json drops MCP-covered ops.
    openapi_mcp_filter: bool = False
    # From arco.exposes_mcp; False disables the openapi_mcp_filter fallback.
    exposes_mcp: bool = True

    # When True the gateway reuses ONE connected backend MCP session for every tool
    # call to THIS server instead of opening a fresh session per call. Required for
    # backends that bind state to the MCP session (the browser, which otherwise
    # resets to about:blank between calls). Default False preserves the
    # per-call-stateless behavior, which is also what forwards the per-call
    # `Authorization: Bearer <actor_id>` rewrite — so tenancy-enforcing backends
    # must NOT set this. Scoped per server: other servers in the same world stay
    # stateless. Set by the platform; see runner/gateway/gateway.py.
    session_affinity: bool = False


class ToolAliasSpec(BaseModel):
    """How the gateway should serve one app's tool names: renamed, or not at all.

    Parsed from a per-task ``.apps_data/<app>/.config/tool_aliases.json``
    delivered through the task snapshot — the same transport dynamic friction
    (``injected_errors.json``) and the tool filter (``tool_filter.json``) use.
    Canonical names here are the UNPREFIXED tool names as the app defines
    them; the gateway preserves fastmcp's multi-server ``{server}_`` prefix
    when renaming. The file itself is the ground truth for grading, which
    reads it back out of the trajectory's snapshot.

    ``extra="forbid"``: this crosses a trust boundary (authored in Studio,
    read inside the world container), so an unrecognized key is a rejected
    file rather than a silently-ignored one. Which is why Studio omits
    ``disabled_tools`` from the file when it is empty — an alias-only config
    stays parseable by gateways built before the key existed.
    """

    model_config = ConfigDict(extra="forbid")

    # Canonical (unprefixed) tool name -> the alias to serve it under.
    aliases: dict[str, str] = Field(default_factory=dict)

    # Canonical (unprefixed) tool names withheld from the agent entirely:
    # hidden from list_tools and rejected on call_tool. Scoped to this app, so
    # two apps serving the same bare name are withheld independently.
    disabled_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_names(self) -> "ToolAliasSpec":
        seen: set[str] = set()
        disabled: list[str] = []
        for name in self.disabled_tools:
            if not TOOL_ALIAS_NAME_RE.match(name):
                raise ValueError(f"invalid disabled tool name {name!r}")
            if name not in seen:
                seen.add(name)
                disabled.append(name)
        self.disabled_tools = disabled
        # Disable wins over an alias for the same tool. Mirrors ToolAliasConfig
        # in Studio: the pair can only arise from the union of two layers, and
        # raising here would drop the whole app to canonical names.
        if seen:
            self.aliases = {
                canonical: alias
                for canonical, alias in self.aliases.items()
                if canonical not in seen
            }
        canonicals = set(self.aliases)
        alias_owners: dict[str, str] = {}
        for canonical, alias in self.aliases.items():
            if not TOOL_ALIAS_NAME_RE.match(canonical):
                raise ValueError(f"invalid canonical tool name {canonical!r}")
            if not TOOL_ALIAS_NAME_RE.match(alias):
                raise ValueError(f"invalid alias {alias!r}")
            # Colliding with another canonical is only a real collision if
            # that tool keeps its name. A tool being renamed FREES its name,
            # so a swap ({a: b, b: c}) is routable: the served names stay
            # distinct, the reverse map is 1:1, and grading normalizes back
            # correctly. An identity entry ({b: b}) does not free anything.
            if (
                alias != canonical
                and alias in canonicals
                and self.aliases[alias] == alias
            ):
                raise ValueError(
                    f"alias {alias!r} collides with another canonical name"
                )
            owner = alias_owners.get(alias)
            if owner is not None and owner != canonical:
                raise ValueError(
                    f"alias {alias!r} appears under both {owner!r} and {canonical!r}"
                )
            alias_owners[alias] = canonical
        return self


class MCPSchema(BaseModel):
    """MCP configuration schema.

    Structure: {"mcpServers": {"server_name": MCPServerConfig(...)}}

    The mcpServers value is a dictionary mapping server names to their
    configuration. Each server config is validated against MCPServerConfig
    to ensure it has the correct structure and fields (transport, command,
    args, url, etc.).
    """

    mcpServers: dict[str, MCPServerConfig] = Field(
        ...,
        description="Dictionary mapping server names to their configuration. Can be empty if no servers are configured.",
    )

    allowed_tool_names: list[str] | None = Field(
        default=None,
        description=(
            "Optional allowlist of fully-namespaced tool names "
            "(e.g. 'bamboohr_get_employee'). When set, the gateway hides any "
            "tool not in this list from list_tools and rejects calls to them."
        ),
    )


class AppConfigRequest(MCPSchema):
    """Request to set/update MCP servers configuration.

    This endpoint accepts a full MCP configuration dict and hot-swaps
    the MCP gateway with the new configuration. Inherits validation from
    MCPSchema, ensuring each server config is validated against MCPServerConfig.
    """

    coordinator_config: CoordinatorConfig | None = None


class ServerReadinessResult(BaseModel):
    """Result of checking a single server's readiness.

    Attributes:
        is_ready: Whether the server is ready to handle requests
        message: Success message with tool count, or error description
        attempts: Number of attempts made before success or timeout
        elapsed_seconds: Time taken from start to success or timeout
    """

    is_ready: bool = Field(..., description="Whether the server is ready")
    message: str = Field(..., description="Success message or error description")
    attempts: int = Field(..., description="Number of attempts made")
    elapsed_seconds: float = Field(..., description="Time taken in seconds")


class ServerReadinessDetails(BaseModel):
    """Details about a server's readiness check failure.

    Attributes:
        error: Error message describing why the server failed
        attempts: Number of attempts made before timeout
    """

    error: str = Field(..., description="Error message")
    attempts: int = Field(..., description="Number of attempts made")


class AppConfigResult(BaseModel):
    """Result of setting MCP servers configuration.

    Returned by the /apps endpoint after successfully hot-swapping
    the MCP gateway with new configuration.
    """

    servers: list[str] = Field(
        ...,
        description="List of configured server names",
    )

    duration_ms: float | None = Field(
        default=None,
        description="Total time spent handling the /apps configuration request (includes gateway warmup)",
    )
