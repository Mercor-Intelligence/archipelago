import json
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
    TARGET_AGENT_ACTOR_ID_VALUE,
    AgentConfig,
    VCAHarnessConfigEnriched,
    VirtualCoworkerAgent,
)
from runner.coordinator.checkpoints.models import PeriodicCheckpoint
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
) -> Path:
    runner_dir = tmp_path / "agent_runner"
    package_dir = runner_dir / "runner"
    package_dir.mkdir(parents=True)
    (package_dir / "main.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "import sys",
                "import time",
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
                "if sleep_seconds:",
                "    time.sleep(sleep_seconds)",
                "messages = json.loads(open(args.initial_messages).read())",
                "open(args.agent_config).read()",
                f"write_output = {write_output!r}",
                "if args.output and write_output:",
                "    with open(args.output, 'w') as f:",
                "        json.dump({",
                "            'actor_id': args.mcp_gateway_actor_id,",
                "            'mcp_gateway_url': args.mcp_gateway_url,",
                f"            'status': {status!r},",
                "            'messages': messages,",
                "        }, f)",
            ]
        ),
        encoding="utf-8",
    )
    return runner_dir


def make_virtual_coworker_agent(actor_id: str = "admin_agent") -> VirtualCoworkerAgent:
    return VirtualCoworkerAgent(
        actor_id=actor_id,
        persona="You are Admin Agent.",
        instructions="advance environment",
        vca_harness_config=make_vca_harness_config(actor_id),
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
    assert not (run_dirs[0] / "stdout.txt").exists()
    assert not (run_dirs[0] / "stderr.txt").exists()


@pytest.mark.asyncio
async def test_invoke_agent_action_times_out_without_capturing_stdio(
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
