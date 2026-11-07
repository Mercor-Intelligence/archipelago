"""Per-request dependency injection for ``@endpoint``-decorated functions.

Lets a service-layer function declare values the handler should supply
at dispatch time (database sessions, auth context, tracing scopes, …)
without manually wiring them at every call site::

    from typing import Annotated
    from sqlalchemy.orm import Session

    from mcp_unified_endpoint import Depends, endpoint
    from db.session import SessionLocal

    @endpoint("getNote")
    def get_note(
        note_id: str,
        db: Annotated[Session, Depends(SessionLocal)],
    ) -> dict[str, Any]:
        ...

At request time the synthesised handler calls ``SessionLocal()`` and
passes the result as ``db``. When the factory returns a context manager
(as ``SessionLocal()`` does — ``Session`` implements ``__enter__`` /
``__exit__``), the handler enters it for the request and exits on the
way out, so the same lifecycle the hand-written wrapper used to manage
with ``with SessionLocal() as db:`` is preserved — including teardown
on exceptions.

The mechanism is deliberately the smallest thing that supports the
"decorate the service directly" pattern; it's not a full FastAPI-style
DI graph. Each ``Depends`` factory takes zero arguments. If you need
factory-of-factories or per-request param-aware dependencies, build them
in the service.
"""

from __future__ import annotations

import inspect
import json
import typing
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from .openapi_lookup import ParamSpec, is_wire_safe_param_name
from .response import EndpointResponse


def _is_json_media_type(media_type: str | None) -> bool:
    """Does ``media_type`` declare a JSON payload?

    Matches ``application/json`` and any structured-suffix JSON type
    (``application/foo+json``), ignoring parameters such as ``; charset=utf-8``.
    The *declared* content type — not byte-sniffing the body — is the
    discriminator: a ``PlainTextResponse("42")`` carries a JSON-decodable body
    but is honestly text, and silently turning it into the int ``42`` on the MCP
    path would be surprising. Intent is read from the media type.
    """
    if not media_type:
        return False
    base = media_type.split(";", 1)[0].strip().lower()
    return base == "application/json" or base.endswith("+json")


def _is_bodiless_status(status_code: int) -> bool:
    """Is ``status_code`` one where an empty body is the *expected* outcome?

    Only 204 (No Content), 304 (Not Modified), and the 1xx informational range
    carry no body by the HTTP spec — and those are exactly the statuses the REST
    handler renders as a bare bodiless response. An empty body on any *other*
    status (a 3xx redirect, or an empty 4xx / 5xx) is not a "no value" outcome:
    it must NOT silently become ``None`` on the MCP path, so it falls through to
    the ``TypeError`` instead.
    """
    return status_code in (204, 304) or 100 <= status_code < 200


def _normalise_mcp_result(result: Any, fn: Callable[..., Any]) -> Any:
    """Coerce an impl's return value into an MCP tool result.

    :class:`EndpointResponse` is unwrapped to its plain ``body`` — an HTTP
    status code is meaningless to an MCP caller, which only receives the value.

    A Starlette :class:`~starlette.responses.Response` carries HTTP framing
    (status line, headers, raw wire bytes) that a ``tools/call`` result cannot
    represent. The REST handler passes it through verbatim; on the MCP path we
    unwrap it to the value it *represents* so a handler that funnels its output
    through a shared ``-> JSONResponse`` helper is dual-surface without having to
    branch on ``_mcp``:

    * a **JSON-typed** Response (``application/json`` / ``…+json``) → its body,
      ``json.loads``-decoded (the HTTP status is dropped, as with
      :class:`EndpointResponse`);
    * a **bodiless** Response — an empty body on a genuinely no-content status
      (204 / 304 / 1xx) → ``None``. An empty body on *any other* status (a 3xx
      redirect, or an empty 4xx / 5xx) is not a "no value" outcome and raises,
      as below;
    * a **non-JSON-bodied** Response (text, binary, streaming, file download)
      still raises :class:`TypeError` — those bytes have no faithful
      ``tools/call`` representation, so the impl must branch on the injected
      ``_mcp`` flag (``True`` on the MCP path) and return a JSON-serialisable
      value for MCP.

    Unwrapping only ever fires when the return *is* a Response — which on the MCP
    path was previously an unconditional error — so value-returning impls are
    unaffected and nothing that worked before changes behaviour.
    """
    if isinstance(result, EndpointResponse):
        return result.body
    if isinstance(result, Response):
        # StreamingResponse / FileResponse expose no ``.body`` — genuinely
        # REST-only, so treat them like any other non-JSON Response.
        if hasattr(result, "body"):
            body = result.body
            if body in (b"", None):
                # Empty body → ``None`` ONLY on a no-content status (204/304/1xx).
                # An empty-bodied redirect or error (3xx/4xx/5xx) is not a "no
                # value" result and must not silently succeed — fall through to
                # the TypeError so the impl branches on ``_mcp`` instead.
                if _is_bodiless_status(getattr(result, "status_code", 200)):
                    return None
            elif _is_json_media_type(getattr(result, "media_type", None)):
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise TypeError(
                        f"{fn.__name__!r} returned a {type(result).__name__} declared as "
                        f"JSON on the MCP tool path, but its body is not valid JSON ({exc}); "
                        "it cannot be represented as a tools/call result."
                    ) from exc
        raise TypeError(
            f"{fn.__name__!r} returned a Starlette {type(result).__name__} with a "
            "non-JSON body on the MCP tool path, which cannot be represented as a "
            "tools/call result. A text / binary / streaming Response is REST-only; "
            "branch on the injected `_mcp` flag (True on the MCP path) to return a "
            "JSON-serialisable value for MCP."
        )
    return result


