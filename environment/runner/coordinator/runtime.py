import asyncio
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from loguru import logger
from pydantic import JsonValue

from runner.utils.metrics import (
    distribution,
    increment,
    record_latency,
    record_latency_and_outcome,
)
from runner.utils.metrics import flush as flush_metrics

from .agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    COORDINATOR_DISPATCH_ORIGIN,
    TOOL_CALL_ACTOR_KEY,
    TOOL_CALL_ORIGIN_KEY,
    AgentRunRecord,
    VirtualCoworkerAgent,
)
from .checkpoints.check_occurrences import get_event_occurrences
from .checkpoints.models import (
    ActionDispatch,
    ActionDispatchStatus,
    Checkpoint,
    EventOccurrence,
    PeriodicCheckpoint,
)
from .config.models import CoordinatorConfig
from .events.models import (
    CallMCPToolAction,
    EventAction,
    EventDefinition,
    InvokeAgentAction,
)
from .state.store import CoordinatorStore
from .utils import (
    get_archipelago_agents_cwd,
    get_mcp_gateway_url,
    summarize_tool_result,
    utc_now,
    validate_mcp_gateway_url,
)

# Fallback for invoke_agent actions with no timeout_seconds, so no persona
# subprocess waits unbounded.
DEFAULT_VCA_INVOKE_TIMEOUT_SECONDS = 900

# Fraction of a persona's budget reserved for its own shutdown, floored so
# short budgets still leave a usable window.
INNER_TIMEOUT_HEADROOM_RATIO = 0.1
MIN_INNER_TIMEOUT_HEADROOM_SECONDS = 30

# Killing the process group SIGTERMs first so the persona's *child* processes
# (the shells it runs tools through) can close their files. The agent itself
# installs no SIGTERM handler, so it dies on the signal either way — its own
# output is saved by `agent_seconds` firing first, not by this window.
SIGTERM_GRACE_SECONDS = 10

# Slack over the longest persona chain when sizing the pre-snapshot drain.
FINISH_ACTIONS_HEADROOM_SECONDS = 300

# After the drain cancels a straggler, how long to let it unwind so the actor
# lock its `finally` holds is released before the snapshot reads the tree.
CANCELLED_ACTION_GRACE_SECONDS = 5.0

# Statuses an agent can self-report in output.json that mean the run did not
# succeed, even if it left output behind.
FAILED_AGENT_RUN_STATUSES = {"failed", "error", "cancelled"}

# How much of a failed persona's stderr to copy into the log line.
STDERR_TAIL_CHARS = 4_000

# Metric emits are fire-and-forget tasks; the sandbox is torn down straight
# after the drain, so give them a bounded chance to land first.
METRICS_FLUSH_SECONDS = 10.0

# Terminal classification of one persona subprocess run. `skipped` means the
# actor lock was already held, so no subprocess ever started;
# `timed_out_salvaged` means the budget expired but the run left a usable
# output.json, so it is counted as a success rather than a failure.
PersonaOutcome = Literal[
    "completed",
    "timed_out",
    "timed_out_salvaged",
    "error_exit",
    "no_output",
    "agent_failed",
    "skipped",
]


@dataclass(frozen=True)
class PersonaDeadlines:
    """One persona budget, expanded into the nested deadlines that enforce it.

    Ordered by construction: ``agent_seconds`` < ``total_seconds`` <
    ``total_seconds + sigterm_grace_seconds``. The agent stops itself and writes
    output.json before the Coordinator ever has to kill it.
    """

    total_seconds: int

    @classmethod
    def for_action(cls, action: InvokeAgentAction) -> "PersonaDeadlines":
        return cls(action.timeout_seconds or DEFAULT_VCA_INVOKE_TIMEOUT_SECONDS)

    @property
    def agent_seconds(self) -> int:
        headroom = max(
            MIN_INNER_TIMEOUT_HEADROOM_SECONDS,
            int(self.total_seconds * INNER_TIMEOUT_HEADROOM_RATIO),
        )
        return max(1, self.total_seconds - headroom)

    @property
    def sigterm_grace_seconds(self) -> float:
        return SIGTERM_GRACE_SECONDS

    @property
    def wall_seconds(self) -> float:
        """Longest the Coordinator can be held by this action."""
        return self.total_seconds + self.sigterm_grace_seconds


