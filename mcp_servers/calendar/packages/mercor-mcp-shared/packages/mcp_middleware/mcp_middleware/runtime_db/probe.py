"""Read-only "is this SQLite DB fully indexed?" probe.

Used by populate-time guards that decide whether a freshly uploaded
``canonical.db`` already contains every FTS shadow table the server
needs, and so the CSV import + FTS rebuild can be skipped at world boot.

The probe opens the file ``mode=ro&immutable=1`` so:

* A WAL-mode DB that shipped without a checkpoint still opens (no need
  to create ``-shm`` / ``-wal`` sidecars).
* The probe can never mutate the canonical EBS file — even an aborted
  transaction can't change page state when ``immutable=1``.

The fts_pairs argument carries the source→shadow mapping. The probe is
intentionally schema-agnostic: it does NOT import ``db.fts``. Apps build
the iterable from whatever metadata they have:

    from db.fts import TABLES  # FGW-style

    fts_pairs = ((entry.source_table, entry.name) for entry in TABLES.values())
    ok, reason = fully_indexed("/tmp/workspace.db", fts_pairs=fts_pairs)

A DB counts as fully indexed only when **every populated source table has
a non-empty shadow**. A single shadow with rows is not enough — another
populated source could still have a missing or empty index, in which case
search would silently miss it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = ["fully_indexed"]


def fully_indexed(
    db_path: str | os.PathLike[str],
    *,
    fts_pairs: Iterable[tuple[str, str]],
) -> tuple[bool, str]:
    """Probe ``db_path`` for fully-populated FTS indexes.

    Args:
        db_path: Path to a SQLite DB file.
        fts_pairs: Iterable of ``(source_table, fts_table)`` pairs. The
            DB is fully indexed iff for every pair where the source has
            ≥1 row, the FTS table exists and has ≥1 row.

    Returns:
        ``(is_fully_indexed, human_reason)``. Any open / read failure is
        reported as ``(False, "...")`` so the caller falls back to a full
        populate — never silently skips on a broken DB.
    """
    path_str = os.fspath(db_path)
    if not os.path.exists(path_str):
        return False, f"DB not found at {path_str}"

    # Materialise the iterable once — callers commonly pass a generator
    # expression, and we iterate it inside the try/finally below.
    pairs = list(fts_pairs)
    if not pairs:
        # Defensive: an empty pairs list would otherwise return
        # "no populated source tables" on every DB. Surface the mistake.
        return False, "fts_pairs is empty; nothing to probe"

    try:
        # ``file:`` URI lets us pass ``mode=ro&immutable=1`` — required to
        # open a WAL DB that shipped without a -shm sidecar without
        # creating one (which would mutate the EBS file).
        con = sqlite3.connect(f"file:{path_str}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        return False, f"could not open {path_str}: {exc}"

    populated_sources = 0
    try:
        try:
            existing = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            return False, f"{path_str} is not a readable SQLite database ({exc})"

        for source_table, fts_table in pairs:
            if source_table not in existing:
                continue
            try:
                src_has = con.execute(
                    f"SELECT 1 FROM {_quote_ident(source_table)} LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                return False, f"{source_table} unreadable ({exc})"
            if not src_has:
                continue  # legitimately empty source never needs an index
            populated_sources += 1

            if fts_table not in existing:
                return False, f"{source_table} has rows but {fts_table} is missing"
            try:
                fts_has = con.execute(f"SELECT 1 FROM {_quote_ident(fts_table)} LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                return False, f"{fts_table} is present but unreadable ({exc})"
            if not fts_has:
                return False, f"{source_table} has rows but {fts_table} is empty"
    finally:
        con.close()

    if populated_sources == 0:
        return False, f"{path_str} has no populated source tables"

    return (
        True,
        f"{path_str} fully indexed ({populated_sources} populated source "
        f"table(s), all with a non-empty FTS shadow)",
    )


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier; reject embedded double-quotes defensively.

    fts_pairs comes from app metadata (db.fts.TABLES etc.), not user
    input, so this is belt-and-braces — but raising on a suspicious
    identifier beats silently producing a malformed query.
    """
    if '"' in name:
        raise ValueError(f"refusing to quote identifier containing double-quote: {name!r}")
    return f'"{name}"'
