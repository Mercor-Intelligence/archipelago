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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ForwardRef, Literal, get_args, get_origin, get_type_hints

import yaml
from mcp_auth import is_auth_configured, setup_auth
from mcp_auth.services.auth_service import AuthService

from mcp_middleware.server_info import register_server_info_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from fastmcp import FastMCP
    from sqlalchemy import Engine
    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    from mcp_middleware.default_user_gate import GateBypass
    from mcp_middleware.runtime_db import DbLifecycleSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpArms:
    """The named HTTP-middleware arms ``run_server`` assembles.

    Handed to a caller-supplied ``http_middleware_builder`` so an app can
    re-order the ASGI stack (e.g. put CORS outermost, or interleave a
    normalizer between the gate and the identity arm) WITHOUT hand-rolling the
    no-proactive-bind primitives. Divergence becomes a supported, tested seam
    under shared ownership rather than a bypass.

    The default builder (no ``http_middleware_builder``) reproduces today's
    order: eager mode → ``[identity_rest, *caller]``; lazy (``db_lifecycle``)
    mode → ``[db_gate, identity_rest, *caller]`` so the DB gate is outermost
    and 503s pre-bind requests before they reach the identity arm's
    holder-backed engine provider.

    Attributes:
        db_gate: The no-proactive-bind DB gate (``DbGateMiddleware``), present
            only under ``db_lifecycle=``; ``None`` in the eager path.
        identity_rest: The mandatory default-user identity REST arm, present
            only when the identity gate is installed; ``None`` otherwise.
        caller: The caller-supplied ``http_middleware`` list, verbatim, as a
            tuple (CORS, path normalizers, problem-json, …).
    """

    db_gate: "Middleware | None"
    identity_rest: "Middleware | None"
    caller: "tuple[Middleware, ...]"


@dataclass(frozen=True)
class McpInjectable:
    """One named entry in the MCP tool-layer injection stack ``run_server`` applies.

    Each injectable is a named thunk: ``apply`` performs the injectable's FULL
    effect on the FastMCP instance — whether that is ``add_middleware`` (tool
    middleware), ``patch_tool_schemas`` (a registry rewrite), a tool
    registration, or a discrete setup function. Representing every entry as a
    uniform ``(name, apply)`` pair makes the heterogeneous stack addressable and
    re-orderable by a caller-supplied ``mcp_layer_builder`` without run_server
    needing to know how each entry mutates the instance.

    Attributes:
        name: Stable key for the injectable (e.g. ``"response_limiter"``). Used
            to reference it from a ``mcp_layer_builder`` and by the soft-warn
            that flags the two documented intra-order constraints.
        apply: Zero-arg callable that performs the injectable's full effect when
            invoked. run_server calls it exactly once, in the final order.
    """

    name: str
    apply: "Callable[[], None]"


@dataclass(frozen=True)
class McpArms:
    """The named MCP tool-layer injectables ``run_server`` assembles.

    Handed to a caller-supplied ``mcp_layer_builder`` so an app can re-order or
    drop the injected tool-layer stack (schema shaping, server_info, auth)
    WITHOUT hand-rolling the pieces — exactly analogous to how
    :class:`HttpArms` exposes the HTTP/ASGI arms to ``http_middleware_builder``.
    A byte-parity clone that owns all six entries itself returns ``[]``; run_server
    still applies the always-on db_lifecycle + identity + HTTP wiring around it.

    The default (no ``mcp_layer_builder``) applies :attr:`default` — the six in
    source order — reproducing the historical inline injection sequence exactly.

    Attributes:
        validation_sanitizer: ValidationErrorSanitizerMiddleware (concise
            Pydantic validation errors for LLM agents).
        response_limiter: ResponseLimiterMiddleware + its ``patch_tool_schemas``
            (auto-pagination of oversized responses). Must precede
            ``schema_flatten`` when both are kept.
        schema_flatten: SchemaFlattenMiddleware + its ``patch_tool_schemas``
            (Gemini/LLM-compatible flattened input schemas).
        error_injection: ``setup_error_injection`` (per-app fault injection,
            config-file driven; wrapped in the existing try/except).
        server_info_tool: ``register_server_info_tool`` (public metadata tool).
            Must precede ``auth`` when both are kept.
        auth: ``setup_auth`` (persona auth; env-gated no-op when auth disabled).
    """

    validation_sanitizer: McpInjectable
    response_limiter: McpInjectable
    schema_flatten: McpInjectable
    error_injection: McpInjectable
    server_info_tool: McpInjectable
    auth: McpInjectable

    @property
    def default(self) -> "tuple[McpInjectable, ...]":
        """The six injectables in canonical source order (== today's sequence)."""
        return (
            self.validation_sanitizer,
            self.response_limiter,
            self.schema_flatten,
            self.error_injection,
            self.server_info_tool,
            self.auth,
        )


def _warn_mcp_layer_misorder(to_apply: "list[McpInjectable]") -> None:
    """Non-blocking soft-warn for the two documented intra-order constraints.

    A ``mcp_layer_builder`` may freely re-order or drop injectables — ordering is
    the app's responsibility and run_server never enforces it. But two orderings
    are load-bearing when BOTH items are kept, so a misorder is almost always a
    bug worth flagging:

    * ``server_info_tool`` before ``auth`` — ``setup_auth``'s AuthGuard discovers
      ``server_info`` as a public tool during setup; register it afterwards and
      the guard treats it as protected.
    * ``response_limiter`` before ``schema_flatten`` — the flattener must run
      after the limiter so the limiter's injected pagination params are flattened
      too.

    Warn only; never raise.
    """
    names = [inj.name for inj in to_apply]

    def _must_precede(earlier: str, later: str) -> bool:
        # A violation: both kept but ``earlier`` is positioned AFTER ``later``.
        return earlier in names and later in names and names.index(earlier) > names.index(later)

    if _must_precede("server_info_tool", "auth"):
        logger.warning(
            "mcp_layer_builder placed 'auth' before 'server_info_tool'; setup_auth "
            "discovers server_info as public during setup, so server_info_tool should "
            "be applied first (server_info may otherwise be treated as protected)"
        )
    if _must_precede("response_limiter", "schema_flatten"):
        logger.warning(
            "mcp_layer_builder placed 'schema_flatten' before 'response_limiter'; the "
            "flattener should run after the limiter so injected pagination params are "
            "flattened too"
        )


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
        import asyncio

        tools = asyncio.run(mcp_instance.list_tools())
        for tool in tools:
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


