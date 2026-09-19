"""Response size limiting middleware with stateless pagination.

When a tool response exceeds the MCP buffer limit (~64KB), this middleware
paginates the data and returns the requested page.  Callers fetch subsequent
pages by re-invoking the **same tool** with an extra ``page_number`` argument.

The middleware is stateless: every call re-executes the underlying tool, then
slices the result to the requested page.  No cache or companion tool is needed.

Tools that declare their own pagination parameters (``page``, ``per_page``)
are detected automatically: the middleware skips injecting its own
``page_number`` / ``limit`` to avoid duplicate controls, but still provides
an overflow safety-net if the response exceeds the buffer limit.  The
``_pagination`` metadata in paginated responses includes ``page_param`` and
``limit_param`` so that UI clients know which parameter names to use.

Applications that use different native pagination parameter names (e.g.
``start`` / ``limit`` instead of ``page`` / ``per_page``) can pass
``native_pagination_params`` to the constructor to tell the middleware
which names to recognise as native pagination.

When ``pagination_key`` is configured (e.g. ``"meta"``), the middleware
also inspects non-overflowing responses for the tool's own pagination
object at that key and synthesises a ``_pagination`` block from it.

Usage:
    Automatically registered by ``run_server()``.  Can also be added manually::

        mcp.add_middleware(ResponseLimiterMiddleware(pagination_key="meta"))
"""

import fnmatch
import json
from collections.abc import Callable, Sequence
from copy import deepcopy

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import Tool, ToolResult
from loguru import logger
from mcp.types import CallToolRequestParams, TextContent

# MCP stdio has a ~64KB line buffer limit. Use 50KB to be safe.
MAX_RESPONSE_SIZE_BYTES = 50 * 1024
METADATA_RESERVE_BYTES = 500

# Parameters the middleware manages.  Each is injected into matching tool
# schemas when the tool does not already declare it natively.
_PAGINATION_PARAMS: dict[str, dict] = {
    "page_number": {
        "type": "integer",
        "description": ("Page number for paginated results (1-indexed). Omit for first page."),
    },
    "limit": {
        "type": "integer",
        "description": "Maximum rows per page for paginated results.",
    },
}

# Names that indicate a tool has its own pagination.  When any of these
# are present in a tool's declared properties, the middleware skips
# injecting page_number / limit to avoid confusing duplication.
_NATIVE_PAGINATION_PARAMS = {"page", "per_page"}

# Top-level keys that carry metadata rather than the primary data array.
# Used by _paginate_data when scanning for the first list-valued key.
_METADATA_KEYS = {"meta", "_pagination", "row_count", "total", "total_count", "links"}


def _token_pattern_match(tool_name: str, pattern: str) -> bool:
    """Match *pattern* against *tool_name* with snake_case token awareness.

    Leading/trailing ``*`` anchors align to ``_`` token boundaries so that
    ``*list*`` expands to the four cases: ``list``, ``list_*``, ``*_list``,
    ``*_list_*`` — matching ``list`` as a whole token, not a substring.

    Patterns without wildcards are compared as exact strings.  A bare ``*``
    matches everything.
    """
    if pattern == "*":
        return True

    # No wildcards → exact match
    if "*" not in pattern and "?" not in pattern and "[" not in pattern:
        return fnmatch.fnmatch(tool_name, pattern)

    # Strip leading/trailing * to get the core, then re-anchor to _ boundaries
    core = pattern.strip("*")
    has_leading = pattern.startswith("*")
    has_trailing = pattern.endswith("*")

    # Build the set of concrete fnmatch patterns
    expanded: list[str] = []
    if has_leading and has_trailing:
        # *core* → core | core_* | *_core | *_core_*
        expanded = [core, f"{core}_*", f"*_{core}", f"*_{core}_*"]
    elif has_leading:
        # *core → core | *_core
        expanded = [core, f"*_{core}"]
    elif has_trailing:
        # core* → core | core_*
        expanded = [core, f"{core}_*"]
    else:
        expanded = [pattern]

    return any(fnmatch.fnmatch(tool_name, p) for p in expanded)


