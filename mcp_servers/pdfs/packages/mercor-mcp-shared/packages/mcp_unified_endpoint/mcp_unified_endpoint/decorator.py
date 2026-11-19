"""The ``@endpoint`` decorator — the only public surface most consumers touch.

One call replaces the three separate registrations the Foundry-Zoho
server has been writing by hand for every operation:

1. ``mcp.tool(fn, name="…", description="…")`` in ``api_wrappers.py:REGISTRY``
2. ``@mcp.custom_route("/path", methods=["GET"])`` in ``main.py``
3. A hand-built path-and-schema dict in ``openapi.py:build_openapi``

After this decorator the function itself just declares which parameters
its implementation needs; everything else (parsing, validation, MCP tool
naming, status codes, OpenAPI integration) flows from the OpenAPI
document at ``register_all`` time.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from starlette.responses import JSONResponse

from .errors import ErrorSpec
from .registry import EndpointDecl, ExpandOverride, add_declaration
from .rest import BodyErrorHook, MissingParamHook, TypeCoercionHook, validate_response_class

F = TypeVar("F", bound=Callable[..., Any])

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def endpoint(
    operation_id: str,
    *,
    tool_name: str | None = None,
    title: str | None = None,
    on_error: Mapping[type[BaseException], ErrorSpec] | None = None,
    on_type_error: TypeCoercionHook | None = None,
    on_body_error: BodyErrorHook | None = None,
    on_missing_param: MissingParamHook | None = None,
    response_class: type[JSONResponse] | None = None,
    expand: Mapping[str, Sequence[str]] | None = None,
    expand_exclude: Mapping[str, Sequence[str]] | None = None,
    expand_overrides: Mapping[str, ExpandOverride] | None = None,
) -> Callable[[F], F]:
    """Register ``fn`` as a unified MCP tool + REST endpoint.

    Args:
        operation_id: The ``operationId`` of the operation this endpoint
            implements — for example ``"getNote"``. It is resolved against the
            OpenAPI document at :func:`~mcp_unified_endpoint.register_all` time
            to find the operation's ``(method, path)`` and per-parameter spec.
            ``operationId`` is the spec's canonical, unique identifier for an
            operation, so it is drift-proof: unlike a hand-copied
            ``"<METHOD> <path>"`` string it does not silently break when the
            spec re-spells a path or moves a parameter into the template. It is
            also how backfill decides coverage — an operation with an explicit
            ``@endpoint`` is never given a not-implemented 501 stub, **whatever
            tool name this endpoint chooses**. The target operation must declare
            an ``operationId``; one that does not cannot be referenced here.
        tool_name: Override the MCP tool name. By default the function
            name is lowercased to snake_case (``get_ZohoCRM_org`` →
            ``"get_zohocrm_org"``), matching the ``^[a-z][a-z_]*$``
            convention enforced by ``validate_tool_names.py``. Pass an
            explicit value when you need a different surface (e.g. an
            operationId-matching kebab name). When ``expand`` is set this
            **must** be given and must contain the expand token as a
            ``{token}`` placeholder (e.g. ``"get_{module}"``).
        title: Optional one-line description used for the MCP tool's
            ``description`` field and as a fallback when the OpenAPI
            operation has no ``summary``.
        on_error: Per-endpoint exception overrides. Values may be:

            * ``int`` — custom status code; envelope built by the
              registered default builder (Zoho V8 shape by default).
            * ``Callable[[exc, request_kwargs], dict]`` — envelope
              builder called with the raised exception and the kwargs
              the handler parsed for this request; status defaults to
              400.
            * ``(builder, int)`` — builder plus a custom status code.

            Per-endpoint values win over the global registry registered
            via :func:`~mcp_unified_endpoint.errors.register_default_errors`.
        on_type_error: Optional hook invoked when a request value cannot be
            parsed to its declared JSON-Schema type on the REST route path
            (e.g. ``maxResults=abc`` for an integer param). This is a
            *structural* failure — the framework has no value of the right type
            to hand the implementation — so by default (no hook) it returns the
            shared ``INVALID_DATA`` 400. A hook receives a
            :class:`~mcp_unified_endpoint.rest.TypeCoercionError` and may either
            **return a substitute value** (used in place of the parsed value —
            e.g. the raw string, ``None``, or a clamped fallback) or **raise a
            domain exception**, which is routed through this endpoint's
            ``on_error`` mapping so the app emits its own envelope (a
            Gmail-parity ``400`` / ``403`` instead of the generic framework
            400). Value constraints (``enum`` / bounds) are *not* enforced by
            the framework at all — only type-parse failures reach this hook.
        on_body_error: Optional hook invoked when the ``application/json``
            request body cannot be turned into a usable value on the REST route
            path — it is **malformed JSON** or **absent while the operation's
            ``requestBody`` is required**. By default (no hook) either case is
            the shared ``INVALID_DATA`` 400. A hook receives a
            :class:`~mcp_unified_endpoint.rest.BodyParseError` (carrying the raw
            body bytes and the parse error) and may either **return a substitute
            value** used as the parsed body (e.g. ``{}`` for an empty body) or
            **raise a domain exception**, routed through this endpoint's
            ``on_error`` mapping so the app emits its own envelope. A *valid
            non-dict* body (top-level array / string / number) parses
            successfully and so never reaches this hook — coercing it is the
            implementation's concern (e.g. ``body if isinstance(body, dict)
            else {}``).
        on_missing_param: Optional hook invoked when a *required* path / query /
            header / cookie parameter is **absent** from the request on the REST
            route path, *before* the framework's shared 400. By default (no hook)
            an absent required parameter is the shared framework 400. A hook
            receives a :class:`~mcp_unified_endpoint.rest.MissingParamError`
            (carrying the parameter's :class:`ParamSpec` — name, location, schema
            — and the :class:`~starlette.requests.Request`) and may either
            **return a substitute value** (used as if it had been parsed from the
            request) or **raise a domain exception**, routed through this
            endpoint's ``on_error`` mapping so the app emits its own envelope
            (e.g. a ``REQUIRED_PARAM_MISSING`` distinct from the ``INVALID_DATA``
            a present-but-empty value would yield). Only a genuinely *absent*
            parameter reaches this hook — a present-but-empty value (``?x=``)
            parses and flows on to the handler or ``on_type_error``, so
            ``required: true`` stays honest without conflating the two cases.
        response_class: Override the JSON response class used for this
            endpoint's body-carrying REST responses (success payload, mapped
            error envelopes, and the framework 400). ``None`` (default) inherits
            the process-wide default passed to
            :func:`~mcp_unified_endpoint.register_all`, falling back to the plain
            :class:`~starlette.responses.JSONResponse`. Pass a subclass — e.g.
            one whose ``render`` strips ``None``-valued keys for exact Discovery
            wire parity — when this endpoint needs a different wire shape than the
            registrar default. A value here wins over the registrar default for
            this endpoint only. The no-content 204 / 304 / 1xx branch and the MCP
            tool path are unaffected. Must be a ``JSONResponse`` subclass or a
            ``TypeError`` is raised at decoration time.
        expand: Parameterised tool-name fan-out. Maps **one** token name to
            the values it takes, e.g. ``{"module": ["Leads", "Accounts"]}``.
            At :func:`~mcp_unified_endpoint.register_all` the declaration
            fans out into one MCP tool per value: the ``{token}`` in
            ``tool_name`` is substituted (``get_Leads``, ``get_Accounts``),
            and the value is pinned into ``token`` — it is dropped from the
            MCP ``inputSchema`` and injected when the tool is invoked. The
            REST route stays single (the one templated ``/{module}`` route).
            The token values are app-supplied (the OpenAPI ``module`` param
            is a free-form string with no ``enum``), so they come from here,
            never inferred from the spec. Omit ``expand`` for the ordinary
            1:1 endpoint behaviour.

            Only a single token is supported (a cartesian product over
            multiple tokens is a deliberate non-goal for now); passing more
            than one key raises ``ValueError``.
        expand_exclude: Token values to skip when fanning out, keyed by the
            same token, e.g. ``{"module": ["Events"]}`` for a module that
            rejects this operation. Lets one canonical value list be shared
            across endpoints and subtracted per-endpoint. Only valid with
            ``expand``.
        expand_overrides: Per-value overrides keyed by the concrete token
            value, e.g. ``{"Leads": ExpandOverride(description="…")}``. Only
            valid with ``expand``.

    Returns:
        The same function, unwrapped. The decorator is a pure
        registration sink — invoking the function directly behaves
        exactly as before.
    """
    if not operation_id or not operation_id.strip():
        raise ValueError("operation_id must be a non-empty operationId string")
    validate_response_class(response_class, where="@endpoint")
    norm_expand, norm_exclude, norm_overrides = _normalise_expand(
        tool_name, expand, expand_exclude, expand_overrides
    )

    def _wrap(fn: F) -> F:
        add_declaration(
            EndpointDecl(
                fn=fn,
                operation_id=operation_id,
                tool_name=tool_name or _snake(fn.__name__),
                title=title,
                on_error_override=on_error,
                on_type_error=on_type_error,
                on_body_error=on_body_error,
                on_missing_param=on_missing_param,
                response_class=response_class,
                expand=norm_expand,
                expand_exclude=norm_exclude,
                expand_overrides=norm_overrides,
            )
        )
        return fn

    return _wrap


def _normalise_expand(
    tool_name: str | None,
    expand: Mapping[str, Sequence[str]] | None,
    expand_exclude: Mapping[str, Sequence[str]] | None,
    expand_overrides: Mapping[str, ExpandOverride] | None,
) -> tuple[
    Mapping[str, tuple[str, ...]] | None,
    Mapping[str, frozenset[str]] | None,
    Mapping[str, ExpandOverride] | None,
]:
    """Validate + normalise the ``expand*`` args into immutable forms.

    Runs at decoration time so misconfiguration surfaces immediately, with
    everything checkable without the OpenAPI spec (the token-is-a-real-param
    check needs ``fn`` and happens later, in ``register_all``). Returns
    ``(None, None, None)`` for a plain declaration.
    """
    if expand is None:
        if expand_exclude is not None or expand_overrides is not None:
            raise ValueError("expand_exclude / expand_overrides require expand to be set")
        return None, None, None

    if len(expand) != 1:
        raise ValueError(
            "expand supports exactly one token; got "
            f"{sorted(expand)!r}. A cartesian product over multiple tokens "
            "is not supported."
        )
    ((token, raw_values),) = expand.items()

    if isinstance(raw_values, (str, bytes)):
        raise ValueError(
            f"expand values for token {token!r} must be a list/tuple of strings, "
            f"not a bare string (got {raw_values!r}); wrap it in a list"
        )

    if tool_name is None:
        raise ValueError(f"expand requires an explicit tool_name containing '{{{token}}}'")
    if "{" + token + "}" not in tool_name:
        raise ValueError(
            f"expand token {token!r} must appear as '{{{token}}}' in tool_name (got {tool_name!r})"
        )
    stray = {t for t in _PLACEHOLDER_RE.findall(tool_name) if t != token}
    if stray:
        raise ValueError(
            f"tool_name {tool_name!r} has placeholder(s) {sorted(stray)!r} "
            f"with no matching expand token"
        )

    values = _dedupe_values(token, raw_values)

    norm_exclude: Mapping[str, frozenset[str]] | None = None
    if expand_exclude is not None:
        bad_keys = set(expand_exclude) - {token}
        if bad_keys:
            raise ValueError(
                f"expand_exclude keys {sorted(bad_keys)!r} do not match the expand token {token!r}"
            )
        raw_exclude = expand_exclude.get(token)
        if isinstance(raw_exclude, (str, bytes)):
            raise ValueError(
                f"expand_exclude values for token {token!r} must be a list/tuple of strings, "
                f"not a bare string (got {raw_exclude!r}); wrap it in a list"
            )
        norm_exclude = {token: frozenset(expand_exclude.get(token, ()))}

    norm_overrides: Mapping[str, ExpandOverride] | None = None
    if expand_overrides is not None:
        unknown = set(expand_overrides) - set(values)
        if unknown:
            raise ValueError(
                f"expand_overrides target unknown value(s) {sorted(unknown)!r}; "
                f"expected a subset of {list(values)!r}"
            )
        norm_overrides = dict(expand_overrides)

    return {token: values}, norm_exclude, norm_overrides


def _dedupe_values(token: str, raw_values: Sequence[str]) -> tuple[str, ...]:
    """Return the token's values as a deduped, order-preserving tuple.

    Duplicate values would fan out to colliding tool names, so they are
    collapsed. An empty value list is a configuration error.
    """
    seen: dict[str, None] = {}
    for value in raw_values:
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"expand values for token {token!r} must be non-empty strings (got {value!r})"
            )
        seen.setdefault(value, None)
    if not seen:
        raise ValueError(f"expand token {token!r} has no values")
    return tuple(seen)


def _snake(name: str) -> str:
    """``get_ZohoCRM_notes_list_for_record`` → ``"get_zohocrm_notes_list_for_record"``.

    Lowercases the identifier and keeps its underscores, so the default tool
    name is snake_case — matching the ``^[a-z][a-z_]*$`` convention enforced
    by ``mcp_scripts/validate_tool_names.py``. Python function names are the
    source, so the only transform needed is the lowercase fold (case-boundary
    words like ``ZohoCRM`` collapse to ``zohocrm``). Callers that need a
    different surface (e.g. an operationId-matching kebab name) pass an
    explicit ``tool_name=``.
    """
    return name.lower()


__all__ = ["endpoint"]
