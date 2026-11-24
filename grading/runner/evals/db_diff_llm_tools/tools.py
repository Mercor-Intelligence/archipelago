"""Tool definitions and handlers for tool-augmented DB diff evaluation."""

import ast
import asyncio
import base64
import builtins
import collections
import io
import json
import math
import re
import sqlite3
import string
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from runner.helpers.db_diff.main import search_diff_rows

# Max characters per tool response to keep conversation context manageable
MAX_TOOL_RESPONSE_SIZE = 10_000

# Default pagination limit for get_rows
DEFAULT_ROW_LIMIT = 20

# Max rows a single search_rows call may return
MAX_SEARCH_LIMIT = 50

# Timeout for run_python exec in seconds
EXEC_TIMEOUT_SECONDS = 30

# Safe builtins for run_python — no file or network access
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "bytearray": bytearray,
    "bytes": bytes,
    "chr": chr,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "hasattr": hasattr,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "ord": ord,
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
            "name": "search_rows",
            "description": (
                "Search ALL changed rows of a table for a substring "
                "(case-insensitive, matched against every column). Unlike "
                "get_rows and run_python — which only see the materialized row "
                "bodies, which are capped per table — this searches the COMPLETE diff "
                "data, so use it to confirm presence or absence of a value in "
                "tables whose row bodies are truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to search",
                    },
                    "change_type": {
                        "type": "string",
                        "enum": ["added", "deleted", "modified"],
                        "description": "Type of change to search within",
                    },
                    "contains": {
                        "type": "string",
                        "description": "Substring to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum matches to return (default: 20)",
                    },
                },
                "required": ["table_name", "change_type", "contains"],
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
                "has keys 'rows_added', 'rows_deleted', 'rows_modified'. "
                "base64, collections, json, math and re are available, either "
                "imported or used directly; nothing else is, there is no file or "
                "network access, and dunder attributes cannot be read. A row's "
                "stored file content is base64, so read it with "
                "base64.b64decode(row['content_base64']).decode('utf-8', "
                "errors='replace') rather than decoding it by hand."
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


def _change_counts(table_diff: dict[str, Any]) -> tuple[int, int, int]:
    """Exact (added, deleted, modified) counts for a table.

    Prefers the helper's ``counts`` (true totals, independent of how many row
    bodies were materialized) and falls back to list lengths for older diffs.
    """
    counts = table_diff.get("counts")
    if isinstance(counts, dict):
        return (
            counts.get("added", 0),
            counts.get("deleted", 0),
            counts.get("modified", 0),
        )
    return (
        len(table_diff.get("rows_added", [])),
        len(table_diff.get("rows_deleted", [])),
        len(table_diff.get("rows_modified", [])),
    )


