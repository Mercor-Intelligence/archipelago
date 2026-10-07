"""Sidecar (or inline) SQLite FTS5 indexes, mode-selected by detection.

:mod:`mcp_middleware.sqlite_fts` keeps its FTS5 shadow tables **inside the
main ``.db``** and syncs them with per-row triggers. That is the right model
when the same file is both written by the app and consumed downstream. But
Foundry snapshots are consumed by *judge machinery* that never searches, so
the FTS bloat is dropped-and-VACUUMed out of every delivered copy.

This module supports an alternative: the FTS5 tables live in a **separate
attached ``.db`` sidecar**. The app runs against ``main`` + the attached
sidecar (fully indexed); the delivered snapshot is just ``main`` (lean, no
drop/VACUUM — the bloat was never in it).

Dual mode, one query path
-------------------------

Everything here is **mode-selected by detecting the sidecar**:

* **Sidecar present** (attached, holds the FTS tables) → queries resolve to
  the ``fts.`` schema; maintenance is done by a single ``after_flush``
  session listener (SQLite triggers cannot cross an ATTACH boundary, so the
  trigger model of :mod:`sqlite_fts` is unavailable here).
* **Sidecar absent** → the FTS tables, if any, are inline in ``main`` and
  queries resolve to the ``main.`` schema. A legacy ``.db`` shipped *with*
  inline FTS keeps working unchanged; a lean judge copy (FTS stripped) simply
  resolves to ``None`` and the caller falls back to its scan path.

The selection is per-table and per-connection: :func:`resolve_schema` looks
for the table in the attached sidecar first, then in ``main`` — so the same
:class:`SidecarFts` object serves both a fully-indexed dev DB and a
sidecar-split production DB with no app-level flag.

What is reused vs. new
----------------------

The eligibility / MATCH-string / ANALYZE primitives are identical to the
inline module and are imported from it (:class:`~sqlite_fts.FtsTable`,
:func:`~sqlite_fts.term_is_fts_eligible`, :func:`~sqlite_fts.match_literal`,
:func:`~sqlite_fts.analyze`). What differs is only *where* the tables live
(schema-qualified DDL/queries) and *how* they are maintained (session
listener, not triggers).
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import Engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .sqlite_fts import (
    DEFAULT_CANDIDATE_CAP,
    ChildSource,
    FtsTable,
    analyze,
    match_literal,
    term_is_fts_eligible,
)

__all__ = [
    "CORPUS_FP_VERSION",
    "DEFAULT_SIDECAR_ALIAS",
    "SIDECAR_SUFFIX",
    "RankedSidecarFts",
    "SidecarFts",
    "attach_sidecar_on_connect",
    "migrate_inline_fts_to_sidecar",
    "resolve_schema",
    "sidecar_build_index_hook",
    "sidecar_mode",
    "sidecar_path_for",
]

# Attach alias for the FTS sidecar. Also the schema prefix that queries and
# DDL are qualified with when the sidecar is active.
DEFAULT_SIDECAR_ALIAS = "fts"

# Conventional sidecar filename: ``foo.db`` -> ``foo.fts.db`` next to it.
SIDECAR_SUFFIX = ".fts.db"

# Prefix of the corpus fingerprint stamped into ``fts_meta.corpus_fp``. Bump
# when the fingerprint's *shape* changes: an old-shape stamp then compares
# unequal to a new-shape recomputation, i.e. rebuilds rather than trusts.
CORPUS_FP_VERSION = "v1"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(value: str) -> str:
    """Validate a SQL identifier used for schema/alias/table interpolation.

    Every identifier that flows into an f-string here is a wiring constant
    (an FtsTable field or a configured alias), never request input. This is
    defense in depth against a typo'd constant reaching the SQL text.
    """
    if not _IDENT_RE.match(value):
        raise ValueError(f"unsafe SQL identifier {value!r}")
    return value


# ---------------------------------------------------------------------------
# Sidecar path + attach-on-connect
# ---------------------------------------------------------------------------


def sidecar_path_for(main_db: str | os.PathLike[str]) -> Path:
    """Conventional sidecar path beside the main DB (``foo.db`` -> ``foo.fts.db``)."""
    p = Path(main_db)
    return p.parent / (p.stem + SIDECAR_SUFFIX)


def attach_sidecar_on_connect(
    engine: Engine,
    sidecar_path: str | os.PathLike[str],
    *,
    alias: str = DEFAULT_SIDECAR_ALIAS,
    create_if_missing: bool = False,
) -> Any:
    """Register a ``connect`` listener that ATTACHes ``sidecar_path`` per connection.

    Detection is the whole point: with ``create_if_missing=False`` (the
    default) the listener attaches **only if the sidecar file already
    exists**. So a boot against a plain ``.db`` with no sidecar simply never
    attaches, and everything downstream resolves to inline/``main`` mode.

    Pass ``create_if_missing=True`` for the *build* path — ATTACH creates the
    file if absent, giving an empty sidecar to populate. (SQLite would
    otherwise create it implicitly; the flag makes the intent explicit and
    lets the det-only default stay side-effect free.)

    Returns the listener callable so it can be removed with
    :func:`sqlalchemy.event.remove` in tests.
    """
    alias = _ident(alias)
    sidecar_path = Path(sidecar_path)

    def _attach(dbapi_connection: Any, _record: Any) -> None:
        if not create_if_missing and not sidecar_path.exists():
            return
        cur = dbapi_connection.cursor()
        try:
            # Path is a bound parameter; alias is a validated identifier.
            cur.execute(f'ATTACH DATABASE ? AS "{alias}"', (str(sidecar_path),))
        finally:
            cur.close()

    event.listen(engine, "connect", _attach)
    return _attach


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _attached_aliases(conn: Connection | Session) -> set[str]:
    rows = conn.execute(text("PRAGMA database_list")).all()
    # rows are (seq, name, file)
    return {row[1] for row in rows}


def resolve_schema(
    conn: Connection | Session,
    table_name: str,
    *,
    alias: str = DEFAULT_SIDECAR_ALIAS,
) -> str | None:
    """Return the schema that holds ``table_name``: the sidecar, ``main``, or ``None``.

    Sidecar wins when attached and populated, so a fully-indexed DB with both
    an inline copy and a sidecar (shouldn't happen, but harmless) prefers the
    sidecar. ``None`` means the table is nowhere — the caller falls back to
    its scan path (e.g. a lean judge copy with FTS stripped).
    """
    alias = _ident(alias)
    if alias in _attached_aliases(conn):
        row = conn.execute(
            text(f"SELECT 1 FROM \"{alias}\".sqlite_master WHERE type = 'table' AND name = :n"),
            {"n": table_name},
        ).first()
        if row is not None:
            return alias
    row = conn.execute(
        text("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = :n"),
        {"n": table_name},
    ).first()
    return "main" if row is not None else None


def sidecar_mode(conn: Connection | Session, *, alias: str = DEFAULT_SIDECAR_ALIAS) -> bool:
    """True when the FTS sidecar is attached on this connection."""
    return _ident(alias) in _attached_aliases(conn)


# ---------------------------------------------------------------------------
# Inline → sidecar migration (relocate a built index, never rebuild)
# ---------------------------------------------------------------------------

# Shadow tables backing a self-contained fts5 table. Contentless / external-
# content variants omit some of these; the mover copies whichever exist.
_FTS5_SHADOW_SUFFIXES = ("_data", "_idx", "_docsize", "_content", "_config")

# Marker table the sidecar registry reads (SidecarFts default ``meta_table``).
_META_TABLE = "fts_meta"


def _sqlite_table_exists(con: sqlite3.Connection, schema: str, table: str) -> bool:
    row = con.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _sqlite_columns(con: sqlite3.Connection, schema: str, table: str) -> set[str] | None:
    """Column names of ``schema.table``; ``None`` when the table is absent."""
    rows = con.execute(f'PRAGMA "{schema}".table_info("{table}")').fetchall()
    return {r[1] for r in rows} if rows else None


def _inline_fts5_tables(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(name, create_sql)`` for every fts5 virtual table living inline in ``main``.

    fts5 shadow tables (``<name>_data`` …) are ordinary tables whose stored SQL
    is a plain ``CREATE TABLE``, so filtering on ``USING fts5`` selects only the
    virtual tables, never their shadows.
    """
    return con.execute(
        "SELECT name, sql FROM main.sqlite_master WHERE type = 'table' AND sql LIKE '%USING fts5%'"
    ).fetchall()


def _drop_dependent_triggers(con: sqlite3.Connection, name: str) -> list[str]:
    """Drop any triggers whose body references the fts5 index ``name``.

    :class:`~mcp_middleware.sqlite_fts.SqliteFts` keeps an inline index in sync
    with its source table via ``AFTER INSERT/UPDATE/DELETE`` triggers defined ON
    the source (``<name>_ai`` / ``_ad`` / ``_au``). Those triggers are
    independent ``sqlite_master`` objects — ``DROP TABLE`` on the vtable does NOT
    remove them. Left behind once the index moves to the sidecar, a post-move
    write to a source row would fire ``INSERT`` / ``DELETE`` against a vtable
    that no longer exists in ``main`` and fail. Mirrors the trigger cleanup in
    :func:`csv_engine.sync._drop_computed_on_dest`.

    Matching is on a whole-identifier reference to ``name`` in the trigger body
    (optionally double-quoted) so a sibling index whose name merely shares a
    prefix (``notes_fts`` vs ``notes_fts_archive``) is never swept up.

    Returns the names of the triggers actually dropped (empty when there were
    none), so callers can log the heal.
    """
    ref = re.compile(r'(?<![A-Za-z0-9_])"?' + re.escape(name) + r'"?(?![A-Za-z0-9_])')
    rows = con.execute(
        "SELECT name, sql FROM main.sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL"
    ).fetchall()
    dropped: list[str] = []
    for trig_name, trig_sql in rows:
        if ref.search(trig_sql):
            esc = trig_name.replace('"', '""')
            con.execute(f'DROP TRIGGER IF EXISTS main."{esc}"')
            dropped.append(trig_name)
    return dropped


def _move_one_fts5(con: sqlite3.Connection, name: str, create_sql: str, alias: str) -> None:
    """Relocate one inline fts5 index into the ``alias`` schema by copying its
    shadow tables verbatim — the built inverted index moves as bytes, with no
    re-tokenisation of the source corpus.
    """
    name = _ident(name)
    # Recreate the vtable in the sidecar from the original module + args, so the
    # exact column set / tokenizer / prefix options are preserved. Everything
    # from ``USING fts5`` onward is copied unchanged; only the (schema, name)
    # target is rewritten.
    pos = create_sql.upper().index("USING FTS5")
    module_and_args = create_sql[pos:]
    con.execute(f'DROP TABLE IF EXISTS "{alias}"."{name}"')
    con.execute(f'CREATE VIRTUAL TABLE "{alias}"."{name}" {module_and_args}')
    # Overwrite the freshly-seeded shadow rows with the source index bytes.
    for suffix in _FTS5_SHADOW_SUFFIXES:
        shadow = f"{name}{suffix}"
        if not _sqlite_table_exists(con, "main", shadow):
            continue
        con.execute(f'DELETE FROM "{alias}"."{shadow}"')
        con.execute(f'INSERT INTO "{alias}"."{shadow}" SELECT * FROM main."{shadow}"')
    # Dropping the inline vtable drops its shadow tables too — but NOT the
    # source-table sync triggers that wrote into it (they are independent
    # sqlite_master objects). Strip them so post-move writes to source rows
    # don't fire against a vtable that no longer exists in ``main``.
    _drop_dependent_triggers(con, name)
    con.execute(f'DROP TABLE main."{name}"')


def _move_marker(con: sqlite3.Connection, alias: str, moved: Sequence[str]) -> None:
    """Carry each moved table's marker row into the sidecar's ``fts_meta``.

    The corpus fingerprint (if any) is copied verbatim: the source tables it is
    derived from stay in ``main`` untouched by the move, so the stamp still
    matches and the relocated index is *trusted*, never rebuilt. A table with no
    inline marker (or a legacy marker with no ``corpus_fp``) gets a fingerprint-
    less row, i.e. presence-only trust — again no rebuild.
    """
    inline_cols = _sqlite_columns(con, "main", _META_TABLE)
    inline_has_fp = inline_cols is not None and "corpus_fp" in inline_cols
    con.execute(
        f'CREATE TABLE IF NOT EXISTS "{alias}"."{_META_TABLE}" '
        "(table_name TEXT PRIMARY KEY, corpus_fp TEXT)"
    )
    for name in moved:
        fp: str | None = None
        if inline_has_fp:
            row = con.execute(
                f"SELECT corpus_fp FROM main.{_META_TABLE} WHERE table_name = ?",
                (name,),
            ).fetchone()
            fp = row[0] if row else None
        con.execute(
            f'INSERT OR REPLACE INTO "{alias}"."{_META_TABLE}"(table_name, corpus_fp) '
            "VALUES (?, ?)",
            (name, fp),
        )
    if inline_cols is not None:
        con.execute(f"DROP TABLE main.{_META_TABLE}")


def migrate_inline_fts_to_sidecar(
    main_db: str | os.PathLike[str],
    *,
    alias: str = DEFAULT_SIDECAR_ALIAS,
    sidecar_path: str | os.PathLike[str] | None = None,
    vacuum: bool = True,
) -> list[str]:
    """Move any inline fts5 indexes out of ``main_db`` into its sidecar file.

    Generic and registry-free: the delivered ``main.db`` carries the full fts5
    definitions in its own schema, so the mover discovers them by introspection
    — no per-app configuration, column list, or ``SidecarFts`` instance needed.

    Movement, not rebuild: each fts5 index is relocated by copying its shadow
    tables verbatim into the sidecar (see :func:`_move_one_fts5`), so a multi-GB
    corpus is never re-tokenised. Source tables stay in ``main``; only the index
    moves. The marker (including any corpus fingerprint) is carried across so the
    relocated index is trusted on the next boot.

    Runs only when inline FTS is present: an already-split delivery (indexes in
    the sidecar, none inline) or an FTS-free DB returns ``[]`` and touches
    nothing. External-content fts5 (``content=`` referencing a table) is left
    inline with a warning — its content would not travel with the index.

    Returns the list of relocated table names (empty on no-op). The whole move
    is one transaction spanning ``main`` and the sidecar, so a failure rolls
    back to the original inline state.
    """
    main_db = Path(main_db)
    if not main_db.exists():
        return []
    alias = _ident(alias)
    sidecar = Path(sidecar_path) if sidecar_path is not None else sidecar_path_for(main_db)

    con = sqlite3.connect(str(main_db))
    con.isolation_level = None  # explicit BEGIN/COMMIT below; ATTACH/VACUUM need autocommit
    try:
        candidates = _inline_fts5_tables(con)
        movable: list[tuple[str, str]] = []
        for name, create_sql in candidates:
            if "content=" in create_sql.lower():
                logger.warning(
                    f"fts_sidecar: {name!r} is an external-content/contentless fts5 table; "
                    "leaving it inline (its content would not move with the index)"
                )
                continue
            movable.append((name, create_sql))
        if not movable:
            return []

        con.execute(f'ATTACH DATABASE ? AS "{alias}"', (str(sidecar),))
        moved: list[str] = []
        con.execute("BEGIN")
        try:
            for name, create_sql in movable:
                _move_one_fts5(con, name, create_sql, alias)
                moved.append(name)
            _move_marker(con, alias, moved)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute(f'DETACH DATABASE "{alias}"')

        if vacuum:
            try:
                con.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                # Space reclaim is best-effort; the move already succeeded.
                logger.warning(f"fts_sidecar: VACUUM after FTS migration skipped: {exc}")

        logger.info(
            f"fts_sidecar: moved {len(moved)} inline FTS index(es) into sidecar "
            f"{sidecar.name!r}: {', '.join(moved)}"
        )
        return moved
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Index registry (schema-qualified, trigger-free)
# ---------------------------------------------------------------------------


class SidecarFts:
    """Lifecycle + candidate lookups for a sidecar/inline FTS registry.

    Same public surface as :class:`sqlite_fts.SqliteFts` where it matters
    (:meth:`tables_ready`, :meth:`match_doc_ids`, :meth:`candidate_clause`),
    but every statement is schema-qualified to whichever schema currently
    holds the table, and there are no triggers — maintenance is via
    :meth:`install_session_maintenance`.
    """

    def __init__(
        self,
        tables: Sequence[FtsTable],
        *,
        alias: str = DEFAULT_SIDECAR_ALIAS,
        meta_table: str = "fts_meta",
        candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    ) -> None:
        self.tables: tuple[FtsTable, ...] = tuple(tables)
        self.alias = _ident(alias)
        self.meta_table = _ident(meta_table)
        self.candidate_cap = candidate_cap
        self._by_name: dict[str, FtsTable] = {t.name: t for t in self.tables}
        self._by_source: dict[str, FtsTable] = {t.source: t for t in self.tables}
        # child table name -> (parent FtsTable, mapping) for aggregated indexes.
        self._by_child: dict[str, tuple[FtsTable, ChildSource]] = {
            cs.table: (t, cs) for t in self.tables for cs in t.child_sources
        }
        # A column_exprs override must name a declared column (catch typos).
        for t in self.tables:
            known = set(t.text_columns)
            for name, _expr in t.column_exprs:
                if name not in known:
                    raise ValueError(
                        f"FtsTable {t.name!r}: column_exprs key {name!r} not in text_columns"
                    )

    def spec(self, name: str) -> FtsTable:
        return self._by_name[name]

    # -- index layout (overridden by the ranked registry) ---------------------

    def _fts_columns(self, t: FtsTable) -> tuple[str, ...]:
        """FTS5 column names for ``t``'s vtable. Unranked: one concatenated column."""
        return ("text",)

    def _select_exprs(self, t: FtsTable) -> tuple[str, ...]:
        """SQL expressions (aligned to :meth:`_fts_columns`) read from the source row."""
        return (t.text_sql,)

    def _column_ddl(self, t: FtsTable) -> str:
        return ", ".join(f'"{_ident(c)}"' for c in self._fts_columns(t))

    def _select_list(self, t: FtsTable) -> str:
        return ", ".join(self._select_exprs(t))

    # -- schema resolution ----------------------------------------------------

    def _resolve(self, conn: Connection | Session, name: str) -> str | None:
        return resolve_schema(conn, name, alias=self.alias)

    def _build_target(self, conn: Connection) -> str:
        """Where a build should write: the sidecar if attached, else ``main``."""
        return self.alias if sidecar_mode(conn, alias=self.alias) else "main"

    # -- corpus fingerprint ---------------------------------------------------

    def corpus_fingerprint(self, conn: Connection | Session, name: str) -> str:
        """Cheap fingerprint of the corpus that FTS table ``name`` indexes.

        Stamped into ``fts_meta.corpus_fp`` by :meth:`build` and re-derived by
        :meth:`needs_build`, so a sidecar paired with a different main DB than
        it was built from is caught instead of silently trusted (world rebuilt
        without regenerating the sidecar, partially-updated image, delivered
        main DB swapped beside a stale sidecar).

        Shape only: per source table the row count and max ``rowid``, plus the
        same for each child source of an aggregated index. Two indexed reads
        per table, never a content hash — unusable on the multi-GB tables this
        library targets. So it is a pairing check, not an integrity check: an
        edit that preserves both counters is not detected.
        """
        t = self.spec(name)
        terms = [self._corpus_term(conn, t.source)]
        terms += [self._corpus_term(conn, cs.table) for cs in t.child_sources]
        return f"{CORPUS_FP_VERSION}:" + ";".join(terms)

    def _corpus_term(self, conn: Connection | Session, table: str) -> str:
        """One source table's contribution. A missing table is its own term:
        :meth:`_build_one` tolerates one, so the fingerprint has to as well.
        """
        if not self._source_exists(conn, table):
            return f"{table}=absent"
        name = _ident(table)
        try:
            row = conn.execute(
                text(f'SELECT count(*), coalesce(max(rowid), 0) FROM main."{name}"')
            ).one()
        except OperationalError:
            # WITHOUT ROWID source: count alone, identically on both ends.
            row = conn.execute(text(f'SELECT count(*), 0 FROM main."{name}"')).one()
        return f"{table}={int(row[0])}/{int(row[1])}"

    # -- build / rebuild ------------------------------------------------------

    def build(self, engine: Engine, *, schema: str | None = None) -> None:
        """(Re)build every FTS table into ``schema`` (default: auto-detected).

        Auto target is the sidecar when it is attached (attach the engine with
        ``create_if_missing=True`` first), otherwise ``main`` — so the same
        call populates a sidecar in production and an inline index in a
        single-file dev DB.
        """
        with engine.begin() as conn:
            target = _ident(schema) if schema else self._build_target(conn)
            # Serve-time self-heal (sidecar mode only). A world that pre-dates
            # the sidecar model may carry legacy inline ``SqliteFts`` sync
            # triggers on its source tables (bodies ``INTO <name>``), baked into
            # the delivered DB's lineage and never reaped because the vtable had
            # already left ``main``. With the index now in the sidecar, the next
            # source-row write fires such a trigger against ``main`` — which no
            # longer holds ``<name>`` — and 500s with ``no such table:
            # main.<name>``. ``build`` runs at boot before the first write, so
            # reap them here, reusing the migrator's whole-identifier match.
            # NEVER in inline mode (``target == 'main'``): there the vtable *is*
            # in ``main`` and the trigger is the legitimate sync mechanism.
            if target != "main":
                self._drop_legacy_source_triggers(conn)
            conn.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "{target}"."{self.meta_table}" '
                    "(table_name TEXT PRIMARY KEY, corpus_fp TEXT)"
                )
            )
            self._ensure_fp_column(conn, target)
            for t in self.tables:
                self._build_one(conn, t, target)
            analyze(conn)
        logger.info(f"fts_sidecar: built {len(self.tables)} FTS table(s) into {target!r}")

    def restamp_all(self, engine: Engine) -> list[str]:
        """Stamp the current corpus fingerprint onto an already-built sidecar
        WITHOUT re-tokenizing.

        For a sidecar delivered/migrated with its FTS content intact but its
        marker left unstamped (``corpus_fp`` NULL — e.g. an inline→sidecar move
        that predates the fingerprint, or a hand-assembled image), a full
        :meth:`build` would needlessly re-tokenize the (multi-GB) source tables
        just to write a pairing stamp. This refreshes each already-marked
        table's fingerprint in place instead, so the next :meth:`needs_build`
        trusts the existing index rather than treating an unstamped (legacy)
        marker as a permanent unknown.

        Trust semantics: writing the stamp *asserts* the sidecar pairs with the
        main DB it is attached to right now — only call it when that pairing is
        known-good (a fresh populate that produced this sidecar, or an operator
        opting into trusting a migrated one). It is therefore the opt-in
        "sidecar-trust" arm of :func:`sidecar_build_index_hook`, never automatic.

        Sidecar-mode only (a no-op unless the sidecar is attached — inline
        markers are the legacy :class:`~mcp_middleware.sqlite_fts.SqliteFts`
        surface and are left untouched). Only tables that resolve to the
        sidecar *and* already carry a marker row are restamped; a
        missing/unbuilt table is skipped (restamp cannot invent an index — that
        needs :meth:`build`). Returns the FTS table names restamped.
        """
        restamped: list[str] = []
        with engine.begin() as conn:
            if not sidecar_mode(conn, alias=self.alias):
                return []
            # A marker table created before the column existed can't be stamped
            # until the column is added; do it once up front (no-op if present).
            self._ensure_fp_column(conn, self.alias)
            for t in self.tables:
                if self._resolve(conn, t.name) != self.alias:
                    continue  # not in the sidecar (missing, or still inline)
                marked, _ = self._marker(conn, self.alias, t.name)
                if not marked:
                    continue  # unbuilt — restamp can't stamp a table with no marker
                self._restamp(conn, self.alias, t.name)
                restamped.append(t.name)
        if restamped:
            logger.info(
                "fts_sidecar: restamped %d FTS marker(s) without re-tokenize: %s",
                len(restamped),
                ", ".join(restamped),
            )
        return restamped

    def drop_legacy_source_triggers(self, engine: Engine) -> list[str]:
        """Reap obsolete inline-FTS sync triggers, but only in sidecar mode.

        Explicit form of the self-heal :meth:`build` performs: for each declared
        table, drop any ``main`` trigger whose body references the FTS index name
        (the legacy ``SqliteFts`` ``<name>_ai`` / ``_ad`` / ``_au`` source
        triggers). Guarded so it is a **no-op unless the sidecar is attached** —
        in inline mode those triggers are the live sync path and must stay.
        Idempotent; returns the trigger names dropped. Lets an app heal an
        already-built world without forcing a full rebuild.
        """
        with engine.begin() as conn:
            if not sidecar_mode(conn, alias=self.alias):
                return []
            return self._drop_legacy_source_triggers(conn)

    def _drop_legacy_source_triggers(self, conn: Connection) -> list[str]:
        """Connection-scoped heal: reuse the migrator's per-name trigger reap."""
        raw = conn.connection.dbapi_connection
        dropped: list[str] = []
        for t in self.tables:
            dropped.extend(_drop_dependent_triggers(raw, t.name))
        if dropped:
            logger.info(
                "fts_sidecar: healed %d dangling inline-FTS sync trigger(s): %s",
                len(dropped),
                ", ".join(dropped),
            )
        return dropped

    def _build_one(self, conn: Connection, t: FtsTable, schema: str) -> None:
        name = _ident(t.name)
        col_ddl = self._column_ddl(t)
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{name}"'))
        conn.execute(
            text(
                f'CREATE VIRTUAL TABLE "{schema}"."{name}" '
                f"USING fts5(doc_id UNINDEXED, {col_ddl}, tokenize='trigram')"
            )
        )
        # The source table always lives in main; the FTS copy may live elsewhere.
        if self._source_exists(conn, t.source):
            conn.execute(
                text(
                    f'INSERT INTO "{schema}"."{name}"(doc_id, {col_ddl}) '
                    f'SELECT {t.doc_id_col}, {self._select_list(t)} FROM main."{_ident(t.source)}"'
                )
            )
        conn.execute(
            text(
                f'INSERT OR REPLACE INTO "{schema}"."{self.meta_table}"'
                "(table_name, corpus_fp) VALUES (:n, :fp)"
            ),
            {"n": t.name, "fp": self.corpus_fingerprint(conn, t.name)},
        )

    def _restamp(self, conn: Connection | Session, schema: str, name: str) -> None:
        """Refresh an existing marker's fingerprint after a maintenance write.

        Incremental maintenance moves the source counters the fingerprint is
        made of, so without this the next boot would read the drift as a
        mispairing and rebuild from scratch. Only touches rows that are already
        marked, and stays silent on a marker table with no ``corpus_fp``
        column: a legacy sidecar keeps its legacy semantics.
        """
        if not self._has_fp_column(conn, schema):
            return
        conn.execute(
            text(
                f'UPDATE "{_ident(schema)}"."{self.meta_table}" SET corpus_fp = :fp '
                "WHERE table_name = :n"
            ),
            {"n": name, "fp": self.corpus_fingerprint(conn, name)},
        )

    def _meta_columns(self, conn: Connection | Session, schema: str) -> set[str]:
        """Marker-table column names; empty when there is no marker table."""
        return {
            row[1]
            for row in conn.execute(
                text(f'PRAGMA "{_ident(schema)}".table_info("{self.meta_table}")')
            ).all()
        }

    def _has_fp_column(self, conn: Connection | Session, schema: str) -> bool:
        return "corpus_fp" in self._meta_columns(conn, schema)

    def _ensure_fp_column(self, conn: Connection, schema: str) -> None:
        """Add ``corpus_fp`` to a marker table created before it existed.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op against such a table, so a
        rebuild into a pre-existing sidecar — or into ``main`` beside
        :class:`~mcp_middleware.sqlite_fts.SqliteFts`'s marker table, which
        still has ``table_name`` only — would otherwise fail to stamp.
        """
        if self._has_fp_column(conn, schema):
            return
        conn.execute(
            text(f'ALTER TABLE "{_ident(schema)}"."{self.meta_table}" ADD COLUMN corpus_fp TEXT')
        )

    @staticmethod
    def _source_exists(conn: Connection | Session, source: str) -> bool:
        row = conn.execute(
            text("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = :n"),
            {"n": source},
        ).first()
        return row is not None

    # -- build-state probes ---------------------------------------------------

    def needs_build(self, engine: Engine) -> bool:
        """True when any FTS table is missing, unmarked, or built elsewhere.

        Sidecar-aware: a table found in the attached sidecar counts as built,
        exactly like an inline table counts for the single-file case.

        Presence alone does not prove the index belongs to the main DB it is
        sitting next to, so a marker carrying a ``corpus_fp`` is trusted only
        while it still matches :meth:`corpus_fingerprint` recomputed from
        ``main``; a mismatch rebuilds.

        A marker with no fingerprint (``NULL`` column, or a marker table
        predating the column) is accepted as-is. Legacy sidecars are
        indistinguishable from correctly-paired ones, so rejecting them would
        turn every boot on an older image into a full rebuild; they keep the
        old presence-only semantics and gain the check on their next rebuild.
        """
        with engine.connect() as conn:
            for t in self.tables:
                schema = self._resolve(conn, t.name)
                if schema is None:
                    return True
                marked, stamped = self._marker(conn, schema, t.name)
                if not marked:
                    return True
                if stamped is not None and stamped != self.corpus_fingerprint(conn, t.name):
                    logger.warning(
                        f"fts_sidecar: {t.name!r} in {schema!r} was built from a different "
                        f"corpus (stamped {stamped!r}); rebuilding"
                    )
                    return True
        return False

    def _marker(
        self, conn: Connection | Session, schema: str, name: str
    ) -> tuple[bool, str | None]:
        """``(marked, corpus_fp)`` for one FTS table; ``corpus_fp`` may be None."""
        if not self._has_fp_column(conn, schema):
            # No marker table, or one predating the corpus_fp column.
            return self._marked(conn, schema, name), None
        row = conn.execute(
            text(
                f'SELECT corpus_fp FROM "{_ident(schema)}"."{self.meta_table}" '
                "WHERE table_name = :n"
            ),
            {"n": name},
        ).first()
        if row is None:
            return False, None
        return True, row[0]

    def _marked(self, conn: Connection | Session, schema: str, name: str) -> bool:
        try:
            row = conn.execute(
                text(f'SELECT 1 FROM "{_ident(schema)}"."{self.meta_table}" WHERE table_name = :n'),
                {"n": name},
            ).first()
        except OperationalError:
            return False
        return row is not None

    def tables_ready(self, db: Session) -> bool:
        """Cheap per-query guard: every FTS table resolves to some schema.

        No trigger check (there are no triggers). A DB with the FTS stripped
        (lean judge copy) fails this and callers take their scan path.
        """
        return all(self._resolve(db, t.name) is not None for t in self.tables)

    # -- candidate lookups ----------------------------------------------------

    def match_doc_ids(
        self, db: Session, fts_name: str, term: str, *, cap: int | None = None
    ) -> set[str] | None:
        """Resolve one term's FTS hits to a ``doc_id`` set (eager, schema-qualified).

        Returns ``None`` when the table is unavailable (no sidecar, stripped
        inline) *or* when more than ``cap`` rows match — both mean "fall back".
        """
        if fts_name not in self._by_name:  # registry names only
            raise ValueError(f"unknown FTS table {fts_name!r}")
        schema = self._resolve(db, fts_name)
        if schema is None:
            return None
        cap = self.candidate_cap if cap is None else cap
        rows = db.execute(
            text(
                f'SELECT doc_id FROM "{schema}"."{_ident(fts_name)}" '
                f'WHERE "{_ident(fts_name)}" MATCH :q LIMIT :lim'
            ),
            {"q": match_literal(term), "lim": cap + 1},
        ).all()
        if len(rows) > cap:
            return None
        return {row[0] for row in rows}

    def candidate_clause(self, db: Session, fts_name: str, id_col: Any, term: str) -> Any | None:
        """One-shot candidate predicate; ``None`` when the index can't answer.

        Same guard order as :meth:`sqlite_fts.SqliteFts.candidate_clause`
        (wildcards, eligibility, readiness, cap) so it is a drop-in for the
        inline registry.
        """
        if fts_name not in self._by_name:  # registry names only
            raise ValueError(f"unknown FTS table {fts_name!r}")
        if "%" in term or "_" in term:
            return None
        if not term_is_fts_eligible(term, self.spec(fts_name)):
            return None
        ids = self.match_doc_ids(db, fts_name, term)
        if ids is None:
            return None
        return id_col.in_(sorted(ids))

    # -- session-driven maintenance (trigger replacement) ---------------------

    def install_session_maintenance(self, session_factory: Any) -> Any:
        """Keep the sidecar FTS in sync from a single ``after_flush`` listener.

        Registered on a ``Session`` / ``sessionmaker`` class. On each flush it
        re-indexes inserted and updated rows and de-indexes deleted rows for
        any registered source table — but **only when the sidecar is
        attached** (inline mode is left to whatever maintains ``main``, e.g.
        the trigger model, so the two never double-write).

        Aggregated indexes: for a table declaring :attr:`FtsTable.child_sources`
        (a parent doc whose text is a ``group_concat`` over child rows), a
        flushed ORM *child* row is mapped back to its parent via
        :attr:`ChildSource.parent_key` and the parent doc is re-aggregated —
        so ORM child insert / update / delete keep the parent in sync without
        per-site wiring. An in-place FK *reparent* (a child's ``parent_key``
        changed) re-aggregates **both** the old and the new parent, so the
        parent the child left does not keep its stale text.

        Limitation (documented, matches the inline module's trigger gap for
        the same reason): Core bulk ``session.execute(delete(...))`` /
        ``update(...)`` — including over a *child* table — bypass the unit of
        work and are *not* seen here. Apps with such call sites (e.g. a bulk
        ``clear_calendar``, or a bulk attendee/cell replace) must re-index
        explicitly via :meth:`reindex_doc` (one parent doc) or :meth:`reindex`
        (whole table). Returns the listener for test removal.
        """

        def _after_flush(session: Session, _flush_context: Any) -> None:
            conn = session.connection()
            if not sidecar_mode(conn, alias=self.alias):
                return
            touched: set[str] = set()
            for obj in session.deleted:
                touched |= self._maintain(session, obj, deleted=True)
            for obj in session.new:
                touched |= self._maintain(session, obj, deleted=False)
            for obj in session.dirty:
                touched |= self._maintain(session, obj, deleted=False)
            for name in touched:
                self._restamp(session, self.alias, name)

        event.listen(session_factory, "after_flush", _after_flush)
        return _after_flush

    def _maintain(self, session: Session, obj: Any, *, deleted: bool) -> set[str]:
        """Sync one flushed ORM row; returns the FTS table names it wrote to."""
        insp = sa_inspect(obj)
        source = insp.mapper.local_table.name

        # Direct source row: index/de-index this doc by its own doc_id.
        t = self._by_source.get(source)
        if t is not None:
            # Only maintain tables that actually live in the sidecar right now.
            if self._resolve(session, t.name) != self.alias:
                return set()
            doc_id = self._attr_value(insp, t.doc_id_col)
            self._apply_doc(session, t, doc_id, self.alias, deleted=deleted)
            return {t.name}

        # Aggregated child row: any mutation (incl. delete) re-aggregates the
        # parent doc, so it is always an upsert of the parent, never a parent
        # delete. The child's just-flushed state is already visible to the
        # parent's INSERT..SELECT on this same connection.
        child = self._by_child.get(source)
        if child is None:
            return set()
        parent_t, cs = child
        if self._resolve(session, parent_t.name) != self.alias:
            return set()
        # Refresh every parent this mutation touches. For an in-place FK
        # reparent (a child's parent_key changed A->B) both A and B must be
        # re-aggregated: B gains the child's text, but A still carries it until
        # refreshed. Reading only the current key would leave A's FTS row stale
        # (search hits the wrong doc). The old key is the ``deleted`` side of
        # the attribute's change history; a delete/insert has no such prior key.
        parent_ids: list[Any] = []
        current_parent = self._attr_value(insp, cs.parent_key)
        if current_parent is not None:
            parent_ids.append(current_parent)
        if not deleted:
            for prev in self._attr_prev_values(insp, cs.parent_key):
                if prev is not None and prev != current_parent:
                    parent_ids.append(prev)
        for parent_doc_id in parent_ids:
            self._apply_doc(session, parent_t, parent_doc_id, self.alias, deleted=False)
        return {parent_t.name}

    def _apply_doc(
        self, conn: Connection | Session, t: FtsTable, doc_id: Any, schema: str, *, deleted: bool
    ) -> None:
        """De-index ``doc_id`` from ``t``, then (unless ``deleted``) re-index it.

        Re-reads the freshly-flushed row from ``main`` so the indexed columns
        are built by the exact same expressions as :meth:`build` — no ORM
        attribute / column-name mismatch to reconcile.
        """
        schema = _ident(schema)
        name = _ident(t.name)
        conn.execute(
            text(f'DELETE FROM "{schema}"."{name}" WHERE doc_id = :id'),
            {"id": doc_id},
        )
        if deleted:
            return
        col_ddl = self._column_ddl(t)
        conn.execute(
            text(
                f'INSERT INTO "{schema}"."{name}"(doc_id, {col_ddl}) '
                f'SELECT {t.doc_id_col}, {self._select_list(t)} FROM main."{_ident(t.source)}" '
                f"WHERE {t.doc_id_col} = :id"
            ),
            {"id": doc_id},
        )

    # -- explicit re-index (for what the flush listener can't see) -------------

    def reindex_doc(self, db: Connection | Session, fts_name: str, doc_id: Any) -> bool:
        """Re-aggregate a single doc's FTS row from ``main`` (delete + INSERT..SELECT).

        For call sites the ``after_flush`` listener cannot see — chiefly Core
        bulk ``delete()`` / ``update()`` and bulk child-table mutations of an
        aggregated index (e.g. replacing one calendar event's attendees, or one
        spreadsheet's cells, via a bulk statement). ``doc_id`` is the *parent*
        doc id for aggregated indexes.

        Writes to whichever schema currently holds the table (sidecar or
        inline). Returns ``False`` when the table is indexed nowhere on this
        copy (a stripped judge snapshot) so callers can no-op.
        """
        if fts_name not in self._by_name:
            raise ValueError(f"unknown FTS table {fts_name!r}")
        schema = self._resolve(db, fts_name)
        if schema is None:
            return False
        self._apply_doc(db, self.spec(fts_name), doc_id, schema, deleted=False)
        self._restamp(db, schema, fts_name)
        return True

    def reindex(self, db: Connection | Session, fts_name: str) -> bool:
        """Rebuild every row of one FTS table from its source (data only, not DDL).

        For bulk-wipe / bulk-replace sites where per-doc tracking isn't
        practical (e.g. a ``clear_calendar`` that truncates the whole table):
        clears the FTS rows and repopulates from the current ``main`` state, so
        the result is always consistent with ``main`` regardless of how it was
        mutated. Returns ``False`` when the table is indexed nowhere here.
        """
        if fts_name not in self._by_name:
            raise ValueError(f"unknown FTS table {fts_name!r}")
        schema = self._resolve(db, fts_name)
        if schema is None:
            return False
        t = self.spec(fts_name)
        name = _ident(fts_name)
        schema_id = _ident(schema)
        db.execute(text(f'DELETE FROM "{schema_id}"."{name}"'))
        col_ddl = self._column_ddl(t)
        db.execute(
            text(
                f'INSERT INTO "{schema_id}"."{name}"(doc_id, {col_ddl}) '
                f'SELECT {t.doc_id_col}, {self._select_list(t)} FROM main."{_ident(t.source)}"'
            )
        )
        self._restamp(db, schema, fts_name)
        return True

    @staticmethod
    def _attr_value(insp: Any, column_name: str) -> Any:
        """Read a column's value off the ORM instance via its mapped attribute."""
        col = insp.mapper.local_table.c[column_name]
        prop = insp.mapper.get_property_by_column(col)
        return getattr(insp.object, prop.key)

    @staticmethod
    def _attr_prev_values(insp: Any, column_name: str) -> tuple[Any, ...]:
        """Prior committed value(s) of a column's mapped attribute this flush.

        Returns the ``deleted`` side of the attribute's change history — the
        value(s) replaced by this flush — or an empty tuple when the attribute
        was not modified. Used to catch an aggregated-child FK *reparent* so the
        parent it just left is re-aggregated alongside the one it joined.
        """
        col = insp.mapper.local_table.c[column_name]
        prop = insp.mapper.get_property_by_column(col)
        return tuple(insp.attrs[prop.key].history.deleted or ())


