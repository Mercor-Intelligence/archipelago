"""Module-level registration of decorated endpoints.

Each ``@endpoint(...)`` call captures one :class:`EndpointDecl` into a
process-global list. The server calls :func:`register_all` once at
startup, passing the MCP server, the OpenAPI document, and the route
mounter (typically FastMCP's ``mcp.custom_route``). At that point each
declared endpoint becomes a registered MCP tool **and** a Starlette
route, with the OpenAPI document as the source of truth for parameter
shape, validation, and the response status code.

The split between decoration-time capture and registration-time wiring
is what lets the call site stay declarative — the function body never
imports the MCP server, FastMCP, or the OpenAPI document.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .dependencies import make_wire_callable
from .errors import ErrorSpec
from .layer_switch import ENV_VAR as _LAYER_SWITCH_ENV
from .layer_switch import is_layer_enabled
from .openapi_lookup import OperationSpec, lookup
from .rest import build_handler

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndpointDecl:
    """One ``@endpoint(...)`` site."""

    fn: Callable[..., Any]
    method: str  # uppercased
    path: str  # OpenAPI path template
    tool_name: str  # kebab-case
    title: str | None
    on_error_override: Mapping[type[BaseException], ErrorSpec] | None


# Module-level capture. Cleared by ``_reset_for_tests``.
_REGISTRY: list[EndpointDecl] = []


def add_declaration(decl: EndpointDecl) -> None:
    """Append a fresh declaration (called by the @endpoint decorator)."""
    _REGISTRY.append(decl)


def get_declarations() -> list[EndpointDecl]:
    """Return a copy of the captured declarations, in registration order."""
    return list(_REGISTRY)


# ---------------------------------------------------------------------------
# Registration protocol — keeps us decoupled from FastMCP's concrete types.
# ---------------------------------------------------------------------------


class MCPLike(Protocol):
    """The methods :func:`register_all` needs from the MCP server.

    FastMCP exposes these; any test double that implements the same shape
    works equally well.
    """

    def tool(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        **kwargs: Any,
    ) -> Any: ...

    def custom_route(
        self,
        path: str,
        methods: list[str],
        **kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


@dataclass
class RegistrationReport:
    """What was registered, for assertions in tests and operator visibility.

    ``tools`` lists the MCP tool names that were actually registered with
    the server. ``tools_gated`` lists the tool names that were *not*
    registered because ``MCP_LAYER_SWITCH`` was off — those tools are
    invisible to ``tools/list`` and unreachable via ``tools/call`` (the
    transport returns a method-not-found error). ``routes`` is unaffected
    by the switch; every endpoint's REST route is always registered.
    """

    tools: list[str]  # MCP tool names actually registered
    routes: list[tuple[str, str]]  # [(method, path), …] always registered
    #: Tool names skipped because ``MCP_LAYER_SWITCH`` was off. Empty when
    #: the switch is on (the default).
    tools_gated: list[str] = field(default_factory=list)


def register_all(
    mcp: MCPLike,
    *,
    openapi_spec: dict[str, Any],
) -> RegistrationReport:
    """Register every captured :class:`EndpointDecl` against ``mcp``.

    Two things happen per declaration:

    * The MCP tool is registered with :meth:`MCPLike.tool` — **only when
      the** ``MCP_LAYER_SWITCH`` **env var is on** (the default; see
      :mod:`mcp_unified_endpoint.layer_switch`). When the switch is off,
      the tool is skipped and recorded in
      :attr:`RegistrationReport.tools_gated` instead — invisible to
      ``tools/list`` and unreachable via ``tools/call``. This lets
      operators flip the wrapper layer off at container-build time
      without each consumer having to hand-maintain a "list of gated
      endpoint tool names" alongside its own ``WrapperLayerMiddleware``.
    * The Starlette REST route is registered with
      :meth:`MCPLike.custom_route` — **always**, regardless of the
      switch. The switch is purely an MCP-side ``tools/list`` /
      ``tools/call`` filter; HTTP clients hitting the REST surface see
      no change.

    Args:
        mcp: The FastMCP server (or a test double matching :class:`MCPLike`).
        openapi_spec: The full OpenAPI document. Each declaration's
            ``(method, path)`` must resolve to an operation in
            ``openapi_spec["paths"]``; otherwise
            :class:`~openapi_lookup.OpenAPILookupError` is raised.
    """
    tools_registered: list[str] = []
    tools_gated: list[str] = []
    routes_registered: list[tuple[str, str]] = []

    # Read the switch once per registration call — Studio bakes the value
    # at build time, so in-process changes mid-run aren't a real-world
    # scenario. Reading once also keeps the per-iteration loop simple.
    layer_on = is_layer_enabled()

    # Dedupe by ``tool_name`` — last write wins. The decorator appends to
    # the module-level ``_REGISTRY`` at import time, so any consumer that
    # reloads its endpoint modules (e.g. ``importlib.reload``, or test
    # suites that re-import the package) will end up with duplicate
    # ``EndpointDecl`` entries for the same tool. Without dedup,
    # ``register_all`` would re-register the same MCP tool and REST route
    # twice per reload, and ``RegistrationReport.tools`` would accumulate
    # across calls — neither matches the "what *this* call registered"
    # contract the report is meant to express. ``dict`` insertion order
    # preserves first-seen-at-this-name registration order while the
    # value tracks the freshest function reference.
    declarations: dict[str, EndpointDecl] = {}
    for decl in _REGISTRY:
        declarations[decl.tool_name] = decl

    for decl in declarations.values():
        spec: OperationSpec = lookup(openapi_spec, decl.method, decl.path)

        # MCP tool: register a *wire-only* view of the function. Any
        # ``Depends``-injected parameters (``db``, etc.) are hidden from
        # the visible signature so FastMCP / Pydantic don't try to build
        # an MCP ``inputSchema`` entry for them — those values flow in
        # via :func:`~mcp_unified_endpoint.dependencies.make_wire_callable`'s
        # internal resolution at call time. Endpoints without
        # ``Depends`` get back the original callable unchanged.
        description = decl.title
        if description and spec.operation_id is None:
            description = f"{description}. {decl.method} {decl.path}"
        elif spec.summary:
            description = spec.summary
        if layer_on:
            mcp.tool(
                make_wire_callable(decl.fn),
                name=decl.tool_name,
                description=description,
            )
            tools_registered.append(decl.tool_name)
        else:
            tools_gated.append(decl.tool_name)

        # REST route: a Starlette handler synthesised from the spec.
        # Always registered — the switch does not affect HTTP visibility.
        handler = build_handler(decl.fn, spec, on_error_override=decl.on_error_override)
        # FastMCP's custom_route is a decorator; invoking it returns a
        # registration function we feed our handler to.
        mcp.custom_route(decl.path, methods=[decl.method])(handler)
        routes_registered.append((decl.method, decl.path))

    # One summary line when gating happened so the operator sees the
    # consequence of the bake-time switch in the boot log.
    if not layer_on and tools_gated:
        _log.info(
            "%s is off: %d endpoint tool(s) skipped from tools/list "
            "(REST routes still registered). Gated: %s",
            _LAYER_SWITCH_ENV,
            len(tools_gated),
            sorted(tools_gated),
        )

    return RegistrationReport(
        tools=tools_registered,
        routes=routes_registered,
        tools_gated=tools_gated,
    )


def _reset_for_tests() -> None:
    """Clear captured declarations. Test-only."""
    _REGISTRY.clear()


__all__ = [
    "EndpointDecl",
    "MCPLike",
    "RegistrationReport",
    "add_declaration",
    "get_declarations",
    "register_all",
]
