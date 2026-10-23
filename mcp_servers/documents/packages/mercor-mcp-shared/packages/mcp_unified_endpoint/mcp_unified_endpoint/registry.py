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

import inspect
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from starlette.responses import JSONResponse

from .dependencies import make_wire_callable
from .errors import ErrorSpec
from .layer_switch import ENV_VAR as _LAYER_SWITCH_ENV
from .layer_switch import is_layer_enabled
from .openapi_lookup import OperationSpec, lookup_by_operation_id
from .rest import (
    BodyErrorHook,
    MissingParamHook,
    TypeCoercionHook,
    build_handler,
    validate_response_class,
)

if TYPE_CHECKING:
    from .backfill import BackfillConfig

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static tool-name mutator
# ---------------------------------------------------------------------------
#
# A process-global prefix/suffix wrapped around EVERY generated MCP tool name
# at registration time, so a server can consistently mark its unified-endpoint
# tools as generated wrappers (e.g. a ``gen_`` prefix or ``_wrapper`` suffix)
# without touching each ``@endpoint`` site. The mutator is a *naming* transform
# only — REST routes and the spec-derived ``operation_id`` are unaffected.

#: A mutator fragment is a plain name token: letters, digits, ``_`` or ``-``
#: (permitting both the snake_case default and Zoho's kebab convention). The
#: caller supplies its own separator (``"gen_"``, ``"_wrapper"``) — fragments
#: are applied verbatim, never with an implicit delimiter.
_MUTATOR_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class NameMutator:
    """A static ``prefix``/``suffix`` wrapped around every generated tool name.

    Applied to the *concrete* name (after ``expand`` fan-out), so the prefix/
    suffix land on each fanned tool and the mutated name is what the collision
    check, the :class:`RegistrationReport`, the :class:`ToolBinding`, and
    ``mcp.tool(name=...)`` all see — one consistent surface.
    """

    prefix: str = ""
    suffix: str = ""

    def apply(self, name: str) -> str:
        return f"{self.prefix}{name}{self.suffix}"


_NAME_MUTATOR: NameMutator | None = None


def register_name_mutator(*, prefix: str = "", suffix: str = "") -> NameMutator:
    """Register a static prefix/suffix applied to every generated tool name.

    Idempotent-by-replacement: the most recent call wins (there is a single
    process-global mutator, mirroring the single ``_REGISTRY``). At least one
    of ``prefix`` / ``suffix`` must be non-empty. Fragments are applied
    verbatim — include your own separator (``prefix="gen_"``,
    ``suffix="_wrapper"``) — and may contain only ``[A-Za-z0-9_-]`` so the
    mutated name stays a valid tool identifier under both the snake_case and
    kebab conventions.

    Returns the installed :class:`NameMutator`.

    Raises:
        ValueError: both fragments empty, or a fragment carries an illegal
            character.
    """
    if not prefix and not suffix:
        raise ValueError("register_name_mutator needs a non-empty prefix and/or suffix")
    for label, fragment in (("prefix", prefix), ("suffix", suffix)):
        if not _MUTATOR_FRAGMENT_RE.match(fragment):
            raise ValueError(
                f"tool-name {label} {fragment!r} may contain only letters, digits, '_' or '-'"
            )
    global _NAME_MUTATOR
    _NAME_MUTATOR = NameMutator(prefix=prefix, suffix=suffix)
    return _NAME_MUTATOR


def clear_name_mutator() -> None:
    """Remove any registered name mutator (generated names pass through)."""
    global _NAME_MUTATOR
    _NAME_MUTATOR = None


def _apply_name_mutator(name: str) -> str:
    """Wrap ``name`` with the registered mutator, or return it unchanged."""
    return _NAME_MUTATOR.apply(name) if _NAME_MUTATOR is not None else name


