"""CLI helper for the :func:`fully_indexed` probe.

Apps that previously shipped a ``python -m db.index_status <path>`` CLI
can replace it with a one-liner wrapper:

    # mcp_servers/myapp/scripts/index_status.py
    import sys
    from db.fts import TABLES
    from mcp_middleware.runtime_db import fully_indexed_cli

    if __name__ == "__main__":
        raise SystemExit(fully_indexed_cli(
            ((entry.source_table, entry.name) for entry in TABLES.values()),
            argv=sys.argv[1:],
        ))

Exit codes match the historical contract used by ``populate.sh``:

* ``0`` — DB is fully indexed; caller skips populate.
* ``1`` — DB is not fully indexed; caller runs full populate.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable

from .probe import fully_indexed

__all__ = ["fully_indexed_cli"]


def fully_indexed_cli(
    fts_pairs: Iterable[tuple[str, str]],
    *,
    argv: list[str] | None = None,
    default_path: str = "workspace.db",
) -> int:
    """Run the :func:`fully_indexed` probe with CLI-style exit codes.

    Args:
        fts_pairs: Same iterable as :func:`fully_indexed`. Pass a
            generator from the app's FTS metadata.
        argv: Command-line argv (without ``argv[0]``). When ``None``,
            falls back to ``sys.argv[1:]``.
        default_path: Path to probe when neither argv nor ``DATABASE_PATH``
            provides one. Matches the historical FGW default
            (``workspace.db``).

    Returns:
        0 if fully indexed (caller skips populate), 1 otherwise. The
        reason string is printed to stderr in either case — populate.sh
        consumes it for the human-readable log line.

    The DB path comes from ``argv[0]`` when given, otherwise
    ``$DATABASE_PATH``, otherwise ``default_path``. The argv path takes
    precedence so callers can pass the *canonical* path explicitly and
    not be defeated by an import-time redirect that rewrote the env var
    to point at the runtime copy.
    """
    args = argv if argv is not None else sys.argv[1:]
    db_path = args[0] if args else (os.environ.get("DATABASE_PATH") or default_path)

    ok, reason = fully_indexed(db_path, fts_pairs=fts_pairs)
    if ok:
        print(f"[guard] {reason} — skipping populate + reindex", file=sys.stderr)
        return 0
    print(f"[guard] {reason} — running full populate", file=sys.stderr)
    return 1
