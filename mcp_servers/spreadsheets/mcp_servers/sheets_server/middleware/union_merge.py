"""Serve every tool's input schema with its object ``oneOf`` unions merged (ENV-501)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import override

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool
from utils.schema_unions import merge_object_unions


class UnionMergeMiddleware(Middleware):
    """Apply ``merge_object_unions`` to each listed tool's input schema.

    ``on_list_tools`` covers every reader: ``tools/list`` over the wire, the
    in-memory client, and direct ``mcp.list_tools()`` callers such as
    ``scripts/extract_tools.py`` (the release sync). It does not depend on the
    startup schema pass having finished, which it has not when ``main`` is
    imported inside a running event loop. Already-merged schemas pass through
    unchanged, so running after the startup pass is a no-op.
    """

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        served: list[Tool] = []
        for tool in tools:
            params = tool.parameters
            if isinstance(params, dict):
                merged = merge_object_unions(params)
                if merged != params:
                    tool = tool.model_copy(update={"parameters": merged})
            served.append(tool)
        return served
