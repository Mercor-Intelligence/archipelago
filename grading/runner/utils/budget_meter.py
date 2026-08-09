"""Per-cost-unit LLM spend meter, backed by Redis — the shared guardrail core.

CANONICAL SOURCE — vendored verbatim into the archipelago runners by
``scripts/vendor_budget_meter.py`` (parity-tested); edit only this file.
Client-injected: every op takes an async Redis client (or ``None``) so the three
consumers (server, agents runner, grading runner) pass their own.

Model: the Postgres ``budget_state`` column is the source of truth; Redis is a 2-day cache.
``budget_total:{unit}`` holds the cap, ``tc:<lane>:{unit}`` accrues real cost per settled
call for each lane in ``LANES`` (mirrored to Postgres via GREATEST),
``est:untracked:{unit}`` is a side ledger for unreadable-usage calls,
``settled:{unit}:{cid}`` dedupes retried accruals, ``hydrate_lock:{unit}`` dedupes
runner-triggered repair. remaining = total − Σ lanes, always derived (never stored).
Keys are hash-tagged ``{unit}`` and hydrated from the mirror on miss (see server).

Key convention: ``<kind>:<qualifier...>:{unit}`` — colon-delimited so a new workload is a
new lane token in ``LANES``, not a new key format. ``unit`` is a trajectory batch id today;
nothing in this module assumes that.

Absent vs unenforced: a MISSING cap key means "Redis lost state, rehydrate"; a cap of
``UNENFORCED_MICROS`` means "no budget was ever set". Conflating them would make every
unbudgeted batch look like an eviction forever, so ``hydrate`` always writes one or the
other and every tracked key (never a bare skip) — that makes "all keys present" an
invariant the runner can test with ``BudgetSnapshot.hydration_needed``.

Fail-open: ops retry only on Redis *service* errors (never a valid response), then
degrade toward "no guardrail" with a DD-alertable ``_note_redis_error`` log.
"""

import asyncio
import contextlib
import importlib
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

# ddtrace is optional — the archipelago runners don't ship it.
try:
    _tracer = importlib.import_module("ddtrace.trace").tracer
except ImportError:  # pragma: no cover - archipelago runners have no ddtrace
    _tracer = None

# 2 days: keys are a cache over the durable mirror and re-hydrate on miss; active
# batches get their TTL refreshed on every accrual/read.
BUDGET_TTL_SECONDS = 2 * 24 * 60 * 60
# Accrual's fire-once idempotency marker; only outlives the retry window.
SETTLED_TTL_SECONDS = 60
# Caps how often one unit can ask the server to rehydrate. Nothing waits on this lock, so
# a stale one only delays a re-request — it can never wedge a worker (and ``hydrate`` is
# idempotent, so a duplicate request is harmless anyway).
HYDRATE_LOCK_TTL_SECONDS = 60
# Hard ceiling on how long the ONE lock-winning worker may wait for the repair request
# before giving up and proceeding. Enforced here because the injected callback may
# retry internally; this sits on the LLM call path, so it must stay small.
HYDRATE_REQUEST_TIMEOUT_SECONDS = 3.0
_MICROS_PER_USD = 1_000_000

# Cap value meaning "no budget is set", as opposed to the key being ABSENT (= Redis lost
# state). Negative because a real cap is always >= 0, so no valid budget can collide.
UNENFORCED_MICROS = -1

# Cost lanes. Add a workload by appending its token here: the Lua scripts loop over the
# keys they are handed and the snapshot carries a per-lane dict, so nothing else changes.
LANE_TRAJECTORY = "trajectory"
LANE_GRADING = "grading"
LANES: tuple[str, ...] = (LANE_TRAJECTORY, LANE_GRADING)