def handle_inspect_table(args: dict[str, Any], db_diff_result: dict[str, Any]) -> str:
    """Handle inspect_table tool call."""
    table_name = args.get("table_name", "")
    tables = _get_all_tables(db_diff_result)

    if table_name not in tables:
        available = sorted(
            t
            for t, d in tables.items()
            if any(_change_counts(d)) or d.get("schema_changed") or d.get("error")
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
    added_count, deleted_count, modified_count = _change_counts(table_diff)

    # Prefer the schema recorded by the helper: the byte cap can retain zero
    # rows for a table with exact positive counts, and this tool promises the
    # judge column names either way. Fall back to the rows for older diffs.
    columns: list[str] = list(table_diff.get("columns") or [])
    if columns:
        pass
    elif rows_added:
        columns = _get_columns_from_rows(rows_added)
    elif rows_deleted:
        columns = _get_columns_from_rows(rows_deleted)
    elif rows_modified:
        first_mod = rows_modified[0]
        columns = _get_columns_from_rows(
            [first_mod.get("before", first_mod.get("after", {}))]
        )

    payload: dict[str, Any] = {
        "table_name": table_name,
        "columns": columns,
        "rows_added_count": added_count,
        "rows_deleted_count": deleted_count,
        "rows_modified_count": modified_count,
        # True if more changed rows exist than were materialized for get_rows
        # (counts are still exact above).
        "rows_truncated": bool(table_diff.get("truncated")),
    }
    # Schema drift: columns added/removed between snapshots are not value-compared
    # for "modified", so flag them for the judge.
    if table_diff.get("schema_changed"):
        payload["schema_changed"] = table_diff["schema_changed"]
    # Surface a per-table diff failure rather than presenting it as a clean,
    # empty table with no changes.
    if table_diff.get("error"):
        payload["error"] = table_diff["error"]
    return json.dumps(payload)


def handle_get_rows(args: dict[str, Any], db_diff_result: dict[str, Any]) -> str:
    """Handle get_rows tool call."""
    table_name = args.get("table_name", "")
    change_type = args.get("change_type", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", DEFAULT_ROW_LIMIT)

    tables = _get_all_tables(db_diff_result)
    if table_name not in tables:
        # Over several databases the keys are qualified, so a bare name misses
        # and a bare error reads as "that table has no changes".
        available = sorted(t for t, d in tables.items() if any(_change_counts(d)))
        return json.dumps(
            {
                "error": f"Table '{table_name}' not found",
                "available_tables_with_changes": available[:50],
            }
        )

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
    materialized = len(all_rows)
    # True total for this change type (may exceed the materialized rows when the
    # helper capped row bodies); fall back to the list length for older diffs.
    counts = table_diff.get("counts")
    total = (
        counts.get(change_type, materialized)
        if isinstance(counts, dict)
        else materialized
    )
    page = all_rows[offset : offset + limit]

    return json.dumps(
        {
            "table_name": table_name,
            "change_type": change_type,
            "total": total,
            "materialized": materialized,
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "rows": page,
        }
    )


def _resolve_table(
    db_diff_result: dict[str, Any], table_name: str
) -> tuple[str, str, dict[str, Any]] | None:
    """Map a judge-facing table name back to ``(db_path, bare_name, table_diff)``.

    Inverse of the naming in ``_get_all_tables``: bare names for a single
    database, ``{db_path}:{table}`` prefixes for multiple.
    """
    databases = db_diff_result.get("databases", {})
    if len(databases) == 1:
        db_path = next(iter(databases))
        table_diff = databases[db_path].get("tables", {}).get(table_name)
        if table_diff is not None:
            return db_path, table_name, table_diff
        return None
    for db_path, db_data in databases.items():
        prefix = f"{db_path}:"
        if table_name.startswith(prefix):
            bare_name = table_name[len(prefix) :]
            table_diff = db_data.get("tables", {}).get(bare_name)
            if table_diff is not None:
                return db_path, bare_name, table_diff
    return None


def handle_search_rows(args: dict[str, Any], db_diff_result: dict[str, Any]) -> str:
    """Handle search_rows tool call — substring search over ALL changed rows.

    All arguments are untrusted LLM output: the table name is resolved against
    the diff's own table set, the substring is only ever bound as a query
    parameter, and the limit is clamped.
    """
    table_name = args.get("table_name", "")
    change_type = args.get("change_type", "")
    contains = str(args.get("contains", ""))
    try:
        limit = int(args.get("limit") or DEFAULT_ROW_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_ROW_LIMIT
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))

    if change_type not in ("added", "deleted", "modified"):
        return json.dumps({"error": f"Invalid change_type: {change_type}"})
    if not contains:
        return json.dumps({"error": "Missing required field: contains"})

    resolved = _resolve_table(db_diff_result, table_name)
    if resolved is None:
        tables = _get_all_tables(db_diff_result)
        available = sorted(t for t, d in tables.items() if any(_change_counts(d)))
        return json.dumps(
            {
                "error": f"Table '{table_name}' not found",
                "available_tables_with_changes": available[:50],
            }
        )
    db_path, bare_name, table_diff = resolved

    response = {
        "table_name": table_name,
        "change_type": change_type,
        "contains": contains,
        "searched_complete_data": True,
    }

    # Completeness is per change type: another change type may be capped
    # (table-level truncated=True) while this one is fully materialized.
    rows = table_diff.get(f"rows_{change_type}", [])
    total = dict(
        zip(("added", "deleted", "modified"), _change_counts(table_diff), strict=True)
    )[change_type]
    if len(rows) >= total:
        # All detected rows of this change type were materialized — search
        # them directly.
        needle = contains.lower()
        matches = [r for r in rows if needle in json.dumps(r, default=str).lower()][
            :limit
        ]
    else:
        source = db_diff_result.get("source_files", {}).get(db_path)
        if not source:
            return json.dumps(
                {
                    "error": (
                        f"Row bodies for change type '{change_type}' are "
                        "truncated and no searchable source was retained. "
                        "Absence from get_rows/run_python output is "
                        "INCONCLUSIVE for this table."
                    ),
                    "searched_complete_data": False,
                }
            )
        try:
            matches = search_diff_rows(source, bare_name, change_type, contains, limit)
        except (sqlite3.Error, OSError, json.JSONDecodeError) as e:
            return json.dumps(
                {
                    "error": f"Search failed: {e}",
                    "searched_complete_data": False,
                }
            )

    response["matches"] = matches
    response["returned"] = len(matches)
    return json.dumps(response)


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
        rejected = _rejected_name(code)
        if rejected is not None:
            return (
                f"Error: ValueError: {rejected} is not readable in this sandbox. "
                "The data is in diff_data; print() returns values; base64, "
                "collections, json, math and re are the available modules."
            )

        # Try to auto-return the last expression's value (like Jupyter/IPython).
        # If the last statement is an expression, compile it separately as eval
        # so its result is captured even without an explicit print().
        lines = code.rstrip().split("\n")
        raw_last_line = lines[-1]
        last_line = raw_last_line.strip()
        auto_result = None

        # Only attempt auto-return if the last line is top-level (not indented).
        # An indented last line is part of a loop/conditional and extracting it
        # would change semantics.
        if (
            last_line
            and not raw_last_line.startswith((" ", "\t"))
            and not last_line.startswith(
                (
                    "import ",
                    "from ",
                    "del ",
                    "raise ",
                    "return ",
                )
            )
        ):
            # Check if the last line is a valid expression first, separately
            # from exec/eval, so a SyntaxError during body execution doesn't
            # trigger the fallback and cause double execution.
            is_expr = True
            try:
                compile(last_line, "<last>", "eval")
            except SyntaxError:
                is_expr = False

            if is_expr:
                # Last line is a valid expression — exec the body, then eval the tail
                body = "\n".join(lines[:-1])
                if body.strip():
                    exec(body, exec_globals)  # noqa: S102
                auto_result = eval(last_line, exec_globals)  # noqa: S307
            else:
                exec(code, exec_globals)  # noqa: S102
        else:
            exec(code, exec_globals)  # noqa: S102

        output = stdout_capture.getvalue()
        if not output and auto_result is not None:
            output = repr(auto_result)
        if not output:
            output = "(no output — use print() to return values)"
        return output
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def _sandbox_modules() -> dict[str, SimpleNamespace]:
    """The stdlib surface run_python gets, as proxies.

    SimpleNamespace rather than the modules themselves: a module would expose
    ``module.__builtins__`` and with it the unrestricted builtins.
    """
    return {
        "base64": SimpleNamespace(
            b64decode=base64.b64decode,
            b64encode=base64.b64encode,
        ),
        "collections": SimpleNamespace(
            Counter=collections.Counter,
            defaultdict=collections.defaultdict,
            OrderedDict=collections.OrderedDict,
        ),
        "json": SimpleNamespace(
            dumps=json.dumps,
            loads=json.loads,
            JSONDecodeError=json.JSONDecodeError,
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
        "re": SimpleNamespace(
            search=re.search,
            match=re.match,
            findall=re.findall,
            finditer=re.finditer,
            sub=re.sub,
            split=re.split,
            compile=re.compile,
            I=re.IGNORECASE,
            IGNORECASE=re.IGNORECASE,
            M=re.MULTILINE,
            MULTILINE=re.MULTILINE,
        ),
    }


def _rejected_name(code: str) -> str | None:
    """The sandbox-refused identifier ``code`` reads, or None.

    Dunders and frame/generator/coroutine/traceback attributes both reach the
    real builtins off a live object, so both are refused before ``exec`` runs.
    """
    frame_attrs = {
        "f_back",
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_code",
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "tb_frame",
        "tb_next",
    }

    def is_dunder(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # exec raises it with its own message

    def bad_format_field(template: str) -> str | None:
        # A str.format field reads an attribute off a live object from a literal.
        specs = [template]
        while specs:
            try:
                fields = list(string.Formatter().parse(specs.pop()))
            except ValueError:
                continue
            for _, field_name, format_spec, _ in fields:
                if format_spec:
                    specs.append(format_spec)
                for part in re.split(r"[.\[\]]", field_name or ""):
                    if part and (is_dunder(part) or part in frame_attrs):
                        return part
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            continue  # a literal is data to read, never an identifier
        if isinstance(node, ast.arg):
            continue  # a dunder parameter is function-scoped, not the builtins
        # Frame attrs leak only as an attribute read, incl. a match keyword; a
        # same-named local stays usable.
        read_attrs: list[str] = []
        if isinstance(node, ast.MatchClass):
            read_attrs.extend(node.kwd_attrs)
        elif isinstance(node, ast.Attribute):
            read_attrs.append(node.attr)
            if node.attr in ("format", "format_map") and isinstance(
                node.value, ast.Constant
            ):
                template = node.value.value
                bad = bad_format_field(template) if isinstance(template, str) else None
                if bad is not None:
                    return bad
        for attr in read_attrs:
            if attr in frame_attrs:
                return attr
        # A dunder reaches the real builtins from anywhere, so refuse it in every
        # position: a bare name, an import alias, an ``except`` binding.
        for _, value in ast.iter_fields(node):
            for name in value if isinstance(value, list) else [value]:
                if isinstance(name, str) and is_dunder(name):
                    return name
    return None


def _sandbox_import(
    modules: dict[str, SimpleNamespace],
) -> Callable[..., SimpleNamespace]:
    """``__import__`` that resolves only ``modules``, so it reaches no new object."""

    def _import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> SimpleNamespace:
        module = modules.get(name) if not level else None
        if module is None:
            raise ImportError(
                f"no module named {name!r} in this sandbox. "
                f"Available: {', '.join(sorted(modules))}. "
                "Decode stored file content with "
                "base64.b64decode(row['content_base64'])."
            )
        return module

    return _import


async def handle_run_python(
    args: dict[str, Any], db_diff_result: dict[str, Any]
) -> str:
    """Handle run_python tool call. Executes code with restricted builtins in a thread with timeout."""
    code = args.get("code", "")

    # Note: db_diff_result is already deep-copied once per verifier in main.py.
    modules = _sandbox_modules()
    exec_globals: dict[str, Any] = {
        "__builtins__": {**_SAFE_BUILTINS, "__import__": _sandbox_import(modules)},
        "diff_data": db_diff_result,
        **modules,
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

    if tool_name == "search_rows":
        # Disk-backed search — run in a thread so it can't stall the event loop.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, handle_search_rows, tool_args, db_diff_result
        )
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


def build_summary(
    db_diff_result: dict[str, Any],
    capped_note: str = " (row bodies capped — use search_rows for full coverage)",
) -> str:
    """Build a compact summary of the DB diff for the initial prompt."""
    lines = ["=== DATABASE CHANGES SUMMARY ==="]

    summary = db_diff_result.get("summary", {})
    lines.append(f"Total rows added: {summary.get('total_rows_added', 0)}")
    lines.append(f"Total rows deleted: {summary.get('total_rows_deleted', 0)}")
    lines.append(f"Total rows modified: {summary.get('total_rows_modified', 0)}")

    # Say what was deliberately left out of the diff, so a criterion about a
    # file the scan excluded reads as a visible gap rather than "no changes".
    excluded = summary.get("json_files_excluded") or {}
    if excluded:
        lines.append("")
        lines.append(
            f"{len(excluded)} JSON file(s) excluded from this diff "
            "(not application state):"
        )
        for path in sorted(excluded)[:20]:
            lines.append(f"  {path}: {excluded[path]}")
        if len(excluded) > 20:
            lines.append(f"  ... and {len(excluded) - 20} more")
    # A database that failed to parse is absent from the tables below; say so,
    # or its missing changes read as "nothing happened".
    errored_dbs = summary.get("databases_errored") or []
    if errored_dbs:
        lines.append("")
        lines.append(
            f"{len(errored_dbs)} database(s) could not be diffed and are not "
            f"listed below: {errored_dbs}"
        )
    lines.append("")

    # List tables with change counts
    tables = _get_all_tables(db_diff_result)
    changed_tables = []
    errored_tables = []
    unchanged_count = 0

    for table_name in sorted(tables.keys()):
        table_diff = tables[table_name]
        if table_diff.get("error"):
            line = f"  {table_name}: {table_diff['error']}"
            schema = table_diff.get("schema_changed")
            if schema:
                ac = len(schema.get("added_columns", []))
                rc = len(schema.get("removed_columns", []))
                line += f" (schema: +{ac}/-{rc} columns recorded before failure)"
            errored_tables.append(line)
            continue
        added, deleted, modified = _change_counts(table_diff)
        schema = table_diff.get("schema_changed")
        if added or deleted or modified or schema:
            line = f"  {table_name}: +{added} added, -{deleted} deleted, ~{modified} modified"
            if schema:
                ac = len(schema.get("added_columns", []))
                rc = len(schema.get("removed_columns", []))
                line += f" (schema: +{ac}/-{rc} columns)"
            if table_diff.get("truncated"):
                line += capped_note
            changed_tables.append(line)
        else:
            unchanged_count += 1

    if changed_tables:
        lines.append(f"Tables with changes ({len(changed_tables)}):")
        lines.extend(changed_tables)

    if errored_tables:
        lines.append(f"\nTables that could not be diffed ({len(errored_tables)}):")
        lines.extend(errored_tables)

    if unchanged_count:
        lines.append(f"\n({unchanged_count} tables unchanged)")

    # Which ceiling actually bound the capped tables. Diagnostic, and it is
    # stored with the grade, so a runaway is explainable after the fact.
    reasons = summary.get("row_cap_reasons") or {}
    if reasons:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        lines.append(f"\n(row bodies capped by: {detail})")

    return "\n".join(lines)
