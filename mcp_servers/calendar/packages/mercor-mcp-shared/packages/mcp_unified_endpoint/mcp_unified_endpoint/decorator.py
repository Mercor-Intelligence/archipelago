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
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from .errors import ErrorSpec
from .registry import EndpointDecl, add_declaration

F = TypeVar("F", bound=Callable[..., Any])

_ROUTE_RE = re.compile(r"^\s*([A-Z]+)\s+(/\S*)\s*$")


def endpoint(
    route: str,
    *,
    tool_name: str | None = None,
    title: str | None = None,
    on_error: Mapping[type[BaseException], ErrorSpec] | None = None,
) -> Callable[[F], F]:
    """Register ``fn`` as a unified MCP tool + REST endpoint.

    Args:
        route: ``"<METHOD> <path>"`` — for example ``"GET /crm/v9/Notes/{note_id}"``.
            The path **must** match a path in the OpenAPI document
            verbatim; that's how the decorator finds the parameter spec.
        tool_name: Override the MCP tool name. By default the function
            name is converted to kebab-case (``get_ZohoCRM_org`` →
            ``"get-ZohoCRM-org"``), which matches the convention the
            ``api_wrappers.py:REGISTRY`` has been using.
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

    Returns:
        The same function, unwrapped. The decorator is a pure
        registration sink — invoking the function directly behaves
        exactly as before.
    """
    method, path = _split_route(route)

    def _wrap(fn: F) -> F:
        add_declaration(
            EndpointDecl(
                fn=fn,
                method=method,
                path=path,
                tool_name=tool_name or _kebab(fn.__name__),
                title=title,
                on_error_override=on_error,
            )
        )
        return fn

    return _wrap


def _split_route(route: str) -> tuple[str, str]:
    """``"GET /crm/v9/Notes/{note_id}"`` → ``("GET", "/crm/v9/Notes/{note_id}")``."""
    m = _ROUTE_RE.match(route)
    if m is None:
        raise ValueError(f"route must be ``METHOD /path`` (got {route!r})")
    return m.group(1), m.group(2)


def _kebab(name: str) -> str:
    """``get_ZohoCRM_notes_list_for_record`` → ``"get-ZohoCRM-notes-list-for-record"``.

    Underscores become hyphens; case is preserved (Zoho's REGISTRY uses
    mixed-case kebab e.g. ``ZohoCRM``). The output matches the existing
    REGISTRY entries one-for-one so the MCP tool surface is unchanged
    when callers migrate.
    """
    return name.replace("_", "-")


__all__ = ["endpoint"]
