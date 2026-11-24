"""Read-only API over the final snapshot with database access.

Extends the base SnapshotCtx (from llm_code_verifier) with ``query_db()``,
``list_tables()``, and ``table_columns()`` methods that operate on a
pre-loaded SQLite database created by the streaming loader.

This module is loaded inside the sandboxed subprocess (uid 65534, no network,
stripped env).  The SQLite file is opened read-only via the ``file:`` URI to
prevent writes from verifier code.

Design notes:
* All reads are path-checked against ``snapshot_dir`` to block ``../`` escapes.
* When ``snapshot_dir`` is the container root (``/``), ``allowed_subdirs``
  scopes ctx to a whitelist of subtrees (e.g. ``filesystem``,
  ``.apps_data``) so verifiers can't read arbitrary host paths like
  ``/etc/passwd`` and ``list_files`` doesn't walk the entire filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SnapshotCtx:
    def __init__(
        self,
        snapshot_dir: str,
        trajectory: dict[str, Any] | None,
        db_path: str | None = None,
        allowed_subdirs: list[str] | None = None,
        baseline_db_path: str | None = None,
    ) -> None:
        self._root = Path(snapshot_dir).resolve()
        self._has_snapshot_dir = self._root.is_dir()
        self._trajectory = trajectory or {}
        self._db_path = db_path
        self._baseline_db_path = baseline_db_path
        self._db_conn: Any = None  # sqlite3.Connection, lazily opened

        # Optional scope-restriction. When set, ctx only exposes paths
        # under one of these subdirs (relative to ``snapshot_dir``). Each
        # subdir is resolved at construction time so symlink shenanigans
        # can't be used later to escape the whitelist.
        #
        # Subdirs that don't exist on disk are silently dropped — older
        # snapshots may carry only ``filesystem/`` without ``.apps_data/``
        # and we don't want construction to fail in that case.
        if allowed_subdirs is None:
            self._allowed_roots: tuple[Path, ...] | None = None
        else:
            self._allowed_roots = tuple(
                (self._root / sub).resolve()
                for sub in allowed_subdirs
                if (self._root / sub).is_dir()
            )

    # ------------------------------------------------------------------
    # Trajectory accessors (read-only views)
    # ------------------------------------------------------------------

    @property
    def trajectory(self) -> list[dict[str, Any]]:
        messages = self._trajectory.get("messages") or []
        return list(messages)

    @property
    def final_answer(self) -> str:
        value = self._trajectory.get("final_answer")
        if not isinstance(value, str):
            return ""
        return value

    @property
    def trajectory_status(self) -> str:
        return str(self._trajectory.get("status", ""))

    # ------------------------------------------------------------------
    # Database accessors (read-only SQLite)
    # ------------------------------------------------------------------

    def _get_db(self) -> Any:
        """Lazily open the SQLite database in read-only mode.

        When a baseline database was staged (``# DB_BASELINE: true``), it is
        attached read-only as the ``baseline`` schema, so queries can compare
        final state against it: ``SELECT * FROM main.t EXCEPT SELECT * FROM
        baseline.t``. URI filenames are honored in ATTACH because the main
        connection is opened with ``uri=True``.
        """
        if self._db_conn is not None:
            return self._db_conn
        if not self._db_path:
            raise RuntimeError(
                "No database available. The snapshot does not contain a SQL dump."
            )
        import sqlite3

        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        if self._baseline_db_path:
            conn.execute(
                "ATTACH DATABASE ? AS baseline",
                (f"file:{self._baseline_db_path}?mode=ro",),
            )
        self._db_conn = conn
        return self._db_conn

    def query_db(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Execute a read-only SQL query against the snapshot database.

        Args:
            sql: SQL query string (SELECT only — the connection is read-only).
            params: Positional parameters for ``?`` placeholders.

        Returns:
            List of result tuples.
        """
        return self._get_db().execute(sql, params).fetchall()

    def list_tables(self) -> list[str]:
        """List all user tables in the snapshot database, lowercased.

        Names are normalised because the raw ``sqlite_master`` set is
        mixed-case *within one database*, as a direct consequence of the
        loader cascade: ``.db``- and dump-loaded tables keep the source's case
        (an H2 dump yields ``REPORT_CARD``) while tables synthesised from CSV
        input files take the filename stem's (``Collection.csv`` ->
        ``collection``). A real snapshot returns ``["REPORT_CARD",
        "collection"]``, and nothing in a verifier's own source says which
        half it is looking at.

        That made the guard every verifier writes —
        ``if "report_card" not in ctx.list_tables()`` — a coin flip, and
        losing it reports a missing table on a trajectory that is actually
        correct, because the query it guards would have succeeded: SQLite
        resolves identifiers case-insensitively, so the Python-side check was
        the only thing that could disagree with its own SQL.

        Lowercasing here makes that idiom correct by construction rather than
        by instruction. Safe in both directions: every other ctx method
        (``query_db``, ``table_columns``, ``table_row_count``) resolves a
        lowercased name against an upper-case-stored table, and the loader
        subtracts lowercased names from its remaining set, so one table never
        loads twice under two cases.
        """
        rows = self.query_db("SELECT name FROM sqlite_master WHERE type='table'")
        # Sorted after lowercasing, not by the SQL: ``ORDER BY name`` orders on
        # the stored spelling, where ASCII puts every upper-case name ahead of
        # every lower-case one (``REPORT_CARD`` before ``collection``). Sorting
        # there would leave the normalised list in an order that looks
        # arbitrary to the reader of a details string.
        return sorted(r[0].lower() for r in rows)

    def has_table(self, table: str) -> bool:
        """True when the snapshot database holds *table*, ignoring case.

        Prefer this over ``name in ctx.list_tables()``. That form compares a
        literal frozen when the verifier was authored, from the app's declared
        schema, against a list produced at grade time, from the database. The
        two disagree on case in opposite directions per app — erpnext stores
        ``tabJournal Entry``, metabase stores ``REPORT_CARD`` — so no single
        spelling of the list serves both, and a mismatch rejects a table the
        next line then queries successfully, SQLite being case-insensitive on
        identifiers.
        """
        rows = self.query_db(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND lower(name) = lower(?) LIMIT 1",
            (table,),
        )
        return bool(rows)

    def table_columns(self, table: str) -> list[str]:
        """List column names for a table."""
        rows = self.query_db(f'PRAGMA table_info("{table}")')
        return [r[1] for r in rows]

    def table_row_count(self, table: str) -> int:
        """Return the number of rows in a table."""
        rows = self.query_db(f'SELECT COUNT(*) FROM "{table}"')
        return rows[0][0]

    # ------------------------------------------------------------------
    # Filesystem accessors (rooted at snapshot_dir, path-traversal safe)
    # ------------------------------------------------------------------

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path).read_text(encoding=encoding)

    def read_bytes(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def read_json(self, path: str) -> Any:
        return json.loads(self.read_text(path))

    def exists(self, path: str) -> bool:
        try:
            self._resolve(path)
        except PermissionError:
            return False
        return (self._root / path).exists()

    def list_files(self, glob: str = "**/*") -> list[str]:
        """Return relative paths of files matching ``glob``.

        When ``allowed_subdirs`` was set on construction, results are
        scoped to those subtrees. The pattern can be either generic
        (``"**/*"``, ``"**/*.csv"``) — applied within each whitelisted
        subtree and unioned — or prefix-qualified
        (``"filesystem/**/*.csv"``, ``".apps_data/xero/*.db"``) — the
        prefix selects exactly one subtree to search. Results are
        always paths relative to the main root, so callers see
        e.g. ``filesystem/result.txt`` and ``.apps_data/xero/data.db``.
        """
        self._require_snapshot_dir()

        searches: list[tuple[Path, str]] = []
        if self._allowed_roots is None:
            searches.append((self._root, glob))
        else:
            # ``removeprefix`` (not ``lstrip``!) — lstrip("./") would strip
            # any leading "." characters, mangling whitelist subdir names
            # that start with "." like ``.apps_data``.
            normalized = glob.removeprefix("./")
            matched_prefix = False
            for base in self._allowed_roots:
                try:
                    base_rel = base.relative_to(self._root).as_posix()
                except ValueError:
                    base_rel = ""
                if base_rel and (
                    normalized == base_rel or normalized.startswith(f"{base_rel}/")
                ):
                    remainder = normalized[len(base_rel) :].lstrip("/")
                    searches.append((base, remainder or "**/*"))
                    matched_prefix = True
                    break
            if not matched_prefix:
                for base in self._allowed_roots:
                    searches.append((base, glob))

        matches: list[str] = []
        for base, pattern in searches:
            if not base.is_dir():
                continue
            for path in base.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                    rel = resolved.relative_to(self._root)
                except (ValueError, OSError):
                    continue
                if not self._within_allowed(resolved):
                    continue
                matches.append(rel.as_posix())
        return sorted(set(matches))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _within_allowed(self, candidate: Path) -> bool:
        """Whitelist check: candidate must equal or be under an allowed root."""
        if self._allowed_roots is None:
            return True
        for base in self._allowed_roots:
            if candidate == base or base in candidate.parents:
                return True
        return False

    def _require_snapshot_dir(self) -> None:
        if not self._has_snapshot_dir:
            raise FileNotFoundError(
                f"snapshot_dir does not exist: {self._root}. "
                "Filesystem methods require a snapshot directory."
            )

    def _resolve(self, path: str) -> Path:
        self._require_snapshot_dir()
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError(f"path escapes snapshot root: {path}") from exc
        if not self._within_allowed(candidate):
            raise PermissionError(f"path outside allowed subdirs: {path}")
        return candidate
