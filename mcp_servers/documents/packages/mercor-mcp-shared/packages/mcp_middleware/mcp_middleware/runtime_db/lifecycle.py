"""Populate-side runtime-DB lifecycle primitives (drain / unlink / handoff / estimate).

These four helpers were proven in Foundry-Google-Workspace's populate hook
(``scripts/populate_engine_main.py`` + ``db/session.py``) and are lifted here so
every SQLite-backed Foundry-* server shares one tested implementation instead of
copy-pasting the cross-uid / cross-process handoff dance.

The problem they solve: the **populate** lifecycle hook runs as a *different
process* (often a different uid — root) than the **live server**, but both point
at the same tmpfs runtime DB (``/tmp/workspace_runtime_<hash>.db``). Naively
harvesting / rewriting that file under a running server:

* races the server's connection pool (whose pooled fds hold ``-shm`` memory maps
  into the file) — a mid-flight replace poisons the pool with ``SQLITE_IOERR``;
* leaves a root-owned ``0o644`` file the non-root server can only open
  **read-only**, so its next pooled write fails with "attempt to write a readonly
  database" and stays poisoned until restart.

The primitives coordinate the handoff:

* :func:`drain_server_pool` — POST the shared ``/_internal/checkpoint`` route so
  the live server folds its WAL and disposes its pool *before* populate touches
  the file. Returns whether it's safe to proceed.
* :func:`unlink_stale_runtime` — delete the stale runtime + sidecars *before*
  ``bind_engine``'s ``cold_seed_runtime`` runs, so SQLite recreates the file
  owned by the populate process (RW) rather than downgrading to a cross-uid RO
  open of the server's leftover file.
* :func:`handoff_runtime_to_server` — after populate finishes writing, relax the
  file perms (``0o666``) and drive one more checkpoint so the server drops any
  poisoned RO connection and reopens the now-writable file RW.
* :func:`estimated_post_import_bytes` — pre-bind sizing so a caller can force
  direct (on-disk) binding when the CSV import would blow the tmpfs budget.

All are best-effort and log-loud: populate's own writes have usually already
succeeded by the time handoff runs, so a chmod / checkpoint failure warns rather
than raising. :func:`drain_server_pool` is the exception — its boolean return is
a *safety gate* the caller must honour (refuse to harvest when it returns False).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "checkpoint_url_for_port",
    "drain_server_pool",
    "estimated_post_import_bytes",
    "handoff_runtime_to_server",
    "persist_server_runtime",
    "persist_url_for_port",
    "unlink_stale_runtime",
]

# Default drain retry policy. Overridable per call; the values match the
# FGW-proven defaults (5 attempts × 2s backoff, 15s per-request timeout).
_DRAIN_ATTEMPTS = 5
_DRAIN_BACKOFF_SECONDS = 2.0
_DRAIN_HTTP_TIMEOUT_SECONDS = 15.0

# Default CSV import growth factor for estimated_post_import_bytes: the ingest
# commits the whole corpus in one WAL-mode transaction, so peak footprint is
# ~1x the resulting rows+indexes PLUS ~1x the whole-corpus WAL held transiently.
_CSV_IMPORT_GROWTH_FACTOR = 2.0


_INTERNAL_MOUNT = "_internal"


def internal_base_url_for_port(server_port: int | str, *, host: str = "127.0.0.1") -> str:
    """Build the loopback ``/_internal`` base URL for ``server_port``.

    Every runtime-DB route (``checkpoint``, ``persist``, …) is a sibling under
    this one mount, so this is the single source of truth callers derive routes
    from. Matches the mount used by
    :func:`mcp_middleware.runtime_db.register_runtime_db_routes`.
    """
    return f"http://{host}:{server_port}/{_INTERNAL_MOUNT}"


def server_base_from_locator(
    *,
    server_port: int | str | None = None,
    checkpoint_url: str | None = None,
) -> str | None:
    """Resolve the ONE base URL that all ``/_internal`` routes hang off.

    persist + checkpoint (+ any future route) are siblings under a single mount,
    so a caller names that base *once* — via a loopback ``server_port`` or an
    explicit ``checkpoint_url`` — and every route is derived from it. This keeps
    persist and drain pointed at the SAME host + mount instead of accepting two
    independently-hostable URLs that could diverge.

    When both are given ``checkpoint_url`` wins: it can name a non-loopback host
    or custom mount that ``server_port`` (always 127.0.0.1) cannot. A
    ``checkpoint_url`` ending in ``/checkpoint`` — with or without a trailing
    slash — is stripped back to its base; any other value is treated as an
    already-bare base.
    """
    if checkpoint_url is not None:
        # Strip a trailing slash FIRST so ".../checkpoint/" is recognised as the
        # checkpoint route (else the suffix survives and persist/drain get built
        # as ".../checkpoint/persist", pointing at a route that doesn't exist).
        base = checkpoint_url.rstrip("/")
        if base.endswith("/checkpoint"):
            return base[: -len("/checkpoint")]
        return base
    if server_port is not None:
        return internal_base_url_for_port(server_port)
    return None


def checkpoint_url_for_port(server_port: int | str, *, host: str = "127.0.0.1") -> str:
    """Build the loopback ``/_internal/checkpoint`` URL for ``server_port``.

    Convenience so callers (and the ``snapshot_with_populate`` facade) don't
    hand-format the URL. Matches the route mounted by
    :func:`mcp_middleware.runtime_db.register_runtime_db_routes`.
    """
    return f"{internal_base_url_for_port(server_port, host=host)}/checkpoint"


def drain_server_pool(
    checkpoint_url: str,
    *,
    attempts: int = _DRAIN_ATTEMPTS,
    backoff_seconds: float = _DRAIN_BACKOFF_SECONDS,
    http_timeout_seconds: float = _DRAIN_HTTP_TIMEOUT_SECONDS,
    _urlopen: Callable[..., object] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> bool:
    """Best-effort drain of the *live server's* connection pool before harvest.

    POSTs the shared ``/_internal/checkpoint`` route (see
    :func:`mcp_middleware.runtime_db.register_runtime_db_routes`), which disposes
    the server's SQLAlchemy pool and folds its WAL via
    ``PRAGMA wal_checkpoint(TRUNCATE)``. Retries until the server reports
    ``busy == 0`` or ``attempts`` is exhausted.

    Returns:
        ``True`` when it is **safe to harvest / rewrite the runtime DB**:

        * the server reported ``busy == 0`` (pool drained, WAL folded), OR
        * the server isn't reachable on **any** attempt — no live reader can be
          holding the source DBs open, so harvesting can't race a pool. A single
          connection-level failure (timeout / refused) is **retried**, not taken
          as terminal: a live-but-slow server that merely exceeds the HTTP
          timeout still holds an undrained pool, so treating one timeout as "no
          server, harvest safe" would let harvest race that pool and poison the
          runtime DB.

        ``False`` when the server is reachable but never reports a drained pool.
        The caller MUST refuse to harvest in that case rather than orphan the
        server's ``-shm`` mmaps (a mid-flight replace poisons the pool with
        ``SQLITE_IOERR`` until restart).

    The ``HTTPError``-before-``URLError`` catch ordering is load-bearing:
    ``urllib.error.HTTPError`` is a ``URLError`` *subclass* representing a
    *reachable* server that returned a non-2xx status (e.g. a 503 mid-shutdown).
    It must be caught first and treated as "server is up, retry" — folding it
    into the generic ``URLError`` branch would misreport a transient hiccup as
    "server not reachable, harvest is safe" and let harvest race a live pool.
    """
    import urllib.error
    import urllib.request

    urlopen = _urlopen if _urlopen is not None else urllib.request.urlopen
    sleep = _sleep if _sleep is not None else time.sleep

    via_server = False
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(checkpoint_url, method="POST")  # noqa: S310 - fixed loopback URL
        try:
            with urlopen(req, timeout=http_timeout_seconds) as resp:  # noqa: S310
                via_server = True
                status = getattr(resp, "status", 200)
                if status != 200:
                    logger.warning(
                        "drain_server_pool: checkpoint returned HTTP %s "
                        "(attempt %s/%s) — retrying in %ss",
                        status,
                        attempt,
                        attempts,
                        backoff_seconds,
                    )
                    sleep(backoff_seconds)
                    continue
                try:
                    payload = json.loads(resp.read().decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                busy = payload.get("busy", 1)
        except urllib.error.HTTPError as exc:
            # Reachable server, non-2xx status. urlopen raises this BEFORE the
            # with-block body reads resp.status, so the 200 check above never
            # sees it. Count the attempt as via_server and retry. MUST precede
            # the URLError catch — subclass ordering matters.
            via_server = True
            logger.warning(
                "drain_server_pool: checkpoint returned HTTP %s %s "
                "(attempt %s/%s) — retrying in %ss",
                exc.code,
                exc.reason,
                attempt,
                attempts,
                backoff_seconds,
            )
            sleep(backoff_seconds)
            continue
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            # Connection-level failure. Could be "no server" (cold populate) OR a
            # transient blip on a live-but-slow server that merely exceeded the
            # HTTP timeout while still holding an undrained pool. Don't conclude
            # "no live reader, harvest safe" on a single failure — retry, and let
            # the post-loop check decide from whether ANY attempt reached the
            # server. Returning True here on a timeout would let harvest race a
            # live pool and poison the runtime DB.
            logger.info(
                "drain_server_pool: %s not reachable (%s: %s) on attempt %s/%s — retrying in %ss",
                checkpoint_url,
                type(exc).__name__,
                exc,
                attempt,
                attempts,
                backoff_seconds,
            )
            sleep(backoff_seconds)
            continue

        if busy == 0:
            logger.info(
                "drain_server_pool: pool drained via %s (attempt %s) — harvest safe",
                checkpoint_url,
                attempt,
            )
            return True
        logger.warning(
            "drain_server_pool: WAL busy=%s (attempt %s/%s) — retrying in %ss",
            busy,
            attempt,
            attempts,
            backoff_seconds,
        )
        sleep(backoff_seconds)

    if not via_server:
        # Every attempt failed to reach the server: no live reader, so the
        # canonical/runtime has no in-flight writer to race — harvest is safe.
        logger.info(
            "drain_server_pool: server never reachable at %s after %s attempt(s) — "
            "no live reader, harvest is safe",
            checkpoint_url,
            attempts,
        )
        return True

    logger.error(
        "drain_server_pool: refusing to harvest — checkpoint never cleared (server reachable=%s)",
        via_server,
    )
    return False


def persist_url_for_port(server_port: int | str, *, host: str = "127.0.0.1") -> str:
    """Build the loopback ``/_internal/persist`` URL for ``server_port``.

    Convenience twin of :func:`checkpoint_url_for_port` — matches the persist
    route mounted by
    :func:`mcp_middleware.runtime_db.register_runtime_db_routes`.
    """
    return f"{internal_base_url_for_port(server_port, host=host)}/persist"


def persist_server_runtime(
    persist_url: str,
    *,
    attempts: int = _DRAIN_ATTEMPTS,
    backoff_seconds: float = _DRAIN_BACKOFF_SECONDS,
    http_timeout_seconds: float = _DRAIN_HTTP_TIMEOUT_SECONDS,
    _urlopen: Callable[..., object] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> bool:
    """Ask the *live server* to fold its per-uid runtime DB onto the canonical.

    POSTs the shared ``/_internal/persist`` route (see
    :func:`mcp_middleware.runtime_db.persist_runtime_to_canonical`). This is the
    write-side counterpart to :func:`drain_server_pool`: it must run BEFORE a
    cross-uid snapshot / fold hook reads the canonical, because the server's
    runtime DB lives under a per-uid ``0o700`` dir the fold process (a different
    uid) can't read. Only the server can persist its own mutations; this drives
    it over HTTP.

    Returns:
        ``True`` when it is **safe to proceed to harvest the canonical**:

        * the server persisted successfully (HTTP 200), OR
        * the server isn't reachable on ANY attempt — no live server means the
          canonical is already the source of truth (cold populate). A single
          connection-level failure (timeout / refused) is retried, not treated
          as terminal, so a momentarily-saturated live server isn't misread as
          "gone" (which would silently harvest a stale canonical), OR
        * the server returned **404** (older build without the persist route) or
          **501** (route registered engine-only — a DIRECT/MEMORY-mode server, or
          one that didn't thread ``runtime_canonical`` through ``run_server``). In
          both cases the server can't fold a cross-uid runtime for us, so fall
          back to the legacy behaviour (the caller's :func:`drain_server_pool`
          still guards the harvest). A warning is logged so the stale-data risk is
          visible.

        ``False`` **only** when the server is reachable and the persist route
        exists but never succeeded across ALL attempts (HTTP 500 every time —
        e.g. the WAL never cleared). A 500 is retried (like the busy path and
        like :func:`drain_server_pool`) since it is often a transient busy
        checkpoint; only exhausting the retries fails closed. The caller MUST
        refuse to harvest in that case rather than snapshot a canonical that is
        missing the server's latest writes.

    Mirrors :func:`drain_server_pool`'s ``HTTPError``-before-``URLError`` catch
    ordering: an ``HTTPError`` is a *reachable* server returning non-2xx. 404 /
    501 fall back explicitly; any other non-2xx is retried and only fails closed
    once attempts are exhausted — never misread as "server unreachable, proceed".
    """
    import urllib.error
    import urllib.request

    urlopen = _urlopen if _urlopen is not None else urllib.request.urlopen
    sleep = _sleep if _sleep is not None else time.sleep

    # Track whether we ever got a *response* from the server. A run that
    # only ever hit connection-level failures (URLError / ConnectionError /
    # TimeoutError) across all attempts means "no live server" → the canonical
    # is authoritative → proceed. But a SINGLE such failure is NOT terminal:
    # a live server that's momentarily saturated / GC-paused / restarting would
    # otherwise be misread as "gone", and the snapshot would silently harvest a
    # stale canonical and drop the server's latest writes. So we retry these
    # like the busy path, and only fall back to "no server" if EVERY attempt
    # failed to reach it.
    ever_reachable = False

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(persist_url, method="POST")  # noqa: S310 - fixed loopback URL
        try:
            with urlopen(req, timeout=http_timeout_seconds) as resp:  # noqa: S310
                ever_reachable = True
                status = getattr(resp, "status", 200)
                if status == 200:
                    try:
                        payload = json.loads(resp.read().decode("utf-8") or "{}")
                    except (ValueError, UnicodeDecodeError):
                        payload = {}
                    logger.info(
                        "persist_server_runtime: server persisted runtime → canonical "
                        "via %s (attempt %s, mode=%s, bytes=%s)",
                        persist_url,
                        attempt,
                        payload.get("mode"),
                        payload.get("bytes_copied"),
                    )
                    return True
                logger.warning(
                    "persist_server_runtime: persist returned HTTP %s (attempt %s/%s) "
                    "— retrying in %ss",
                    status,
                    attempt,
                    attempts,
                    backoff_seconds,
                )
                sleep(backoff_seconds)
                continue
        except urllib.error.HTTPError as exc:
            # Reachable server, non-2xx. 404 = older build without the route;
            # 501 = route registered engine-only (DIRECT/MEMORY mode, or no
            # runtime_canonical threaded through run_server). Neither can fold a
            # cross-uid runtime for us, so fall back (return True; the drain still
            # guards harvest). Any other non-2xx = a real persist failure → refuse.
            ever_reachable = True
            if exc.code in (404, 501):
                logger.warning(
                    "persist_server_runtime: %s returned HTTP %s — this server "
                    "either predates the persist route (404) or registered it "
                    "engine-only without a runtime binding (501). Falling back to "
                    "legacy harvest (a cross-uid runtime's writes may be missing "
                    "from the snapshot). Upgrade mercor-mcp-shared on the server "
                    "and thread runtime_canonical through run_server to close this "
                    "gap.",
                    persist_url,
                    exc.code,
                )
                return True
            # Any other non-2xx (e.g. 500 — the WAL was busy and the fold could
            # not complete) is potentially transient. RETRY it like the busy path
            # above and like drain_server_pool does for the same class of error —
            # a single busy checkpoint shouldn't abort populate when the raised
            # error itself tells the operator to retry. Only after all attempts
            # are exhausted does the post-loop check return False (fail closed).
            logger.warning(
                "persist_server_runtime: persist returned HTTP %s %s at %s "
                "(attempt %s/%s) — retrying in %ss",
                exc.code,
                exc.reason,
                persist_url,
                attempt,
                attempts,
                backoff_seconds,
            )
            sleep(backoff_seconds)
            continue
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            # Connection-level failure. Could be "no server" (cold populate) OR a
            # transient blip on a live server. Don't conclude "no server" yet —
            # retry, and let the post-loop check decide based on whether ANY
            # attempt reached the server.
            logger.info(
                "persist_server_runtime: %s not reachable (%s: %s) on attempt %s/%s "
                "— retrying in %ss",
                persist_url,
                type(exc).__name__,
                exc,
                attempt,
                attempts,
                backoff_seconds,
            )
            sleep(backoff_seconds)
            continue

    if not ever_reachable:
        # Every attempt failed to reach the server: treat as "no live server",
        # canonical is already the source of truth (cold populate / server down).
        logger.info(
            "persist_server_runtime: server never reachable at %s after %s attempt(s) "
            "— no live server, canonical is already the source of truth",
            persist_url,
            attempts,
        )
        return True

    # Reached the server at least once but it never returned 200 (busy WAL /
    # non-200 status every time): refuse to harvest a canonical missing writes.
    logger.error(
        "persist_server_runtime: persist never succeeded at %s after %s attempt(s) "
        "— refusing to harvest a canonical missing the server's latest writes",
        persist_url,
        attempts,
    )
    return False


def unlink_stale_runtime(
    canonical: str | os.PathLike[str],
    *,
    set_umask: bool = True,
) -> list[Path]:
    """Delete the stale runtime DB + sidecars BEFORE ``bind_engine`` cold-seeds.

    Cross-uid failure mode this fixes: the live server cold-seeds
    ``/tmp/workspace_runtime_<hash>.db`` as its per-service uid, mode ``0o644``.
    The populate hook runs as a *different* uid (often root). Even after
    ``chmod 0o666`` + a successful ``os.open(O_RDWR)``, SQLite still downgrades
    the connection to read-only — the C library sees residual state from the
    live server's prior open (WAL/SHM ownership, advisory locks) and rejects
    writes with "attempt to write a readonly database".

    The fix is to ``unlink`` the stale runtime + ``-wal`` / ``-shm`` sidecars
    *before* ``bind_engine`` runs its ``cold_seed_runtime``. With the file gone,
    SQLite creates a fresh one owned by the populate process (RW) on first open.
    Linux ``unlink`` semantics preserve any fds the live server still holds (the
    drain's ``engine.dispose()`` should have closed them, but an in-flight
    handler could keep one alive): they keep reading the now-orphan inode while
    new connections see the fresh file.

    Args:
        canonical: The **resolved** canonical DB path (i.e. the value of
            ``DATABASE_PATH``, NOT ``state_dir / "workspace.db"``). The runtime
            path is hashed from this via :func:`runtime_paths_for`; hashing a
            different path (e.g. the state-dir copy, which can diverge from
            ``DATABASE_PATH`` under platform-check deploy legs) computes a
            *different* ``/tmp`` hash and unlinks the wrong file — leaving the
            stale cross-uid-RO runtime exactly where it was.
        set_umask: When ``True`` (default), call ``os.umask(0)`` so the file
            SQLite recreates inherits a wide mode mask, letting the live server
            (a different uid) regain write access via the "other" bits. This is
            a **process-global** side effect; pass ``False`` if the caller
            manages umask itself.

    Returns:
        The list of paths actually unlinked (may be empty on a cold start where
        no stale runtime exists). Useful for post-populate assertions / logs.
    """
    paths = runtime_paths_for(canonical)
    runtime = paths.runtime

    if set_umask:
        # Wide mask so SQLite's mode=0o644 open() lands writable-by-other once
        # the explicit chmod in handoff relaxes it. Process-global by design.
        os.umask(0)

    removed: list[Path] = []
    for stale in (
        runtime,
        runtime.with_name(runtime.name + "-wal"),
        runtime.with_name(runtime.name + "-shm"),
    ):
        try:
            if stale.exists():
                pre = stale.stat()
                stale.unlink()
                logger.info(
                    "unlink_stale_runtime: unlinked stale runtime %s (was "
                    "mode=%s uid=%s size=%s) — clean cross-uid start",
                    stale,
                    oct(pre.st_mode),
                    pre.st_uid,
                    pre.st_size,
                )
                removed.append(stale)
        except OSError as exc:
            logger.warning(
                "unlink_stale_runtime: unlink %s failed (%s: %s) — proceeding with stale file",
                stale,
                type(exc).__name__,
                exc,
            )
    return removed


def handoff_runtime_to_server(
    runtime: str | os.PathLike[str],
    checkpoint_url: str,
    *,
    drain: Callable[..., bool] | None = None,
    **drain_kwargs: object,
) -> bool:
    """Hand a populate-written runtime DB back to the (differently-owned) server.

    Runs AFTER populate has finished writing the runtime DB. Because
    :func:`unlink_stale_runtime` let SQLite recreate the file mid-populate as the
    populate uid (mode ``0o644``), and the ``chmod`` that ran right after binding
    was too early to see the freshly-created ``-wal`` / ``-shm`` sidecars, the
    file may be un-writable by the live server's uid. A root-owned ``0o644``
    runtime downgrades the server's next connection to read-only, and that
    connection then sits **poisoned** in the pool — every later write fails.

    Two ordered, best-effort steps (log, never raise — populate's writes already
    succeeded):

    1. ``chmod 0o666`` the runtime + sidecars **that exist now**, post-write.
    2. Drive one more server-side checkpoint (:func:`drain_server_pool`) so the
       live server disposes its pool and drops the poisoned RO connection; the
       replacement connection opens the now-writable file RW. An unreachable
       server (cold populate, no live reader) has no pool to poison, and
       :func:`drain_server_pool` treats that as success.

    Args:
        runtime: The runtime DB path populate wrote (typically
            ``binding.runtime`` for a RUNTIME-mode binding). Callers should only
            invoke this for RUNTIME-mode bindings — a DIRECT / MEMORY binding has
            no separate tmpfs runtime to hand off.
        checkpoint_url: The live server's ``/_internal/checkpoint`` URL.
        drain: Injectable drain function (defaults to :func:`drain_server_pool`);
            ``drain_kwargs`` are forwarded to it. Primarily a test seam.

    Returns:
        Whether step 2's drain reported success (``True``) or the pool never
        drained (``False``, a poisoned RO connection may persist until restart).
    """
    runtime_path = Path(runtime)
    for path in (
        runtime_path,
        runtime_path.with_name(runtime_path.name + "-wal"),
        runtime_path.with_name(runtime_path.name + "-shm"),
    ):
        try:
            if path.exists():
                os.chmod(path, 0o666)  # noqa: S103 - intentional cross-uid writability
        except OSError as exc:
            logger.warning(
                "handoff_runtime_to_server: chmod %s -> 0o666 failed (%s: %s)",
                path,
                type(exc).__name__,
                exc,
            )

    drain_fn = drain if drain is not None else drain_server_pool
    ok = bool(drain_fn(checkpoint_url, **drain_kwargs))
    if ok:
        logger.info(
            "handoff_runtime_to_server: handoff complete — perms relaxed and "
            "server pool recycled via %s",
            checkpoint_url,
        )
    else:
        logger.warning(
            "handoff_runtime_to_server: server pool never drained; a poisoned "
            "read-only connection may persist until restart",
        )
    return ok


def estimated_post_import_bytes(
    canonical: str | os.PathLike[str],
    state_location: str | os.PathLike[str] | None,
    *,
    csv_growth_factor: float = _CSV_IMPORT_GROWTH_FACTOR,
    sidecar_globs: Iterable[str] = (),
    extra_sidecar_paths: Iterable[str | os.PathLike[str]] = (),
) -> int:
    """Estimated peak tmpfs footprint (bytes) through populate's CSV import.

    Type-aware sum, used pre-bind to decide whether the runtime DB fits the
    tmpfs / RAM budget (if not, the caller forces direct on-disk binding):

    * canonical ``st_size`` counts **1x** (0 when absent — a fresh populate
      starts from a seed-sized or missing canonical);
    * every ``*.csv`` under ``state_location`` counts **``csv_growth_factor``x**
      (default 2x). The ingest commits the whole corpus in one WAL-mode
      transaction, so the WAL transiently holds ~1x the written bytes on top of
      the ~1x resulting rows+indexes. A canonical-size-only check waves a fresh
      populate through (seed canonical < 1 MB) and then the import grows the
      runtime DB inside ``/tmp`` (RAM) to multi-GB — the exact OOM the budget
      exists to prevent;
    * each sidecar (see ``sidecar_globs`` / ``extra_sidecar_paths``) counts 1x.

    Sidecars (e.g. a ``workspace_docvec*.db`` vector DB that is always mirrored
    into tmpfs alongside the runtime) compete for the same RAM budget, so every
    DISTINCT sidecar is **summed** into the estimate — two large sidecars count
    as their combined size, not just the larger. Matches are de-duped by resolved
    path so the same file matched by overlapping globs counts once; a sidecar's
    ``state_location`` copy and its already-relocated ``/tmp`` copy are distinct
    paths and both count (a harmless over-count — see below).

    Over-counting only pushes the binding decision toward on-disk — the safe
    side. Pure ``os.stat`` arithmetic; per-file ``OSError`` tolerated; no
    subprocess, no SQL.
    """
    canonical_path = Path(canonical)
    try:
        total = canonical_path.stat().st_size
    except OSError:
        total = 0  # Absent canonical: a fresh populate starts from zero.

    csv_bytes = 0
    state_path = Path(state_location) if state_location is not None else None
    if state_path is not None:
        for csv_path in state_path.rglob("*.csv"):
            try:
                csv_bytes += csv_path.stat().st_size
            except OSError:
                continue  # Racing rename / prune — best-effort, skip.
    total += int(csv_bytes * csv_growth_factor)

    # Sidecar: each DISTINCT sidecar competes for its own slice of the tmpfs
    # budget, so they must SUM — a single running max across all of them would
    # undercount peak RAM when two or more large sidecars ship together (the
    # dangerous, OOM-prone direction). We de-dupe by resolved path so the same
    # file matched by overlapping globs is counted once; the shipped copy under
    # state_location and its already-relocated /tmp copy are distinct paths and
    # so both count (a harmless over-count — over-counting only pushes the
    # binding decision toward safe on-disk).
    sidecar_sizes: dict[Path, int] = {}
    if state_path is not None:
        for glob in sidecar_globs:
            for sidecar in state_path.rglob(glob):
                try:
                    sidecar_sizes[sidecar.resolve()] = sidecar.stat().st_size
                except OSError:
                    continue
    for extra in extra_sidecar_paths:
        try:
            extra_path = Path(extra)
            sidecar_sizes[extra_path.resolve()] = os.stat(extra).st_size
        except OSError:
            continue

    return total + sum(sidecar_sizes.values())
