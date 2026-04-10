"""MCP server runner with transport configuration.

Provides a centralized function for running MCP servers with:
- Transport selection (http/stdio) via MCP_TRANSPORT env var
- Port configuration via MCP_PORT env var
- Processing of remaining CLI args for FastMCP
- Automatic server_info tool registration
- Automatic authentication setup (via ENABLE_AUTH/DISABLE_AUTH env vars)

Usage:
    from mcp_middleware import run_server, apply_configurations, ServerConfig

    mcp = FastMCP(name="my-server")

    # Register your tools
    @mcp.tool()
    async def my_tool():
        return "Hello!"

    # Parse args and configure
    args, remaining = apply_configurations(parser, mcp, configurators)

    # Run server with config - handles server_info and auth setup
    config = ServerConfig(
        name="my-server",
        version="1.0.0",
        description="My MCP server",
        features={"persistence": "sqlite"},
    )
    run_server(mcp, config=config, remaining_args=remaining)
"""

import importlib.util
import inspect
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ForwardRef, Literal, get_args, get_origin, get_type_hints

import yaml
from mcp_auth import is_auth_configured, setup_auth
from mcp_auth.services.auth_service import AuthService

from mcp_middleware.server_info import register_server_info_tool

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Configuration for server metadata used by run_server.

    This provides server information for the server_info tool response.
    If not provided to run_server(), metadata is read from the FastMCP instance.

    Attributes:
        name: Server name (e.g., "greenhouse-mcp")
        version: Server version (e.g., "1.0.0")
        description: Human-readable description of the server
        features: Additional features to include in server_info response
                 (e.g., {"personas": ["admin"], "persistence": "sqlite"})
        paginate_tools: Glob patterns for tool names that should be paginated.
                       Matching is snake_case-token-aware: ``*list*`` matches
                       ``list_folders`` and ``get_list`` but not ``enlist``.
                       Tools not matching any pattern are passed through unchanged.
                       Set to ["*"] to paginate all tools.  Default: ["*list*"].
        pagination_key: Response key that contains the tool's own pagination
                       object (e.g., ``"meta"``).  When set, the middleware
                       extracts ``page``, ``per_page``, and ``total`` from
                       this object and synthesises a ``_pagination`` block so
                       the UI can show pagination controls.  Default: None.
        native_pagination_params: Mapping of semantic role to native parameter
                       name.  Keys are ``"page"`` and ``"limit"``; values are
                       the actual parameter names used by the application's
                       tools.  For example, ``{"page": "start", "limit": "limit"}``
                       tells the middleware to recognise ``start`` and ``limit``
                       as native pagination and skip injecting duplicates.
                       Default: None (detects ``page`` / ``per_page``).
    """

    name: str
    version: str
    description: str = ""
    features: dict = field(default_factory=dict)
    paginate_tools: list[str] = field(default_factory=lambda: ["*list*"])
    pagination_key: str | None = None
    native_pagination_params: dict[str, str] | None = None


# Packages to skip when walking the call stack to find the server code
_SKIP_PACKAGES = ("/mcp_auth/", "/mcp_middleware/")

# Global storage for server state, set by run_server()
_server_directory: Path | None = None
_server_config: ServerConfig | None = None


def get_server_directory() -> Path | None:
    """Get the directory of the server's main module.

    This is set automatically when run_server() is called. It allows
    mcp_auth and other code to locate files (like users.json) relative
    to the server code, not the calling middleware.

    Returns:
        The directory containing the server code, or None if run_server
        hasn't been called yet.
    """
    return _server_directory


def get_server_config() -> ServerConfig | None:
    """Get the server configuration passed to run_server().

    This is set automatically when run_server() is called. It allows
    tools and other code to access server metadata (name, version, etc.)
    without circular imports.

    Returns:
        The ServerConfig passed to run_server(), or None if run_server
        hasn't been called yet.
    """
    return _server_config


def _capture_server_directory() -> None:
    """Capture the server directory from the call stack.

    Walks up the call stack to find the first frame outside mcp_auth
    and mcp_middleware packages, then stores that directory globally.
    """
    global _server_directory
    frame = inspect.currentframe()
    try:
        caller_frame = frame.f_back if frame else None
        while caller_frame:
            filename = caller_frame.f_code.co_filename
            # Skip frames from mcp_auth and mcp_middleware packages
            if not any(pkg in filename for pkg in _SKIP_PACKAGES):
                _server_directory = Path(filename).parent
                logger.debug(f"Server directory: {_server_directory}")
                return
            caller_frame = caller_frame.f_back
    finally:
        del frame


def _get_registered_tools(mcp_instance: "FastMCP") -> list[str]:
    """Get the list of registered tool names from an MCP instance.

    Args:
        mcp_instance: The FastMCP instance to query

    Returns:
        List of registered tool names in registration order, or empty list.
    """
    registered_tools: list[str] = []
    try:
        tool_manager = getattr(mcp_instance, "_tool_manager", None)
        if tool_manager:
            tools = getattr(tool_manager, "_tools", {})
            for tool in tools.values():
                registered_tools.append(tool.name)
    except Exception as e:
        logger.warning(f"Failed to get registered tools: {e}")

    return registered_tools


def _parse_tool_to_category(server_dir: Path) -> dict[str, str]:
    """Parse tool-to-category mapping from mcp-build-spec.yaml.

    Reads the mcp-build-spec.yaml file and builds a mapping of tool names
    to their categories from the tool_overrides section.

    Args:
        server_dir: Directory containing the mcp-build-spec.yaml file

    Returns:
        Dict mapping tool names to their category names (lowercase),
        or empty dict if the spec file doesn't exist or can't be parsed.

    Example output:
        {
            "greenhouse_candidates_search": "candidates",
            "greenhouse_candidates_get": "candidates",
            "greenhouse_applications_list": "applications",
            ...
        }
    """
    spec_file = server_dir / "mcp-build-spec.yaml"
    if not spec_file.exists():
        spec_file = server_dir / "mcp-build-spec.yml"
        if not spec_file.exists():
            return {}

    try:
        with open(spec_file) as f:
            spec = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to parse {spec_file}: {e}")
        return {}

    if not spec or "tool_overrides" not in spec:
        return {}

    # Build tool name -> category mapping
    tool_to_category: dict[str, str] = {}

    for override in spec.get("tool_overrides", []):
        tool_spec = override.get("tool", "")  # e.g., "greenhouse.greenhouse_candidates_search"
        category = override.get("category", "")

        if not tool_spec or not category:
            continue

        # Extract the tool name (after the dot)
        parts = tool_spec.split(".")
        tool_name = parts[-1] if len(parts) >= 2 else tool_spec

        category_snake = category.lower().replace(" ", "_")
        tool_to_category[tool_name] = category_snake

    return tool_to_category


def _parse_meta_tool_actions(server_dir: Path) -> dict[str, list[str]]:
    """Parse meta tool actions by introspecting TOOL_SCHEMAS from _meta_tools module.

    Meta tools follow a consistent pattern across all servers:
    1. A TOOL_SCHEMAS dict mapping tool names to input/output models
    2. Input models have an `action: Literal[...]` field defining valid actions

    This function imports the _meta_tools module and extracts actions
    from the Literal type annotation on each input model's action field.

    Args:
        server_dir: Directory containing the server's tools package

    Returns:
        Dict mapping meta tool names to lists of their action names,
        or empty dict if no meta tools are found or can't be parsed.

    Example output:
        {
            "greenhouse_candidates": ["help", "search", "get", "create", "update"],
            "greenhouse_applications": ["help", "list", "get", "create", "advance"],
            ...
        }
    """
    # Try to import the _meta_tools module from the server's tools package
    meta_tools_path = server_dir / "tools" / "_meta_tools.py"
    if not meta_tools_path.exists():
        return {}

    try:
        spec = importlib.util.spec_from_file_location("_meta_tools", meta_tools_path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        logger.warning(f"Failed to import _meta_tools module: {e}")
        return {}

    # Look for TOOL_SCHEMAS dict
    tool_schemas = getattr(module, "TOOL_SCHEMAS", None)
    if not tool_schemas or not isinstance(tool_schemas, dict):
        return {}

    # Extract actions from each meta tool's input model
    meta_tool_actions: dict[str, list[str]] = {}

    for tool_name, schemas in tool_schemas.items():
        input_model = schemas.get("input")
        if input_model is None:
            continue

        # Get the action field's type annotation
        try:
            action_annotation = None

            # First try get_type_hints() which resolves ForwardRef annotations
            # This requires the module's global namespace for proper resolution
            try:
                type_hints = get_type_hints(input_model, globalns=vars(module))
                action_annotation = type_hints.get("action")
            except Exception:
                pass  # Fall back to direct annotation access

            # If get_type_hints failed, try direct access
            if action_annotation is None:
                # Pydantic v2: use model_fields
                if hasattr(input_model, "model_fields"):
                    action_field = input_model.model_fields.get("action")
                    if action_field is not None:
                        action_annotation = action_field.annotation
                else:
                    # Pydantic v1 fallback: use __fields__
                    action_field = input_model.__fields__.get("action")
                    if action_field is not None:
                        action_annotation = action_field.outer_type_

            if action_annotation is None:
                continue

            # Handle ForwardRef by parsing the string if get_type_hints didn't resolve it
            if isinstance(action_annotation, ForwardRef):
                # Extract the string from ForwardRef and parse Literal values
                ref_str = action_annotation.__forward_arg__
                if ref_str.startswith("Literal["):
                    # Parse "Literal['a', 'b', 'c']" -> ['a', 'b', 'c']
                    import ast

                    inner = ref_str[8:-1]  # Remove "Literal[" and "]"
                    # Parse as a tuple to handle the comma-separated values
                    try:
                        parsed = ast.literal_eval(f"({inner},)")
                        actions = list(parsed)
                        if actions:
                            meta_tool_actions[tool_name] = actions
                    except (ValueError, SyntaxError):
                        pass
                continue

            # Extract values from resolved Literal type
            if get_origin(action_annotation) is Literal:
                actions = list(get_args(action_annotation))
                if actions:
                    meta_tool_actions[tool_name] = actions
        except Exception as e:
            logger.warning(f"Failed to extract actions for {tool_name}: {e}")
            continue

    return meta_tool_actions


def run_server(
    mcp_instance: "FastMCP",
    *,
    config: ServerConfig | None = None,
    remaining_args: list[str] | None = None,
    default_port: int = 5000,
    default_host: str = "0.0.0.0",
) -> None:
    """Run an MCP server with transport configured via environment variables.

    This function handles:
    1. Registering the server_info tool (public, returns auth status)
    2. Setting up authentication via mcp_auth.setup_auth (if ENABLE_AUTH=true)
    3. Running the server with the configured transport

    Args:
        mcp_instance: Configured FastMCP instance (tools and middleware already added)
        config: Server configuration with name, version, description, and features.
               If provided, these values are used for the server_info tool response.
               If None, metadata is read from the FastMCP instance attributes.
        remaining_args: Remaining CLI args to pass to FastMCP (from apply_configurations)
        default_port: Default port for HTTP transport (default: 5000).
                     Can be overridden by MCP_PORT env var.
        default_host: Default host for HTTP transport (default: "0.0.0.0").

    Environment Variables:
        MCP_TRANSPORT: Transport type - "http" (default) or "stdio"
        MCP_PORT: Port for HTTP transport (default: 5000)
        ENABLE_AUTH: Set to "true" to enable authentication
        DISABLE_AUTH: Set to "true" to disable authentication (takes precedence)

    Example:
        from fastmcp import FastMCP
        from mcp_middleware import run_server, apply_configurations, ServerConfig

        mcp = FastMCP(name="my-server")

        @mcp.tool()
        async def my_tool():
            return "Hello!"

        # Parse args and configure
        args, remaining = apply_configurations(parser, mcp, configurators)

        # Run server with config
        config = ServerConfig(
            name="my-server",
            version="1.0.0",
            description="My MCP server",
            features={"persistence": "sqlite"},
        )
        run_server(mcp, config=config, remaining_args=remaining)
    """
    # Capture server directory from call stack (for locating users.json, etc.)
    _capture_server_directory()
    server_dir = get_server_directory()

    # Auto-detect tool info: registered tools, categories, and meta tool actions
    registered_tools = _get_registered_tools(mcp_instance)

    if server_dir and registered_tools:
        tool_to_category = _parse_tool_to_category(server_dir)
        meta_tool_actions = _parse_meta_tool_actions(server_dir)

        if tool_to_category or meta_tool_actions:
            if config is None:
                # Create a minimal config with auto-detected data
                config = ServerConfig(
                    name=getattr(mcp_instance, "name", "mcp-server"),
                    version=getattr(mcp_instance, "version", "0.0.0"),
                    description=getattr(mcp_instance, "instructions", "") or "",
                    features={},
                )

            # Pass all tool info to server_info for building the response
            config.features["registered_tools"] = registered_tools
            if tool_to_category:
                config.features["tool_to_category"] = tool_to_category
                logger.debug("Auto-detected tool categories from build spec")
            if meta_tool_actions:
                config.features["meta_tool_actions"] = meta_tool_actions
                logger.debug(f"Auto-detected meta tool actions: {list(meta_tool_actions.keys())}")

    # Find users.json relative to the server's main module
    users_file = (server_dir / "users.json") if server_dir else Path("users.json")

    # If auth is configured, create AuthService early to get personas for server_info
    # We'll pass this same instance to setup_auth later to avoid creating it twice
    auth_service: AuthService | None = None
    if is_auth_configured():
        auth_service = AuthService(users_file)
        persona_names = list(auth_service.users.keys())
        if persona_names:
            if config is None:
                config = ServerConfig(
                    name=getattr(mcp_instance, "name", "mcp-server"),
                    version=getattr(mcp_instance, "version", "0.0.0"),
                    description=getattr(mcp_instance, "instructions", "") or "",
                    features={},
                )
            config.features["personas"] = persona_names
            logger.debug(f"Auto-detected personas: {persona_names}")

    # Store config globally for access by tools (e.g., admin tools need version)
    global _server_config
    _server_config = config

    # Sanitize Pydantic validation errors so LLM agents see concise messages
    # instead of verbose strings with documentation URLs.
    from mcp_middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

    mcp_instance.add_middleware(ValidationErrorSanitizerMiddleware())

    # Add response limiter middleware to paginate large responses automatically.
    # - on_call_tool: strips page_number, paginates oversized responses
    # - on_list_tools: injects page_number into schemas for MCP list_tools
    # - patch_tool_schemas: injects page_number directly into the tool registry
    #   so get_tools() (used by the UI generator scanner) also sees it
    from mcp_middleware.response_limiter import ResponseLimiterMiddleware

    paginate_patterns = config.paginate_tools if config else ["*list*"]
    pagination_key = config.pagination_key if config else None
    native_pagination_params = config.native_pagination_params if config else None
    limiter = ResponseLimiterMiddleware(
        tool_patterns=paginate_patterns,
        pagination_key=pagination_key,
        native_pagination_params=native_pagination_params,
    )
    mcp_instance.add_middleware(limiter)
    limiter.patch_tool_schemas(mcp_instance)

    # Error injection middleware (auto-detected from per-app config file)
    # Reads config from /.apps_data/{app}/.config/injected_errors.json
    from mcp_middleware.injected_errors import setup_error_injection

    try:
        setup_error_injection(mcp_instance)
    except Exception as e:
        logger.warning(f"Error injection setup failed, skipping: {e}")

    # Register server_info tool FIRST (uses @public_tool decorator for auth bypass)
    # This must happen before setup_auth so AuthGuard discovers it as public
    register_server_info_tool(mcp_instance, config=config)

    # Set up authentication AFTER server_info is registered
    # Pass the existing auth_service to avoid creating it twice
    setup_auth(mcp_instance, users_file=users_file, auth_service=auth_service)

    # If we're in UI generation mode, skip starting the server
    # The UI generator calls main() to trigger tool registration and setup_auth,
    # but doesn't want the server to actually start
    if os.getenv("MCP_UI_GEN", "").lower() in ("true", "1", "yes"):
        logger.info("UI generation mode: skipping server start")
        return

    # Pass remaining args to FastMCP (after configurators have processed their args)
    if remaining_args is not None:
        sys.argv = [sys.argv[0]] + remaining_args

    transport = os.getenv("MCP_TRANSPORT", "http").lower()

    if transport == "stdio":
        logger.info("Starting stdio server")
        mcp_instance.run(transport="stdio")
    else:
        port_str = os.getenv("MCP_PORT", str(default_port))
        try:
            port = int(port_str)
        except ValueError:
            logger.error(f"Invalid MCP_PORT value: '{port_str}' (must be a number)")
            sys.exit(1)
        logger.info(f"Starting HTTP server on {default_host}:{port}")
        mcp_instance.run(transport="http", host=default_host, port=port)
