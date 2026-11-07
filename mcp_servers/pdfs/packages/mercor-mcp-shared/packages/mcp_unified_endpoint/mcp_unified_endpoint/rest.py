"""Synthesise a Starlette route handler from an endpoint declaration.

Takes the implementation function, the OpenAPI ``OperationSpec`` for the
matching ``(method, path)``, and the optional per-endpoint error override
map, and returns the async callable Starlette will invoke for each
inbound request.

The handler:

1. Pulls path parameters from ``request.path_params`` (already extracted
   by Starlette's router).
2. Pulls query parameters from ``request.query_params``. A query param
   whose schema is ``type: array`` is read as all its values (repeated
   ``?k=a&k=b`` when ``explode`` is true — the default — or one
   comma-joined ``?k=a,b`` when false) and coerced element-wise to a
   Python ``list``; scalar params keep first-value semantics.
3. Parses ``application/json`` body for non-GET methods if the operation
   declares a ``requestBody``.
4. Marshals every value through :func:`openapi_lookup.coerce` to the
   declared JSON-Schema ``type`` (``int`` / ``float`` / ``bool`` /
   ``str``) — the minimum needed to build the call. It does **not**
   enforce OAS *value* constraints (``enum`` / ``minimum`` /
   ``maximum``): those stay documentation, and the implementation owns
   value validation plus its own error envelope (so an app can emit a
   domain-correct error — a Gmail-parity ``403`` — instead of a generic
   framework 400). A value that cannot be parsed to the declared type
   is a structural failure and still yields a 400 here.
5. Calls the implementation with **only** the kwargs whose names appear
   in the implementation's signature. The OpenAPI document may declare
   more parameters than a given implementation needs; those are silently
   ignored. A handler that declares ``**kwargs`` opts into *pass-through*:
   every coerced spec parameter is forwarded (only dependency-injected
   params and ``_mcp`` are withheld), so it receives the full operation
   input without naming each parameter. A handler may additionally declare a
   ``request`` parameter to receive the live Starlette ``Request`` on the REST
   path (``None`` on the MCP tool path); like ``_mcp`` it is framework-injected,
   never parsed from the wire, and never advertised in the MCP ``inputSchema``.
6. Wraps the return in ``response_class(…, status_code=success_status)`` —
   the plain :class:`~starlette.responses.JSONResponse` unless the endpoint
   / registrar supplied a subclass (e.g. a null-stripping renderer that
   drops ``None``-valued keys for exact Discovery-shape parity). The same
   class is used for the error-envelope and 400 responses; the no-content
   (204 / 304 / 1xx) branch always sends a bare bodiless ``Response``. An
   implementation that returns a Starlette :class:`~starlette.responses.Response`
   itself (a redirect, streaming / file / plain-text body, custom content
   type) bypasses this wrapping — the ``Response`` is returned verbatim — so
   a non-JSON operation can live under ``@endpoint`` beside JSON ones.
7. Maps registered exceptions to envelopes via :mod:`.errors`.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .dependencies import (
    _is_json_media_type,
    extract_dependencies,
    invoke_impl,
    resolve_dependency,
)
from .errors import ErrorSpec, resolve_error
from .openapi_lookup import OperationSpec, ParamSpec, coerce, coerce_array
from .response import EndpointResponse

RouteHandler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True)
class TypeCoercionError:
    """Context handed to an endpoint's ``on_type_error`` hook.

    Built when a raw request value cannot be parsed to its parameter's declared
    JSON-Schema type — a *structural* failure, since there is no value of the
    right type to pass to the implementation. An endpoint may register a hook
    (``@endpoint(..., on_type_error=...)``) to decide what happens instead of
    the framework's default 400.
    """

    #: The parameter whose value failed to parse (name, location, schema, …).
    parameter: ParamSpec
    #: The raw request value(s): a single string for a scalar parameter, or the
    #: list of raw strings for an ``array`` parameter.
    raw_value: str | list[str]
    #: The original parse error (e.g. ``expected integer, got 'abc'``).
    error: ValueError


#: An endpoint's type-coercion hook. Receives the failure context and either
#: **returns a substitute value** to hand the implementation (e.g. the raw
#: string for a ``str``-typed handler, ``None``, or a clamped fallback), **or
#: raises** a domain exception — which the handler routes through the endpoint's
#: ``on_error`` mapping so the app emits its own error envelope (a Gmail-parity
#: ``400`` / ``403`` in place of the generic framework 400).
TypeCoercionHook = Callable[["TypeCoercionError"], Any]


@dataclass(frozen=True)
class BodyParseError:
    """Context handed to an endpoint's ``on_body_error`` hook.

    Built when the ``application/json`` request body cannot be turned into a
    usable value — either it is **malformed JSON** or it is **absent while the
    operation's ``requestBody`` is ``required``**. Both are framework-level
    request-parse failures that otherwise become the shared 400; an endpoint may
    register a hook (``@endpoint(..., on_body_error=...)``) to decide what
    happens instead.

    A *non-dict but valid* JSON body (a top-level array / string / number) does
    **not** reach this hook — it parses successfully, so there is no parse
    failure. Coercing such a body (e.g. to ``{}``) is the implementation's
    concern, not the framework's.
    """

    #: The raw request body bytes exactly as received (``b""`` when absent). The
    #: hook typically branches on this — e.g. return ``{}`` for an empty body,
    #: raise a domain exception for genuinely malformed content.
    raw_body: bytes
    #: The parse failure. ``str(error)`` is the message the framework would have
    #: used for its default 400 (``"invalid JSON body: …"`` /
    #: ``"request body is required"``); ``error.__cause__`` carries the original
    #: :class:`json.JSONDecodeError` for a malformed body.
    error: Exception


#: An endpoint's body-parse hook. Receives the failure context and either
#: **returns a substitute value** used as the parsed body (e.g. ``{}`` for an
#: empty body), **or raises** a domain exception — routed through the endpoint's
#: ``on_error`` mapping so the app emits its own envelope (a Gmail-parity 400 in
#: place of the generic framework 400). With no hook the failure is the shared
#: ``INVALID_DATA`` 400, exactly as before.
BodyErrorHook = Callable[["BodyParseError"], Any]


@dataclass(frozen=True)
class MissingParamError:
    """Context handed to an endpoint's ``on_missing_param`` hook.

    Built when a **required** ``path`` / ``query`` / ``header`` / ``cookie``
    parameter is **absent** from the request and the parameter declares no
    default — a framework-level request-parse failure that otherwise becomes the
    shared 400. An endpoint may register a hook
    (``@endpoint(..., on_missing_param=...)``) to decide what happens instead:
    substitute a value, or raise a domain exception carrying provider-authentic
    detail (e.g. Zoho's ``REQUIRED_PARAM_MISSING`` naming the missing param).

    This is the *absent* case only. A parameter that is **present but empty**
    (``?module=``) is not missing — its raw value is ``""``, which parses to the
    handler (or, if un-coercible to the declared type, reaches ``on_type_error``).
    Distinguishing absent from empty is exactly what lets an app split
    ``REQUIRED_PARAM_MISSING`` (absent) from ``INVALID_DATA`` (present-but-empty)
    without weakening ``required: true`` in the OpenAPI document.
    """

    #: The required parameter that was absent (its ``name``, ``location``,
    #: ``schema`` — everything the hook needs to name the offending field).
    parameter: ParamSpec
    #: The inbound request, so the hook can consult other parameters / headers
    #: when shaping a substitute value or choosing which domain exception to
    #: raise. Provided for symmetry with the app's needs; the other structural
    #: hooks carry only their failure context because they already receive the
    #: raw value.
    request: Request


#: An endpoint's missing-required-parameter hook. Receives the failure context
#: and either **returns a substitute value** handed to the implementation as if
#: parsed (a sentinel / ``None`` / a computed default), **or raises** a domain
#: exception — routed through the endpoint's ``on_error`` mapping so the app
#: emits its own envelope. With no hook the failure is the shared 400, exactly as
#: before (fully backward compatible).
MissingParamHook = Callable[["MissingParamError"], Any]


class _RequestParseError(ValueError):
    """Internal: a framework-level request-parse failure → default 400.

    Kept distinct from an exception raised inside an ``on_type_error`` hook,
    which the handler instead routes through the ``on_error`` mapping so the
    application controls the envelope.
    """


def validate_response_class(response_class: type[JSONResponse] | None, *, where: str) -> None:
    """Fail loud if ``response_class`` is not a :class:`JSONResponse` subclass.

    ``None`` is always allowed (it selects the default plain ``JSONResponse``).
    Called at decoration / registration time — a misconfigured response class is
    a wiring bug that should surface at boot, not as an opaque ``TypeError`` the
    first time a request is served. ``where`` names the call site for the error
    message (e.g. ``"@endpoint"`` or ``"register_all"``).
    """
    if response_class is None:
        return
    if not (isinstance(response_class, type) and issubclass(response_class, JSONResponse)):
        raise TypeError(
            f"{where} response_class must be a subclass of "
            f"starlette.responses.JSONResponse, got {response_class!r}"
        )


class _StackClosingResponse(Response):
    """ASGI wrapper that closes ``stack`` once ``inner`` has finished sending.

    The REST handler resolves ``Depends`` values through an
    :class:`AsyncExitStack` that normally unwinds when the handler returns —
    which, for a lazily-streamed body (``StreamingResponse`` / ``FileResponse``,
    no materialised ``body``), is *before* Starlette reads the stream. Popping
    the stack out of the handler's ``async with`` and closing it here, in a
    ``finally`` around the inner response's ASGI call, keeps context-managed
    dependency resources open for the whole send and — crucially —
    **guarantees** teardown on normal completion, a mid-stream exception, *and*
    client-disconnect cancellation.

    This replaces an earlier approach that handed teardown to
    ``response.background``. That is unsafe: Starlette runs the background task
    only after a *successful* send, so a stream that raises (or a disconnect
    that propagates a cancellation) would skip it and leak the dependency's
    context manager. A ``try/finally`` around the ASGI call has no such gap.

    The wrapper subclasses :class:`~starlette.responses.Response` purely so it
    satisfies the ``-> Response`` handler contract and Starlette's
    ``request_response`` (which only ever *awaits* the returned object). It
    delegates the entire wire outcome to ``inner``; its own base attributes are
    never read, so the base initialiser is intentionally not invoked.
    """

    __slots__ = ("_inner", "_stack")

    def __init__(self, inner: Response, stack: AsyncExitStack) -> None:
        # Deliberately does not call ``super().__init__``: every wire concern
        # (status, headers, body, background) belongs to ``inner``; this object
        # is a thin lifecycle shim whose only job is to close ``stack`` after
        # the inner response has fully sent.
        self._inner = inner
        self._stack = stack

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self._inner(scope, receive, send)
        finally:
            # Shield the teardown from cancellation. A client disconnect cancels
            # the ASGI task, and AnyIO cancellation is *level-triggered*: a bare
            # ``await self._stack.aclose()`` would be aborted at its first
            # checkpoint — before the ``Depends`` context managers' async
            # ``__aexit__`` runs — re-leaking exactly what this wrapper exists to
            # free on disconnect. A shielded scope lets ``aclose`` run to
            # completion; the pending cancellation resumes propagating after.
            with anyio.CancelScope(shield=True):
                await self._stack.aclose()


def build_handler(
    fn: Callable[..., Any],
    spec: OperationSpec,
    *,
    on_error_override: Mapping[type[BaseException], ErrorSpec] | None = None,
    on_type_error: TypeCoercionHook | None = None,
    on_body_error: BodyErrorHook | None = None,
    on_missing_param: MissingParamHook | None = None,
    response_class: type[JSONResponse] | None = None,
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

    ``response_class`` overrides the JSON response class used for every
    body-carrying REST response this handler emits — the success payload,
    the mapped error envelopes, and the framework 400. ``None`` (default)
    uses the plain :class:`~starlette.responses.JSONResponse`; pass a
    subclass (e.g. one whose ``render`` strips ``None``-valued keys) to
    control the exact wire shape without post-processing each handler. The
    no-content 204 / 304 / 1xx branch is unaffected — it always emits a bare
    bodiless :class:`~starlette.responses.Response`.
    """
    json_response: type[JSONResponse] = response_class or JSONResponse
    sig = inspect.signature(fn)
    accepted_names = set(sig.parameters)
    # ``_mcp`` is injected by the framework (True on the MCP path, False on
    # the REST path).  Exclude it from request-parameter collection so it is
    # never parsed from the HTTP request, and inject it explicitly below.
    _accepts_mcp = "_mcp" in accepted_names
    # ``request`` is likewise framework-injected: the *live* Starlette
    # ``Request`` on this REST path, ``None`` on the MCP tool path (there is no
    # HTTP request there — see ``make_wire_callable``). A handler that needs the
    # raw request (``scope["raw_path"]`` for percent-encoding parity,
    # ``await request.form()`` for multipart) declares ``request`` and the
    # framework supplies it; it is never parsed from the wire nor advertised in
    # the MCP ``inputSchema``.
    _accepts_request = "request" in accepted_names
    dependencies = extract_dependencies(fn)
    accepted_from_request = accepted_names - dependencies.keys() - {"_mcp", "request"}
    # A ``**kwargs`` (VAR_KEYWORD) parameter marks a *pass-through* handler:
    # every coerced spec parameter is forwarded into ``**kwargs`` rather than
    # dropped when the signature does not name it. Dependency, ``_mcp`` and
    # ``request`` names are still withheld (they are injected, never
    # request-parsed).
    _forward_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    _forward_exclude = frozenset(dependencies.keys() | {"_mcp", "request"})
    # Named handlers bind each parameter by its Python-identifier name (dashed /
    # cased headers fold to snake_case — see ``_param_bind_name``). If two spec
    # parameters fold to the *same* identifier the binding would be ambiguous,
    # so fail loud at registration rather than silently drop one at request
    # time. Pass-through handlers keep verbatim wire names, so they are exempt.
    if not _forward_all:
        _bind_origin: dict[str, str] = {}
        for _p in spec.parameters:
            _bind = _param_bind_name(_p.name)
            if _bind not in accepted_from_request:
                continue  # this parameter is dropped, so its binding can't clash
            if _bind in _bind_origin and _bind_origin[_bind] != _p.name:
                raise ValueError(
                    f"parameters {_bind_origin[_bind]!r} and {_p.name!r} both bind to the "
                    f"Python identifier {_bind!r}; rename one in the OpenAPI spec or consume "
                    f"them via a **kwargs pass-through handler"
                )
            _bind_origin[_bind] = _p.name

    async def handler(request: Request) -> Response:
        async with AsyncExitStack() as stack:
            # Filled incrementally by ``_collect_kwargs`` so that if a later
            # parameter's ``on_type_error`` hook raises, the values parsed
            # before it are still available to the ``on_error`` builder (which
            # the package documents reading request values, e.g. ``kw["id"]``).
            collected: dict[str, Any] = {}
            try:
                kwargs = await _collect_kwargs(
                    request,
                    spec,
                    accepted_from_request,
                    on_type_error,
                    forward_all=_forward_all,
                    forward_exclude=_forward_exclude,
                    sink=collected,
                    on_body_error=on_body_error,
                    on_missing_param=on_missing_param,
                )
            except _RequestParseError as exc:
                # Framework-level parse failure (missing required value, bad
                # JSON body, or a type-parse failure with no ``on_type_error``
                # hook) → the shared 400 envelope.
                return _bad_request(str(exc), json_response)
            except Exception as exc:
                # A type-coercion hook raised a domain exception. Route it
                # through the endpoint's ``on_error`` mapping so the app emits
                # its own envelope; if it maps nothing, let it surface. Pass the
                # partially-parsed kwargs so builders can read what was parsed
                # before the failing parameter.
                resolved = resolve_error(exc, collected, overrides=on_error_override)
                if resolved is None:
                    raise
                envelope, status = resolved
                return json_response(envelope, status_code=status)

            for name, dep in dependencies.items():
                kwargs[name] = await resolve_dependency(stack, dep)

            if _accepts_mcp:
                kwargs["_mcp"] = False
            if _accepts_request:
                # The live Starlette request for this REST call — the same
                # object Starlette handed this handler, so a verbatim-wrapped
                # impl sees the byte-exact request (raw_path, headers, unread
                # body / form) it would on the pre-``@endpoint`` route.
                kwargs["request"] = request

            try:
                # Offload sync impls to a worker thread (async impls stay
                # inline) so blocking I/O never stalls the event loop — see
                # ``invoke_impl``. Restores the threadpool offload Starlette
                # gave a sync ``custom_route`` handler pre-``@endpoint``.
                result = await invoke_impl(fn, **kwargs)
            except BaseException as exc:
                resolved = resolve_error(exc, kwargs, overrides=on_error_override)
                if resolved is None:
                    raise
                envelope, status = resolved
                return json_response(envelope, status_code=status)

            # An implementation that returns a fully-formed Starlette
            # ``Response`` owns the entire wire outcome — status, headers,
            # media type, body — so it passes straight through untouched. This
            # lets an operation whose payload isn't JSON (a redirect, a
            # streaming / file / plain-text body, a custom content type) live
            # under ``@endpoint`` beside JSON siblings without being forced
            # through ``response_class``. ``EndpointResponse`` is the package's
            # own dataclass (not a ``Response``), so it is unaffected by this
            # check and keeps its status-override behaviour below.
            if isinstance(result, Response):
                # A lazily-streamed body (``StreamingResponse`` /
                # ``FileResponse`` — no materialised ``body``) is read by
                # Starlette *after* this handler returns, i.e. after the
                # ``AsyncExitStack`` unwinds. If that stream draws on a
                # ``Depends``-yielded resource it would read from a torn-down
                # dependency. Pop the stack out of the ``async with`` and hand
                # it to a wrapper that closes it in a ``finally`` around the
                # response's ASGI send, so dependencies stay open for the whole
                # stream and are torn down even if the stream raises or the
                # client disconnects (a background task would be skipped on
                # error). A buffered Response already holds its bytes, so its
                # teardown stays inline (behaviour unchanged).
                if dependencies and getattr(result, "body", None) is None:
                    return _StackClosingResponse(result, stack.pop_all())
                return result
            if isinstance(result, EndpointResponse):
                return _render_result(result.body, result.status, json_response)
            return _render_result(result, spec.success_status, json_response)
        # ``AsyncExitStack`` swallows the suite's return; mypy/pyright
        # want every branch to terminate. The line above already returned
        # — this is unreachable.
        raise AssertionError("unreachable")  # pragma: no cover

    return handler


# Statuses whose responses MUST NOT carry a message body: 204 No Content
# (RFC 7231 §6.3.5), 304 Not Modified (RFC 7232 §4.1), and every 1xx
# informational status (RFC 7231 §6.2). Emitting a body with these is a
# protocol violation, so the REST tail sends a bare bodiless response.
_NO_CONTENT_STATUSES: frozenset[int] = frozenset({204, 304}) | frozenset(range(100, 200))


def _render_result(
    body: Any, status: int, response_class: type[JSONResponse] = JSONResponse
) -> Response:
    """Serialise a handler result to an HTTP response, honouring no-content statuses.

    For a normal status the ``body`` is JSON-encoded via ``response_class`` (the
    plain :class:`~starlette.responses.JSONResponse` unless the endpoint /
    registrar supplied a subclass — e.g. one that strips ``None``-valued keys).
    For a no-content status (204 / 304 / 1xx) a bare
    :class:`~starlette.responses.Response` is emitted instead: those responses
    MUST NOT carry a message body, and any ``JSONResponse`` would serialise the
    value onto the wire regardless — even ``None`` renders as ``b"null"`` — which
    is a protocol violation. The custom class is therefore intentionally *not*
    consulted on this branch; it only shapes JSON bodies.

    This is the **REST wire** only. The MCP tool path never reaches here: it
    unwraps :class:`~mcp_unified_endpoint.response.EndpointResponse` to its
    ``.body`` directly, so an MCP caller of an operation whose empty result is a
    204 still receives the (possibly empty) payload, while the HTTP response goes
    correctly empty.
    """
    if status in _NO_CONTENT_STATUSES:
        return Response(status_code=status)
    return response_class(body, status_code=status)


async def _collect_kwargs(
    request: Request,
    spec: OperationSpec,
    accepted_names: set[str],
    on_type_error: TypeCoercionHook | None = None,
    *,
    forward_all: bool = False,
    forward_exclude: frozenset[str] = frozenset(),
    sink: dict[str, Any] | None = None,
    on_body_error: BodyErrorHook | None = None,
    on_missing_param: MissingParamHook | None = None,
) -> dict[str, Any]:
    """Parse path / query / body params from ``request`` per ``spec``.

    By default only parameters whose names also appear in ``accepted_names``
    (the implementation's signature) make it into the returned dict. The rest
    are validated for required-ness then dropped, so the implementation can
    stay focused on what it actually uses.

    ``forward_all`` (set when the handler declares ``**kwargs``) instead
    forwards *every* coerced spec parameter, dropping only the names in
    ``forward_exclude`` (dependency-injected params and ``_mcp``, which the
    framework supplies rather than request-parses). The handler's ``**kwargs``
    then receives the full set. The request body stays gated on an explicit
    ``body`` parameter either way (it is a ``requestBody``, not a spec
    parameter, and the MCP ``inputSchema`` advertises spec parameters only).

    ``on_type_error``, when supplied, is invoked for any value that fails to
    parse to its declared type (see :func:`_dispatch_type_error`).

    ``sink``, when given, is the dict accepted values are written into (and is
    returned). The caller passes one so that if a parameter's ``on_type_error``
    hook raises partway through, the values parsed *before* it remain visible to
    the ``on_error`` builder. When omitted a fresh dict is used.

    ``on_body_error``, when supplied, is invoked when the JSON body is malformed
    or absent-while-required (see :func:`_dispatch_body_error`). The body is
    parsed *after* the parameters, so a body hook that raises still leaves the
    parsed params visible in ``sink`` to the ``on_error`` builder.

    Body parsing is **content-type-aware**: the JSON parse (and its
    required-body / malformed-body handling) applies only when the request
    content-type is JSON (``application/json`` / ``…+json``) or absent — the
    latter preserving the historical default that an unlabelled body is JSON. A
    request whose content-type is explicitly *non-JSON* (``multipart/form-data``,
    ``application/octet-stream``, …) is never read or JSON-parsed here even when
    the operation declares a ``requestBody``: the injected ``body`` is left
    ``None`` and no ``required``-body 400 is raised, so a handler can own the raw
    body via ``await request.form()`` / ``await request.body()`` while the OAS
    keeps documenting the requestBody.

    ``on_missing_param``, when supplied, is invoked when a *required* parameter
    is absent (see :func:`_dispatch_missing_param`) before the framework 400.
    """

    out: dict[str, Any] = sink if sink is not None else {}
    for p in spec.parameters:
        if _is_array_query(p):
            value = _coerce_array_or_default(request, p, on_type_error, on_missing_param)
        else:
            raw = _raw_value(request, p)
            value = _coerce_or_default(raw, p, on_type_error, request, on_missing_param)
        # A pass-through (``**kwargs``) handler keeps the wire name verbatim as
        # the kwargs key — preserving today's behavior byte-for-byte (a dashed
        # header lands under ``kwargs["If-Modified-Since"]``). A named handler
        # binds by the parameter's Python-identifier name (see
        # :func:`_param_bind_name`), so a dashed/cased header parameter such as
        # ``If-Modified-Since`` reaches a snake_case ``if_modified_since`` param.
        if forward_all:
            if p.name not in forward_exclude:
                out[p.name] = value
        elif (bind := _param_bind_name(p.name)) in accepted_names:
            out[bind] = value

    if spec.body is not None:
        content_type = request.headers.get("content-type")
        if content_type is not None and not _is_json_media_type(content_type):
            # A non-JSON request body — ``multipart/form-data`` (a file upload),
            # ``application/octet-stream`` (raw bytes), etc. The framework does
            # NOT read or JSON-parse it: ``json.loads`` on binary bytes would
            # raise ``UnicodeDecodeError`` and, even guarded, a ``required``
            # requestBody would then turn every valid upload into a 400. The
            # handler owns the body instead — ``await request.form()`` for
            # multipart, ``await request.body()`` for raw bytes (declare a
            # ``request`` parameter to receive the live request). The declared
            # requestBody stays in the OAS for documentation; a non-JSON
            # content-type is a *present* body of another shape, not an absent
            # one, so a ``required`` body does NOT raise the framework 400 here.
            # The injected ``body`` value is left ``None``.
            if "body" in accepted_names:
                out["body"] = None
        else:
            # JSON content-type — or none at all. An unlabelled body is treated
            # as JSON, preserving the historical default: an absent *required*
            # body still 400s and a bare ``content=b"{...}"`` POST still parses.
            body_bytes = await request.body()
            parsed = _parse_json_body(body_bytes, spec.body, on_body_error)
            if "body" in accepted_names:
                out["body"] = parsed

    return out


def _parse_json_body(
    body_bytes: bytes, body_spec: ParamSpec, on_body_error: BodyErrorHook | None
) -> Any:
    """Parse the JSON request body, routing failures to ``on_body_error``.

    Failures — malformed JSON, or an absent body when ``body_spec.required`` —
    are dispatched via :func:`_dispatch_body_error`: with no hook they re-raise
    as the framework 400; a hook may return a substitute value (used as the
    parsed body) or raise a domain exception (routed through ``on_error``). An
    empty *optional* body stays ``None`` and a *valid non-dict* body is returned
    as-is — neither is a parse failure, so neither reaches the hook.
    """
    if body_bytes:
        try:
            return json.loads(body_bytes)
        except json.JSONDecodeError as exc:
            err = ValueError(f"invalid JSON body: {exc.msg}")
            err.__cause__ = exc
            return _dispatch_body_error(body_bytes, err, on_body_error)
        except UnicodeDecodeError as exc:
            # Non-UTF-8 bytes reaching the JSON path (a body with no content-type
            # header, or one mislabelled as JSON). ``json.loads`` raises
            # ``UnicodeDecodeError`` — a ``ValueError`` subclass but NOT a
            # ``JSONDecodeError`` — which previously escaped this guard as an
            # unhandled 500. Route it through the same body-error path so it
            # becomes the shared 400 (or the endpoint's ``on_body_error``).
            # Genuinely-binary uploads never reach here: their non-JSON
            # content-type is skipped in ``_collect_kwargs`` before body read.
            err = ValueError("invalid JSON body: not valid UTF-8")
            err.__cause__ = exc
            return _dispatch_body_error(body_bytes, err, on_body_error)
    if body_spec.required:
        return _dispatch_body_error(
            body_bytes, ValueError("request body is required"), on_body_error
        )
    return None


def _dispatch_body_error(
    raw_body: bytes, error: Exception, on_body_error: BodyErrorHook | None
) -> Any:
    """Route a body-parse failure to the endpoint's ``on_body_error`` hook.

    With no hook the failure re-raises as a framework parse error (the shared
    400, message preserved from ``error``). A hook may **return a substitute
    value** (used as the parsed body) or **raise its own domain exception**
    (which the handler routes through the endpoint's ``on_error`` mapping so the
    app owns the envelope).
    """
    if on_body_error is None:
        raise _RequestParseError(str(error))
    return on_body_error(BodyParseError(raw_body=raw_body, error=error))


def _param_bind_name(name: str) -> str:
    """The Python-identifier a parameter binds to on a *named* handler.

    An OpenAPI parameter name that is already a valid identifier binds verbatim
    (unchanged behavior). A name that is not — a dashed/cased HTTP header
    (``If-Modified-Since``, ``X-EXTERNAL``, ``content-type``) or a bracketed
    query key (``filter[status]``) — is folded to a canonical snake_case
    identifier: every run of non-alphanumeric characters collapses to a single
    ``_``, the result is lowercased and stripped of leading/trailing ``_``, and
    a leading digit is prefixed with ``_``. So ``If-Modified-Since`` →
    ``if_modified_since``, ``X-EXTERNAL`` → ``x_external``, ``content-type`` →
    ``content_type``.

    The parameter keeps its **wire name** everywhere else — the
    :func:`_raw_value` request lookup (case-insensitive for headers via
    Starlette's ``Headers``), the OpenAPI document, the MCP ``inputSchema``, and
    ``MissingParamError.parameter.name`` — so ``required: true`` and the hooks
    stay honest; only the *handler kwarg key* is canonicalized. Because a
    non-identifier name could never bind to a named parameter before, this is
    fully backward compatible; pass-through (``**kwargs``) handlers are
    unaffected (they keep the verbatim wire name as their key).
    """
    if name.isidentifier():
        return name
    folded = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not folded:
        return name
    if folded[0].isdigit():
        folded = f"_{folded}"
    return folded


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


def _coerce_or_default(
    raw: str | None,
    p: ParamSpec,
    on_type_error: TypeCoercionHook | None,
    request: Request,
    on_missing_param: MissingParamHook | None = None,
) -> Any:
    """Apply default → required check → type parse, in that order.

    An absent *required* parameter is dispatched to ``on_missing_param`` (or,
    with no hook, becomes a framework 400) — see :func:`_dispatch_missing_param`.
    A type-parse failure is dispatched to ``on_type_error`` (or, with no hook,
    becomes a framework 400) — see :func:`_dispatch_type_error`.
    """
    if raw is None:
        if p.default is not None:
            return p.default
        if p.required:
            return _dispatch_missing_param(p, request, on_missing_param)
        return None
    try:
        return coerce(raw, p.schema)
    except ValueError as exc:
        return _dispatch_type_error(p, raw, exc, on_type_error)


def _schema_declares_array(schema: Any) -> bool:
    """True when a JSON-Schema node structurally declares an ``array``.

    Recognises the literal ``{"type": "array"}``, the nullable list form
    ``{"type": ["array", "null"]}`` (JSON-Schema / OpenAPI 3.1 type arrays), and
    a ``type: array`` nested inside a single ``allOf`` / ``anyOf`` / ``oneOf``
    branch (a common way specs attach constraints or nullability to an array).

    It deliberately does **not** resolve a bare ``$ref`` — the REST handler is
    built from the un-dereferenced ``OperationSpec`` and has no ``components``
    map in scope, so a query param whose schema is a top-level ``$ref`` to an
    array keeps scalar first-value semantics. That is a rare shape for a query
    parameter and resolving it would require plumbing ``components`` through the
    whole build path; left as a documented limitation.
    """
    return _array_schema(schema) is not None


def _array_schema(schema: Any) -> dict[str, Any] | None:
    """Return the array-declaring JSON-Schema node, or ``None`` if not an array.

    For a literal ``{"type": "array"}`` (including the nullable list form
    ``{"type": ["array", "null"]}``) this is ``schema`` itself. When array-ness
    comes from an ``allOf`` / ``anyOf`` / ``oneOf`` branch, the ``items`` live on
    that branch — not the top level — so the branch node is returned. Callers
    that need element type-parsing (``coerce_array``) must read ``items`` from
    *this* node, not the original schema, or a combinator array's element type is
    lost and e.g. an integer array reaches the handler as strings. A bare
    ``$ref`` is not resolved (documented limitation; see below)."""
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if declared == "array" or (isinstance(declared, list) and "array" in declared):
        return schema
    for combinator in ("allOf", "anyOf", "oneOf"):
        members = schema.get(combinator)
        if isinstance(members, list):
            for member in members:
                found = _array_schema(member)
                if found is not None:
                    return found
    return None


def _is_array_query(p: ParamSpec) -> bool:
    """True for a query parameter whose schema declares ``type: array``.

    Only query arrays are hoisted to a Python ``list`` here — that's where
    repeated (``?labelIds=INBOX&labelIds=SENT``) and comma-joined
    (``?labelIds=INBOX,SENT``) values arrive. Array-typed path / header /
    cookie params are rare and keep the scalar path. Array-ness is detected
    structurally (literal, nullable-list, or ``allOf``/``anyOf``/``oneOf``
    branch) via :func:`_schema_declares_array`.
    """
    return p.location == "query" and _schema_declares_array(p.schema)


def _raw_values(request: Request, p: ParamSpec) -> list[str]:
    """All raw strings for an array query param, honoring ``explode``.

    ``explode`` true (the OpenAPI/Google default) → each repeated occurrence
    of the key is one element: ``getlist`` returns them directly. ``explode``
    false → the values arrive as a single comma-joined string, so each raw
    occurrence is split on ``,`` and the results are flattened. Empty string
    segments from a trailing/duplicate comma are dropped.
    """
    raw = request.query_params.getlist(p.name)
    if p.explode:
        return raw
    out: list[str] = []
    for chunk in raw:
        out.extend(part for part in chunk.split(",") if part != "")
    return out


def _coerce_array_or_default(
    request: Request,
    p: ParamSpec,
    on_type_error: TypeCoercionHook | None,
    on_missing_param: MissingParamHook | None = None,
) -> Any:
    """Array analogue of :func:`_coerce_or_default`.

    Absent (no occurrences) mirrors scalar semantics exactly: an explicit
    ``default`` wins, else a required param is dispatched to ``on_missing_param``
    (or becomes a framework 400 with no hook), else ``None`` (so the impl can
    ``labelIds or []`` just as it would for a scalar). Present values are
    type-parsed element-by-element against the schema's ``items`` type; a
    failing element is dispatched to ``on_type_error`` (with the full raw value
    list as context) or becomes a framework 400.
    """
    values = _raw_values(request, p)
    if not values:
        if p.default is not None:
            return p.default
        if p.required:
            return _dispatch_missing_param(p, request, on_missing_param)
        return None
    # Coerce against the array-declaring node so a combinator array
    # (``anyOf: [{type: array, items: {type: integer}}, {type: null}]``) parses
    # its elements against the branch's ``items``; ``p.schema`` here may only
    # carry the combinator wrapper, whose top level has no ``items``.
    array_schema = _array_schema(p.schema) or p.schema
    try:
        return coerce_array(values, array_schema)
    except ValueError as exc:
        return _dispatch_type_error(p, values, exc, on_type_error)


def _dispatch_type_error(
    p: ParamSpec,
    raw: str | list[str],
    exc: ValueError,
    on_type_error: TypeCoercionHook | None,
) -> Any:
    """Route a structural type-parse failure to the endpoint's hook.

    With no hook the failure re-raises as a framework parse error (the shared
    400). A hook may **return a substitute value** (used in place of the parsed
    value) or **raise its own domain exception** (which the handler routes
    through the endpoint's ``on_error`` mapping so the app owns the envelope).
    """
    if on_type_error is None:
        raise _RequestParseError(str(exc)) from exc
    return on_type_error(TypeCoercionError(parameter=p, raw_value=raw, error=exc))


def _dispatch_missing_param(
    p: ParamSpec, request: Request, on_missing_param: MissingParamHook | None
) -> Any:
    """Route an absent *required* parameter to the endpoint's hook.

    With no hook the failure re-raises as a framework parse error (the shared
    400, message unchanged from the previous inline raise). A hook may **return a
    substitute value** (handed to the implementation as if parsed) or **raise its
    own domain exception** (which the handler routes through the endpoint's
    ``on_error`` mapping so the app owns the envelope — e.g. Zoho's
    ``REQUIRED_PARAM_MISSING``).
    """
    if on_missing_param is None:
        raise _RequestParseError(f"missing required {p.location} parameter {p.name!r}")
    return on_missing_param(MissingParamError(parameter=p, request=request))


def _bad_request(message: str, response_class: type[JSONResponse] = JSONResponse) -> JSONResponse:
    """Default 400 envelope when a request fails parameter validation.

    Rendered through ``response_class`` so a custom (e.g. null-stripping) JSON
    renderer shapes the framework 400 identically to success and mapped-error
    responses.
    """
    return response_class(
        {
            "code": "INVALID_DATA",
            "message": message,
            "details": {},
            "status": "error",
        },
        status_code=400,
    )


__all__ = [
    "BodyErrorHook",
    "BodyParseError",
    "EndpointResponse",
    "RouteHandler",
    "TypeCoercionError",
    "TypeCoercionHook",
    "build_handler",
    "validate_response_class",
]
