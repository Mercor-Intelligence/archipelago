"""Cold-start seed for the SQLite runtime DB.

:func:`cold_seed_runtime` is called by the server itself, exactly once at
import time, before the engine is opened. It is idempotent (a no-op when the
runtime DB already exists), so it is safe to call from any process that imports
the DB module — but the *intent* is server-process cold start.

A second process that copies over a runtime DB the live server still has open
would (a) overwrite un-checkpointed WAL frames the agent committed but the
server hasn't folded back yet, and (b) poison the server's connection pool —
every pooled fd holds a memory map into the now-stale ``-shm`` sidecar; the next
operation raises ``SQLITE_IOERR`` and sticks until the server restarts. The
"runtime is absent" idempotency guard is what keeps cold seed safe.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.request import pathname2url

from .paths import (
    RuntimePaths,
    ensure_runtime_dir,
    runtime_paths_for,
    secure_file,
    write_marker,
)

logger = logging.getLogger(__name__)


def _unlink_quietly(path: Path) -> None:
    """Best-effort unlink: swallow ``FileNotFoundError``, warn on other OSError."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("runtime copy: could not unlink %s: %s", path, exc)


def _sqlite_backup(src: Path, dst: Path) -> None:
    """WAL-aware SQLite copy ``src`` → ``dst`` — bytes only, no perm change.

    Uses :meth:`sqlite3.Connection.backup` rather than ``shutil.copy2``
    because the source's main ``.db`` file alone does NOT contain every
    committed page — frames committed under WAL mode that the writer
    hasn't yet checkpointed live in the ``-wal`` sidecar. ``shutil.copy2``
    of the main file would silently omit those frames and produce a
    stale destination. The online-backup API reads a consistent snapshot
    that includes every committed page regardless of checkpoint state.

    The destination is self-contained: no ``-wal`` / ``-shm`` sidecars
    are produced (backup writes pages directly into the destination
    main file). This function deliberately does NOT touch the
    destination's permissions — see :func:`_copy_db_with_wal_fold` (which
    locks the runtime copy to ``0o600``).

    Raises ``sqlite3.DatabaseError`` if ``src`` isn't a readable SQLite
    DB — surfacing the misconfiguration loudly is better than
    ``shutil.copy2``'s format-agnostic silent corruption.
    """
    # timeout=60 so a busy writer in the source doesn't make the backup
    # fail with SQLITE_BUSY immediately.
    src_conn = sqlite3.connect(str(src), timeout=60)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn, pages=-1)
    finally:
        # Order: close source first so its locks release before the
        # destination close finalises.
        src_conn.close()
        dst_conn.close()