# Settle a call: dedupe via the settled marker, accrue actual into the lane total and
# any unreadable-usage estimate into the side ledger; refresh TTLs (incl. the cap's).
# KEYS = [tc_lane, est_untracked, settled, budget_total];
# ARGV = [actual_micros, ttl_seconds, settled_ttl_seconds, untracked_micros].
_ACCRUE_LUA = """
if not redis.call('SET', KEYS[3], '1', 'NX', 'EX', tonumber(ARGV[3])) then return 0 end
redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
if tonumber(ARGV[4]) > 0 then
  redis.call('INCRBY', KEYS[2], tonumber(ARGV[4]))
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
end
redis.call('EXPIRE', KEYS[4], tonumber(ARGV[2]))
return 1
"""

# Read the whole meter in ONE round-trip and refresh the TTL of every key that
# exists (so an actively-gated or swept batch — including one being denied — can
# never let its keys lapse). Missing keys come back as '' so "absent" stays
# distinguishable from "zero" — that distinction is the hydration signal.
# KEYS = [budget_total, tc:<lane>… (one per LANES), est:untracked]; ARGV = [ttl_seconds].
_READ_LUA = """
local out = {}
for i = 1, #KEYS do
  local v = redis.call('GET', KEYS[i])
  if v then
    redis.call('EXPIRE', KEYS[i], tonumber(ARGV[1]))
    out[i] = v
  else
    out[i] = ''
  end
end
return out
"""

# Raise a counter to at least `base` (hydration floor from the durable mirror) and refresh
# its TTL. CREATES the key when absent — even at 0 — so that "every tracked key exists"
# holds after a hydrate and absence is unambiguously "Redis lost it" rather than "nothing
# spent yet". Still monotonic: an existing counter is never lowered.
# KEYS=[counter]; ARGV=[base, ttl].
# Arm the cap ONLY when it is unset or still the unenforced sentinel (negative).
# Plain NX is not enough: after sampling the key already EXISTS holding the sentinel,
# so NX would silently skip and the batch would keep spending ungated. A real cap
# (>= 0) is never lowered by this — only `set_batch_budget` may overwrite one.
_ARM_CAP_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur or tonumber(cur) < 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return 1
"""

_FLOOR_LUA = """
local cur = redis.call('GET', KEYS[1])
local base = tonumber(ARGV[1])
if not cur or base > tonumber(cur) then redis.call('SET', KEYS[1], base) end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""


class BudgetExceededError(Exception):
    """Raised when a call is denied because the batch is over budget. Terminal."""

    def __init__(
        self,
        cost_unit: str,
        *,
        remaining_usd: float | None = None,
        model: str | None = None,
    ) -> None:
        self.cost_unit = cost_unit
        self.remaining_usd = remaining_usd
        self.model = model
        super().__init__(
            f"budget exceeded for {cost_unit} "
            f"(remaining={remaining_usd}, model={model})"
        )


class BudgetSnapshot(BaseModel):
    """Point-in-time meter read (USD). ``costs_usd`` holds the running total per lane
    (mirror model); ``remaining_usd`` is derived = total budget − Σ lanes, None when
    unenforced (no cap set, or the cap key is missing)."""

    budget_total_usd: float | None
    remaining_usd: float | None
    costs_usd: dict[str, float]
    untracked_est_usd: float
    # True when any tracked key was absent, i.e. Redis lost meter state and the durable
    # mirror has to rebuild it. Distinct from "unenforced": an unbudgeted unit still has
    # all its keys (the cap holds UNENFORCED_MICROS). Excludes the per-call `settled`
    # markers, which are meant to expire.
    hydration_needed: bool = False

    @property
    def total_traj_usd(self) -> float:
        return self.costs_usd.get(LANE_TRAJECTORY, 0.0)

    @property
    def total_grading_usd(self) -> float:
        return self.costs_usd.get(LANE_GRADING, 0.0)

    @property
    def total_cost_usd(self) -> float:
        """What counts against the cap. Excludes ``untracked_est_usd`` by design — see
        the module docstring; making it count is a pending product decision."""
        return sum(self.costs_usd.values())


def to_micros(usd: float) -> int:
    return round(usd * _MICROS_PER_USD)


def from_micros(micros: int) -> float:
    return micros / _MICROS_PER_USD


def _as_int(value: object) -> int:
    """Redis value → int (handles str and the runners' undecoded bytes), default 0."""
    if value is None:
        return 0
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return int(value)  # pyright: ignore[reportArgumentType]  (str | int)
    except (TypeError, ValueError):
        return 0


