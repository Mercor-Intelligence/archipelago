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
from typing import TYPE_CHECKING, ForwardRef, Literal, get_args, get_origin, get_type_hints

import yaml
from mcp_auth import is_auth_configured, setup_auth
from mcp_auth.services.auth_service import AuthService

from mcp_middleware.server_info import register_server_info_tool

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from sqlalchemy import Engine

    from mcp_middleware.default_user_gate import GateBypass

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


def _read_default_user_csv(
    csv_path: "str | os.PathLike[str] | None",
) -> "tuple[list[dict[str, str]], list[str]]":
    """Read a default-user CSV into ``(rows, columns)``.

    Returns ``([], [])`` when the CSV is absent, empty, or header-only (no data
    row) — every "nothing to apply" case collapses to the same empty signal so
    the caller's branch logic stays simple.
    """
    import csv as _csv

    if csv_path is None:
        return [], []
    path = Path(csv_path)
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = _csv.DictReader(fh)
        columns = [c for c in (reader.fieldnames or []) if c]
        rows = [dict(row) for row in reader]
    if not columns or not rows:
        return [], []
    return rows, columns


def apply_default_user_from_csv(
    engine: "Engine",
    table: str = "default_users",
    csv_path: "str | os.PathLike[str] | None" = None,
    *,
    trust_baked_rows: bool = False,
) -> str:
    """CSV-authoritative default-user apply at populate time.

    Governs the singleton default-user row *before* the strict
    :func:`require_default_user` assert. The CSV, when present, is
    **authoritative** — it wins over any baked/stale row a pre-built DB shipped
    from a prior deploy:

    * **CSV present with >=1 data row** → ``DELETE`` the table + ``INSERT`` the
      CSV rows. Applied **unconditionally** — NOT gated behind "row already
      present", so a freshly-shipped ``default_user.csv`` always re-points the
      identity. Returns ``"applied_csv"``.
    * **CSV absent / blank (missing, empty, or header-only):**

        * ``trust_baked_rows=False`` (default) → ``DELETE`` the table so no
          *untrusted* baked identity survives. The subsequent strict assert then
          fails (fail-closed): identity must come from populate. Returns
          ``"cleared"``.
        * ``trust_baked_rows=True`` → **no-op**; a baked row is a deliberate
          signal to use it (if it's invalid, operations fail naturally at use
          via scope enforcement). Returns ``"trusted_baked"``.

    The ``trust_baked_rows`` knob only governs the **baked-row + no-CSV-this-run**
    case; the CSV-present ``DELETE``+``INSERT`` is identical in both modes.

    Args:
        engine: SQLAlchemy ``Engine`` connected to the runtime DB.
        table: Name of the singleton default-user table. Must be a plain SQL
            identifier.
        csv_path: Path to the default-user CSV in the state dir, or ``None``
            when no such CSV is shipped.
        trust_baked_rows: See above. Default ``False`` (fail-closed).

    Returns:
        One of ``"applied_csv"`` / ``"cleared"`` / ``"trusted_baked"``.

    Raises:
        ValueError: ``table`` or a CSV column name is not a plain SQL identifier.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"default_user_table must be a plain SQL identifier, got {table!r}")

    rows, columns = _read_default_user_csv(csv_path)

    if rows:
        for col in columns:
            if not _IDENTIFIER_RE.match(col):
                raise ValueError(
                    f"default_user CSV column must be a plain SQL identifier, got {col!r}"
                )
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        with engine.begin() as conn:
            conn.execute(text(f'DELETE FROM "{table}"'))
            conn.execute(
                text(f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'),
                rows,
            )
        logger.info(
            "apply_default_user_from_csv: applied %d CSV row(s) to %r (CSV authoritative)",
            len(rows),
            table,
        )
        return "applied_csv"

    if trust_baked_rows:
        logger.info(
            "apply_default_user_from_csv: no default-user CSV this run; trusting "
            "baked rows in %r (trust_baked_rows=True)",
            table,
        )
        return "trusted_baked"

    # Fail-closed: no CSV and baked rows are not trusted → clear any stale row so
    # the strict assert fails and the gate stays closed. A missing table is
    # already "empty" — swallow the error.
    try:
        with engine.begin() as conn:
            conn.execute(text(f'DELETE FROM "{table}"'))
    except (OperationalError, ProgrammingError):
        pass
    logger.info(
        "apply_default_user_from_csv: no CSV and trust_baked_rows=False — cleared "
        "%r (fail-closed; identity must come from populate)",
        table,
    )
    return "cleared"


def _runtime_binding_for_routes(
    engine: "Engine",
    runtime_canonical: "str | os.PathLike[str] | None",
) -> "object | None":
    """Reconstruct a RUNTIME :class:`EngineBinding` for the /_internal routes.

    ``run_server`` holds a raw engine, not a binding, but the persist route
    needs the canonical + runtime paths + mode. When ``runtime_canonical`` is
    provided the canonical + hashed runtime paths are recoverable via
    :func:`runtime_paths_for`. The *mode*, however, must be read from the engine
    rather than assumed: an app in RUNTIME mode bound the engine to
    ``runtime_paths_for(runtime_canonical).runtime``, but an app in DIRECT mode
    bound it straight at the canonical. Assuming RUNTIME unconditionally would
    make persist run the copy-and-``os.replace`` fold from the hashed tmp runtime
    path (possibly absent / empty / stale) over the canonical the server is
    actively using — dropping in-flight writes. So we compare the engine's bound
    file to the canonical and pick DIRECT (fold in place, no copy) when they
    match, RUNTIME otherwise.

    Returns ``None`` when ``runtime_canonical`` is absent (MEMORY, or an app that
    hasn't adopted it) — the caller then registers engine-only and the persist
    route degrades to a 501 the snapshot client falls back on.
    """
    if runtime_canonical is None:
        return None
    from mcp_middleware.runtime_db import (
        BindingMode,
        EngineBinding,
        runtime_paths_for,
    )

    rp = runtime_paths_for(runtime_canonical)

    # Detect the actual mode from the engine's bound file. A direct-mode engine
    # points at the canonical itself; treating it as RUNTIME would fold from the
    # separate hashed tmp path over the live canonical (data loss).
    engine_db = getattr(engine.url, "database", None)
    is_direct = False
    if engine_db:
        try:
            is_direct = Path(engine_db).resolve() == rp.canonical.resolve()
        except OSError:
            is_direct = False

    if is_direct:
        return EngineBinding(
            engine=engine,
            url=str(engine.url),
            mode=BindingMode.DIRECT,
            canonical=rp.canonical,
            runtime=rp.canonical,  # runtime IS canonical → persist folds in place
            paths=rp,
        )
    return EngineBinding(
        engine=engine,
        url=str(engine.url),
        mode=BindingMode.RUNTIME,
        canonical=rp.canonical,
        runtime=rp.runtime,
        paths=rp,
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
                     mode. When set alongside ``default_user_table``, the
                     identity gate **self-heals**: the first time it sees an
                     absent identity row it refreshes this server's per-uid
                     runtime DB from canonical (in-process, correct uid) and
                     re-probes — closing the cross-uid "populate updated
                     canonical but the server still serves its stale runtime"
                     gap with no POST or lifecycle wiring. Leave ``None`` for
                     DIRECT/MEMORY bindings (no separate runtime to refresh) or
                     when self-heal isn't wanted. Only meaningful alongside
                     ``default_user_table`` + ``engine``.

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

    # Sanitize Pydantic validation errors so LLM agents see concise messages
    # instead of verbose strings with documentation URLs.
    from mcp_middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

    mcp_instance.add_middleware(ValidationErrorSanitizerMiddleware())

    # Add response limiter middleware to paginate large responses automatically.
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

    # UI generation calls main()/run_server() to trigger tool registration and
    # setup_auth, but runs WITHOUT a populated DB (and often without an engine at
    # all). Compute the flag once, up front, so the fail-fast engine check below
    # and the server-start skip further down both honour it.
    ui_gen_mode = os.getenv("MCP_UI_GEN", "").lower() in ("true", "1", "yes")

    # default_user_table needs an engine to run its COUNT(*) probe — fail fast
    # at boot if the caller asked for enforcement without one. Exempt UI
    # generation: it legitimately registers tools with a default-user table name
    # but no engine, and must exit cleanly at the MCP_UI_GEN return below rather
    # than crash here.
    if default_user_table and engine is None and not ui_gen_mode:
        raise ValueError(
            "run_server: default_user_table is set but no engine was provided; "
            "pass engine=... so the default-user table can be checked."
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
            register_runtime_db_routes(mcp_instance, route_binding)
        else:
            register_runtime_db_routes(mcp_instance, engine)
        logger.info("run_server: mounted /_internal/* runtime DB routes")

    # If we're in UI generation mode, skip starting the server. The UI generator
    # wants tool registration + setup_auth (done above) but not a running server.
    # Returning here also skips the default-user GATE below — UI generation runs
    # without a populated DB.
    if ui_gen_mode:
        logger.info("UI generation mode: skipping server start")
        return

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
    # The gate does THREE independent jobs, only the first of which is governed by
    # enforcement:
    #   1. refuse-until-seeded (enforced) — closes on a genuinely-unconfigured
    #      world until the identity row lands;
    #   2. dangling-reference protection (needs a ``ref``) — a row present but its
    #      FK resolving to no user is CORRUPT, not a valid empty world, so it fails
    #      closed REGARDLESS of enforcement;
    #   3. runtime<-canonical self-heal refresh (needs a ``runtime_canonical``) —
    #      runs inside the gate's decide() on every request, INDEPENDENT of
    #      enforcement, so a live server folds in a cross-uid populate / repopulate
    #      that rewrote canonical instead of serving its stale per-uid runtime.
    # Because the refresh lives inside the gate, skipping the gate when enforcement
    # is off would silently drop data-freshness (FGW root-cause: enforce=false went
    # red because the live server kept serving a stale runtime after populate). So
    # install whenever ANY of the three has work: enforcement on, a ref set, OR a
    # canonical set. Skip only when all three are absent (nothing to enforce,
    # validate, or refresh) — the inert backward-compatible path.
    watches_dangling = default_user_ref is not None
    watches_freshness = runtime_canonical is not None
    install_gate = (
        bool(default_user_table)
        and engine is not None
        and (enforced or watches_dangling or watches_freshness)
    )
    if default_user_table and engine is not None and not install_gate:
        logger.info(
            "run_server: default-user enforcement disabled with no reference spec "
            "and no runtime canonical — booting without the identity gate (set "
            "MCP_ENFORCE_DEFAULT_USER=true to re-enable refuse-until-seeded, pass "
            "default_user_ref for dangling-reference protection, or thread "
            "runtime_canonical to keep the runtime<-canonical self-heal refresh)"
        )
    if install_gate:
        from mcp_middleware.default_user_gate import install_default_user_gate

        assert engine is not None  # install_gate implies engine is not None
        if not enforced:
            # Installed with enforcement OFF: an unconfigured/empty world serves
            # (no refuse-until-seeded), but the gate still runs the self-heal
            # refresh and/or fails closed on a dangling reference. Spell out which
            # so operators aren't surprised to see the gate wired with enforcement
            # disabled.
            non_enforcing_jobs = []
            if watches_freshness:
                non_enforcing_jobs.append("runtime<-canonical self-heal refresh")
            if watches_dangling:
                non_enforcing_jobs.append("dangling-reference protection")
            logger.info(
                "run_server: default-user enforcement disabled — installing the gate "
                "in non-enforcing mode for %s (an empty identity table serves; a "
                "dangling reference still fails closed)",
                " + ".join(non_enforcing_jobs),
            )

        # run_server owns a fixed Engine (it doesn't rebind on DB swap the way
        # an app managing its own session might), so a constant provider is
        # correct here.
        gate_rest_mw = install_default_user_gate(
            mcp_instance,
            lambda: engine,
            table=default_user_table,
            bypass=default_user_bypass,
            canonical=runtime_canonical,
            ref=default_user_ref,
            enforced=enforced,
        )
        # Prepend the REST arm outermost so it gates before any DB-touching
        # HTTP middleware. Ignored on stdio (no HTTP surface) — the tool arm,
        # installed on the mcp instance above, covers stdio tool calls.
        http_middleware = [gate_rest_mw, *(http_middleware or [])]

    # Pass remaining args to FastMCP (after configurators have processed their args)
    if remaining_args is not None:
        sys.argv = [sys.argv[0]] + remaining_args

    transport = os.getenv("MCP_TRANSPORT", "http").lower()

    if transport == "stdio":
        if http_middleware:
            logger.debug("http_middleware ignored on stdio transport (no HTTP surface)")
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
        if http_middleware:
            mcp_instance.run(
                transport="http",
                host=default_host,
                port=port,
                middleware=http_middleware,
            )
        else:
            mcp_instance.run(transport="http", host=default_host, port=port)
