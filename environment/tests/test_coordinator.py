import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.tools import ToolResult

from runner.coordinator import middleware as coordinator_middleware
from runner.coordinator import runtime as coordinator_runtime
from runner.coordinator import utils as coordinator_utils
from runner.coordinator.agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    COORDINATOR_DISPATCH_ORIGIN,
    TARGET_AGENT_ACTOR_ID_VALUE,
    TOOL_CALL_ORIGIN_KEY,
    AgentConfig,
    VCAHarnessConfigEnriched,
    VirtualCoworkerAgent,
)
from runner.coordinator.checkpoints.models import (
    EventOccurrence,
    PeriodicCheckpoint,
    PhysicalTimeElapsedEventTriggerOccurrence,
)
from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.events.models import (
    AndEventTrigger,
    CallMCPToolAction,
    EventDefinition,
    InvokeAgentAction,
    OrEventTrigger,
    PhysicalTimeElapsedEventTrigger,
    ToolCallArgumentCondition,
    ToolCallCountEventTrigger,
    ToolCallSeenEventTrigger,
    ToolCallSelector,
)
from runner.coordinator.middleware import CoordinatorToolCallMiddleware
from runner.coordinator.runtime import (
    Coordinator,
    set_coordinator_for_tests,
)
from runner.coordinator.state import store as coordinator_store
from runner.coordinator.vca_prompt import (
    build_vca_system_prompt,
    build_vca_user_prompt,
)
from runner.utils import metrics as runner_metrics


def write_config(root: Path, config: CoordinatorConfig) -> None:
    path = root / "config/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json(), encoding="utf-8")


def prompt_sections(prompt: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for block in prompt.split("\n\n"):
        title, separator, body = block.partition("\n")
        if not separator or not title.startswith("## "):
            continue
        sections[title.removeprefix("## ")] = body
    return sections


def make_gateway() -> FastMCP:
    return FastMCP("test", middleware=[CoordinatorToolCallMiddleware()])


def make_agent_runner(
    tmp_path: Path,
    *,
    status: str = "completed",
    write_output: bool = True,
    stdout_text: str = "",
    stderr_text: str = "",
    sleep_seconds: int = 0,
    output_before_sleep: bool = False,
    ignore_sigterm: bool = False,
    honour_agent_timeout: bool = False,
) -> Path:
    runner_dir = tmp_path / "agent_runner"
    package_dir = runner_dir / "runner"
    package_dir.mkdir(parents=True)
    (package_dir / "main.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "import os",
                "import signal",
                "import sys",
                "import time",
                f"if {ignore_sigterm!r}:",
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--trajectory-id', required=True)",
                "parser.add_argument('--initial-messages', required=True)",
                "parser.add_argument('--mcp-gateway-url', required=True)",
                "parser.add_argument('--mcp-gateway-actor-id')",
                "parser.add_argument('--agent-config', required=True)",
                "parser.add_argument('--orchestrator-model', required=True)",
                "parser.add_argument('--output')",
                "args, _ = parser.parse_known_args()",
                f"stdout_text = {stdout_text!r}",
                f"stderr_text = {stderr_text!r}",
                "if stdout_text:",
                "    print(stdout_text)",
                "if stderr_text:",
                "    print(stderr_text, file=sys.stderr)",
                f"sleep_seconds = {sleep_seconds!r}",
                f"if {honour_agent_timeout!r}:",
                "    import os",
                "    sleep_seconds = min(",
                "        sleep_seconds, int(os.environ['AGENT_TIMEOUT_SECONDS'])",
                "    )",
                "messages = json.loads(open(args.initial_messages).read())",
                "open(args.agent_config).read()",
                f"write_output = {write_output!r}",
                f"if {output_before_sleep!r} and args.output:",
                "    with open(args.output, 'w') as f:",
                f"        json.dump({{'status': {status!r}, 'messages': messages}}, f)",
                "if sleep_seconds:",
                "    time.sleep(sleep_seconds)",
                "if args.output and write_output:",
                "    with open(args.output, 'w') as f:",
                "        json.dump({",
                # Mirrors the real runner: argv first, then the environment.
                "            'actor_id': (args.mcp_gateway_actor_id",
                "                or os.environ.get('MCP_GATEWAY_ACTOR_ID') or None),",
                "            'mcp_gateway_url': args.mcp_gateway_url,",
                f"            'status': {status!r},",
                "            'messages': messages,",
                "        }, f)",
            ]
        ),
        encoding="utf-8",
    )
    return runner_dir


def make_virtual_coworker_agent(
    actor_id: str = "admin_agent",
    run_as_user: str | None = None,
    allowed_tool_names: list[str] | None = None,
) -> VirtualCoworkerAgent:
    return VirtualCoworkerAgent(
        actor_id=actor_id,
        persona="You are Admin Agent.",
        instructions="advance environment",
        vca_harness_config=make_vca_harness_config(actor_id),
        run_as_user=run_as_user,
        allowed_tool_names=allowed_tool_names,
    )


def make_vca_harness_config(vca_id: str = "admin_agent") -> VCAHarnessConfigEnriched:
    now = datetime.now(UTC)
    return VCAHarnessConfigEnriched(
        vca_harness_config_id="vca_harness_test",
        vca_id=vca_id,
        agent_id="agent_test",
        agent_version=1,
        orchestrator_id="orch_test",
        orchestrator_version=1,
        created_by="user_test",
        created_at=now,
        updated_at=now,
        archived_at=None,
        agent_config=AgentConfig(
            agent_config_id="loop_agent",
            agent_name="Loop",
            agent_config_values={},
        ),
        orchestrator_model="openai/gpt-4o-mini",
    )


def write_agent_config_files(
    root: Path,
    actor_id: str = "admin_agent",
) -> None:
    agent_dir = root / "agent_configs" / actor_id / "archipelago_agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent_config.json").write_text(
        json.dumps(
            {
                "agent_config_id": "loop_agent",
                "agent_name": "Loop",
                "agent_config_values": {},
            }
        ),
        encoding="utf-8",
    )
    (agent_dir / "orchestrator_model.txt").write_text(
        "openai/gpt-4o-mini",
        encoding="utf-8",
    )


def test_build_vca_system_prompt_composes_policy_role_context_and_instructions() -> (
    None
):
    vca = VirtualCoworkerAgent(
        actor_id="bob_vca",
        persona=" You are Bob. ",
        instructions=" Reply with ORCHID-17. ",
        vca_harness_config=make_vca_harness_config("bob_vca"),
    )

    prompt = build_vca_system_prompt(vca)
    sections = prompt_sections(prompt)

    assert {
        "Role",
        "Instruction Priority",
        "Assigned Role Context",
        "Task-Specific Instructions",
        "Tool-Grounded Catch-Up",
        "Communication Protocol",
        "State And Memory",
        "Delegation Boundary",
        "Response Friction",
        "Bounded Uncertainty",
        "Side Effects",
        "When Task Instructions Are Empty",
    } <= sections.keys()
    assert sections["Assigned Role Context"] == "You are Bob."
    assert sections["Task-Specific Instructions"] == "Reply with ORCHID-17."
    assert "platform policy" in sections["Instruction Priority"]
    assert "app and tool state" in sections["Instruction Priority"]
    assert "entire task" in sections["Delegation Boundary"]
    assert "repeated" in sections["Response Friction"]
    assert "previous VCA trajectories" not in prompt
    assert "email_send_email" not in prompt
    assert "filesystem_read_text_file" not in prompt
    assert "Virtual Coworker Agent" not in prompt
    assert "simulated coworker" not in prompt
    assert "coworker" not in prompt.lower()
    assert "Target Agent" not in prompt
    assert "Environment Coordinator" not in prompt


def test_build_vca_user_prompt_contains_activation_only() -> None:
    prompt = build_vca_user_prompt()

    assert "workplace request" in prompt
    assert not prompt.startswith("## ")
    assert "Assigned Role Context" not in prompt
    assert "Task-Specific Instructions" not in prompt
    assert "Delegation Boundary" not in prompt
    assert "coworker" not in prompt.lower()


