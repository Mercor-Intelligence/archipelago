"""Cost accounting for harnesses whose model calls happen outside Studio.

A CLI harness runs a vendor CLI that opens its own connection to the gateway, so
the in-process gate/accrue hooks in ``runner.utils.llm`` never see a call and the
batch's trajectory lane stays at $0. What the harness does report is aggregate
token counts at the end of the run; this module prices those.
"""

from dataclasses import dataclass
from functools import partial
from importlib import import_module
from typing import Any, cast
from uuid import uuid4

import litellm
from loguru import logger
from redis.asyncio import Redis

from runner.utils import budget_meter
from runner.utils.budget_hydration import request_meter_hydration
from runner.utils.decorators import (
    budget_enabled_ctx,
    budget_stop_ctx,
    model_rates_ctx,
    trajectory_batch_id_ctx,
)

# Conservative fallback rates for unknown models (same values as runner.utils.llm).
_DEFAULT_INPUT_RATE = 1e-5
_DEFAULT_OUTPUT_RATE = 3e-5

# Redis client for the batch-spend meter. Some contexts (tests, standalone runs)
# don't configure Redis — fall back to None so the meter no-ops. Mirrors
# runner.utils.llm.
_budget_redis: Redis | None
try:
    _budget_redis = cast(Redis | None, import_module("runner.utils.redis").redis_client)
except (ImportError, ValueError, AttributeError):
    _budget_redis = None


@dataclass(frozen=True)
class _Rates:
    input: float
    output: float
    cached: float
    creation: float


def _with_cache_rates(
    in_rate: float,
    out_rate: float,
    cached: float | None,
    creation: float | None,
) -> _Rates:
    return _Rates(
        input=in_rate,
        output=out_rate,
        cached=cached if cached is not None else in_rate * 0.1,
        creation=creation if creation is not None else in_rate * 1.25,
    )


