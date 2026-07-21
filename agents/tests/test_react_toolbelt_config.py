from runner.agents.models import AgentRunInput
from runner.agents.react_toolbelt_agent.main import ReActAgent


def test_tool_call_timeout_comes_from_agent_config() -> None:
    run_input = AgentRunInput(
        trajectory_id="trajectory",
        initial_messages=[],
        mcp_gateway_url="http://localhost:8080/mcp",
        mcp_gateway_auth_token=None,
        orchestrator_model="openai/test-model",
        orchestrator_extra_args=None,
        agent_config_values={"tool_call_timeout": 123},
    )

    agent = ReActAgent(run_input)

    assert agent.tool_call_timeout == 123