@dataclass(frozen=True)
class ExpandOverride:
    """Per-value overrides for one fanned-out ``expand`` token value.

    Supplied via ``@endpoint(..., expand_overrides={"Leads": ExpandOverride(...)})``.
    Only the fields set here differ from the fanned-out defaults; everything
    else (route, spec-derived schema, pinned token) is shared across the
    fan-out. Kept intentionally small — the base case stays declarative.
    """

    #: Overrides the MCP tool ``description`` for this one value. ``None``
    #: falls back to the shared description computed from the spec/title.
    description: str | None = None


@dataclass(frozen=True)
class EndpointDecl:
    """One ``@endpoint(...)`` site.

    A declaration carrying ``expand`` fans out at :func:`register_all` into
    one MCP tool per token value (see :func:`register_all`). The REST route
    is still registered once from the single templated ``path``; fan-out is
    MCP-side only.
    """

    fn: Callable[..., Any]
    operation_id: str  # the spec's operationId; resolved to (method, path) at register_all
    tool_name: str  # snake_case default; may carry a ``{token}`` placeholder when expanded
    title: str | None
    on_error_override: Mapping[type[BaseException], ErrorSpec] | None
    #: ``{token: (value, …)}`` — normalised, single token. ``None`` means the
    #: declaration is a plain 1:1 endpoint (the pre-fan-out behaviour).
    expand: Mapping[str, tuple[str, ...]] | None = None
    #: ``{token: {value, …}}`` — token values to skip when fanning out (e.g.
    #: a module that rejects this operation). Empty / missing → nothing skipped.
    expand_exclude: Mapping[str, frozenset[str]] | None = None
    #: ``{value: ExpandOverride}`` — per-value overrides, keyed by the concrete
    #: token value (single-token fan-out makes the value unambiguous).
    expand_overrides: Mapping[str, ExpandOverride] | None = None
    #: Optional hook invoked when a request value cannot be parsed to its
    #: declared type on the REST route path. ``None`` (default) → the framework
    #: returns its shared 400. See :data:`~mcp_unified_endpoint.rest.TypeCoercionHook`.
    on_type_error: TypeCoercionHook | None = None
    #: Optional hook invoked when the JSON request body is malformed or absent
    #: while required, on the REST route path. ``None`` (default) → the framework
    #: returns its shared 400. See :data:`~mcp_unified_endpoint.rest.BodyErrorHook`.
    on_body_error: BodyErrorHook | None = None
    #: Optional hook invoked when a *required* path/query/header/cookie parameter
    #: is absent, on the REST route path. ``None`` (default) → the framework
    #: returns its shared 400. See
    #: :data:`~mcp_unified_endpoint.rest.MissingParamHook`.
    on_missing_param: MissingParamHook | None = None
    #: Optional JSON response class for this endpoint's REST responses (success,
    #: mapped errors, and the framework 400). ``None`` (default) → the registrar
    #: default passed to :func:`register_all`, or plain ``JSONResponse`` if that
    #: is also unset. A per-endpoint value wins over the registrar default. Only
    #: the REST wire is affected; the MCP tool path returns the raw ``.body``.
    response_class: type[JSONResponse] | None = None


# Module-level capture. Cleared by ``_reset_for_tests``.
_REGISTRY: list[EndpointDecl] = []


def add_declaration(decl: EndpointDecl) -> None:
    """Append a fresh declaration (called by the @endpoint decorator)."""
    _REGISTRY.append(decl)


def get_declarations() -> list[EndpointDecl]:
    """Return a copy of the captured declarations, in registration order."""
    return list(_REGISTRY)


@dataclass(frozen=True)
class _RegistryState:
    """An immutable snapshot of the process-global registration state.

    Captures the exact set of :class:`EndpointDecl` entries **and** the
    installed :class:`NameMutator` at one instant. ``EndpointDecl`` and
    ``NameMutator`` are both frozen, so the tuple is a stable capture that a
    later mutation of ``_REGISTRY`` / ``_NAME_MUTATOR`` cannot disturb.
    """

    declarations: tuple[EndpointDecl, ...]
    name_mutator: NameMutator | None


