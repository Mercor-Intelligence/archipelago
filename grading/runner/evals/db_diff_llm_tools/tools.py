"""Tool definitions and handlers for tool-augmented DB diff evaluation."""

import asyncio
import builtins
import collections
import io
import json
import math
import re
from types import SimpleNamespace
from typing import Any

# Max characters per tool response to keep conversation context manageable
MAX_TOOL_RESPONSE_SIZE = 10_000

# Default pagination limit for get_rows
DEFAULT_ROW_LIMIT = 20

# Timeout for run_python exec in seconds
EXEC_TIMEOUT_SECONDS = 30

# Safe builtins for run_python — no file/network/import access
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "frozenset": frozenset,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "zip": zip,
    # Exception types for error handling in user code
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "StopIteration": StopIteration,
    "RuntimeError": RuntimeError,
    "ZeroDivisionError": ZeroDivisionError,
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_table",
            "description": (
                "Get metadata for a specific table's changes: column names and "
                "row counts by change type (added/deleted/modified)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to inspect",
                    }
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rows",
            "description": (
                "Get specific changed rows from a table. Returns paginated results. "
                "For modified rows, each entry has 'before' and 'after' dicts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table",
                    },
                    "change_type": {
                        "type": "string",
                        "enum": ["added", "deleted", "modified"],
                        "description": "Type of change to retrieve",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of rows to skip (default: 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return (default: 20)",
                    },
                },
                "required": ["table_name", "change_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code against the diff data. The variable `diff_data` "
                "is pre-loaded as a dict with the full DB diff structure. "
                "Use print() to return values. "
                "Structure: diff_data['databases'][db_name]['tables'][table_name] "
                "has keys 'rows_added', 'rows_deleted', 'rows_modified'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                    }
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_verdict",
            "description": "Submit your final evaluation. Call this once you have enough evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "integer",
                        "enum": [0, 1],
                        "description": "1 = criteria satisfied, 0 = not satisfied",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concise explanation (2-3 sentences)",
                    },
                },
                "required": ["result", "reason"],
            },
        },
    },
]


