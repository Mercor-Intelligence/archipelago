"""Bounded synchronous ensure — bind the runtime DB on demand (S2).

This is the driver that turns a declarative :class:`DbLifecycleSpec` into a
live, published :class:`~mcp_middleware.runtime_db.EngineBinding`, lazily, on
the first trigger that needs the DB. It is the counterpart to the state
machine in :mod:`.ready`: the holder tracks *what state* the binding is in;
:func:`ensure_db_ready` is *how it gets to READY*.

Where the bind decision moved
-----------------------------

The classic boot bound the DB eagerly at import (``db.session`` called
:func:`~mcp_middleware.runtime_db.bind_engine` the first time it was
imported). That ran :func:`bind_engine`'s **delivered vs awaiting-delivery
classification** at *import time* — before the world download had a chance to
land — which is the structural root of the PSP mis-read (a boot-before-
delivery imported a canonical-absent world, classified it ``delivered=False``,
and that provenance stuck). Nothing in this module runs at import. The
classification still lives inside :func:`bind_engine` (it is the right owner —
it has the file in hand), but it now runs **only when ensure_db_ready is
first called**, i.e. on a real request or the populate signal, by which point
the canonical is present and the classification is correct.

Ruling D1 — bounded synchronous ensure
--------------------------------------

On the stdio / MCP arm the first request cannot 503 (there is no HTTP status
to return), so it must *block* until the DB is ready. This function is that
block, made safe and bounded:

* The first caller to arrive wins the ``UNBOUND → BINDING`` race
  (:meth:`EngineHolder.begin_binding`), runs :func:`bind_engine` exactly once,
  optionally runs the spec's blank-world ``cold_init`` schema hook, and
  :meth:`~EngineHolder.publish`\\ es the binding.
* Every concurrent caller loses the race and blocks in
  :meth:`~EngineHolder.wait_bindable_or_ready` for up to ``timeout`` seconds —
  they never bind a second engine. When the winner publishes they wake and
  return the same binding.
* If ``bind_engine`` (or ``cold_init``) raises, the winner
  :meth:`~EngineHolder.abort_binding`\\ s back to UNBOUND (ruling D3 — a
  request-triggered bind is non-terminal) and re-raises, so the next request
  retries instead of the process wedging in BINDING forever. A concurrent
  loser waiting on that bind wakes on the abort and **re-races the claim**
  within its remaining budget rather than stalling the full ``timeout`` — so a
  transient bind failure costs one attempt, not a 30s hang for every waiter.
* If the wait times out (a genuinely stuck winner), a :class:`DbNotReadyError`
  is raised rather than blocking indefinitely — the bound is real.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .binding import bind_engine
from .identity_seed import invoke_identity_seed, should_seed_fallback
from .ready import DbLifecycleSpec, DbNotReadyError, DbReadyState, EngineHolder, engine_holder

if TYPE_CHECKING:
    from .binding import EngineBinding

__all__ = [
    "DEFAULT_ENSURE_TIMEOUT",
    "ensure_db_ready",
]

#: Default upper bound (seconds) a losing caller waits for the winning caller
#: to finish binding. Binding itself is fast (open engine + a writability
#: probe); ``cold_init`` schema creation on a blank world is the only variable
#: cost and is still sub-second in practice. 30s is a generous ceiling that
#: still guarantees a stuck bind surfaces as an error rather than an infinite
#: hang. Callers on a latency-sensitive path may pass a tighter ``timeout``.
DEFAULT_ENSURE_TIMEOUT = 30.0


def ensure_db_ready(
    spec: DbLifecycleSpec,
    *,
    holder: EngineHolder | None = None,
    timeout: float = DEFAULT_ENSURE_TIMEOUT,
) -> EngineBinding:
    """Return a READY binding for ``spec``, binding it now if needed.

    Idempotent and safe under concurrency: the first call binds, subsequent
    calls (or concurrent callers) return the already-published binding. See
    the module docstring for the full D1/D3 contract.

    Args:
        spec: The declarative bind contract (canonical path, world tables,
            cold-init hook, FTS-sidecar flags, engine kwargs).
        holder: The :class:`EngineHolder` to bind against. Defaults to the
            process-global :func:`engine_holder` singleton; tests pass an
            isolated instance.
        timeout: Seconds a losing caller waits for the winning caller to
            publish before raising :class:`DbNotReadyError`. Defaults to
            :data:`DEFAULT_ENSURE_TIMEOUT`.

    Returns:
        The live :class:`~mcp_middleware.runtime_db.EngineBinding`.

    Raises:
        DbNotReadyError: If a losing caller's ``wait_ready`` times out.
        Exception: Anything :func:`bind_engine` or ``spec.cold_init`` raises
            is re-raised to the *winning* caller after rolling the holder back
            to UNBOUND (so a retry re-attempts the bind).
    """
    h = holder if holder is not None else engine_holder()
    deadline = time.monotonic() + max(0.0, timeout)

    # Deadline-bounded claim/wait loop (Finding 2). A losing caller that wakes
    # to a FAILED bind (the winner aborted → UNBOUND) re-races the claim within
    # the remaining budget instead of hanging the full timeout waiting for a
    # READY that will never arrive. Each bind failure costs exactly one attempt;
    # only one loser re-claims per abort (the rest keep waiting on the new
    # BINDING), so the loop converges and never busy-spins.
    while True:
        # Fast path: already bound. A racing stale miss here just falls through
        # to the claim/wait below, which self-corrects.
        if h.is_ready():
            existing = h.binding
            if existing is not None:
                return existing

        # Claim the bind. Exactly one concurrent caller wins.
        if h.begin_binding():
            try:
                binding = bind_engine(
                    spec.canonical,
                    deliver_dir=spec.deliver_dir,
                    create_engine_kwargs=spec.create_engine_kwargs,
                    attach_fts_sidecar=spec.attach_fts_sidecar,
                    create_fts_sidecar=spec.create_fts_sidecar,
                    world_tables=spec.world_tables,
                )
            except BaseException:
                # Bind failed — roll back so the next trigger retries (D3).
                h.abort_binding()
                raise

            # Blank-world cold init: create the SCHEMA (idempotent bootstrap
            # ONLY — never a world-data seed; a cold_init must not INSERT into
            # tables the populate facade imports, or it collides with populate
            # on the same in-place DB. See DbLifecycleSpec.cold_init) so a
            # server that bound before its world landed still has a queryable
            # DB. Gate on ``not delivered`` — that is True for memory mode AND
            # the blank in-place bind, and False for a real delivery (whose
            # schema is already present, so we must not write over it).
            if spec.cold_init is not None and not binding.delivered:
                try:
                    spec.cold_init(binding.engine)
                except BaseException:
                    # cold_init failed — dispose the half-bound engine and roll
                    # back so a retry rebinds cleanly rather than publishing a
                    # binding whose schema init never completed.
                    try:
                        binding.engine.dispose()
                    except Exception:  # noqa: BLE001 - dispose failure mustn't mask the real error
                        pass
                    h.abort_binding()
                    raise

            # Blank-world fallback identity seed (D4). On this request-driven
            # bind of a world that never had a drop (see should_seed_fallback:
            # not delivered, no drop configured/pending via STATE_LOCATION +
            # populate_in_progress, enforcement on, table empty), seed a single
            # fallback identity so the server serves best-effort instead of
            # 401ing on actor=None. Runs AFTER cold_init (schema present) and
            # BEFORE publish (the gate opens) so the very request that triggered
            # this bind already sees the row. Same rollback discipline as
            # cold_init — a failed seed must not publish a half-seeded binding.
            if should_seed_fallback(spec, binding):
                try:
                    invoke_identity_seed(spec, binding.engine)
                except BaseException:
                    try:
                        binding.engine.dispose()
                    except Exception:  # noqa: BLE001 - dispose failure mustn't mask the real error
                        pass
                    h.abort_binding()
                    raise

            h.publish(binding)
            return binding

        # Lost the race: another caller is binding (or already did), or a
        # populate lock-out holds the holder closed. Wait until the state
        # settles to something actionable — READY (return it) or UNBOUND &
        # unlocked (re-race) — or the deadline lapses.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        settled = h.wait_bindable_or_ready(remaining)
        if settled is DbReadyState.READY:
            published = h.binding
            if published is not None:
                return published
        # settled is UNBOUND (& unlocked) → loop to re-race; or timed out with
        # a non-actionable state → the next iteration's remaining<=0 breaks.

    raise DbNotReadyError(
        f"runtime DB did not become ready within {timeout}s "
        f"(holder state={h.state.value}); the binding caller may be stuck. "
        "Retry the request; a request-triggered bind is non-terminal."
    )
