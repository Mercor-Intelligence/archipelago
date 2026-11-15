"""Backfill uncovered OpenAPI operations with default *not-implemented* tools.

``register_all(..., backfill_missing=...)`` uses this module to turn every
operation in the OpenAPI document that no ``@endpoint`` explicitly covers into
an MCP tool whose **name and input schema are real** but whose invocation
returns a ``501 NOT_IMPLEMENTED`` envelope. This lets a server expose its whole
documented REST surface as a uniformly-named wrapper family (e.g. under an
``api_`` prefix via :func:`~mcp_unified_endpoint.register_name_mutator`) while
only hand-implementing the operations it actually serves.

Design — reproduces the flat mapping proven across ~295 operations in the
Foundry-Google-Workspace ``_rest_wrappers.py`` generator (which this replaces):

* A stub callable is synthesised per operation with a real ``__signature__``
  derived from the operation's parameters, so it flows through the ordinary
  :func:`~mcp_unified_endpoint.register_all` tool path — the name mutator and
  the concrete-name collision guard apply for free, with **no** new FastMCP
  registration primitive.
* Schema fidelity is deliberately **flat**: path + query parameters and each
  top-level request-body property become one keyword-only parameter, typed
  coarsely (``object`` → ``dict``, ``array`` → ``list``, …). Nested object
  structure, array item types, ``enum`` and ``format`` are intentionally not
  recursed — this matches the validated wrapper surface exactly. Richer,
  nested fidelity is a deliberate future enhancement, not this pass.
* Only **operation-level** parameters are read (not path-item-level shared
  parameters), mirroring the reference generator so the produced surface is
  identical.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .openapi_lookup import is_wire_safe_param_name
from .registry import EndpointDecl
from .response import EndpointResponse

#: HTTP methods that map to an operation object in an OpenAPI path item.
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch"})

#: JSON-Schema ``type`` → Python annotation (drives the MCP ``inputSchema``).
_JSON_TO_PY: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def snake_operation_id(operation_id: str) -> str:
    """``operationId`` → ``snake_case`` tool-name fragment.

    Distinct from the decorator's default name derivation (which merely
    lowercases an already-snake Python function name): ``operationId`` values
    are commonly camelCase (``gmailMessageList``), so this splits case
    boundaries before lowercasing (``gmail_message_list``) and collapses any
    run of non-alphanumerics to a single ``_``. The result then passes through
    the registered name mutator like every other generated name.
    """
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", operation_id)
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    return re.sub(r"_+", "_", s) or "op"


def _default_not_implemented_envelope(operation_id: str, method: str, path: str) -> dict[str, Any]:
    """Default body returned when a backfilled wrapper tool is invoked."""
    return {
        "error": {
            "code": 501,
            "status": "NOT_IMPLEMENTED",
            "message": (
                f"{operation_id} ({method} {path}) is not implemented in this "
                "MCP wrapper layer; use the corresponding implemented tool or "
                "the live REST endpoint."
            ),
        }
    }


@dataclass(frozen=True)
class BackfillConfig:
    """Options for the :func:`register_all` ``backfill_missing`` pass.

    Attributes:
        include_routes: When ``False`` (default) backfilled operations register
            an MCP tool **only** — no Starlette REST route is mounted, so a
            tools-only proxy server is unnecessary. Set ``True`` to also mount a
            REST route whose handler returns the not-implemented envelope.
        on_missing_operation_id: What to do for an operation with no
            ``operationId`` (no stable name can be derived): ``"skip"``
            (default) records it in :attr:`RegistrationReport.backfill_skipped`
            and warns; ``"error"`` raises ``ValueError``.
        envelope_factory: Builds the not-implemented body, called
            ``(operation_id, method, path) -> dict``. Defaults to a
            ``{"error": {"code": 501, "status": "NOT_IMPLEMENTED", ...}}`` shape.
    """

    include_routes: bool = False
    on_missing_operation_id: str = "skip"
    envelope_factory: Callable[[str, str, str], dict[str, Any]] = field(
        default=_default_not_implemented_envelope
    )


def normalise_backfill(value: bool | BackfillConfig | Any) -> BackfillConfig | None:
    """Normalise the ``backfill_missing`` argument to a config or ``None`` (off).

    Accepts ``False``/``None`` (off), ``True`` or the ``NotImplemented``
    sentinel (default config — the latter honours the terse
    ``backfill_missing=NotImplemented`` spelling), or an explicit
    :class:`BackfillConfig`.
    """
    if value is None or value is False:
        return None
    if value is True or value is NotImplemented:
        return BackfillConfig()
    if isinstance(value, BackfillConfig):
        if value.on_missing_operation_id not in ("skip", "error"):
            raise ValueError(
                "BackfillConfig.on_missing_operation_id must be 'skip' or 'error', "
                f"got {value.on_missing_operation_id!r}"
            )
        return value
    raise TypeError(
        "backfill_missing must be a bool, a BackfillConfig, or NotImplemented; "
        f"got {type(value).__name__}"
    )


#: The env var that toggles the backfill pass at deploy time, independent of the
#: ``register_all(backfill_missing=...)`` code argument. See :func:`resolve_backfill`.
ENV_VAR: str = "MCP_BACKFILL_MISSING"

#: Tokens that, case-folded and whitespace-trimmed, count as truthy. Mirrors the
#: contract in :mod:`mcp_unified_endpoint.layer_switch` so the two MCP-side
#: operator toggles read env values the same way.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _env_override() -> bool | None:
    """Tri-state read of :data:`ENV_VAR` from ``os.environ``.

    * **Unset** → ``None`` (defer entirely to the code argument).
    * A **truthy** token (``1`` / ``true`` / ``yes`` / ``on``, any case) →
      ``True`` (force the backfill pass on).
    * **Any other value** (``0`` / ``false`` / ``no`` / ``off`` / ``garbage``) →
      ``False`` (force it off).

    Read fresh each call (no caching), matching
    :func:`mcp_unified_endpoint.layer_switch.is_layer_enabled` — Studio bakes the
    operator-selected value at container-build time, so a test can flip it with
    ``monkeypatch.setenv`` and observe the new state immediately.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUTHY


def resolve_backfill(value: bool | BackfillConfig | Any) -> BackfillConfig | None:
    """Combine the :data:`ENV_VAR` operator override with the code argument.

    The env var decides *whether* backfill runs; the code argument decides *its
    shape* when it does. Precedence (tri-state override):

    * env **unset** → :func:`normalise_backfill` of ``value`` unchanged — full
      back-compat, existing callers behave exactly as before.
    * env **truthy** → force **on**: honour a caller-supplied
      :class:`BackfillConfig` (its routes / envelope / missing-``operationId``
      behaviour is preserved); otherwise fall back to a default
      :class:`BackfillConfig`.
    * env **falsy** → force **off**: return ``None`` regardless of ``value`` — an
      operator kill-switch that overrides even an explicit ``backfill_missing=``.

    ``value`` is still validated in every branch except the falsy kill-switch, so
    a malformed ``backfill_missing`` argument fails loud whenever backfill could
    run (matching :func:`normalise_backfill`'s ``TypeError`` / ``ValueError``).
    """
    override = _env_override()
    if override is False:
        return None
    if override is True:
        return normalise_backfill(value) or BackfillConfig()
    return normalise_backfill(value)


def _deref(schema: Any, components: dict[str, Any], _seen: frozenset[str] = frozenset()) -> Any:
    """Resolve a top-level ``$ref`` against ``components/schemas`` (cycle-safe)."""
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in _seen:
            return {"type": "object"}
        return _deref(components.get(key, {}), components, _seen | {key})
    return schema


def _deref_request_body(
    body: Any, request_bodies: dict[str, Any], _seen: frozenset[str] = frozenset()
) -> Any:
    """Resolve a ``requestBody`` that is itself a ``$ref`` (cycle-safe).

    OpenAPI lets an operation reference a shared request body:
    ``requestBody: {$ref: '#/components/requestBodies/CreateMessage'}``. Such a
    ref points at ``components/requestBodies`` (a *different* section from the
    schema ``$ref`` targets), so without resolving it here ``body.get("content")``
    is empty and every body property is silently dropped from the stub schema.
    """
    if not isinstance(body, dict):
        return body
    ref = body.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in _seen:
            return {}
        return _deref_request_body(request_bodies.get(key, {}), request_bodies, _seen | {key})
    return body