async def invoke_impl(fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Call an endpoint implementation without blocking the event loop.

    An ``async def`` implementation is awaited **inline** on the running loop.
    A plain *sync* implementation is dispatched to Starlette's threadpool via
    :func:`starlette.concurrency.run_in_threadpool`, so its blocking work
    (SQLAlchemy / sqlite I/O, ``requests`` calls, CPU) does not stall the event
    loop and starve every other request sharing that worker.

    This restores the offload Starlette gave a *sync* ``@mcp.custom_route``
    handler for free before the ``@endpoint`` conversion: the conversion wraps
    the impl in an ``async`` dispatcher, so a sync impl called directly would
    otherwise run inline on the loop. Both the REST handler and the MCP wire
    wrapper route their impl call through here, so the offload holds on both
    surfaces.

    Mirrors FastAPI's sync-endpoint model: a sync impl (and any sync
    dependency it uses) may run on a worker thread, so a Depends-injected sqlite
    session must be created with ``check_same_thread=False`` — or the impl
    should open its own session inside the function body.

    A sync callable that itself returns an awaitable (rare) is still awaited.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    result = await run_in_threadpool(fn, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class Depends:
    """Marker for a parameter the handler should resolve via ``factory``.

    Used inside ``Annotated[T, Depends(factory)]`` on the implementation
    signature. ``factory`` is invoked with no arguments per request; the
    result is the dependency value.

    If ``factory()`` returns a sync or async context manager the handler
    enters it for the duration of the request and exits before the
    response leaves — so resources clean up cleanly even when the
    implementation raises. Otherwise the value is passed through.
    """

    __slots__ = ("factory",)

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.factory = factory

    def __repr__(self) -> str:
        name = getattr(self.factory, "__name__", None) or repr(self.factory)
        return f"Depends({name})"


def extract_dependencies(fn: Callable[..., Any]) -> dict[str, Depends]:
    """Return ``{param_name: Depends(...)}`` for every dependency-tagged param.

    Walks the function's type hints with ``include_extras=True`` so the
    ``Annotated`` metadata survives, then keeps only the params whose
    metadata includes a :class:`Depends` instance.

    Returns an empty dict when ``fn`` has no Annotated dependencies — the
    handler then parses every accepted parameter from the request as
    before.
    """
    hints = typing.get_type_hints(fn, include_extras=True)
    deps: dict[str, Depends] = {}
    for name, hint in hints.items():
        for meta in getattr(hint, "__metadata__", ()):
            if isinstance(meta, Depends):
                deps[name] = meta
                break
    return deps


async def resolve_dependency(stack: AsyncExitStack, dep: Depends) -> Any:
    """Call ``dep.factory()`` and (when applicable) enter its CM via ``stack``.

    The handler holds one ``AsyncExitStack`` per request so multiple
    dependencies tear down in LIFO order at response time. Supports:

    * **async context managers** (``__aenter__`` / ``__aexit__``) — entered
      via :meth:`AsyncExitStack.enter_async_context`.
    * **sync context managers** (``__enter__`` / ``__exit__``) — entered
      via :meth:`AsyncExitStack.enter_context`. ``SessionLocal()`` from
      SQLAlchemy lands here.
    * **plain values** — returned as-is.
    """
    obj = dep.factory()
    if hasattr(obj, "__aenter__") and hasattr(obj, "__aexit__"):
        return await stack.enter_async_context(obj)
    if hasattr(obj, "__enter__") and hasattr(obj, "__exit__"):
        return stack.enter_context(obj)
    return obj


def _wire_type_for_schema(schema: Mapping[str, Any]) -> Any:
    """Map a parameter's JSON-Schema ``type`` to a Python annotation.

    Only the *structural* type matters for the wire ``inputSchema`` — the same
    scope :func:`~mcp_unified_endpoint.openapi_lookup.coerce` marshals. Value
    constraints (``enum`` / ``minimum`` / ``maximum``) are deliberately dropped
    (the app owns value validation). An ``array`` maps to ``list[<item type>]``;
    an absent or unrecognised type maps to :data:`~typing.Any`.
    """
    declared = schema.get("type")
    if declared == "integer":
        return int
    if declared == "number":
        return float
    if declared == "boolean":
        return bool
    if declared == "string":
        return str
    if declared == "array":
        items = schema.get("items")
        return list[_wire_type_for_schema(items)] if isinstance(items, dict) else list[Any]
    return Any


def _synthesised_param(p: ParamSpec) -> inspect.Parameter:
    """Build a keyword-only :class:`inspect.Parameter` advertising ``p``.

    Used to project an OpenAPI parameter onto the wire signature of a
    ``**kwargs`` (pass-through) handler so FastMCP / Pydantic surface it in the
    MCP ``inputSchema``. Required params get no default (so the schema marks
    them ``required``); optional params carry their spec default, or become
    ``Optional[...] = None`` when the spec declares none.
    """
    base = _wire_type_for_schema(p.schema)
    if p.required:
        annotation: Any = base
        default: Any = inspect.Parameter.empty
    elif p.default is not None:
        annotation = base
        default = p.default
    else:
        annotation = base | None
        default = None
    return inspect.Parameter(
        p.name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
    )


def make_wire_callable(
    fn: Callable[..., Any],
    *,
    pinned: Mapping[str, Any] | None = None,
    spec_params: Sequence[ParamSpec] | None = None,
) -> Callable[..., Any]:
    """Return a callable whose visible signature drops ``fn``'s dependencies.

    FastMCP introspects each tool callable's signature (via Pydantic's
    :class:`~pydantic.TypeAdapter`) to build the ``inputSchema`` MCP
    clients render. Dependency-injected parameters — ``db`` from
    :class:`Depends(SessionLocal)`, etc. — are dispatch-internal; they
    must not appear in the schema (and a SQLAlchemy ``Session`` is not a
    Pydantic-compatible type anyway). This helper builds a slim async
    wrapper that:

    * Exposes only the wire-facing parameters in its ``__signature__``
      and ``__annotations__``, so Pydantic / FastMCP see exactly the
      surface MCP clients should fill in.
    * On invocation, resolves the original function's dependencies via
      :class:`AsyncExitStack` (same lifecycle the REST handler uses) and
      then calls ``fn`` with the merged kwargs.

    ``pinned`` supports the ``@endpoint(..., expand=...)`` fan-out: it maps
    parameter names to fixed values that are **bound** into the call. Each
    pinned parameter is dropped from the wire signature (so it does not
    appear in the MCP ``inputSchema`` — an MCP client neither sees nor
    supplies it) and is injected authoritatively at call time. This turns
    one templated operation (``POST /crm/v8/{module}``) into N per-value
    MCP tools (``create_Leads``, ``create_Deals``, …) that each pin their
    module. Pinned values win over anything a client might send for the
    same name. Names not present in ``fn``'s signature are ignored (the
    caller validates the mapping upfront; this stays defensive).

    ``spec_params`` enables the **pass-through** surface for handlers that
    declare ``**kwargs``. When ``fn`` has a ``**kwargs`` parameter, the given
    OpenAPI parameters are projected onto explicit keyword-only parameters on
    the wire signature, so FastMCP / Pydantic advertise the *full* operation
    input surface in the ``inputSchema`` even though the handler names none of
    them individually. Each param's structural type is honoured (``int`` /
    ``float`` / ``bool`` / ``str`` / ``list[...]``); value constraints
    (``enum`` / bounds) are dropped, matching
    :func:`~mcp_unified_endpoint.openapi_lookup.coerce`. Explicitly-named
    parameters always win over the spec projection. Handlers **without**
    ``**kwargs`` ignore ``spec_params`` entirely and keep the exact same tool
    surface as before.

    When ``fn`` has no Annotated dependencies and nothing is pinned the
    wrapper still normalises :class:`EndpointResponse`, so endpoints that
    don't use ``Depends`` keep the exact same MCP tool surface they had
    before.
    """
    deps = extract_dependencies(fn)
    sig = inspect.signature(fn)
    pinned_kwargs: dict[str, Any] = {
        name: value for name, value in (pinned or {}).items() if name in sig.parameters
    }
    # ``_mcp`` is an optional flag the handler can declare to know whether
    # it is being invoked via the MCP tool path (True) or the REST path
    # (False / absent).  It is injected by the framework — MCP clients must
    # never supply it — so it is excluded from both the wire signature (MCP
    # inputSchema) and the REST request-parameter collection. Pinned
    # parameters are likewise hidden from the wire signature (the framework
    # supplies them, not the client).
    _accepts_mcp = "_mcp" in sig.parameters
    # ``request`` mirrors ``_mcp``: framework-injected, never client-supplied.
    # On this MCP tool path there is no HTTP request, so it is injected as
    # ``None`` (a handler that needs the raw request must branch on it, exactly
    # as it branches on ``_mcp``). Hidden from the wire signature below so it
    # never appears in the MCP ``inputSchema``.
    _accepts_request = "request" in sig.parameters

    def _is_hidden(name: str) -> bool:
        return name in deps or name == "_mcp" or name == "request" or name in pinned_kwargs

    # A ``**kwargs`` (VAR_KEYWORD) parameter marks a *pass-through* handler: it
    # accepts and forwards every remaining spec parameter. ``**kwargs`` /
    # ``*args`` cannot be expressed as ``inputSchema`` fields, so they are
    # dropped from the wire signature; the full input surface is instead
    # advertised by projecting the OpenAPI ``spec_params`` onto explicit
    # keyword-only parameters below.
    has_var_keyword = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    explicit_wire_params = [
        p
        for name, p in sig.parameters.items()
        if not _is_hidden(name)
        and p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    ]
    explicit_names = {p.name for p in explicit_wire_params}
    wire_annotations = {
        name: ann
        for name, ann in getattr(fn, "__annotations__", {}).items()
        if name == "return" or name in explicit_names
    }

    synthesised_params: list[inspect.Parameter] = []
    if has_var_keyword and spec_params:
        seen = set(explicit_names)
        for p_spec in spec_params:
            # Explicit params win over the spec (they are the handler's own
            # view); hidden names (deps / ``_mcp`` / pinned) never advertise.
            # Names that are not valid Python parameters (``from``, ``$.xgafv``,
            # ``oauth-token``) cannot be synthesised — ``inspect.Parameter``
            # would raise and abort ``register_all`` — so they are dropped from
            # the wire surface. The REST path still forwards them by string key
            # into ``**kwargs``; only the MCP ``inputSchema`` omits them.
            if (
                p_spec.name in seen
                or _is_hidden(p_spec.name)
                or not is_wire_safe_param_name(p_spec.name)
            ):
                continue
            seen.add(p_spec.name)
            param = _synthesised_param(p_spec)
            synthesised_params.append(param)
            wire_annotations[p_spec.name] = param.annotation

    wire_params = explicit_wire_params + synthesised_params

    if not deps:
        # No Depends — still wrap to unwrap EndpointResponse for the MCP path.
        # (Status codes are an HTTP-only concept; the MCP caller must receive
        # the plain body, not the EndpointResponse wrapper object.)
        async def wire_nodeps(**kwargs: Any) -> Any:
            if pinned_kwargs:
                kwargs.update(pinned_kwargs)
            if _accepts_mcp:
                kwargs["_mcp"] = True
            if _accepts_request:
                kwargs["request"] = None
            return _normalise_mcp_result(await invoke_impl(fn, **kwargs), fn)

        wire_nodeps.__signature__ = sig.replace(parameters=wire_params)  # type: ignore[attr-defined]
        wire_nodeps.__annotations__ = wire_annotations
        wire_nodeps.__name__ = fn.__name__
        wire_nodeps.__qualname__ = getattr(fn, "__qualname__", fn.__name__)
        wire_nodeps.__doc__ = fn.__doc__
        return wire_nodeps

    async def wire(**kwargs: Any) -> Any:
        async with AsyncExitStack() as stack:
            for name, dep in deps.items():
                kwargs[name] = await resolve_dependency(stack, dep)
            if pinned_kwargs:
                kwargs.update(pinned_kwargs)
            if _accepts_mcp:
                kwargs["_mcp"] = True
            if _accepts_request:
                kwargs["request"] = None
            return _normalise_mcp_result(await invoke_impl(fn, **kwargs), fn)

    wire.__signature__ = sig.replace(parameters=wire_params)  # type: ignore[attr-defined]
    wire.__annotations__ = wire_annotations
    wire.__name__ = fn.__name__
    wire.__qualname__ = getattr(fn, "__qualname__", fn.__name__)
    wire.__doc__ = fn.__doc__
    return wire


__all__ = [
    "Depends",
    "extract_dependencies",
    "invoke_impl",
    "make_wire_callable",
    "resolve_dependency",
]
