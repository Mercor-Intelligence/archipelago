"""SQLite FTS5 candidate indexes for Foundry-* search paths.

Foundry apps emulate real search APIs by running a cheap SQL *superset*
pre-filter and then a precise app-side matcher over the surviving candidate
rows. At multi-GB populate scale the historical pre-filters (``LIKE
'%term%'`` scans or full-table loads) are linear in the corpus. This module
supplies a sublinear superset: one trigram-tokenized FTS5 virtual table per
searchable entity, kept in sync by AFTER INSERT/UPDATE/DELETE triggers.

Why trigram: app matchers only accept a row when the (lower-cased) term is a
**literal substring** of the haystack, and a trigram FTS5 ``MATCH`` of a
quoted string is exactly case-insensitive substring containment for strings
of three or more characters. So for FTS-eligible terms the FTS hit set is a
provable superset of the precise matcher's — the same guarantee a
``LIKE '%term%'`` scan gives, at index cost instead of O(n).

Terms that are NOT FTS-eligible (see :func:`term_is_fts_eligible`) degrade
per-term to the caller's legacy path, never to a wrong answer.

Hard-won integration rules (validated in the Foundry-MS-Teams pilot):

* Resolve hits **eagerly** (:meth:`SqliteFts.match_doc_ids`) and emit literal
  ``id IN (:ids)`` — an ``IN (SELECT ... MATCH ...)`` subquery lets SQLite's
  planner drive from a broad B-tree index and probe the FTS set per row,
  which is linear in the table (observed: 94ms vs 0.3ms for a 20-id lookup
  at 200k rows).
* Run :func:`analyze` after bulk builds and at boot — without
  ``sqlite_stat1`` the planner over-trusts composite indexes.
* Check trigger presence, not just table presence: SQLite drops a table's
  triggers with the table, so any drop+recreate cycle silently severs sync.
  :meth:`SqliteFts.init_fts` rebuilds (never just re-creates triggers over
  stale content) and :meth:`SqliteFts.tables_ready` falls back until healed.

Lifecycle wiring: call :meth:`SqliteFts.init_fts` from the app's ``init_db``
(skippable via an app env flag during populate) and pass
``lambda: index.rebuild_fts(engine)`` as ``build_index_hook`` to
:func:`mcp_middleware.csv_engine.snapshot_with_populate` — the facade builds
on the runtime DB in step 3 and strips virtual tables from the canonical
copy in step 5; :meth:`SqliteFts.index_needs_build` detects the stripped
state on the next boot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

# Minimum term length the trigram tokenizer can match.
MIN_TERM_LEN = 3

# Ceiling on materialized candidate sets. Above this a term is unselective
# enough that the legacy scan path is competitive anyway — callers fall back.
DEFAULT_CANDIDATE_CAP = 20_000


@dataclass(frozen=True)
class FtsTable:
    """One FTS5 index over a source table.

    ``text_columns`` are source-table column names concatenated (space
    separated, NULL-coalesced) into the indexed haystack — rendered both as
    ``NEW.<col>``-rewritten trigger bodies and as a bulk ``INSERT..SELECT``.
    All values are code constants supplied by the app at import time, never
    request input. ``json_text`` marks haystacks built from raw serialized
    JSON columns: JSON escaping (``ensure_ascii``) breaks substring
    containment for non-ASCII terms, so those terms are not FTS-eligible
    against these tables.
    """

    name: str
    source: str
    doc_id_col: str
    text_columns: tuple[str, ...]
    json_text: bool = False

    @property
    def text_sql(self) -> str:
        return " || ' ' || ".join(f"coalesce({c}, '')" for c in self.text_columns)

    def text_sql_new(self) -> str:
        return " || ' ' || ".join(f"coalesce(NEW.{c}, '')" for c in self.text_columns)


# ---------------------------------------------------------------------------
# Term eligibility + MATCH-string construction (injection-safe)
# ---------------------------------------------------------------------------


def term_is_fts_eligible(term: str, table: FtsTable) -> bool:
    """True when ``term`` can be answered by ``table``'s trigram index.

    Conservative allow-list — anything rejected here falls back to the
    caller's existing scan path, so a ``False`` is never a correctness bug:

    * length >= 3 (trigram floor);
    * no ``"`` / ``\\`` (both are FTS query metacharacters, and both are
      JSON-escaped in serialized columns, breaking substring containment);
    * no whitespace: multi-word phrases can span field-join boundaries whose
      exact spacing differs between the FTS text and the app-side haystack;
    * against ``json_text`` tables: ASCII-only (``json.dumps`` default
      ``ensure_ascii`` turns non-ASCII into ``\\uXXXX`` escapes) and not a
      substring of ``"none"`` (app haystacks render ``None`` values as the
      string ``"None"`` while the serialized JSON column holds ``null``).
    """
    if len(term) < MIN_TERM_LEN:
        return False
    if '"' in term or "\\" in term:
        return False
    if any(ch.isspace() for ch in term):
        return False
    if table.json_text:
        if not term.isascii():
            return False
        # Case-insensitive: the trigram tokenizer folds case, so "None"/"One"
        # are as dangerous as their lower-cased forms (app renders ``None`` as
        # the string ``"None"`` while the JSON column holds ``null``).
        if term.lower() in ("non", "one", "none"):
            return False
    return True


def match_literal(term: str) -> str:
    """Render ``term`` as a single quoted FTS5 string literal.

    The term is wrapped in double quotes with internal quotes doubled, so the
    FTS5 query parser treats the whole value as one string and user input can
    never inject query syntax (AND/OR/NOT/NEAR/column filters). Eligibility
    already rejects ``"`` — the doubling is defense in depth. The result is
    always passed as a bound SQL parameter, never interpolated.
    """
    return '"' + term.replace('"', '""') + '"'


def analyze(conn: Connection) -> None:
    """Refresh (bounded) planner statistics.

    Without ``sqlite_stat1`` rows SQLite assumes multi-column equality
    indexes are highly selective and will happily drive a broad-scope query
    from one, probing a tiny ``id IN (...)`` candidate list per row — linear
    in the table. ``analysis_limit`` bounds the sampling work so this is
    safe to run on every boot and after every bulk build.
    """
    conn.execute(text("PRAGMA analysis_limit=1000"))
    conn.execute(text("ANALYZE"))


# ---------------------------------------------------------------------------
# Per-app index registry
# ---------------------------------------------------------------------------


class SqliteFts:
    """Lifecycle + candidate lookups for one app's FTS table registry."""

    def __init__(
        self,
        tables: Sequence[FtsTable],
        *,
        meta_table: str = "fts_meta",
        candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    ) -> None:
        self.tables: tuple[FtsTable, ...] = tuple(tables)
        self.meta_table = meta_table
        self.candidate_cap = candidate_cap
        self._by_name: dict[str, FtsTable] = {t.name: t for t in self.tables}
        self._fts5_probe_cache: dict[str, bool] = {}

    def spec(self, name: str) -> FtsTable:
        return self._by_name[name]

    # -- DDL -----------------------------------------------------------------

    def _vtable_ddl(self, t: FtsTable) -> str:
        return (
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {t.name} "
            f"USING fts5(doc_id UNINDEXED, text, tokenize='trigram')"
        )

    def _trigger_names(self, t: FtsTable) -> tuple[str, ...]:
        return (f"{t.name}_ai", f"{t.name}_ad", f"{t.name}_au")

    def _trigger_ddls(self, t: FtsTable) -> list[str]:
        ai = (
            f"CREATE TRIGGER IF NOT EXISTS {t.name}_ai AFTER INSERT ON {t.source} BEGIN "
            f"INSERT INTO {t.name}(doc_id, text) "
            f"VALUES (NEW.{t.doc_id_col}, {t.text_sql_new()}); "
            f"END"
        )
        ad = (
            f"CREATE TRIGGER IF NOT EXISTS {t.name}_ad AFTER DELETE ON {t.source} BEGIN "
            f"DELETE FROM {t.name} WHERE doc_id = OLD.{t.doc_id_col}; "
            f"END"
        )
        au = (
            f"CREATE TRIGGER IF NOT EXISTS {t.name}_au AFTER UPDATE ON {t.source} BEGIN "
            f"DELETE FROM {t.name} WHERE doc_id = OLD.{t.doc_id_col}; "
            f"INSERT INTO {t.name}(doc_id, text) "
            f"VALUES (NEW.{t.doc_id_col}, {t.text_sql_new()}); "
            f"END"
        )
        return [ai, ad, au]

    def _drop_triggers(self, conn: Connection, t: FtsTable) -> None:
        for name in self._trigger_names(t):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))

    @staticmethod
    def _table_exists(conn: Connection, name: str) -> bool:
        row = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"),
            {"n": name},
        ).first()
        return row is not None

    def _triggers_present(self, conn: Connection, t: FtsTable) -> bool:
        """All three sync triggers exist for ``t``.

        SQLite drops a table's triggers with the table, so any code path that
        drops + recreates a source table (``Base.metadata.drop_all`` in
        tests, schema repair helpers) silently severs FTS sync. Missing
        triggers mean the FTS content can be missing rows — the dangerous
        direction — so both the per-query readiness check and ``init_fts``
        treat it as "rebuild".
        """
        names = self._trigger_names(t)
        placeholders = ", ".join(f":n{i}" for i in range(len(names)))
        count = conn.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                f"AND name IN ({placeholders})"
            ),
            {f"n{i}": n for i, n in enumerate(names)},
        ).scalar()
        return int(count or 0) == len(names)

    # -- Availability + build-state probes ------------------------------------

    def fts5_available(self, engine: Engine) -> bool:
        """True when this SQLite build supports FTS5 with trigram."""
        key = str(engine.url)
        if key in self._fts5_probe_cache:
            return self._fts5_probe_cache[key]
        ok = False
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe "
                        "USING fts5(x, tokenize='trigram')"
                    )
                )
                conn.execute(text("DROP TABLE IF EXISTS _fts5_probe"))
                conn.commit()
            ok = True
        except OperationalError:  # pragma: no cover - old SQLite builds only
            logger.warning(
                "sqlite_fts: SQLite build lacks FTS5/trigram; search falls back to scans"
            )
        self._fts5_probe_cache[key] = ok
        return ok

    def tables_ready(self, db: Session) -> bool:
        """Cheap per-query guard: all FTS tables AND sync triggers exist.

        A single ``sqlite_master`` lookup — callers use it to pick the FTS
        candidate path vs. the legacy scan path, so a DB created outside the
        app's ``init_db`` (or with its virtual tables stripped by the
        snapshot facade) keeps working unchanged.

        Each ``IN`` list is scoped to its own object type: a regular table
        named like a trigger (``{fts}_ai``) must not backfill a missing
        trigger's count slot, or callers would take the FTS path against an
        index that no longer receives rows (silent false-negative results).
        """
        table_names = tuple(t.name for t in self.tables)
        trigger_names = tuple(n for t in self.tables for n in self._trigger_names(t))
        tph = ", ".join(f":t{i}" for i in range(len(table_names)))
        gph = ", ".join(f":g{i}" for i in range(len(trigger_names)))
        params = {f"t{i}": n for i, n in enumerate(table_names)}
        params.update({f"g{i}": n for i, n in enumerate(trigger_names)})
        row = db.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE "
                f"(type = 'table' AND name IN ({tph})) "
                f"OR (type = 'trigger' AND name IN ({gph}))"
            ),
            params,
        ).scalar()
        return int(row or 0) == len(table_names) + len(trigger_names)

    def index_needs_build(self, engine: Engine) -> bool:
        """True when any FTS table, trigger or backfill marker is missing.

        The snapshot facade strips virtual tables from the canonical DB but
        regular tables (including the marker) survive the copy — so presence
        is checked table-by-table, never via the marker alone.
        """
        with engine.connect() as conn:
            if not self._table_exists(conn, self.meta_table):
                return True
            for t in self.tables:
                if not self._table_exists(conn, t.name) or not self._triggers_present(conn, t):
                    return True
                marked = conn.execute(
                    text(f"SELECT 1 FROM {self.meta_table} WHERE table_name = :n"),
                    {"n": t.name},
                ).first()
                if marked is None:
                    return True
        return False

    # -- Build / rebuild -------------------------------------------------------

    def init_fts(self, engine: Engine) -> bool:
        """Idempotently ensure FTS tables, sync triggers and backfilled content.

        Returns True when a backfill ran. Safe to call on every boot: in the
        steady state (table + marker + all triggers intact) nothing runs.
        Anything less means the FTS content may be missing rows (e.g. a
        drop_all/create_all cycle severed the triggers) — rebuild, don't
        just patch the triggers back over stale content.
        """
        if not self.fts5_available(engine):
            return False
        built_any = False
        with engine.begin() as conn:
            conn.execute(
                text(f"CREATE TABLE IF NOT EXISTS {self.meta_table} (table_name TEXT PRIMARY KEY)")
            )
            for t in self.tables:
                marked = conn.execute(
                    text(f"SELECT 1 FROM {self.meta_table} WHERE table_name = :n"),
                    {"n": t.name},
                ).first()
                if (
                    self._table_exists(conn, t.name)
                    and marked is not None
                    and self._triggers_present(conn, t)
                ):
                    continue
                built_any = True
                self._rebuild_one(conn, t)
            if built_any:
                analyze(conn)
        if built_any:
            logger.info("sqlite_fts: backfilled search indexes")
        return built_any

    def rebuild_fts(self, engine: Engine) -> None:
        """Force a full rebuild of every FTS table (populate's build_index_hook).

        Drops triggers first so the bulk ``INSERT..SELECT`` isn't shadowed by
        per-row trigger work, then recreates them — also healing any trigger
        that references a stripped virtual table.
        """
        if not self.fts5_available(engine):
            return
        with engine.begin() as conn:
            conn.execute(
                text(f"CREATE TABLE IF NOT EXISTS {self.meta_table} (table_name TEXT PRIMARY KEY)")
            )
            for t in self.tables:
                self._rebuild_one(conn, t)
            analyze(conn)
        logger.info("sqlite_fts: rebuilt all search indexes")

    def _rebuild_one(self, conn: Connection, t: FtsTable) -> None:
        self._drop_triggers(conn, t)
        conn.execute(text(f"DROP TABLE IF EXISTS {t.name}"))
        conn.execute(text(self._vtable_ddl(t)))
        if self._table_exists(conn, t.source):
            conn.execute(
                text(
                    f"INSERT INTO {t.name}(doc_id, text) "
                    f"SELECT {t.doc_id_col}, {t.text_sql} FROM {t.source}"
                )
            )
        for ddl in self._trigger_ddls(t):
            conn.execute(text(ddl))
        conn.execute(
            text(f"INSERT OR REPLACE INTO {self.meta_table}(table_name) VALUES (:n)"),
            {"n": t.name},
        )

    # -- Candidate lookups -----------------------------------------------------

    def match_doc_ids(
        self, db: Session, fts_name: str, term: str, *, cap: int | None = None
    ) -> set[str] | None:
        """Resolve one term's FTS hits to a ``doc_id`` set (eagerly, in Python).

        Deliberately NOT an ``IN (SELECT ... MATCH ...)`` subquery: SQLite's
        planner may drive such a query from a broad B-tree index and probe
        the FTS hit set per row — linear in the table, not in the hits.
        Materializing the (capped) hit set and letting callers emit
        ``id IN (:ids)`` keeps the plan PK-driven regardless of planner
        statistics or SQLite version.

        Returns ``None`` when more than ``cap`` rows match (query LIMITed,
        never materializing the overage). ``fts_name`` must be a registry
        name; the term is rendered via :func:`match_literal` and bound as a
        parameter.
        """
        if fts_name not in self._by_name:  # defense in depth — registry names only
            raise ValueError(f"unknown FTS table {fts_name!r}")
        cap = self.candidate_cap if cap is None else cap
        rows = db.execute(
            text(f"SELECT doc_id FROM {fts_name} WHERE {fts_name} MATCH :q LIMIT :lim"),
            {"q": match_literal(term), "lim": cap + 1},
        ).all()
        if len(rows) > cap:
            return None
        return {row[0] for row in rows}

    def candidate_clause(self, db: Session, fts_name: str, id_col: Any, term: str) -> Any | None:
        """One-shot candidate predicate for simple (single-term) service searches.

        Returns ``None`` when the index can't answer this term — table not
        ready, term ineligible, unselective beyond the cap, or containing SQL
        LIKE wildcards (callers that pair this with an unescaped ``LIKE``
        keep wildcard semantics the index lacks). Callers must keep their
        existing precise filter; this only narrows.

        ``fts_name`` must be a registry name (a wiring constant, never request
        input); an unknown name raises ``ValueError`` like ``match_doc_ids``
        rather than a bare ``KeyError``.
        """
        if fts_name not in self._by_name:  # defense in depth — registry names only
            raise ValueError(f"unknown FTS table {fts_name!r}")
        if "%" in term or "_" in term:
            return None
        if not term_is_fts_eligible(term, self.spec(fts_name)):
            return None
        if not self.tables_ready(db):
            return None
        ids = self.match_doc_ids(db, fts_name, term)
        if ids is None:
            return None
        return id_col.in_(sorted(ids))
