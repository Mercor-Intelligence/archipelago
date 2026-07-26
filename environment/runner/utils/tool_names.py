"""
FastMCP changes the tool names when multiple MCP servers are proxied.

When one MCP server is proxied:

- `{tool_name}`

When multiple MCP servers are proxied:

- `{server_name}_{tool_name}`

These utils help us match configured tool names vs observed tool names.
For example an Email MCP with `mark_read` tool would be:

- Configured: `mark_read`
- Observed: `email_mark_read`
"""


def prefixed_tool_name(server_name: str, configured_tool_name: str) -> str:
    """
    Mirror FastMCP's multi MCP tool naming logic. FastMCP does this automatically
    but this is our copy of their logic.
    """
    return f"{server_name}_{configured_tool_name}"


def tool_name_matches(
    configured_tool_name: str | None, observed_tool_name: str
) -> bool:
    """
    Match configured tool names against observed tool names.

    Examples:
    - configured "mark_read" matches observed "mark_read"
    - configured "mark_read" matches observed "email_mark_read"
    - configured "email_mark_read" does not match observed "mark_read"
    """
    if configured_tool_name is None:
        return True
    return configured_tool_name == observed_tool_name or observed_tool_name.endswith(
        f"_{configured_tool_name}"
    )


def tool_counts_by_server(
    observed_tool_names: list[str], expected_servers: list[str]
) -> dict[str, int]:
    servers_with_tools: dict[str, int] = {}
    single_server = len(expected_servers) == 1
    if single_server:
        server = expected_servers[0]
        if observed_tool_names:
            servers_with_tools[server] = len(observed_tool_names)
    else:
        # Sort by name length (longest first) to handle prefix collisions
        # e.g., "api_v2" before "api" so "api_v2_tool" isn't claimed by "api"
        sorted_servers = sorted(expected_servers, key=len, reverse=True)
        claimed_tools: set[str] = set()

        for server in sorted_servers:
            prefix = f"{server}_"
            matching = [
                name
                for name in observed_tool_names
                if name.startswith(prefix) and name not in claimed_tools
            ]
            if matching:
                servers_with_tools[server] = len(matching)
                claimed_tools.update(matching)

    return servers_with_tools
