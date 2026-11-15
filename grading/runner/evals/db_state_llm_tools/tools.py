"""Tool definitions and handlers for tool-augmented final-DB-state evaluation.

Sibling of ``db_diff_llm_tools.tools``: same judge loop, different evidence. The
diff judge explores a materialized baseline-vs-final diff, so every one of its
tools is change-shaped (``change_type`` = added/deleted/modified). This one
explores the FINAL database state directly over read-only SQLite connections, so
there is no ``change_type`` and no baseline — the criteria our judges actually
carry ("the email sent to X says Y") are retrieval questions, and the final state
answers them.

Every tool argument is untrusted LLM output:

* table names are resolved against the live schema read from ``sqlite_master``
  and re-quoted — never interpolated from the model's string;
* the ``search_rows`` needle is bound as a ``?`` parameter and matched with
  ``instr`` (literal substring) rather than ``LIKE`` (whose ``%``/``_`` the
  model could otherwise smuggle in);
* ``run_sql`` accepts a single read-only statement — Python's ``sqlite3`` only
  ever executes one statement per ``execute()``, and the connection itself is
  read-only, so DDL/DML fails at the SQLite layer as well;
* every query runs under a wall-clock deadline enforced by a progress handler,
  and every response is truncated to a fixed budget.
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Max characters per tool response to keep conversation context manageable
MAX_TOOL_RESPONSE_SIZE = 10_000

# Default / maximum pagination limits for get_rows
DEFAULT_ROW_LIMIT = 20
MAX_ROW_LIMIT = 100

# Max rows a single search_rows call may return
MAX_SEARCH_LIMIT = 50

# Max rows a single run_sql call may return
MAX_SQL_ROWS = 200

# Wall-clock budget for one SQLite query, enforced by a progress handler
QUERY_TIMEOUT_SECONDS = 30

# How many VM instructions between progress-handler deadline checks
_PROGRESS_HANDLER_INTERVAL = 10_000

# Statement shapes run_sql accepts. The read-only connection already rejects
# writes; this allowlist is the first gate so a rejected statement comes back as
# a readable tool error instead of a SQLite exception.
_ALLOWED_SQL_PREFIXES = ("select", "with", "explain", "pragma table_info")

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": (
                "List every table in the database(s) under evaluation, with its "
                "row count."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_table",
            "description": (
                "Get metadata for a table's final state: column names and total "
                "row count."
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
                "Read rows from a table's final state. Returns paginated results "
                "in the table's natural order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of rows to skip (default: 0)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum rows to return (default: {DEFAULT_ROW_LIMIT}, max: {MAX_ROW_LIMIT})",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_rows",
            "description": (
                "Search a table's COMPLETE final state for a substring "
                "(case-insensitive, matched against every column). Use this to "
                "confirm whether a value is present anywhere in a table without "
                "paging through it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to search",
                    },
                    "contains": {
                        "type": "string",
                        "description": "Substring to search for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum matches to return (default: {DEFAULT_ROW_LIMIT}, max: {MAX_SEARCH_LIMIT})",
                    },
                },
                "required": ["table_name", "contains"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run one read-only SQL statement (SELECT / WITH / EXPLAIN / "
                "PRAGMA table_info) against the final database state. Use this "
                "for joins, filters and aggregates that the other tools cannot "
                "express."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A single read-only SQL statement",
                    },
                    "database": {
                        "type": "string",
                        "description": (
                            "Which database to query. Optional when only one is "
                            "under evaluation; use the path shown in the summary."
                        ),
                    },
                },
                "required": ["query"],
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


# ---------------------------------------------------------------------------
# Query plumbing
# ---------------------------------------------------------------------------


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier. Only ever applied to a name that was read back
    out of ``sqlite_master`` / ``PRAGMA table_info``, never to raw model output."""
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def _deadline(conn: sqlite3.Connection) -> Iterator[None]:
    """Abort any query on ``conn`` that outruns ``QUERY_TIMEOUT_SECONDS``.

    A read-only connection cannot corrupt anything, but an unbounded scan over a
    multi-gigabyte application database would hold a grading worker forever.
    """
    expires_at = time.monotonic() + QUERY_TIMEOUT_SECONDS
    conn.set_progress_handler(
        lambda: time.monotonic() > expires_at, _PROGRESS_HANDLER_INTERVAL
    )
    try:
        yield
    finally:
        conn.set_progress_handler(None, 0)


