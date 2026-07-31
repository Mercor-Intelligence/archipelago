"""HTTP-layer DB gate — block traffic while the runtime DB is unstable.

This module provides a process-global "is the runtime DB safe to touch?"
flag plus the Starlette middleware that enforces it. The semantic is
deliberately broad: when the gate is closed, **the DB is unavailable
right now and handlers should not touch it**. The canonical use case is
populate / snapshot lifecycle work that mutates the runtime DB file
inode out from under live SQLAlchemy connections, but the same machinery
covers anything that needs the runtime DB file stable for a window —
restore-from-backup, schema migrations, cold-storage promotions.

How the pieces fit together
---------------------------

* :func:`is_db_disabled` / :func:`set_db_disabled` — the primitives.
  Process-global boolean guarded by a :class:`threading.Lock`. Flips are
  atomic; reads are lock-free (the cost of a stale read on the request
  path is one extra request served against a DB that just became
  available, or a 503 that should have passed — both acceptable).
* :class:`DbGateMiddleware` — Starlette ``BaseHTTPMiddleware`` that
  consults the flag on every request and short-circuits gated requests
  with 503 + ``Retry-After``. Whitelisted paths pass through with
  ``request.state.db_disabled = True`` so handlers (typically health
  probes) can adapt — e.g. skip a DB ping and return liveness only.
* :data:`DEFAULT_WHITELIST` — the paths that MUST stay reachable while
  the gate is closed: ``/health`` for orchestrators, ``/_internal/*`` for
  the lifecycle scripts that own the gate.

Sticky-closed-on-failure is intentional
---------------------------------------

When a lifecycle script crashes mid-run (populate dies after the
canonical is half-overwritten, snapshot exits before swapping the new
runtime in, etc.) the runtime DB is in an indeterminate state. There is
no watchdog that auto-clears the flag after a timeout, and no fallback
"if no enable_db within N seconds, assume the script crashed and reopen
traffic". That behaviour would be **wrong** — serving requests against
an indeterminate DB masks the real problem and produces hard-to-debug
data corruption downstream. A stuck-503 stream is a feature: operators
see it, page the on-call, and investigate. The lifecycle script's
``finally`` block (or a ``trap`` in bash) is the *only* mechanism that
should clear the flag, and only on a clean exit from the script's
recovery path.

Operator wiring guidance
------------------------

Register :class:`DbGateMiddleware` **first** (outermost) in the app's
middleware stack so requests are gated *before* any other middleware
gets a chance to touch the DB. Starlette runs middleware
last-added-runs-first, so the call order is::

    # Other middleware first (they end up "inside" the gate)...
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(AuthMiddleware)

    # ...and the gate last, so it runs outermost.
    app.add_middleware(DbGateMiddleware)

For FastMCP-shaped apps, follow the equivalent "register the gate so it
wraps everything else" ordering in whatever middleware-stacking API the
app exposes.

Health check handler pattern
----------------------------

A health probe that hits the DB will 503 itself when the gate closes,
which defeats the purpose of /health. Branch on ``request.state``::

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        if getattr(request.state, "db_disabled", False):
            # Skip DB probe; return liveness only.
            return JSONResponse({"status": "ok", "db_disabled": True})
        # Normal path: probe the DB.
        ...

Lifecycle script pattern
------------------------

Always wrap the work in a try/finally so a clean exit clears the gate.
A failure leaving the gate closed is correct — see the sticky-closed
discussion above. Bash::

    curl -fsS -X POST "http://localhost:$PORT/_internal/disable_db" || exit 1
    trap 'curl -fsS -X POST "http://localhost:$PORT/_internal/enable_db" || true' EXIT
    python -m populate

Python::

    requests.post(f"{base}/_internal/disable_db").raise_for_status()
    try:
        run_populate()
    finally:
        try:
            requests.post(f"{base}/_internal/enable_db", timeout=5)
        except Exception:
            pass  # sticky-closed-on-failure is correct
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp

__all__ = [
    "DEFAULT_WHITELIST",
    "DbGateMiddleware",
    "db_gate_epoch",
    "is_db_disabled",
    "set_db_disabled",
]


# Paths that MUST remain reachable while the gate is closed. Health probes
# (so orchestrators don't kill the pod mid-populate) and the /_internal/*
# routes that own the gate itself (so the lifecycle script can open / close
# / drain). Literal exact-string match — no prefixes, regex, or globs.
DEFAULT_WHITELIST: frozenset[str] = frozenset(
    {
        "/health",
        "/_internal/disable_db",
        "/_internal/enable_db",
        "/_internal/checkpoint",
        "/_internal/persist",
        "/_internal/db-path",
        "/_internal/populate",
    }
)


# ---------------------------------------------------------------------------
# Module-level flag (per-process)
# ---------------------------------------------------------------------------

# Guards writes to ``_db_disabled``. Reads are lock-free — the worst
# observable outcome is a one-request delay before the flip is visible,
# which is harmless for both directions: a request served just before
# the gate closes is fine, and a 503 served just after it opens is
# self-correcting on retry.
_lock = threading.Lock()
_db_disabled = False

# Monotonic counter bumped on every close (open→closed transition). It exists so
# a consumer that *caches* a positive DB observation (e.g. the default-user
# identity gate's open latch) can tell whether a disable/enable cycle happened
# since it cached — even if that cycle completed without the consumer running.
# A closed gate means the runtime DB may have been swapped underneath; anything
# latched before that close must re-verify. Reads are lock-free (a stale read
# costs at most one extra re-verify, which is harmless).
_db_epoch = 0


def is_db_disabled() -> bool:
    """Return ``True`` if the runtime DB is currently gated off.

    Lock-free read. Use this from request handlers, health probes, or
    anywhere else you want to early-bail before touching the DB. The
    middleware itself uses it on every request.
    """
    return _db_disabled


def db_gate_epoch() -> int:
    """Return the monotonic close-epoch (see ``_db_epoch``).

    Bumped once per open→closed transition. A consumer caches this alongside a
    positive DB observation and re-verifies when it changes, so a DB swap that
    happened entirely within a disable→enable window (no consumer call in
    between) can't leave a stale cache serving the swapped-out DB. Lock-free.
    """
    return _db_epoch


def set_db_disabled(active: bool) -> None:
    """Flip the gate. ``True`` closes (gates traffic), ``False`` opens.

    Atomic under ``_lock``. Safe to call from any thread, including from
    inside a request handler that wants to gate itself off (e.g. a
    handler that detects DB corruption mid-request and wants to slam the
    door behind it). Idempotent — re-setting the same value is a no-op
    semantically though it still takes the lock.

    Each genuine open→closed transition bumps :func:`db_gate_epoch` so cached
    positive observations can be invalidated even if no consumer ran during the
    closed window.
    """
    global _db_disabled, _db_epoch
    with _lock:
        if bool(active) and not _db_disabled:
            _db_epoch += 1
        _db_disabled = bool(active)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class DbGateMiddleware(BaseHTTPMiddleware):
    """Short-circuit requests when :func:`is_db_disabled` is ``True``.

    Behaviour per request:

    * Gate open (default): pass through unchanged.
    * Gate closed AND path in the whitelist: set
      ``request.state.db_disabled = True`` and pass through. Handlers
      can branch on the attribute to skip DB work (the canonical case is
      ``/health`` returning liveness without a DB ping).
    * Gate closed and path NOT in the whitelist: return ``503`` with
      body ``{"error": "db_disabled"}`` and a ``Retry-After`` header.

    Args:
        app: The inner ASGI app (passed by Starlette).
        whitelist: Iterable of literal paths that pass through while the
            gate is closed. Defaults to :data:`DEFAULT_WHITELIST`. Match
            semantics are exact string equality on ``request.url.path``
            — no prefix matching, no regex, no globs. If you want a
            sub-tree (``/admin/*``) reachable, list every concrete path
            explicitly.
        retry_after_seconds: Value emitted in the ``Retry-After`` header
            on gated 503s. Clients / load balancers use this as a hint
            for backoff. Defaults to 30; tune to the expected duration
            of your lifecycle work.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        whitelist: Iterable[str] | None = None,
        retry_after_seconds: int = 30,
    ) -> None:
        super().__init__(app)
        # Freeze the whitelist into a set for O(1) membership checks
        # AND to defend against callers mutating the iterable after
        # construction. ``frozenset`` is enough — we never need to add
        # to it at runtime.
        self._whitelist: frozenset[str] = (
            frozenset(whitelist) if whitelist is not None else DEFAULT_WHITELIST
        )
        self._retry_after_seconds = int(retry_after_seconds)

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        # Fast path: gate open, pass through with zero state mutation.
        if not is_db_disabled():
            return await call_next(request)

        # Gate closed. Whitelisted paths get an explicit signal so they
        # can adapt; everyone else gets a 503.
        if request.url.path in self._whitelist:
            request.state.db_disabled = True
            return await call_next(request)

        return JSONResponse(
            {"error": "db_disabled"},
            status_code=503,
            headers={"Retry-After": str(self._retry_after_seconds)},
        )
