"""Path computation and provenance markers for the runtime DB.

The runtime DB lives at a hashed ``/tmp`` path derived from the canonical
EBS / NFS path. We pair it with a ``.srcmeta`` marker file that records
the canonical's size + ``mtime_ns`` at the moment of the last copy, so a
later refresh can decide "same canonical (no-op)" from "fresh upload
(must refresh)" *without* trusting the runtime DB's mtime — the server
mutates the runtime in place (WAL checkpoints, tool writes) and its mtime
races ahead of an uploaded canonical.

The hash-of-canonical scheme means every distinct canonical path maps to
a distinct runtime path. A re-deploy that moves the canonical to a new
directory produces a fresh runtime, so the previous container's tmpfs
detritus can't accidentally satisfy a probe for the new canonical.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "RuntimePaths",
    "fingerprint_canonical",
    "read_marker",
    "runtime_paths_for",
    "write_marker",
]


# ``/tmp`` on Linux, but allow override for tests + non-Linux. ``TMPDIR`` is
# the POSIX standard; ``tempfile.gettempdir()`` honours it.
def _tmp_root() -> Path:
    return Path(tempfile.gettempdir())


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved set of paths for a single canonical DB.

    Attributes:
        canonical: The original (slow-storage) DB path, as the caller passed it.
        runtime: The hashed ``<tmp>/`` runtime copy.
        marker: The ``<runtime>.srcmeta`` provenance file.
        wal: ``<runtime>-wal`` sidecar (may not exist).
        shm: ``<runtime>-shm`` sidecar (may not exist).
    """

    canonical: Path
    runtime: Path
    marker: Path
    wal: Path
    shm: Path


def runtime_paths_for(canonical: str | os.PathLike[str]) -> RuntimePaths:
    """Compute the runtime-DB paths derived from ``canonical``.

    The runtime filename embeds the canonical's stem (for readability when
    grepping ``/tmp``) and an 8-char md5 of the resolved canonical path
    (for uniqueness across distinct canonicals on the same host).

    Pure function: never touches the filesystem beyond ``Path.resolve``
    (which itself doesn't require the file to exist on Python 3.12+ —
    ``strict=False`` is the default).
    """
    canonical_path = Path(os.fspath(canonical)).resolve()
    h = hashlib.md5(str(canonical_path).encode()).hexdigest()[:8]
    stem = canonical_path.stem or "db"
    runtime = _tmp_root() / f"{stem}_runtime_{h}.db"
    return RuntimePaths(
        canonical=canonical_path,
        runtime=runtime,
        marker=runtime.with_name(runtime.name + ".srcmeta"),
        wal=runtime.with_name(runtime.name + "-wal"),
        shm=runtime.with_name(runtime.name + "-shm"),
    )


def fingerprint_canonical(canonical: str | os.PathLike[str]) -> str:
    """Return ``"<size>:<mtime_ns>"`` for ``canonical``.

    The fingerprint is the entire identity check we keep in ``.srcmeta``:
    same size + same mtime_ns ⇒ same canonical bytes ⇒ runtime is in sync.
    We deliberately do NOT hash the contents: 100+ MB hashes per cold
    boot would dominate startup; size + mtime_ns is what every other
    sync tool (rsync, tar --listed-incremental, ZFS send) trusts.

    Raises ``OSError`` if ``canonical`` doesn't exist — callers must
    decide whether that's "no canonical yet" or fatal.
    """
    st = os.stat(os.fspath(canonical))
    return f"{st.st_size}:{st.st_mtime_ns}"


def read_marker(marker: str | os.PathLike[str]) -> str:
    """Return the marker contents, or empty string if missing / unreadable.

    Empty string is a valid "no recorded fingerprint" signal — callers
    treat it as "definitely needs refresh", which is the correct
    fail-safe (worst case: an unnecessary copy).
    """
    try:
        return Path(os.fspath(marker)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_marker(marker: str | os.PathLike[str], canonical: str | os.PathLike[str]) -> None:
    """Stamp ``marker`` with the canonical's current fingerprint.

    Best-effort writes: a marker-write failure is logged by the caller
    but never aborts the copy itself, because the caller has already
    finished the cp — at worst the next refresh will compare against an
    empty marker and copy again unnecessarily.
    """
    fp = fingerprint_canonical(canonical)
    Path(os.fspath(marker)).write_text(fp, encoding="utf-8")