def _optional_rate(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return float(value)
    return None


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_rates(model: str) -> _Rates | None:
    """Per-token rates for `model`: server-threaded rate card, then litellm.

    ``None`` when neither knows the model, so the caller can record the run
    unpriced instead of inventing a number.
    """
    ctx = model_rates_ctx.get() or {}
    in_rate = float(ctx.get("input_cost_per_token") or 0.0)
    out_rate = float(ctx.get("output_cost_per_token") or 0.0)
    if in_rate or out_rate:
        return _with_cache_rates(
            in_rate,
            out_rate,
            _optional_rate(ctx, "cached_input_cost_per_token"),
            _optional_rate(ctx, "cache_creation_cost_per_token"),
        )
    try:
        info: dict[str, Any] = dict(litellm.get_model_info(model) or {})
    except Exception:
        return None
    in_rate = float(info.get("input_cost_per_token") or 0.0)
    out_rate = float(info.get("output_cost_per_token") or 0.0)
    if not in_rate and not out_rate:
        return None
    return _with_cache_rates(
        in_rate,
        out_rate,
        _optional_rate(info, "cache_read_input_token_cost"),
        _optional_rate(info, "cache_creation_input_token_cost"),
    )


def _apply(usage: dict[str, Any], rates: _Rates) -> float:
    """Cache-aware cost of `usage` at `rates`, taking ``prompt_tokens`` to
    include the cached and cache-creation slices."""
    prompt = _as_float(usage.get("prompt_tokens"))
    completion = _as_float(usage.get("completion_tokens"))
    cached = _as_float(usage.get("cached_tokens"))
    creation = _as_float(usage.get("cache_creation_tokens"))
    uncached = max(prompt - cached - creation, 0.0)
    return (
        uncached * rates.input
        + cached * rates.cached
        + creation * rates.creation
        + completion * rates.output
    )


def price_usage_metrics(
    usage: dict[str, Any] | None,
    model: str,
    *,
    default_rates: bool = True,
) -> float | None:
    """Cost (USD) of one harness run's aggregated token counts, cache-aware.

    Mirrors the server-side ``packages/budget/estimate.py:price_usage``. Returns
    None when there is nothing to price, and — with ``default_rates=False`` —
    when the model has no rate card.
    """
    if not usage:
        return None
    rates = _resolve_rates(model)
    if rates is None:
        if not default_rates:
            return None
        rates = _with_cache_rates(_DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE, None, None)
    return _apply(usage, rates)


async def gate_trajectory_start(*, model: str, source: str) -> str | None:
    """Refuse to start a CLI harness run once the batch has spent its budget.

    The vendor CLI's calls never reach the in-process gate, so a running eval
    can't be stopped; the entry gate is the only enforcement point, and the real
    cost lands afterwards via :func:`settle_trajectory_cost`. Returns the budget
    unit to accrue against, or None when metering is off. Fails open on Redis
    trouble.
    """
    budget_unit = trajectory_batch_id_ctx.get() if budget_enabled_ctx.get() else None
    if budget_unit is None:
        return None
    snap = await budget_meter.read_state(
        _budget_redis, budget_unit, lanes=budget_meter.BATCH_LANES
    )
    # Redis lost this unit's keys, so the gate below would read "no cap" as
    # "unenforced". Ask the server to rebuild from the durable mirror: this call
    # proceeds ungated and the next one is gated again.
    await budget_meter.hydrate_if_stale(
        _budget_redis,
        budget_unit,
        snap,
        partial(request_meter_hydration, budget_unit),
    )
    if snap and snap.remaining_usd is not None and snap.remaining_usd <= 0:
        # Stash the structured stop so the worker persists it; the raised error is
        # otherwise swallowed by the agent's generic handler.
        budget_stop_ctx.set(
            {
                "reason": "budget_exceeded",
                "cost_unit": budget_unit,
                "remaining_usd": snap.remaining_usd,
                "model": model,
            }
        )
        logger.bind(
            event="budget_call_denied",
            budget_unit=budget_unit,
            model=model,
        ).warning(f"{source} run denied: batch {budget_unit} over budget")
        raise budget_meter.BudgetExceededError(
            budget_unit, remaining_usd=snap.remaining_usd, model=model
        )
    return budget_unit


def _has_tokens(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(
        _as_float(usage.get(key)) > 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


async def settle_trajectory_cost(
    *,
    usage: Any,
    model: str,
    trajectory_id: str | None,
    source: str,
) -> None:
    """Price a CLI harness run and record it on the trajectory and the meter.

    Mutates `usage` in place: ``cost_usd_spent`` when the model is priced,
    ``cost_unpriced_calls`` when it isn't. Never raises — a pricing miss must not
    fail a completed run.
    """
    if not _has_tokens(usage):
        return
    call_id = (
        f"{source}:{trajectory_id}" if trajectory_id else f"{source}:{uuid4().hex}"
    )
    usage["accounting_mode"] = "harness_cost"
    cost: float | None = None
    untracked = 0.0
    try:
        cost = price_usage_metrics(usage, model, default_rates=False)
        if cost is None:
            untracked = price_usage_metrics(usage, model) or 0.0
    except Exception as exc:
        logger.bind(event="harness_cost_error", model=model).warning(
            f"{source} run pricing failed ({exc!r}); trajectory stays unpriced"
        )
    if cost is None:
        usage["cost_unpriced_calls"] = 1
        logger.bind(
            event="llm_usage_missing_estimate_fallback",
            model=model,
            budget_call_id=call_id,
            estimated_usd=untracked,
        ).warning(
            f"{source} run has no rate card for {model}; counted $0 real cost, "
            f"${untracked:.6f} to the untracked ledger"
        )
    else:
        usage["cost_usd_spent"] = cost
    budget_unit = trajectory_batch_id_ctx.get() if budget_enabled_ctx.get() else None
    if budget_unit is None:
        return
    await budget_meter.accrue(
        _budget_redis,
        budget_unit,
        call_id,
        cost or 0.0,
        budget_meter.LANE_TRAJECTORY,
        untracked_est_usd=untracked,
    )