def _copy_db_with_wal_fold(
    src: Path,
    dst: Path,
    *,
    post_copy_hook: Callable[[Path], None] | None = None,
) -> None:
    """Atomic WAL-aware copy that locks the destination to the owner (``0o600``).

    Used for the runtime cold-seed placement whose destination is a private
    per-uid runtime file: restrict it to the owner. The enclosing runtime dir is
    already ``0o700``, so this is defense-in-depth.

    **Atomicity.** The backup is written into a **private temp file in the
    destination's directory** and swapped into place with a single
    :func:`os.replace`. Readers therefore observe either the old, complete
    inode or the new, complete inode — **never a partial main file** (the
    torn-read window the old "unlink dst then back up into it" approach left,
    during which a concurrent connection could open a half-written or empty
    runtime). The temp is a FRESH inode owned by THIS process, so the swap also
    sidesteps the cross-uid read-only residual-state trap that backing up into
    a reused inode can cause (a prior — possibly cross-uid — WAL connection can
    leave ``-wal`` / ``-shm`` ownership + advisory-lock state that SQLite
    honours by serving the reopened file **read-only**; see
    :func:`~mcp_middleware.runtime_db.unlink_stale_runtime`). The old
    destination's stale ``-wal`` / ``-shm`` are dropped immediately before the
    swap so a new WAL-mode reader can't pair the freshly-swapped main file with
    a mismatched sidecar. ``os.replace`` keeps any fds an in-flight handler
    still holds pointed at the now-orphan old inode (POSIX) while new
    connections see the new file.

    **Failure is clean by construction.** If the backup (or the hook) fails the
    temp is removed and ``dst`` is left EXACTLY as it was — the previous runtime
    on the refresh path (so the server keeps serving it, stale, until the next
    retry), or ABSENT on the cold-seed path (so ``cold_seed_runtime`` re-copies
    and bind falls back to the canonical). No partial ever masquerades as a
    present, writable runtime.

    Args:
        src: Source DB (canonical / delivered file).
        dst: Destination runtime path.
        post_copy_hook: Optional callable invoked with the **temp** path after
            the backup + perm lock but BEFORE the atomic swap, so any app
            runtime-only columns (idempotent DDL — e.g. a ``default_users``
            column the app's boot migration adds but that is absent from the
            canonical) are present the instant ``dst`` points at the new inode.
            It runs with no concurrency (the temp is private and unpublished).
            The hook must leave the temp self-contained (commit + close its
            connections); any ``-wal`` / ``-shm`` it produces are dropped before
            the swap. A raising hook aborts the fold (nothing is swapped in) and
            the exception propagates to the caller.
    """
    dst_dir = dst.parent
    ensure_runtime_dir()
    # Temp in the SAME directory → the os.replace below is a same-filesystem
    # atomic rename (never a cross-device copy).
    tmp_fd, tmp_name = tempfile.mkstemp(dir=dst_dir, prefix=dst.name + ".", suffix=".tmp")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    tmp_sidecars = (
        tmp_path.with_name(tmp_path.name + "-wal"),
        tmp_path.with_name(tmp_path.name + "-shm"),
    )
    dst_sidecars = (dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm"))
    swapped = False
    try:
        # mkstemp made an empty file; sqlite3.connect treats it as a new empty
        # DB and the backup writes every committed page into it.
        _sqlite_backup(src, tmp_path)
        secure_file(tmp_path)
        if post_copy_hook is not None:
            post_copy_hook(tmp_path)
        # Any journal sidecars the hook's DDL produced must not ride along.
        for s in tmp_sidecars:
            _unlink_quietly(s)
        # Clear the OLD destination's stale sidecars right before the swap so a
        # new WAL-mode reader can't pair the new main file with a mismatched -wal.
        for stale in dst_sidecars:
            _unlink_quietly(stale)
        os.replace(tmp_path, dst)
        swapped = True
    finally:
        if not swapped:
            for leftover in (tmp_path, *tmp_sidecars):
                _unlink_quietly(leftover)


def _operational_error_is_writable(message: str) -> bool:
    """Classify a :class:`sqlite3.OperationalError` message from the write probe.

    * A genuine read-only bind ("attempt to write a readonly database") →
      ``False``: the DB cannot be written.
    * Contention ("database is locked" / "database is busy") → ``True``: another
      writer holds the DB, so it IS writable — reporting False here would let
      the self-repair path clobber a live writer's runtime.
    * Anything else (disk I/O error, malformed image, …) → ``False``: the DB is
      not safely usable, so fail closed.
    """
    msg = message.lower()
    if "readonly" in msg or "read-only" in msg:
        return False
    if "locked" in msg or "busy" in msg:
        return True
    return False


def _assert_runtime_writable(path: Path) -> bool:
    """Return True iff ``path`` can be opened for writing by THIS process.

    Probes the exact failure the self-heal refresh must not paper over: a
    cross-uid handoff (populate rewrote the canonical ``0o644`` under a
    different uid, or the runtime inode carries residual read-only state
    from a prior open) can leave SQLite serving the file **read-only** even
    though every byte is present — the next real write then raises
    ``sqlite3.OperationalError: attempt to write a readonly database`` deep
    inside a request handler (an audit ``INSERT`` / ``commit``), 500-looping
    every readiness probe instead of failing fast.

    The probe must force an actual page **write**, not merely a lock:
    ``BEGIN IMMEDIATE`` alone acquires SQLite's RESERVED lock via ``fcntl``
    (an advisory lock that needs no write permission) and returns cleanly even
    on a connection SQLite opened read-only — it does NOT raise
    ``SQLITE_READONLY``. The cheapest probe that does exercise the write path is
    a header write: inside the transaction we bump ``PRAGMA user_version`` (it
    dirties page 1) and then ``rollback`` so the value is restored — no durable
    change on a writable DB, but an immediate ``SQLITE_READONLY`` on a
    read-only one.

    Contention is NOT read-only. A ``BEGIN IMMEDIATE`` that loses the race for
    the RESERVED lock raises ``SQLITE_BUSY`` — that means *another writer holds
    the DB*, i.e. the file is writable, just contended. Reporting False there
    would let the self-repair path clobber a live writer's runtime, so we treat
    ``BUSY`` / ``LOCKED`` as writable. Only a genuine ``readonly`` error (or a
    corrupt / unopenable file) counts as not-writable, which is the direction
    that fails closed.

    The connection MUST open in ``mode=rw`` (never ``mode=rwc``): a plain
    ``sqlite3.connect(path)`` *creates* a missing file as an empty DB, so a
    probe of an absent runtime — e.g. after a re-seed unlinked the old file and
    the copy then failed — would fabricate an empty writable DB, pass, and let
    the caller bind an empty database instead of failing loud. ``mode=rw`` opens
    an existing file read-write and raises ``unable to open database file`` when
    it is missing, so an absent runtime correctly reads as not-writable.
    """
    conn: sqlite3.Connection | None = None
    try:
        # file: URI with mode=rw → open existing read-write, never create.
        uri = f"file:{pathname2url(str(path))}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        # Write the header page (same net value after rollback) — this is what
        # actually surfaces "attempt to write a readonly database".
        conn.execute(f"PRAGMA user_version = {int(current) + 1}")
        conn.rollback()
        return True
    except sqlite3.OperationalError as exc:
        # Contention (BUSY/LOCKED) means a writer is active → the DB IS
        # writable. Only a real read-only bind fails the probe.
        return _operational_error_is_writable(str(exc))
    except sqlite3.Error:
        # Corrupt / unopenable → treat as not writable (fail closed).
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


__all__ = [
    "RuntimeDbReadonlyError",
    "cold_seed_runtime",
]


class RuntimeDbReadonlyError(RuntimeError):
    """Raised by the ``bind_engine`` runtime-mode preflight when the bound
    runtime is not writable by the serving process even after a re-seed.

    In runtime mode a non-writable runtime is always a bug: the server would
    accept requests and then 500 ("attempt to write a readonly database") on
    the first write, 500-looping the readiness probe into a multi-minute
    silent hang. Raising at bind time turns that into an instant, diagnosable
    startup failure carrying the mode / path / uid / owner and a remediation
    hint, instead of a mystery timeout.
    """


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


def cold_seed_runtime(canonical: str | os.PathLike[str]) -> RuntimePaths:
    """Copy ``canonical`` → runtime, exactly when the runtime is absent.

    Call this from the server process, at import time, before opening
    the SQLAlchemy engine. The function is idempotent: a second call (or
    a call from a second process at the same canonical) is a no-op so
    long as the runtime file already exists.

    Args:
        canonical: The original (slow-storage) DB path. Typically the
            value of ``DATABASE_PATH`` before any tmpfs redirect.

    Returns:
        :class:`RuntimePaths` for ``canonical`` — the caller passes
        ``paths.runtime`` to SQLAlchemy as the new DB file.

    The contract on idempotency is critical: ``_resolve_db_path``-style
    code runs at *import* time in EVERY process that imports the DB
    module (including snapshot / populate workers that import alongside
    the live server). A re-copy from one of those secondary processes
    would (a) overwrite un-checkpointed WAL frames the agent has already
    committed and (b) poison the live server's pool via stale SHM
    mappings. By gating on "runtime is absent", this function is safe to
    call from anywhere — but the *intent* is server-process cold start.
    """
    paths = runtime_paths_for(canonical)

    # Aliased mode: a caller (typically ``bind_engine`` in direct mode)
    # has wired runtime == canonical via a custom RuntimePaths, OR the
    # canonical lives directly under tmp and the hash collision puts the
    # runtime at the same place. There's nothing to copy; the canonical
    # IS the runtime. Return the paths so the caller's downstream
    # bookkeeping (marker stamp) still works.
    try:
        aliased = paths.runtime.resolve() == paths.canonical.resolve()
    except OSError:
        aliased = False
    if aliased:
        logger.debug(
            "cold_seed: runtime %s IS canonical (aliased mode) — no copy",
            paths.canonical,
        )
        return paths

    # Materialise the private runtime dir (0o700) before anything writes into
    # it. This covers both the copy path below and the blank-world case, where
    # SQLAlchemy lazily creates the runtime file inside this dir on first
    # connect — the dir permission keeps that lazily-created file private too.
    ensure_runtime_dir()

    if not paths.canonical.exists():
        # Blank world: SQLAlchemy will create the runtime on first
        # connect. Don't stamp a marker — there's nothing yet to track.
        logger.debug(
            "cold_seed: canonical %s absent; runtime will be created lazily",
            paths.canonical,
        )
        return paths

    if paths.runtime.exists():
        # The runtime is present. Could be (a) this process is a re-import
        # of the live server, (b) a worker process that imported alongside
        # it, or (c) leftover tmpfs from a previous container that happens
        # to have the same hashed path. In (a)/(b) the live server owns the
        # runtime and we must NOT clobber it — but a runtime WE cannot write
        # is a different beast: the per-uid runtime path already scopes this
        # file to the current uid, so a non-writable pre-existing runtime at
        # our own path is the cross-uid handoff / read-only-inode bug (the
        # gap the removed per-app 0o666 handoff used to cover), not a live
        # peer. Re-seed a fresh, current-uid-owned copy rather than silently
        # binding a DB the server can't write (→ 300s readiness hang on the
        # first audit INSERT). `_assert_runtime_writable` treats BUSY/LOCKED
        # as writable, so a genuinely-live writer is never mistaken for this.
        if _assert_runtime_writable(paths.runtime):
            logger.debug("cold_seed: runtime %s already present; not re-copying", paths.runtime)
            return paths
        logger.warning(
            "cold_seed: runtime %s present but NOT writable by uid=%d — re-seeding a "
            "fresh copy (cross-uid handoff / read-only inode)",
            paths.runtime,
            os.getuid(),
        )
        # Fall through to the copy path; _copy_db_with_wal_fold unlinks the
        # stale inode + sidecars first, so SQLite recreates a fresh file owned
        # by this process.

    # Runtime absent: cold start. Copy (folding any WAL frames), drop any
    # orphan sidecars at the runtime path, stamp the marker. Best-effort
    # throughout — if any step fails we log and return the paths anyway
    # so the caller can fall back to the canonical DB path.
    try:
        _copy_db_with_wal_fold(paths.canonical, paths.runtime)
    except (OSError, sqlite3.DatabaseError) as exc:
        # _copy_db_with_wal_fold removes any partial destination it left behind
        # before re-raising, so the runtime is cleanly ABSENT here (not a stray
        # empty DB a later path lookup could return) — the caller falls
        # back to the canonical DB path.
        logger.warning(
            "cold_seed: copy %s → %s failed: %s — caller should fall back to canonical",
            paths.canonical,
            paths.runtime,
            exc,
        )
        return paths

    # The runtime was absent, so any -wal / -shm at the same path are
    # orphans from a previous boot (no live server owns them). Drop
    # them so the fresh copy isn't paired with a stale journal (salt
    # mismatch = pool poisoning on first read).
    for sidecar in (paths.wal, paths.shm):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("cold_seed: could not drop orphan sidecar %s: %s", sidecar, exc)

    # Post-copy writability assert — BEFORE the marker stamp. The fresh copy
    # should be owner-writable (0o600, this uid). If it somehow isn't (a dir
    # this uid can't write, a priv-drop between import and seed), do NOT stamp
    # the marker on a read-only runtime; leave it unstamped so the bind-level
    # preflight (bind_engine, runtime mode) turns this into a fail-loud raise so
    # the server never serves it silently.
    if not _assert_runtime_writable(paths.runtime):
        logger.error(
            "cold_seed: runtime %s is NOT writable after fresh copy (uid=%d) — NOT "
            "stamping the marker (keeps refresh pending); bind preflight will refuse "
            "to bind",
            paths.runtime,
            os.getuid(),
        )
        return paths

    try:
        write_marker(paths.marker, paths.canonical)
    except OSError as exc:
        logger.warning("cold_seed: could not write marker %s: %s", paths.marker, exc)

    try:
        size_mb = paths.canonical.stat().st_size / 1_000_000
        logger.info(
            "cold_seed: copied %s → %s (%.1f MB)",
            paths.canonical,
            paths.runtime,
            size_mb,
        )
    except OSError:
        pass

    return paths
