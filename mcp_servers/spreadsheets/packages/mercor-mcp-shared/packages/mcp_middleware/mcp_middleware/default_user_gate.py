"""Mandatory default-user identity gate — refuse work until an identity exists.

Mercor MCP apps simulate an authenticated caller via a single-row "default
user" table, seeded during populate from a ``default_user.csv``. A default user
is mandatory: the auth layer resolves the caller identity from that row. If the
server serves traffic with an empty table, every request runs with *no*
identity — a silent, dangerous hole.

Why a gate, not a boot-time raise
---------------------------------

A raise at startup deadlocks two real topologies:

* **Boot-before-populate.** The platform health-checks the port *before*
  populate delivers the seed. A raise means the port never goes healthy →
  populate never runs → the seed never arrives.
* **Populate-less.** Some deployments never run populate; they point at an
  already-initialized ``workspace.db``. There is no populate step to gate on.

So identity is enforced as a **runtime gate**: the server boots normally, but
operations are refused until the table holds a row. The check is *live*
(:func:`mcp_middleware.default_user_present`), never signal-driven — it opens
the instant populate seeds the row and re-closes if a DB swap brings in an
empty table, needing zero new signaling.

Enforced vs corrupt — two different closes
------------------------------------------

The gate distinguishes a genuinely-*unconfigured* world from a *corrupt* one:

* **Missing / empty** (no row, or the identity FK is null/empty) is a legitimate
  cold-start world. It closes the gate only when enforcement is on
  (:func:`mcp_middleware.default_user_enforced`); with enforcement off, an empty
  identity table serves.
* **Dangling** (a row whose FK is *set* but resolves to no user — detectable only
  when a :class:`~mcp_middleware.DefaultUserRef` is supplied) is a corrupt state
  from a bad populate/UPDATE. It **always** fails closed, regardless of the
  enforcement flag: disabling enforcement is for empty worlds, not for serving on
  top of a broken reference.

``decide()`` returns which case applies so each arm can report a distinct error
(``db_disabled`` / ``default_user_not_configured`` / ``default_user_dangling_reference``).

Two arms, one predicate
------------------------

The open predicate — ``NOT is_db_disabled()`` AND a resolvable identity (or an
empty world when unenforced, never a dangling one) — is enforced on BOTH
surfaces, because the real Studio runtime is STDIO where HTTP middleware never
runs:

* a FastMCP ``on_call_tool`` middleware (covers MCP tool calls under every
  transport), and
* a Starlette HTTP middleware (the primary surface agents exercise over REST).

The gate is **additive** to any existing DB gate (:class:`DbGateMiddleware`,
inode-stability only) and scope middleware — it closes the identity hole those
don't.

The latch
---------

Evaluating a ``COUNT(*)`` on every tool call and every request is wasteful once
an identity exists. The gate latches: while the row is *absent* it probes live
every call (so boot→populate self-heals the moment the row lands); once it
observes a row it latches open, keyed on the **identity of the engine object**
returned by the provider AND the **db-gate close-epoch**
(:func:`mcp_middleware.runtime_db.db_gate.db_gate_epoch`) at latch time. An app
that rebinds its engine on a DB swap (dispose + re-bind) yields a *new* engine
object → the latch misses → one fresh probe. And any close of the process-global
DB gate (:func:`set_db_disabled`) bumps the epoch, so a disable→enable cycle
invalidates the latch even if it completed entirely within the closed window
(only bypassed ``/_internal/*`` routes ran, so ``serve()`` never fired during it)
— the next ``serve()`` sees the advanced epoch and re-probes.

Assumed invariant
~~~~~~~~~~~~~~~~~~

The open latch's soundness rests on: *a default-user row going present→absent
always coincides with either an engine-object rebind, a* :func:`set_db_disabled`
*close (which lifecycle scripts always issue around a runtime swap), or a
canonical the freshness check below detects as drifted.* An app that DELETEs the
default-user row on the **same live engine object** without touching the gate or
the canonical would keep serving stale-open until the next rebind/close. The
supported way to clear/replace the identity at runtime is to dispose + rebind the
engine (new object → latch miss → re-probe) or to run the swap inside a
disable_db/enable_db window (epoch bump → re-probe); do not add a runtime "reset
identity" tool that mutates the row in place on the live engine without either.

Self-heal from canonical
------------------------

The live probe reopens the gate the instant a row lands in the runtime DB — but
nothing lands there on its own when the runtime is a *different file* from the
one populate writes. The server boots as its service uid and cold-seeds a runtime
under ``/tmp/mcp-runtime-<serviceuid>/``; populate runs as a *different* uid
(often root), imports into canonical, and snapshots. The server's runtime is a
*different* per-uid file. Only the server can refresh its own runtime — the path
is keyed on ``os.getuid()`` — so an out-of-process populate physically cannot do
it for it.

When ``install_default_user_gate`` is given a ``canonical=`` path, the gate
closes that gap itself, *in-process*. On every ``serve()`` — **before** the latch
short-circuit — it cheaply checks whether canonical has drifted from its runtime
and, if so, refreshes (drain pool → copy → clear latch → re-probe). No POST, no
``/_internal/*`` round-trip, no lifecycle coordination — any app that passes
``binding.canonical`` inherits it.

Running the check *before* the latch (not only while the gate is closed) is what
makes it correct for the two-container case: container B boots, sees the seed
identity row, latches open, then a *repopulate* rewrites canonical with a record
created in container A. A refresh gated on "identity absent" would never fire
(B's identity is present), and the latch would keep serving B's stale runtime, so
the new record would be invisible. Checking freshness on every request
generalises the recovery from "identity absent" to "runtime out of sync with
canonical for ANY reason".

It stays cheap by construction: the freshness check is fingerprint-gated (a
single ``stat`` on canonical + a marker read) and lock-free when in sync, so a
steady-state open gate is still O(1) — no ``COUNT(*)``, no pool dispose, no copy.
Only an actual canonical change pays for the serialized dispose+copy, and only
once (the copy rewrites the marker, so the next request is back on the fast path).
A genuinely-empty system just keeps cheaply polling, exactly as before.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from mcp_middleware.runtime_db.db_gate import db_gate_epoch, is_db_disabled

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from starlette.requests import Request
    from starlette.types import ASGIApp

    from mcp_middleware.runner import DefaultUserRef

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GATE_BYPASS",
    "GateBypass",
    "install_default_user_gate",
]

# A default-user table name is interpolated into a COUNT(*) probe downstream, so
# it must be a plain SQL identifier. Validated at install time (fail fast).
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Sentinel attribute marking that the tool arm has already been installed on a
# given FastMCP instance, so a double install_default_user_gate() call (e.g.
# run_server AND the app both wiring it) doesn't stack duplicate tool middleware.
_TOOL_ARM_SENTINEL = "_mcp_default_user_gate_tool_arm_installed"

# HTTP path prefixes that bypass the identity gate. Prefix (startswith) match,
# NOT exact — populate's pre-flight POST /_internal/checkpoint (a pool drain)
# runs BEFORE the identity is seeded, so gating /_internal/* would 500 the drain
# and deadlock populate. Health probes must answer while the gate is closed, and
# the MCP transport paths (/mcp, /sse, /messages) are gated by the TOOL arm
# instead (double-gating them would be redundant).
_DEFAULT_REST_BYPASS_PREFIXES: tuple[str, ...] = (
    "/_internal/",
    "/health",
    "/workspace/health",
    "/mcp",
    "/sse",
    "/messages",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/api/v1/_playground/",
)

# Tool names that bypass the identity gate — public tools that must answer
# before an identity exists (server metadata + health).
_DEFAULT_PUBLIC_TOOLS: tuple[str, ...] = (
    "server_info",
    "workspace_health",
)


@dataclass(frozen=True)
class GateBypass:
    """Which HTTP paths and tool names skip the identity gate.

    REST paths and tool names are different namespaces, so they're carried
    separately. Apps share ONE ``GateBypass`` instance between the identity
    gate and their own scope middleware so the two never diverge.

    Attributes:
        rest_prefixes: HTTP path prefixes that bypass the REST arm. Matched
            with ``str.startswith`` (prefix, not exact).
        public_tools: Tool names that bypass the tool arm. Exact membership.
    """

    rest_prefixes: tuple[str, ...] = field(default=_DEFAULT_REST_BYPASS_PREFIXES)
    public_tools: tuple[str, ...] = field(default=_DEFAULT_PUBLIC_TOOLS)

    def is_rest_bypassed(self, path: str) -> bool:
        """True if ``path`` matches any bypass prefix (startswith)."""
        return any(path.startswith(p) for p in self.rest_prefixes)

    def is_tool_bypassed(self, tool_name: str) -> bool:
        """True if ``tool_name`` is an exact public-tool match."""
        return tool_name in self.public_tools


# The shared default. Apps extend by constructing GateBypass(rest_prefixes=...,
# public_tools=...) with their own additions folded in.
DEFAULT_GATE_BYPASS = GateBypass()


class _Freshness(Enum):
    """Outcome of the gate's per-request canonical-freshness check.

    Richer than a bool so ``serve()`` can tell "runtime is fresh" apart from
    "canonical drifted but the refresh could NOT fold it in" — the latter must
    fail closed rather than keep serving a stale runtime on the open latch.
    """

    IN_SYNC = "in_sync"  # runtime already matches canonical (or no canonical) → latch valid
    REFRESHED = "refreshed"  # runtime copied fresh (or a peer refreshed it) → clear latch, re-probe
    STALE = "stale"  # drift detected but refresh refused/failed → fail closed, don't serve stale


class _GateOutcome(Enum):
    """Why the gate opened or closed on a given ``decide()`` — richer than a bool
    so the two arms can emit a *distinct* error for each closed reason.

    The crucial distinction for enforcement: a genuinely-unconfigured world
    (:attr:`MISSING`) is flag-governed — an app may legitimately run with an
    empty identity table (cold start), so it only closes when enforcement is on.
    A :attr:`DANGLING` pointer (a row whose FK is set but resolves to no user) is
    a *corrupt* state from a bad populate/UPDATE, never a valid empty world, so it
    fails closed **regardless of the enforcement flag** — disabling enforcement
    must not paper over data corruption.
    """

    OPEN = "open"  # serve
    DB_DISABLED = "db_disabled"  # DB-maintenance pause (snapshot / repopulate)
    STALE = "stale"  # canonical drifted, refresh refused → transient; retry like a pause
    MISSING = "missing"  # no/empty identity — flag-governed close
    DANGLING = "dangling"  # corrupt FK — ALWAYS closed


# Each closed outcome → the external error token (REST ``{"error": ...}`` and the
# prefix of the tool-arm ``ToolError`` message) plus the human-facing message.
# STALE maps to the same token as DB_DISABLED: canonical drifted but the refresh
# was refused, a transient maintenance-like condition the caller should retry —
# NOT a missing identity, so it must not send operators chasing a populate ghost.
_CLOSED_ERROR: dict[_GateOutcome, tuple[str, str]] = {
    _GateOutcome.DB_DISABLED: (
        "db_disabled",
        "db_disabled: the server is temporarily paused for database maintenance "
        "(snapshot / repopulate). Retry shortly.",
    ),
    _GateOutcome.STALE: (
        "db_disabled",
        "db_disabled: the server is briefly refreshing its runtime from canonical. Retry shortly.",
    ),
    _GateOutcome.MISSING: (
        "default_user_not_configured",
        "default_user_not_configured: the server has no default-user identity yet "
        "— the singleton default-user table is empty. This clears automatically "
        "once populate seeds the identity row.",
    ),
    _GateOutcome.DANGLING: (
        "default_user_dangling_reference",
        "default_user_dangling_reference: the default-user identity row points at a "
        "user that does not exist (dangling foreign key). This is a corrupt state "
        "from a bad populate/UPDATE — re-run populate to seed a resolvable identity. "
        "Disabling enforcement does NOT rescue a dangling reference.",
    ),
}


class _IdentityGate:
    """Live ``serve = NOT is_db_disabled() AND present`` predicate, with latch.

    Holds the engine provider + table and memoizes the positive result keyed on
    the identity of the engine object. See the module docstring for the latch
    rationale. One instance is shared by both arms so a probe benefits both.
    """

    def __init__(
        self,
        engine_provider: Callable[[], Engine],
        table: str,
        *,
        refresh: Callable[[Engine], _Freshness] | None = None,
        ref: DefaultUserRef | None = None,
        enforced: bool = True,
    ) -> None:
        self._engine_provider = engine_provider
        self._table = table
        # Optional referential-integrity spec: when set, an identity row's FK must
        # resolve. A dangling pointer (FK set, no matching user) is CORRUPT and
        # always closes the gate, independent of ``enforced`` below.
        self._ref = ref
        # Whether a genuinely-*unconfigured* world (no row / empty FK) closes the
        # gate. When ``False`` (enforcement disabled) an empty identity table is a
        # valid cold-start world and the gate serves it — but a DANGLING pointer
        # still fails closed, because disabling enforcement is meant for empty
        # worlds, not to serve on top of a corrupt reference.
        self._enforced = enforced
        # Optional hook: refresh THIS server's runtime from canonical when the
        # canonical has drifted (see the "Self-heal from canonical" section in
        # the module docstring). Runs on every serve() *before* the latch, so it
        # catches both the cross-uid populate that first seeds the identity and a
        # repopulate that swaps canonical after the gate latched open. Returns
        # True iff it actually copied (so serve() re-probes and re-latches); it is
        # internally cheap (a stat + marker read) and lock-free when in sync.
        self._refresh = refresh
        # The engine object we last observed a row on. A rebind yields a new
        # object (identity mismatch) → re-probe. Lock-free single-attr reads /
        # writes, mirroring db_gate's lock-free flag: a stale read costs at most
        # one extra probe or one request that self-corrects on retry.
        self._latched_engine: Engine | None = None
        # The db-gate close-epoch observed when we latched. A disable→enable
        # cycle (which may swap the runtime DB out from under the SAME engine
        # object) bumps the epoch; if it advanced since we latched we re-probe
        # even though `is_db_disabled()` currently reads False. This makes the
        # "closing the DB gate clears the latch" guarantee hold regardless of
        # WHEN serve() next runs — even if the whole cycle completed while only
        # bypassed /_internal/* routes were hit and serve() never ran.
        self._latched_epoch: int | None = None

    def serve(self) -> bool:
        """Thin bool wrapper over :meth:`decide` — ``True`` iff the gate is open."""
        return self.decide() is _GateOutcome.OPEN

    def decide(self) -> _GateOutcome:
        """Classify this request: open, or *why* it's closed.

        The arms call this (not :meth:`serve`) so a closed gate reports a
        distinct error per reason. Enforcement governs only the MISSING
        (genuinely-unconfigured) case; a DANGLING pointer always closes.
        """
        # DB gate closed → fail closed AND clear the latch (a swap is in flight).
        if is_db_disabled():
            self._latched_engine = None
            return _GateOutcome.DB_DISABLED

        engine = self._engine_provider()

        # Freshness first, BEFORE the latch short-circuit. The latch keeps a
        # steady-state open gate O(1) (no COUNT(*) per request), but on its own
        # it masks a canonical that changed *after* we latched — the two-container
        # case: container B boots, sees the seed row, latches open, then a
        # repopulate rewrites canonical with a record created in container A; the
        # latch would keep serving B's stale runtime and the new record would be
        # invisible. Running the refresh here generalises the old identity-absent
        # self-heal to "runtime out of sync with canonical for ANY reason": it
        # also covers the cross-uid first-seed (canonical gets the identity while
        # the boot-time runtime is empty). Cheap by construction — the hook does a
        # lock-free fingerprint check and returns IN_SYNC without disposing when
        # already current; a real copy (or a peer's copy) returns REFRESHED, which
        # clears the latch so we re-probe the now-fresh runtime below.
        if self._refresh is not None:
            freshness = self._refresh(engine)
            if freshness is _Freshness.STALE:
                # Canonical drifted but the refresh could NOT fold it into the
                # runtime (busy checkpoint / planner failure). Serving the open
                # latch here would hand out a stale runtime after a repopulate —
                # exactly the drift the freshness check exists to catch. Clear the
                # latch and fail closed; the caller retries (503 / ToolError) and
                # the next decide() re-attempts the refresh.
                self._latched_engine = None
                return _GateOutcome.STALE
            if freshness is _Freshness.REFRESHED:
                self._latched_engine = None

        # Latched on this exact engine object AND no DB-gate close since we
        # latched → identity already confirmed. The epoch guard catches a
        # disable→enable cycle that swapped the runtime out from under the same
        # engine object while only bypassed routes ran (so this branch never
        # executed during the closed window): the epoch advanced, so we fall
        # through and re-probe instead of trusting the stale latch.
        if engine is self._latched_engine and db_gate_epoch() == self._latched_epoch:
            return _GateOutcome.OPEN

        # Absent, unknown, rebound, just-refreshed, or a DB-gate cycle since we
        # latched → probe live, classifying valid / dangling / missing in one query.
        from mcp_middleware.runner import _default_user_status

        _, valid, dangling_sample = _default_user_status(engine, self._table, self._ref)
        if valid >= 1:
            # A resolvable identity exists (even if other rows dangle) → latch open,
            # recording the current close-epoch so a later swap invalidates us.
            self._latched_engine = engine
            self._latched_epoch = db_gate_epoch()
            return _GateOutcome.OPEN
        if dangling_sample is not None:
            # Row(s) present with a set-but-unresolved FK and NONE valid: corrupt.
            # Fail closed regardless of enforcement, and don't latch (so a later
            # repopulate that heals the reference re-probes and opens).
            return _GateOutcome.DANGLING
        # Genuinely unconfigured (no row / empty / null FK). Flag-governed: closed
        # when enforced, served as a valid empty world when not. Never latched, so
        # the gate keeps polling live — it self-heals the instant a row lands and
        # catches a later dangling state too.
        return _GateOutcome.MISSING if self._enforced else _GateOutcome.OPEN


class _DefaultUserToolGate:
    """FastMCP tool arm: refuse ``on_call_tool`` until an identity exists.

    Installed via ``mcp.add_middleware``. Subclasses the FastMCP ``Middleware``
    base (imported lazily so importing this module doesn't hard-require a
    FastMCP version at import time).
    """

    def __init__(self, gate: _IdentityGate, bypass: GateBypass) -> None:
        self._gate = gate
        self._bypass = bypass

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tool_name = getattr(context.message, "name", "")
        if self._bypass.is_tool_bypassed(tool_name):
            return await call_next(context)
        # decide() can do a synchronous pool-dispose + SQLite copy when it detects
        # canonical drift; run it off the event loop so a single drift-detecting
        # tool call doesn't stall every other request for the copy's duration.
        outcome = await anyio.to_thread.run_sync(self._gate.decide)
        if outcome is _GateOutcome.OPEN:
            return await call_next(context)
        from fastmcp.exceptions import ToolError

        # Distinct message per closed reason — a DB-maintenance pause, a missing
        # identity, and a corrupt (dangling) reference each read differently so
        # operators don't chase a phantom populate problem.
        _, message = _CLOSED_ERROR[outcome]
        raise ToolError(message)


class _DefaultUserHTTPGate(BaseHTTPMiddleware):
    """Starlette REST arm: 503 non-bypassed requests until an identity exists."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        gate: _IdentityGate,
        bypass: GateBypass,
        retry_after_seconds: int = 30,
    ) -> None:
        super().__init__(app)
        self._gate = gate
        self._bypass = bypass
        self._retry_after_seconds = int(retry_after_seconds)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if self._bypass.is_rest_bypassed(request.url.path):
            return await call_next(request)
        # Offload decide() — on canonical drift it disposes the pool and copies the
        # runtime synchronously; running it inline would block the event loop for
        # every concurrent request while the copy runs.
        outcome = await anyio.to_thread.run_sync(self._gate.decide)
        if outcome is _GateOutcome.OPEN:
            return await call_next(request)
        # Distinct error token per closed reason: a DB-maintenance pause, a
        # genuinely-missing identity, and a corrupt (dangling) reference each read
        # differently so a snapshot window / corruption doesn't masquerade as a
        # populate failure (or vice versa).
        error, _ = _CLOSED_ERROR[outcome]
        return JSONResponse(
            {"error": error},
            status_code=503,
            headers={"Retry-After": str(self._retry_after_seconds)},
        )


