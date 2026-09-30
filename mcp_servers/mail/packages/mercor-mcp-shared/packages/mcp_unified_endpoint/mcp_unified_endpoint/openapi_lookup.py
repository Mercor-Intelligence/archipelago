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

from dataclasses import dataclass, field
from typing import Any, Literal

ParamIn = Literal["path", "query", "header", "cookie", "body"]


@dataclass(frozen=True)
class ParamSpec:
    """One parameter in an OpenAPI operation."""

    name: str
    location: ParamIn
    required: bool
    schema: dict[str, Any]  # raw JSON Schema dict from the spec
    description: str | None = None
    default: Any = None  # convenience: pulled out of ``schema.default``


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
    """Raised when an ``@endpoint`` references a (method, path) not in the spec."""


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
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in path_item.get("parameters", []):
        by_key[(raw["name"], raw["in"])] = raw
    for raw in operation.get("parameters", []):
        by_key[(raw["name"], raw["in"])] = raw

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
    """Coerce a raw string (from query / path params) to the schema's type.

    Handles the JSON-Schema primitive types and ``enum``. Out-of-range
    integers / unknown enum values raise :class:`ValueError` so the REST
    handler can convert them to 400s consistently.
    """
    declared = schema.get("type")
    if declared in (None, "string"):
        out = value
    elif declared == "integer":
        try:
            out = int(value)
        except ValueError as exc:
            raise ValueError(f"expected integer, got {value!r}") from exc
        _check_numeric_bounds(out, schema)
    elif declared == "number":
        try:
            out = float(value)
        except ValueError as exc:
            raise ValueError(f"expected number, got {value!r}") from exc
        _check_numeric_bounds(out, schema)
    elif declared == "boolean":
        if value.lower() in ("true", "1", "yes", "on"):
            out = True
        elif value.lower() in ("false", "0", "no", "off"):
            out = False
        else:
            raise ValueError(f"expected boolean, got {value!r}")
    else:
        out = value

    enum = schema.get("enum")
    if enum is not None and out not in enum:
        raise ValueError(f"value {out!r} not in {enum!r}")
    return out


def _check_numeric_bounds(value: int | float, schema: dict[str, Any]) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"value {value} below minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise ValueError(f"value {value} above maximum {schema['maximum']}")


__all__ = [
    "OpenAPILookupError",
    "OperationSpec",
    "ParamIn",
    "ParamSpec",
    "coerce",
    "lookup",
]
