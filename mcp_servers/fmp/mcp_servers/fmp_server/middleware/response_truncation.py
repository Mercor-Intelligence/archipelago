"""Middleware that truncates oversized list responses before they reach the LLM.

Intercepts large list payloads in tool responses (e.g. 89K stock symbols) and
caps them at a configurable maximum, injecting a ``_pagination`` metadata block
so the LLM knows the response was truncated and can refine its request.

Controlled by the ``MAX_RESPONSE_ITEMS`` environment variable (default: 200).
"""

from typing import override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams


class ResponseTruncationMiddleware(Middleware):
    """Caps oversized list fields in tool responses to prevent context-window blowouts.

    When a tool returns a ``dict`` with a list field exceeding ``max_items``,
    the field is truncated and a ``_pagination`` key is added describing what
    was cut and how to narrow the request.

    The ``data`` field is checked first; if absent, the first other oversized
    list field is truncated. At most one field is truncated per response.

    Args:
        max_items: Maximum number of items allowed in any single list field.
    """

    def __init__(self, max_items: int = 200) -> None:
        self.max_items = max_items

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)

        if result.structured_content is None:
            return result

        structured = result.structured_content.copy()

        # Support both plain dicts and FastMCP wrapped results ({"result": {...}})
        if (
            isinstance(structured, dict)
            and "result" in structured
            and isinstance(structured["result"], dict)
        ):
            sc = structured["result"]
        elif isinstance(structured, dict):
            sc = structured
        else:
            return result

        # Check candidate list fields: prefer "data", then any other list field
        candidate_keys = ["data"] + [k for k in sc.keys() if k != "data"]
        truncated_any = False

        for key in candidate_keys:
            value = sc.get(key)
            if isinstance(value, list) and len(value) > self.max_items:
                total = len(value)
                sc[key] = value[: self.max_items]
                sc["_pagination"] = {
                    "field": key,
                    "total_count": total,
                    "returned": self.max_items,
                    "truncated": True,
                    "message": (
                        f"Response truncated from {total} to {self.max_items} items. "
                        "Use the 'limit' parameter or add filters to narrow results."
                    ),
                }
                if "count" in sc:
                    sc["count"] = self.max_items
                truncated_any = True
                logger.info(
                    f"Truncated {context.message.name!r} response: "
                    f"{key!r} had {total} items, capped to {self.max_items}"
                )
                break

        if not truncated_any:
            return result

        # Rebuild ToolResult using FastMCP-native conversion so content and
        # structured_content stay in sync without hardcoding TextContent.
        return ToolResult(
            content=structured,
            structured_content=structured,
        )