def _make_tool_arm(gate: _IdentityGate, bypass: GateBypass) -> Any:
    """Build a FastMCP-``Middleware``-typed tool arm.

    FastMCP's ``Middleware`` base is imported lazily and used as a mixin so the
    ``on_call_tool`` hook is dispatched by FastMCP's middleware machinery.
    """
    from fastmcp.server.middleware import Middleware as _FastMCPMiddleware

    class _ToolArm(_DefaultUserToolGate, _FastMCPMiddleware):
        def __init__(self, gate: _IdentityGate, bypass: GateBypass) -> None:
            _FastMCPMiddleware.__init__(self)
            _DefaultUserToolGate.__init__(self, gate, bypass)

    return _ToolArm(gate, bypass)


def _make_refresh(canonical: str | os.PathLike[str]) -> Callable[[Engine], _Freshness]:
    """Build the gate's freshness hook: refresh THIS server's runtime from canonical.

    ``serve()`` calls this on every request, *before* the latch short-circuit, so
    it must be cheap on the hot path and only pay for a real copy when the
    canonical actually drifted. It runs *in the live server process*, so
    :func:`~mcp_middleware.runtime_db.refresh_runtime_from_canonical` resolves the
    server's OWN per-uid runtime dir (keyed on ``os.getuid()``) — the whole point:
    a cross-uid populate (often root) or a repopulate that rewrote canonical
    physically cannot refresh the server's runtime for it, so the server does it
    itself.

    Two layers keep it cheap and correct under concurrency:

    * **Lock-free fast path.** :func:`~mcp_middleware.runtime_db.runtime_refresh_pending`
      does a single ``stat`` + marker read and returns False when the runtime is
      already in sync — the overwhelmingly common case — so steady-state requests
      never take the lock, never dispose the pool, never copy.
    * **Serialized slow path.** When drift IS detected, a lock serializes the
      dispose+copy so concurrent requests don't stampede it. The real refresh
      re-checks the fingerprint under the lock, so a caller that loses the race
      gets a cheap ``NOOP`` (returns False) rather than a second copy.

    ``drain=engine.dispose`` releases the pool before the copy overwrites the
    runtime inode; ``checkpoint_url=None`` because we ARE the live server (there
    is no *other* process to drive a checkpoint on). Return mapping:

    * in sync (fast path) / ``NO_CANONICAL`` → :attr:`_Freshness.IN_SYNC` (latch
      honored; nothing to fold in);
    * ``REFRESHED`` (we copied) or ``NOOP`` (a concurrent request already folded
      it under the lock — we lost the race but the runtime IS fresh now) →
      :attr:`_Freshness.REFRESHED` so ``serve()`` re-probes the fresh runtime;
    * ``REFUSED_BUSY`` / ``REFUSED_PLANNER_FAILURE`` → :attr:`_Freshness.STALE`:
      canonical drifted but we could NOT fold it in, so the runtime is stale and
      ``serve()`` MUST fail closed rather than serve it on the open latch.
    """
    canonical_path = os.fspath(canonical)
    lock = threading.Lock()

    def _refresh(engine: Engine) -> _Freshness:
        from mcp_middleware.runtime_db import (
            RefreshOutcome,
            refresh_runtime_from_canonical,
            runtime_refresh_pending,
        )

        # Lock-free fast path: in sync → nothing to do, no lock, no dispose.
        if not runtime_refresh_pending(canonical_path):
            return _Freshness.IN_SYNC

        # Drift detected: serialize the dispose+copy. The refresh re-checks the
        # fingerprint under the lock, so a thread that lost the race NOOPs.
        with lock:
            result = refresh_runtime_from_canonical(
                canonical_path,
                checkpoint_url=None,
                drain=engine.dispose,
            )
        outcome = result.outcome
        if outcome is RefreshOutcome.REFRESHED:
            logger.info(
                "default_user gate: refreshed runtime from canonical (%s)",
                result.detail,
            )
            return _Freshness.REFRESHED
        if outcome is RefreshOutcome.NOOP:
            # A concurrent request folded the drift under the lock (we lost the
            # race). The runtime is current now → re-probe to pick up its rows.
            return _Freshness.REFRESHED
        if outcome is RefreshOutcome.NO_CANONICAL:
            # No canonical to sync against → the runtime is the only truth.
            return _Freshness.IN_SYNC
        # REFUSED_BUSY / REFUSED_PLANNER_FAILURE: drift is real but we couldn't
        # fold it. Do NOT serve the stale runtime — fail closed and retry.
        logger.warning(
            "default_user gate: canonical drifted but refresh was refused (%s: %s) "
            "— failing closed to avoid serving a stale runtime",
            outcome,
            result.detail,
        )
        return _Freshness.STALE

    return _refresh