# ---------------------------------------------------------------------------
# Ranked variant (per-column bm25 weighting)
# ---------------------------------------------------------------------------


class RankedSidecarFts(SidecarFts):
    """Sidecar/inline FTS that preserves per-column ``bm25()`` ranking.

    Where :class:`SidecarFts` concatenates ``text_columns`` into one indexed
    ``text`` column (a substring-superset *prefilter*, no ranking), this
    registry indexes **each ``text_columns`` entry as its own FTS5 column** so
    ``bm25()`` can weight them per :attr:`FtsTable.weights`. It adds
    :meth:`match_ranked` (ordered ``(doc_id, rank)``) for surfaces whose result
    *order* is user-visible.

    A ranked (multi-column) vtable still answers a plain ``MATCH`` across all
    columns, so the inherited :meth:`match_doc_ids` / :meth:`candidate_clause`
    keep working unchanged — one registry over the same tables serves both the
    ranked surfaces and the unranked prefilter surfaces.

    All storage/detection/maintenance behaviour (attach, ``fts.``/``main.``
    resolution, trigger-free ``after_flush`` sync, lean-snapshot exclusion) is
    inherited from :class:`SidecarFts`.
    """

    def __init__(
        self,
        tables: Sequence[FtsTable],
        *,
        alias: str = DEFAULT_SIDECAR_ALIAS,
        meta_table: str = "fts_meta",
        candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    ) -> None:
        super().__init__(tables, alias=alias, meta_table=meta_table, candidate_cap=candidate_cap)
        for t in self.tables:
            if t.weights and len(t.weights) != len(t.text_columns):
                raise ValueError(
                    f"FtsTable {t.name!r}: weights ({len(t.weights)}) must align with "
                    f"text_columns ({len(t.text_columns)})"
                )

    # -- layout: one FTS column per source column -----------------------------

    def _fts_columns(self, t: FtsTable) -> tuple[str, ...]:
        return t.text_columns

    def _select_exprs(self, t: FtsTable) -> tuple[str, ...]:
        # Each named column is sourced by its column_exprs override when one is
        # declared (e.g. a group_concat over a child table), else coalesce(col).
        # This keeps an aggregated column a real, separately-weighted bm25
        # ranking column rather than folding it into a single concatenation.
        return tuple(t.source_expr(c) for c in t.text_columns)

    def _bm25_weights_sql(self, t: FtsTable) -> str:
        """The trailing ``, w0, w1, w2, …`` for ``bm25()`` — empty when uniform.

        ``bm25()`` weights are positional over EVERY column in declaration
        order, including the leading ``doc_id UNINDEXED``. So we prepend a
        weight for ``doc_id`` (0.0 — an unindexed column contributes nothing to
        the score, but it still consumes a positional slot) ahead of the
        per-``text_columns`` weights, or the weights would be shifted by one.
        """
        if not t.weights:
            return ""
        return ", 0.0" + "".join(f", {float(w)}" for w in t.weights)

    # -- ranked lookup --------------------------------------------------------

    def match_ranked(
        self, db: Session, fts_name: str, term: str, *, cap: int | None = None
    ) -> list[tuple[str, float]] | None:
        """Ordered ``(doc_id, bm25_rank)`` best-first, or ``None`` if unanswerable.

        ``None`` means the index cannot answer this term (ineligible per
        :func:`term_is_fts_eligible`, or the table resolves nowhere — a stripped
        judge copy) → the caller takes its scan path. A non-``None`` list is the
        definitive FTS hit set (possibly empty → a real "no match"), truncated
        to ``cap`` best rows.

        ``rank`` is SQLite's raw ``bm25()`` value (more negative = better);
        rows are pre-sorted ascending (best first). Callers keep their own
        score blending (e.g. a ``relevance_from_bm25``) on top of it.
        """
        if fts_name not in self._by_name:  # registry names only
            raise ValueError(f"unknown FTS table {fts_name!r}")
        t = self.spec(fts_name)
        if not term_is_fts_eligible(term, t):
            return None
        schema = self._resolve(db, fts_name)
        if schema is None:
            return None
        cap = self.candidate_cap if cap is None else cap
        name = _ident(fts_name)
        weights = self._bm25_weights_sql(t)
        # bm25()'s first arg is the bare FTS table name (it cannot be
        # schema-qualified); the FROM clause already scopes it to the sidecar.
        rows = db.execute(
            text(
                f'SELECT doc_id, bm25("{name}"{weights}) AS rank '
                f'FROM "{schema}"."{name}" WHERE "{name}" MATCH :q '
                "ORDER BY rank LIMIT :lim"
            ),
            {"q": match_literal(term), "lim": cap},
        ).all()
        return [(row[0], float(row[1])) for row in rows]


