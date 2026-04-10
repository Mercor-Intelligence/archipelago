"""Smoke tests for Bamboohr MCP server."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

SMOKE_TEST_DIR = Path(__file__).parent
MCP_REPO_DIR = SMOKE_TEST_DIR.parent

ARCHIPELAGO_PATHS = [
    MCP_REPO_DIR.parent / "archipelago" / "agents",
    MCP_REPO_DIR.parent.parent / "archipelago" / "agents",
    Path.home() / "dev" / "mercor" / "archipelago" / "agents",
]

for path in ARCHIPELAGO_PATHS:
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
        break

from conftest import MCP_REPO_DIR, SMOKE_TEST_DIR, configure_mcp_servers  # noqa: E402

SMOKE_CONFIG_PATH = SMOKE_TEST_DIR / "smoke_config.json"


def load_smoke_config() -> dict[str, Any]:
    """Load config from smoke_config.json."""
    if SMOKE_CONFIG_PATH.exists():
        with open(SMOKE_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "agent": {
            "name": "Smoke Test Agent",
            "config_id": "react_toolbelt_agent",
            "timeout": 300,
            "max_steps": 50,
        },
        "task_prompt": "List all available tools and test them.",
        "required_tools": [],
        "orchestrator": {"model": "gemini/gemini-2.5-flash", "temperature": 0.0},
    }


SMOKE_CONFIG = load_smoke_config()
ARCO_VALIDATE_URL = "https://api.studio.mercor.com/arco/validate"


def _extract_tool_name_from_call(tc: Any) -> str | None:
    """Extract tool name from a tool_call object."""
    fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
    name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
    return name.lower() if name else None


def get_tools_called(output: Any) -> set[str]:
    """Extract tool names from agent output messages."""
    tools: set[str] = set()
    for msg in output.messages:
        if isinstance(msg, dict):
            role = msg.get("role")
            tool_name = msg.get("name")
            tool_calls = msg.get("tool_calls") or []
        else:
            role = getattr(msg, "role", None)
            tool_name = getattr(msg, "name", None)
            tool_calls = getattr(msg, "tool_calls", None) or []

        if role == "tool" and tool_name:
            tools.add(tool_name.lower())
        elif role == "assistant":
            for tc in tool_calls:
                if name := _extract_tool_name_from_call(tc):
                    tools.add(name)

    return tools


class TestArcoValidation:
    """Test arco.toml and mise.toml configuration."""

    def test_arco_mise_validation(self) -> None:
        """Validate arco.toml + mise.toml configuration via arco API."""
        arco_path = MCP_REPO_DIR / "arco.toml"
        mise_path = MCP_REPO_DIR / "mise.toml"

        assert arco_path.exists(), f"arco.toml not found at {arco_path}"
        assert mise_path.exists(), f"mise.toml not found at {mise_path}"

        arco_content = arco_path.read_text()
        mise_content = mise_path.read_text()

        response = httpx.post(
            ARCO_VALIDATE_URL,
            json={"arco_toml": arco_content, "mise_toml": mise_content},
            timeout=30,
        )

        assert response.status_code == 200, f"Arco API returned {response.status_code}"

        result = response.json()
        if not result.get("valid"):
            errors = result.get("errors", [])
            pytest.fail(f"Arco validation failed: {errors}")

        print("Arco + mise.toml validation passed")


_runner_available = False
run_agent = None
AgentConfig = None

try:
    from runner.main import main as _run_agent  # type: ignore[import-not-found]
    from runner.models import AgentConfig as _AgentConfig  # type: ignore[import-not-found]

    run_agent = _run_agent
    AgentConfig = _AgentConfig
    _runner_available = True
except ImportError:
    pass

RUNNER_AVAILABLE: bool = _runner_available


@pytest.mark.skipif(not RUNNER_AVAILABLE, reason="runner module not available")
class TestMCPAgent:
    """Full agent test for MCP server."""

    @pytest.fixture
    def agent_config(self) -> Any:
        """Agent config from smoke_config.json."""
        if AgentConfig is None:
            pytest.skip("AgentConfig not available")
        cfg = SMOKE_CONFIG.get("agent", {})
        return AgentConfig(
            agent_config_id=cfg.get("config_id", "react_toolbelt_agent"),
            agent_name=cfg.get("name", "Smoke Test Agent"),
            agent_config_values={
                "timeout": cfg.get("timeout", 300),
                "max_steps": cfg.get("max_steps", 50),
                "max_toolbelt_size": cfg.get("max_toolbelt_size", 80),
                "tool_call_timeout": cfg.get("tool_call_timeout", 30),
                "llm_response_timeout": cfg.get("llm_response_timeout", 60),
            },
        )

    @pytest.fixture
    def initial_messages(self) -> list[dict[str, str]]:
        """Task prompt from config."""
        prompt = SMOKE_CONFIG.get("task_prompt", "List all available tools.")
        return [{"role": "user", "content": prompt}]

    @pytest.fixture
    def orchestrator_extra_args(self) -> dict[str, Any]:
        """LLM args from config."""
        orch = SMOKE_CONFIG.get("orchestrator", {})
        return {"temperature": orch.get("temperature", 0.0)}

    @pytest.mark.asyncio
    async def test_agent_completes_task(
        self,
        base_url: str,
        mcp_config: dict[str, Any],
        agent_config: Any,
        initial_messages: list[dict[str, str]],
        orchestrator_extra_args: dict[str, Any],
    ) -> None:
        """Run agent and verify it completes the task using expected tools."""
        from runner.agents.models import AgentStatus  # type: ignore[import-not-found]

        if run_agent is None:
            pytest.skip("run_agent not available")

        print("\n[1] Configuring MCP server...")
        await configure_mcp_servers(base_url, mcp_config, timeout=300)
        print("    OK - MCP server configured")

        print("\n[2] Running agent...")
        orch_config = SMOKE_CONFIG.get("orchestrator", {})
        default_model = orch_config.get("model", "gemini/gemini-2.5-flash")
        model = os.environ.get("ORCHESTRATOR_MODEL", default_model)
        print(f"   Model: {model}")

        trajectory_id = f"smoke_test_{int(time.time())}"

        output = await run_agent(
            trajectory_id=trajectory_id,
            initial_messages=initial_messages,
            mcp_gateway_url=f"{base_url}/mcp/",
            mcp_gateway_auth_token=None,
            agent_config=agent_config,
            orchestrator_model=model,
            orchestrator_extra_args=orchestrator_extra_args,
        )

        results_dir = SMOKE_TEST_DIR / "results"
        results_dir.mkdir(exist_ok=True)
        output_json: str = output.model_dump_json(indent=2)
        safe_model_name = model.replace("/", "-") if model else "unknown"
        (results_dir / f"agent_output_{safe_model_name}.json").write_text(output_json)

        print("\n[3] Agent Results:")
        print(f"    Status: {output.status}")
        print(f"    Time: {output.time_elapsed:.2f}s")
        print(f"    Messages: {len(output.messages)}")

        assert output.status == AgentStatus.COMPLETED, f"Agent did not complete: {output.status}"
        print("    OK - Agent completed")

        tools_called = get_tools_called(output)
        expected_tools = SMOKE_CONFIG.get("expected_tools", [])
        for tool in expected_tools:
            if tool.lower() in tools_called:
                print(f"    OK - Used tool: {tool}")
            else:
                print(f"    WARN - Tool not used: {tool}")

        required_tools = SMOKE_CONFIG.get("required_tools", [])
        for tool in required_tools:
            assert tool.lower() in tools_called, f"Required tool '{tool}' was not called"

        print("\n" + "=" * 60)
        print("SMOKE TEST PASSED")
        print("=" * 60)


class TestMCPAgentFallback:
    """Fallback test when runner not available."""

    @pytest.mark.asyncio
    async def test_tools_callable(self, base_url: str, mcp_config: dict[str, Any]) -> None:
        """Verify MCP server registers tools correctly."""
        from fastmcp import Client as FastMCPClient

        if RUNNER_AVAILABLE:
            pytest.skip("Agent runner available - use TestMCPAgent instead")

        print("\n[1] Configuring MCP server...")
        await configure_mcp_servers(base_url, mcp_config, timeout=300)
        print("    OK - MCP server configured")

        print("\n[2] Testing MCP tools...")
        gateway_config = {
            "mcpServers": {"gateway": {"transport": "streamable-http", "url": f"{base_url}/mcp/"}}
        }

        print("    Waiting 30s for MCP server to initialize...")
        await asyncio.sleep(30)

        async with FastMCPClient(gateway_config) as client:
            tools = await client.list_tools()
            tool_names = [t.name for t in tools]
            print(f"    Found {len(tool_names)} tools")
            print(f"    Sample tools: {tool_names[:5]}")

        print("\n" + "=" * 60)
        print("Results Summary:")
        print(f"    {len(tool_names)} tools registered")
        print("=" * 60)

        assert len(tool_names) > 0, "No tools registered"
