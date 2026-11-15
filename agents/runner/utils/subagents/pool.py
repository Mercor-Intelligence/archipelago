from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict

from runner.agents.harness_descriptor_registry import declared
from runner.agents.models import (
    AgentImpl,
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
)
from runner.save.main import enrich_output_with_captured_diagnostics
from runner.save.webhook import report_trajectory_result
from runner.utils.logging.final_answer import pop_final_answer
from runner.utils.system_instructions import (
    CONTRACT_FAULT,
    system_instructions_check_for_harness,
)

from .result import SubagentResult
from .run import run_child

_CHILD_RESULT_POST_TIMEOUT_S = 10.0


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
    DRAINING = "draining"


@dataclass
class _SharedRunState:
    total_spawned: int = 0
    active_tasks: set[asyncio.Task[SubagentResult]] = field(default_factory=set)
    running_tasks: set[asyncio.Task[SubagentResult]] = field(default_factory=set)
    parent_usage: dict[str, Any] | None = None
    child_tokens: int = 0
    child_prompt_tokens: int = 0
    child_completion_tokens: int = 0
    completed: list[SubagentResult] = field(default_factory=list)
    draining: bool = False
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
        drain_timeout_s: float = 60.0,
        trajectory_id: str | None = None,
        register_child: Callable[[str, str | None, int], Awaitable[str]] | None = None,
        _shared_state: _SharedRunState | None = None,
    ) -> None:
        self.limits = limits
        self.depth = depth
        self.token_budget = token_budget
        self.drain_timeout_s = drain_timeout_s
        self.trajectory_id = trajectory_id
        self.register_child = register_child
        self._state = _shared_state or _SharedRunState()
        self._tasks: dict[str, asyncio.Task[SubagentResult]] = {}
        self._results: dict[str, SubagentResult] = {}
        self._child_trajectory_ids: dict[str, str] = {}
        self._spawn_refs: dict[str, str | None] = {}
        self._started_at: dict[str, float] = {}

    async def __aenter__(self) -> SubagentPool:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.drain(self.drain_timeout_s)

    def child_pool(self, *, child_trajectory_id: str | None = None) -> SubagentPool:
        if (
            self.register_child is not None
            and self.trajectory_id is not None
            and child_trajectory_id is None
        ):
            raise ValueError(
                "child_trajectory_id is required when child registration is enabled"
            )
        return SubagentPool(
            self.limits,
            depth=self.depth + 1,
            token_budget=self.token_budget,
            trajectory_id=(
                child_trajectory_id
                if child_trajectory_id is not None
                else self.trajectory_id
            ),
            register_child=self.register_child,
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
        if self._state.draining:
            raise self._refusal(
                SpawnRefusedCode.DRAINING,
                "Cannot spawn subagent: the run is draining.",
            )
        if self.token_budget and self.tokens_spent() >= self.token_budget:
            raise self._refusal(
                SpawnRefusedCode.BUDGET_EXHAUSTED,
                f"Cannot spawn subagent: token budget ({self.token_budget}) exhausted.",
            )
        if self.depth + 1 > self.limits.max_depth:
            raise self._refusal(
                SpawnRefusedCode.DEPTH_CAP,
                f"Cannot spawn subagent: depth limit ({self.limits.max_depth}) reached.",
            )
        if self._state.total_spawned >= self.limits.max_total:
            raise self._refusal(
                SpawnRefusedCode.TOTAL_CAP,
                f"Cannot spawn subagent: total limit ({self.limits.max_total}) reached.",
            )
        if len(self._state.active_tasks) >= self.limits.max_concurrent:
            raise self._refusal(
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
        self._state.active_tasks.add(task)
        self._state.running_tasks.add(task)

        def store_cancelled_result(
            completed_task: asyncio.Task[SubagentResult],
        ) -> None:
            if completed_task.cancelled() and subagent_id not in self._results:
                self._cancelled_task_result(subagent_id)

        task.add_done_callback(store_cancelled_result)
        task.add_done_callback(self._state.active_tasks.discard)
        task.add_done_callback(self._state.running_tasks.discard)
        return subagent_id

    async def record(
        self,
        *,
        spawn_ref: str | None,
        output: AgentTrajectoryOutput,
        duration_s: float,
        replay: Callable[[], None] | None = None,
        defer_completion: bool = False,
    ) -> SubagentResult:
        sequence = self._state.total_spawned + 1
        subagent_id = f"sub_{sequence}"
        self._state.total_spawned = sequence

        child_trajectory_id: str | None = None
        registration_error: Exception | None = None
        cancelled = False
        if self.register_child is not None and self.trajectory_id is not None:
            registration = asyncio.ensure_future(
                self.register_child(self.trajectory_id, spawn_ref, self.depth + 1)
            )
            try:
                child_trajectory_id = await asyncio.shield(registration)
            except asyncio.CancelledError:
                # The helper already finished: still record it, then propagate.
                cancelled = True
                try:
                    child_trajectory_id = await asyncio.wait_for(
                        registration, timeout=_CHILD_RESULT_POST_TIMEOUT_S
                    )
                except Exception:
                    registration.cancel()
            except Exception as exc:
                registration_error = exc
                logger.exception(
                    "Failed to register subagent trajectory for {}",
                    subagent_id,
                )

        if replay is not None:
            if child_trajectory_id is None and self.trajectory_id is None:
                replay()
            else:
                with logger.contextualize(
                    trajectory_id=child_trajectory_id or self.trajectory_id
                ):
                    replay()

        if registration_error is not None:
            result = SubagentResult(
                subagent_id=subagent_id,
                child_trajectory_id="",
                spawn_ref=spawn_ref,
                depth=self.depth + 1,
                status=AgentStatus.FAILED,
                final_answer=output.final_answer,
                error=f"subagent registration failed: {registration_error}",
                fault=output.fault,
                usage=None,
                duration_s=duration_s,
            )
            self._record_result(subagent_id, result)
            return result

        recorded_output = output
        if child_trajectory_id is not None:
            final_answer = pop_final_answer(child_trajectory_id)
            if recorded_output.final_answer is None:
                recorded_output = recorded_output.model_copy(
                    update={"final_answer": final_answer}
                )

        result = SubagentResult(
            subagent_id=subagent_id,
            child_trajectory_id=child_trajectory_id or "",
            spawn_ref=spawn_ref,
            depth=self.depth + 1,
            status=recorded_output.status,
            final_answer=recorded_output.final_answer,
            error=recorded_output.error,
            fault=recorded_output.fault,
            usage=None,
            duration_s=duration_s,
        )
        self._record_result(subagent_id, result)
        if child_trajectory_id is not None and (not defer_completion or cancelled):
            await self._post_child_result(child_trajectory_id, recorded_output)
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def complete_record(
        self, result: SubagentResult, output: AgentTrajectoryOutput
    ) -> None:
        if result.child_trajectory_id:
            if output.final_answer is None:
                output = output.model_copy(update={"final_answer": result.final_answer})
            await self._post_child_result(result.child_trajectory_id, output)

    def _refusal(self, code: SpawnRefusedCode, message: str) -> SpawnRefusedError:
        self._state.cap_hits[code] += 1
        return SpawnRefusedError(code, message)

    def add_parent_usage(self, usage: dict[str, Any] | None) -> None:
        if usage is None:
            return
        if self._state.parent_usage is None:
            self._state.parent_usage = {
                "total_tokens": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        self._state.parent_usage["total_tokens"] += _usage_tokens(usage)
        prompt_tokens, completion_tokens = _usage_breakdown(usage)
        self._state.parent_usage["prompt_tokens"] += prompt_tokens
        self._state.parent_usage["completion_tokens"] += completion_tokens

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
        async def checked_impl(
            child_input: AgentRunInput,
        ) -> AgentTrajectoryOutput:
            try:
                declared(agent_config_id)
            except KeyError:
                return await impl(child_input)
            with system_instructions_check_for_harness(
                agent_config_id, child_input.initial_messages
            ) as instructions:
                output = await impl(child_input)
                if (
                    instructions is not None
                    and instructions.failure is not None
                    and output.status == AgentStatus.COMPLETED
                ):
                    output.status = AgentStatus.FAILED
                    output.error = instructions.failure
                    output.fault = dict(CONTRACT_FAULT)
            return output

        child_trajectory_id: str | None = None
        if self.register_child is not None and self.trajectory_id is not None:
            registration = asyncio.ensure_future(
                self.register_child(self.trajectory_id, spawn_ref, self.depth + 1)
            )
            try:
                registered_id = await asyncio.shield(registration)
            except asyncio.CancelledError:
                return await self._cancelled_during_registration(
                    subagent_id, spawn_ref, registration, started_at=started_at
                )
            except Exception as exc:
                logger.exception(
                    "Failed to register subagent trajectory for {}",
                    subagent_id,
                )
                result = SubagentResult(
                    subagent_id=subagent_id,
                    child_trajectory_id="",
                    spawn_ref=spawn_ref,
                    depth=self.depth + 1,
                    status=AgentStatus.FAILED,
                    final_answer=None,
                    error=f"subagent registration failed: {exc}",
                    fault=None,
                    usage=None,
                    duration_s=time.monotonic() - started_at,
                )
                self._record_result(subagent_id, result)
                active_task = asyncio.current_task()
                if active_task is not None:
                    self._state.active_tasks.discard(active_task)
                return result
            child_trajectory_id = registered_id
            self._child_trajectory_ids[subagent_id] = registered_id

        terminal_output: AgentTrajectoryOutput | None = None
        try:
            child = await run_child(
                checked_impl,
                run_input,
                agent_config_id=agent_config_id,
                reason="delegated_task",
                sequence=sequence,
                parent_step=parent_step,
                dropped_fields=dropped_fields,
                child_trajectory_id=child_trajectory_id,
            )
        except asyncio.CancelledError:
            result = self._cancelled_result(
                subagent_id, spawn_ref, started_at=started_at
            )
            terminal_output = AgentTrajectoryOutput(
                messages=[],
                status=AgentStatus.CANCELLED,
                time_elapsed=time.monotonic() - started_at,
                error=result.error,
            )
        except Exception as exc:
            result = SubagentResult(
                subagent_id=subagent_id,
                child_trajectory_id=child_trajectory_id or "",
                spawn_ref=spawn_ref,
                depth=self.depth + 1,
                status=AgentStatus.FAILED,
                final_answer=None,
                error=f"{type(exc).__name__}: {exc}",
                fault=None,
                usage=None,
                duration_s=time.monotonic() - started_at,
            )
            terminal_output = AgentTrajectoryOutput(
                messages=[],
                status=AgentStatus.FAILED,
                time_elapsed=time.monotonic() - started_at,
                error=result.error,
            )
        else:
            result = SubagentResult.from_child(
                child,
                subagent_id=subagent_id,
                spawn_ref=spawn_ref,
                depth=self.depth + 1,
                child_trajectory_id=child_trajectory_id,
            )
            terminal_output = child.output
            if (
                terminal_output.final_answer is None
                and child.event.final_answer is not None
            ):
                terminal_output = terminal_output.model_copy(
                    update={"final_answer": child.event.final_answer}
                )

        if child_trajectory_id is not None and terminal_output is not None:
            final_answer = pop_final_answer(child_trajectory_id)
            if terminal_output.final_answer is None:
                terminal_output = terminal_output.model_copy(
                    update={"final_answer": final_answer}
                )
                if result.final_answer is None and final_answer is not None:
                    result = result.model_copy(update={"final_answer": final_answer})
            enrich_output_with_captured_diagnostics(
                child_trajectory_id, terminal_output
            )
            diagnostics: dict[str, Any] = {}
            if result.error is None and terminal_output.error is not None:
                diagnostics["error"] = terminal_output.error
            if result.fault is None and terminal_output.fault is not None:
                diagnostics["fault"] = deepcopy(terminal_output.fault)
            if diagnostics:
                result = result.model_copy(update=diagnostics)

        self._record_result(subagent_id, result)
        active_task = asyncio.current_task()
        if active_task is not None:
            self._state.active_tasks.discard(active_task)
        if child_trajectory_id is not None and terminal_output is not None:
            await self._post_child_result(child_trajectory_id, terminal_output)
        return result

    async def _cancelled_during_registration(
        self,
        subagent_id: str,
        spawn_ref: str | None,
        registration: asyncio.Future[str],
        *,
        started_at: float,
    ) -> SubagentResult:
        """Cancelled while minting the child row: still close any row the server minted."""
        try:
            child_trajectory_id: str | None = await asyncio.wait_for(
                registration, timeout=_CHILD_RESULT_POST_TIMEOUT_S
            )
        except Exception:
            # A late registration would mint a row nothing closes.
            registration.cancel()
            child_trajectory_id = None
        if child_trajectory_id is not None:
            self._child_trajectory_ids[subagent_id] = child_trajectory_id
        result = self._cancelled_result(subagent_id, spawn_ref, started_at=started_at)
        self._record_result(subagent_id, result)
        active_task = asyncio.current_task()
        if active_task is not None:
            self._state.active_tasks.discard(active_task)
        if child_trajectory_id is not None:
            await self._post_child_result(
                child_trajectory_id,
                AgentTrajectoryOutput(
                    messages=[],
                    status=AgentStatus.CANCELLED,
                    time_elapsed=time.monotonic() - started_at,
                    error=result.error,
                ),
            )
        return result

    def _record_result(self, subagent_id: str, result: SubagentResult) -> None:
        self._state.child_tokens += _usage_tokens(result.usage)
        prompt_tokens, completion_tokens = _usage_breakdown(result.usage)
        self._state.child_prompt_tokens += prompt_tokens
        self._state.child_completion_tokens += completion_tokens
        self._store_result(subagent_id, result)

    async def _post_child_result(
        self, child_trajectory_id: str, output: AgentTrajectoryOutput
    ) -> None:
        enrich_output_with_captured_diagnostics(child_trajectory_id, output)
        post_task = asyncio.create_task(
            report_trajectory_result(
                trajectory_id=child_trajectory_id,
                output=output,
                snapshot_id=None,
            )
        )
        deadline = time.monotonic() + _CHILD_RESULT_POST_TIMEOUT_S
        try:
            await asyncio.wait_for(
                asyncio.shield(post_task), timeout=_CHILD_RESULT_POST_TIMEOUT_S
            )
        except asyncio.CancelledError:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(post_task), timeout=remaining)
                except TimeoutError:
                    post_task.cancel()
                except Exception:
                    logger.exception(
                        "Failed to post subagent trajectory result for {}",
                        child_trajectory_id,
                    )
            else:
                post_task.cancel()
            raise
        except TimeoutError:
            post_task.cancel()
            logger.warning(
                "Timed out posting subagent trajectory result for {}",
                child_trajectory_id,
            )
        except Exception:
            logger.exception(
                "Failed to post subagent trajectory result for {}",
                child_trajectory_id,
            )

    def _store_result(self, subagent_id: str, result: SubagentResult) -> None:
        self._results[subagent_id] = result
        if not any(
            completed.subagent_id == subagent_id for completed in self._state.completed
        ):
            self._state.completed.append(result)

    def _cancelled_result(
        self,
        subagent_id: str,
        spawn_ref: str | None,
        *,
        started_at: float,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=subagent_id,
            child_trajectory_id=self._child_trajectory_ids.get(subagent_id, ""),
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
            self._store_result(subagent_id, result)
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
            return self._results.get(subagent_id)
        except asyncio.CancelledError:
            if (
                caller is not None and caller.cancelling() > before
            ) or not task.cancelled():
                raise
            return self._cancelled_task_result(subagent_id)

    async def cancel(self, subagent_id: str) -> SubagentResult:
        task = self._tasks[subagent_id]
        result = self._results.get(subagent_id)
        if result is not None:
            caller = asyncio.current_task()
            before = caller.cancelling() if caller is not None else 0
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if (
                    caller is not None and caller.cancelling() > before
                ) or not task.cancelled():
                    raise
            except Exception:
                pass
            return self._results.get(subagent_id, result)

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
        subagent_ids = list(self._tasks)
        subagent_ids.extend(
            subagent_id
            for subagent_id in self._results
            if subagent_id not in self._tasks
        )
        return [
            self._results[subagent_id]
            for subagent_id in subagent_ids
            if subagent_id in self._results
        ]

    def rollup(self) -> dict[str, Any]:
        """Return a run-wide summary of all completed subagents."""
        results = sorted(
            self._state.completed,
            key=lambda result: int(result.subagent_id.removeprefix("sub_")),
        )
        parent_usage = self._state.parent_usage or {}
        by_status: dict[str, int] = {}
        for result in results:
            status = result.status.value
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "subagents": [result.model_dump(mode="json") for result in results],
            "run_totals": {
                "count": len(results),
                "max_depth": max((result.depth for result in results), default=0),
                "prompt_tokens": (
                    int(parent_usage.get("prompt_tokens") or 0)
                    + self._state.child_prompt_tokens
                ),
                "completion_tokens": (
                    int(parent_usage.get("completion_tokens") or 0)
                    + self._state.child_completion_tokens
                ),
                "total_tokens": self.tokens_spent(),
                "by_status": by_status,
                "cap_hits": {
                    code.value: count for code, count in self._state.cap_hits.items()
                },
            },
        }

    async def drain(self, timeout_s: float) -> list[SubagentResult]:
        """Wait for children and cancel unfinished tasks.

        A cancelled child may leave partial edits.
        """
        is_root = self.depth == 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_s, 0.0)
        if is_root:
            self._state.draining = True
        try:
            while True:
                if is_root:
                    tasks = [
                        task for task in self._state.running_tasks if not task.done()
                    ]
                else:
                    tasks = [self._tasks[subagent_id] for subagent_id in self.running()]
                if not tasks:
                    break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    if await self._cancel_and_wait(tasks):
                        raise asyncio.CancelledError
                    continue

                try:
                    _, pending = await asyncio.wait(tasks, timeout=remaining)
                except asyncio.CancelledError:
                    await self._cancel_and_wait(tasks)
                    raise
                if pending and await self._cancel_and_wait(pending):
                    raise asyncio.CancelledError
            return self.results()
        finally:
            if is_root:
                self._state.draining = False

    async def _cancel_and_wait(
        self, tasks: Iterable[asyncio.Task[SubagentResult]]
    ) -> bool:
        tasks = list(tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        caller_cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                caller_cancelled = True
        await cleanup
        return caller_cancelled


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


def _usage_breakdown(usage: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    prompt_tokens = _coerce_token_count(usage.get("prompt_tokens"))
    completion_tokens = _coerce_token_count(usage.get("completion_tokens"))
    resum_usage = usage.get("resum_usage")
    if isinstance(resum_usage, dict):
        resum_prompt, resum_completion = _usage_breakdown(resum_usage)
        prompt_tokens += resum_prompt
        completion_tokens += resum_completion
    return prompt_tokens, completion_tokens