def sidecar_build_index_hook(
    fts: SidecarFts,
    engine: Engine,
    *,
    trust_existing: bool = False,
) -> bool:
    """Fingerprint-gated post-populate index builder for a :class:`SidecarFts`.

    The generic replacement for the per-app ``build_index_hook`` closures that
    called ``rebuild_fts`` unconditionally — which re-tokenized (and rewrote)
    even a sidecar that was already correctly paired with the delivered main
    DB. Here the (expensive) rebuild runs **only when** :meth:`SidecarFts.needs_build`
    says the index is missing, unmarked, or paired with a different corpus.

    ``trust_existing`` is the sidecar-trust knob. When a rebuild is *not*
    needed but the existing marker is unstamped (legacy ``corpus_fp`` NULL — a
    migrated Teams/Zoho sidecar), passing ``trust_existing=True`` calls
    :meth:`SidecarFts.restamp_all` to write the pairing stamp WITHOUT
    re-tokenizing, so subsequent boots get a real fingerprint check instead of
    the permanent presence-only fallback. Leave it ``False`` (default) to keep
    the conservative legacy semantics (an unstamped marker stays unstamped and
    is trusted on presence alone).

    Wire it as an app's populate ``build_index_hook`` — e.g.
    ``build_index_hook=lambda: sidecar_build_index_hook(fts, engine,
    trust_existing=True)``.

    Returns ``True`` if a full rebuild ran, ``False`` if the existing index was
    trusted (whether or not it was restamped).
    """
    if fts.needs_build(engine):
        fts.build(engine)
        return True
    if trust_existing:
        fts.restamp_all(engine)
    return False