def _json_default(value: Any) -> str:
    """Render values sqlite3 returns that JSON cannot (BLOBs, mostly)."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default)


def _rows_as_dicts(cursor: sqlite3.Cursor, rows: list[Any]) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description or []]
    return [dict(zip(columns, row, strict=False)) for row in rows]


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


# What a table's row count reports when the COUNT(*) behind it did not complete.
# It must not be 0: the system prompt tells the judge to fail immediately on a
# table with 0 rows, so rendering an aborted count as "empty" turns a scan that
# outran QUERY_TIMEOUT_SECONDS on a large database into a false fail on a table
# that has rows and that get_rows can still read.
UNKNOWN_ROW_COUNT = "unknown"


def _row_count(entry: dict[str, Any]) -> Any:
    count = entry.get("row_count")
    return UNKNOWN_ROW_COUNT if count is None else count


# ---------------------------------------------------------------------------
# Table resolution
# ---------------------------------------------------------------------------


def read_table_index(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Judge-facing table name -> ``{db_path, name, columns, row_count}``.

    Naming mirrors ``db_diff_llm_tools``: bare table names when a single database
    is under evaluation, ``{db_path}:{table}`` when several are, so a name
    collision across connectors stays unambiguous.
    """
    databases: dict[str, dict[str, Any]] = state.get("databases", {})
    index: dict[str, dict[str, Any]] = {}

    for db_path, db_data in databases.items():
        conn: sqlite3.Connection = db_data["connection"]
        with _deadline(conn):
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
        for name in names:
            entry: dict[str, Any] = {"db_path": db_path, "name": name}
            quoted = _quote_ident(name)
            try:
                with _deadline(conn):
                    entry["columns"] = [
                        row[1]
                        for row in conn.execute(
                            f"PRAGMA table_info({quoted})"
                        ).fetchall()
                    ]
                    entry["row_count"] = conn.execute(
                        f"SELECT COUNT(*) FROM {quoted}"  # noqa: S608 - identifier read from sqlite_master
                    ).fetchone()[0]
            except (sqlite3.Error, sqlite3.Warning) as exc:
                # A table that cannot be read is surfaced, not presented as empty.
                # COUNT(*) is a full scan and runs under the same deadline as
                # everything else, so on a big enough table it is the statement
                # that aborts — None, never 0, so no reader can mistake an
                # unfinished count for an empty table.
                entry["columns"] = entry.get("columns", [])
                entry["row_count"] = None
                entry["error"] = str(exc)
            key = name if len(databases) == 1 else f"{db_path}:{name}"
            index[key] = entry

    return index


def _resolve(
    state: dict[str, Any], table_name: str
) -> tuple[dict[str, Any], sqlite3.Connection] | None:
    entry = state.get("tables", {}).get(table_name)
    if entry is None:
        return None
    db_data = state["databases"].get(entry["db_path"])
    if db_data is None:
        return None
    return entry, db_data["connection"]


# Returned when the verifier's ``.db`` scope matched no database in the
# snapshot. That is a deliberate fail-closed state, not a lookup miss — the
# scope note in the initial prompt already explains it — so say so rather than
# inviting the judge to retry against a scope that will stay empty.
_EMPTY_SCOPE_ERROR = (
    "No database is under evaluation: the verifier's expected output database "
    "name(s) matched nothing in the final snapshot (see the scope note in the "
    "task prompt). There is nothing to query — submit a fail verdict."
)


def _empty_scope(state: dict[str, Any]) -> bool:
    return not state.get("databases")


