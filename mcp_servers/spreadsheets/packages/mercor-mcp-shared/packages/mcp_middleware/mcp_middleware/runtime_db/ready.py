"""Lazy runtime-DB readiness — bind on demand, never proactively at boot.

The classic boot sequence bound the runtime DB **eagerly** at import time
(``db.session`` called :func:`~mcp_middleware.runtime_db.bind_engine` on
first import). That is exactly the behaviour the operator wants gone:

    "Stop creating a DB proactively at boot."

This module is the shared machinery for the replacement contract. Nothing
binds a DB at import. A DB is bound **only** on one of two triggers:

1. A **populate lifecycle signal** — the out-of-process populate / snapshot
   worker delivers a world and pokes the server (``/_internal/enable_db``);
   :func:`~mcp_middleware.runtime_db.adopt_late_delivery` performs the
   *initial* bind (S5).
2. The **first REST / MCP request** that actually needs the DB — a bounded
   synchronous ensure binds it inline (S2's ``ensure_db_ready``) on the
   stdio / MCP arm, or the HTTP arm serves a 503 + ``Retry-After`` from the
   DB gate (default-CLOSED at boot, S4) until a bind completes.

Both triggers funnel through the process-global :class:`EngineHolder`
(operator ruling **D2**: a single shared lazy holder, not a per-app
re-implementation). The holder is a three-state machine:

    UNBOUND ──begin_binding()──▶ BINDING ──publish(binding)──▶ READY
       ▲                            │
       └──────abort_binding()───────┘   (bind failed; a later trigger retries)

* **UNBOUND** — boot state. No engine. Any ``engine()`` access raises
  :class:`DbNotReadyError`; the request arm turns that into a bounded wait
  (stdio) or a 503 (HTTP).
* **BINDING** — exactly one caller won the ``UNBOUND → BINDING`` race and is
  running :func:`bind_engine`. Concurrent callers block in
  :meth:`EngineHolder.wait_ready` until the winner publishes.
* **READY** — a live :class:`~mcp_middleware.runtime_db.EngineBinding` is
  published; ``engine()`` returns it and the DB gate is opened.

Operator ruling **D3** (request-bind is *non-terminal*): reaching READY via
the first-request trigger does not freeze the lifecycle. Late-delivery
adoption still runs against the published binding, the populate trigger can
still fire, and a failed bind falls back to UNBOUND so the next request
retries rather than wedging the process.

This module is deliberately low-level and dependency-light: it imports no
other runtime_db module at load time (``EngineBinding`` is a TYPE_CHECKING
reference; the DB-gate open in :meth:`publish` is a deferred import) so it
sits *below* :mod:`.binding` / :mod:`.checkpoint` in the import graph and
can be consumed by both without a cycle.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from .binding import EngineBinding

__all__ = [
    "DbLifecycleSpec",
    "DbNotReadyError",
    "DbReadyState",
    "EngineHolder",
    "engine_holder",
    "lifecycle_spec",
]


class DbReadyState(Enum):
    """Lifecycle state of the process-global runtime-DB binding.

    Ordering is meaningful only as a progression: a process boots
    ``UNBOUND``, moves to ``BINDING`` while exactly one caller runs
    :func:`bind_engine`, and lands on ``READY`` once a live binding is
    published. A failed bind returns to ``UNBOUND`` (see ruling D3 — a
    request-triggered bind is non-terminal), so the sequence is not a
    strict one-way ratchet.
    """

    #: Boot state. No engine bound; ``engine()`` raises ``DbNotReadyError``.
    UNBOUND = "unbound"
    #: One caller is mid-``bind_engine``; others wait in ``wait_ready``.
    BINDING = "binding"
    #: A live ``EngineBinding`` is published; ``engine()`` returns it.
    READY = "ready"


class DbNotReadyError(RuntimeError):
    """Raised by :meth:`EngineHolder.engine` when no binding is published yet.

    The request arm catches this and translates it into the right shape for
    its transport: a bounded synchronous ensure-then-retry on the stdio /
    MCP arm (ruling D1), or a 503 + ``Retry-After`` on the HTTP arm (the DB
    gate is default-CLOSED at boot, so HTTP callers normally never reach the
    handler to see this at all).
    """


@dataclass(frozen=True)
class DbLifecycleSpec:
    """Everything :func:`ensure_db_ready` needs to bind the runtime DB lazily.

    A server declares this **once** (at import, cheaply — it captures paths
    and callables, it does not touch the DB) and hands it to the lifecycle
    wiring. The actual :func:`bind_engine` call is deferred until the first
    trigger fires. Keeping the spec frozen makes it safe to stash on the
    process-global holder / app state and read from any thread.

    Attributes:
        canonical: The receive path passed straight to
            :func:`~mcp_middleware.runtime_db.bind_engine`. ``None`` /
            ``":memory:"`` selects memory mode.
        world_tables: Optional world-table names forwarded to
            ``bind_engine(world_tables=...)`` so the stale-sentinel self-heal
            probes real world data (not catalogue scaffolding) when deciding
            whether a sentinel-carrying canonical is a genuine delivery.
        cold_init: Optional callable invoked with the freshly bound
            :class:`sqlalchemy.Engine` **only** when the bind produced a blank
            world (``binding.awaiting_delivery``). This is where an app runs
            its ``Base.metadata.create_all`` (plus any idempotent migrations /
            derived-data rebuild) so a cold-started server has a *queryable
            schema* before the real world lands. Skipped for a delivered DB
            (the schema is already present) so it never writes an empty schema
            over a pending delivery.

            **CONTRACT — schema/bootstrap ONLY, never a world-data seed.** A
            ``cold_init`` MUST NOT ``INSERT`` rows into any table the populate
            facade imports (roles, default-user identity, and other CSV-backed
            world data are populate-owned). Under the no-proactive-bind
            lifecycle a live-then-populate deployment binds blank and runs
            ``cold_init`` on the **same in-place DB file** that ``populate``
            then imports into — so a world-data ``INSERT`` here collides with
            the import (e.g. ``UNIQUE`` on ``roles.name``). Populate is the
            sole authority for world data. If a table genuinely needs a
            bootstrap row to serve *before* delivery, make the seed idempotent
            (``INSERT OR IGNORE`` / upsert) OR mark it
            ``import_strategy="clear"`` so populate stays clear-authoritative.
            Default-user identity is handled by the shared identity gate, not
            by ``cold_init``. (Corollary: whitelisted liveness probes such as
            ``/health`` must not touch the DB — a whitelisted path is a gate
            *bypass*, not a bind trigger — so they never provoke a pre-populate
            ``cold_init`` in the first place.)
        deliver_dir: Optional snapshot-export directory override, forwarded to
            ``bind_engine(deliver_dir=...)`` (defaults to ``MCP_SNAPSHOT_DIR``
            / the receive path's directory).
        attach_fts_sidecar: Forwarded to ``bind_engine`` — attach a
            co-located ``<db>.fts.db`` sidecar on connect if present. Default
            ``True`` (detection-only; a no-op for FTS-free apps).
        create_fts_sidecar: Forwarded to ``bind_engine`` — ATTACH even when
            the sidecar file is absent (the build path). Default ``False``.
        create_engine_kwargs: Forwarded verbatim to
            :func:`sqlalchemy.create_engine` via ``bind_engine`` (e.g.
            ``{"poolclass": StaticPool, "connect_args": {"check_same_thread":
            False}}`` for a cross-thread memory-mode test engine). ``None``
            (default) lets ``bind_engine`` apply its ``future=True`` default.
        identity_seed: **Blank-world fallback default-user seed (ruling D4).**
            An optional callable invoked with a committed
            :class:`sqlalchemy.orm.Session` to seed a single *fallback* identity
            so a world that **never had a drop** can serve best-effort instead
            of 401ing on ``actor=None``. Fires only on the request-driven bind,
            AFTER ``cold_init`` and BEFORE the gate opens, and only when the full
            predicate in
            :func:`~mcp_middleware.runtime_db.identity_seed.should_seed_fallback`
            holds (not delivered, no drop configured/pending via STATE_LOCATION
            emptiness + ``populate_in_progress``, enforcement on, table empty).
            ``None`` (default) means no fallback — behaviour is unchanged.
            Single-table apps can pass
            :func:`~mcp_middleware.runtime_db.seed_fallback_default_user`; apps
            with FK chains supply their own multi-row callback.
        identity_table: Name of the single-row default-user identity table, used
            only for the fallback predicate's empty-check. Defaults to
            ``"default_users"`` (the identity gate's default). Ignored when
            ``identity_seed`` is ``None``.
        enforce_default_user: The app's in-code enforcement stance, forwarded to
            :func:`~mcp_middleware.default_user_enforced` (``MCP_ENFORCE_DEFAULT_USER``
            env still takes precedence). ``None`` (default) defers to env / the
            global default. Governs whether the fallback fires — an unenforced
            empty world serves empty and is never seeded.
    """

    canonical: str | os.PathLike[str] | None
    world_tables: Sequence[str] | None = None
    cold_init: Callable[[Engine], None] | None = None
    deliver_dir: str | os.PathLike[str] | None = None
    attach_fts_sidecar: bool = True
    create_fts_sidecar: bool = False
    create_engine_kwargs: dict[str, object] | None = None
    #: D4 fallback seed — see the class docstring and
    #: :mod:`mcp_middleware.runtime_db.identity_seed`.
    identity_seed: Callable[[Session], None] | None = None
    identity_table: str = "default_users"
    enforce_default_user: bool | None = None


class EngineHolder:
    """Process-global home for the lazily-bound runtime-DB binding (ruling D2).

    One instance lives per process (see :func:`engine_holder`). It owns the
    :class:`DbReadyState` transition and the published
    :class:`~mcp_middleware.runtime_db.EngineBinding`, guarded by a single
    :class:`threading.Condition` so that:

    * the ``UNBOUND → BINDING`` race has exactly one winner
      (:meth:`begin_binding`), and
    * concurrent losers block in :meth:`wait_ready` until the winner
      :meth:`publish`\\ es (bounded — ruling D1's synchronous ensure never
      waits forever).

    All mutating transitions take the lock; ``engine()`` /  ``binding`` take
    it too so a reader never observes a half-updated ``(state, binding)``
    pair. :meth:`is_ready` is the one lock-free fast-path read (a stale
    ``False`` costs at most one extra ensure round-trip, which is harmless).
    """

    def __init__(self) -> None:
        # A Condition wrapping an RLock: publish()/abort_binding() notify;
        # wait_ready() blocks. RLock so a caller already holding the lock
        # (e.g. a future re-entrant transition) doesn't self-deadlock.
        self._cond = threading.Condition(threading.RLock())
        self._state: DbReadyState = DbReadyState.UNBOUND
        self._binding: EngineBinding | None = None
        self._spec: DbLifecycleSpec | None = None
        # Populate bind lock-out (Finding 1). While True, begin_binding refuses
        # to claim so a populate/snapshot that is rewriting the DB file can't
        # race a request-triggered bind. Set by lock_out_binding() from the
        # in-process disable_db route, cleared by allow_binding() from enable_db.
        self._bind_locked: bool = False

    # ── reads ──────────────────────────────────────────────────────────
    @property
    def state(self) -> DbReadyState:
        """Current lifecycle state (locked snapshot)."""
        with self._cond:
            return self._state

    @property
    def spec(self) -> DbLifecycleSpec | None:
        """The declarative bind recipe registered via ``register_db_lifecycle``.

        Stashed once at registration so every trigger arm can bind without
        threading the spec through call sites — in particular the populate
        signal arm (``adopt_late_delivery``'s initial bind), which runs deep
        in the ``/_internal/enable_db`` route with no spec in scope. ``None``
        until :meth:`set_spec` is called (memory-mode / legacy servers that
        never register a lifecycle).
        """
        with self._cond:
            return self._spec

    def set_spec(self, spec: DbLifecycleSpec) -> None:
        """Register the bind recipe. Called once, before any trigger fires."""
        with self._cond:
            self._spec = spec

    def is_ready(self) -> bool:
        """Lock-free fast check: is a binding published?

        Cheap enough to call on the hot request path before deciding whether
        to enter the (locked) ensure flow. A racing stale ``False`` merely
        triggers one extra ``ensure_db_ready`` which no-ops on the re-check.
        """
        return self._state is DbReadyState.READY

    @property
    def binding(self) -> EngineBinding | None:
        """The published binding, or ``None`` if not READY yet."""
        with self._cond:
            return self._binding

    def engine(self) -> Engine:
        """Return the live engine, or raise :class:`DbNotReadyError`.

        The single access point the request arm uses. When the holder is not
        READY this raises rather than lazily binding — binding is the caller's
        job (``ensure_db_ready``), so this stays a pure accessor with no side
        effects and no surprise blocking.
        """
        with self._cond:
            if self._state is not DbReadyState.READY or self._binding is None:
                raise DbNotReadyError(
                    f"runtime DB is not ready (state={self._state.value}); "
                    "no engine bound yet. The first-request ensure or the "
                    "populate late-delivery adopt binds it."
                )
            return self._binding.engine

    # ── transitions ────────────────────────────────────────────────────
    def begin_binding(self) -> bool:
        """Atomically claim the ``UNBOUND → BINDING`` transition.

        Returns ``True`` for the single caller that won the race (it must then
        run :func:`bind_engine` and call :meth:`publish` or
        :meth:`abort_binding`). Returns ``False`` for everyone else — already
        BINDING (another caller is mid-bind), already READY, or a populate
        bind lock-out is in force (:meth:`lock_out_binding`). Losers should
        :meth:`wait_bindable_or_ready` rather than bind a second engine.
        """
        with self._cond:
            if self._state is DbReadyState.UNBOUND and not self._bind_locked:
                self._state = DbReadyState.BINDING
                return True
            return False

    def publish(self, binding: EngineBinding) -> None:
        """Install ``binding`` and transition to READY, then open the DB gate.

        Idempotent-safe to call from the ``begin_binding`` winner. Wakes every
        :meth:`wait_ready` waiter. The DB gate is opened *after* the state is
        visible (deferred import to keep this module dependency-light and
        below :mod:`.db_gate` at load time) so a waiter released by the notify
        finds both READY state and open gate.

        The gate is opened via :func:`~mcp_middleware.runtime_db.db_gate.open_gate_after_bind`,
        which is a no-op while a populate window owns the DB: a bind that was
        in flight when ``disable_db`` closed the gate must not reopen it (the
        populate worker owns the file until ``enable_db``). On the ordinary
        first-bind path this opens the gate exactly as before.
        """
        with self._cond:
            self._binding = binding
            self._state = DbReadyState.READY
            self._cond.notify_all()
        # Open the gate outside the lock: db_gate has its own lock, and we
        # never want to hold two locks at once. Deferred import avoids a
        # load-time dependency (ready.py sits below db_gate in the graph).
        from .db_gate import open_gate_after_bind

        open_gate_after_bind()

    def abort_binding(self) -> None:
        """Roll ``BINDING → UNBOUND`` back after a failed bind (ruling D3).

        A request-triggered bind is non-terminal: if :func:`bind_engine`
        raises, the winner calls this so the next trigger (a retried request
        or the populate signal) can attempt the bind afresh instead of the
        process wedging in BINDING forever. No-op unless currently BINDING.
        Wakes waiters so they re-observe UNBOUND and can re-race.
        """
        with self._cond:
            if self._state is DbReadyState.BINDING:
                self._state = DbReadyState.UNBOUND
                self._cond.notify_all()

    def wait_ready(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds for the state to reach READY.

        Returns ``True`` if READY (either already, or reached before the
        deadline), ``False`` on timeout. Used by the bounded synchronous
        ensure (ruling D1): a loser of the ``begin_binding`` race waits here
        for the winner instead of binding a duplicate engine. A ``timeout``
        of ``0`` is a non-blocking poll.

        The wait is deadline-based (not a single ``Condition.wait(timeout)``)
        so a spurious wake or an ``abort_binding`` bounce-back doesn't reset
        the full budget — the caller gets at most ``timeout`` seconds total.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._state is not DbReadyState.READY:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._state is DbReadyState.READY
                self._cond.wait(remaining)
            return True

    def wait_bindable_or_ready(self, timeout: float) -> DbReadyState:
        """Block until READY, or claimable (UNBOUND & unlocked), or timeout.

        The re-race primitive for :func:`ensure_db_ready` (Finding 2). A loser
        of the ``begin_binding`` race calls this instead of :meth:`wait_ready`
        so that when the winning bind *fails* (:meth:`abort_binding` →
        ``UNBOUND``) the loser wakes and returns promptly to re-attempt the
        claim, rather than hanging the full ``timeout`` waiting for a READY
        that will never come. It returns as soon as the state settles to
        something actionable:

        * ``READY`` — a binding was published; the caller returns it.
        * ``UNBOUND`` (and no populate lock-out) — the previous binder aborted
          or never started; the caller re-races :meth:`begin_binding`.

        It keeps waiting while ``BINDING`` (another caller is mid-bind) and
        while a populate :meth:`lock_out_binding` holds an ``UNBOUND`` holder
        closed (so a loser doesn't busy-spin re-racing a claim that
        :meth:`begin_binding` will only refuse). Returns the current state on
        timeout. Deadline-based so a spurious wake doesn't reset the budget.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                if self._state is DbReadyState.READY:
                    return DbReadyState.READY
                if self._state is DbReadyState.UNBOUND and not self._bind_locked:
                    return DbReadyState.UNBOUND
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._state
                self._cond.wait(remaining)

    def lock_out_binding(self, timeout: float = 0.0) -> bool:
        """Forbid new binds and wait for any in-flight bind to settle (Finding 1).

        Called by the in-process ``/_internal/disable_db`` route so a
        populate/snapshot that is about to rewrite the canonical DB file cannot
        race a request-triggered bind. Sets the lock-out — :meth:`begin_binding`
        refuses to claim while it is set — then blocks up to ``timeout`` seconds
        for a current ``BINDING`` to reach ``READY`` / ``UNBOUND`` so the worker
        never starts writing over a bind that is still opening the file.

        Returns ``True`` if no bind is in flight (settled or never started),
        ``False`` if a bind is still ``BINDING`` at the deadline (pathological;
        the caller should log and proceed — the gate is already closed). Paired
        with :meth:`allow_binding` from the enable_db route. The lock-out itself
        is set even on the ``False`` path so no *new* bind can start.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            self._bind_locked = True
            self._cond.notify_all()
            while self._state is DbReadyState.BINDING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def allow_binding(self) -> None:
        """Clear the populate bind lock-out and wake waiters (Finding 1).

        Paired with :meth:`lock_out_binding`; called by the ``enable_db`` route
        before its Trigger-1 adopt so the initial bind (and subsequent
        request-triggered binds) can proceed once populate has finished writing
        the canonical. Wakes any :meth:`wait_bindable_or_ready` waiter parked on
        the lock-out so it re-races promptly. No-op if not currently locked.
        """
        with self._cond:
            self._bind_locked = False
            self._cond.notify_all()

    def reset(self) -> None:
        """Drop back to UNBOUND and forget the binding. **Test-only.**

        Does NOT dispose the binding's engine (the caller owns that) and does
        NOT touch the DB gate. Production code never resets a published
        holder; this exists so a test can reuse the process-global singleton
        across cases without leaking a prior binding. Wakes any waiters.
        """
        with self._cond:
            self._state = DbReadyState.UNBOUND
            self._binding = None
            self._spec = None
            self._bind_locked = False
            self._cond.notify_all()


# Process-global singleton. One runtime DB per process → one holder. Tests
# that need isolation call ``engine_holder().reset()`` rather than
# constructing their own (the request arm and the lifecycle routes all read
# this singleton, so a fresh instance wouldn't be seen by them).
_HOLDER = EngineHolder()


def engine_holder() -> EngineHolder:
    """Return the process-global :class:`EngineHolder` (ruling D2)."""
    return _HOLDER


def lifecycle_spec() -> DbLifecycleSpec | None:
    """Return the process-global lifecycle :class:`DbLifecycleSpec`, if wired.

    Single-source accessor for the declarative bind recipe stashed on the
    holder by :func:`register_db_lifecycle`. ``run_server`` derives the
    identity gate's canonical from ``lifecycle_spec().canonical`` so an app
    declares the canonical path exactly once (in the spec) rather than
    threading ``runtime_canonical`` separately. Returns ``None`` before any
    lifecycle is registered.
    """
    return _HOLDER.spec
