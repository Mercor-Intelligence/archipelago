"""Verify every registered agent satisfies the final_answer log
contract documented in agents/README.md.

Tight scope: this test verifies the run() callable is correctly
registered for every agent and runs echo_agent end-to-end to
confirm the final_answer log fires. It does NOT mock LiteLLM to
run loop_agent and react_toolbelt_agent end-to-end - that is
follow-up work.
"""

import inspect

import pytest
from loguru import logger

from runner.agents.models import AgentConfigIds, AgentRunInput
from runner.agents.registry import AGENT_REGISTRY


def test_every_agent_id_has_registry_entry():
    for agent_id in AgentConfigIds:
        assert agent_id in AGENT_REGISTRY, (
            f"{agent_id} is in AgentConfigIds enum but has no "
            f"AGENT_REGISTRY entry"
        )


def test_every_registered_agent_has_callable_run():
    for agent_id, defn in AGENT_REGISTRY.items():
        assert callable(defn.agent_impl), (
            f"{agent_id} agent_impl is not callable"
        )
        assert inspect.iscoroutinefunction(defn.agent_impl), (
            f"{agent_id} agent_impl is not async"
        )


@pytest.mark.asyncio
async def test_echo_agent_emits_final_answer_log():
    """End-to-end check on echo_agent: run it, confirm the
    final_answer log fires with the expected message_type binding.

    echo_agent is the only agent in the registry that runs without
    an LLM call, so it is the only one we can exercise here without
    mocking LiteLLM. Adding mocked end-to-end coverage for
    loop_agent and react_toolbelt_agent is a separate PR.
    """
    from runner.agents.echo_agent.main import run as echo_run

    captured = []
    handler_id = logger.add(
        lambda msg: captured.append(msg.record),
        level="INFO",
    )

    try:
        run_input = AgentRunInput(
            trajectory_id="test_traj",
            initial_messages=[{"role": "user", "content": "hello"}],
            mcp_gateway_url=None,
            mcp_gateway_auth_token=None,
            orchestrator_model="none",
            orchestrator_extra_args={},
            agent_config_values={},
        )
        output = await echo_run(run_input)
    finally:
        logger.remove(handler_id)

    assert output.status.value == "completed"
    final_answer_logs = [
        r for r in captured
        if r["extra"].get("message_type") == "final_answer"
    ]
    assert len(final_answer_logs) == 1, (
        f"Expected exactly one final_answer log, got "
        f"{len(final_answer_logs)}"
    )
    assert "hello" in final_answer_logs[0]["message"]
