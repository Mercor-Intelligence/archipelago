from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from runner.agents.models import AgentImpl, AgentRunInput, AgentStatus

from .result import SubagentResult
from .run import run_child


class SubagentLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_total: int = 10
    max_concurrent: int = 3
    max_depth: int = 1

    @classmethod
    def from_config(cls, values: dict[str, Any]) -> SubagentLimits:
        def read_int(key: str, default: int, minimum: int) -> int:
            try:
                value = int(values.get(key, default))
            except (TypeError, ValueError, OverflowError):
                value = default
            return max(value, minimum)

        return cls(
            max_total=read_int("max_subagents", 10, 0),
            max_concurrent=read_int("max_concurrent_subagents", 3, 1),
            max_depth=read_int("max_subagent_depth", 1, 1),
        )


class SpawnRefusedCode(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOTAL_CAP = "total_cap"
    CONCURRENT_CAP = "concurrent_cap"
    DEPTH_CAP = "depth_cap"


@dataclass
class _SharedRunState:
    total_spawned: int = 0
    running_tasks: set[asyncio.Task[SubagentResult]] = field(default_factory=set)
    parent_usage: dict[str, Any] | None = None
    child_tokens: int = 0
    cap_hits: dict[SpawnRefusedCode, int] = field(
        default_factory=lambda: {code: 0 for code in SpawnRefusedCode}
    )


class SpawnRefusedError(Exception):
    def __init__(self, code: SpawnRefusedCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class SubagentPool:
    def __init__(
        self,
        limits: SubagentLimits,
        *,
        depth: int = 0,
        token_budget: int | None = None,
        _shared_state: _SharedRunState | None = None,
    ) -> None:
        self.limits = limits
        self.depth = depth
        self.token_budget = token_budget
        self._state = _shared_state or _SharedRunState()
        self._tasks: dict[str, asyncio.Task[SubagentResult]] = {}
        self._results: dict[str, SubagentResult] = {}
        self._spawn_refs: dict[str, str | None] = {}
        self._started_at: dict[str, float] = {}

    def child_pool(self) -> SubagentPool:
        return SubagentPool(
            self.limits,
            depth=self.depth + 1,
            token_budget=self.token_budget,
            _shared_state=self._state,
        )

    async def spawn(
        self,
        impl: AgentImpl,
        run_input: AgentRunInput,
        *,
        agent_config_id: str,
        spawn_ref: str | None = None,
        parent_step: int | None = None,
        dropped_fields: frozenset[str] = frozenset(),
    ) -> str:
        if self.token_budget and self.tokens_spent() >= self.token_budget:
            raise SpawnRefusedError(
                SpawnRefusedCode.BUDGET_EXHAUSTED,
                f"Cannot spawn subagent: token budget ({self.token_budget}) exhausted.",
            )
        if self.depth + 1 > self.limits.max_depth:
            raise SpawnRefusedError(
                SpawnRefusedCode.DEPTH_CAP,
                f"Cannot spawn subagent: depth limit ({self.limits.max_depth}) reached.",
            )
        if self._state.total_spawned >= self.limits.max_total:
            raise SpawnRefusedError(
                SpawnRefusedCode.TOTAL_CAP,
                f"Cannot spawn subagent: total limit ({self.limits.max_total}) reached.",
            )
        if (
            sum(not task.done() for task in self._state.running_tasks)
            >= self.limits.max_concurrent
        ):
            raise SpawnRefusedError(
                SpawnRefusedCode.CONCURRENT_CAP,
                f"Cannot spawn subagent: concurrent limit ({self.limits.max_concurrent}) reached.",
            )

        sequence = self._state.total_spawned + 1
        subagent_id = f"sub_{sequence}"
        started_at = time.monotonic()
        self._started_at[subagent_id] = started_at
        self._spawn_refs[subagent_id] = spawn_ref
        task = asyncio.create_task(
            self._run_child(
                impl,
                run_input,
                subagent_id=subagent_id,
                sequence=sequence,
                agent_config_id=agent_config_id,
                spawn_ref=spawn_ref,
                parent_step=parent_step,
                dropped_fields=dropped_fields,
                started_at=started_at,
            )
        )
        self._tasks[subagent_id] = task
        self._state.total_spawned = sequence
        self._state.running_tasks.add(task)
        task.add_done_callback(self._state.running_tasks.discard)
        return subagent_id

    def add_parent_usage(self, usage: dict[str, Any] | None) -> None:
        if usage is None:
            return
        if self._state.parent_usage is None:
            self._state.parent_usage = {"total_tokens": 0}
        self._state.parent_usage["total_tokens"] += _usage_tokens(usage)

    def tokens_spent(self) -> int:
        parent_tokens = int((self._state.parent_usage or {}).get("total_tokens") or 0)
        return parent_tokens + self._state.child_tokens

    async def _run_child(
        self,
        impl: AgentImpl,
        run_input: AgentRunInput,
        *,
        subagent_id: str,
        sequence: int,
        agent_config_id: str,
        spawn_ref: str | None,
        parent_step: int | None,
        dropped_fields: frozenset[str],
        started_at: float,
    ) -> SubagentResult:
        try:
            child = await run_child(
                impl,
                run_input,
                agent_config_id=agent_config_id,
                reason="delegated_task",
                sequence=sequence,
                parent_step=parent_step,
                dropped_fields=dropped_fields,
            )
        except asyncio.CancelledError:
            result = self._cancelled_result(
                subagent_id, spawn_ref, started_at=started_at
            )
        except Exception as exc:
            result = SubagentResult(
                subagent_id=subagent_id,
                child_trajectory_id="",
                spawn_ref=spawn_ref,
                depth=self.depth + 1,
                status=AgentStatus.FAILED,
                final_answer=None,
                error=f"{type(exc).__name__}: {exc}",
                fault=None,
                usage=None,
                duration_s=time.monotonic() - started_at,
            )
        else:
            result = SubagentResult.from_child(
                child,
                subagent_id=subagent_id,
                spawn_ref=spawn_ref,
                depth=self.depth + 1,
            )
        self._state.child_tokens += _usage_tokens(result.usage)
        self._results[subagent_id] = result
        return result

    def _cancelled_result(
        self,
        subagent_id: str,
        spawn_ref: str | None,
        *,
        started_at: float,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=subagent_id,
            child_trajectory_id="",
            spawn_ref=spawn_ref,
            depth=self.depth + 1,
            status=AgentStatus.CANCELLED,
            final_answer=None,
            error="cancelled",
            fault=None,
            usage=None,
            duration_s=time.monotonic() - started_at,
        )

    def _cancelled_task_result(self, subagent_id: str) -> SubagentResult:
        result = self._results.get(subagent_id)
        if result is None:
            result = self._cancelled_result(
                subagent_id,
                self._spawn_refs[subagent_id],
                started_at=self._started_at[subagent_id],
            )
            self._results[subagent_id] = result
        return result

    async def wait(
        self, subagent_id: str, timeout_s: float | None = None
    ) -> SubagentResult | None:
        task = self._tasks[subagent_id]
        caller = asyncio.current_task()
        before = caller.cancelling() if caller is not None else 0
        try:
            if timeout_s is None:
                return await asyncio.shield(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            if (
                caller is not None and caller.cancelling() > before
            ) or not task.cancelled():
                raise
            return self._cancelled_task_result(subagent_id)

    async def cancel(self, subagent_id: str) -> SubagentResult:
        task = self._tasks[subagent_id]
        task.cancel()
        caller = asyncio.current_task()
        before = caller.cancelling() if caller is not None else 0
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if (
                caller is not None and caller.cancelling() > before
            ) or not task.cancelled():
                raise
            return self._cancelled_task_result(subagent_id)

    def running(self) -> list[str]:
        return [
            subagent_id for subagent_id, task in self._tasks.items() if not task.done()
        ]

    def results(self) -> list[SubagentResult]:
        return [
            self._results[subagent_id]
            for subagent_id in self._tasks
            if subagent_id in self._results
        ]


def _usage_tokens(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        tokens = _coerce_token_count(usage.get("prompt_tokens")) + _coerce_token_count(
            usage.get("completion_tokens")
        )
    else:
        tokens = _coerce_token_count(usage.get("total_tokens"))
    resum_usage = usage.get("resum_usage")
    if isinstance(resum_usage, dict):
        tokens += _usage_tokens(resum_usage)
    return tokens


def _coerce_token_count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
