"""Middleware that flattens served tool INPUT schemas for LLM compatibility.

Why this exists
---------------
``GeminiBaseModel`` only *annotates* optional fields; the actual collapsing of
``anyOf`` / ``$defs`` / ``$ref`` into a Gemini-compatible shape is done by
``flatten_schema``. That flatten must be applied to the schema that
``tools/list`` actually serves. This middleware additionally collapses
``oneOf`` / ``allOf`` (which ``flatten_schema`` leaves): the Gemini API
tolerates them, but google-genai's typed ``types.Schema`` (the path some
customer harnesses use) rejects them as unknown fields.

A one-shot startup pass over ``await mcp.list_tools()`` does **not** achieve
this on fastmcp 3.x: ``list_tools()`` returns freshly-built copies on every
call, so mutating them is discarded and the served schema keeps its ``anyOf``.
This middleware instead flattens on the two paths that reach clients:

* ``on_list_tools`` — the runtime MCP ``tools/list`` response.
* ``patch_tool_schemas`` — the tool registry itself, so the direct
  ``list_tools()`` used by UI generators / tool-extraction sync is also flat.

This mirrors ``ResponseLimiterMiddleware``'s two-pronged approach.

Only INPUT (``parameters``) schemas are flattened. Output schemas are left
untouched: flattening them breaks MCP client-side structured-output validation.

``additionalProperties`` handling
----------------------------------
``flatten_schema`` drops free-form-map ``additionalProperties`` (a dict or
``true``) but deliberately KEEPS a bare ``additionalProperties: false`` (from
``extra="forbid"``), because Vertex accepts it. The **Gemini Developer API
(v1beta) rejects ``additionalProperties`` in every form, including ``false``**
(``400 "Unknown name additionalProperties"``). Since these servers ship to
customers who may hit either surface without our litellm layer (which strips it
for us), the served schema drops ``additionalProperties`` entirely here. This is
safe: the ``false`` is only an advisory hint to the model — server-side pydantic
still enforces ``extra="forbid"`` on the actual tool input.
"""

from collections.abc import Sequence
from copy import deepcopy
from typing import Any, override

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool
from loguru import logger
from mcp_schema import flatten_schema


def _strip_additional_properties(node: Any) -> None:
    """Recursively remove every ``additionalProperties`` key in place.

    Covers the bare ``false`` that ``flatten_schema`` keeps (Developer-API-unsafe).
    """
    if isinstance(node, dict):
        node.pop("additionalProperties", None)
        for value in node.values():
            _strip_additional_properties(value)
    elif isinstance(node, list):
        for value in node:
            _strip_additional_properties(value)


def _merge_into(target: dict, source: dict) -> None:
    """Shallow-merge ``source`` schema keys into ``target`` (target wins on conflict).

    ``properties`` dicts are merged key-wise; ``required`` lists are unioned;
    the union keywords themselves are never copied.
    """
    for key, value in source.items():
        if key in ("allOf", "oneOf", "anyOf"):
            continue
        if key == "properties" and isinstance(value, dict):
            props = target.setdefault("properties", {})
            for pk, pv in value.items():
                props.setdefault(pk, pv)
        elif key == "required" and isinstance(value, list):
            req = target.setdefault("required", [])
            for item in value:
                if item not in req:
                    req.append(item)
        else:
            target.setdefault(key, value)


def _collapse_unions(node: Any) -> None:
    """Recursively remove ``oneOf``/``allOf`` in place (``anyOf`` is handled by
    ``flatten_schema``). Gemini's API tolerates these, but google-genai's typed
    ``types.Schema`` (the OBI/AGY harness path) rejects them as unknown fields.

    - ``allOf``: merge every member into the node (the common single-member
      ``$ref``-wrapper collapses to the inlined object).
    - ``oneOf``: keep non-``null`` variants and merge the first into the node,
      mirroring ``flatten_schema``'s ``anyOf`` handling (lossy but safe).
    """
    if isinstance(node, dict):
        all_of = node.pop("allOf", None)
        if isinstance(all_of, list):
            for member in all_of:
                if isinstance(member, dict):
                    _collapse_unions(member)
                    _merge_into(node, member)
        one_of = node.pop("oneOf", None)
        if isinstance(one_of, list):
            dict_members = [m for m in one_of if isinstance(m, dict)]
            variants = [m for m in dict_members if m.get("type") != "null"]
            chosen = variants[0] if variants else (dict_members[0] if dict_members else None)
            if isinstance(chosen, dict):
                _collapse_unions(chosen)
                _merge_into(node, chosen)
        for value in node.values():
            _collapse_unions(value)
    elif isinstance(node, list):
        for value in node:
            _collapse_unions(value)


def _flatten_for_serving(params: dict) -> dict:
    """Flatten a tool input schema: collapse anyOf/$ref (via ``flatten_schema``)
    and oneOf/allOf, then drop all ``additionalProperties``."""
    flat = flatten_schema(deepcopy(params))
    _collapse_unions(flat)
    _strip_additional_properties(flat)
    return flat


class SchemaFlattenMiddleware(Middleware):
    """Flattens each served tool's parameter schema for Gemini/Vertex compatibility."""

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Flatten input schemas on the runtime ``tools/list`` response."""
        tools = await call_next(context)
        result: list[Tool] = []
        for tool in tools:
            params = getattr(tool, "parameters", None)
            if isinstance(params, dict) and params:
                tool = tool.model_copy(update={"parameters": _flatten_for_serving(params)})
            result.append(tool)
        return result

    def patch_tool_schemas(self, mcp_instance: object) -> None:
        """Flatten input schemas directly in the FastMCP tool registry.

        This makes ``list_tools()`` (used by the UI generator scanner and the
        tool-extraction sync) return flat schemas as well. The ``on_list_tools``
        hook covers the runtime MCP path; direct registry access bypasses
        middleware entirely, so both are needed.
        """
        components = getattr(getattr(mcp_instance, "_local_provider", None), "_components", None)
        if not components:
            return

        for key, tool in list(components.items()):
            if not key.startswith("tool:"):
                continue
            params = getattr(tool, "parameters", None)
            if not isinstance(params, dict) or not params:
                continue
            flat = _flatten_for_serving(params)
            if flat != params:
                components[key] = tool.model_copy(update={"parameters": flat})
                logger.debug(f"Flattened registry schema for tool {tool.name}")