# Keys are hash-tagged on {unit} for cluster-slot locality. All are built through _key so
# the `<kind>:<qualifier...>:{unit}` convention lives in exactly one place.
def _key(kind: str, unit: str, *qualifiers: str) -> str:
    parts = ":".join((kind, *qualifiers))
    return f"{parts}:{{{unit}}}"


def _budget_total_key(unit: str) -> str:
    return f"budget_total:{{{unit}}}"


def _settled_key(unit: str, call_id: str) -> str:
    return f"settled:{{{unit}}}:{call_id}"


def _total_cost_key(unit: str, lane: str) -> str:
    return _key("tc", unit, lane)


def _untracked_key(unit: str) -> str:
    return _key("est", unit, "untracked")


def _hydrate_lock_key(unit: str) -> str:
    return _key("hydrate_lock", unit)


def _tracked_keys(unit: str) -> list[str]:
    """Every key that must exist for the meter to be considered intact — the cap, one
    counter per lane, and the untracked ledger. Order is the contract for _READ_LUA."""
    return [
        _budget_total_key(unit),
        *(_total_cost_key(unit, lane) for lane in LANES),
        _untracked_key(unit),
    ]


@contextlib.contextmanager
def _span(op: str, unit: str) -> Iterator[None]:
    """Dedicated ``budget.<op>`` APM span; no-op without ddtrace."""
    if _tracer is None:
        yield
        return
    with _tracer.trace(f"budget.{op}", resource=f"budget.{op}") as span:
        span.set_tag("component", "budget_meter")
        span.set_tag("budget.unit", unit)
        yield


def _note_redis_error(op: str, unit: str, exc: Exception) -> None:
    """DD-alertable signal (``@event:budget_meter_redis_unavailable``) for an op that
    got no answer after retries; the op still fails open."""
    if _tracer is not None:
        span = _tracer.current_span()
        if span is not None:
            span.set_tag("budget.redis_error", True)
    logger.bind(
        event="budget_meter_redis_unavailable", budget_unit=unit, budget_op=op
    ).warning(f"budget_meter.{op}: Redis error for unit={unit}, degrading: {exc!r}")


def is_over_budget(total_cost_micros: int, budget_total_micros: int) -> bool:
    """Pure gate predicate: the batch is over budget once cost reaches the cap."""
    return total_cost_micros >= budget_total_micros


_MAX_REDIS_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.05


