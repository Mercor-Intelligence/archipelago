import asyncio
import os
import signal
from pathlib import Path
from uuid import uuid4

from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from loguru import logger
from pydantic import JsonValue

from .agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    TOOL_CALL_ACTOR_KEY,
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
        self._started = False

    # ---------------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------------

    async def start(self, *, mcp_proxy: FastMCP) -> None:
        self._mcp_proxy = mcp_proxy
        if self._started:
            return
        config = self.store.config.read()
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

    async def finish_actions(self) -> None:
        """
        Let the coordinator finish executing actions before Archipelago snapshots.
        """
        if not self._started:
            return
        config = self.store.config.read()
        if not config.enabled:
            return
        for checkpoint in config.checkpoints:
            if checkpoint.type == "periodic":
                await self._check_for_event_occurrences(checkpoint, config.events)
                break
        while self._action_tasks:
            await asyncio.gather(*self._action_tasks, return_exceptions=True)

    # ---------------------------------------------------------------------------------
    # Checkpoints
    # ---------------------------------------------------------------------------------

    async def record_tool_call(
        self,
        *,
        tool_name: str,
        arguments: dict[str, JsonValue],
        actor_id: str,
        result: ToolResult | None = None,
        error: str | None = None,
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
        result_text = (observation.result_summary or {}).get("text")
        if result_text is not None:
            result_text = str(result_text)[:1_000]
        logger.info(
            "Environment Coordinator recorded MCP call "
            + f"sequence={observation.sequence} actor={actor_id} tool={tool_name} "
            + f"error={error!r} result_text={result_text!r}"
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
            if not self.store.event_occurrences.create(occurrence):
                logger.info(
                    "Environment Coordinator skipped duplicate event occurrence "
                    + f"event={occurrence.event.event_id} checkpoint={checkpoint.type}"
                )
                continue
            logger.info(
                "Environment Coordinator created event occurrence "
                + f"event={occurrence.event.event_id} checkpoint={checkpoint.type} "
                + f"trigger={occurrence.trigger.type}"
            )
            # Each event action is run sequentially, run together in same background task
            task = asyncio.create_task(self._run_event_actions(occurrence))
            self._action_tasks.add(task)
            task.add_done_callback(self._action_tasks.discard)

    # ---------------------------------------------------------------------------------
    # Event Actions
    # ---------------------------------------------------------------------------------

    async def _run_event_actions(self, occurrence: EventOccurrence) -> None:
        for action in occurrence.event.actions:
            dispatch = await self._run_event_action(occurrence.event, action)
            occurrence.dispatches.append(dispatch)
            self.store.event_occurrences.write(occurrence)
            logger.info(
                "Environment Coordinator action dispatch recorded "
                + f"event={occurrence.event.event_id} action={action.action_id} "
                + f"type={action.type} status={dispatch.status} "
                + f"error={dispatch.error!r} output={dispatch.output}"
            )
            if dispatch.status == "failed":
                occurrence.status = "failed"
                self.store.event_occurrences.write(occurrence)
                logger.error(
                    "Environment Coordinator event failed "
                    + f"event={occurrence.event.event_id} action={action.action_id}"
                )
                return
        occurrence.status = "completed"
        self.store.event_occurrences.write(occurrence)
        logger.info(
            f"Environment Coordinator event completed event={occurrence.event.event_id}"
        )

    async def _run_event_action(
        self, event: EventDefinition, action: EventAction
    ) -> ActionDispatch:
        started_at = utc_now()
        logger.info(
            "Environment Coordinator action starting "
            + f"event={event.event_id} action={action.action_id} type={action.type}"
        )
        try:
            if isinstance(action, CallMCPToolAction):
                status: ActionDispatchStatus = "completed"
                output = await self._run_direct_tool_action(action)
            elif isinstance(action, InvokeAgentAction):
                status, output = await self._run_agent_action(event, action)
            else:
                raise ValueError(f"Unknown Coordinator action type: {action.type}")
            logger.info(
                "Environment Coordinator action completed "
                + f"event={event.event_id} action={action.action_id} "
                + f"type={action.type} status={status} output={output}"
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
            logger.error(
                f"Environment Coordinator action failed event={event.event_id} action={action.action_id}: {repr(e)}"
            )
            return ActionDispatch(
                action_id=action.action_id,
                action_type=action.type,
                status="failed",
                started_at=started_at,
                completed_at=utc_now(),
                error=repr(e),
            )

    async def _run_direct_tool_action(
        self, action: CallMCPToolAction
    ) -> dict[str, JsonValue]:
        async with FastMCPClient(self._mcp_proxy) as client:
            result = await client.call_tool(
                action.tool_name,
                action.arguments,
                meta={
                    TOOL_CALL_ACTOR_KEY: action.actor_id or COORDINATOR_ACTOR_ID_VALUE
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
        with self.store.agent_configs.lock(vca.actor_id) as lock_acquired:
            if not lock_acquired:
                logger.info(
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
                + f"timeout_seconds={action.timeout_seconds}"
            )
            record = AgentRunRecord(
                run_id=run_id,
                status="running",
                started_at=utc_now(),
            )
            self.store.agent_configs.write_run(vca.actor_id, record)
            record = await self._run_vca_command(vca, action, record)
            self.store.agent_configs.write_run(vca.actor_id, record)
            logger.info(
                "Environment Coordinator finished VCA run "
                + f"event={event.event_id} actor={vca.actor_id} run_id={run_id} "
                + f"status={record.status} error={record.error!r}"
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
        command, env = self.store.agent_configs.prepare_agent_run(
            vca=vca,
            run_id=record.run_id,
            mcp_gateway_url=self._mcp_gateway_url,
            filesystem_dir=self.store.agent_filesystems.filesystem_dir(vca.actor_id),
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=get_archipelago_agents_cwd(),
            env=env,
            start_new_session=True,
        )
        logger.info(
            "Environment Coordinator VCA process spawned "
            + f"actor={vca.actor_id} run_id={record.run_id} pid={process.pid}"
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=action.timeout_seconds)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            record.status = "failed"
            record.error = f"Timed out after {action.timeout_seconds}s"
        else:
            record.status = "completed" if process.returncode == 0 else "failed"
            if process.returncode != 0:
                record.error = f"Exited with status {process.returncode}"
            else:
                run_output = self.store.agent_configs.read_run_output(
                    vca.actor_id, record.run_id
                )
                if run_output is None:
                    record.status = "failed"
                    record.error = "Agent did not write output.json"
                else:
                    output_status = run_output.get("status")
                    if output_status in {"failed", "error", "cancelled"}:
                        record.status = "failed"
                        record.error = f"Agent finished with status {output_status}"

        record.completed_at = utc_now()
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
