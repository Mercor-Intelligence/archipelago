"""Per-cost-unit LLM spend meter, backed by Redis — the shared guardrail core.

CANONICAL SOURCE — vendored verbatim into the archipelago runners by
``scripts/vendor_budget_meter.py`` (parity-tested); edit only this file.
Client-injected: every op takes an async Redis client (or ``None``) so the three
consumers (server, agents runner, grading runner) pass their own.

Model: the Postgres ``budget_state`` column is the source of truth; Redis is a 2-day cache.
``budget_total:{unit}`` holds the cap, ``tc_traj/tc_grading:{unit}`` accrue real cost
per settled call (mirrored to Postgres via GREATEST), ``est_untracked:{unit}`` is a
side ledger for unreadable-usage calls, ``settled:{unit}:{cid}`` dedupes retried
accruals. remaining = total − cost, always derived (never stored).
Keys are hash-tagged ``{unit}`` and hydrated from the mirror on miss (see server).

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
_MICROS_PER_USD = 1_000_000

LANE_TRAJECTORY = "traj"
LANE_GRADING = "grading"

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
# distinguishable from "zero"; the cap being absent means the batch is unenforced.
# KEYS = [budget_total, tc_traj, tc_grading, est_untracked]; ARGV = [ttl_seconds].
_READ_LUA = """
local out = {}
for i = 1, 4 do
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

# Raise a counter to at least `base` (hydration floor from the durable mirror) and
# refresh its TTL. Never lowers a live counter. KEYS=[counter]; ARGV=[base, ttl].
_FLOOR_LUA = """
local cur = tonumber(redis.call('GET', KEYS[1])) or 0
local base = tonumber(ARGV[1])
if base > cur then redis.call('SET', KEYS[1], base) end
if base > 0 or cur > 0 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])) end
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
    """Point-in-time meter read (USD). ``total_*`` are running totals (mirror model);
    ``remaining_usd`` is derived = total budget − total cost, None when unenforced."""

    budget_total_usd: float | None
    remaining_usd: float | None
    total_traj_usd: float
    total_grading_usd: float
    untracked_est_usd: float


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


# Keys are hash-tagged on {unit} for cluster-slot locality.
def _budget_total_key(unit: str) -> str:
    return f"budget_total:{{{unit}}}"


def _settled_key(unit: str, call_id: str) -> str:
    return f"settled:{{{unit}}}:{call_id}"


def _total_cost_key(unit: str, lane: str) -> str:
    return f"tc_{lane}:{{{unit}}}"


def _untracked_key(unit: str) -> str:
    return f"est_untracked:{{{unit}}}"


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


async def set_budget_total(client: Redis | None, unit: str, total_usd: float) -> None:
    """Set the total-budget cap (mirrors the DB value; idempotent)."""
    if client is None:
        return
    with _span("set_budget_total", unit):
        try:
            await _run_with_retry(
                lambda: client.set(
                    _budget_total_key(unit), to_micros(total_usd), ex=BUDGET_TTL_SECONDS
                )
            )
        except RedisError as exc:
            _note_redis_error("set_budget_total", unit, exc)


async def hydrate(
    client: Redis | None,
    unit: str,
    *,
    total_usd: float | None,
    traj_usd: float,
    grading_usd: float,
    untracked_usd: float,
) -> None:
    """Rebuild evicted/expired keys from the durable mirror: SET the cap, floor each
    counter to the mirrored value (never lowers), refresh TTLs. Safe to run every sweep.
    """
    if client is None:
        return
    with _span("hydrate", unit):
        try:
            if total_usd is not None:
                await set_budget_total(client, unit, total_usd)
            script = client.register_script(_FLOOR_LUA)
            for key, base in (
                (_total_cost_key(unit, LANE_TRAJECTORY), traj_usd),
                (_total_cost_key(unit, LANE_GRADING), grading_usd),
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
            total, tc_traj, tc_grading, untracked = await _run_with_retry(
                lambda: script(
                    keys=[
                        _budget_total_key(unit),
                        _total_cost_key(unit, LANE_TRAJECTORY),
                        _total_cost_key(unit, LANE_GRADING),
                        _untracked_key(unit),
                    ],
                    args=[BUDGET_TTL_SECONDS],
                )
            )
        except RedisError as exc:
            _note_redis_error("read_state", unit, exc)
            return None
    traj_usd = from_micros(_as_int(tc_traj))
    grading_usd = from_micros(_as_int(tc_grading))
    # '' (or b'') = key absent -> no cap set -> not enforced.
    total_usd = from_micros(_as_int(total)) if total else None
    return BudgetSnapshot(
        budget_total_usd=total_usd,
        remaining_usd=(total_usd - traj_usd - grading_usd)
        if total_usd is not None
        else None,
        total_traj_usd=traj_usd,
        total_grading_usd=grading_usd,
        untracked_est_usd=from_micros(_as_int(untracked)),
    )