def test_build_vca_system_prompt_allows_empty_instructions() -> None:
    vca = VirtualCoworkerAgent(
        actor_id="bob_vca",
        persona="You are Bob.",
        instructions="",
        vca_harness_config=make_vca_harness_config("bob_vca"),
    )

    prompt = build_vca_system_prompt(vca)
    sections = prompt_sections(prompt)

    assert sections["Assigned Role Context"] == "You are Bob."
    assert (
        "No task-specific instructions were provided."
        in sections["Task-Specific Instructions"]
    )


def test_coordinator_config_log_json_filters_agent_env() -> None:
    config = CoordinatorConfig(
        enabled=True,
        agents={
            "bob_vca": VirtualCoworkerAgent(
                actor_id="bob_vca",
                persona="You are Bob.",
                instructions="Reply to the email.",
                env={"SECRET": "do-not-log"},
                vca_harness_config=make_vca_harness_config("bob_vca"),
            )
        },
        events=[
            EventDefinition(
                event_id="email_seen",
                trigger=ToolCallSeenEventTrigger(
                    selector=ToolCallSelector(tool_name="send_email")
                ),
            )
        ],
    )

    payload = json.loads(config.model_dump_log_json())

    assert payload["agents"]["bob_vca"] == {
        "actor_id": "bob_vca",
        "persona": "You are Bob.",
        "instructions": "Reply to the email.",
        "run_as_user": None,
        "allowed_tool_names": None,
    }
    assert payload["events"][0]["event_id"] == "email_seen"
    assert payload["checkpoints"][0]["type"] == "tool_call"


def test_get_archipelago_agents_cwd_from_sibling_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    coordinator_file = tools_dir / "runner/coordinator/state/store.py"
    coordinator_file.parent.mkdir(parents=True)
    coordinator_file.write_text("", encoding="utf-8")
    agents_dir = tools_dir / "agents"
    (agents_dir / "runner").mkdir(parents=True)
    (agents_dir / "pyproject.toml").write_text("", encoding="utf-8")
    (agents_dir / "runner/main.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(coordinator_utils, "__file__", str(coordinator_file))

    assert coordinator_utils.get_archipelago_agents_cwd() == str(agents_dir)


@pytest.fixture(autouse=True)
def reset_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def validate(_: str) -> None:
        return None

    set_coordinator_for_tests(None)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(coordinator_runtime, "validate_mcp_gateway_url", validate)


@pytest.mark.asyncio
async def test_coordinator_disabled_without_config(tmp_path: Path) -> None:
    root = tmp_path / "state"

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    assert not (root / "config/config.json").exists()
    assert coordinator.store.config.read().enabled is False
    assert coordinator._started is False


@pytest.mark.asyncio
async def test_coordinator_disabled_when_config_omits_enabled(tmp_path: Path) -> None:
    root = tmp_path / "state"
    path = root / "config/config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "event_id": "skipped_by_default",
                        "trigger": {"type": "tool_call_seen"},
                        "actions": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.record_tool_call(
        tool_name="any_tool",
        arguments={},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()

    assert coordinator.store.config.read().enabled is False
    assert coordinator._started is False
    assert not (root / "checkpoint_observations/mcp_calls.jsonl").exists()
    assert list((root / "event_occurrences").glob("*.json")) == []


@pytest.mark.asyncio
async def test_coordinator_disabled_when_config_sets_enabled_false(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=False,
            events=[
                EventDefinition(
                    event_id="skipped_when_disabled",
                    trigger=ToolCallSeenEventTrigger(),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.record_tool_call(
        tool_name="any_tool",
        arguments={},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()

    assert coordinator.store.config.read().enabled is False
    assert coordinator._started is False
    assert list((root / "event_occurrences").glob("*.json")) == []


@pytest.mark.asyncio
async def test_coordinator_start_can_retry_after_config_validation_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.json").write_text(
        json.dumps({"enabled": True, "agents": {"admin_agent": {"actor_id": 123}}}),
        encoding="utf-8",
    )

    coordinator = Coordinator(root=root)
    with pytest.raises(RuntimeError, match="Invalid Environment Coordinator config"):
        await coordinator.start(mcp_proxy=make_gateway())

    assert coordinator._started is False

    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
        ),
    )
    await coordinator.start(mcp_proxy=make_gateway())

    assert coordinator._started is True


@pytest.mark.asyncio
async def test_tool_call_count_event_runs_direct_tool_action(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="after_two_marks",
                    trigger=ToolCallCountEventTrigger(count=2),
                    actions=[
                        CallMCPToolAction(
                            action_id="mark_complete",
                            actor_id=COORDINATOR_ACTOR_ID_VALUE,
                            tool_name="mark",
                            arguments={"value": "done"},
                        )
                    ],
                )
            ],
        ),
    )

    tool_calls: list[str] = []
    server = make_gateway()

    @server.tool
    def mark(value: str) -> str:
        tool_calls.append(value)
        return "ok"

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)

    await coordinator.record_tool_call(
        tool_name="read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.record_tool_call(
        tool_name="write", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    occurrence = json.loads(
        (root / "event_occurrences/after_two_marks.json").read_text()
    )
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["type"] == "tool_call_count"
    assert tool_calls == ["done"]
    tool_call_observations = [
        json.loads(line)
        for line in (root / "checkpoint_observations/mcp_calls.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [call["actor_id"] for call in tool_call_observations] == [
        TARGET_AGENT_ACTOR_ID_VALUE,
        TARGET_AGENT_ACTOR_ID_VALUE,
        COORDINATOR_ACTOR_ID_VALUE,
    ]


@pytest.mark.asyncio
async def test_finish_actions_abandons_stuck_task_past_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    monkeypatch.setattr(coordinator_runtime, "FINISH_ACTIONS_HEADROOM_SECONDS", 0.05)
    stuck = asyncio.create_task(asyncio.sleep(3600), name="event_actions:stuck")
    coordinator._action_tasks.add(stuck)
    stuck.add_done_callback(coordinator._action_tasks.discard)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator.finish_actions()

    assert loop.time() - started < 30  # returned on the budget, not after 3600s
    with pytest.raises(asyncio.CancelledError):
        await stuck


@pytest.mark.asyncio
async def test_finish_actions_releases_the_actor_lock_it_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    monkeypatch.setattr(coordinator_runtime, "FINISH_ACTIONS_HEADROOM_SECONDS", 0.05)

    holding = asyncio.Event()

    async def hold_the_lock() -> None:
        with coordinator.store.agent_configs.lock("vca_riley") as acquired:
            assert acquired
            holding.set()
            await asyncio.sleep(3600)

    stuck = asyncio.create_task(hold_the_lock(), name="event_actions:stuck")
    coordinator._action_tasks.add(stuck)
    stuck.add_done_callback(coordinator._action_tasks.discard)
    await holding.wait()
    lock_path = coordinator.store.agent_configs.agent_configs_dir / "vca_riley" / "lock"
    assert lock_path.exists()

    await coordinator.finish_actions()

    # A lock riding into the snapshot reads as already_running to every
    # continuation, so the drain has to see the cancel through.
    assert not lock_path.exists()


@pytest.mark.asyncio
async def test_tool_call_selector_matches_prefixed_observed_tool_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="saw_read",
                    trigger=ToolCallSeenEventTrigger(
                        selector=ToolCallSelector(tool_name="read")
                    ),
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/saw_read.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["tool_call"]["tool_name"] == "insurance_read"


@pytest.mark.asyncio
async def test_tool_call_selector_filters_by_actor_id(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="target_read",
                    trigger=ToolCallSeenEventTrigger(
                        selector=ToolCallSelector(
                            tool_name="read",
                            actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
                        )
                    ),
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id="claims_admin"
    )
    await coordinator.finish_actions()
    assert not (root / "event_occurrences/target_read.json").exists()

    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/target_read.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["tool_call"]["actor_id"] == TARGET_AGENT_ACTOR_ID_VALUE


@pytest.mark.asyncio
async def test_tool_call_selector_filters_by_argument_condition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="email_to_bob",
                    trigger=ToolCallSeenEventTrigger(
                        selector=ToolCallSelector(
                            tool_name="send_email",
                            actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
                            argument_conditions=[
                                ToolCallArgumentCondition(
                                    path=["to"],
                                    operator="contains",
                                    value="bob@acme.example",
                                )
                            ],
                        )
                    ),
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="email_send_email",
        arguments={"to": ["alice@acme.example"], "subject": "Hi", "body": "Hello"},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()
    assert not (root / "event_occurrences/email_to_bob.json").exists()

    await coordinator.record_tool_call(
        tool_name="email_send_email",
        arguments={"to": ["bob@acme.example"], "subject": "Hi", "body": "Hello"},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/email_to_bob.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["tool_call"]["arguments"]["to"] == ["bob@acme.example"]


@pytest.mark.asyncio
async def test_tool_call_count_selector_filters_by_nested_argument_condition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="two_bob_emails",
                    trigger=ToolCallCountEventTrigger(
                        count=2,
                        selector=ToolCallSelector(
                            tool_name="send_email",
                            argument_conditions=[
                                ToolCallArgumentCondition(
                                    path=["recipients", 0, "email"],
                                    operator="equals",
                                    value="bob@acme.example",
                                )
                            ],
                        ),
                    ),
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="send_email",
        arguments={"recipients": [{"email": "bob@acme.example"}]},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.record_tool_call(
        tool_name="send_email",
        arguments={"recipients": [{"email": "alice@acme.example"}]},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()
    assert not (root / "event_occurrences/two_bob_emails.json").exists()

    await coordinator.record_tool_call(
        tool_name="send_email",
        arguments={"recipients": [{"email": "bob@acme.example"}]},
        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
    )
    await coordinator.finish_actions()

    occurrence = json.loads(
        (root / "event_occurrences/two_bob_emails.json").read_text()
    )
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["observed_tool_call_count"] == 2
    assert occurrence["trigger"]["last_call"]["arguments"]["recipients"] == [
        {"email": "bob@acme.example"}
    ]


@pytest.mark.asyncio
async def test_physical_time_event_runs_before_snapshot_drain(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="timer_event",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        CallMCPToolAction(
                            action_id="mark_timer",
                            actor_id=COORDINATOR_ACTOR_ID_VALUE,
                            tool_name="mark",
                        )
                    ],
                )
            ],
        ),
    )

    tool_calls: list[str] = []
    server = make_gateway()

    @server.tool
    def mark() -> str:
        tool_calls.append("timer")
        return "ok"

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/timer_event.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["event"]["actions"][0]["tool_name"] == "mark"
    assert tool_calls == ["timer"]


