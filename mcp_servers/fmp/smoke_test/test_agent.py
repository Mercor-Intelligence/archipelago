import json
import os
import re
import sys
import time
import tomllib as tomli
from typing import Any

import httpx
import pytest

from conftest import SMOKE_TEST_DIR, MCP_REPO_DIR, configure_mcp_servers

ARCHIPELAGO_AGENTS = MCP_REPO_DIR.parent / "archipelago" / "agents"
if ARCHIPELAGO_AGENTS.exists():
    sys.path.insert(0, str(ARCHIPELAGO_AGENTS))

SMOKE_CONFIG_PATH = SMOKE_TEST_DIR / "smoke_config.json"


def load_smoke_config() -> dict[str, Any]:
    if SMOKE_CONFIG_PATH.exists():
        with open(SMOKE_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "agent": {"name": "Smoke Test Agent", "config_id": "react_toolbelt_agent"},
        "task_prompt": "Test the code_exec tool.",
        "expected_tools": ["code_exec"],
        "required_tools": ["code_exec"],
        "orchestrator": {"model": "gemini/gemini-2.5-flash", "temperature": 0.0},
    }


SMOKE_CONFIG = load_smoke_config()

ARCO_VALIDATE_URL = "https://api.studio.mercor.com/arco/validate"


def _extract_tool_name_from_call(tc: Any) -> str | None:
    """Extract tool name from a tool_call object (dict or Pydantic)."""
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
    """Test that arco.toml and mise.toml are correctly configured."""

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


class TestEnvVarConsistency:
    """Validate that env vars used in server code are declared in arco.env.runtime."""

    # Platform-convention vars with hardcoded defaults that don't need to be in arco.toml
    ALLOWED_MISSING = {
        "APPS_DATA_ROOT",
    }

    # Patterns for platform-injected vars (e.g. APP_FS_ROOT, OPENEMR_DATA_ROOT)
    ALLOWED_MISSING_PATTERNS = [
        re.compile(r"^APP_\w+_ROOT$"),
        re.compile(r"^\w+_DATA_ROOT$"),
    ]

    # Captures var name AND optional string-literal default value.
    # Groups: (environ_var, environ_default, getenv_var, getenv_default)
    ENV_VAR_PATTERN = re.compile(
        r'os\.environ\.get\(\s*["\'](\w+)["\']'
        r'(?:\s*,\s*["\']([^"\']*)["\'])?'
        r'|os\.getenv\(\s*["\'](\w+)["\']'
        r'(?:\s*,\s*["\']([^"\']*)["\'])?'
    )

    def _find_env_vars_in_code(self) -> dict[str, list[str]]:
        """Scan mcp_servers/**/*.py for os.environ.get / os.getenv references.

        Only includes vars that have no default or an empty-string default.
        Vars with a non-empty default value are safe to omit from config.

        Returns a mapping of env var name to list of file paths where it's referenced.
        """
        mcp_servers_dir = MCP_REPO_DIR / "mcp_servers"
        env_vars: dict[str, list[str]] = {}

        for py_file in mcp_servers_dir.rglob("*.py"):
            content = py_file.read_text()
            for match in self.ENV_VAR_PATTERN.finditer(content):
                var_name = match.group(1) or match.group(3)
                default = match.group(2) if match.group(1) else match.group(4)

                # Skip vars with a non-empty string default — they won't break at runtime
                if default is not None and default != "":
                    continue

                # Skip vars with a non-string default (variable, int, etc.)
                # The regex only captures string-literal defaults; check if a
                # comma follows the match indicating a non-literal default arg.
                if default is None:
                    after = content[match.end() :]
                    if after.lstrip().startswith(","):
                        continue

                rel_path = str(py_file.relative_to(MCP_REPO_DIR))
                env_vars.setdefault(var_name, []).append(rel_path)

        return env_vars

    def _load_runtime_env_keys(self) -> set[str]:
        """Load keys available at runtime from arco.toml.

        Includes arco.env.runtime keys and arco.secrets.{runtime,host} keys,
        since all of these are injected into the runtime environment.
        """
        arco_path = MCP_REPO_DIR / "arco.toml"
        with open(arco_path, "rb") as f:
            config = tomli.load(f)

        arco = config.get("arco", {})

        runtime_env = arco.get("env", {}).get("runtime", {})
        keys = {
            k
            for k, v in runtime_env.items()
            if k != "schema" and not k.endswith(".schema") and not isinstance(v, dict)
        }

        # Secrets injected at runtime (runtime secrets + host secrets)
        for section in ["runtime", "host"]:
            secrets = arco.get("secrets", {}).get(section, {})
            keys.update(secrets.keys())

        return keys

    def test_code_env_vars_declared_in_runtime(self) -> None:
        """Every env var referenced in server code must be in arco.env.runtime."""
        code_vars = self._find_env_vars_in_code()
        runtime_keys = self._load_runtime_env_keys()

        missing: dict[str, list[str]] = {}
        for var, files in sorted(code_vars.items()):
            if var in runtime_keys or var in self.ALLOWED_MISSING:
                continue
            if any(p.match(var) for p in self.ALLOWED_MISSING_PATTERNS):
                continue
            missing[var] = files

        if missing:
            lines = []
            for var, files in missing.items():
                locations = ", ".join(files)
                lines.append(f"  {var}  (used in: {locations})")
            detail = "\n".join(lines)
            pytest.fail(f"Env vars used in code but missing from [arco.env.runtime]:\n{detail}")

        print(f"All {len(code_vars)} env vars in code are declared in arco.env.runtime")


_runner_available = False
run_agent = None
AgentConfig = None