def _persona_base_tags(vca: VirtualCoworkerAgent) -> list[str]:
    cfg = vca.vca_harness_config
    return [f"agent_id:{cfg.agent_id}", f"orchestrator_model:{cfg.orchestrator_model}"]


def _finish_actions_budget_seconds(config: CoordinatorConfig) -> float:
    """Drain ceiling: the longest sequential persona chain in any one event.

    Derived from the timeouts actually configured rather than the fallback
    constant, so chaining personas can't silently outlive the drain.
    """
    longest = 0.0
    for event in config.events:
        chain = sum(
            PersonaDeadlines.for_action(action).wall_seconds
            for action in event.actions
            if isinstance(action, InvokeAgentAction)
        )
        longest = max(longest, chain)
    return longest + FINISH_ACTIONS_HEADROOM_SECONDS


class Coordinator:
    def __init__(
        self,
        *,
        root: Path | None = None,
    ) -> None:
        self.store = CoordinatorStore(root=root)
        self._mcp_proxy: FastMCP
        self._mcp_gateway_url = get_mcp_gateway_url()
        self._cron_tasks: set[asyncio.Task[None]] = set()
        self._action_tasks: set[asyncio.Task[None]] = set()
        self._requeued_events: set[str] = set()
        self._started = False

    @property
    def is_started(self) -> bool:
        """Whether the coordinator is running. Public read-only interface for other
        modules (e.g. the gateway middleware) that gate on coordinator state, so the
        coupling is explicit rather than reaching into the private ``_started``."""
        return self._started

    # ---------------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------------

    async def start(
        self,
        *,
        mcp_proxy: FastMCP,
        config: CoordinatorConfig | None = None,
    ) -> None:
        self._mcp_proxy = mcp_proxy
        if config is not None and self._started:
            await self.stop()
        if config is not None:
            self.store.config.write(config)
        if self._started:
            return
        config = self.store.config.read()
        # From disk, so a restart with no fresh push scopes to the same run.
        self.store.event_occurrences.run_id = config.run_id
        if not config.enabled:
            logger.debug("Environment Coordinator disabled")
            return

        self.store.agent_configs.validate_configs(config.agents.values())
        await validate_mcp_gateway_url(self._mcp_gateway_url)
        self._started = True
        for checkpoint in config.checkpoints:
            if checkpoint.type == "periodic":
                task = asyncio.create_task(self._cron_loop(checkpoint))
                self._cron_tasks.add(task)
                task.add_done_callback(self._cron_tasks.discard)
                break
        logger.info(
            "Environment Coordinator started config=" + config.model_dump_log_json()
        )

    async def stop(self) -> None:
        for task in self._cron_tasks:
            task.cancel()
        for task in list(self._cron_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._cron_tasks.clear()
        await self.finish_actions()
        self._started = False

    async def finish_actions(self) -> None:
        """
        Let the coordinator finish executing actions before Archipelago snapshots.

        Bounded by the configured persona timeouts; stragglers are cancelled.
        """
        if not self._started:
            return
        config = self.store.config.read()
        if not config.enabled:
            return
        drained = 0
        budget = _finish_actions_budget_seconds(config)
        with record_latency("studio.vca.drain.duration_seconds"):
            for checkpoint in config.checkpoints:
                if checkpoint.type == "periodic":
                    await self._check_for_event_occurrences(checkpoint, config.events)
                    break
            deadline = time.monotonic() + budget
            while self._action_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stuck = {t for t in self._action_tasks if not t.done()}
                    increment(
                        "studio.vca.drain.abandoned",
                        tags=["reason:budget_exceeded"],
                        value=len(stuck),
                    )
                    logger.error(
                        f"Environment Coordinator finish_actions exceeded {budget}s; "
                        f"abandoning {len(stuck)} pending action task(s) so the "
                        "snapshot can proceed: "
                        + ", ".join(sorted(t.get_name() for t in stuck))
                    )
                    for task in stuck:
                        task.cancel()
                    # A cancel only unwinds once the loop resumes the task, and
                    # the actor lock is released by a `finally` on the way out.
                    # Returning here instead would carry live locks into the
                    # snapshot, where every continuation reads them as busy.
                    _, still_running = await asyncio.wait(
                        stuck, timeout=CANCELLED_ACTION_GRACE_SECONDS
                    )
                    if still_running:
                        increment(
                            "studio.vca.drain.abandoned",
                            tags=["reason:cancel_ignored"],
                            value=len(still_running),
                        )
                        logger.error(
                            "Environment Coordinator action task(s) ignored "
                            f"cancellation within {CANCELLED_ACTION_GRACE_SECONDS}s; "
                            "their actor locks may survive into the snapshot: "
                            + ", ".join(sorted(t.get_name() for t in still_running))
                        )
                    break
                done, _ = await asyncio.wait(set(self._action_tasks), timeout=remaining)
                drained += len(done)
                for task in done:
                    exc = task.exception() if not task.cancelled() else None
                    if exc is not None:
                        logger.error(
                            f"Environment Coordinator action task {task.get_name()} "
                            f"raised: {exc!r}"
                        )
        distribution("studio.vca.drain.tasks", float(drained))
        # Last thing before the snapshot: nothing emitted above survives the
        # teardown that follows unless it has already been submitted.
        await flush_metrics(METRICS_FLUSH_SECONDS)

    # ---------------------------------------------------------------------------------
    # Checkpoints
    # ---------------------------------------------------------------------------------

    async def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, JsonValue],
        actor_id: str,
        agent_identity: dict[str, str] | None = None,
        result: ToolResult | None = None,
        error: str | None = None,
        error_type: str | None = None,
    ) -> None:
        if not self._started:
            return
        config = self.store.config.read()
        if not config.enabled:
            return

        observation = self.store.observations.tool_calls.record(
            actor_id=actor_id,
            tool_name=tool_name,
            arguments=arguments,
            result_summary=summarize_tool_result(result)
            if result is not None
            else None,
            error=error,
        )
        # Size, not content: a tool result is agent-controlled, so `env` output lands here
        # verbatim. It stays in mcp_calls.jsonl, which is where grading reads it from.
        result_text = (observation.result_summary or {}).get("text")
        result_chars = len(str(result_text)) if result_text is not None else 0
        # Attribute the gateway's own tool-call audit to the specific agent revision that
        # made it (empty when the runner didn't stamp the headers — CLI / older server).
        attribution = "".join(
            f" {field}={value}" for field, value in (agent_identity or {}).items()
        )
        logger.info(
            "Environment Coordinator recorded MCP call "
            + f"sequence={observation.sequence} actor={actor_id} tool={tool_name} "
            + f"error_type={error_type} result_chars={result_chars}"
            + attribution
        )
        for checkpoint in config.checkpoints:
            if checkpoint.type == "tool_call":
                await self._check_for_event_occurrences(checkpoint, config.events)
                break

    async def _cron_loop(self, checkpoint: PeriodicCheckpoint) -> None:
        while True:
            await asyncio.sleep(checkpoint.interval_seconds)
            try:
                config = self.store.config.read()
                if config.enabled:
                    await self._check_for_event_occurrences(checkpoint, config.events)
            except Exception as e:
                logger.error(f"Environment Coordinator cron tick failed: {repr(e)}")

    async def _check_for_event_occurrences(
        self, checkpoint: Checkpoint, events: list[EventDefinition]
    ) -> None:
        observations = self.store.observations.read()
        occurred_event_ids = self.store.event_occurrences.event_ids()
        for occurrence in get_event_occurrences(
            events,
            checkpoint,
            observations,
            occurred_event_ids,
        ):
            # create_task copies the current context, so every log the event's
            # actions emit — down to a persona's stderr tail — carries these.
            with logger.contextualize(
                vca_event_id=occurrence.event.event_id,
                vca_trigger_type=occurrence.trigger.type,
                vca_checkpoint_type=checkpoint.type,
            ):
                if not self.store.event_occurrences.create(occurrence):
                    logger.info(
                        "Environment Coordinator skipped duplicate event occurrence "
                        + f"event={occurrence.event.event_id} "
                        + f"checkpoint={checkpoint.type}"
                    )
                    continue
                logger.info(
                    "Environment Coordinator created event occurrence "
                    + f"event={occurrence.event.event_id} checkpoint={checkpoint.type} "
                    + f"trigger={occurrence.trigger.type}"
                )
                # Each event action is run sequentially, run together in same background task.
                # Name carries invoke_agent actors so the drain's abandon log names the persona.
                actors = ",".join(
                    a.actor_id
                    for a in occurrence.event.actions
                    if a.type == "invoke_agent"
                )
                task_name = f"event_actions:{occurrence.event.event_id}"
                if actors:
                    task_name += f" actors={actors}"
                task = asyncio.create_task(
                    self._run_event_actions(occurrence),
                    name=task_name,
                )
                self._action_tasks.add(task)
                task.add_done_callback(self._action_tasks.discard)

    # ---------------------------------------------------------------------------------
    # Event Actions
    # ---------------------------------------------------------------------------------

    async def _run_event_actions(self, occurrence: EventOccurrence) -> None:
        failed_action_id: str | None = None
        for action in occurrence.event.actions:
            dispatch = await self._run_event_action(occurrence.event, action)
            occurrence.dispatches.append(dispatch)
            self.store.event_occurrences.write(occurrence)
            logger.info(
                "Environment Coordinator action dispatch recorded "
                + f"event={occurrence.event.event_id} action={action.action_id} "
                + f"type={action.type} status={dispatch.status} "
                + f"errored={dispatch.error is not None} "
                + f"output_keys={sorted(dispatch.output or ())}"
            )
            if dispatch.status == "failed":
                failed_action_id = failed_action_id or action.action_id
                if action.on_failure == "abort":
                    break
                continue
            if dispatch.status == "skipped":
                # The actor was mid-run, so this action never happened.
                # Independent of on_failure: the action never ran, so there is
                # nothing to abort or continue past. An occurrence is what
                # stops the trigger matching again, so forgetting it lets the
                # event come back around — but only when nothing before it has
                # already landed. Re-running an event replays it from the
                # first action, and a completed tool call or persona run would
                # fire a second time.
                if any(
                    earlier.status == "completed"
                    for earlier in occurrence.dispatches[:-1]
                ):
                    occurrence.status = "failed"
                    self.store.event_occurrences.write(occurrence)
                    increment(
                        "studio.vca.event.completed",
                        tags=[
                            "status:failed",
                            f"trigger_type:{occurrence.trigger.type}",
                        ],
                    )
                    logger.bind(vca_event_status="failed").error(
                        "Environment Coordinator event stopped part-way: actor "
                        + "was busy and earlier actions already took effect, so "
                        + "it cannot be retried without repeating them "
                        + f"event={occurrence.event.event_id} "
                        + f"action={action.action_id}"
                    )
                    return
                self.store.event_occurrences.discard(occurrence.event.event_id)
                # A requeued event re-triggers on every checkpoint until the
                # actor frees up, which is a tick a second for the length of a
                # persona run. Count it once so the retries stay quiet.
                first_requeue = occurrence.event.event_id not in self._requeued_events
                self._requeued_events.add(occurrence.event.event_id)
                if first_requeue:
                    increment(
                        "studio.vca.event.completed",
                        tags=[
                            "status:requeued",
                            f"trigger_type:{occurrence.trigger.type}",
                        ],
                    )
                bound = logger.bind(vca_event_status="requeued")
                log = bound.info if first_requeue else bound.debug
                log(
                    "Environment Coordinator event requeued because its actor "
                    + f"was busy event={occurrence.event.event_id} "
                    + f"action={action.action_id}"
                )
                return
        if failed_action_id is not None:
            occurrence.status = "failed"
            self.store.event_occurrences.write(occurrence)
            increment(
                "studio.vca.event.completed",
                tags=["status:failed", f"trigger_type:{occurrence.trigger.type}"],
            )
            logger.bind(vca_event_status="failed").error(
                "Environment Coordinator event failed "
                + f"event={occurrence.event.event_id} action={failed_action_id}"
            )
            return
        occurrence.status = "completed"
        self.store.event_occurrences.write(occurrence)
        increment(
            "studio.vca.event.completed",
            tags=["status:completed", f"trigger_type:{occurrence.trigger.type}"],
        )
        logger.bind(vca_event_status="completed").info(
            f"Environment Coordinator event completed event={occurrence.event.event_id}"
        )

    async def _run_event_action(
        self, event: EventDefinition, action: EventAction
    ) -> ActionDispatch:
        started_at = utc_now()
        with logger.contextualize(
            vca_action_id=action.action_id, vca_action_type=action.type
        ):
            logger.info(
                "Environment Coordinator action starting "
                + f"event={event.event_id} action={action.action_id} type={action.type}"
            )
            try:
                with record_latency_and_outcome(
                    "studio.vca.action.duration_seconds",
                    "studio.vca.action.completed",
                    [f"action_type:{action.type}"],
                ) as t:
                    if isinstance(action, CallMCPToolAction):
                        status: ActionDispatchStatus = "completed"
                        output = await self._run_direct_tool_action(action)
                    elif isinstance(action, InvokeAgentAction):
                        status, output = await self._run_agent_action(event, action)
                    else:
                        raise ValueError(
                            f"Unknown Coordinator action type: {action.type}"
                        )
                    t.status = status
                logger.bind(vca_action_status=status).info(
                    "Environment Coordinator action completed "
                    + f"event={event.event_id} action={action.action_id} "
                    + f"type={action.type} status={status} "
                    + f"output_keys={sorted(output or ())}"
                )
                return ActionDispatch(
                    action_id=action.action_id,
                    action_type=action.type,
                    status=status,
                    started_at=started_at,
                    completed_at=utc_now(),
                    output=output,
                )
            except Exception as e:
                logger.bind(
                    vca_action_status="failed", error_type=type(e).__name__
                ).error(
                    "Environment Coordinator action failed "
                    + f"event={event.event_id} action={action.action_id}"
                )
                return ActionDispatch(
                    action_id=action.action_id,
                    action_type=action.type,
                    status="failed",
                    started_at=started_at,
                    completed_at=utc_now(),
                    error=repr(e),
                )

    def _run_stderr_tail(
        self, actor_id: str, run_id: str, limit: int = STDERR_TAIL_CHARS
    ) -> str:
        _, stderr_path = self.store.agent_configs.run_stdio_paths(actor_id, run_id)
        try:
            return stderr_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError as e:
            return f"<unreadable: {e!r}>"

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        run_id: str,
        grace_seconds: float,
    ) -> None:
        """SIGTERM the persona's process group, then always follow up with SIGKILL.

        The leader has no SIGTERM handler and dies immediately either way, so
        its own exit says nothing about child shells in the same group that
        ignored the signal. The follow-up SIGKILL is unconditional rather than
        gated on the leader surviving — a no-op on an already-empty group.
        """
        if not await self._signal_group(process, signal.SIGTERM, grace_seconds):
            logger.warning(
                "Environment Coordinator VCA group survived SIGTERM for "
                + f"{grace_seconds}s run_id={run_id} pid={process.pid}"
            )
        await self._signal_group(process, signal.SIGKILL, None)
        await process.wait()

    async def _signal_group(
        self,
        process: asyncio.subprocess.Process,
        sig: signal.Signals,
        grace_seconds: float | None,
    ) -> bool:
        """Signal the group; return whether it exited within ``grace_seconds``.
        ``None`` sends without waiting. A vanished group counts as exited."""
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return True
        if grace_seconds is None:
            return False
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            return False
        return True

    def _apply_timeout(
        self, vca: VirtualCoworkerAgent, record: AgentRunRecord, error: str
    ) -> PersonaOutcome:
        """Classify a run whose budget expired, by whether it left a result.

        Personas are routinely authored to hold their turn open waiting on work
        that may never arrive, so reaching the budget is an ordinary end to a
        run that already acted. Failing it would abort the event behind it.
        """
        record.error = error
        if self._salvaged_run_output(vca.actor_id, record.run_id):
            record.status = "completed"
            return "timed_out_salvaged"
        record.status = "failed"
        return "timed_out"

    def _salvaged_run_output(self, actor_id: str, run_id: str) -> bool:
        """Whether a killed run left output worth counting as a success.

        Read directly rather than through ``read_run_output``: on this path a
        half-written file is expected, and its stricter errors would be noise.
        A dict alone isn't enough — an agent that reported its own failure
        right before the kill landed is still a failure, not a save.
        """
        path = self.store.agent_configs.run_output_path(actor_id, run_id)
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(output, dict):
            return False
        return output.get("status") not in FAILED_AGENT_RUN_STATUSES

    async def _run_direct_tool_action(
        self, action: CallMCPToolAction
    ) -> dict[str, JsonValue]:
        async with FastMCPClient(self._mcp_proxy) as client:
            result = await client.call_tool(
                action.tool_name,
                action.arguments,
                meta={
                    TOOL_CALL_ACTOR_KEY: action.actor_id or COORDINATOR_ACTOR_ID_VALUE,
                    # Mark this as a Coordinator dispatch so shadow-mode per-agent authz
                    # (SECPRJ-1646) doesn't attribute a task-authored tool action to the
                    # persona it runs "as" — the persona's agent didn't pick this tool.
                    TOOL_CALL_ORIGIN_KEY: COORDINATOR_DISPATCH_ORIGIN,
                },
            )
        return summarize_tool_result(result)

    async def _run_agent_action(
        self, event: EventDefinition, action: InvokeAgentAction
    ) -> tuple[ActionDispatchStatus, dict[str, JsonValue]]:
        config = self.store.config.read()
        vca = config.agents.get(action.actor_id)
        if vca is None:
            raise ValueError(f"Unknown virtual coworker: {action.actor_id}")

        run_id = f"run_{uuid4().hex}"
        # Scopes the persona's whole run, including _run_vca_command below.
        with (
            logger.contextualize(
                vca_id=vca.actor_id,
                vca_run_id=run_id,
                agent_id=vca.vca_harness_config.agent_id,
                orchestrator_model=vca.vca_harness_config.orchestrator_model,
            ),
            self.store.agent_configs.lock(vca.actor_id) as lock_acquired,
        ):
            if not lock_acquired:
                increment(
                    "studio.vca.persona.run",
                    tags=[*_persona_base_tags(vca), "status:skipped"],
                )
                logger.bind(vca_run_status="skipped").info(
                    "Environment Coordinator skipped VCA run because actor is locked "
                    + f"event={event.event_id} actor={vca.actor_id} run_id={run_id}"
                )
                return "skipped", {
                    "actor_id": vca.actor_id,
                    "reason": "already_running",
                }
            logger.info(
                "Environment Coordinator starting VCA run "
                + f"event={event.event_id} actor={vca.actor_id} run_id={run_id} "
                + f"timeout_seconds={PersonaDeadlines.for_action(action).total_seconds}"
            )
            record = AgentRunRecord(
                run_id=run_id,
                status="running",
                started_at=utc_now(),
            )
            self.store.agent_configs.write_run(vca.actor_id, record)
            record = await self._run_vca_command(vca, action, record)
            self.store.agent_configs.write_run(vca.actor_id, record)
            logger.bind(vca_run_status=record.status).info(
                "Environment Coordinator finished VCA run "
                + f"event={event.event_id} actor={vca.actor_id} run_id={run_id} "
                + f"status={record.status} error={record.error!r}"
            )
            if record.status == "failed":
                # The run dir rides into the snapshot, but a failed persona is
                # exactly when someone is reading Datadog rather than unpacking
                # a snapshot. Put the cause where they are already looking.
                logger.bind(vca_run_status=record.status).error(
                    "Environment Coordinator VCA run stderr "
                    + f"actor={vca.actor_id} run_id={run_id} "
                    + f"tail={self._run_stderr_tail(vca.actor_id, run_id)!r}"
                )
            if record.status == "failed":
                raise RuntimeError(
                    record.error or f"Virtual coworker {vca.actor_id} failed"
                )
            return "completed", {"run_id": run_id, "actor_id": vca.actor_id}

    async def _run_vca_command(
        self,
        vca: VirtualCoworkerAgent,
        action: InvokeAgentAction,
        record: AgentRunRecord,
    ) -> AgentRunRecord:
        # Drop to the confined user when the VCA requests it (run_as_user); None = inherit (prior behavior).
        run_as_user = vca.run_as_user
        deadlines = PersonaDeadlines.for_action(action)
        command, env = self.store.agent_configs.prepare_agent_run(
            vca=vca,
            run_id=record.run_id,
            mcp_gateway_url=self._mcp_gateway_url,
            filesystem_dir=self.store.agent_filesystems.filesystem_dir(vca.actor_id),
            run_as_user=run_as_user,
            agent_timeout_seconds=deadlines.agent_seconds,
        )
        # uvloop lacks asyncio's user/group privilege-drop kwargs, so drop via an external
        # wrapper: setpriv (--clear-groups) or runuser. Username is its own argv element (no shell).
        if run_as_user is not None:
            if shutil.which("setpriv"):
                command = [
                    "setpriv",
                    "--reuid",
                    run_as_user,
                    "--regid",
                    run_as_user,
                    "--clear-groups",
                    "--",
                    *command,
                ]
            else:
                command = ["runuser", "-u", run_as_user, "--", *command]
        with record_latency_and_outcome(
            "studio.vca.persona.duration_seconds",
            "studio.vca.persona.run",
            _persona_base_tags(vca),
        ) as t:
            stdout_path, stderr_path = self.store.agent_configs.run_stdio_paths(
                vca.actor_id, record.run_id
            )
            with (
                stdout_path.open("wb") as stdout_file,
                stderr_path.open("wb") as stderr_file,
            ):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=get_archipelago_agents_cwd(),
                    env=env,
                    start_new_session=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            logger.info(
                "Environment Coordinator VCA process spawned "
                + f"actor={vca.actor_id} run_id={record.run_id} pid={process.pid}"
            )
            outcome: PersonaOutcome
            started = time.monotonic()
            try:
                await asyncio.wait_for(process.wait(), timeout=deadlines.total_seconds)
            except TimeoutError:
                await self._terminate_process_group(
                    process, record.run_id, deadlines.sigterm_grace_seconds
                )
                outcome = self._apply_timeout(
                    vca, record, f"Timed out after {deadlines.total_seconds}s"
                )
            else:
                elapsed = time.monotonic() - started
                if process.returncode != 0:
                    record.status = "failed"
                    record.error = f"Exited with status {process.returncode}"
                    outcome = "error_exit"
                else:
                    run_output = self.store.agent_configs.read_run_output(
                        vca.actor_id, record.run_id
                    )
                    if run_output is None:
                        record.status = "failed"
                        record.error = "Agent did not write output.json"
                        outcome = "no_output"
                    elif run_output.get("status") in FAILED_AGENT_RUN_STATUSES:
                        # A failure reported at or past the deadline we handed the
                        # agent is our budget expiring, not the agent breaking. The
                        # runner has no separate "timed_out" status, so this is what
                        # its own timeout looks like — trust it outright rather than
                        # re-deriving it through the hard-kill salvage check, which
                        # would otherwise read this same self-reported failure as
                        # nothing worth salvaging.
                        if elapsed >= deadlines.agent_seconds:
                            record.status = "completed"
                            record.error = f"Timed out after {deadlines.agent_seconds}s"
                            outcome = "timed_out_salvaged"
                        else:
                            record.status = "failed"
                            # The agent writes this field, so bound it; the rest of the
                            # string is the coordinator's own.
                            reported = str(run_output.get("status"))[:40]
                            record.error = f"Agent finished with status {reported}"
                            outcome = "agent_failed"
                    else:
                        record.status = "completed"
                        outcome = "completed"

            record.completed_at = utc_now()
            t.status = outcome
        return record


_coordinator: Coordinator | None = None


def get_coordinator() -> Coordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = Coordinator()
    return _coordinator


def set_coordinator_for_tests(coordinator: Coordinator | None) -> None:
    global _coordinator
    _coordinator = coordinator
