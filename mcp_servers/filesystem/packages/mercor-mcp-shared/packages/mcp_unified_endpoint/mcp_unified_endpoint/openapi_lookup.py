"""Pull per-parameter info out of an existing OpenAPI 3.x document.

The unified-endpoint decorator does not introduce a new spec format. It
relies on the OpenAPI document the server already publishes (the source of
truth for every other consumer — Swagger UI, the REST bridge, generated
clients). At ``register_all`` time we walk the decorated endpoints and
ask this module: "for ``GET /crm/v9/Notes/{note_id}``, what are the
parameters, where do they live (path / query / header / cookie), what
type, what defaults, what validation?"

The returned :class:`OperationSpec` is everything the REST handler
synthesiser needs to parse one inbound request without the call site
re-declaring it.
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass, field
from typing import Any, Literal

ParamIn = Literal["path", "query", "header", "cookie", "body"]


def is_wire_safe_param_name(name: str) -> bool:
    """True when ``name`` can be a Python keyword argument / ``inspect.Parameter``.

    OpenAPI parameter names are far more permissive than Python identifiers:
    Google specs alone carry ``$.xgafv``, ``oauth-token``, hyphenated header
    names, and reserved words like ``from``. ``inspect.Parameter`` rejects every
    one of those, so a caller projecting a parameter onto a synthesised wire
    signature (the ``**kwargs`` pass-through surface, the backfill stub) must
    skip names that fail this check — otherwise building the signature raises
    ``ValueError`` and aborts registration.

    A skipped name is *not* lost on the REST path: request values are forwarded
    by string key through a plain dict (``fn(**{"from": ...})`` is legal), so
    the handler still receives it. It simply cannot be advertised as a named
    field in the MCP ``inputSchema``, which Python/Pydantic could not represent
    anyway.

    Note ``str.isidentifier()`` returns ``True`` for reserved words (``from``
    *is* a lexically valid identifier), so the explicit ``iskeyword`` guard is
    load-bearing, not redundant.
    """
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


def _deref_parameter(
    raw: Any, components_params: dict[str, Any], _seen: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """Resolve a parameter that may be a ``$ref`` into ``components/parameters``.

    OpenAPI lets operations reference shared parameter definitions
    (``{"$ref": "#/components/parameters/PageToken"}``) — Google/Foundry specs
    lean on this heavily. An unresolved ref has no ``name`` / ``in``, so reading
    them directly raises ``KeyError`` and aborts the whole lookup. Resolve the
    ref (cycle-safe) and return the concrete parameter object; return ``None``
    for anything that still isn't a usable parameter so the caller can skip it.
    """
    if not isinstance(raw, dict):
        return None
    ref = raw.get("$ref")
    if isinstance(ref, str):
        key = ref.rsplit("/", 1)[-1]
        if key in _seen:
            return None
        return _deref_parameter(components_params.get(key, {}), components_params, _seen | {key})
    return raw if "name" in raw and "in" in raw else None


@dataclass(frozen=True)
class ParamSpec:
    """One parameter in an OpenAPI operation."""

    name: str
    location: ParamIn
    required: bool
    schema: dict[str, Any]  # raw JSON Schema dict from the spec
    description: str | None = None
    default: Any = None  # convenience: pulled out of ``schema.default``
    explode: bool = True  # OpenAPI form/query default; False = one comma-joined value


@dataclass(frozen=True)
class OperationSpec:
    """One ``(method, path)`` entry in the OpenAPI document."""

    method: str  # uppercase: "GET", "POST", …
    path: str  # OpenAPI path template, e.g. "/crm/v9/Notes/{note_id}"
    operation_id: str | None
    summary: str | None
    parameters: list[ParamSpec] = field(default_factory=list)
    body: ParamSpec | None = None  # synthesized from requestBody when present
    success_status: int = 200  # primary 2xx response code from the spec


class OpenAPILookupError(LookupError):
    """Raised when an ``@endpoint`` references an operation not in the spec."""


#: The HTTP methods an OpenAPI 3.x path-item may map to an operation object.
#: Every other key on a path item (``parameters``, ``summary``, ``$ref``, …)
#: is not an operation and is skipped when scanning for an ``operationId``.
_OPERATION_METHODS: tuple[str, ...] = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)


def lookup_by_operation_id(spec: dict[str, Any], operation_id: str) -> OperationSpec:
    """Find the operation whose ``operationId`` is ``operation_id``.

    ``operationId`` is the canonical, spec-unique identifier for an operation
    (OpenAPI requires it be unique across the whole document), so it is a
    drift-proof key for an ``@endpoint`` declaration — unlike a hand-copied
    ``"<METHOD> <path>"`` string, it does not silently break when the spec
    re-spells a path or moves a parameter into the template. The scan resolves
    ``operation_id`` to its ``(method, path)`` and delegates to :func:`lookup`
    for the per-parameter spec.

    Args:
        spec: The full OpenAPI 3.x document.
        operation_id: The exact (case-sensitive) ``operationId`` to resolve.

    Raises:
        OpenAPILookupError: When no operation in ``spec["paths"]`` declares
            ``operation_id``, or — a malformed spec — more than one does.
    """
    paths = spec.get("paths", {})
    matches: list[tuple[str, str]] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _OPERATION_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict) and operation.get("operationId") == operation_id:
                matches.append((method, path))
    if not matches:
        raise OpenAPILookupError(f"operationId not in OpenAPI spec: {operation_id!r}")
    if len(matches) > 1:
        locations = ", ".join(f"{m.upper()} {p}" for m, p in matches)
        raise OpenAPILookupError(
            f"operationId {operation_id!r} is ambiguous: declared on {locations}. "
            f"OpenAPI requires operationId to be unique across the document."
        )
    method, path = matches[0]
    return lookup(spec, method, path)


def lookup(spec: dict[str, Any], method: str, path: str) -> OperationSpec:
    """Find ``(method, path)`` in ``spec`` and return its :class:`OperationSpec`.

    Args:
        spec: The full OpenAPI 3.x document.
        method: HTTP method (case-insensitive).
        path: Path template exactly as written in the spec
            (e.g. ``"/crm/v9/Notes/{note_id}"``).

    Raises:
        OpenAPILookupError: When ``path`` is not in ``spec["paths"]`` or
            ``method`` is not declared on that path.
    """
    paths = spec.get("paths", {})
    path_item = paths.get(path)
    if path_item is None:
        raise OpenAPILookupError(f"path not in OpenAPI spec: {path!r}")
    operation = path_item.get(method.lower())
    if operation is None:
        raise OpenAPILookupError(f"method {method!r} not declared on {path!r}")

    # OpenAPI 3.x lets parameters be declared at the path-item level
    # (shared across every method on that path) AND at the operation level
    # (per-method only). Method-level entries shadow path-level entries with
    # the same ``(name, in)``. The Foundry-Zoho openapi.py prefers the
    # path-level form for path/cookie params that don't vary per method.
    # A parameter (at either level) may be a ``$ref`` into
    # ``components/parameters`` — resolve it before reading ``name`` / ``in``,
    # skipping anything that still isn't a concrete parameter.
    components_params = spec.get("components", {}).get("parameters", {})
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in path_item.get("parameters", []):
        resolved = _deref_parameter(raw, components_params)
        if resolved is not None:
            by_key[(resolved["name"], resolved["in"])] = resolved
    for raw in operation.get("parameters", []):
        resolved = _deref_parameter(raw, components_params)
        if resolved is not None:
            by_key[(resolved["name"], resolved["in"])] = resolved

    parameters: list[ParamSpec] = []
    for raw in by_key.values():
        schema_obj = raw.get("schema", {})
        parameters.append(
            ParamSpec(
                name=raw["name"],
                location=raw["in"],
                required=bool(raw.get("required", raw["in"] == "path")),
                schema=schema_obj,
                description=raw.get("description"),
                default=schema_obj.get("default"),
                explode=bool(raw.get("explode", True)),
            )
        )

    body: ParamSpec | None = None
    request_body = operation.get("requestBody")
    if request_body is not None:
        content = request_body.get("content", {})
        json_part = content.get("application/json") or next(iter(content.values()), {})
        body_schema = json_part.get("schema", {})
        body = ParamSpec(
            name="body",
            location="body",
            required=bool(request_body.get("required", True)),
            schema=body_schema,
            description=request_body.get("description"),
            default=None,
        )

    success_status = _first_success_status(operation.get("responses", {}))

    return OperationSpec(
        method=method.upper(),
        path=path,
        operation_id=operation.get("operationId"),
        summary=operation.get("summary"),
        parameters=parameters,
        body=body,
        success_status=success_status,
    )


def _first_success_status(responses: dict[str, Any]) -> int:
    """Pick the primary success status code (2xx) from a responses dict.

    OpenAPI keys responses as strings. We prefer the smallest-numbered
    2xx; if none is declared, fall back to 200.
    """
    success_codes: list[int] = []
    for key in responses:
        try:
            n = int(key)
        except ValueError:
            continue
        if 200 <= n < 300:
            success_codes.append(n)
    if not success_codes:
        return 200
    return min(success_codes)


def coerce(value: str, schema: dict[str, Any]) -> Any:
    """Marshal a raw string to the schema's declared JSON-Schema type.

    **Structural type-parse only** — the minimum needed to build the handler
    call: ``int`` / ``float`` / ``bool`` / ``str``, so the synthesised handler
    can call ``fn(maxResults=5)``.

    The framework deliberately does **not** enforce OAS *value* constraints
    (``enum`` / ``minimum`` / ``maximum``). Those remain in the OpenAPI document
    as documentation, and the application handler owns value validation plus its
    own error envelope — e.g. a Gmail-parity ``403 PERMISSION_DENIED`` for a
    disallowed ``userId``, rather than a generic framework ``INVALID_DATA`` 400
    that would pre-empt it. Bare-minimum-to-route is the contract: the router
    marshals types and delegates every domain decision to the app.

    A value that cannot be parsed to the declared type (``"abc"`` for an
    ``integer``) still raises :class:`ValueError` — this is a *structural*
    failure, not a value-range one: there is no value of the right type to pass
    to the handler at all, so the framework returns a 400 at that boundary.
    """
    declared = schema.get("type")
    if declared in (None, "string"):
        return value
    if declared == "integer":
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"expected integer, got {value!r}") from exc
    if declared == "number":
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"expected number, got {value!r}") from exc
    if declared == "boolean":
        low = value.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"expected boolean, got {value!r}")
    return value


def coerce_array(values: list[str], schema: dict[str, Any]) -> list[Any]:
    """Marshal a list of raw query strings to an ``array`` param's element type.

    Each element is type-parsed against ``items`` via :func:`coerce` (so a
    ``{"items": {"type": "integer"}}`` array yields a ``list[int]``). As with
    scalars, element-level *value* constraints (``enum`` / ``minimum`` /
    ``maximum``) are **not** enforced — the application handler owns value
    validation. A missing or non-dict ``items`` marshals each element as a plain
    string (the JSON-Schema default).
    """
    items = schema.get("items")
    item_schema = items if isinstance(items, dict) else {}
    return [coerce(v, item_schema) for v in values]


__all__ = [
    "OpenAPILookupError",
    "OperationSpec",
    "ParamIn",
    "ParamSpec",
    "coerce",
    "coerce_array",
    "lookup",
    "lookup_by_operation_id",
]
