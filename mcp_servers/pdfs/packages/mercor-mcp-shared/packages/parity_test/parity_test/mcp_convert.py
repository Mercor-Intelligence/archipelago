"""FastMCP → OAS converter (spec §MCP-parity, task P1).

An MCP ``OasIndex`` needs a *document* to parse. Rather than make every app
hand-write an OpenAPI doc that mirrors its MCP server, this module derives one
from the server's own tool list:

    tools_to_openapi(from_fastmcp(app))

Two composable functions, split so the ``fastmcp`` import stays app-side:

- :func:`from_fastmcp` reads a live ``FastMCP`` instance's registered tools and
  normalizes each into a plain ``{"name", "description", "inputSchema", ...}``
  dict. This is the only place that touches ``fastmcp``; it is called by the
  app's ``mcp_document`` hook, never by the framework.
- :func:`tools_to_openapi` is a **pure** function over those dicts → a standard
  OpenAPI 3.1 document the :class:`~parity_test.oas.OasIndex` parses like any
  other. No ``fastmcp`` dependency, fully testable in isolation.

**Forward-only, by design.** The converter maps each wire tool to *one*
operation. By default that is an **identity** operation — ``operationId`` == the
wire tool name, no templating — which is exactly what a live server exposes: if a
server fans ``getRecords`` into ``get_leads`` / ``get_contacts``, those *are*
separate tools and become separate operations. The converter never tries to
*collapse* ``get_leads`` back into ``getRecords`` + ``get_{module}`` (the
ambiguous reverse we deliberately don't model — see ``oas.py``). An app that
wants the aligned/templated view supplies the mapping explicitly: a tool may
carry ``operationId`` and ``x-mcp-tool-name`` (e.g. stashed in FastMCP
``meta["parity"]``), which is *carried through* verbatim — never inferred.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from .oas import _template_params


def tools_to_openapi(
    tools: list[Mapping[str, Any]],
    *,
    title: str = "MCP tools",
    version: str = "0.0.0",
) -> dict[str, Any]:
    """Build a standard OpenAPI 3.1 document from a list of MCP tool descriptors.

    Each descriptor is a mapping with:

    - ``name`` (**required**) — the wire tool name.
    - ``description`` — free text (optional).
    - ``inputSchema`` — JSON Schema for the tool ``arguments`` (optional). Becomes
      the operation's ``requestBody`` schema.
    - ``outputSchema`` — JSON Schema for a successful result (optional). Becomes
      the ``200`` response schema so shape extraction has something to chew on.
    - ``responses`` — a full OpenAPI ``responses`` object (optional). When present
      it is used verbatim (an authored override winning over ``outputSchema``).
    - ``operationId`` — the operation key (optional; defaults to ``name``).
    - ``x-mcp-tool-name`` — a wire-name *template* (optional). Its ``{placeholder}``
      names become path params of the synthesized operation, so the index's
      tool-name mapping validates and renders exactly as for a hand-authored doc.

    The wire name and ``operationId`` must each be unique across the list (a tool
    mirrors at most one operation); a duplicate is a hard error, not a silent
    overwrite.
    """
    paths: dict[str, Any] = {}
    seen_ids: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    for tool in tools:
        name = tool.get("name")
        if not name:
            raise ValueError(f"MCP tool descriptor missing 'name': {dict(tool)!r}")
        name = str(name)
        op_id = str(tool.get("operationId") or name)
        if name in seen_names:
            raise ValueError(f"duplicate MCP tool name {name!r}")
        seen_names[name] = op_id
        if op_id in seen_ids:
            raise ValueError(
                f"duplicate operationId {op_id!r} (tools {seen_ids[op_id]!r} and {name!r})"
            )
        seen_ids[op_id] = name

        template = str(tool.get("x-mcp-tool-name") or "")
        path_params = _template_params(template)

        operation: dict[str, Any] = {"operationId": op_id}
        description = tool.get("description")
        if description:
            operation["description"] = str(description)
        input_schema = tool.get("inputSchema")
        if input_schema:
            operation["requestBody"] = {"content": {"application/json": {"schema": input_schema}}}
        if template:
            operation["x-mcp-tool-name"] = template
        if path_params:
            operation["parameters"] = [
                {"name": p, "in": "path", "required": True} for p in path_params
            ]
        operation["responses"] = _responses_for(tool)

        paths[_synth_path(op_id, path_params)] = {"post": operation}

    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }


def _responses_for(tool: Mapping[str, Any]) -> dict[str, Any]:
    """The ``responses`` object for a tool — an authored map wins, else the
    ``outputSchema`` as the ``200`` body, else a bare documented ``200`` (so the
    operation always contributes at least one ``(200, "")`` response shape)."""
    authored = tool.get("responses")
    if isinstance(authored, Mapping) and authored:
        return dict(authored)
    output_schema = tool.get("outputSchema")
    if output_schema:
        return {
            "200": {
                "description": "tool result",
                "content": {"application/json": {"schema": output_schema}},
            }
        }
    return {"200": {"description": "tool result"}}


def _synth_path(operation_id: str, path_params: list[str]) -> str:
    """A unique, parseable path key for an operation. MCP has no REST path, so we
    synthesize one from the operationId; any tool-name template placeholders are
    appended as ``{param}`` segments so :class:`OasIndex` recovers them as path
    params (the tool-name discriminants)."""
    tail = "".join(f"/{{{p}}}" for p in path_params)
    return f"/{operation_id}{tail}"


def from_fastmcp(app: Any) -> list[dict[str, Any]]:
    """Read a live ``FastMCP`` instance's tools into :func:`tools_to_openapi`
    descriptors. The only ``fastmcp``-aware entry point (kept app-side).

    Accepts any object exposing ``list_tools()`` (FastMCP ≥ 2/3) returning tool
    objects (or a name→tool mapping); an awaitable result is resolved. Each tool's
    ``name`` / ``description`` / ``parameters`` (inputSchema) / ``output_schema``
    are read, and an optional ``meta["parity"]`` block (``operationId`` /
    ``x-mcp-tool-name``) is carried through so an app can opt into the aligned,
    templated view without the converter guessing it."""
    return [_normalize_tool(t) for t in _list_tools(app)]


def _list_tools(app: Any) -> list[Any]:
    lister = getattr(app, "list_tools", None) or getattr(app, "_list_tools", None)
    if lister is None:
        raise TypeError(
            "from_fastmcp expected a FastMCP-like object exposing list_tools(); "
            f"got {type(app).__name__}"
        )
    result = lister()
    if inspect.isawaitable(result):
        result = _run_sync(result)
    if isinstance(result, Mapping):
        return list(result.values())
    return list(result)


def _run_sync(awaitable: Any) -> Any:
    """Resolve an awaitable off any running loop (FastMCP list_tools is sync in
    3.x, but older/async variants return a coroutine)."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


