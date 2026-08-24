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
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "RuntimePaths",
    "ensure_runtime_dir",
    "fingerprint_canonical",
    "read_marker",
    "runtime_paths_for",
    "secure_file",
    "write_marker",
]

# Runtime DBs and their sidecars must be readable only by the user that owns
# the server process. Agents running under *other* OS users were finding and
# reading these files in the shared temp root; nesting them in a per-uid
# ``0o700`` directory (below) and chmod'ing each file ``0o600`` closes that off.
RUNTIME_DIR_MODE = 0o700
RUNTIME_FILE_MODE = 0o600


# ``/tmp`` on Linux, but allow override for tests + non-Linux. ``TMPDIR`` is
# the POSIX standard; ``tempfile.gettempdir()`` honours it.
def _tmp_root() -> Path:
    return Path(tempfile.gettempdir())


def _runtime_dir() -> Path:
    """Private, per-user directory under the temp root for runtime DBs.

    Runtime DBs used to live directly in the shared temp root (``/tmp``),
    where any process — including agents running under *other* OS users —
    could list and read them. We now nest them in a per-uid subdirectory
    created with mode ``0o700`` (see :func:`ensure_runtime_dir`), so other
    users cannot even traverse into it. That single directory permission
    protects the DB plus every ``-wal`` / ``-shm`` / ``.srcmeta`` sidecar
    and any lazily-created blank-world DB in one stroke.

    The uid is embedded in the directory name so distinct users on a shared
    host get distinct, separately-owned directories and never collide on one
    another owns with the wrong permissions.
    """
    try:
        suffix = str(os.getuid())  # POSIX
    except AttributeError:  # pragma: no cover - non-POSIX (e.g. Windows)
        import getpass

        suffix = getpass.getuser() or "shared"
    return _tmp_root() / f"mcp-runtime-{suffix}"


def ensure_runtime_dir() -> Path:
    """Create the private runtime directory (mode ``0o700``) and return it.

    Best-effort and idempotent: creates the directory when absent and
    re-asserts ``0o700`` when it already exists with looser permissions
    (a stale dir from before this hardening, or one widened by a permissive
    umask). Never raises — a failure here must not abort server startup. The
    downstream copy step falls back to the canonical DB if the runtime dir is
    unusable, and the per-file ``0o600`` chmod plus the process umask remain
    as additional layers.
    """
    d = _runtime_dir()
    try:
        # ``mode`` is masked by the umask on creation and ignored entirely when
        # the directory already exists, so chmod unconditionally afterwards to
        # guarantee 0o700 regardless of umask or a pre-existing looser mode.
        d.mkdir(mode=RUNTIME_DIR_MODE, parents=True, exist_ok=True)
        os.chmod(d, RUNTIME_DIR_MODE)
        if hasattr(os, "getuid"):
            st = os.stat(d)
            if st.st_uid != os.getuid():
                logger.warning(
                    "runtime dir %s is owned by uid %d, not the current user "
                    "%d; runtime DB isolation may be ineffective",
                    d,
                    st.st_uid,
                    os.getuid(),
                )
    except OSError as exc:
        logger.warning("could not secure runtime dir %s: %s", d, exc)
    return d


def secure_file(path: str | os.PathLike[str]) -> None:
    """Best-effort ``chmod 0o600`` on ``path`` (owner read/write only).

    Silently ignores a missing file or a chmod failure: this is a
    defense-in-depth layer on top of the ``0o700`` runtime directory, not a
    hard guarantee, and must never break a copy/seed that otherwise succeeded.
    """
    try:
        os.chmod(path, RUNTIME_FILE_MODE)
    except OSError:
        pass


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
    grepping the runtime dir) and an 8-char md5 of the resolved canonical path
    (for uniqueness across distinct canonicals on the same host). The file
    lives in the private per-uid runtime directory (see :func:`_runtime_dir`),
    not the shared temp root.

    Pure function: never touches the filesystem beyond ``Path.resolve`` (which
    itself doesn't require the file to exist on Python 3.12+ — ``strict=False``
    is the default). Callers that intend to *write* the runtime path must first
    materialise its parent via :func:`ensure_runtime_dir`.
    """
    canonical_path = Path(os.fspath(canonical)).resolve()
    h = hashlib.md5(str(canonical_path).encode()).hexdigest()[:8]
    stem = canonical_path.stem or "db"
    runtime = _runtime_dir() / f"{stem}_runtime_{h}.db"
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
    # The marker records canonical size/mtime — low-sensitivity, but keep it
    # owner-only for consistency with the DB it sits beside.
    secure_file(marker)