def _get_all_tables(db_diff_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten all tables across all databases into a single dict.

    If there's only one database, table names are used as-is.
    If there are multiple databases, table names are prefixed with the db path
    to avoid collisions (e.g., "db1:users", "db2:users").
    """
    databases = db_diff_result.get("databases", {})
    tables: dict[str, dict[str, Any]] = {}

    if len(databases) == 1:
        db_data = next(iter(databases.values()))
        for table_name, table_diff in db_data.get("tables", {}).items():
            tables[table_name] = table_diff
    else:
        for db_path, db_data in databases.items():
            for table_name, table_diff in db_data.get("tables", {}).items():
                tables[f"{db_path}:{table_name}"] = table_diff

    return tables


def _get_columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    """Extract column names from the first row in a list."""
    if rows:
        return list(rows[0].keys())
    return []


def handle_inspect_table(args: dict[str, Any], db_diff_result: dict[str, Any]) -> str:
    """Handle inspect_table tool call."""
    table_name = args.get("table_name", "")
    tables = _get_all_tables(db_diff_result)

    if table_name not in tables:
        available = sorted(
            t
            for t, d in tables.items()
            if d.get("rows_added") or d.get("rows_deleted") or d.get("rows_modified")
        )
        return json.dumps(
            {
                "error": f"Table '{table_name}' not found",
                "available_tables_with_changes": available[:50],
            }
        )

    table_diff = tables[table_name]
    rows_added = table_diff.get("rows_added", [])
    rows_deleted = table_diff.get("rows_deleted", [])
    rows_modified = table_diff.get("rows_modified", [])

    # Get columns from whichever row list has data
    columns: list[str] = []
    if rows_added:
        columns = _get_columns_from_rows(rows_added)
    elif rows_deleted:
        columns = _get_columns_from_rows(rows_deleted)
    elif rows_modified:
        first_mod = rows_modified[0]
        columns = _get_columns_from_rows(
            [first_mod.get("before", first_mod.get("after", {}))]
        )

    return json.dumps(
        {
            "table_name": table_name,
            "columns": columns,
            "rows_added_count": len(rows_added),
            "rows_deleted_count": len(rows_deleted),
            "rows_modified_count": len(rows_modified),
        }
    )


def handle_get_rows(args: dict[str, Any], db_diff_result: dict[str, Any]) -> str:
    """Handle get_rows tool call."""
    table_name = args.get("table_name", "")
    change_type = args.get("change_type", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", DEFAULT_ROW_LIMIT)

    tables = _get_all_tables(db_diff_result)
    if table_name not in tables:
        return json.dumps({"error": f"Table '{table_name}' not found"})

    table_diff = tables[table_name]
    key_map = {
        "added": "rows_added",
        "deleted": "rows_deleted",
        "modified": "rows_modified",
    }
    key = key_map.get(change_type)
    if not key:
        return json.dumps({"error": f"Invalid change_type: {change_type}"})

    all_rows = table_diff.get(key, [])
    total = len(all_rows)
    page = all_rows[offset : offset + limit]

    return json.dumps(
        {
            "table_name": table_name,
            "change_type": change_type,
            "total": total,
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "rows": page,
        }
    )


def _run_exec_in_thread(code: str, exec_globals: dict[str, Any]) -> str:
    """Run exec() synchronously — meant to be called from a thread.

    Uses a thread-local print() that writes to a StringIO buffer instead of
    redirect_stdout, which is not thread-safe across concurrent verifiers.
    """
    stdout_capture = io.StringIO()

    def _safe_print(*args: object, **kwargs: Any) -> None:
        kwargs["file"] = stdout_capture
        builtins.print(*args, **kwargs)

    exec_globals["print"] = _safe_print

    try:
        exec(code, exec_globals)  # noqa: S102
        output = stdout_capture.getvalue()
        if not output:
            output = "(no output — use print() to return values)"
        return output
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def handle_run_python(
    args: dict[str, Any], db_diff_result: dict[str, Any]
) -> str:
    """Handle run_python tool call. Executes code with restricted builtins in a thread with timeout."""
    code = args.get("code", "")

    # Use SimpleNamespace proxies for stdlib modules to avoid exposing
    # module.__builtins__ which would allow sandbox escape.
    # Note: db_diff_result is already deep-copied once per verifier in main.py.
    exec_globals: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "diff_data": db_diff_result,
        "json": SimpleNamespace(
            dumps=json.dumps,
            loads=json.loads,
            JSONDecodeError=json.JSONDecodeError,
        ),
        "re": SimpleNamespace(
            search=re.search,
            match=re.match,
            findall=re.findall,
            sub=re.sub,
            split=re.split,
            compile=re.compile,
            IGNORECASE=re.IGNORECASE,
            MULTILINE=re.MULTILINE,
        ),
        "math": SimpleNamespace(
            ceil=math.ceil,
            floor=math.floor,
            sqrt=math.sqrt,
            log=math.log,
            log10=math.log10,
            pow=math.pow,
            fabs=math.fabs,
            inf=math.inf,
            nan=math.nan,
            pi=math.pi,
        ),
        "collections": SimpleNamespace(
            Counter=collections.Counter,
            defaultdict=collections.defaultdict,
            OrderedDict=collections.OrderedDict,
        ),
    }

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _run_exec_in_thread, code, exec_globals),
            timeout=EXEC_TIMEOUT_SECONDS,
        )
        return result
    except TimeoutError:
        return f"Error: Code execution timed out after {EXEC_TIMEOUT_SECONDS}s"


def truncate_tool_response(
    response: str, max_size: int = MAX_TOOL_RESPONSE_SIZE
) -> str:
    """Truncate tool response to stay within context budget."""
    if len(response) <= max_size:
        return response
    return response[:max_size] + "\n... [truncated]"


async def execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    db_diff_result: dict[str, Any],
) -> str:
    """Execute a tool call and return the result string."""
    if tool_name == "run_python":
        result = await handle_run_python(tool_args, db_diff_result)
        return truncate_tool_response(result)

    sync_handlers = {
        "inspect_table": handle_inspect_table,
        "get_rows": handle_get_rows,
    }

    handler = sync_handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    result = handler(tool_args, db_diff_result)
    return truncate_tool_response(result)


def build_summary(db_diff_result: dict[str, Any]) -> str:
    """Build a compact summary of the DB diff for the initial prompt."""
    lines = ["=== DATABASE CHANGES SUMMARY ==="]

    summary = db_diff_result.get("summary", {})
    lines.append(f"Total rows added: {summary.get('total_rows_added', 0)}")
    lines.append(f"Total rows deleted: {summary.get('total_rows_deleted', 0)}")
    lines.append(f"Total rows modified: {summary.get('total_rows_modified', 0)}")
    lines.append("")

    # List tables with change counts
    tables = _get_all_tables(db_diff_result)
    changed_tables = []
    unchanged_count = 0

    for table_name in sorted(tables.keys()):
        table_diff = tables[table_name]
        added = len(table_diff.get("rows_added", []))
        deleted = len(table_diff.get("rows_deleted", []))
        modified = len(table_diff.get("rows_modified", []))
        if added or deleted or modified:
            changed_tables.append(
                f"  {table_name}: +{added} added, -{deleted} deleted, ~{modified} modified"
            )
        else:
            unchanged_count += 1

    if changed_tables:
        lines.append(f"Tables with changes ({len(changed_tables)}):")
        lines.extend(changed_tables)

    if unchanged_count:
        lines.append(f"\n({unchanged_count} tables unchanged)")

    return "\n".join(lines)