# A default-user table name must be a plain SQL identifier — it's interpolated
# into the COUNT(*) probe, so reject anything that isn't ``[A-Za-z_][A-Za-z0-9_]*``.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _default_user_count(engine: "Engine", table: str) -> int:
    """Return ``COUNT(*)`` of the default-user table, or 0 if it's missing.

    Shared by :func:`require_default_user` (the strict populate-time assert)
    and :func:`default_user_present` (the non-raising runtime gate predicate).
    A missing table (``OperationalError`` / ``ProgrammingError``) reads as 0 —
    same meaning as an empty table: no identity has been seeded yet.

    Raises:
        ValueError: ``table`` is not a valid SQL identifier.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"default_user_table must be a plain SQL identifier, got {table!r}")

    try:
        with engine.connect() as conn:
            count = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
    except (OperationalError, ProgrammingError):
        # Table doesn't exist yet (populate never ran, or the DB shipped
        # without it). Same meaning as an empty table: no default user (yet).
        return 0
    return int(count or 0)


@dataclass(frozen=True)
class DefaultUserRef:
    """Referential-integrity spec for the default-user identity check.

    By default the identity check keys purely on *presence* of a row in the
    single-row default-user table. But a row can carry a foreign key that points
    at a user that doesn't exist — SQLite ships ``foreign_keys=OFF``, so a bad
    populate/UPDATE can land a **dangling** pointer that passes a presence-only
    check and then blows up downstream when the identity is resolved.

    The shared lib doesn't own app schemas, so an app that wants the check to
    additionally require the FK to *resolve* passes this spec describing the FK
    column and the table + column it references. When supplied, the identity is
    considered present only if the row's non-empty FK resolves to a referenced
    row; a dangling FK is reported distinctly from a missing row (see
    :func:`require_default_user`).

    All three names are validated as plain SQL identifiers at construction so a
    typo fails fast rather than reaching an interpolated query.

    Attributes:
        fk_column: FK column on the default-user table (e.g. ``"user_id"``).
        ref_table: Referenced table the FK must resolve into (e.g. ``"users"``).
        ref_column: PK column on ``ref_table`` (default ``"id"``).
    """

    fk_column: str
    ref_table: str
    ref_column: str = "id"

    def __post_init__(self) -> None:
        for attr, value in (
            ("fk_column", self.fk_column),
            ("ref_table", self.ref_table),
            ("ref_column", self.ref_column),
        ):
            if not _IDENTIFIER_RE.match(value):
                raise ValueError(
                    f"DefaultUserRef.{attr} must be a plain SQL identifier, got {value!r}"
                )


def _default_user_status(
    engine: "Engine", table: str, ref: "DefaultUserRef | None"
) -> tuple[int, int, str | None]:
    """Return ``(present, valid, dangling_sample)`` for the default-user table.

    * ``present`` — rows in the singleton table (0 if the table is missing).
    * ``valid`` — rows whose FK is non-empty AND resolves to a ``ref.ref_table``
      row. With ``ref is None`` no referential check runs and ``valid == present``.
    * ``dangling_sample`` — a non-empty FK value that does NOT resolve (the
      signal that distinguishes "id present but matches no user" from "no id"),
      else ``None``.

    Shared by :func:`require_default_user` and :func:`default_user_present` so
    the assert and the runtime gate agree on what "configured" means.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"default_user_table must be a plain SQL identifier, got {table!r}")

    if ref is None:
        count = _default_user_count(engine, table)
        return count, count, None

    fk, ref_table, ref_col = ref.fk_column, ref.ref_table, ref.ref_column
    resolves = (
        f'd."{fk}" IS NOT NULL AND d."{fk}" <> \'\' '
        f'AND EXISTS (SELECT 1 FROM "{ref_table}" r WHERE r."{ref_col}" = d."{fk}")'
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f'SELECT d."{fk}" AS fk, '
                    f"CASE WHEN {resolves} THEN 1 ELSE 0 END AS ok "
                    f'FROM "{table}" d'
                )
            ).all()
    except (OperationalError, ProgrammingError):
        # The base table is missing → no identity (present 0). If instead the
        # REFERENCED table/column is missing, a non-empty FK genuinely resolves
        # to nothing — treat present rows as dangling and sample one.
        present = _default_user_count(engine, table)
        if present == 0:
            return 0, 0, None
        try:
            with engine.connect() as conn:
                sample = conn.execute(
                    text(
                        f'SELECT d."{fk}" FROM "{table}" d '
                        f'WHERE d."{fk}" IS NOT NULL AND d."{fk}" <> \'\' LIMIT 1'
                    )
                ).scalar()
        except (OperationalError, ProgrammingError):
            sample = None
        return present, 0, (str(sample) if sample is not None else None)

    present = len(rows)
    valid = sum(1 for row in rows if row.ok)
    dangling_sample: str | None = None
    for row in rows:
        if not row.ok and row.fk not in (None, ""):
            dangling_sample = str(row.fk)
            break
    return present, valid, dangling_sample


# ── Global kill-switch for the mandatory default-user identity requirement ──
# Both enforcement points — the populate-time assert (:func:`require_default_user`,
# called from ``snapshot_with_populate``) and the runtime gate (installed by
# ``run_server`` via :func:`install_default_user_gate`) — consult this single
# predicate. It is ENABLED by default: apps must have a default-user identity
# seeded by populate, and the runtime gate refuses tools/REST until it lands. No
# per-app config is needed for that default.
#
# To disable everywhere (e.g. while a cross-uid identity issue is worked out),
# flip the one constant below to ``False`` (a single-line change, no app edits).
# An app may also declare its stance IN CODE (``run_server`` /
# ``snapshot_with_populate`` take ``enforce_default_user=True/False``), and a
# single per-deploy env var beats both — set it either way in the app's
# ``mise.toml`` (or the process env):
#   MCP_ENFORCE_DEFAULT_USER=true    → force enforcement ON for this process
#   MCP_ENFORCE_DEFAULT_USER=false   → force enforcement OFF for this process
_DEFAULT_USER_ENFORCED_DEFAULT = True

