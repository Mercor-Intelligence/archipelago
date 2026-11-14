"""Middleware that truncates oversized list responses before they reach the LLM.

Intercepts large list payloads in tool responses (e.g. 89K stock symbols) and
caps them at a configurable maximum, injecting a ``_pagination`` metadata block
so the LLM knows the response was truncated and can refine its request.

Controlled by the ``MAX_RESPONSE_ITEMS`` environment variable (default: 200).

A caller that asked for a specific number of rows is honoured up to
``MAX_EXPLICIT_LIMIT`` rather than being cut to the default cap. Without that,
the ``limit`` every historical-price tool advertises could not raise the ceiling
it was documented to raise, and the truncation notice below recommended a
parameter it then ignored.
"""

from typing import override

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams

# Ceiling on an explicit caller ``limit``. Matches the upper bound the request
# models declare (``le=1000``), so a caller cannot use it to defeat the cap
# entirely.
MAX_EXPLICIT_LIMIT = 1000


def _is_date_series(items: list) -> bool:
    """Whether a truncated list is a dated series, i.e. ordered by trading day.

    Decides which remedy the notice can honestly recommend: paging with
    ``from_date``/``to_date`` only means something for the price histories, not
    for a symbol directory or a batch of quotes, which this same middleware also
    truncates.
    """
    return bool(items) and isinstance(items[0], dict) and "date" in items[0]


def _requested_limit(arguments: object) -> int | None:
    """Read the caller's explicit ``limit``, clamped to ``MAX_EXPLICIT_LIMIT``.

    Tools in this server take their arguments as a single ``request`` object, so
    the limit lives one level down; the flat form is accepted too.
    """
    if not isinstance(arguments, dict):
        return None
    source = arguments.get("request") if isinstance(arguments.get("request"), dict) else arguments
    value = source.get("limit")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return min(value, MAX_EXPLICIT_LIMIT)


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

        # An explicit caller limit raises the cap for this call only.
        cap = max(self.max_items, _requested_limit(context.message.arguments) or 0)

        # Check candidate list fields: prefer "data", then any other list field
        candidate_keys = ["data"] + [k for k in sc.keys() if k != "data"]
        truncated_any = False

        for key in candidate_keys:
            value = sc.get(key)
            if isinstance(value, list) and len(value) > cap:
                total = len(value)
                message = (
                    f"Response truncated from {total} to {cap} items, keeping the first "
                    f"{cap} the upstream feed returned. Raise 'limit' (up to "
                    f"{MAX_EXPLICIT_LIMIT}) or add filters to narrow results."
                )
                if _is_date_series(value):
                    message += (
                        " These are newest-first by date, so the oldest part of the "
                        "requested range is the part that is missing; narrow "
                        "'from_date'/'to_date' and page back through the range to read it."
                    )
                sc[key] = value[:cap]
                sc["_pagination"] = {
                    "field": key,
                    "total_count": total,
                    "returned": cap,
                    "truncated": True,
                    "message": message,
                }
                if "count" in sc:
                    sc["count"] = cap
                truncated_any = True
                logger.info(
                    f"Truncated {context.message.name!r} response: "
                    f"{key!r} had {total} items, capped to {cap}"
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
