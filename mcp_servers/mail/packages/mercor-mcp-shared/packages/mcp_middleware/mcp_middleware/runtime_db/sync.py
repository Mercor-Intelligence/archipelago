"""Cold-start seed + warm-task refresh for the SQLite runtime DB.

Three functions, one per process role. Calling the wrong one in the
wrong place can clobber an active server's runtime DB; the API split is
the safety contract.

============================  ============================================
Function                       Who calls it
============================  ============================================
:func:`cold_seed_runtime`     The server itself, exactly once at import
                              time, before the engine is opened. Idempotent
                              (no-op when the runtime DB already exists).
:func:`refresh_runtime_from_canonical`
                              Out-of-process workers (populate, sync,
                              backup) before they touch the runtime DB.
                              Drives a server-side checkpoint over HTTP
                              and refuses to copy if the WAL never clears.
:func:`resolve_runtime_path`  Anyone — pure path lookup. Raises
                              :class:`RuntimeDbMissingError` if the
                              runtime DB hasn't been seeded yet (escape
                              hatch ``_force=True``).
============================  ============================================

The "out-of-process" contract is load-bearing. A second process that
copies over a runtime DB the live server still has open will:

1. Overwrite un-checkpointed WAL frames the agent committed but the
   server hasn't folded back yet.
2. Poison the server's connection pool — every pooled fd holds a memory
   map into the now-stale ``-shm`` sidecar; the next operation raises
   ``SQLITE_IOERR`` and sticks until the server restarts.

That's why ``refresh_runtime_from_canonical`` drives a checkpoint via
HTTP first (so the server itself flushes its pool) and refuses to copy
if the WAL never reaches ``busy=0``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.request import pathname2url

from .paths import (
    RuntimePaths,
    ensure_runtime_dir,
    fingerprint_canonical,
    read_marker,
    runtime_paths_for,
    secure_file,
    write_marker,
)

logger = logging.getLogger(__name__)


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
    locks the runtime copy to ``0o600``) and :func:`copy_db_wal_aware`
    (which leaves the destination readable for a cross-uid consumer).

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


def _copy_db_with_wal_fold(src: Path, dst: Path) -> None:
    """WAL-aware copy that locks the destination to the owner (``0o600``).

    Used for the runtime cold-seed / refresh copies whose destination is
    the private per-uid runtime file: restrict it to the owner. The
    enclosing runtime dir is already ``0o700``, so this is
    defense-in-depth — but it also covers the rare case where the dir
    permission couldn't be applied. For a destination that a *different*
    uid must read (the persist route's canonical), use
    :func:`copy_db_wal_aware` instead, which leaves perms alone.

    The destination (and any ``-wal`` / ``-shm`` sidecars) is **unlinked
    before** the backup so SQLite recreates a FRESH inode owned by THIS
    process. Backing up *into* a pre-existing runtime inode that a prior —
    possibly cross-uid — connection opened under WAL can leave residual
    ``-wal`` / ``-shm`` ownership and advisory-lock state that SQLite
    honours by serving the reopened file **read-only**, so the next write
    raises "attempt to write a readonly database" (the exact trap
    :func:`~mcp_middleware.runtime_db.unlink_stale_runtime` documents for
    the cross-uid populate handoff). This matters most on the in-process
    self-heal refresh, which copies a repopulated canonical *over* the
    boot-time runtime; a fresh inode sidesteps the downgrade. On the
    cold-seed path the destination is already absent, so the unlink is a
    harmless no-op. Linux ``unlink`` semantics preserve any fds an
    in-flight handler still holds (they read the now-orphan inode) while
    new connections see the fresh file.
    """
    sidecars = (dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm"))
    for stale in (dst, *sidecars):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Couldn't drop the stale file (dir perms / race). Proceed anyway —
            # the backup below reuses the inode; the writability check the
            # refresh caller runs afterwards catches a resulting read-only bind.
            logger.warning("runtime copy: could not unlink stale %s: %s", stale, exc)
    try:
        _sqlite_backup(src, dst)
    except (OSError, sqlite3.DatabaseError):
        # `_sqlite_backup` opens (creates) the destination via ``sqlite3.connect``
        # before it reads the source, so an interrupted / failed backup leaves a
        # partial — usually *empty* — DB behind. Because the live runtime was
        # unlinked above, that partial would otherwise masquerade as a present,
        # writable runtime: a later ``cold_seed_runtime`` exists()-shortcut passes
        # its writability probe on the empty file and binds/snapshots an empty
        # database (this is what the refresh path's own error return could not
        # clean up). Remove the partial (and any sidecars) so the destination is
        # cleanly ABSENT on failure — cold_seed then re-copies and bind falls back
        # to the canonical DB — and re-raise for the caller to log / branch on.
        for partial in (dst, *sidecars):
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("runtime copy: could not clean partial %s: %s", partial, exc)
        raise
    secure_file(dst)


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


def copy_db_wal_aware(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Public WAL-aware SQLite copy that does NOT lock the destination down.

    The counterpart to the internal cold-seed copy: same byte-consistent,
    WAL-folding :meth:`sqlite3.Connection.backup`, but it leaves the
    destination's permissions untouched. This is what the persist route
    uses to fold the server's per-uid runtime back onto the canonical on
    shared storage — the canonical must stay readable by the (different-uid)
    snapshot/fold process, so the ``0o600`` owner-lock that
    :func:`_copy_db_with_wal_fold` applies would defeat the purpose.
    """
    _sqlite_backup(Path(src), Path(dst))


__all__ = [
    "RefreshOutcome",
    "RefreshResult",
    "RuntimeDbMissingError",
    "RuntimeDbReadonlyError",
    "cold_seed_runtime",
    "copy_db_wal_aware",
    "refresh_runtime_from_canonical",
    "resolve_runtime_path",
    "runtime_refresh_pending",
]


def runtime_refresh_pending(canonical: str | os.PathLike[str]) -> bool:
    """Cheap, read-only check: would :func:`refresh_runtime_from_canonical` copy?

    Returns True iff the canonical exists *and* its fingerprint (size +
    mtime_ns) differs from the runtime marker — i.e. the runtime is stale
    (or absent) and a refresh would actually copy. Returns False when the
    canonical is absent (nothing to refresh from) or the runtime is already
    in sync (a refresh would be a ``NOOP``).

    This is the **lock-free fast path** an in-process, hot-path caller (the
    default-user identity gate) uses to decide whether to bother serializing
    on the real refresh. It mirrors the planner branch of
    :func:`refresh_runtime_from_canonical` exactly — canonical present, then
    ``fingerprint == marker`` — but does no copy, no pool dispose, and no
    checkpoint: just one ``stat`` on the canonical plus one marker read. A
    stat that fails mid-flight returns True (conservative: "can't prove it's
    in sync, let the full refresh decide/report"), matching the
    ``REFUSED_PLANNER_FAILURE`` "must not silently skip" contract.
    """
    paths = runtime_paths_for(canonical)
    if not paths.canonical.exists():
        return False
    try:
        current = fingerprint_canonical(paths.canonical)
    except OSError:
        # Couldn't stat the canonical mid-flight — don't silently skip; let
        # the caller take the slow path where the full refresh reports it.
        return True
    stored = read_marker(paths.marker)
    return not (paths.runtime.exists() and stored == current)


class RuntimeDbMissingError(RuntimeError):
    """Raised by :func:`resolve_runtime_path` when the runtime DB is absent.

    A read-only caller that hits this exception should treat it as
    "server hasn't booted yet" rather than auto-seed. Seeding is the
    *server's* responsibility (via :func:`cold_seed_runtime`); a worker
    that auto-seeds races a cold-start in progress.
    """


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


class RefreshOutcome(StrEnum):
    """Outcome of :func:`refresh_runtime_from_canonical`.

    String-valued so the bash callers (curl + jq) can compare directly:

        outcome=$(uv run python -c "from mcp_middleware.runtime_db import \
            refresh_runtime_from_canonical; print(\
            refresh_runtime_from_canonical('$CANONICAL').value)")
        case "$outcome" in
            noop|refreshed) ... ;;
            *) ... ;;
        esac
    """

    NOOP = "noop"
    """Canonical hasn't changed since the last refresh — runtime is in sync."""

    REFRESHED = "refreshed"
    """Canonical differed; runtime was successfully copied + marker rewritten."""

    NO_CANONICAL = "no_canonical"
    """Canonical doesn't exist on disk — nothing to refresh from."""

    REFUSED_BUSY = "refused_busy"
    """Server-side checkpoint never cleared after ``max_attempts``;
    we refused to overwrite a runtime that may still pin un-checkpointed
    WAL frames. Caller should fall back to a full populate."""

    REFUSED_PLANNER_FAILURE = "refused_planner_failure"
    """Couldn't decide whether refresh is needed (e.g. ``os.stat`` failed
    on the canonical mid-flight). Treated as "must not silently skip" —
    caller should fall back to a full populate."""

    REFUSED_READONLY = "refused_readonly"
    """The copy completed but the resulting runtime is **not writable** by
    this process (cross-uid handoff / residual read-only inode). We refuse
    to report a successful refresh, because binding this runtime would 500
    on the first write ("attempt to write a readonly database") — a silent
    hang instead of a clean failure. The in-process gate caller must fail
    closed (serve a retryable maintenance error) rather than open onto it;
    a later retry re-copies into a fresh inode and typically succeeds."""


@dataclass(frozen=True)
class RefreshResult:
    """Structured result for :func:`refresh_runtime_from_canonical`.

    Attributes:
        outcome: One of :class:`RefreshOutcome`.
        paths: The resolved canonical / runtime / marker paths.
        checkpoint_attempted: True iff we made any HTTP POST to the
            checkpoint endpoint. False when ``checkpoint_url`` was
            ``None`` *and* we copied (no live server to flush) or when
            we returned ``NOOP`` / ``NO_CANONICAL`` before reaching the
            checkpoint step.
        checkpoint_clean: True iff the checkpoint reached ``busy=0``.
            ``False`` when we never attempted, when the server was
            unreachable, or when ``REFUSED_BUSY`` was returned.
        detail: Human-readable summary suitable for log lines.
    """

    outcome: RefreshOutcome
    paths: RuntimePaths
    checkpoint_attempted: bool
    checkpoint_clean: bool
    detail: str


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
    call from anywhere — but the *intent* is server-process cold start;
    workers should use :func:`refresh_runtime_from_canonical` instead.
    """
    paths = runtime_paths_for(canonical)

    # Aliased mode: a caller (typically ``bind_engine`` in direct mode)
    # has wired runtime == canonical via a custom RuntimePaths, OR the
    # canonical lives directly under tmp and the hash collision puts the
    # runtime at the same place. There's nothing to copy; the canonical
    # IS the runtime. Return the paths so the caller's downstream
    # bookkeeping (marker stamp, resolve_runtime_path) still works.
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
        # empty DB a later resolve_runtime_path could return) — the caller falls
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
    # the marker: the marker is what runtime_refresh_pending compares against
    # the canonical fingerprint, so a matching marker on a read-only runtime
    # would make the gate's lock-free fast path read it as IN_SYNC and open onto
    # it instead of retrying. Leaving the marker unstamped keeps
    # runtime_refresh_pending True (a later refresh re-copies into a fresh
    # inode), and the bind-level preflight (bind_engine, runtime mode) turns
    # this into a fail-loud raise so the server never serves it silently.
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


# ---------------------------------------------------------------------------
# Read-only resolve
# ---------------------------------------------------------------------------


def resolve_runtime_path(canonical: str | os.PathLike[str], *, _force: bool = False) -> Path:
    """Return the runtime path for ``canonical`` (read-only lookup).

    Args:
        canonical: The slow-storage path. Same value passed to
            :func:`cold_seed_runtime` and :func:`refresh_runtime_from_canonical`.
        _force: If True, return the would-be runtime path even when the
            file doesn't exist. **Almost no caller should pass this** —
            it's an escape hatch for advanced cases (logging the
            expected path, computing a sidecar location, integration
            tests). Auto-seeding from a worker that observes "runtime
            missing" is exactly the race :class:`RuntimeDbMissingError`
            exists to prevent.

    Returns:
        Absolute :class:`Path` to the runtime DB.

    Raises:
        RuntimeDbMissingError: When the runtime DB hasn't been seeded
            yet and ``_force=False``. Callers should treat this as
            "server hasn't booted" rather than auto-seed.
    """
    paths = runtime_paths_for(canonical)
    if not _force and not paths.runtime.exists():
        raise RuntimeDbMissingError(
            f"runtime DB not seeded for canonical {paths.canonical} "
            f"(expected at {paths.runtime}). The server process is "
            f"responsible for seeding via cold_seed_runtime; a worker "
            f"that auto-seeds races a cold-start in progress."
        )
    return paths.runtime


# ---------------------------------------------------------------------------
# Warm refresh (out-of-process worker → live server)
# ---------------------------------------------------------------------------


def refresh_runtime_from_canonical(
    canonical: str | os.PathLike[str],
    *,
    checkpoint_url: str | None = None,
    drain: Callable[[], None] | None = None,
    max_attempts: int = 5,
    attempt_sleep: float = 2.0,
    _sleep: object = time.sleep,
    _http_post: object = None,
) -> RefreshResult:
    """Refresh the runtime DB from ``canonical`` when their fingerprints differ.

    Call this from a separate process (populate, sync, backup) BEFORE
    the worker touches the runtime DB. If the canonical's fingerprint
    (size + mtime_ns) matches the marker, the runtime is already in
    sync and this is a no-op.

    When the fingerprints differ, the function drives a server-side
    checkpoint via ``checkpoint_url`` (so the live server folds its WAL
    and releases its pool's SHM mappings), then copies ``canonical →
    runtime``, drops the stale sidecars, and rewrites the marker.

    Args:
        canonical: The slow-storage path.
        checkpoint_url: Full URL of the shared checkpoint endpoint,
            e.g. ``"http://127.0.0.1:5000/_internal/checkpoint"``. When
            ``None``, no server is assumed to be running (CI / offline
            populate) and we skip straight to the copy. When set but
            unreachable, treated the same as ``None`` — there's no live
            reader to coordinate with, so the copy is safe.
        drain: Optional zero-arg hook called **in-process, immediately
            before the copy** — and ONLY when a copy is actually going to
            happen (i.e. past the ``NOOP`` / ``NO_CANONICAL`` /
            ``REFUSED_BUSY`` early returns). This is the in-process
            counterpart to ``checkpoint_url``: an *in-process* caller
            (e.g. the default-user gate's self-heal, which IS the live
            server) passes ``drain=engine.dispose`` so the live pool
            releases its fds before we overwrite the runtime inode.
            A ``NOOP`` refresh never calls it, so a caller can invoke this
            on a hot path without paying a pool-dispose when nothing
            changed. A hook exception is logged, not raised.
        max_attempts: How many times to POST the checkpoint before
            giving up. Each attempt waits ``attempt_sleep`` seconds.
        attempt_sleep: Seconds to wait between checkpoint retries.
        _sleep / _http_post: Test hooks (not part of the public API);
            default to :func:`time.sleep` and ``httpx.post``.

    Returns:
        :class:`RefreshResult` — outcome enum + the paths it operated on
        + checkpoint metadata + a human-readable detail string.

    The fail-safe is "must not silently skip". A planner failure (we
    couldn't read the canonical's stat) and a refused-busy checkpoint
    both return distinct outcomes so the caller can choose to fall back
    to a full populate rather than serve potentially stale data.
    """
    paths = runtime_paths_for(canonical)

    if not paths.canonical.exists():
        return RefreshResult(
            outcome=RefreshOutcome.NO_CANONICAL,
            paths=paths,
            checkpoint_attempted=False,
            checkpoint_clean=False,
            detail=f"canonical {paths.canonical} does not exist",
        )

    # ── Planner: decide refresh-needed vs noop ──────────────────────────
    try:
        current = fingerprint_canonical(paths.canonical)
    except OSError as exc:
        return RefreshResult(
            outcome=RefreshOutcome.REFUSED_PLANNER_FAILURE,
            paths=paths,
            checkpoint_attempted=False,
            checkpoint_clean=False,
            detail=f"could not stat canonical {paths.canonical}: {exc}",
        )

    stored = read_marker(paths.marker)
    if paths.runtime.exists() and stored == current:
        return RefreshResult(
            outcome=RefreshOutcome.NOOP,
            paths=paths,
            checkpoint_attempted=False,
            checkpoint_clean=False,
            detail=(
                f"runtime {paths.runtime} already in sync with canonical "
                f"{paths.canonical} (fingerprint {current})"
            ),
        )

    # ── Checkpoint: ask the live server to flush its WAL + drain pool ──
    checkpoint_attempted = False
    checkpoint_clean = False
    if checkpoint_url:
        checkpoint_attempted, checkpoint_clean, ckpt_detail = _retry_checkpoint(
            checkpoint_url,
            max_attempts=max_attempts,
            attempt_sleep=attempt_sleep,
            sleep=_sleep,  # type: ignore[arg-type]
            http_post=_http_post,
        )
        # An attempted checkpoint that never cleared = live server still
        # pins WAL frames. Copying now would overwrite committed work
        # and poison the pool — refuse and let the caller decide.
        if checkpoint_attempted and not checkpoint_clean:
            return RefreshResult(
                outcome=RefreshOutcome.REFUSED_BUSY,
                paths=paths,
                checkpoint_attempted=True,
                checkpoint_clean=False,
                detail=(
                    f"checkpoint at {checkpoint_url} never cleared after "
                    f"{max_attempts} attempt(s); refusing to overwrite the "
                    f"live runtime db ({ckpt_detail})"
                ),
            )
        # If we never reached the server it's the "no live reader" case —
        # safe to copy. checkpoint_attempted stays False so the caller
        # can tell.

    # In-process drain: an in-process caller (the identity gate's self-heal)
    # passes drain=engine.dispose so the live pool releases its fds before we
    # overwrite the runtime inode — the local counterpart to the checkpoint
    # POST above. Only fires here, past every early return, so a NOOP refresh
    # never disposes a pool. Best-effort: a drain failure must not abort the
    # refresh (worst case the copy races a pooled fd, which the marker rewrite
    # + next-connection reopen still recovers from).
    if drain is not None:
        try:
            drain()
        except Exception as exc:  # noqa: BLE001 - drain failure mustn't abort the refresh
            logger.warning("refresh: drain hook failed: %s", exc)

    # ── Copy: canonical → runtime + drop sidecars + rewrite marker ─────
    # Use the WAL-aware backup so a canonical whose -wal still holds
    # un-checkpointed frames (raw upload, manual cp without checkpoint)
    # produces a fully-folded runtime — not a stale main-file-only copy.
    try:
        ensure_runtime_dir()
        _copy_db_with_wal_fold(paths.canonical, paths.runtime)
    except (OSError, sqlite3.DatabaseError) as exc:
        return RefreshResult(
            outcome=RefreshOutcome.REFUSED_PLANNER_FAILURE,
            paths=paths,
            checkpoint_attempted=checkpoint_attempted,
            checkpoint_clean=checkpoint_clean,
            detail=f"copy {paths.canonical} → {paths.runtime} failed: {exc}",
        )

    for sidecar in (paths.wal, paths.shm):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("refresh: could not drop sidecar %s: %s", sidecar, exc)

    # Writability gate — BEFORE the marker stamp. The copy folded every byte,
    # but a cross-uid handoff or a residual read-only inode can still leave
    # SQLite serving the runtime read-only — the next write (an audit INSERT /
    # commit) would then raise "attempt to write a readonly database" inside a
    # request handler and 500-loop the readiness probe. Refuse to report
    # REFRESHED so the in-process gate fails closed (retryable maintenance
    # error) instead of opening onto a DB it cannot write.
    #
    # Crucially, do NOT stamp the marker on this path: the marker is what
    # `runtime_refresh_pending` compares against the canonical fingerprint. If
    # we stamped it here it would match the (drifted) canonical, so the gate's
    # lock-free fast path would read the read-only runtime as IN_SYNC and open
    # onto it — silently defeating the retry. Leaving the marker stale keeps
    # `runtime_refresh_pending` True, so the next request re-copies into a fresh
    # inode (and either recovers or fails closed again). Only a writable runtime
    # earns the marker below.
    if not _assert_runtime_writable(paths.runtime):
        logger.error(
            "refresh: runtime %s is NOT writable after copy from canonical %s "
            "(cross-uid handoff / read-only inode) — refusing to report a fresh "
            "runtime the server would 500 on at first write",
            paths.runtime,
            paths.canonical,
        )
        return RefreshResult(
            outcome=RefreshOutcome.REFUSED_READONLY,
            paths=paths,
            checkpoint_attempted=checkpoint_attempted,
            checkpoint_clean=checkpoint_clean,
            detail=(
                f"runtime {paths.runtime} not writable after refresh copy from "
                f"canonical {paths.canonical} (fingerprint {current})"
            ),
        )

    try:
        write_marker(paths.marker, paths.canonical)
    except OSError as exc:
        logger.warning("refresh: could not write marker %s: %s", paths.marker, exc)

    return RefreshResult(
        outcome=RefreshOutcome.REFRESHED,
        paths=paths,
        checkpoint_attempted=checkpoint_attempted,
        checkpoint_clean=checkpoint_clean,
        detail=(
            f"refreshed runtime {paths.runtime} from canonical {paths.canonical} "
            f"(fingerprint {current})"
        ),
    )


def _retry_checkpoint(
    url: str,
    *,
    max_attempts: int,
    attempt_sleep: float,
    sleep,
    http_post,
) -> tuple[bool, bool, str]:
    """Poll the checkpoint endpoint until ``busy=0`` or attempts exhausted.

    Returns ``(attempted, clean, detail)``:
        attempted: True if we got *any* HTTP response (server reachable).
        clean: True if the server reported ``busy=0``.
        detail: Human-readable summary for the result.
    """
    # Defer the import so callers that never pass a checkpoint_url don't
    # pay the httpx import cost (e.g. CI / unit-test populate paths).
    if http_post is None:
        import httpx

        def _default_post(u: str, *, timeout: float):
            return httpx.post(u, timeout=timeout)

        post_fn = _default_post
    else:
        post_fn = http_post

    last_response_busy: int | None = None
    server_reachable = False
    for attempt in range(1, max_attempts + 1):
        try:
            resp = post_fn(url, timeout=15.0)
        except Exception as exc:  # noqa: BLE001 - any HTTP failure means "not reachable"
            # If a PRIOR attempt in this loop already reached the server
            # (server_reachable=True), we KNOW a live reader exists — the
            # current exception is a transient HTTP failure mid-checkpoint
            # (timeout, connection reset, etc.). Returning attempted=False
            # here would tell the caller "no live server, safe to copy",
            # but the server is up and we have no proof the WAL ever
            # cleared — copying could overwrite frames the server still
            # has pinned. Force the caller down the REFUSED_BUSY path
            # instead so a full populate runs.
            if server_reachable:
                return (
                    True,
                    False,
                    (
                        f"server at {url} returned busy on a prior attempt then "
                        f"became unreachable on attempt {attempt}: {exc}"
                    ),
                )
            return (
                False,
                False,
                f"server unreachable at {url}: {exc}",
            )
        server_reachable = True
        try:
            payload = resp.json()
        except ValueError:
            status = getattr(resp, "status_code", "?")
            return (
                True,
                False,
                f"server returned non-JSON checkpoint response (HTTP {status})",
            )
        busy = int(payload.get("busy", 1))
        last_response_busy = busy
        if busy == 0:
            return (
                True,
                True,
                f"checkpoint clean on attempt {attempt} (response={payload})",
            )
        if attempt < max_attempts:
            sleep(attempt_sleep)

    return (
        server_reachable,
        False,
        f"checkpoint busy after {max_attempts} attempt(s) (last busy={last_response_busy})",
    )