class ResponseLimiterMiddleware(Middleware):
    """Paginates large tool responses to prevent buffer overflow.

    On every tool call the middleware:

    1. Extracts ``page_number`` and ``limit`` from the arguments.
       Parameters that the middleware *injected* (because the tool did not
       declare them natively) are stripped so the tool never sees them.
       Native parameters are left in place and forwarded to the tool.
    2. Forwards the call to the next handler via ``call_next``.
    3. If the serialised response exceeds *max_size* bytes, paginates the
       result and returns only the requested page (default 1).
    4. Adds ``_pagination`` metadata telling the caller to re-invoke the
       same tool with ``page_number=N`` for additional pages.
    """

    def __init__(
        self,
        max_size: int = MAX_RESPONSE_SIZE_BYTES,
        tool_patterns: list[str] | None = None,
        pagination_key: str | None = None,
        native_pagination_params: dict[str, str] | None = None,
    ):
        self.max_size = max_size
        self.tool_patterns = tool_patterns or ["*"]
        self.pagination_key = pagination_key
        # Custom native pagination param names.  Keys are semantic roles
        # ("page", "limit"), values are the actual parameter names used by
        # the application (e.g. {"page": "start", "limit": "limit"}).
        # When None, falls back to the default _NATIVE_PAGINATION_PARAMS.
        self._native_param_map = native_pagination_params
        # Track which pagination params were injected (not native) per tool.
        # tool_name -> set of param names that we added.
        self._injected_params: dict[str, set[str]] = {}
        # Track the actual page/limit parameter names each tool uses.
        # tool_name -> (page_param_name, limit_param_name | None)
        self._page_param_names: dict[str, tuple[str, str | None]] = {}
        # Track wrapper-model property names per tool so on_call_tool can
        # default them to {} when absent/None (prevents validation errors).
        # tool_name -> set of wrapper property names (e.g. {"params"})
        self._wrapper_props: dict[str, set[str]] = {}

    @staticmethod
    def _resolve_inner_props(
        props: dict[str, dict], schema: dict | None = None
    ) -> tuple[set[str], set[str]]:
        """Collect all property names, resolving wrapper/``$ref`` patterns.

        When a tool has a single Pydantic-model parameter (e.g.
        ``params: SearchCandidatesInput``), FastMCP exposes it as a single
        ``params`` property with a ``$ref``.  This helper resolves that
        reference so we can detect native pagination fields like ``page``
        and ``per_page`` inside the wrapper.

        Returns ``(all_property_names, wrapper_property_names)`` where
        *wrapper_property_names* lists the top-level properties that are
        ``$ref`` wrappers.  The caller uses this to default absent wrapper
        args to ``{}`` at call time.
        """
        all_names = set(props)
        wrapper_names: set[str] = set()
        if schema is None:
            return all_names, wrapper_names

        defs = schema.get("$defs", schema.get("definitions", {}))
        if not defs:
            return all_names, wrapper_names

        for prop_name, prop_info in props.items():
            ref = prop_info.get("$ref") or ""
            # Also check inside allOf (Pydantic v2 wraps $ref in allOf)
            if not ref and "allOf" in prop_info:
                for item in prop_info["allOf"]:
                    if "$ref" in item:
                        ref = item["$ref"]
                        break
            if ref.startswith(("#/$defs/", "#/definitions/")):
                wrapper_names.add(prop_name)
                def_name = ref.split("/")[-1]
                resolved = defs.get(def_name, {})
                inner = resolved.get("properties", {})
                all_names.update(inner)
        return all_names, wrapper_names

    def _inject_params(
        self,
        tool_name: str,
        props: dict[str, dict],
        schema: dict | None = None,
    ) -> dict[str, dict] | None:
        """Return pagination params to inject, or None if nothing to add.

        If the tool already declares native pagination parameters (``page``,
        ``per_page``) — either directly or inside a wrapper model — injection
        is skipped entirely so users don't see duplicate pagination controls.
        The native param names are recorded in ``self._page_param_names`` for
        use by the overflow safety-net.

        Records injected param names in ``self._injected_params``.
        """
        # Check both top-level and wrapper-resolved properties
        all_prop_names, wrapper_names = self._resolve_inner_props(props, schema)

        # Record wrapper properties so on_call_tool can default them to {}
        if wrapper_names:
            self._wrapper_props[tool_name] = wrapper_names

        # If tool already has native pagination params, skip injection.
        # Check custom native params first, then fall back to defaults.
        if self._native_param_map:
            native_names = set(self._native_param_map.values())
            if native_names & all_prop_names:
                page_name = self._native_param_map.get("page")
                limit_name = self._native_param_map.get("limit")
                if page_name and page_name in all_prop_names:
                    self._page_param_names[tool_name] = (
                        page_name,
                        limit_name if limit_name and limit_name in all_prop_names else None,
                    )
                return None
        elif _NATIVE_PAGINATION_PARAMS & all_prop_names:
            page_name = "page" if "page" in all_prop_names else None
            limit_name = "per_page" if "per_page" in all_prop_names else None
            if page_name:
                self._page_param_names[tool_name] = (page_name, limit_name)
            return None

        to_inject: dict[str, dict] = {}
        for param_name, param_schema in _PAGINATION_PARAMS.items():
            if param_name not in props:
                to_inject[param_name] = param_schema
                self._injected_params.setdefault(tool_name, set()).add(param_name)
        if to_inject:
            self._page_param_names[tool_name] = (
                "page_number",
                "limit" if "limit" in to_inject else None,
            )
        return to_inject or None

    def patch_tool_schemas(self, mcp_instance: object) -> None:
        """Inject pagination parameters into matching tools on the FastMCP instance.

        This patches the tool manager directly so that ``get_tools()`` (used
        by the UI generator scanner) returns schemas that already include the
        pagination parameters.  The ``on_list_tools`` middleware hook provides
        the same injection at runtime for MCP ``list_tools`` calls, but
        ``get_tools()`` bypasses middleware entirely.
        """
        tools = getattr(getattr(mcp_instance, "_tool_manager", None), "_tools", None)
        if not tools:
            return

        for name, tool in list(tools.items()):
            if self._matches(name):
                props = tool.parameters.get("properties", {})
                to_inject = self._inject_params(name, props, schema=tool.parameters)
                if to_inject:
                    params = deepcopy(tool.parameters)
                    params.setdefault("properties", {}).update(to_inject)
                    tools[name] = tool.model_copy(update={"parameters": params})

    def _matches(self, tool_name: str) -> bool:
        """Return True if the tool name matches any of the configured patterns.

        Matching is snake_case-token-aware: ``*`` at the edges of a pattern
        aligns to ``_`` token boundaries so that ``*list*`` matches
        ``list_folders`` and ``get_list`` but **not** ``enlist`` or
        ``blacklisted``.  A bare ``*`` still matches everything.
        """
        return any(_token_pattern_match(tool_name, pat) for pat in self.tool_patterns)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Inject pagination parameters into matching tool schemas."""
        tools = await call_next(context)
        result = []
        for tool in tools:
            if self._matches(tool.name):
                props = tool.parameters.get("properties", {})
                to_inject = self._inject_params(tool.name, props, schema=tool.parameters)
                if to_inject:
                    params = deepcopy(tool.parameters)
                    params.setdefault("properties", {}).update(to_inject)
                    tool = tool.model_copy(update={"parameters": params})
            result.append(tool)
        return result

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        # If tool doesn't match patterns, skip pagination entirely
        tool_name = context.message.name
        if not self._matches(tool_name):
            return await call_next(context)

        injected = self._injected_params.get(tool_name, set())
        args = context.message.arguments or {}
        context.message.arguments = args

        # Ensure wrapper-model properties default to {} when absent —
        # without this, FastMCP receives None for required wrapper params
        # (e.g. params: EmptyModel) and raises a validation error.
        for prop_name in self._wrapper_props.get(tool_name, ()):
            if prop_name not in args or args[prop_name] is None:
                args[prop_name] = {}

        page_param, limit_param = self._page_param_names.get(tool_name, ("page_number", "limit"))

        def _safe_int(value: object, default: int | None = None) -> int | None:
            try:
                return int(value)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                return default

        # Extract page_number; strip only if it was injected (not native)
        page_number = 1
        if args and "page_number" in args:
            page_number = _safe_int(args["page_number"], 1) or 1
            if "page_number" in injected:
                args.pop("page_number")

        # Extract limit; strip only if it was injected (not native)
        requested_limit = None
        if args and "limit" in args:
            requested_limit = _safe_int(args["limit"])
            if "limit" in injected:
                args.pop("limit")

        # Also read native pagination params for the overflow safety-net.
        # These are never stripped — they belong to the tool and are
        # forwarded as-is.
        if page_param not in ("page_number",) and args and page_param in args:
            page_number = _safe_int(args[page_param], 1) or 1
        if limit_param and limit_param not in ("limit",) and args and limit_param in args:
            requested_limit = _safe_int(args[limit_param])

        response = await call_next(context)

        if not isinstance(response, ToolResult):
            return response

        try:
            content_text = ""
            if response.content:
                for item in response.content:
                    if isinstance(item, TextContent):
                        content_text += item.text

            size = len(content_text.encode("utf-8"))

            if size > self.max_size:
                logger.warning(f"Response too large ({size} bytes) for {tool_name}. Paginating...")
                # When a natively-paginated tool overflows, we are
                # sub-paginating a single response — always start at
                # sub-page 1 regardless of the tool's own page value.
                overflow_page = 1 if page_param != "page_number" else page_number
                return self._paginate(
                    content_text,
                    size,
                    overflow_page,
                    tool_name,
                    requested_limit,
                    page_param=page_param,
                    limit_param=limit_param,
                )

            # When a pagination_key is configured, check if the tool's
            # response contains its own pagination object at that key.
            # Convert it to the standard ``_pagination`` block so the UI
            # can render pagination controls uniformly.
            if self.pagination_key and content_text:
                response = self._extract_pagination(
                    content_text, response, tool_name, page_param, limit_param
                )

        except Exception as e:
            logger.warning(f"Could not check response size: {e}")

        return response

    def _extract_pagination(
        self,
        content_text: str,
        response: ToolResult,
        tool_name: str,
        page_param: str,
        limit_param: str | None,
    ) -> ToolResult:
        """Extract pagination from the configured ``pagination_key`` in the response.

        When ``pagination_key`` is set (e.g. ``"meta"``), the middleware looks
        for that key in the tool's JSON response and expects an object with
        ``page``, ``per_page``, and ``total`` fields.  These are converted
        into the standard ``_pagination`` block so the UI can show pagination
        controls uniformly.
        """
        try:
            data = json.loads(content_text)
        except (json.JSONDecodeError, TypeError):
            return response

        if not isinstance(data, dict):
            return response

        meta = data.get(self.pagination_key)
        if not isinstance(meta, dict):
            return response

        page = meta.get("page")
        per_page = meta.get("per_page")
        total = meta.get("total")

        if not (isinstance(page, int) and isinstance(per_page, int) and per_page > 0):
            return response

        # If total is unknown or everything fits on one page, nothing to do.
        if not isinstance(total, int) or total <= per_page:
            return response

        total_pages = (total + per_page - 1) // per_page
        has_more = page < total_pages

        page_info: dict = {
            "page": page,
            "total_pages": total_pages,
            "total_rows": total,
            "rows_per_page": per_page,
            "has_more": has_more,
            "page_param": page_param,
        }
        if limit_param:
            page_info["limit_param"] = limit_param

        if has_more:
            page_info["message"] = (
                f"Showing page {page} of {total_pages}. "
                f"Call {tool_name} with {page_param}={page + 1} for more."
            )
        else:
            page_info["message"] = f"Page {page} of {total_pages} (last page)."

        data["_pagination"] = page_info
        result_json = json.dumps(data)
        return ToolResult(
            content=[TextContent(type="text", text=result_json)],
            structured_content=data,
        )

    def _paginate(
        self,
        content_text: str,
        original_size: int,
        page_number: int,
        tool_name: str,
        requested_limit: int | None = None,
        page_param: str = "page_number",
        limit_param: str | None = "limit",
    ) -> ToolResult:
        """Paginate response and return the requested page."""
        try:
            data = json.loads(content_text)
            result = self._paginate_data(
                data,
                original_size,
                page_number,
                tool_name,
                requested_limit,
                page_param=page_param,
                limit_param=limit_param,
            )
            result_json = json.dumps(result)
            return ToolResult(
                content=[TextContent(type="text", text=result_json)],
                structured_content=result,
            )
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Cannot paginate: {e}")
            target = self.max_size - 100
            truncated = content_text.encode("utf-8")[:target].decode("utf-8", errors="ignore")
            return ToolResult(
                content=[TextContent(type="text", text=truncated + "\n[TRUNCATED]")],
            )

    def _paginate_data(
        self,
        data: dict,
        original_size: int,
        page_number: int,
        tool_name: str,
        requested_limit: int | None = None,
        page_param: str = "page_number",
        limit_param: str | None = "limit",
    ) -> dict:
        """Find data array and paginate it."""
        if not isinstance(data, dict):
            return {"error": "Cannot paginate", "original_size": original_size}

        pag_kw = {"page_param": page_param, "limit_param": limit_param}

        # Pattern 1: {"data": {"data": [...], ...}, ...} (nested from meta-tools)
        if "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "data" in inner and isinstance(inner["data"], list):
                return self._do_paginate(
                    data_array=inner["data"],
                    build_page=lambda rows, page_info: {
                        **data,
                        "data": {**inner, "data": rows, "row_count": len(rows)},
                        **({"_pagination": page_info} if page_info else {}),
                    },
                    page_number=page_number,
                    tool_name=tool_name,
                    requested_limit=requested_limit,
                    **pag_kw,
                )

        # Pattern 2: {"data": [...], ...} (flat)
        if "data" in data and isinstance(data["data"], list):
            return self._do_paginate(
                data_array=data["data"],
                build_page=lambda rows, page_info: {
                    **data,
                    "data": rows,
                    "row_count": len(rows),
                    **({"_pagination": page_info} if page_info else {}),
                },
                page_number=page_number,
                tool_name=tool_name,
                requested_limit=requested_limit,
                **pag_kw,
            )

        # Pattern 3: {<key>: [...], ...} — first list-valued key that isn't
        # a known metadata key (handles e.g. {"departments": [...]}).
        for key, value in data.items():
            if key in _METADATA_KEYS:
                continue
            if isinstance(value, list):
                array_key = key
                return self._do_paginate(
                    data_array=value,
                    build_page=lambda rows, page_info, _k=array_key: {
                        **{k: v for k, v in data.items() if k != _k},
                        _k: rows,
                        **({"_pagination": page_info} if page_info else {}),
                    },
                    page_number=page_number,
                    tool_name=tool_name,
                    requested_limit=requested_limit,
                    **pag_kw,
                )

        return {
            "error": "Response too large, cannot paginate",
            "original_size": original_size,
        }

    def _do_paginate(
        self,
        data_array: list,
        build_page: Callable,
        page_number: int,
        tool_name: str,
        requested_limit: int | None = None,
        page_param: str = "page_number",
        limit_param: str | None = "limit",
    ) -> dict:
        """Paginate array and return the requested page."""
        total_rows = len(data_array)
        max_page_size = self._find_page_size(data_array, build_page)

        # Use user-requested limit if provided, but cap at the size-safe maximum
        if requested_limit is not None and requested_limit > 0:
            rows_per_page = min(requested_limit, max_page_size)
        else:
            rows_per_page = max_page_size

        if rows_per_page >= total_rows:
            return build_page(data_array, None)

        total_pages = (total_rows + rows_per_page - 1) // rows_per_page

        # Clamp page_number
        page_number = max(1, min(page_number, total_pages))

        start = (page_number - 1) * rows_per_page
        end = min(start + rows_per_page, total_rows)
        page_rows = data_array[start:end]

        has_more = page_number < total_pages
        page_info: dict = {
            "page": page_number,
            "total_pages": total_pages,
            "total_rows": total_rows,
            "rows_per_page": rows_per_page,
            "has_more": has_more,
            "page_param": page_param,
        }
        if limit_param:
            page_info["limit_param"] = limit_param

        if has_more:
            next_page = page_number + 1
            page_info["message"] = (
                f"Showing page {page_number} of {total_pages}. "
                f"Call {tool_name} with {page_param}={next_page} for more."
            )
        else:
            page_info["message"] = f"Page {page_number} of {total_pages} (last page)."

        logger.info(
            f"Paginated {tool_name}: {total_rows} rows, "
            f"page {page_number}/{total_pages} ({rows_per_page}/page)"
        )
        return build_page(page_rows, page_info)

    def _find_page_size(self, data_array: list, build_page: Callable) -> int:
        """Binary search for max rows that fit."""
        target = self.max_size - METADATA_RESERVE_BYTES
        total = len(data_array)
        low, high, best = 1, total, 1

        while low <= high:
            mid = (low + high) // 2
            test = build_page(data_array[:mid], {"page": 1, "total_pages": 1})
            try:
                size = len(json.dumps(test, default=str).encode("utf-8"))
                if size <= target:
                    best = mid
                    low = mid + 1
                else:
                    high = mid - 1
            except (TypeError, ValueError):
                high = mid - 1

        return best
