"""Per-request dependency injection for ``@endpoint``-decorated functions.

Lets a service-layer function declare values the handler should supply
at dispatch time (database sessions, auth context, tracing scopes, …)
without manually wiring them at every call site::

    from typing import Annotated
    from sqlalchemy.orm import Session

    from mcp_unified_endpoint import Depends, endpoint
    from db.session import SessionLocal

    @endpoint("GET /crm/v9/Notes/{note_id}")
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
import typing
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

from .response import EndpointResponse


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


def make_wire_callable(fn: Callable[..., Any]) -> Callable[..., Any]:
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

    When ``fn`` has no Annotated dependencies the original callable is
    returned unchanged, so endpoints that don't use ``Depends`` keep the
    exact same MCP tool surface they had before.
    """
    deps = extract_dependencies(fn)
    sig = inspect.signature(fn)
    # ``_mcp`` is an optional flag the handler can declare to know whether
    # it is being invoked via the MCP tool path (True) or the REST path
    # (False / absent).  It is injected by the framework — MCP clients must
    # never supply it — so it is excluded from both the wire signature (MCP
    # inputSchema) and the REST request-parameter collection.
    _accepts_mcp = "_mcp" in sig.parameters
    wire_params = [p for name, p in sig.parameters.items() if name not in deps and name != "_mcp"]
    wire_annotations = {
        name: ann
        for name, ann in getattr(fn, "__annotations__", {}).items()
        if name == "return" or (name not in deps and name != "_mcp")
    }

    if not deps:
        # No Depends — still wrap to unwrap EndpointResponse for the MCP path.
        # (Status codes are an HTTP-only concept; the MCP caller must receive
        # the plain body, not the EndpointResponse wrapper object.)
        async def wire_nodeps(**kwargs: Any) -> Any:
            if _accepts_mcp:
                kwargs["_mcp"] = True
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, EndpointResponse):
                return result.body
            return result

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
            if _accepts_mcp:
                kwargs["_mcp"] = True
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, EndpointResponse):
                return result.body
            return result

    wire.__signature__ = sig.replace(parameters=wire_params)  # type: ignore[attr-defined]
    wire.__annotations__ = wire_annotations
    wire.__name__ = fn.__name__
    wire.__qualname__ = getattr(fn, "__qualname__", fn.__name__)
    wire.__doc__ = fn.__doc__
    return wire


__all__ = [
    "Depends",
    "extract_dependencies",
    "make_wire_callable",
    "resolve_dependency",
]