def install_default_user_gate(
    mcp: Any,
    engine_provider: Callable[[], Engine],
    *,
    table: str = "default_users",
    bypass: GateBypass | None = None,
    canonical: str | os.PathLike[str] | None = None,
    ref: DefaultUserRef | None = None,
    enforced: bool = True,
) -> Middleware:
    """Install the two-arm default-user identity gate.

    Adds the FastMCP tool arm to ``mcp`` (via ``mcp.add_middleware``) and
    RETURNS the Starlette ``Middleware`` for the REST arm so the caller can
    insert it into its own HTTP middleware list at the position it wants
    (outermost, ahead of any DB-touching middleware). Apps that build FastMCP
    directly pass their HTTP middleware list to ``mcp.run(middleware=[...])``,
    so the installer cannot self-attach the REST arm — hence the return value.

    Args:
        mcp: The FastMCP instance. The tool arm is added to it.
        engine_provider: Zero-arg callable returning the LIVE engine. Must be a
            provider, not a captured handle: apps that rebind the engine on a
            DB swap (dispose + re-bind) would otherwise gate against a stale,
            disposed engine. The latch keys on the identity of the object this
            returns, so a rebind triggers exactly one fresh probe.
        table: Name of the singleton default-user table. Must be a plain SQL
            identifier (validated here, at install, so a typo fails fast).
        bypass: A :class:`GateBypass` controlling which HTTP prefixes and tool
            names skip the gate. Defaults to :data:`DEFAULT_GATE_BYPASS`.
        canonical: Optional path to the canonical (slow-storage) DB. When
            provided, the gate **self-heals**: on every request it cheaply checks
            whether canonical has drifted from this server's per-uid runtime and,
            if so, refreshes the runtime from canonical (in-process, correct uid)
            before deciding. This closes two cross-uid gaps without any POST or
            lifecycle wiring — (1) "populate seeded the identity into canonical
            but the server still serves its empty boot-time runtime", and (2) a
            *repopulate* that rewrites canonical **after** the gate latched open
            (e.g. a record created in another container), which the latch alone
            would otherwise mask. The check is fingerprint-gated (a ``stat`` +
            marker read) and lock-free when in sync, so it stays O(1) on the hot
            path. Pass ``binding.canonical`` from
            :func:`~mcp_middleware.runtime_db.bind_engine` (RUNTIME mode only; a
            DIRECT/MEMORY binding has no separate runtime to refresh, so leave it
            ``None``). When ``None`` (default), the gate keeps its original
            behaviour: poll live and open when the row lands.

            The refresh runs on every request **regardless of** ``enforced`` —
            disabling enforcement suppresses only the refuse-until-seeded
            behaviour (job 1), never the freshness self-heal. Install with
            ``enforced=False`` + a ``canonical`` to keep an empty-world server
            data-fresh without refusing traffic.
        ref: Optional :class:`mcp_middleware.DefaultUserRef`. When provided, the
            gate opens only if the identity row's foreign key *resolves*. A
            **dangling** pointer (FK set but matching no user) is a corrupt state
            and keeps the gate closed **regardless of** ``enforced`` — see below.
        enforced: Whether a genuinely-*unconfigured* world (no row / empty FK)
            closes the gate. ``True`` (default) is the mandatory-identity mode.
            ``False`` serves an empty identity table as a valid cold-start world —
            but a ``ref`` dangling reference STILL fails closed, because disabling
            enforcement is for empty worlds, not for serving on corrupt data.
            Install with ``enforced=False`` + a ``ref`` to get dangling-only
            protection while otherwise letting an unconfigured world serve.

    Returns:
        The Starlette ``Middleware`` for the REST arm.

    Raises:
        ValueError: ``table`` is not a valid SQL identifier.
    """
    if not _TABLE_RE.match(table):
        raise ValueError(
            f"install_default_user_gate: table must be a plain SQL identifier, got {table!r}"
        )
    resolved_bypass = bypass if bypass is not None else DEFAULT_GATE_BYPASS
    refresh = _make_refresh(canonical) if canonical is not None else None
    gate = _IdentityGate(engine_provider, table, refresh=refresh, ref=ref, enforced=enforced)

    # Tool arm — guard against a double install stacking duplicate middleware.
    # ``is True`` (not truthiness) so a real second install — which sets the
    # sentinel to the literal ``True`` below — is the only thing that skips;
    # a fresh instance reads the ``False`` default and installs.
    if getattr(mcp, _TOOL_ARM_SENTINEL, False) is True:
        logger.warning(
            "install_default_user_gate: tool arm already installed on this mcp "
            "instance; not adding a duplicate (the returned REST arm is still fresh)."
        )
    else:
        mcp.add_middleware(_make_tool_arm(gate, resolved_bypass))
        setattr(mcp, _TOOL_ARM_SENTINEL, True)

    # REST arm — returned for the caller to place in its HTTP middleware stack.
    return Middleware(_DefaultUserHTTPGate, gate=gate, bypass=resolved_bypass)
