"""Synthesise a Starlette route handler from an endpoint declaration.

Takes the implementation function, the OpenAPI ``OperationSpec`` for the
matching ``(method, path)``, and the optional per-endpoint error override
map, and returns the async callable Starlette will invoke for each
inbound request.

The handler:

1. Pulls path parameters from ``request.path_params`` (already extracted
   by Starlette's router).
2. Pulls query parameters from ``request.query_params``.
3. Parses ``application/json`` body for non-GET methods if the operation
   declares a ``requestBody``.
4. Coerces every value through :func:`openapi_lookup.coerce` so the
   schema's ``type`` / ``enum`` / ``minimum`` / ``maximum`` constraints
   are enforced exactly once, here, against the same JSON-Schema the
   OpenAPI document publishes — no second copy at the call site.
5. Calls the implementation with **only** the kwargs whose names appear
   in the implementation's signature. The OpenAPI document may declare
   more parameters than a given implementation needs; those are silently
   ignored.
6. Wraps the return in ``JSONResponse(…, status_code=success_status)``.
7. Maps registered exceptions to envelopes via :mod:`.errors`.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .dependencies import extract_dependencies, resolve_dependency
from .errors import ErrorSpec, resolve_error
from .openapi_lookup import OperationSpec, ParamSpec, coerce
from .response import EndpointResponse

RouteHandler = Callable[[Request], Awaitable[Response]]


def build_handler(
    fn: Callable[..., Any],
    spec: OperationSpec,
    *,
    on_error_override: Mapping[type[BaseException], ErrorSpec] | None = None,
) -> RouteHandler:
    """Return the Starlette callable for one endpoint.

    Inspects ``fn``'s signature once at build time to separate:

    * **Dependency-injected** parameters — those typed
      ``Annotated[T, Depends(factory)]`` — resolved per request via the
      handler's :class:`AsyncExitStack` (so context managers tear down).
    * **Request-parsed** parameters — everything else, parsed from
      path / query / header / cookie / JSON body according to the
      OpenAPI ``OperationSpec``.

    Dependency names are excluded from the request-parsing path so a
    ``Depends`` annotation always wins over an equally-named OpenAPI
    parameter (collisions are almost always a bug at the call site).
    """
    sig = inspect.signature(fn)
    accepted_names = set(sig.parameters)
    # ``_mcp`` is injected by the framework (True on the MCP path, False on
    # the REST path).  Exclude it from request-parameter collection so it is
    # never parsed from the HTTP request, and inject it explicitly below.
    _accepts_mcp = "_mcp" in accepted_names
    dependencies = extract_dependencies(fn)
    accepted_from_request = accepted_names - dependencies.keys() - {"_mcp"}

    async def handler(request: Request) -> Response:
        async with AsyncExitStack() as stack:
            try:
                kwargs = await _collect_kwargs(request, spec, accepted_from_request)
            except ValueError as exc:
                return _bad_request(str(exc))

            for name, dep in dependencies.items():
                kwargs[name] = await resolve_dependency(stack, dep)

            if _accepts_mcp:
                kwargs["_mcp"] = False

            try:
                result = fn(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except BaseException as exc:
                resolved = resolve_error(exc, kwargs, overrides=on_error_override)
                if resolved is None:
                    raise
                envelope, status = resolved
                return JSONResponse(envelope, status_code=status)

            if isinstance(result, EndpointResponse):
                return JSONResponse(result.body, status_code=result.status)
            return JSONResponse(result, status_code=spec.success_status)
        # ``AsyncExitStack`` swallows the suite's return; mypy/pyright
        # want every branch to terminate. The line above already returned
        # — this is unreachable.
        raise AssertionError("unreachable")  # pragma: no cover

    return handler


async def _collect_kwargs(
    request: Request,
    spec: OperationSpec,
    accepted_names: set[str],
) -> dict[str, Any]:
    """Parse path / query / body params from ``request`` per ``spec``.

    Only parameters whose names also appear in ``accepted_names`` (the
    implementation's signature) make it into the returned dict. The rest
    are validated for required-ness then dropped, so the implementation
    can stay focused on what it actually uses.
    """
    out: dict[str, Any] = {}
    for p in spec.parameters:
        raw = _raw_value(request, p)
        value = _coerce_or_default(raw, p)
        if p.name in accepted_names:
            out[p.name] = value

    if spec.body is not None:
        body_bytes = await request.body()
        if body_bytes:
            try:
                parsed = json.loads(body_bytes)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON body: {exc.msg}") from exc
        elif spec.body.required:
            raise ValueError("request body is required")
        else:
            parsed = None
        if "body" in accepted_names:
            out["body"] = parsed

    return out


def _raw_value(request: Request, p: ParamSpec) -> str | None:
    """Pull the raw string for ``p`` from the appropriate request location."""
    if p.location == "path":
        return request.path_params.get(p.name)
    if p.location == "query":
        return request.query_params.get(p.name)
    if p.location == "header":
        return request.headers.get(p.name)
    if p.location == "cookie":
        return request.cookies.get(p.name)
    return None


def _coerce_or_default(raw: str | None, p: ParamSpec) -> Any:
    """Apply default → required check → schema coercion, in that order."""
    if raw is None:
        if p.default is not None:
            return p.default
        if p.required:
            raise ValueError(f"missing required {p.location} parameter {p.name!r}")
        return None
    return coerce(raw, p.schema)


def _bad_request(message: str) -> JSONResponse:
    """Default 400 envelope when a request fails parameter validation."""
    return JSONResponse(
        {
            "code": "INVALID_DATA",
            "message": message,
            "details": {},
            "status": "error",
        },
        status_code=400,
    )


__all__ = ["EndpointResponse", "RouteHandler", "build_handler"]
