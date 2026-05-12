import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner.agents.loop_agent.main import LoopAgent
from runner.agents.models import AgentRunInput, AgentStatus


class FakeMCPClient:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0
        self.client = SimpleNamespace(session=object())

    async def __aenter__(self) -> SimpleNamespace:
        self.enter_count += 1
        return self.client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exit_count += 1


@pytest.mark.asyncio
async def test_loop_agent_reuses_single_mcp_session() -> None:
    run_input = AgentRunInput(
        trajectory_id="traj_test",
        initial_messages=[{"role": "user", "content": "Solve the task"}],
        mcp_gateway_url="http://example.com/mcp/",
        mcp_gateway_auth_token=None,
        orchestrator_model="openai/gpt-5",
        orchestrator_extra_args=None,
        agent_config_values={"max_steps": 3, "timeout": 5},
    )
    agent = LoopAgent(run_input)
    fake_mcp_client = FakeMCPClient()
    agent.mcp_client = fake_mcp_client

    initialize_clients: list[object] = []
    step_clients: list[object] = []

    async def fake_initialize_tools(client: SimpleNamespace) -> None:
        initialize_clients.append(client)

    async def fake_step(client: SimpleNamespace) -> None:
        step_clients.append(client)
        if len(step_clients) == 2:
            agent._finalized = True

    agent._initialize_tools = fake_initialize_tools
    agent.step = fake_step

    output = await agent.run()

    assert fake_mcp_client.enter_count == 1
    assert fake_mcp_client.exit_count == 1
    assert initialize_clients == [fake_mcp_client.client]
    assert step_clients == [fake_mcp_client.client, fake_mcp_client.client]
    assert output.status == AgentStatus.COMPLETED
