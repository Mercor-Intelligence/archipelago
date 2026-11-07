"""The snapshot model — a canonical, sanitized *expected artifact* (spec §7).

Unlike the legacy wire-capture, a ``parity_test`` snapshot is not raw bytes: it
is the canonical expected value after sanitize-on-capture (``ignore`` stripped,
``type_only`` reduced to a typed placeholder, ``reference`` ids tokenized — see
:mod:`sanitize`). This is what makes full-refresh churn-free (spec §7).

Snapshots are intrinsically tied to a test and resolved by ``(test id, binding,
label)`` with a positional-ordinal fallback (spec §7). Big-int fidelity is
preserved because capture/persist run in Python (arbitrary-precision ints); we
persist with ``json`` (no float coercion of ints).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Binding = Literal["rest", "mcp"]


@dataclass
class Snapshot:
    """One recorded, sanitized request/response artifact."""

    binding: Binding
    request: dict[str, Any]
    response: dict[str, Any]
    label: str | None = None
    ordinal: int | None = None
    operation_id: str | None = None
    discriminant: dict[str, Any] = field(default_factory=dict)
    request_hash: str | None = None
    source_path: Path | None = None

    @property
    def kind(self) -> Binding:  # legacy-compatible alias
        return self.binding

    @property
    def status(self) -> int | None:
        return self.response.get("status")

    @property
    def is_error(self) -> bool:
        return bool(self.response.get("isError", False))

    @property
    def body(self) -> Any:
        return self.response.get("body")

    # --- serialization -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "binding": self.binding,
            "operation_id": self.operation_id,
            "discriminant": self.discriminant,
            "label": self.label,
            "ordinal": self.ordinal,
            "request": self.request,
            "response": self.response,
        }
        # Only emit the staleness fingerprint when present, so snapshots captured
        # before this field existed round-trip byte-identically (no spurious diffs).
        # A snapshot with no fingerprint is treated as stale on the next run and
        # regenerated (which stamps one) — see ``Harness._is_stale``.
        if self.request_hash is not None:
            out["request_hash"] = self.request_hash
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source_path: Path | None = None) -> Snapshot:
        return cls(
            binding=data.get("binding", "rest"),
            request=data.get("request", {}),
            response=data.get("response", {}),
            label=data.get("label"),
            ordinal=data.get("ordinal"),
            operation_id=data.get("operation_id"),
            discriminant=data.get("discriminant", {}) or {},
            request_hash=data.get("request_hash"),
            source_path=source_path,
        )


def _slug(text: str) -> str:
    """Filesystem-safe slug; collapses everything but ``[A-Za-z0-9_.-]``."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "_"


def snapshot_reltail(test_id: str) -> Path:
    """The co-located snapshot directory for a pytest ``nodeid``, **relative to the
    directory the test file itself lives in**.

    A nodeid is ``<file-path>::<class>::<test>[params]``. Snapshots live *beside*
    the test file: each file ``test_get.py`` gets a sibling folder
    ``test_get_snapshots/`` holding one sub-folder per test. So
    ``tests/rest/test_get.py::test_leads`` →  ``test_get_snapshots/test_leads``
    (anchored at ``tests/rest/``); ``…::TestC::test_it[a-b]`` →
    ``test_get_snapshots/TestC__test_it_a-b_``.

    Only the file's **basename** matters here — the directory portion is implied by
    anchoring at the file's own directory (the store's ``root``), so this never
    reproduces the ``tests/rest/`` prefix. The ``_snapshots`` suffix is deliberate:
    it makes the whole committed snapshot tree greppable and removable in one shot
    (``rm -rf **/*_snapshots``) for a clean recapture. A nodeid with no ``::`` (an
    unusual, synthetic id) uses the file stem as the leaf so nothing lands at an
    ambiguous depth."""
    file_part, _, tail = test_id.partition("::")
    stem = file_part.rsplit("/", 1)[-1]
    if stem.endswith(".py"):
        stem = stem[:-3] or stem
    folder = f"{_slug(stem)}_snapshots"
    leaf = _slug(tail.replace("::", "__")) if tail else _slug(stem)
    return Path(folder, leaf)


class SnapshotStore:
    """Resolves and persists snapshots for one test, keyed by ``(binding, label)``
    with an ordinal fallback (spec §7). Replay reads; capture writes.

    On-disk layout — snapshots are **co-located beside the test file** (see
    :func:`snapshot_reltail`); ``root`` is the test file's own directory, and the
    coverage manifest sits alongside the snapshots in the per-test leaf::

        <test-file-dir>/<file-stem>_snapshots/<class__test>/<binding>__<label-or-ord>.json
    """

    def __init__(self, test_id: str, root: str | Path):
        self.test_id = test_id
        self.root = Path(root)

    def _dir(self) -> Path:
        return self.root / snapshot_reltail(self.test_id)

    def _read_path(self, filename: str) -> Path | None:
        """Resolve a committed artifact for reading. Returns ``None`` if absent."""
        path = self._dir() / filename
        return path if path.exists() else None

    @staticmethod
    def _key(binding: str, label: str | None, ordinal: int) -> str:
        tail = _slug(label) if label else f"ord-{ordinal}"
        return f"{binding}__{tail}"

    def _path(self, binding: str, label: str | None, ordinal: int) -> Path:
        return self._dir() / f"{self._key(binding, label, ordinal)}.json"

    def get(self, binding: str, label: str | None, ordinal: int) -> Snapshot | None:
        path = self._read_path(f"{self._key(binding, label, ordinal)}.json")
        if path is None:
            return None
        return load_snapshot(path)

    def put(self, snap: Snapshot) -> Path:
        path = self._path(snap.binding, snap.label, snap.ordinal or 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snap.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        snap.source_path = path
        return path

    # --- coverage manifest (spec §5) -----------------------------------------
    #
    # The per-test coverage manifest lives beside the snapshots at
    # ``<test-slug>/coverage.json`` — a committed artifact captured alongside the
    # bodies and diffed on replay.
    def _coverage_path(self) -> Path:
        from .coverage import MANIFEST_NAME

        return self._dir() / MANIFEST_NAME

    def put_coverage(self, cells: list[Any]) -> Path:
        path = self._coverage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"test_id": self.test_id, "cells": [c.to_dict() for c in cells]}
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def get_coverage(self) -> list[Any] | None:
        from .coverage import MANIFEST_NAME

        path = self._read_path(MANIFEST_NAME)
        if path is None:
            return None
        from .coverage import load_manifest

        return load_manifest(path)


def load_snapshot(path: str | Path) -> Snapshot:
    """Load a single snapshot file. ``json.loads`` keeps ints as Python ints, so
    19-digit Zoho ids survive round-trip (spec §7 big-int fidelity)."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return Snapshot.from_dict(data, source_path=p)