def _deref_parameter(
    param: Any, parameters: dict[str, Any], _seen: frozenset[str] = frozenset()
) -> Any:
    """Resolve a parameter that is itself a ``$ref`` (cycle-safe).

    OpenAPI lets an operation reference a shared parameter:
    ``parameters: [{$ref: '#/components/parameters/PageToken'}]``. Such a ref
    points at ``components/parameters`` (a *different* section from the schema
    and requestBody ``$ref`` targets), so without resolving it here the object
    has no ``in`` / ``name`` and is silently dropped from the stub signature and
    the MCP ``inputSchema`` — mirroring the requestBody-``$ref`` drop
    :func:`_deref_request_body` fixes.
    """
    if not isinstance(param, dict):
        return param
    ref = param.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in _seen:
            return {}
        return _deref_parameter(parameters.get(key, {}), parameters, _seen | {key})
    return param


def _annotation(schema: Any, components: dict[str, Any]) -> Any:
    """Map a (possibly ``$ref``) JSON-Schema node to a coarse Python annotation."""
    node = _deref(schema, components)
    if not isinstance(node, dict):
        return Any
    jtype = node.get("type")
    if isinstance(jtype, list):  # e.g. ["string", "null"]
        jtype = next((t for t in jtype if t != "null"), None)
    return _JSON_TO_PY.get(jtype, Any) if jtype else Any


def _params_for_operation(
    op: dict[str, Any],
    components: dict[str, Any],
    request_bodies: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
) -> list[inspect.Parameter]:
    """Build the flat, top-level keyword-only parameter list for one operation.

    Path + query parameters and each top-level request-body property become a
    single keyword-only parameter. Required inputs get no default (so the MCP
    ``inputSchema`` marks them ``required``); optional ones default to ``None``.
    Names are de-duplicated (first occurrence wins) so the synthesised
    signature is always valid. Only ``application/json``-style top-level body
    properties are hoisted; nested structure is not recursed.

    A path parameter defaults to *required* when the spec omits ``required``
    (path params are always required per OpenAPI), matching ``openapi_lookup``;
    query params default to optional. A parameter that is itself a ``$ref`` into
    ``components/parameters`` is resolved via :func:`_deref_parameter` before its
    ``in`` is inspected. A ``requestBody`` that is itself a ``$ref`` into
    ``components/requestBodies`` is resolved via :func:`_deref_request_body`
    before its properties are hoisted, and the ``application/json`` media type is
    preferred (falling back to the first entry) so the hoisted body matches the
    JSON body ``openapi_lookup`` models.
    """
    request_bodies = request_bodies or {}
    parameters = parameters or {}
    params: list[inspect.Parameter] = []
    seen: set[str] = set()

    def _add(name: str, annotation: Any, required: bool) -> None:
        # ``inspect.Parameter`` rejects names that are non-identifiers OR Python
        # keywords, and ``"from".isidentifier()`` is ``True`` — so a plain
        # ``isidentifier()`` guard still lets ``from`` (a real Google-spec param
        # name) through and crashes signature synthesis, aborting the whole
        # backfill pass. ``is_wire_safe_param_name`` adds the load-bearing
        # keyword check. A dropped name is not fatal here: the stub accepts
        # ``**_kwargs`` and returns not-implemented regardless.
        if name in seen or not is_wire_safe_param_name(name):
            return
        seen.add(name)
        default = inspect.Parameter.empty if required else None
        ann = annotation if required else (annotation | None if annotation is not Any else Any)
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=ann,
            )
        )

    for raw_p in op.get("parameters", []):
        # A parameter may be a ``$ref`` into ``components/parameters`` (a shared
        # parameter definition). Resolve it first — an unresolved ref has no
        # ``in`` / ``name`` and would be silently dropped below.
        p = _deref_parameter(raw_p, parameters)
        if not isinstance(p, dict) or p.get("in") not in ("path", "query"):
            continue
        _add(
            str(p.get("name", "")),
            _annotation(p.get("schema", {}), components),
            # Path params are required by definition; only query params may be
            # optional. Mirrors ``openapi_lookup.lookup``'s ``in == "path"`` default.
            bool(p.get("required", p.get("in") == "path")),
        )

    body = _deref_request_body(op.get("requestBody"), request_bodies)
    if isinstance(body, dict):
        content = body.get("content", {})
        if isinstance(content, dict) and content:
            # Prefer ``application/json`` (the body the rest of the package
            # models), falling back to the first entry — mirrors ``lookup``'s
            # ``content.get("application/json") or next(iter(...))``. Iterating
            # ``.items()`` and breaking picked whatever the spec listed first,
            # so an ``application/xml``-first op hoisted the wrong schema.
            media = content.get("application/json") or next(iter(content.values()), {})
            raw_schema = media.get("schema", {}) if isinstance(media, dict) else {}
            body_schema = _deref(raw_schema, components)
            if isinstance(body_schema, dict):
                required = set(body_schema.get("required", []))
                for prop, sub in body_schema.get("properties", {}).items():
                    _add(str(prop), _annotation(sub, components), prop in required)

    # KEYWORD_ONLY params don't require required-before-optional ordering, but
    # keep required first for readable synthesised signatures.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    return params


