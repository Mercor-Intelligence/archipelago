"""Read-only API over the final snapshot, handed to user code as ``ctx``.

This module is loaded inside the sandboxed subprocess (uid 65534, no network,
stripped env). It never sees S3, never sees Postgres, never sees the original
zip — by the time it runs, the grader has already extracted the snapshot to
``snapshot_dir`` and serialized the trajectory to JSON.

Design notes:
* All reads are path-checked against ``snapshot_dir`` to block ``../`` escapes.
* No method writes, sockets, or spawns subprocesses.
* The class is intentionally small: every public attribute is something the
  codegen LLM is taught to use in the system prompt.
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
    ) -> None:
        self._root = Path(snapshot_dir).resolve()
        if not self._root.is_dir():
            raise FileNotFoundError(f"snapshot_dir does not exist: {self._root}")
        self._trajectory = trajectory or {}

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
        """
        matches: list[str] = []
        for path in self._root.glob(glob):
            if not path.is_file():
                continue
            try:
                # resolve() collapses '..' segments — pathlib's relative_to
                # operates on the literal path, so a glob like '../*.txt'
                # would otherwise produce '../hidden.txt' as a "child" of
                # the root. Resolve first, then check containment.
                resolved = path.resolve()
                rel = resolved.relative_to(self._root)
            except (ValueError, OSError):
                continue
            matches.append(rel.as_posix())
        return sorted(matches)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError(f"path escapes snapshot root: {path}") from exc
        return candidate