async def _run_with_retry(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Retry ONLY on ``RedisError`` (no answer); any valid result is final."""
    last: RedisError | None = None
    for attempt in range(_MAX_REDIS_ATTEMPTS):
        try:
            return await factory()
        except RedisError as exc:
            last = exc
            if attempt + 1 < _MAX_REDIS_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    assert last is not None
    raise last


async def set_budget_total(
    client: Redis | None,
    unit: str,
    total_usd: float | None,
    *,
    only_if_unenforced: bool = False,
) -> None:
    """Set the total-budget cap (mirrors the DB value; idempotent). ``None`` records the
    explicit "no budget set" sentinel rather than leaving the key absent, so a reader can
    tell an unbudgeted unit from one whose cap Redis dropped.

    A real cap is written unconditionally — the DB is authoritative. The sentinel is
    written **NX**, i.e. it may only fill an ABSENT key: hydration can run from a stale
    durable read (respawn arming, runner-triggered repair), and overwriting a live cap
    with "unenforced" would silently disable gating for the whole batch (bugbot). Losing
    that race the other way just means Redis briefly keeps a cap the DB no longer has,
    which errs toward enforcing.

    ``only_if_unenforced`` marks a cap this process DERIVED rather than read
    authoritatively — the enforced 0 given to a metered batch with no budget. It is
    written by compare-and-set: applied when the key is absent OR still holds the
    sentinel, skipped when a real cap is already there. NX alone would be wrong in
    both directions — it cannot replace the sentinel a sampling batch already wrote
    (leaving it ungated), and an unconditional write would clobber a live budget with
    0 and deny the batch until the TTL lapsed, since the rehydrate check repairs
    None-vs-set but not 0-vs-positive (bugbot).
    """
    if client is None:
        return
    unenforced = total_usd is None
    micros = UNENFORCED_MICROS if unenforced else to_micros(total_usd or 0.0)
    with _span("set_budget_total", unit):
        try:
            if only_if_unenforced and not unenforced:
                arm = client.register_script(_ARM_CAP_LUA)
                await _run_with_retry(
                    lambda: arm(
                        keys=[_budget_total_key(unit)],
                        args=[micros, BUDGET_TTL_SECONDS],
                    )
                )
                return
            await _run_with_retry(
                lambda: client.set(
                    _budget_total_key(unit),
                    micros,
                    ex=BUDGET_TTL_SECONDS,
                    nx=unenforced,
                )
            )
        except RedisError as exc:
            _note_redis_error("set_budget_total", unit, exc)


async def hydrate(
    client: Redis | None,
    unit: str,
    *,
    total_usd: float | None,
    lane_usd: dict[str, float],
    untracked_usd: float,
    cap_only_if_unenforced: bool = False,
) -> None:
    """Rebuild evicted/expired keys from the durable mirror: SET the cap (or the
    unenforced sentinel), floor each lane counter to the mirrored value (never lowers),
    refresh TTLs. Safe to run every sweep.

    Writes EVERY tracked key, including zeros and the sentinel, so that afterwards
    "all keys present" holds — that is what lets a runner treat absence as "Redis lost
    state" instead of guessing. Lanes absent from ``lane_usd`` floor to 0.
    """
    if client is None:
        return
    with _span("hydrate", unit):
        try:
            await set_budget_total(
                client, unit, total_usd, only_if_unenforced=cap_only_if_unenforced
            )
            script = client.register_script(_FLOOR_LUA)
            for key, base in (
                *((_total_cost_key(unit, ln), lane_usd.get(ln, 0.0)) for ln in LANES),
                (_untracked_key(unit), untracked_usd),
            ):
                await _run_with_retry(
                    lambda k=key, b=base: script(
                        keys=[k], args=[to_micros(b), BUDGET_TTL_SECONDS]
                    )
                )
        except RedisError as exc:
            _note_redis_error("hydrate", unit, exc)


async def accrue(
    client: Redis | None,
    unit: str,
    call_id: str,
    actual_usd: float,
    lane: str,
    untracked_est_usd: float = 0.0,
) -> None:
    """Settle a call: accrue its actual cost into the lane's running total (and any
    unreadable-usage estimate into the side ledger). Idempotent per call_id; atomic."""
    if client is None:
        return
    script = client.register_script(_ACCRUE_LUA)
    with _span("accrue", unit):
        try:
            await _run_with_retry(
                lambda: script(
                    keys=[
                        _total_cost_key(unit, lane),
                        _untracked_key(unit),
                        _settled_key(unit, call_id),
                        _budget_total_key(unit),
                    ],
                    args=[
                        to_micros(actual_usd),
                        BUDGET_TTL_SECONDS,
                        SETTLED_TTL_SECONDS,
                        to_micros(untracked_est_usd),
                    ],
                )
            )
        except RedisError as exc:
            _note_redis_error("accrue", unit, exc)


async def read_state(client: Redis | None, unit: str) -> BudgetSnapshot | None:
    """Snapshot the meter in one round-trip (and refresh every live key's TTL);
    ``None`` when Redis is unavailable. The gate: a call is admitted unless
    ``remaining_usd`` is present and <= 0 (a missing cap means unenforced —
    fail-open; the server re-hydrates evicted caps from the durable mirror)."""
    if client is None:
        return None
    script = client.register_script(_READ_LUA)
    with _span("read_state", unit):
        try:
            raw = await _run_with_retry(
                lambda: script(keys=_tracked_keys(unit), args=[BUDGET_TTL_SECONDS])
            )
        except RedisError as exc:
            _note_redis_error("read_state", unit, exc)
            return None
    total, *lane_raw, untracked = raw
    # strict: _tracked_keys and _READ_LUA agree on shape by construction, so a mismatch is
    # a bug — and silently zero-filling a lane would UNDER-count spend and over-grant.
    costs = {
        lane: from_micros(_as_int(value))
        for lane, value in zip(LANES, lane_raw, strict=True)
    }
    # '' (or b'') = key absent. A present cap below zero is the explicit "no budget"
    # sentinel; either way there is nothing to enforce, but only absence means the
    # meter needs rebuilding — hence the separate flag.
    total_micros = _as_int(total)
    total_usd = from_micros(total_micros) if total and total_micros >= 0 else None
    return BudgetSnapshot(
        budget_total_usd=total_usd,
        remaining_usd=(total_usd - sum(costs.values()))
        if total_usd is not None
        else None,
        costs_usd=costs,
        untracked_est_usd=from_micros(_as_int(untracked)),
        hydration_needed=not all(raw),
    )


async def claim_hydration(client: Redis | None, unit: str) -> bool:
    """``True`` for the ONE caller that should ask the server to rebuild this meter.

    A batch fans out across hundreds of workers that all read the same missing keys at
    the same time, so this SET-NX collapses that stampede into a single repair request.
    Fail-open on Redis trouble: returns False (skip the request) rather than letting
    every worker through, since the sweep repairs anyway.
    """
    if client is None:
        return False
    try:
        return bool(
            await _run_with_retry(
                lambda: client.set(
                    _hydrate_lock_key(unit),
                    "1",
                    nx=True,
                    ex=HYDRATE_LOCK_TTL_SECONDS,
                )
            )
        )
    except RedisError as exc:
        _note_redis_error("claim_hydration", unit, exc)
        return False


async def hydrate_if_stale(
    client: Redis | None,
    unit: str,
    snap: BudgetSnapshot | None,
    request_hydration: Callable[[], Awaitable[None]],
) -> bool:
    """Ask the server to rebuild a meter whose keys Redis lost. Returns whether a repair
    was requested.

    Does not gate the CURRENT call on the repair. Blocking every worker until the meter is
    rebuilt would park hundreds of containers on a poll loop, and the only thing it buys is
    avoiding one ungated call — which is the pre-call gate's inherent exposure anyway,
    since cost is known only after the call. So this call proceeds and the NEXT one is
    gated: a bounded single-call overshoot instead of stalls and a deadlock class.

    Cost to the one worker that wins the lock: it awaits the request, bounded hard by
    ``HYDRATE_REQUEST_TIMEOUT_SECONDS``. That bound lives here rather than in the caller
    because the injected callback may retry internally (the runner's HTTP helper does),
    which would otherwise stretch a "quick" repair into tens of seconds on the LLM path
    (bugbot). Every other worker returns immediately at the lock.

    ``request_hydration`` is injected so this module keeps its runner-safe dependency set
    (redis/pydantic/loguru) — it must never learn about HTTP or server config.

    Fail-open, and broadly so: this sits on the per-LLM-call path, where raising would fail
    a real customer call to protect a best-effort repair. The whole body is guarded, not
    just the request — a snapshot of an unexpected shape or a client that does not
    implement SET NX must degrade to "no repair requested", never propagate. The sweep
    remains the backstop.
    """
    try:
        if snap is None or not snap.hydration_needed:
            return False
        if not await claim_hydration(client, unit):
            return False  # another worker already asked, or Redis is unavailable
        logger.bind(
            event="budget_cap_missing_hydration_requested", budget_unit=unit
        ).warning(
            f"budget_meter: Redis lost meter state for unit={unit}; requesting rehydrate "
            "(this call proceeds ungated, the next is gated)"
        )
        await asyncio.wait_for(
            request_hydration(), timeout=HYDRATE_REQUEST_TIMEOUT_SECONDS
        )
        return True
    except Exception as exc:
        logger.bind(
            event="budget_meter_hydration_request_failed", budget_unit=unit
        ).warning(
            f"budget_meter: rehydrate request failed for unit={unit}, "
            f"continuing unenforced: {exc!r}"
        )
        return False