def _snapshot_state() -> _RegistryState:
    """Capture the current registry + name-mutator as a restorable snapshot.

    This is the snapshot half of the snapshot/restore isolation used by
    :func:`mcp_unified_endpoint.testing.registry_snapshot`. Unlike
    :func:`_reset_for_tests` it reads state without mutating it, so it is safe
    to call in a process that has legitimately-registered declarations.
    """
    return _RegistryState(declarations=tuple(_REGISTRY), name_mutator=_NAME_MUTATOR)


def _restore_state(state: _RegistryState) -> None:
    """Restore the registry + name mutator to a previously captured snapshot.

    Replaces the ``_REGISTRY`` **contents in place** (so any module that holds
    a reference to the list object still sees the live registry) with exactly
    the captured declarations, and re-installs the captured mutator. Anything
    added after the snapshot is dropped; anything present at snapshot time is
    put back verbatim — including declarations other modules registered before
    the snapshot. This is the deliberate contrast with :func:`_reset_for_tests`,
    which unconditionally empties the registry.
    """
    global _NAME_MUTATOR
    _REGISTRY[:] = state.declarations
    _NAME_MUTATOR = state.name_mutator


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


@dataclass(frozen=True)
class ToolBinding:
    """Resolves one concrete MCP tool back to its OpenAPI operation.

    This is the drift-proof, intrinsic replacement for a hand-maintained
    side file (Zoho's ``oas_v8/tool_map.json``): every registered tool —
    fanned-out or plain — carries the ``(method, path)`` / ``operation_id``
    it binds to, plus the token values that were pinned into it. A parity
    harness can walk :attr:`RegistrationReport.bindings` to map any concrete
    tool name to its base operation and pinned arguments with no external
    lookup table.
    """

    tool_name: str  # concrete registered name, e.g. "get_Leads"
    method: str  # uppercase HTTP method of the base operation
    path: str  # base templated OpenAPI path (single, shared across the fan-out)
    operation_id: str | None  # operationId of the base operation, if the spec has one
    #: ``{token: value}`` pinned into this tool (e.g. ``{"module": "Leads"}``).
    #: Empty for plain, non-fanned tools.
    pinned: Mapping[str, str] = field(default_factory=dict)


@dataclass
class RegistrationReport:
    """What was registered, for assertions in tests and operator visibility.

    ``tools`` lists the MCP tool names that were actually registered with
    the server. ``tools_gated`` lists the tool names that were *not*
    registered because ``MCP_LAYER_SWITCH`` was off — those tools are
    invisible to ``tools/list`` and unreachable via ``tools/call`` (the
    transport returns a method-not-found error). ``routes`` is unaffected
    by the switch; every endpoint's REST route is always registered.

    ``bindings`` carries one :class:`ToolBinding` per **registered** tool
    (1:1 with ``tools``, same order), recording the base operation and any
    pinned ``expand`` token values. This is the programmatic fan-out map the
    parity framework consumes; :meth:`fanned_out` narrows it to just the
    tools produced by an ``expand`` declaration.

    ``backfilled`` lists the subset of ``tools`` that were auto-registered
    from uncovered OpenAPI operations by ``backfill_missing`` (empty unless
    that option is used); ``backfill_skipped`` lists uncovered operations
    that could not be backfilled (no ``operationId``), as ``"METHOD /path"``
    strings. Together they make coverage assertable:
    ``len(explicit) + len(backfilled) + len(backfill_skipped)`` accounts for
    every operation the pass considered.
    """

    tools: list[str]  # MCP tool names actually registered
    routes: list[tuple[str, str]]  # [(method, path), …] always registered
    #: Tool names skipped because ``MCP_LAYER_SWITCH`` was off. Empty when
    #: the switch is on (the default).
    tools_gated: list[str] = field(default_factory=list)
    #: One entry per registered tool, parallel to ``tools``. Empty ``pinned``
    #: for plain endpoints; populated for fanned-out ``expand`` tools.
    bindings: list[ToolBinding] = field(default_factory=list)
    #: Subset of ``tools`` auto-registered from uncovered OAS operations by
    #: ``backfill_missing``. Empty when backfill is off.
    backfilled: list[str] = field(default_factory=list)
    #: Uncovered operations skipped by backfill for lack of an ``operationId``,
    #: as ``"METHOD /path"`` strings. Empty when backfill is off or unused.
    backfill_skipped: list[str] = field(default_factory=list)

    def fanned_out(self) -> list[ToolBinding]:
        """Return only the bindings produced by an ``expand`` fan-out."""
        return [b for b in self.bindings if b.pinned]

    def explicit(self) -> list[str]:
        """Return registered tools that were **not** produced by backfill."""
        backfilled = set(self.backfilled)
        return [t for t in self.tools if t not in backfilled]