def _get(tool: Any, attr: str, default: Any = None) -> Any:
    """Attribute-or-key access, so a real FastMCP ``Tool`` object and a plain dict
    descriptor both work (the latter keeps tests free of a fastmcp dependency)."""
    if isinstance(tool, Mapping):
        return tool.get(attr, default)
    return getattr(tool, attr, default)


def _normalize_tool(tool: Any) -> dict[str, Any]:
    name = _get(tool, "name")
    if not name:
        raise ValueError(f"FastMCP tool has no name: {tool!r}")
    out: dict[str, Any] = {"name": str(name)}
    description = _get(tool, "description")
    if description:
        out["description"] = str(description)
    input_schema = _get(tool, "parameters") or _get(tool, "inputSchema")
    if input_schema:
        out["inputSchema"] = input_schema
    output_schema = _get(tool, "output_schema") or _get(tool, "outputSchema")
    if output_schema:
        out["outputSchema"] = output_schema

    meta = _get(tool, "meta") or {}
    parity = meta.get("parity") if isinstance(meta, Mapping) else None
    if isinstance(parity, Mapping):
        if parity.get("operationId"):
            out["operationId"] = str(parity["operationId"])
        if parity.get("x-mcp-tool-name"):
            out["x-mcp-tool-name"] = str(parity["x-mcp-tool-name"])
    return out