def _not_found(state: dict[str, Any], table_name: str) -> str:
    if _empty_scope(state):
        return _dump({"error": _EMPTY_SCOPE_ERROR})
    return _dump(
        {
            "error": f"Table '{table_name}' not found",
            "available_tables": sorted(state.get("tables", {}))[:50],
        }
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def handle_list_tables(args: dict[str, Any], state: dict[str, Any]) -> str:
    if _empty_scope(state):
        return _dump({"databases": [], "tables": [], "note": _EMPTY_SCOPE_ERROR})
    tables = state.get("tables", {})
    return _dump(
        {
            "databases": sorted(state.get("databases", {})),
            "tables": [
                {
                    "table_name": key,
                    "row_count": _row_count(entry),
                    **({"error": entry["error"]} if entry.get("error") else {}),
                }
                for key, entry in sorted(tables.items())
            ],
        }
    )


def handle_inspect_table(args: dict[str, Any], state: dict[str, Any]) -> str:
    table_name = str(args.get("table_name", ""))
    resolved = _resolve(state, table_name)
    if resolved is None:
        return _not_found(state, table_name)
    entry, _ = resolved

    payload: dict[str, Any] = {
        "table_name": table_name,
        "database": entry["db_path"],
        "columns": entry.get("columns", []),
        "row_count": _row_count(entry),
    }
    if entry.get("error"):
        payload["error"] = entry["error"]
    return _dump(payload)


def handle_get_rows(args: dict[str, Any], state: dict[str, Any]) -> str:
    table_name = str(args.get("table_name", ""))
    resolved = _resolve(state, table_name)
    if resolved is None:
        return _not_found(state, table_name)
    entry, conn = resolved

    limit = _clamp(args.get("limit"), DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT)
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    quoted = _quote_ident(entry["name"])
    try:
        with _deadline(conn):
            cursor = conn.execute(
                f"SELECT * FROM {quoted} LIMIT ? OFFSET ?",  # noqa: S608 - identifier read from sqlite_master
                (limit, offset),
            )
            rows = cursor.fetchall()
    except (sqlite3.Error, sqlite3.Warning) as exc:
        return _dump({"error": f"Read failed: {exc}"})

    return _dump(
        {
            "table_name": table_name,
            "total": _row_count(entry),
            "offset": offset,
            "limit": limit,
            "returned": len(rows),
            "rows": _rows_as_dicts(cursor, rows),
            # list_tables and inspect_table already carry the indexing error;
            # without it here a page read back "total": "unknown" with nothing
            # saying why.
            **({"error": entry["error"]} if entry.get("error") else {}),
        }
    )


# SQLite's built-in ``lower()`` only folds ASCII (A-Z), so it disagrees with
# Python's Unicode-aware ``str.lower()`` on the needle: searching "Älfred" for a
# stored "älfred" folds one side and not the other and misses a row that is
# present. Registered on demand, and only for a needle that actually needs it —
# for an ASCII needle the two foldings are identical and the built-in stays.
_UNICODE_LOWER = "judge_lower"


def _unicode_lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _match_expression(column: str, unicode_fold: bool) -> str:
    """``instr(...) > 0`` over one column. Every ``?`` binds the same needle, so
    the count of placeholders matters and their order does not."""
    quoted = _quote_ident(column)
    ascii_match = f"instr(lower(CAST({quoted} AS TEXT)), ?) > 0"
    if not unicode_fold:
        return ascii_match
    # BLOBs keep the built-in: sqlite3 decodes a UDF's text argument as UTF-8, so
    # handing it arbitrary bytes raises and aborts the whole search instead of
    # simply not matching that row. CASE evaluates one branch, so the UDF is
    # never reached for a BLOB.
    return (
        f"(CASE WHEN typeof({quoted}) = 'blob' THEN {ascii_match} "
        f"ELSE instr({_UNICODE_LOWER}(CAST({quoted} AS TEXT)), ?) > 0 END)"
    )


def handle_search_rows(args: dict[str, Any], state: dict[str, Any]) -> str:
    """Literal, case-insensitive substring search over every column of a table.

    ``instr`` rather than ``LIKE``: the needle is model-authored, and ``LIKE``
    would treat any ``%``/``_`` in it as a wildcard, quietly widening the search
    the judge believes it ran.

    Case folding is Unicode-aware on both sides — see ``_match_expression``.
    """
    table_name = str(args.get("table_name", ""))
    contains = str(args.get("contains", ""))
    if not contains:
        return _dump({"error": "Missing required field: contains"})

    resolved = _resolve(state, table_name)
    if resolved is None:
        return _not_found(state, table_name)
    entry, conn = resolved

    columns = entry.get("columns") or []
    if not columns:
        return _dump(
            {
                "error": f"Table '{table_name}' exposes no readable columns",
                **({"detail": entry["error"]} if entry.get("error") else {}),
            }
        )

    limit = _clamp(args.get("limit"), DEFAULT_ROW_LIMIT, MAX_SEARCH_LIMIT)
    unicode_fold = not contains.isascii()
    predicate = " OR ".join(
        _match_expression(column, unicode_fold) for column in columns
    )
    # Counted, not scanned out of the predicate: a column name may legally
    # contain a '?', which is an identifier character there and not a parameter.
    needles = len(columns) * (2 if unicode_fold else 1)
    params = [contains.lower()] * needles + [limit]

    try:
        if unicode_fold:
            conn.create_function(_UNICODE_LOWER, 1, _unicode_lower, deterministic=True)
        with _deadline(conn):
            cursor = conn.execute(
                f"SELECT * FROM {_quote_ident(entry['name'])} WHERE {predicate} LIMIT ?",  # noqa: S608 - identifiers read from sqlite_master; needle is a bound parameter
                params,
            )
            rows = cursor.fetchall()
    except (sqlite3.Error, sqlite3.Warning) as exc:
        return _dump(
            {"error": f"Search failed: {exc}", "searched_complete_data": False}
        )

    return _dump(
        {
            "table_name": table_name,
            "contains": contains,
            # Unlike the diff judge there is no materialization cap to work
            # around: the query ran against the whole table.
            "searched_complete_data": True,
            "returned": len(rows),
            "matches": _rows_as_dicts(cursor, rows),
        }
    )


def _validate_read_only_sql(query: str) -> str | None:
    """Reason the statement is rejected, or None when it may run.

    Python's ``sqlite3`` executes at most one statement per ``execute()`` call
    and the connection is opened read-only, so this allowlist exists to turn a
    disallowed statement into a readable tool error rather than an exception.
    """
    stripped = query.strip().rstrip(";").strip()
    if not stripped:
        return "Missing required field: query"
    lowered = " ".join(stripped.lower().split())
    if not lowered.startswith(_ALLOWED_SQL_PREFIXES):
        return (
            "Only read-only statements are allowed "
            f"({', '.join(_ALLOWED_SQL_PREFIXES)}); got: {stripped.split()[0]!r}"
        )
    return None


def handle_run_sql(args: dict[str, Any], state: dict[str, Any]) -> str:
    query = str(args.get("query", ""))
    rejection = _validate_read_only_sql(query)
    if rejection:
        return _dump({"error": rejection})

    databases: dict[str, dict[str, Any]] = state.get("databases", {})
    requested = str(args.get("database") or "").strip()
    if requested:
        db_path = next(
            (path for path in databases if path.lower() == requested.lower()), None
        )
        if db_path is None:
            return _dump(
                {
                    "error": f"Database '{requested}' is not under evaluation",
                    "available_databases": sorted(databases),
                }
            )
    elif len(databases) == 1:
        db_path = next(iter(databases))
    elif not databases:
        # Zero and many are different answers: without this branch an
        # intentionally empty scope was reported as "several databases", telling
        # the judge to disambiguate between none.
        return _dump({"error": _EMPTY_SCOPE_ERROR})
    else:
        return _dump(
            {
                "error": "Several databases are under evaluation — pass 'database'",
                "available_databases": sorted(databases),
            }
        )

    conn: sqlite3.Connection = databases[db_path]["connection"]
    try:
        with _deadline(conn):
            cursor = conn.execute(query.strip().rstrip(";"))
            rows = cursor.fetchmany(MAX_SQL_ROWS)
            more = cursor.fetchone() is not None
    except (sqlite3.Error, sqlite3.Warning) as exc:
        return _dump({"error": f"Query failed: {exc}"})

    return _dump(
        {
            "database": db_path,
            "returned": len(rows),
            "truncated": more,
            "rows": _rows_as_dicts(cursor, rows),
        }
    )


def truncate_tool_response(
    response: str, max_size: int = MAX_TOOL_RESPONSE_SIZE
) -> str:
    """Truncate tool response to stay within context budget."""
    if len(response) <= max_size:
        return response
    return response[:max_size] + "\n... [truncated]"


_HANDLERS = {
    "list_tables": handle_list_tables,
    "inspect_table": handle_inspect_table,
    "get_rows": handle_get_rows,
    "search_rows": handle_search_rows,
    "run_sql": handle_run_sql,
}


async def execute_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    state: dict[str, Any],
) -> str:
    """Execute a tool call and return the result string.

    Every handler talks to SQLite on disk, so all of them run in a worker thread
    rather than stalling the event loop shared with concurrent verifiers.
    """
    handler = _HANDLERS.get(tool_name)
    if not handler:
        return _dump({"error": f"Unknown tool: {tool_name}"})

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, handler, tool_args, state)
    return truncate_tool_response(result)