@pytest.mark.asyncio
async def test_invoke_agent_action_records_run_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    runs_dir = root / "agent_configs/admin_agent/runs"
    run_dirs = list(runs_dir.iterdir())
    assert len(run_dirs) == 1
    run_record = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_record["status"] == "completed"
    assert run_record["completed_at"] is not None
    assert run_record["error"] is None
    assert not (root / "agent_configs/admin_agent/lock").exists()
    output = json.loads((runs_dir / run_record["run_id"] / "output.json").read_text())
    assert output["actor_id"] == "admin_agent"
    assert output["mcp_gateway_url"] == "http://127.0.0.1:8080/mcp/"
    assert output["status"] == "completed"
    assert [message["role"] for message in output["messages"]] == ["system", "user"]
    system_sections = prompt_sections(output["messages"][0]["content"])
    user_sections = prompt_sections(output["messages"][1]["content"])
    assert "Delegation Boundary" in system_sections
    assert system_sections["Assigned Role Context"] == "You are Admin Agent."
    assert system_sections["Task-Specific Instructions"] == "advance environment"
    assert user_sections == {}
    assert "workplace request" in output["messages"][1]["content"]


@pytest.mark.asyncio
async def test_invoke_agent_drops_privileges_when_user_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_as_user wraps the command in a privilege-drop tool (setpriv, or runuser fallback);
    the real drop is bypassed here (test user isn't root) so we assert on the prepended wrapper."""
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )

    captured: dict[str, Any] = {}
    real_spawn = coordinator_runtime.asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)
        captured.update(kwargs)
        # Strip the setpriv/runuser wrapper (up to and including its "--"
        # sentinel) so the underlying runner still executes as the test user.
        command = list(args)
        if command and command[0] in ("setpriv", "runuser"):
            command = command[command.index("--") + 1 :]
        return await real_spawn(*command, **kwargs)

    monkeypatch.setattr(coordinator_runtime.asyncio, "create_subprocess_exec", spy)

    # The 'vca' user doesn't exist on the test host; record the ownership
    # handoff instead of performing a real chown, and stub the home lookup.
    chowned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        coordinator_store,
        "chown_tree",
        lambda path, user: chowned.append((str(path), user)),
    )
    monkeypatch.setattr(coordinator_store, "user_home", lambda user: f"/home/{user}")

    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent(run_as_user="vca")},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    argv = captured.get("argv", [])
    assert argv and argv[0] in ("setpriv", "runuser")
    if argv[0] == "setpriv":
        assert argv[:7] == [
            "setpriv",
            "--reuid",
            "vca",
            "--regid",
            "vca",
            "--clear-groups",
            "--",
        ]
    else:
        assert argv[:4] == ["runuser", "-u", "vca", "--"]
    # Confined user gets a writable HOME instead of the Coordinator's.
    assert captured.get("env", {}).get("HOME") == "/home/vca"
    # The VCA's run dir is handed to the confined user so it can write output.
    run_dir = root / "agent_configs/admin_agent/runs"
    assert any(
        path.startswith(str(run_dir)) and user == "vca" for path, user in chowned
    )