def register_all(
    mcp: MCPLike,
    *,
    openapi_spec: dict[str, Any],
    backfill_missing: bool | BackfillConfig = False,
    response_class: type[JSONResponse] | None = None,
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
            ``operation_id`` must resolve to exactly one operation in
            ``openapi_spec["paths"]``; otherwise
            :class:`~openapi_lookup.OpenAPILookupError` is raised.
        backfill_missing: Opt-in coverage backfill. ``False`` (default)
            registers only the explicit ``@endpoint`` declarations. ``True``
            (or the terse ``NotImplemented`` sentinel, or an explicit
            :class:`~mcp_unified_endpoint.backfill.BackfillConfig`) additionally
            registers a default *not-implemented* (501) tool for **every**
            operation in ``openapi_spec["paths"]`` that no explicit declaration
            covers — a real name + input schema whose invocation returns a 501
            envelope. Backfilled tool names derive from the operation's
            ``operationId`` (camelCase-aware snake_case) and pass through the
            registered name mutator, so a static prefix marks them uniformly.
            Explicit declarations always win on any name collision (they are
            registered first). By default backfilled operations register an MCP
            tool only (no REST route); see
            :class:`~mcp_unified_endpoint.backfill.BackfillConfig` to opt into
            routes or to change the missing-``operationId`` behaviour.

            The ``MCP_BACKFILL_MISSING`` env var overrides this argument at
            deploy time (tri-state; see
            :func:`~mcp_unified_endpoint.backfill.resolve_backfill`): **unset**
            defers to this argument; a **truthy** value forces the pass on
            (honouring a :class:`~mcp_unified_endpoint.backfill.BackfillConfig`
            passed here, else using the default); any other value forces it
            **off**, an operator kill-switch that wins over an explicit
            ``backfill_missing=``. The env var decides *whether* backfill runs;
            this argument decides *its shape* when it does.
        response_class: Process-wide default JSON response class for every
            REST route's body-carrying responses (success payload, mapped error
            envelopes, and the framework 400). ``None`` (default) uses the plain
            :class:`~starlette.responses.JSONResponse`. Pass a subclass — e.g.
            one whose ``render`` strips ``None``-valued keys for exact Discovery
            wire parity — to shape every handler's output without post-processing
            each one. An ``@endpoint(response_class=...)`` set on an individual
            declaration wins over this default for that endpoint. The no-content
            204 / 304 / 1xx branch and the MCP tool path are unaffected. Must be
            a ``JSONResponse`` subclass or a ``TypeError`` is raised here.
    """
    # Imported lazily: ``backfill`` imports ``EndpointDecl`` from this module,
    # so a top-level import here would be circular.
    from .backfill import build_backfill_declarations, resolve_backfill

    # Fail loud at registration on a misconfigured registrar-level default
    # rather than on the first request served.
    validate_response_class(response_class, where="register_all")

    tools_registered: list[str] = []
    tools_gated: list[str] = []
    routes_registered: list[tuple[str, str]] = []
    bindings: list[ToolBinding] = []

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

    # Opt-in backfill: synthesise a not-implemented declaration for every OAS
    # operation no explicit declaration covers. Coverage is keyed on
    # ``operationId`` — the operation's identity — so an explicit ``@endpoint``
    # suppresses the 501 stub for its operation whatever tool name it chose; the
    # MCP tool *name* never enters the coverage decision. Built from fresh
    # objects (never appended to ``_REGISTRY``) so a later ``register_all`` call
    # is unaffected. Backfill decls are processed *after* the explicit ones, so
    # explicit declarations win on any concrete-name collision.
    backfill_cfg = resolve_backfill(backfill_missing)
    backfill_decls: list[EndpointDecl] = []
    backfill_skipped: list[str] = []
    if backfill_cfg is not None:
        covered = {decl.operation_id for decl in declarations.values()}
        backfill_decls, backfill_skipped = build_backfill_declarations(
            openapi_spec, covered, backfill_cfg
        )

    # Concrete MCP tool identity is the *expanded* name, not the ``tool_name``
    # template the dict above dedupes on. Distinct declarations can still collide
    # on a concrete name — two templates whose fan-outs overlap, a plain tool
    # sharing a fanned name, or a template missing its ``{token}`` placeholder so
    # every value yields the same name. Left unchecked both would register (the
    # second silently clobbering the first in FastMCP) while the report claims both
    # succeeded. Track concrete names and fail loud at registration — the same
    # posture ``_expand_variants`` takes for an unpinnable token.
    concrete_source: dict[str, str] = {}
    backfilled: list[str] = []

    # Explicit declarations first (they win any concrete-name collision), then
    # the synthesised backfill declarations flagged so route mounting and the
    # report can treat them distinctly.
    work: list[tuple[EndpointDecl, bool]] = [(d, False) for d in declarations.values()]
    work += [(d, True) for d in backfill_decls]

    for decl, is_backfill in work:
        spec: OperationSpec = lookup_by_operation_id(openapi_spec, decl.operation_id)
        method, path = spec.method, spec.path
        base_description = _base_description(decl, spec)

        # REST route: a Starlette handler synthesised from the spec. Registered
        # **once** per explicit declaration regardless of the switch or
        # ``expand`` (the single templated path is the whole REST surface).
        # Backfilled operations are MCP-tools-only by default — no route — so a
        # tools-only proxy server is unnecessary; opt in via
        # ``BackfillConfig.include_routes``.
        mount_route = not is_backfill or (backfill_cfg is not None and backfill_cfg.include_routes)
        if mount_route:
            handler = build_handler(
                decl.fn,
                spec,
                on_error_override=decl.on_error_override,
                on_type_error=decl.on_type_error,
                on_body_error=decl.on_body_error,
                on_missing_param=decl.on_missing_param,
                # Per-endpoint class wins; else the registrar-level default.
                response_class=decl.response_class or response_class,
            )
            # FastMCP's custom_route is a decorator; invoking it returns a
            # registration function we feed our handler to.
            mcp.custom_route(path, methods=[method])(handler)
            routes_registered.append((method, path))

        # MCP tool(s): one for a plain declaration, or one per ``expand``
        # token value for a fanned-out declaration. Each is a *wire-only*
        # view of the function — ``Depends``-injected params (``db``, …) and
        # pinned ``expand`` tokens are hidden from the visible signature so
        # FastMCP / Pydantic build an ``inputSchema`` covering only what an
        # MCP client should fill in. Pinned values are injected at call time
        # by :func:`~mcp_unified_endpoint.dependencies.make_wire_callable`.
        for concrete_name, pinned, description in _expand_variants(decl, base_description):
            prior = concrete_source.get(concrete_name)
            if prior is not None:
                raise ValueError(
                    f"MCP tool name collision: {concrete_name!r} is produced by both "
                    f"{prior!r} and {decl.tool_name!r}. Each declaration (after "
                    f"``expand`` fan-out) must yield a globally unique concrete tool "
                    f"name — check overlapping expand values, a plain tool sharing a "
                    f"fanned name, a tool_name template missing its {{token}} "
                    f"placeholder, or a backfilled operationId colliding with an "
                    f"explicit tool name."
                )
            concrete_source[concrete_name] = decl.tool_name
            if layer_on:
                mcp.tool(
                    make_wire_callable(decl.fn, pinned=pinned or None, spec_params=spec.parameters),
                    name=concrete_name,
                    description=description,
                )
                tools_registered.append(concrete_name)
                if is_backfill:
                    backfilled.append(concrete_name)
                bindings.append(
                    ToolBinding(
                        tool_name=concrete_name,
                        method=method,
                        path=path,
                        operation_id=spec.operation_id,
                        pinned=pinned,
                    )
                )
            else:
                tools_gated.append(concrete_name)

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

    # Visibility into the backfill pass so operators can see coverage at boot.
    if backfill_cfg is not None:
        _log.info(
            "backfill_missing: %d not-implemented wrapper tool(s) registered, "
            "%d operation(s) skipped (no operationId).",
            len(backfilled),
            len(backfill_skipped),
        )

    return RegistrationReport(
        tools=tools_registered,
        routes=routes_registered,
        tools_gated=tools_gated,
        bindings=bindings,
        backfilled=backfilled,
        backfill_skipped=backfill_skipped,
    )


def _base_description(decl: EndpointDecl, spec: OperationSpec) -> str | None:
    """Compute the shared MCP tool description for a declaration.

    The OpenAPI ``summary`` wins (the spec is the source of truth); the
    user-supplied ``title`` is the fallback when the operation has no summary.
    Fanned-out tools start from this and may replace it per value via
    :class:`ExpandOverride.description`.
    """
    return spec.summary or decl.title


def _expand_variants(
    decl: EndpointDecl,
    base_description: str | None,
) -> list[tuple[str, dict[str, str], str | None]]:
    """Yield ``(tool_name, pinned, description)`` per MCP tool for ``decl``.

    A declaration without ``expand`` yields exactly one variant: its own
    ``tool_name``, no pinned values, and the shared description — i.e. the
    pre-fan-out behaviour, unchanged.

    A declaration with ``expand`` yields one variant per token value: the
    ``{token}`` placeholder in ``tool_name`` is substituted with the value,
    the value is pinned (``{token: value}``) so it drops off the wire and is
    injected at call time, values in ``expand_exclude`` are skipped, and a
    matching :class:`ExpandOverride` may replace the description.

    Raises:
        ValueError: if the fanned token is not a parameter of the
            implementation function — pinning would then be a silent no-op
            and every fanned tool would behave identically, defeating the
            fan-out. Caught here at registration rather than surfacing as a
            confusing runtime duplicate.
    """
    if decl.expand is None:
        return [(_apply_name_mutator(decl.tool_name), {}, base_description)]

    # Decoration-time validation guarantees exactly one token key.
    ((token, values),) = decl.expand.items()

    sig_params = inspect.signature(decl.fn).parameters
    if token not in sig_params:
        raise ValueError(
            f"@endpoint expand token {token!r} is not a parameter of "
            f"{decl.fn.__name__!r}; add a {token!r} parameter so the fanned "
            f"value can be pinned into each tool "
            f"(tool_name template {decl.tool_name!r})."
        )

    excluded = (decl.expand_exclude or {}).get(token, frozenset())
    overrides = decl.expand_overrides or {}
    placeholder = "{" + token + "}"

    variants: list[tuple[str, dict[str, str], str | None]] = []
    for value in values:
        if value in excluded:
            continue
        concrete_name = _apply_name_mutator(decl.tool_name.replace(placeholder, value))
        override = overrides.get(value)
        description = (
            override.description
            if override is not None and override.description is not None
            else base_description
        )
        variants.append((concrete_name, {token: value}, description))
    return variants


def _reset_for_tests() -> None:
    """Clear captured declarations and any name mutator. Test-only."""
    _REGISTRY.clear()
    clear_name_mutator()


__all__ = [
    "EndpointDecl",
    "ExpandOverride",
    "MCPLike",
    "RegistrationReport",
    "ToolBinding",
    "add_declaration",
    "get_declarations",
    "register_all",
]