def _make_stub(
    operation_id: str,
    method: str,
    path: str,
    params: list[inspect.Parameter],
    config: BackfillConfig,
) -> Callable[..., EndpointResponse]:
    """Synthesise a not-implemented stub whose signature drives the MCP schema.

    The returned callable accepts arbitrary kwargs (the MCP client's inputs)
    and returns an :class:`EndpointResponse` wrapping the configured
    not-implemented envelope at HTTP ``501``. Its ``__signature__`` and
    ``__annotations__`` advertise the flat parameter surface so FastMCP /
    Pydantic build a matching ``inputSchema``.

    Wrapping in an :class:`EndpointResponse` gives the REST route a ``501
    Not Implemented`` status uniformly — regardless of the operation's declared
    success status — while the MCP tool path unwraps it back to the envelope
    ``dict``. Returning the bare envelope instead would let the REST tail render
    it at the OAS success status (e.g. ``200``, contradicting the 501 marker in
    the body, or ``204``, which strips the body entirely).
    """
    envelope = config.envelope_factory(operation_id, method, path)
    response = EndpointResponse(body=envelope, status=501)

    def _stub(**_kwargs: Any) -> EndpointResponse:
        return response

    _stub.__name__ = snake_operation_id(operation_id)
    _stub.__qualname__ = _stub.__name__
    _stub.__doc__ = f"Not-implemented REST wrapper for {method} {path}."
    _stub.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    _stub.__annotations__ = {
        p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty
    }
    return _stub


def build_backfill_declarations(
    openapi_spec: dict[str, Any],
    covered: set[str],
    config: BackfillConfig,
) -> tuple[list[EndpointDecl], list[str]]:
    """Return ``(declarations, skipped)`` for every uncovered OAS operation.

    Walks ``openapi_spec["paths"]`` × HTTP methods. Each operation whose
    ``operationId`` is not in ``covered`` (the set of operationIds an explicit
    ``@endpoint`` already registered) becomes a synthesised
    :class:`EndpointDecl` wrapping a not-implemented stub. Coverage is by
    operationId — the operation's identity — so an explicit declaration
    suppresses the stub for its operation whatever tool name it chose.
    Operations lacking an ``operationId`` cannot be covered (there is nothing to
    key on) nor backfilled (no stable tool name); they are handled per
    ``config.on_missing_operation_id``.

    ``declarations`` are fresh objects — they are **not** appended to the
    module-global ``_REGISTRY``, so a backfill pass never leaks into a later
    ``register_all`` call.
    """
    all_components = openapi_spec.get("components", {})
    components = all_components.get("schemas", {})
    request_bodies = all_components.get("requestBodies", {})
    parameters = all_components.get("parameters", {})
    declarations: list[EndpointDecl] = []
    skipped: list[str] = []

    paths = openapi_spec.get("paths", {})
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for raw_method, op in item.items():
            if raw_method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            method = raw_method.upper()

            operation_id = op.get("operationId")
            if not operation_id:
                if config.on_missing_operation_id == "error":
                    raise ValueError(
                        f"backfill: operation {method} {path} has no operationId; "
                        "cannot derive a stable tool name (set "
                        "on_missing_operation_id='skip' to skip it)"
                    )
                skipped.append(f"{method} {path}")
                continue

            if str(operation_id) in covered:
                continue

            params = _params_for_operation(op, components, request_bodies, parameters)
            stub = _make_stub(str(operation_id), method, path, params, config)
            declarations.append(
                EndpointDecl(
                    fn=stub,
                    operation_id=str(operation_id),
                    tool_name=snake_operation_id(str(operation_id)),
                    title=None,
                    on_error_override=None,
                )
            )

    return declarations, skipped


__all__ = [
    "ENV_VAR",
    "BackfillConfig",
    "build_backfill_declarations",
    "normalise_backfill",
    "resolve_backfill",
    "snake_operation_id",
]