def test_prepare_agent_run_adds_no_sync_for_all_vca_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-sync`` is inserted after ``uv run`` for every VCA spawn.

    The agents project venv is baked during platform image build. Runtime
    ``uv sync`` would re-resolve against CodeArtifact without credentials and
    ignore the coordinator's ``VIRTUAL_ENV``.
    """
    monkeypatch.setattr(coordinator_store, "chown_tree", lambda path, user: None)
    monkeypatch.setattr(coordinator_store, "user_home", lambda user: f"/home/{user}")
    store = coordinator_store.CoordinatorStore(root=tmp_path)

    confined_cmd, _ = store.agent_configs.prepare_agent_run(
        vca=make_virtual_coworker_agent(run_as_user="vca"),
        run_id="run_confined",
        mcp_gateway_url="http://gw",
        filesystem_dir=str(tmp_path / "fs"),
        run_as_user="vca",
    )
    plain_cmd, _ = store.agent_configs.prepare_agent_run(
        vca=make_virtual_coworker_agent(run_as_user=None),
        run_id="run_plain",
        mcp_gateway_url="http://gw",
        filesystem_dir=str(tmp_path / "fs"),
        run_as_user=None,
    )

    assert confined_cmd[:3] == ["uv", "run", "--no-sync"]
    assert plain_cmd[:3] == ["uv", "run", "--no-sync"]


@pytest.mark.asyncio
async def test_invoke_agent_action_uses_runner_port_for_mcp_gateway_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path)
    mcp_gateway_url = "http://127.0.0.1:9142/mcp/"
    validated_urls: list[str] = []

    async def validate(url: str) -> None:
        validated_urls.append(url)

    monkeypatch.setenv("PORT", "9142")
    monkeypatch.setattr(coordinator_runtime, "validate_mcp_gateway_url", validate)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    output = json.loads((run_dirs[0] / "output.json").read_text())
    assert output["mcp_gateway_url"] == mcp_gateway_url
    assert validated_urls == [mcp_gateway_url]


@pytest.mark.asyncio
async def test_invoke_agent_action_fails_when_agent_output_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(
        tmp_path,
        status="error",
        stdout_text="agent stdout context",
        stderr_text="agent stderr context",
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    assert len(run_dirs) == 1
    run_record = json.loads((run_dirs[0] / "run.json").read_text())
    assert occurrence["status"] == "failed"
    assert occurrence["dispatches"][0]["status"] == "failed"
    assert "Agent finished with status error" in occurrence["dispatches"][0]["error"]
    assert run_record["status"] == "failed"
    assert run_record["error"] == "Agent finished with status error"
    assert (run_dirs[0] / "stdout.txt").exists()
    assert (run_dirs[0] / "stderr.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_timeout_seconds", [1, None], ids=["explicit", "default_fallback"]
)
async def test_invoke_agent_action_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_timeout_seconds: int | None,
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, sleep_seconds=60)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    if action_timeout_seconds is None:
        monkeypatch.setattr(
            coordinator_runtime, "DEFAULT_VCA_INVOKE_TIMEOUT_SECONDS", 1
        )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=action_timeout_seconds,
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    run_record = json.loads((run_dirs[0] / "run.json").read_text())
    assert occurrence["status"] == "failed"
    assert occurrence["dispatches"][0]["status"] == "failed"
    assert "Timed out after 1s" in occurrence["dispatches"][0]["error"]
    assert run_record["status"] == "failed"
    assert run_record["error"] == "Timed out after 1s"
    assert not (root / "agent_configs/admin_agent/lock").exists()


def test_finish_actions_budget_covers_longest_persona_chain() -> None:
    config = CoordinatorConfig(
        enabled=True,
        events=[
            EventDefinition(
                event_id="one_persona",
                trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                actions=[
                    InvokeAgentAction(
                        action_id="a", actor_id="admin_agent", timeout_seconds=600
                    )
                ],
            ),
            EventDefinition(
                event_id="chained_personas",
                trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                actions=[
                    InvokeAgentAction(
                        action_id="b", actor_id="admin_agent", timeout_seconds=300
                    ),
                    InvokeAgentAction(
                        action_id="c", actor_id="admin_agent", timeout_seconds=500
                    ),
                ],
            ),
        ],
    )
    # The chained event (800s + 2 grace windows) outweighs the single 600s one.
    grace = 2 * coordinator_runtime.SIGTERM_GRACE_SECONDS
    assert coordinator_runtime._finish_actions_budget_seconds(config) == (
        800 + grace + coordinator_runtime.FINISH_ACTIONS_HEADROOM_SECONDS
    )


@pytest.mark.parametrize("total_seconds", [1, 5, 60, 300, 900])
def test_persona_deadlines_always_leave_the_agent_room_to_stop_itself(
    total_seconds: int,
) -> None:
    deadlines = coordinator_runtime.PersonaDeadlines(total_seconds=total_seconds)

    # The ladder must hold at every budget the model accepts, not just the 300s
    # the generator emits today: agent stops < Coordinator waits < kill lands.
    assert deadlines.agent_seconds <= deadlines.total_seconds
    assert deadlines.total_seconds < deadlines.wall_seconds
    if total_seconds > coordinator_runtime.MIN_INNER_TIMEOUT_HEADROOM_SECONDS:
        assert deadlines.agent_seconds < deadlines.total_seconds


def test_persona_deadlines_headroom_scales_with_the_budget() -> None:
    assert coordinator_runtime.PersonaDeadlines(300).agent_seconds == 270
    # 10% of 900 beats the 30s floor.
    assert coordinator_runtime.PersonaDeadlines(900).agent_seconds == 810


def test_prepare_agent_run_puts_inner_timeout_under_coordinator_deadline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_agent_config_files(root)
    store = coordinator_store.CoordinatorStore(root=root)
    _, env = store.agent_configs.prepare_agent_run(
        vca=make_virtual_coworker_agent(),
        run_id="run_1",
        mcp_gateway_url="http://localhost:1/mcp",
        filesystem_dir=str(tmp_path / "fs"),
        agent_timeout_seconds=270,
    )
    assert env["AGENT_TIMEOUT_SECONDS"] == "270"


@pytest.mark.asyncio
async def test_agent_deadline_fires_before_the_coordinator_kills_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    # Honours AGENT_TIMEOUT_SECONDS the way runner.main does: stop early, write
    # output, exit 0 — so the Coordinator never reaches the kill path.
    runner_dir = make_agent_runner(
        tmp_path, sleep_seconds=30, status="error", honour_agent_timeout=True
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    monkeypatch.setattr(coordinator_runtime, "MIN_INNER_TIMEOUT_HEADROOM_SECONDS", 2)
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=5,
                        )
                    ],
                )
            ],
        ),
    )

    emitted: list[tuple[str, list[str]]] = []

    def record(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
        emitted.append((metric, tags or []))

    monkeypatch.setattr(runner_metrics, "increment", record)
    monkeypatch.setattr(coordinator_runtime, "increment", record)

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    run_record = json.loads((run_dirs[0] / "run.json").read_text())
    # The agent's deadline won — it exited cleanly with its output intact — so
    # the run succeeded, and its own tag keeps it distinguishable from a run
    # that finished with time to spare.
    assert run_record["status"] == "completed"
    assert run_record["error"] == "Timed out after 3s"
    assert (run_dirs[0] / "output.json").exists()
    persona_tags = [
        tags for metric, tags in emitted if metric == "studio.vca.persona.run"
    ]
    assert persona_tags and "status:timed_out_salvaged" in persona_tags[0]
    # The event behind it must not be dragged down by the overrun.
    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["dispatches"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_invoke_agent_timeout_keeps_output_written_before_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(
        tmp_path, sleep_seconds=60, output_before_sleep=True, write_output=False
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                        )
                    ],
                )
            ],
        ),
    )

    emitted: list[tuple[str, list[str]]] = []

    def record(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
        emitted.append((metric, tags or []))

    # `record_latency_and_outcome` resolves `increment` from the metrics module;
    # the Coordinator's own emits use its imported binding. Patch both.
    monkeypatch.setattr(runner_metrics, "increment", record)
    monkeypatch.setattr(coordinator_runtime, "increment", record)

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    # The kill still lands, but the partial result the agent flushed survives it,
    # so the run counts as a success rather than dragging its event down.
    assert json.loads((run_dirs[0] / "run.json").read_text())["status"] == "completed"
    assert (run_dirs[0] / "output.json").exists()
    persona_tags = [
        tags for metric, tags in emitted if metric == "studio.vca.persona.run"
    ]
    assert persona_tags and "status:timed_out_salvaged" in persona_tags[0]
    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["status"] == "completed"


@pytest.mark.asyncio
async def test_invoke_agent_timeout_does_not_salvage_a_self_reported_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(
        tmp_path,
        sleep_seconds=60,
        output_before_sleep=True,
        write_output=False,
        status="failed",
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                        )
                    ],
                )
            ],
        ),
    )

    emitted: list[tuple[str, list[str]]] = []

    def record(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
        emitted.append((metric, tags or []))

    monkeypatch.setattr(runner_metrics, "increment", record)
    monkeypatch.setattr(coordinator_runtime, "increment", record)

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    # The agent recorded its own failure before the kill landed — a hard kill
    # doesn't retroactively make that a success just because output.json exists.
    assert json.loads((run_dirs[0] / "run.json").read_text())["status"] == "failed"
    assert (run_dirs[0] / "output.json").exists()
    persona_tags = [
        tags for metric, tags in emitted if metric == "studio.vca.persona.run"
    ]
    assert persona_tags and "status:timed_out" in persona_tags[0]
    assert "status:timed_out_salvaged" not in persona_tags[0]
    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["status"] == "failed"


@pytest.mark.asyncio
async def test_invoke_agent_timeout_with_nothing_salvageable_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, sleep_seconds=60, write_output=False)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                        )
                    ],
                )
            ],
        ),
    )

    emitted: list[tuple[str, list[str]]] = []

    def record(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
        emitted.append((metric, tags or []))

    monkeypatch.setattr(runner_metrics, "increment", record)
    monkeypatch.setattr(coordinator_runtime, "increment", record)

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    # Nothing survived the kill, so this is the hang the budget exists to catch
    # and it must keep failing its event.
    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    assert json.loads((run_dirs[0] / "run.json").read_text())["status"] == "failed"
    assert not (run_dirs[0] / "output.json").exists()
    persona_tags = [
        tags for metric, tags in emitted if metric == "studio.vca.persona.run"
    ]
    assert persona_tags and "status:timed_out" in persona_tags[0]
    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["status"] == "failed"


@pytest.mark.asyncio
async def test_invoke_agent_escalates_to_sigkill_when_sigterm_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, sleep_seconds=120, ignore_sigterm=True)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    monkeypatch.setattr(coordinator_runtime, "SIGTERM_GRACE_SECONDS", 0.5)
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator.finish_actions()

    # A persona that ignores SIGTERM must not outlive the grace window.
    assert loop.time() - started < 30
    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["dispatches"][0]["status"] == "failed"
    assert not (root / "agent_configs/admin_agent/lock").exists()


class _FakeLeaderProcess:
    """A leader that exits the instant it's signalled, the way the agent does
    (no SIGTERM handler) — independent of whether its process group empties."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_terminate_process_group_always_follows_sigterm_with_sigkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leader with no SIGTERM handler dying immediately must not skip the
    follow-up SIGKILL — a child shell in its group may have ignored SIGTERM
    and be left running behind it."""
    calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))

    monkeypatch.setattr(coordinator_runtime.os, "killpg", fake_killpg)

    coordinator = Coordinator(root=tmp_path / "state")
    await coordinator._terminate_process_group(
        cast(asyncio.subprocess.Process, cast(object, _FakeLeaderProcess(pid=4242))),
        run_id="run_1",
        grace_seconds=0.05,
    )

    assert calls == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_event_runs_remaining_actions_when_configured_to_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, sleep_seconds=60)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="slow_persona",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                            on_failure="continue",
                        ),
                        CallMCPToolAction(
                            action_id="followup_tool",
                            actor_id=COORDINATOR_ACTOR_ID_VALUE,
                            tool_name="mark",
                            arguments={"value": "done"},
                        ),
                    ],
                )
            ],
        ),
    )

    tool_calls: list[str] = []
    server = make_gateway()

    @server.tool
    def mark(value: str) -> str:
        tool_calls.append(value)
        return "ok"

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    dispatches = occurrence["dispatches"]
    assert tool_calls == ["done"]
    # The timed-out persona still fails the event, but no longer suppresses the
    # action queued behind it.
    assert occurrence["status"] == "failed"
    assert [d["action_id"] for d in dispatches] == ["slow_persona", "followup_tool"]
    assert dispatches[0]["status"] == "failed"
    assert dispatches[1]["status"] == "completed"


@pytest.mark.asyncio
async def test_event_aborts_remaining_actions_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, sleep_seconds=60)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="slow_persona",
                            actor_id="admin_agent",
                            timeout_seconds=1,
                        ),
                        CallMCPToolAction(
                            action_id="followup_tool",
                            actor_id=COORDINATOR_ACTOR_ID_VALUE,
                            tool_name="mark",
                            arguments={"value": "done"},
                        ),
                    ],
                )
            ],
        ),
    )

    tool_calls: list[str] = []
    server = make_gateway()

    @server.tool
    def mark(value: str) -> str:
        tool_calls.append(value)
        return "ok"

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    assert occurrence["status"] == "failed"
    assert [d["action_id"] for d in occurrence["dispatches"]] == ["slow_persona"]
    assert tool_calls == []


@pytest.mark.asyncio
async def test_invoke_agent_captures_persona_stdio_to_the_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(
        tmp_path,
        status="error",
        stdout_text="persona-said-this",
        stderr_text="Traceback (most recent call last): persona-broke-here",
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(action_id="admin_run", actor_id="admin_agent")
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    run_dir = next(iter((root / "agent_configs/admin_agent/runs").iterdir()))
    # A crash writes its traceback to stderr, never through the agent's logger,
    # so logs.jsonl can't hold it — this is the only copy.
    assert "persona-said-this" in (run_dir / "stdout.txt").read_text()
    assert "persona-broke-here" in (run_dir / "stderr.txt").read_text()


@pytest.mark.asyncio
async def test_failed_persona_run_logs_its_stderr_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(
        tmp_path, status="error", stderr_text="persona-broke-here"
    )
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(action_id="admin_run", actor_id="admin_agent")
                    ],
                )
            ],
        ),
    )

    logged: list[str] = []
    coordinator = Coordinator(root=root)
    handler_id = coordinator_runtime.logger.add(
        lambda message: logged.append(message.record["message"]), level="ERROR"
    )
    try:
        await coordinator.start(mcp_proxy=make_gateway())
        await coordinator.finish_actions()
    finally:
        coordinator_runtime.logger.remove(handler_id)

    # The cause has to reach Datadog; nobody unpacks a snapshot to find out why
    # a persona died.
    assert any("persona-broke-here" in message for message in logged)


@pytest.mark.asyncio
async def test_invoke_agent_action_fails_when_agent_output_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    runner_dir = make_agent_runner(tmp_path, write_output=False)
    monkeypatch.setattr(
        coordinator_store,
        "AGENT_RUNNER_COMMAND",
        (sys.executable, "-m", "runner.main"),
    )
    monkeypatch.setattr(
        coordinator_runtime, "get_archipelago_agents_cwd", lambda: str(runner_dir)
    )
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[
                EventDefinition(
                    event_id="invoke_admin",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[
                        InvokeAgentAction(
                            action_id="admin_run",
                            actor_id="admin_agent",
                        )
                    ],
                )
            ],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/invoke_admin.json").read_text())
    run_dirs = list((root / "agent_configs/admin_agent/runs").iterdir())
    assert len(run_dirs) == 1
    run_record = json.loads((run_dirs[0] / "run.json").read_text())
    assert occurrence["status"] == "failed"
    assert occurrence["dispatches"][0]["status"] == "failed"
    assert "Agent did not write output.json" in occurrence["dispatches"][0]["error"]
    assert run_record["status"] == "failed"
    assert run_record["error"] == "Agent did not write output.json"


@pytest.mark.asyncio
async def test_invoke_agent_action_skips_when_actor_is_running(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_agent_config_files(root)
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    event = EventDefinition(
        event_id="invoke_admin",
        trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
        actions=[],
    )
    action = InvokeAgentAction(
        action_id="admin_run",
        actor_id="admin_agent",
    )

    with coordinator.store.agent_configs.lock("admin_agent"):
        dispatch = await coordinator._run_event_action(event, action)

    assert dispatch.status == "skipped"
    assert dispatch.output == {
        "actor_id": "admin_agent",
        "reason": "already_running",
    }
    assert list((root / "agent_configs/admin_agent/runs").iterdir()) == []


@pytest.mark.asyncio
async def test_event_requeues_when_its_actor_is_busy(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_agent_config_files(root)
    event = EventDefinition(
        event_id="invoke_admin",
        trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
        actions=[InvokeAgentAction(action_id="admin_run", actor_id="admin_agent")],
    )
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[event],
        ),
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    occurrence = EventOccurrence(
        event=event,
        status="running",
        occurred_at=datetime.now(UTC).isoformat(),
        checkpoint="periodic",
        trigger=PhysicalTimeElapsedEventTriggerOccurrence(
            trajectory_started_at=datetime.now(UTC).isoformat(), elapsed_seconds=1.0
        ),
    )
    assert coordinator.store.event_occurrences.create(occurrence)

    with coordinator.store.agent_configs.lock("admin_agent"):
        await coordinator._run_event_actions(occurrence)

    # A busy actor means the event did not happen. Banking the occurrence would
    # stop the trigger ever matching again, silently dropping the beat.
    assert "invoke_admin" not in coordinator.store.event_occurrences.event_ids()


@pytest.mark.asyncio
async def test_event_is_not_requeued_once_an_earlier_action_has_landed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_agent_config_files(root)
    event = EventDefinition(
        event_id="invoke_admin",
        trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
        actions=[
            CallMCPToolAction(
                action_id="announce",
                actor_id=COORDINATOR_ACTOR_ID_VALUE,
                tool_name="mark",
                arguments={"value": "done"},
            ),
            InvokeAgentAction(action_id="admin_run", actor_id="admin_agent"),
        ],
    )
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            agents={"admin_agent": make_virtual_coworker_agent()},
            events=[event],
        ),
    )

    tool_calls: list[str] = []
    server = make_gateway()

    @server.tool
    def mark(value: str) -> str:
        tool_calls.append(value)
        return "ok"

    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=server)
    occurrence = EventOccurrence(
        event=event,
        status="running",
        occurred_at=datetime.now(UTC).isoformat(),
        checkpoint="periodic",
        trigger=PhysicalTimeElapsedEventTriggerOccurrence(
            trajectory_started_at=datetime.now(UTC).isoformat(), elapsed_seconds=1.0
        ),
    )
    assert coordinator.store.event_occurrences.create(occurrence)

    with coordinator.store.agent_configs.lock("admin_agent"):
        await coordinator._run_event_actions(occurrence)

    # Retrying replays the event from its first action, so the already-sent tool
    # call would fire twice. Keep the occurrence and surface the event as failed
    # instead of duplicating a side effect.
    assert tool_calls == ["done"]
    assert "invoke_admin" in coordinator.store.event_occurrences.event_ids()
    assert (
        coordinator.store.event_occurrences.read("invoke_admin").status  # pyright: ignore[reportOptionalMemberAccess]
        == "failed"
    )


def test_stale_lock_is_reclaimed_when_its_holder_is_gone(tmp_path: Path) -> None:
    coordinator = Coordinator(root=tmp_path / "state")
    lock_path = tmp_path / "state/agent_configs/admin_agent/lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("4242:a-previous-coordinator", encoding="utf-8")

    with coordinator.store.agent_configs.lock("admin_agent") as lock_acquired:
        assert lock_acquired


def test_stale_lock_is_reclaimed_even_when_its_pid_was_reused(
    tmp_path: Path,
) -> None:
    coordinator = Coordinator(root=tmp_path / "state")
    lock_path = tmp_path / "state/agent_configs/admin_agent/lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The state dir travels in the snapshot and a restored container restarts
    # pid numbering, so a leftover pid routinely names a live, unrelated
    # process. Liveness of the pid says nothing about the lock.
    lock_path.write_text(f"{os.getpid()}:a-previous-coordinator", encoding="utf-8")

    with coordinator.store.agent_configs.lock("admin_agent") as lock_acquired:
        assert lock_acquired


def test_lock_held_by_this_coordinator_is_not_evicted(tmp_path: Path) -> None:
    coordinator = Coordinator(root=tmp_path / "state")

    # Concurrent tasks in one Coordinator are exactly what the lock is for.
    with coordinator.store.agent_configs.lock("admin_agent") as outer:
        assert outer
        with coordinator.store.agent_configs.lock("admin_agent") as inner:
            assert not inner


def test_lock_with_unwritten_stamp_is_left_alone(tmp_path: Path) -> None:
    coordinator = Coordinator(root=tmp_path / "state")
    lock_path = tmp_path / "state/agent_configs/admin_agent/lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The stamp is written just after the file is created, so an empty lock is
    # more likely a holder mid-acquire than a dead one.
    lock_path.write_text("", encoding="utf-8")

    with coordinator.store.agent_configs.lock("admin_agent") as lock_acquired:
        assert not lock_acquired


def test_agent_lock_preserves_caller_file_exists_error(tmp_path: Path) -> None:
    coordinator = Coordinator(root=tmp_path / "state")

    with pytest.raises(FileExistsError):
        with coordinator.store.agent_configs.lock("admin_agent") as lock_acquired:
            assert lock_acquired
            raise FileExistsError("caller error")

    assert not (tmp_path / "state/agent_configs/admin_agent/lock").exists()


@pytest.mark.asyncio
async def test_gateway_middleware_logs_completed_tool_calls(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="saw_tool",
                    trigger=ToolCallSeenEventTrigger(),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)

    server = make_gateway()
    await coordinator.start(mcp_proxy=server)

    @server.tool
    def echo(value: str) -> str:
        return value

    async with FastMCPClient(server) as client:
        await client.call_tool("echo", {"value": "hello"})
    await coordinator.finish_actions()

    lines = (root / "checkpoint_observations/mcp_calls.jsonl").read_text().splitlines()
    assert len(lines) == 1
    tool_call_observation = json.loads(lines[0])
    assert tool_call_observation["tool_name"] == "echo"
    assert tool_call_observation["actor_id"] == TARGET_AGENT_ACTOR_ID_VALUE
    occurrence = json.loads((root / "event_occurrences/saw_tool.json").read_text())
    assert occurrence["status"] == "completed"


@pytest.mark.asyncio
async def test_gateway_middleware_propagates_vca_actor_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(agents={"bob": make_virtual_coworker_agent("bob")}),
    )
    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)

    request = SimpleNamespace(
        scope={
            "headers": [
                (b"authorization", b"Bearer bob"),
                (b"x-test", b"kept"),
            ]
        }
    )
    monkeypatch.setattr(coordinator_middleware, "get_http_request", lambda: request)
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="email_send",
            arguments={},
            meta=SimpleNamespace(),
        ),
        fastmcp_context=None,
    )
    propagated_headers: list[list[tuple[bytes, bytes]]] = []

    async def call_next(_: object) -> ToolResult:
        propagated_headers.append(list(request.scope["headers"]))
        return ToolResult(content=[])

    await CoordinatorToolCallMiddleware().on_call_tool(
        cast(Any, context), cast(Any, call_next)
    )

    headers = dict(propagated_headers[0])
    assert headers[b"authorization"] == b"Bearer bob"
    assert headers[b"x-test"] == b"kept"
    assert [name for name, _ in propagated_headers[0]].count(b"authorization") == 1


@pytest.mark.asyncio
async def test_gateway_middleware_attributes_tool_call_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=make_gateway())

    request = SimpleNamespace(
        scope={
            "headers": [
                (b"authorization", b"Bearer alice"),
                (b"x-agent-id", b"agent_1"),
                (b"x-agent-version", b"3"),
                (b"x-orchestrator-id", b"orch_1"),
                (b"x-orchestrator-version", b"5"),
            ]
        }
    )
    monkeypatch.setattr(coordinator_middleware, "get_http_request", lambda: request)
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="email_send", arguments={}, meta=SimpleNamespace()
        ),
        fastmcp_context=None,
    )

    async def call_next(_: object) -> ToolResult:
        return ToolResult(content=[])

    logged: list[str] = []
    handler_id = coordinator_runtime.logger.add(
        lambda message: logged.append(message.record["message"]), level="INFO"
    )
    try:
        await CoordinatorToolCallMiddleware().on_call_tool(
            cast(Any, context), cast(Any, call_next)
        )
    finally:
        coordinator_runtime.logger.remove(handler_id)

    # The gateway's own tool-call audit log carries the calling agent revision.
    recorded = [line for line in logged if "recorded MCP call" in line]
    assert len(recorded) == 1
    assert "agent_id=agent_1" in recorded[0]
    assert "agent_version=3" in recorded[0]
    assert "orchestrator_id=orch_1" in recorded[0]
    assert "orchestrator_version=5" in recorded[0]


@pytest.mark.asyncio
async def test_gateway_middleware_records_without_attribution_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backward compatibility: an older runner that stamps no X-Agent-* headers still
    # records the call, just without the attribution suffix.
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True))
    coordinator = Coordinator(root=root)
    set_coordinator_for_tests(coordinator)
    await coordinator.start(mcp_proxy=make_gateway())

    request = SimpleNamespace(scope={"headers": [(b"authorization", b"Bearer alice")]})
    monkeypatch.setattr(coordinator_middleware, "get_http_request", lambda: request)
    context = SimpleNamespace(
        message=SimpleNamespace(
            name="email_send", arguments={}, meta=SimpleNamespace()
        ),
        fastmcp_context=None,
    )

    async def call_next(_: object) -> ToolResult:
        return ToolResult(content=[])

    logged: list[str] = []
    handler_id = coordinator_runtime.logger.add(
        lambda message: logged.append(message.record["message"]), level="INFO"
    )
    try:
        await CoordinatorToolCallMiddleware().on_call_tool(
            cast(Any, context), cast(Any, call_next)
        )
    finally:
        coordinator_runtime.logger.remove(handler_id)

    recorded = [line for line in logged if "recorded MCP call" in line]
    assert len(recorded) == 1
    assert "agent_id=" not in recorded[0]
    assert "orchestrator_id=" not in recorded[0]


async def _run_persona_tool_call_capturing_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vca: VirtualCoworkerAgent,
    tool_name: str,
    *,
    actor_id: str | None = None,
    raise_in_tool: bool = False,
    origin: str | None = None,
) -> tuple[ToolResult | None, list[dict[str, Any]], list[tuple[str, list[str]]]]:
    """Drive one persona tool call through the middleware and return
    (result, shadow-violation records, shadow metric calls). SECPRJ-1646 shadow authz.

    Asserts the telemetry CONTRACT, not rendered log prose: ``violations`` are the
    ``record["extra"]`` dicts of ``message_type == "tool_authz_shadow_violation"`` logs,
    and ``metrics`` are (name, tags) captured from a monkeypatched ``increment`` — so a
    renamed bound key or a changed metric tag fails a test (protecting PR 2's query).

    Production-shaped ``enabled=True`` config. ``raise_in_tool`` exercises the error
    path, ``origin`` stamps the coordinator-dispatch marker, ``actor_id`` overrides the
    meta actor (e.g. to hit the target-agent early return)."""
    root = tmp_path / "state"
    write_config(root, CoordinatorConfig(enabled=True, agents={vca.actor_id: vca}))
    coordinator = Coordinator(root=root)
    # Mark live: the shadow check gates on _started (parity with record_tool_call).
    # We set it directly rather than start(), which does gateway/readiness checks
    # this middleware-level test doesn't need.
    coordinator._started = True
    set_coordinator_for_tests(coordinator)

    request = SimpleNamespace(
        scope={"headers": [(b"x-agent-id", b"agent_1"), (b"x-agent-version", b"3")]}
    )
    monkeypatch.setattr(coordinator_middleware, "get_http_request", lambda: request)

    # Capture the metric contract (name + bounded tags), not just the log.
    metrics: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        coordinator_middleware,
        "increment",
        lambda name, tags=None, value=1: metrics.append((name, list(tags or []))),
    )

    meta = SimpleNamespace(actor_id=actor_id or vca.actor_id)
    if origin is not None:
        setattr(meta, TOOL_CALL_ORIGIN_KEY, origin)
    context = SimpleNamespace(
        message=SimpleNamespace(name=tool_name, arguments={}, meta=meta),
        fastmcp_context=None,
    )

    async def call_next(_: object) -> ToolResult:
        if raise_in_tool:
            raise RuntimeError("tool unavailable (401)")
        return ToolResult(content=[])

    # Capture the structured record (extra), not the rendered message string.
    extras: list[dict[str, Any]] = []
    handler_id = coordinator_runtime.logger.add(
        lambda message: extras.append(dict(message.record["extra"])), level="WARNING"
    )
    result: ToolResult | None = None
    try:
        result = await CoordinatorToolCallMiddleware().on_call_tool(
            cast(Any, context), cast(Any, call_next)
        )
    except RuntimeError:
        # Error-path case: the middleware records + re-raises; shadow still fires.
        pass
    finally:
        coordinator_runtime.logger.remove(handler_id)
    violations = [
        extra
        for extra in extras
        if extra.get("message_type") == "tool_authz_shadow_violation"
    ]
    return result, violations, metrics


def _assert_shadow_violation(
    violations: list[dict[str, Any]],
    metrics: list[tuple[str, list[str]]],
    *,
    tool_name: str,
    outcome: str,
) -> None:
    """Assert exactly one shadow violation was recorded with the expected structured
    contract (fields + metric tags), including the SECPRJ-1599 agent identity."""
    assert len(violations) == 1
    v = violations[0]
    assert v["message_type"] == "tool_authz_shadow_violation"
    assert v["vca_id"] == "bob"
    assert v["tool_name"] == tool_name
    assert v["outcome"] == outcome
    assert v["allowed_count"] == 1
    # Agent identity comes from the X-Agent-* headers (the SECPRJ-1599 dependency).
    assert v["agent_id"] == "agent_1"
    assert v["agent_version"] == "3"
    assert len(metrics) == 1
    name, tags = metrics[0]
    assert name == "studio.mcp.tool_authz.shadow_violation"
    assert "actor_kind:vca" in tags
    assert f"outcome:{outcome}" in tags
    # tool_name is unbounded/agent-controlled and must NOT be a metric tag.
    assert not any(t.startswith("tool:") for t in tags)


@pytest.mark.asyncio
async def test_shadow_tool_authz_flags_out_of_set_persona_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["email_read"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send"
    )
    _assert_shadow_violation(
        violations, metrics, tool_name="email_send", outcome="admitted"
    )
    # Fail-open: shadow mode never blocks — the call still returns its result.
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_silent_on_in_set_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["email_send"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send"
    )
    assert violations == []
    assert metrics == []
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_inert_without_declared_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default (allowed_tool_names=None): persona is unscoped -> no evaluation, no signal.
    vca = make_virtual_coworker_agent("bob")
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send"
    )
    assert violations == []
    assert metrics == []
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_empty_allowlist_is_unscoped_not_deny_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty list must mean "unscoped" (no signal), not "deny everything" — otherwise
    # an accidentally-empty allowlist would flag every call and bury the baseline.
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=[])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send"
    )
    assert violations == []
    assert metrics == []
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_exact_match_does_not_over_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exact (not suffix) matching: a grant of "read" must NOT admit "secrets_read"
    # from a different mounted server, so this call is flagged.
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["read"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "secrets_read"
    )
    _assert_shadow_violation(
        violations, metrics, tool_name="secrets_read", outcome="admitted"
    )
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_skips_target_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target agent is never persona-scoped; it's excluded before the config read.
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["email_read"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send", actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    assert violations == []
    assert metrics == []
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_shadow_tool_authz_flags_on_error_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A would-deny that also fails (401 / not found / task-blocked) is still captured,
    # tagged outcome=error — the case most worth having in the baseline.
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["email_read"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send", raise_in_tool=True
    )
    _assert_shadow_violation(
        violations, metrics, tool_name="email_send", outcome="error"
    )
    # The middleware re-raised, so no result — but the tool error was never suppressed.
    assert result is None


@pytest.mark.asyncio
async def test_shadow_tool_authz_skips_coordinator_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A coordinator-dispatched CallMCPToolAction acting "as" the persona is not the
    # persona's agent choosing the tool, so it must not log a violation against it.
    vca = make_virtual_coworker_agent("bob", allowed_tool_names=["email_read"])
    result, violations, metrics = await _run_persona_tool_call_capturing_shadow(
        tmp_path, monkeypatch, vca, "email_send", origin=COORDINATOR_DISPATCH_ORIGIN
    )
    assert violations == []
    assert metrics == []
    assert isinstance(result, ToolResult)


@pytest.mark.asyncio
async def test_and_event_trigger_requires_all_child_triggers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="timer_and_tool",
                    trigger=AndEventTrigger(
                        triggers=[
                            PhysicalTimeElapsedEventTrigger(after_seconds=0),
                            ToolCallSeenEventTrigger(),
                        ]
                    ),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()
    assert not (root / "event_occurrences/timer_and_tool.json").exists()

    await coordinator.record_tool_call(
        tool_name="read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    occurrence = json.loads(
        (root / "event_occurrences/timer_and_tool.json").read_text()
    )
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["type"] == "and"
    assert [child["type"] for child in occurrence["trigger"]["triggers"]] == [
        "physical_time_elapsed",
        "tool_call_seen",
    ]


@pytest.mark.asyncio
async def test_or_event_trigger_occurs_for_any_child_trigger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="timer_or_tool",
                    trigger=OrEventTrigger(
                        triggers=[
                            ToolCallSeenEventTrigger(
                                selector=ToolCallSelector(tool_name="missing_tool")
                            ),
                            PhysicalTimeElapsedEventTrigger(after_seconds=0),
                        ]
                    ),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.finish_actions()

    occurrence = json.loads((root / "event_occurrences/timer_or_tool.json").read_text())
    assert occurrence["status"] == "completed"
    assert occurrence["trigger"]["type"] == "or"
    assert [child["type"] for child in occurrence["trigger"]["triggers"]] == [
        "physical_time_elapsed"
    ]


@pytest.mark.asyncio
async def test_tool_call_checkpoint_checks_time_trigger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            events=[
                EventDefinition(
                    event_id="timer_after_tool",
                    trigger=PhysicalTimeElapsedEventTrigger(after_seconds=0),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )

    occurrence = json.loads(
        (root / "event_occurrences/timer_after_tool.json").read_text()
    )
    assert occurrence["checkpoint"] == "tool_call"
    assert occurrence["trigger"]["type"] == "physical_time_elapsed"


@pytest.mark.asyncio
async def test_interval_checkpoint_checks_tool_call_trigger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    write_config(
        root,
        CoordinatorConfig(
            enabled=True,
            checkpoints=[
                PeriodicCheckpoint(interval_seconds=60),
            ],
            events=[
                EventDefinition(
                    event_id="saw_tool_on_cron",
                    trigger=ToolCallSeenEventTrigger(),
                    actions=[],
                )
            ],
        ),
    )
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    assert not (root / "event_occurrences/saw_tool_on_cron.json").exists()

    await coordinator.finish_actions()

    occurrence = json.loads(
        (root / "event_occurrences/saw_tool_on_cron.json").read_text()
    )
    assert occurrence["checkpoint"] == "periodic"


def test_chown_tree_strips_group_and_other_permissions(monkeypatch, tmp_path):
    """The confinement handover must leave the tree owner-only, so a sibling
    confined user (same group) cannot read another VCA's artifacts."""
    run_dir = tmp_path / "run"
    sub_dir = run_dir / "nested"
    sub_dir.mkdir(parents=True)
    file_path = sub_dir / "output.json"
    file_path.write_text("{}")
    run_dir.chmod(0o755)
    sub_dir.chmod(0o755)
    file_path.chmod(0o644)
    link_path = run_dir / "escape"
    link_path.symlink_to(tmp_path / "outside")

    # Non-root test host: record ownership changes instead of performing them.
    lchowned: list[str] = []
    monkeypatch.setattr(
        coordinator_utils.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/vca"),
    )
    monkeypatch.setattr(
        coordinator_utils.os, "lchown", lambda p, uid, gid: lchowned.append(str(p))
    )

    coordinator_utils.chown_tree(run_dir, "vca")

    assert (run_dir.stat().st_mode & 0o777) == 0o700
    assert (sub_dir.stat().st_mode & 0o777) == 0o700
    assert (file_path.stat().st_mode & 0o777) == 0o600
    # Symlink itself is chowned but never chmod-followed.
    assert str(link_path) in lchowned
    assert set(lchowned) == {str(run_dir), str(sub_dir), str(file_path), str(link_path)}


def _seed_occurrence(
    root: Path, event_id: str, occurred_at: str, run_id: str | None = None
) -> Path:
    """Write an occurrence file the way a populated world's snapshot carries one."""
    path = root / f"event_occurrences/{event_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "event": {
                    "event_id": event_id,
                    "trigger": {"type": "tool_call_seen", "selector": {}},
                    "actions": [],
                },
                "status": "completed",
                "occurred_at": occurred_at,
                "checkpoint": "tool_call",
                "trigger": {
                    "type": "tool_call_seen",
                    "tool_call": {
                        "sequence": 1,
                        "actor_id": TARGET_AGENT_ACTOR_ID_VALUE,
                        "tool_name": "read",
                        "arguments": {},
                        "timestamp": occurred_at,
                    },
                },
                "dispatches": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _read_occurrence(root: Path, event_id: str) -> dict[str, Any]:
    return json.loads((root / f"event_occurrences/{event_id}.json").read_text())