def build_summary(state: dict[str, Any]) -> str:
    """Build a compact summary of the final database state for the initial prompt."""
    databases = state.get("databases", {})
    tables = state.get("tables", {})

    lines = ["=== FINAL DATABASE STATE SUMMARY ==="]
    lines.append(f"Databases under evaluation ({len(databases)}):")
    lines.extend(f"  {path}" for path in sorted(databases))
    lines.append("")

    populated: list[str] = []
    errored: list[str] = []
    empty_count = 0
    for table_name in sorted(tables):
        entry = tables[table_name]
        if entry.get("error"):
            errored.append(
                f"  {table_name}: {entry['error']} "
                "(row count unknown — the table may still have rows)"
            )
            continue
        row_count = entry.get("row_count")
        if row_count is None:
            # No error, no count: not empty, just uncounted. Say so rather than
            # letting it fall into the "N tables empty" tally.
            errored.append(f"  {table_name}: row count unknown")
            continue
        if row_count:
            populated.append(f"  {table_name}: {row_count} rows")
        else:
            empty_count += 1

    if populated:
        lines.append(f"Tables with rows ({len(populated)}):")
        lines.extend(populated)
    if errored:
        lines.append(f"\nTables that could not be fully read ({len(errored)}):")
        lines.extend(errored)
    if empty_count:
        lines.append(f"\n({empty_count} tables empty)")

    return "\n".join(lines)