_ENFORCE_DEFAULT_USER_ENV = "MCP_ENFORCE_DEFAULT_USER"
_TRUTHY = ("true", "1", "yes")
_FALSEY = ("false", "0", "no")


def default_user_enforced(override: bool | None = None) -> bool:
    """Is the mandatory default-user identity requirement currently enforced?

    Consulted by both the populate-time assert and the runtime gate so a single
    switch governs the whole feature. Precedence (first match wins):

    1. ``MCP_ENFORCE_DEFAULT_USER`` when set to a recognized bool → that value
       (the per-deploy escape hatch, set either way in ``mise.toml``);
    2. ``override`` when not ``None`` → the app's in-code stance (the
       ``enforce_default_user`` argument to ``run_server`` /
       ``snapshot_with_populate``);
    3. the module default (:data:`_DEFAULT_USER_ENFORCED_DEFAULT`, currently
       ``True``).

    So env beats code beats the global default — an app declares its stance in
    its own ``main.py``/``snapshot.py`` without touching the shared constant, and
    ops can still force either way per deploy without a code change.

    Args:
        override: The app's in-code choice, or ``None`` (default) to defer to
            the env var / global constant.
    """
    env = os.getenv(_ENFORCE_DEFAULT_USER_ENV, "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSEY:
        return False
    if override is not None:
        return override
    return _DEFAULT_USER_ENFORCED_DEFAULT


def require_default_user(
    engine: "Engine",
    table: str = "default_users",
    *,
    ref: "DefaultUserRef | None" = None,
) -> None:
    """Raise unless the default-user table holds a usable identity row.

    Strict, non-tolerant identity assertion. Mercor MCP apps resolve the
    caller identity from a single-row "default user" table seeded during
    populate from a ``default_user.csv``. This is the **fail-loud assert** run
    at the END of populate (post-harvest): if populate produced no identity,
    it raises so the populate process exits non-zero.

    Runtime enforcement is a separate concern owned by the identity **gate**
    (:func:`mcp_middleware.install_default_user_gate`), which lets the server
    boot and refuses operations until the row lands. This raise is
    deliberately NOT used at boot — a raise there would deadlock a
    populate-after-start deployment whose port is health-checked before
    populate delivers the seed. A *missing* table is treated identically to an
    *empty* one.

    When ``ref`` is given the assert additionally requires the row's foreign
    key to *resolve*, and reports the two failure modes distinctly:

    * **no identity** (no row, or the row's FK is empty) →
      :class:`~mcp_middleware.errors.DefaultUserNotConfiguredError`;
    * **dangling identity** (the row's FK is set but matches no ``ref.ref_table``
      row) → :class:`~mcp_middleware.errors.DefaultUserDanglingReferenceError`,
      naming the offending id.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the runtime DB.
        table: Name of the singleton default-user table (default
            ``"default_users"``). Must be a plain SQL identifier.
        ref: Optional referential-integrity spec. When ``None`` (default) the
            assert keys purely on presence of a row.

    Raises:
        DefaultUserNotConfiguredError: The table is empty/missing, or (with
            ``ref``) the row carries no FK value.
        DefaultUserDanglingReferenceError: With ``ref``, the row's FK is set but
            resolves to no ``ref.ref_table`` row.
        ValueError: ``table`` is not a valid SQL identifier.
    """
    from mcp_middleware.errors import DefaultUserNotConfiguredError

    if ref is None:
        if _default_user_count(engine, table) >= 1:
            logger.info("default user present in %r — identity requirement satisfied", table)
            return
        raise DefaultUserNotConfiguredError(table)

    present, valid, dangling = _default_user_status(engine, table, ref)
    if valid >= 1:
        logger.info(
            "default user in %r resolves to a %r row — identity requirement satisfied",
            table,
            ref.ref_table,
        )
        return
    if dangling is not None:
        from mcp_middleware.errors import DefaultUserDanglingReferenceError

        raise DefaultUserDanglingReferenceError(
            dangling, ref_table=ref.ref_table, fk_column=ref.fk_column, table=table
        )
    raise DefaultUserNotConfiguredError(table)


def default_user_present(
    engine: "Engine",
    table: str = "default_users",
    *,
    ref: "DefaultUserRef | None" = None,
) -> bool:
    """Non-raising sibling of :func:`require_default_user`.

    Returns ``True`` iff the default-user table holds a usable identity row. A
    missing table reads as ``False``. This is the live predicate the identity
    **gate** evaluates on every (un-latched) tool call and HTTP request. It
    keys purely on the table row — no seed-file heuristic — so the gate opens
    the instant populate seeds the row and closes the moment a DB swap brings
    in an empty table.

    When ``ref`` is given, presence additionally requires the row's non-empty FK
    to resolve to a ``ref.ref_table`` row — so a dangling pointer reads as
    ``False`` (gate stays closed), identical to a missing row. The distinct
    dangling-vs-missing reporting lives in the raising :func:`require_default_user`.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the runtime DB.
        table: Name of the singleton default-user table (default
            ``"default_users"``). Must be a plain SQL identifier.
        ref: Optional referential-integrity spec (see :class:`DefaultUserRef`).

    Raises:
        ValueError: ``table`` is not a valid SQL identifier.
    """
    if ref is None:
        return _default_user_count(engine, table) >= 1
    _, valid, _ = _default_user_status(engine, table, ref)
    return valid >= 1


def _runtime_binding_for_routes(
    engine: "Engine",
    runtime_canonical: "str | os.PathLike[str] | None",
) -> "object | None":
    """Reconstruct an :class:`EngineBinding` for the /_internal routes.

    ``run_server`` holds a raw engine, not a binding, but the persist route
    needs the canonical + working paths + mode. All binding is in place now
    (runtime IS canonical), so the working path is the engine's own bound
    file and the mode is always :data:`WorkingMode.IN_PLACE` — persist folds
    in place, no copy.

    Returns ``None`` when ``runtime_canonical`` is absent (MEMORY, or an app
    that hasn't adopted it), the engine has no file (memory engine), or the
    engine's bound file is NOT the canonical (an unexpected/legacy placement
    we won't fold over the canonical). The caller then registers engine-only
    and the persist route degrades to a 501 the snapshot client falls back on.
    """
    if runtime_canonical is None:
        return None
    from mcp_middleware.runtime_db import (
        EngineBinding,
        RuntimePaths,
        WorkingMode,
        is_awaiting_delivery,
        mark_awaiting_delivery,
    )

    canonical_path = Path(os.fspath(runtime_canonical)).expanduser().resolve()
    engine_db = getattr(engine.url, "database", None)
    if not engine_db:
        return None  # memory engine — no file paths to report/persist
    try:
        bound = Path(engine_db).resolve()
    except OSError:
        bound = Path(engine_db)
    if bound != canonical_path:
        # All binding is in place now (runtime IS canonical). An engine whose
        # bound file is NOT the canonical is an unexpected/legacy placement we
        # won't fold over the canonical — degrade to engine-only (persist 501s,
        # snapshot client falls back) rather than risk clobbering it.
        return None
    mode = WorkingMode.IN_PLACE
    # In-place bind with the canonical not yet on disk is a blank-world boot:
    # the engine will lazily create the file at the canonical path. Arm the
    # blank-world sentinel so a RESTART — which finds that lazily-created blank
    # present — still reconstructs delivered=False (see the delivered=
    # computation below) instead of mistaking the blank for a real delivery
    # and folding it. Best-effort.
    if not canonical_path.exists():
        mark_awaiting_delivery(canonical_path)

    rp = RuntimePaths(
        canonical=canonical_path,
        runtime=bound,
        marker=bound.with_name(bound.name + ".srcmeta"),
        wal=bound.with_name(bound.name + "-wal"),
        shm=bound.with_name(bound.name + "-shm"),
    )
    return EngineBinding(
        engine=engine,
        url=str(engine.url),
        mode=mode,
        canonical=canonical_path,
        runtime=bound,
        paths=rp,
        # Mirror bind_engine's provenance rule, not just canonical.exists().
        # The bind is in place (runtime IS canonical), so nothing stamps
        # ``.srcmeta`` and a real delivery is indistinguishable from a blank
        # the engine created on a prior boot. We invert the signal via the
        # ``.awaiting`` sentinel: a canonical present WITH the sentinel is a
        # blank leftover (still awaiting delivery), one present WITHOUT it is
        # genuine. See ``mark_awaiting_delivery`` above / the paths module.
        delivered=canonical_path.exists() and not is_awaiting_delivery(canonical_path),
    )


def run_server(
    mcp_instance: "FastMCP",
    *,
    config: ServerConfig | None = None,
    remaining_args: list[str] | None = None,
    default_port: int = 5000,
    default_host: str = "0.0.0.0",
    http_middleware: list | None = None,
    engine: "Engine | None" = None,
    mount_runtime_db_routes: bool = True,
    default_user_table: str | None = None,
    default_user_bypass: "GateBypass | None" = None,
    default_user_ref: "DefaultUserRef | None" = None,
    enforce_default_user: bool | None = None,
    runtime_canonical: "str | os.PathLike[str] | None" = None,
    runtime_schema_hook: "Callable[[Path], None] | None" = None,
    db_lifecycle: "DbLifecycleSpec | None" = None,
    db_lifecycle_bypass_tools: "Collection[str] | None" = None,
    db_gate_whitelist_extra: "Collection[str] | None" = None,
    http_transport: str = "http",
    http_path: str | None = None,
    http_app_customizer: "Callable[[Starlette], Starlette | None] | None" = None,
    http_middleware_builder: "Callable[[HttpArms], list[Middleware]] | None" = None,
    mcp_layer_builder: "Callable[[McpArms], list[McpInjectable]] | None" = None,
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
        http_middleware: Optional list of Starlette ``Middleware`` objects
                     (``starlette.middleware.Middleware``) applied to the HTTP
                     app, forwarded to FastMCP's ``run(middleware=...)``. Use
                     for app-level ASGI concerns such as CORS, an ASGI auth
                     gate, or path normalization. Defaults to None (no extra
                     HTTP middleware). Ignored on the stdio transport, which
                     has no HTTP surface.
        engine: Optional SQLAlchemy ``Engine``. When provided, enables the
                     runtime-DB routes (``/_internal/checkpoint``) so
                     out-of-process workers can ask the live server to fold
                     its WAL before they copy the runtime DB. The engine
                     MUST be the same instance the server's tool calls
                     use — checkpointing a second engine misses the frames
                     pinned by the first.
        mount_runtime_db_routes: Default ``True``. When ``engine`` is
                     provided and this flag is True, the shared
                     ``register_runtime_db_routes(mcp_instance, engine)``
                     is called automatically. Pass ``False`` to opt out
                     (e.g. you want to mount the route at a custom path,
                     or you need to layer auth in front of it). With no
                     engine, the flag is ignored — there's nothing to
                     checkpoint.
        default_user_table: Optional name of the app's single-row
                     "default user" table (e.g. ``"default_users"``). When
                     set, ``run_server`` installs the mandatory-identity
                     **gate** via :func:`install_default_user_gate`: the
                     server boots normally, but every tool call and HTTP
                     request is refused until the table holds a row. This is
                     a runtime gate, NOT a boot-time raise — a raise at boot
                     would deadlock a populate-after-start deployment whose
                     port is health-checked before populate delivers the
                     seed. The gate opens the instant populate seeds the row
                     (live check) and re-closes if a DB swap brings in an
                     empty table. Requires ``engine`` (raises ``ValueError``
                     otherwise). Skipped for UI generation. Default ``None``
                     — opt-in, no gate.
        default_user_bypass: Optional :class:`GateBypass` controlling which
                     HTTP path prefixes and tool names skip the identity gate
                     (e.g. ``/_internal/`` for the pre-identity checkpoint
                     drain, ``/health`` probes, and public tools like
                     ``server_info``). Only meaningful alongside
                     ``default_user_table``. Defaults to
                     :data:`DEFAULT_GATE_BYPASS`.
        default_user_ref: Optional :class:`DefaultUserRef`. When provided, the
                     identity gate opens only if the default-user row's foreign
                     key *resolves* to a referenced row. A **dangling** pointer
                     (FK set but matching no user) is a corrupt state and fails
                     the gate closed **even when enforcement is off** — so passing
                     a ref keeps dangling-reference protection regardless of
                     ``enforce_default_user``. A genuinely-empty world stays
                     flag-governed. Only meaningful alongside ``default_user_table``.
        enforce_default_user: The app's in-code enforcement stance for the
                     *unconfigured* (empty-table) case. ``None`` (default) defers
                     to the global default; ``True``/``False`` force it on/off for
                     this app without editing the shared constant. A per-deploy
                     ``MCP_ENFORCE_DEFAULT_USER`` env var still overrides this
                     (env > code > global default). Note this governs only the
                     empty-world close: a ``default_user_ref`` dangling reference
                     always fails closed regardless. Only meaningful alongside
                     ``default_user_table``.
        runtime_canonical: Optional path to the canonical (slow-storage) DB,
                     typically ``binding.canonical`` from
                     :func:`~mcp_middleware.runtime_db.bind_engine` in RUNTIME
                     mode. When set alongside ``engine`` it drives the persist
                     route's runtime binding (see
                     :func:`register_runtime_db_routes`) so the server can fold
                     its runtime back onto canonical. Leave ``None`` for
                     DIRECT/MEMORY bindings (no separate canonical to persist to).
        runtime_schema_hook: Optional callable invoked with the freshly
                     adopted runtime DB path when this server adopts a newer
                     canonical on the ``/_internal/enable_db`` late-delivery
                     adopt. Use it to rebuild ephemeral runtime tables / indexes
                     the snapshot strips (e.g. FTS/vec shadow tables) so a
                     freshly-copied working DB is query-ready. Threaded to
                     ``register_runtime_db_routes``; leave ``None`` to skip.
        db_lifecycle: Optional :class:`~mcp_middleware.runtime_db.DbLifecycleSpec`
                     enabling the **no-proactive-bind** lifecycle. When set,
                     ``run_server`` binds NOTHING at boot; instead it wires the
                     three lazy triggers and boots with the DB gate CLOSED:
                       * the MCP tool arm (first tool call synchronously ensures
                         the bind — ruling D1, via ``register_db_lifecycle``);
                       * the holder-mode ``/_internal/*`` routes (the
                         ``enable_db`` populate signal is Trigger 1 — the initial
                         bind, via ``register_runtime_db_routes(holder=...)``);
                       * the HTTP gate (``DbGateMiddleware``, default-CLOSED at
                         boot): a boot-closed non-whitelisted miss returns 503 +
                         ``Retry-After`` immediately AND kicks a non-blocking
                         ``background_ensure`` so the client's retry finds the
                         gate open (the locked "first HTTP request does not hold"
                         contract). Mutually exclusive with ``engine=`` (pass one
                         or the other; passing both raises ``ValueError``).
                     Skipped under UI generation. Default ``None`` (classic
                     eager-engine behaviour).
        db_lifecycle_bypass_tools: Tool names that must NOT trigger a bind under
                     ``db_lifecycle`` — DB-independent tools the UI may call
                     before any world exists (a login tool, etc.). ``server_info``
                     is always bypassed automatically. Only meaningful alongside
                     ``db_lifecycle``. Default ``None``.
        db_gate_whitelist_extra: Extra HTTP path prefixes the ``db_lifecycle``
                     DB gate lets through while the DB is still unbound, UNIONED
                     with the built-in
                     :data:`~mcp_middleware.runtime_db.DEFAULT_WHITELIST` (which
                     already covers ``/health`` and the ``/_internal/*`` control
                     routes). Use for public, DB-independent HTTP surfaces an app
                     serves before any world exists (e.g. ``/``, ``/openapi.json``,
                     ``/docs``, ``/redoc``). Only meaningful alongside
                     ``db_lifecycle``. Default ``None`` (built-in whitelist only).
        http_transport: FastMCP HTTP sub-transport for the self-built app —
                     ``"http"`` (default), ``"streamable-http"``, or ``"sse"``.
                     Passed to ``mcp.http_app(transport=...)``. A non-default
                     value triggers the self-build serve path. Ignored on stdio.
        http_path: Mount path for the self-built HTTP app (e.g. ``"/mcp"``),
                     passed to ``mcp.http_app(path=...)``. ``None`` (default)
                     keeps FastMCP's default mount. A non-``None`` value triggers
                     the self-build serve path. Ignored on stdio.
        http_app_customizer: Optional callable handed the freshly-built Starlette
                     ``http_app`` (with the assembled middleware already applied),
                     returning the app to serve (or ``None`` to keep the one it
                     was given). Use to mount extra routes, wrap the app, or set
                     ``redirect_slashes=False``. Setting this triggers the
                     self-build serve path; the app's MCP session-manager lifespan
                     is preserved. Ignored on stdio.
        http_middleware_builder: Optional callable handed an :class:`HttpArms`
                     (the named ``db_gate`` / ``identity_rest`` arms plus the
                     caller ``http_middleware`` tuple) and returning the final
                     ordered ``list[Middleware]`` for the HTTP app. Use to place a
                     caller middleware OUTSIDE the shared arms (e.g. CORS as the
                     outermost layer) without hand-rolling the primitives — the
                     divergence stays a supported, tested seam. ``None`` (default)
                     uses the built-in order (eager: identity outermost; lazy: DB
                     gate outermost, then identity). Setting this triggers the
                     self-build serve path. Ignored on stdio.
        mcp_layer_builder: Optional callable handed a :class:`McpArms` (the six
                     named MCP tool-layer injectables — ``validation_sanitizer``,
                     ``response_limiter``, ``schema_flatten``, ``error_injection``,
                     ``server_info_tool``, ``auth``) and returning the ordered
                     ``list[McpInjectable]`` run_server should apply. Use to
                     re-order or drop injected tool-layer middleware and setup
                     steps (e.g. a byte-parity clone that owns all six itself
                     returns ``[]``) without hand-rolling the pieces — exactly
                     analogous to ``http_middleware_builder``. The always-on
                     db_lifecycle + identity + HTTP wiring is applied around the
                     returned subset regardless, so a hook returning ``[]`` still
                     gets the DB gate, identity gate, and HTTP arms. ``None``
                     (default) applies ``McpArms.default`` — the six in source
                     order — byte-identical to the historical inline sequence.
                     A soft-warn (never raises) flags the two load-bearing
                     intra-orderings if a reordering hook keeps both items:
                     ``server_info_tool`` before ``auth`` and ``response_limiter``
                     before ``schema_flatten``. Honored on both the serve and
                     MCP_UI_GEN paths.

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
    # Restrict every file this process subsequently creates to the owner
    # (0o600 files / 0o700 dirs). Agents running under other OS users were
    # reading server-owned data — notably the SQLite runtime DBs — out of the
    # shared temp dir. This umask is the broad safety net that also covers
    # lazily-created runtime DBs, their -wal/-shm sidecars, logs, and exports;
    # the runtime_db layer additionally sets explicit modes for the case where
    # its files are created (at import time) before this runs.
    os.umask(0o077)

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

    # ── MCP tool-layer injection stack ───────────────────────────────────────
    # run_server injects a fixed set of tool-layer middleware AND discrete setup
    # steps (schema shaping, server_info, auth). Each is wrapped as a named
    # McpInjectable thunk whose apply() performs its FULL effect, so an app can
    # supply mcp_layer_builder(McpArms) -> list[McpInjectable] to re-order or drop
    # entries — exactly analogous to the http_middleware_builder(HttpArms) seam.
    # This is a GENERIC seam: run_server encodes no app-specific choice. The
    # default (mcp_layer_builder=None) applies arms.default — the six in source
    # order — byte-identical to the historical inline sequence. The always-on
    # db_lifecycle + identity + HTTP wiring lives in its own blocks below, so a
    # hook returning [] still gets all of that; it only drops the six.

    def _apply_validation_sanitizer() -> None:
        # Sanitize Pydantic validation errors so LLM agents see concise messages
        # instead of verbose strings with documentation URLs.
        from mcp_middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

        mcp_instance.add_middleware(ValidationErrorSanitizerMiddleware())

    def _apply_response_limiter() -> None:
        # Paginate large responses automatically.
        # - on_call_tool: strips page_number, paginates oversized responses
        # - on_list_tools: injects page_number into schemas for MCP list_tools
        # - patch_tool_schemas: injects page_number directly into the tool registry
        #   so list_tools() (used by the UI generator scanner) also sees it
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

    def _apply_schema_flatten() -> None:
        # Flatten tool INPUT schemas for Gemini/LLM compatibility. GeminiBaseModel
        # only annotates optional fields; the anyOf/$defs collapse must run on the
        # schema tools/list actually serves. Registered both as on_list_tools
        # middleware (runtime path) and via patch_tool_schemas (registry/scanner
        # path); runs after the limiter so injected pagination params are flattened
        # too.
        from mcp_middleware.schema_flatten import SchemaFlattenMiddleware

        flattener = SchemaFlattenMiddleware()
        mcp_instance.add_middleware(flattener)
        flattener.patch_tool_schemas(mcp_instance)

    def _apply_error_injection() -> None:
        # Error injection middleware (auto-detected from per-app config file)
        # Reads config from /.apps_data/{app}/.config/injected_errors.json
        from mcp_middleware.injected_errors import setup_error_injection

        try:
            setup_error_injection(mcp_instance)
        except Exception as e:
            logger.warning(f"Error injection setup failed, skipping: {e}")

    def _apply_server_info_tool() -> None:
        # Register server_info tool FIRST (uses @public_tool decorator for auth
        # bypass). This must happen before setup_auth so AuthGuard discovers it
        # as public.
        register_server_info_tool(mcp_instance, config=config)

    def _apply_auth() -> None:
        # Set up authentication AFTER server_info is registered. Pass the existing
        # auth_service to avoid creating it twice.
        setup_auth(mcp_instance, users_file=users_file, auth_service=auth_service)

    mcp_arms = McpArms(
        validation_sanitizer=McpInjectable("validation_sanitizer", _apply_validation_sanitizer),
        response_limiter=McpInjectable("response_limiter", _apply_response_limiter),
        schema_flatten=McpInjectable("schema_flatten", _apply_schema_flatten),
        error_injection=McpInjectable("error_injection", _apply_error_injection),
        server_info_tool=McpInjectable("server_info_tool", _apply_server_info_tool),
        auth=McpInjectable("auth", _apply_auth),
    )
    if mcp_layer_builder is None:
        to_apply = list(mcp_arms.default)
    else:
        to_apply = list(mcp_layer_builder(mcp_arms))
        _warn_mcp_layer_misorder(to_apply)
    for _injectable in to_apply:
        _injectable.apply()

    # UI generation calls main()/run_server() to trigger tool registration and
    # setup_auth, but runs WITHOUT a populated DB (and often without an engine at
    # all). Compute the flag once, up front, so the fail-fast engine check below
    # and the server-start skip further down both honour it.
    ui_gen_mode = os.getenv("MCP_UI_GEN", "").lower() in ("true", "1", "yes")

    # The no-proactive-bind lifecycle (db_lifecycle) and the classic eager-engine
    # path are mutually exclusive: db_lifecycle means "bind nothing at boot; the
    # holder owns the engine", so a pre-bound engine= would contradict it.
    if db_lifecycle is not None and engine is not None:
        raise ValueError(
            "run_server: pass db_lifecycle= (no-proactive-bind) OR engine= "
            "(eager bind), not both. db_lifecycle binds lazily on the first "
            "trigger; a pre-bound engine defeats the purpose."
        )

    # default_user_table needs a DB source to run its COUNT(*) probe — fail fast
    # at boot if the caller asked for enforcement without one. A source is EITHER
    # an eager ``engine=`` OR a lazy ``db_lifecycle=`` (whose holder-backed engine
    # the gate resolves once the first trigger binds). Exempt UI generation: it
    # legitimately registers tools with a default-user table name but no engine,
    # and must exit cleanly at the MCP_UI_GEN return below rather than crash here.
    if default_user_table and engine is None and db_lifecycle is None and not ui_gen_mode:
        raise ValueError(
            "run_server: default_user_table is set but no DB source was provided; "
            "pass engine=... (eager) or db_lifecycle=... (lazy) so the "
            "default-user table can be checked."
        )

    # Mount runtime-DB routes (/_internal/checkpoint, /_internal/enable_db, ...)
    # when the caller passed an engine. Default ON: the entire point of moving
    # this to shared is "adopt by upgrading the pin"; apps that go through
    # run_server should get the safe-snapshot behaviour for free. The
    # engine-presence check is the right gate: no engine, nothing to
    # checkpoint, no route. Pass ``mount_runtime_db_routes=False`` to opt out
    # (custom path / auth-gated route).
    if engine is not None and mount_runtime_db_routes:
        from mcp_middleware.runtime_db import register_runtime_db_routes

        # Prefer registering with a full EngineBinding so the /_internal/persist
        # route can fold this server's per-uid runtime back onto the canonical
        # (the write-side cross-uid fix). We can reconstruct a RUNTIME binding
        # from engine + runtime_canonical: the app cold-seeded + bound the engine
        # to runtime_paths_for(runtime_canonical).runtime, and passes
        # runtime_canonical=binding.canonical, so the two are consistent by
        # construction. Without runtime_canonical we can't know the canonical, so
        # fall back to the engine-only registration (persist reports 501; the
        # snapshot client treats that as "fall back to legacy harvest").
        route_binding = _runtime_binding_for_routes(engine, runtime_canonical)
        if route_binding is not None:
            register_runtime_db_routes(
                mcp_instance, route_binding, runtime_schema_hook=runtime_schema_hook
            )
        else:
            register_runtime_db_routes(
                mcp_instance, engine, runtime_schema_hook=runtime_schema_hook
            )
        logger.info("run_server: mounted /_internal/* runtime DB routes")

    # If we're in UI generation mode, skip starting the server. The UI generator
    # wants tool registration + setup_auth (done above) but not a running server.
    # Returning here also skips the default-user GATE below — UI generation runs
    # without a populated DB.
    if ui_gen_mode:
        logger.info("UI generation mode: skipping server start")
        return

    # Canonical pure-liveness /health — auto-mounted for EVERY server so apps
    # get it for free by upgrading the pin (and must NOT register their own).
    # Returns 200 regardless of gate state and NEVER touches the DB, so it stays
    # a true liveness probe even while the DB gate is closed at boot / during
    # populate (a DB-pinging health check would 503 itself). /health is already
    # in DEFAULT_WHITELIST; this backs that passthrough with a real handler.
    # Defensive: if an app still registers its own /health it is left in place
    # (a warning fires) so the migration window can't crash boot.
    from mcp_middleware.runtime_db import register_health_route

    register_health_route(mcp_instance)

    # The two named HTTP arms run_server may build. They are stashed here (not
    # prepended in-place) so the final ordering is decided ONCE, below, by the
    # default builder or a caller-supplied http_middleware_builder.
    db_gate_arm: Middleware | None = None
    identity_rest_arm: Middleware | None = None

    # ── No-proactive-bind DB lifecycle (opt-in via db_lifecycle) ─────────────
    # Bind NOTHING at boot. Wire the three lazy triggers and boot gate-CLOSED:
    #   * MCP tool arm — first tool call synchronously ensures (D1);
    #   * holder-mode /_internal/* routes — enable_db is Trigger 1 (initial bind);
    #   * HTTP gate default-CLOSED — a boot-closed miss 503s immediately AND kicks
    #     a non-blocking background bind so the Retry-After retry finds it open.
    # Placed after the MCP_UI_GEN early-return so UI generation is exempt.
    if db_lifecycle is not None:
        from starlette.middleware import Middleware as _StarletteMiddleware

        from mcp_middleware.runtime_db import (
            DbGateMiddleware,
            background_ensure,
            engine_holder,
            register_db_lifecycle,
            register_runtime_db_routes,
            set_db_disabled,
            set_populate_in_progress,
        )

        holder = engine_holder()
        # server_info (registered above) is DB-independent and the UI calls it
        # before any world exists — never let it trigger a bind.
        bypass = {"server_info", *(db_lifecycle_bypass_tools or ())}
        register_db_lifecycle(mcp_instance, db_lifecycle, holder=holder, bypass_tools=bypass)

        if mount_runtime_db_routes:
            register_runtime_db_routes(
                mcp_instance,
                holder=holder,
                runtime_schema_hook=runtime_schema_hook,
            )
            logger.info("run_server: mounted holder-mode /_internal/* runtime DB routes")

        # Boot-closed: the DB isn't bound yet and NO populate is running. Clear
        # the populate flag first so the gate classifies this window as
        # boot-closed (on_gated_miss kicks a bind), not populate-closed.
        set_populate_in_progress(False)
        set_db_disabled(True)

        # The gate is the HTTP arm: a boot-closed miss 503s + Retry-After AND
        # kicks a non-blocking background bind (background_ensure no-ops if the
        # DB is already binding, so racing requests don't stampede it).
        def _on_gated_miss() -> None:
            background_ensure(db_lifecycle, holder=holder)

        # Whitelist: DEFAULT_WHITELIST (/, health + /_internal/* control routes)
        # UNIONED with any app-declared public prefixes. DbGateMiddleware's
        # ``whitelist=`` REPLACES the default, so the union is built here.
        gate_kwargs: dict[str, Any] = {"on_gated_miss": _on_gated_miss}
        if db_gate_whitelist_extra:
            from mcp_middleware.runtime_db import DEFAULT_WHITELIST

            gate_kwargs["whitelist"] = set(DEFAULT_WHITELIST) | set(db_gate_whitelist_extra)
        db_gate_arm = _StarletteMiddleware(DbGateMiddleware, **gate_kwargs)
        logger.info("run_server: no-proactive-bind DB lifecycle wired (gate CLOSED at boot)")

    # Install the mandatory default-user identity GATE (opt-in via
    # default_user_table, and only when enforcement is on — see
    # default_user_enforced(); enabled by default, overridable in code via
    # enforce_default_user or per-deploy via MCP_ENFORCE_DEFAULT_USER). Unlike a
    # boot-time raise, the gate lets the server boot and refuses tool calls + HTTP
    # requests until the identity row lands — correct whether populate runs before
    # or after start (a raise at boot would deadlock a populate-after-start deploy
    # whose port is health-checked before populate delivers the seed). Placed
    # after the MCP_UI_GEN early-return so UI generation is exempt.
    enforced = default_user_enforced(enforce_default_user)
    # The gate does TWO independent jobs, only the first of which is governed by
    # enforcement:
    #   1. refuse-until-seeded (enforced) — closes on a genuinely-unconfigured
    #      world until the identity row lands;
    #   2. dangling-reference protection (needs a ``ref``) — a row present but its
    #      FK resolving to no user is CORRUPT, not a valid empty world, so it fails
    #      closed REGARDLESS of enforcement.
    # Install whenever EITHER has work: enforcement on OR a ref set. Skip only when
    # both are absent (nothing to enforce or validate) — the inert
    # backward-compatible path.
    watches_dangling = default_user_ref is not None
    # A DB source is EITHER an eager engine OR a lazy db_lifecycle (holder-backed).
    has_db_source = engine is not None or db_lifecycle is not None
    install_gate = bool(default_user_table) and has_db_source and (enforced or watches_dangling)
    if default_user_table and has_db_source and not install_gate:
        logger.info(
            "run_server: default-user enforcement disabled with no reference spec "
            "— booting without the identity gate (set MCP_ENFORCE_DEFAULT_USER=true "
            "to re-enable refuse-until-seeded, or pass default_user_ref for "
            "dangling-reference protection)"
        )
    if install_gate:
        from mcp_middleware.default_user_gate import install_default_user_gate

        if not enforced:
            # Installed with enforcement OFF: an unconfigured/empty world serves
            # (no refuse-until-seeded), but the gate still fails closed on a
            # dangling reference. Spell that out so operators aren't surprised to
            # see the gate wired with enforcement disabled.
            logger.info(
                "run_server: default-user enforcement disabled — installing the gate "
                "in non-enforcing mode for dangling-reference protection (an empty "
                "identity table serves; a dangling reference still fails closed)"
            )

        # Engine provider. EAGER mode owns a fixed Engine (run_server doesn't
        # rebind on DB swap the way an app managing its own session might), so a
        # constant provider is correct. LAZY (db_lifecycle) mode reads THROUGH
        # the holder so the gate resolves the engine only once the first trigger
        # has bound it — the holder's engine() raises DbNotReadyError before
        # then, but the outermost DB gate 503s pre-bind requests before the
        # provider is ever called (see the default lazy assembly order below).
        if engine is not None:
            _fixed_engine = engine

            def _engine_provider() -> "Engine":
                return _fixed_engine
        else:
            from mcp_middleware.runtime_db import engine_holder as _engine_holder

            def _engine_provider() -> "Engine":
                return _engine_holder().engine()

        gate_rest_mw = install_default_user_gate(
            mcp_instance,
            _engine_provider,
            table=default_user_table,
            bypass=default_user_bypass,
            ref=default_user_ref,
            enforced=enforced,
        )
        # Stash the REST arm; final ordering is decided once, below. Ignored on
        # stdio (no HTTP surface) — the tool arm, installed on the mcp instance
        # above, covers stdio tool calls.
        identity_rest_arm = gate_rest_mw

    # ── Assemble the HTTP middleware stack ───────────────────────────────────
    # Decide the final ordering ONCE from the named arms. The default order
    # preserves today's behaviour:
    #   * eager (engine=) → [identity_rest, *caller] (identity outermost);
    #   * lazy (db_lifecycle=) → [db_gate, identity_rest, *caller] so the DB gate
    #     is OUTERMOST and 503s pre-bind requests before they reach the identity
    #     arm's holder-backed engine provider.
    # A caller-supplied http_middleware_builder can re-order using the named arms
    # (e.g. CORS outermost) — divergence stays a supported, tested seam.
    caller_mw: tuple[Middleware, ...] = tuple(http_middleware or ())
    arms = HttpArms(db_gate=db_gate_arm, identity_rest=identity_rest_arm, caller=caller_mw)
    if http_middleware_builder is not None:
        assembled = list(http_middleware_builder(arms))
    elif db_lifecycle is not None:
        assembled = [m for m in (db_gate_arm, identity_rest_arm) if m is not None]
        assembled += list(caller_mw)
    else:
        assembled = [m for m in (identity_rest_arm, db_gate_arm) if m is not None]
        assembled += list(caller_mw)

    # Pass remaining args to FastMCP (after configurators have processed their args)
    if remaining_args is not None:
        sys.argv = [sys.argv[0]] + remaining_args

    transport = os.getenv("MCP_TRANSPORT", "http").lower()

    if transport == "stdio":
        if assembled:
            logger.debug("http_middleware ignored on stdio transport (no HTTP surface)")
        logger.info("Starting stdio server")
        mcp_instance.run(transport="stdio")
        return

    port_str = os.getenv("MCP_PORT", str(default_port))
    try:
        port = int(port_str)
    except ValueError:
        logger.error(f"Invalid MCP_PORT value: '{port_str}' (must be a number)")
        sys.exit(1)
    logger.info(f"Starting HTTP server on {default_host}:{port}")

    # Self-build the ASGI app ONLY when an HTTP-shaping param is set — otherwise
    # defer to mcp.run() exactly as before (unchanged BC for Teams/Zoho). Shaping
    # means the caller needs the app object itself (a customizer), a re-ordered
    # stack (a builder), a non-default mount path, or a non-default HTTP
    # sub-transport (streamable-http / sse).
    shaping = (
        http_app_customizer is not None
        or http_middleware_builder is not None
        or http_path is not None
        or http_transport != "http"
    )
    if not shaping:
        if assembled:
            mcp_instance.run(transport="http", host=default_host, port=port, middleware=assembled)
        else:
            mcp_instance.run(transport="http", host=default_host, port=port)
        return

    # Shaping path: build the real FastMCP http_app so the caller can customize
    # it, then serve it with uvicorn. http_app carries the MCP session-manager
    # lifespan, so uvicorn must run the app object (which preserves app.lifespan)
    # rather than re-deriving it.
    app = mcp_instance.http_app(
        path=http_path,
        transport=http_transport,
        middleware=assembled or None,
    )
    if http_app_customizer is not None:
        app = http_app_customizer(app) or app
    import uvicorn

    logger.info(
        "run_server: serving self-built http_app (transport=%s, path=%s)",
        http_transport,
        http_path or "<default>",
    )
    uvicorn.run(app, host=default_host, port=port, log_level="info")