def _watch_read_config(root: Path, run_id: str | None) -> CoordinatorConfig:
    return CoordinatorConfig(
        enabled=True,
        run_id=run_id,
        events=[
            EventDefinition(
                event_id="target_read",
                trigger=ToolCallSeenEventTrigger(
                    selector=ToolCallSelector(
                        tool_name="read",
                        actor_id=TARGET_AGENT_ACTOR_ID_VALUE,
                    )
                ),
            )
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded_run_id", [None, "run_that_built_the_seed"])
async def test_occurrence_from_another_run_does_not_suppress_the_event(
    tmp_path: Path, seeded_run_id: str | None
) -> None:
    """A seed's occurrences must not read as already fired, or the VCA never wakes."""
    root = tmp_path / "state"
    write_config(root, _watch_read_config(root, "run_this_one"))
    _seed_occurrence(
        root, "target_read", "2020-01-01T00:00:00+00:00", run_id=seeded_run_id
    )

    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())
    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    occurrence = _read_occurrence(root, "target_read")
    assert occurrence["occurred_at"] != "2020-01-01T00:00:00+00:00"
    assert occurrence["run_id"] == "run_this_one"


@pytest.mark.asyncio
async def test_restart_mid_run_keeps_already_fired_events_suppressed(
    tmp_path: Path,
) -> None:
    """A restart is the same run, so re-firing would re-invoke landed side effects."""
    root = tmp_path / "state"
    write_config(root, _watch_read_config(root, "run_this_one"))

    first = Coordinator(root=root)
    await first.start(mcp_proxy=make_gateway())
    await first.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await first.finish_actions()
    before = _read_occurrence(root, "target_read")

    # Process dies, /.apps_data survives, a new Coordinator comes up on it.
    restarted = Coordinator(root=root)
    await restarted.start(mcp_proxy=make_gateway())
    assert restarted.store.event_occurrences.event_ids() == {"target_read"}

    await restarted.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await restarted.finish_actions()

    assert _read_occurrence(root, "target_read") == before


@pytest.mark.asyncio
async def test_occurrence_from_this_run_still_suppresses_the_event(
    tmp_path: Path,
) -> None:
    """The dedupe still holds: a second matching call must not wake the VCA twice."""
    root = tmp_path / "state"
    write_config(root, _watch_read_config(root, "run_this_one"))
    coordinator = Coordinator(root=root)
    await coordinator.start(mcp_proxy=make_gateway())

    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()
    first = _read_occurrence(root, "target_read")

    await coordinator.record_tool_call(
        tool_name="insurance_read", arguments={}, actor_id=TARGET_AGENT_ACTOR_ID_VALUE
    )
    await coordinator.finish_actions()

    assert _read_occurrence(root, "target_read") == first


def test_replacing_a_foreign_occurrence_yields_one_winner(tmp_path: Path) -> None:
    """Only the caller that replaces the seed's file may report a fresh fire."""
    store = coordinator_store.CoordinatorEventOccurrenceStore(
        tmp_path / "event_occurrences", run_id="run_this_one"
    )
    _seed_occurrence(
        tmp_path, "target_read", "2020-01-01T00:00:00+00:00", run_id="run_seed"
    )
    occurrence = EventOccurrence.model_validate(
        json.loads((tmp_path / "event_occurrences/target_read.json").read_text())
    )

    assert store.create(occurrence) is True
    assert store.create(occurrence) is False
    assert store.event_ids() == {"target_read"}
