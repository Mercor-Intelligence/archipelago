"""A tool result is agent-controlled, so it must not ride out on a log line.

An agent that runs `env` gets the sandbox's environment back as a tool result. The
coordinator logged the first 1000 chars of it, which is how production credentials reached
Datadog. The result still goes to mcp_calls.jsonl, which is where grading reads it.
"""

import json
from pathlib import Path

import pytest
from fastmcp.tools import ToolResult
from loguru import logger
from mcp.types import TextContent

from runner.coordinator import runtime as coordinator_runtime
from runner.coordinator.agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    TARGET_AGENT_ACTOR_ID_VALUE,
)
from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.events.models import (
    CallMCPToolAction,
    EventDefinition,
    ToolCallCountEventTrigger,
)
from runner.coordinator.runtime import Coordinator, set_coordinator_for_tests

from .test_coordinator import make_gateway, write_config

ENV_OUTPUT = (
    "PATH=/usr/bin\n"
    "LITELLM_PROXY_API_KEY=sk-not-a-real-key-0123456789abcdef\n"
    "DATADOG_APP_KEY=0123456789abcdef0123456789abcdef01234567\n"
)


@pytest.fixture(autouse=True)
def _reset_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def validate(_: str) -> None:
        return None

    set_coordinator_for_tests(None)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(coordinator_runtime, "validate_mcp_gateway_url", validate)


@pytest.mark.asyncio
async def test_the_tool_call_log_line_holds_no_result_content(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    emitted: list[str] = []
    sink_id = logger.add(lambda message: emitted.append(str(message)), level="DEBUG")
    try:
        await coordinator.record_tool_call(
            tool_name="run_shell",
            arguments={"command": "env"},
            actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
            result=ToolResult(content=[TextContent(type="text", text=ENV_OUTPUT)]),
        )
    finally:
        logger.remove(sink_id)
    await coordinator.finish_actions()

    logged = "".join(emitted)

    assert "recorded MCP call" in logged
    assert "LITELLM_PROXY_API_KEY" not in logged
    assert "DATADOG_APP_KEY" not in logged
    assert f"result_chars={len(ENV_OUTPUT)}" in logged


@pytest.mark.asyncio
async def test_the_result_is_still_recorded_for_grading(tmp_path: Path) -> None:
    """Dropping it from the log must not drop it from the observation grading reads."""
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="run_shell",
        arguments={"command": "env"},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
        result=ToolResult(content=[TextContent(type="text", text=ENV_OUTPUT)]),
    )
    await coordinator.finish_actions()

    recorded = json.loads(
        (root / "checkpoint_observations/mcp_calls.jsonl").read_text().splitlines()[0]
    )

    assert recorded["tool_name"] == "run_shell"
    assert "LITELLM_PROXY_API_KEY" in str(recorded["result_summary"]["text"])


@pytest.mark.asyncio
async def test_the_action_log_lines_hold_no_output_content(tmp_path: Path) -> None:
    """An action's output is a tool-result summary, so it is agent content too."""
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="on_first_call",
                    trigger=ToolCallCountEventTrigger(count=1),
                    actions=[
                        CallMCPToolAction(
                            action_id="read_env",
                            actor_id=COORDINATOR_ACTOR_ID_VALUE,
                            tool_name="read_env",
                            arguments={},
                        )
                    ],
                )
            ],
        ),
    )
    server = make_gateway()

    @server.tool
    def read_env() -> str:
        return ENV_OUTPUT

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)

    emitted: list[str] = []
    sink_id = logger.add(lambda message: emitted.append(str(message)), level="DEBUG")
    try:
        await coordinator.record_tool_call(
            tool_name="any", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
        )
        await coordinator.finish_actions()
    finally:
        logger.remove(sink_id)

    logged = "".join(emitted)

    assert "action completed" in logged
    assert "action dispatch recorded" in logged
    assert "LITELLM_PROXY_API_KEY" not in logged
    assert "DATADOG_APP_KEY" not in logged
    # The field names still ship, so a reader knows what came back.
    assert "output_keys=['content_items'" in logged
    assert "'text'" in logged


@pytest.mark.asyncio
async def test_a_failed_tool_call_logs_its_type_not_its_message(tmp_path: Path) -> None:
    """An exception message can echo the environment that produced it."""
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    emitted: list[str] = []
    sink_id = logger.add(lambda message: emitted.append(str(message)), level="DEBUG")
    try:
        await coordinator.record_tool_call(
            tool_name="run_shell",
            arguments={"command": "env"},
            actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
            error=f"RuntimeError({ENV_OUTPUT!r})",
            error_type="RuntimeError",
        )
    finally:
        logger.remove(sink_id)
    await coordinator.finish_actions()

    logged = "".join(emitted)

    assert "error_type=RuntimeError" in logged
    assert "LITELLM_PROXY_API_KEY" not in logged
    # The full repr is still recorded where grading reads it.
    recorded = (root / "checkpoint_observations/mcp_calls.jsonl").read_text()
    assert "LITELLM_PROXY_API_KEY" in recorded
