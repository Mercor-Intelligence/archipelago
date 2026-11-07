"""Read-only API over the final snapshot, handed to user code as ``ctx``.

This module is loaded inside the sandboxed subprocess (uid 65534, no network,
stripped env). It never sees S3, never sees Postgres, never sees the original
zip — by the time it runs, the grader has already extracted the snapshot to
``snapshot_dir`` and serialized the trajectory to JSON.

Design notes:
* All reads are path-checked against ``snapshot_dir`` to block ``../`` escapes.
* When ``snapshot_dir`` is the container root (``/``), ``allowed_subdirs``
  scopes ctx to a whitelist of subtrees (e.g. ``filesystem``,
  ``.apps_data``) so verifiers can't read arbitrary host paths like
  ``/etc/passwd`` or ``/proc/self/environ`` and ``list_files`` doesn't
  walk the entire filesystem.
* No method writes, sockets, or spawns subprocesses.
* The class is intentionally small: every public attribute is something the
  codegen LLM is taught to use in the system prompt.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SnapshotCtx:
    def __init__(
        self,
        snapshot_dir: str,
        trajectory: dict[str, Any] | None,
        allowed_subdirs: list[str] | None = None,
    ) -> None:
        self._root = Path(snapshot_dir).resolve()
        if not self._root.is_dir():
            raise FileNotFoundError(f"snapshot_dir does not exist: {self._root}")
        self._trajectory = trajectory or {}
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
        """Canonical text of the agent's last message.

        Sourced from the FINAL_ANSWER helper (last message content) by the
        grader and serialized into trajectory.json as a plain string. NOT
        from trajectory.output, which is typed dict[str, Any] | None.
        """
        value = self._trajectory.get("final_answer")
        if not isinstance(value, str):
            return ""
        return value

    @property
    def trajectory_status(self) -> str:
        return str(self._trajectory.get("status", ""))

    # ------------------------------------------------------------------
    # Filesystem accessors (rooted at snapshot_dir, path-traversal safe)
    # ------------------------------------------------------------------

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path).read_text(encoding=encoding)

    def read_bytes(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def read_json(self, path: str) -> Any:
        return json.loads(self.read_text(path))

    def sqlite_connect(self, path: str) -> sqlite3.Connection:
        """Open a read-only SQLite connection to a snapshot file.

        Why this exists: the verifier subprocess runs with ``cwd =
        sandbox_root`` (a per-call scratch dir), but the snapshot is
        rooted at ``snapshot_dir`` (``/`` in production, a tempdir in
        test-run). ``sqlite3.connect("file:foo.db?mode=ro", uri=True)``
        resolves relative paths against cwd, not against the snapshot
        root, so the same relative path that works for ``ctx.read_*``
        would fail when handed to bare ``sqlite3.connect``.

        This helper resolves the snapshot-relative path through the
        same safety check used by every other ``ctx.*`` method, then
        opens an absolute ``file:`` URI with ``mode=ro``. Result is
        the same kind of ``sqlite3.Connection`` users would expect —
        they call ``.execute(...)``, ``.close()``, etc. as normal.

        Read-only is enforced at the SQLite level; even if the sandbox
        FS were writable (it isn't, the unprivileged uid can't), a
        verifier still couldn't mutate the snapshot.
        """
        absolute = self._resolve(path)
        return sqlite3.connect(f"file:{absolute}?mode=ro", uri=True)

    def exists(self, path: str) -> bool:
        try:
            self._resolve(path)
        except PermissionError:
            return False
        return (self._root / path).exists()

    def list_files(self, glob: str = "**/*") -> list[str]:
        """Return relative paths of files (not directories) matching ``glob``.

        Uses ``pathlib.Path.glob`` semantics. Use ``**`` for recursive matching,
        ``*`` for a single path segment. A bare pattern like ``*.csv`` only
        matches at the snapshot root; use ``**/*.csv`` to recurse.

        Patterns that escape the snapshot root (e.g., ``"../*"``) are silently
        skipped — escaping paths simply don't appear in the result, matching
        the read_* method behavior of refusing out-of-root access without
        crashing.

        When ``allowed_subdirs`` was set on construction, results are
        scoped to those subtrees. The pattern can be either generic
        (``"**/*"``, ``"**/*.csv"``) — applied within each whitelisted
        subtree and unioned — or prefix-qualified
        (``"filesystem/**/*.csv"``, ``".apps_data/xero/*.db"``) — the
        prefix selects exactly one subtree to search. Results are
        always paths relative to the main root, so callers see
        e.g. ``filesystem/result.txt`` and ``.apps_data/xero/data.db``.
        """
        # Pair each search base with the pattern to evaluate inside it.
        # When the user passes a prefix-qualified pattern
        # (``"filesystem/**/*.csv"``) we strip the prefix and search
        # only the matching subtree, so the glob does what the path
        # string implies. Otherwise every whitelisted subtree gets
        # globbed with the same pattern and results are unioned.
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
                    # Strip the prefix + trailing slash; what remains is
                    # the pattern evaluated *inside* this subtree. Empty
                    # remainder means the user asked for the subtree
                    # itself — map to ``"**/*"`` so they still get every
                    # file under it.
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
                    # resolve() collapses '..' segments — pathlib's
                    # relative_to operates on the literal path, so a glob
                    # like '../*.txt' would otherwise produce
                    # '../hidden.txt' as a "child" of the root. Resolve
                    # first, then check containment.
                    resolved = path.resolve()
                    rel = resolved.relative_to(self._root)
                except (ValueError, OSError):
                    continue
                if not self._within_allowed(resolved):
                    continue
                matches.append(rel.as_posix())
        # Multiple bases can occasionally surface the same file (e.g. if
        # an allowed subdir is a symlink into another). Dedupe + sort.
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

    def _resolve(self, path: str) -> Path:
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError(f"path escapes snapshot root: {path}") from exc
        if not self._within_allowed(candidate):
            raise PermissionError(f"path outside allowed subdirs: {path}")
        return candidate