try:
    # fmt: off
    # ruff: noqa: I001
    from runner.main import main as _run_agent  # type: ignore[import-not-found]
    from runner.models import AgentConfig as _AgentConfig  # type: ignore[import-not-found]
    # fmt: on
    run_agent = _run_agent
    AgentConfig = _AgentConfig
    _runner_available = True
except ImportError:
    pass

RUNNER_AVAILABLE: bool = _runner_available


@pytest.mark.skipif(not RUNNER_AVAILABLE, reason="runner module not available")
class TestMCPAgent:
    @pytest.fixture
    def agent_config(self) -> Any:
        cfg = SMOKE_CONFIG.get("agent", {})
        return AgentConfig(
            agent_config_id=cfg.get("config_id", "react_toolbelt_agent"),
            agent_name=cfg.get("name", "Smoke Test Agent"),
            agent_config_values={
                "timeout": cfg.get("timeout", 300),
                "max_steps": cfg.get("max_steps", 50),
                "max_toolbelt_size": cfg.get("max_toolbelt_size", 80),
                "tool_call_timeout": cfg.get("tool_call_timeout", 60),
                "llm_response_timeout": cfg.get("llm_response_timeout", 60),
            },
        )

    @pytest.fixture
    def initial_messages(self) -> list[dict[str, str]]:
        return [{"role": "user", "content": SMOKE_CONFIG.get("task_prompt", "")}]

    @pytest.fixture
    def orchestrator_extra_args(self) -> dict[str, Any]:
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
        from runner.agents.models import AgentStatus  # type: ignore[import-not-found]

        print("\n[1] Configuring MCP server...")
        await configure_mcp_servers(base_url, mcp_config, timeout=300)
        print("    OK - MCP server configured")

        print("\n[2] Running agent...")
        orch_config = SMOKE_CONFIG.get("orchestrator", {})
        model = os.environ.get(
            "ORCHESTRATOR_MODEL", orch_config.get("model", "gemini/gemini-2.5-flash")
        )
        print(f"   Model: {model}")

        output = await run_agent(
            trajectory_id=f"smoke_test_{int(time.time())}",
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
        safe_model = model.replace("/", "-") if model else "unknown"
        (results_dir / f"agent_output_{safe_model}.json").write_text(output_json)

        print("\n[3] Agent Results:")
        print(f"    Status: {output.status}")
        print(f"    Time: {output.time_elapsed:.2f}s")

        assert output.status == AgentStatus.COMPLETED
        print("    OK - Agent completed")

        tools_called = get_tools_called(output)
        for tool in SMOKE_CONFIG.get("required_tools", []):
            assert tool.lower() in tools_called, f"Required tool '{tool}' was not called"

        print("\n" + "=" * 60)
        print("SMOKE TEST PASSED")
        print("=" * 60)


class TestMCPAgentFallback:
    @pytest.mark.asyncio
    async def test_tools_callable(self, base_url: str, mcp_config: dict[str, Any]) -> None:
        """Call code_exec tool directly to verify it works."""
        from fastmcp import Client as FastMCPClient

        if RUNNER_AVAILABLE:
            pytest.skip("Agent runner available - use TestMCPAgent instead")

        print("\n[1] Configuring MCP server...")
        await configure_mcp_servers(base_url, mcp_config, timeout=300)
        print("    OK - MCP server configured")

        print("\n[2] Testing code_exec tool...")
        gateway_config = {
            "mcpServers": {"gateway": {"transport": "streamable-http", "url": f"{base_url}/mcp/"}}
        }

        results: dict[str, Any] = {}

        async with FastMCPClient(gateway_config) as client:
            # Test 1: Simple print
            print("\n    [1/4] Testing code_exec - simple print...")
            try:
                result = await client.call_tool(
                    "code_exec", {"code": 'print("Hello from smoke test")'}
                )
                results["simple_print"] = {"success": True}
                result_str = str(result)
                assert "hello" in result_str.lower(), "Output not found"
                print("    OK - simple print")
            except AssertionError:
                raise
            except Exception as e:
                results["simple_print"] = {"success": False, "error": str(e)}
                print(f"    FAIL - simple print: {e}")

            # Test 2: Math operation
            print("\n    [2/4] Testing code_exec - math...")
            try:
                result = await client.call_tool(
                    "code_exec", {"code": "x = 2 + 2\nprint(f'Result: {x}')"}
                )
                results["math"] = {"success": True}
                print("    OK - math")
            except Exception as e:
                results["math"] = {"success": False, "error": str(e)}
                print(f"    FAIL - math: {e}")

            # Test 3: Import module
            print("\n    [3/4] Testing code_exec - import...")
            try:
                result = await client.call_tool(
                    "code_exec", {"code": "import math\nprint(f'Pi = {math.pi}')"}
                )
                results["import"] = {"success": True}
                print("    OK - import")
            except Exception as e:
                results["import"] = {"success": False, "error": str(e)}
                print(f"    FAIL - import: {e}")

            # Test 4: Expression
            print("\n    [4/4] Testing code_exec - expression...")
            try:
                result = await client.call_tool("code_exec", {"code": "[i**2 for i in range(5)]"})
                results["expression"] = {"success": True}
                print("    OK - expression")
            except Exception as e:
                results["expression"] = {"success": False, "error": str(e)}
                print(f"    FAIL - expression: {e}")

        print("\n" + "=" * 60)
        print("Results Summary:")
        passed = sum(1 for r in results.values() if r.get("success"))
        print(f"    {passed}/{len(results)} tests passed")
        print("=" * 60)

        assert results.get("simple_print", {}).get("success"), "simple_print failed"
        assert results.get("math", {}).get("success"), "math failed"
        assert results.get("import", {}).get("success"), "import failed"
        assert results.get("expression", {}).get("success"), "expression failed"
