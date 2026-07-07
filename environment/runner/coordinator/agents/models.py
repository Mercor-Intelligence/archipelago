from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# MCP tool calls are made by three kinds of actors:
# - Target Agent (TA)
# - Virtual Coworker Agents (VCAs)
# - Environment Coordinator
#
# To distinguish between the three, we add "actor_id" FastMCP metadata.
#
# The TA is "actor_id: target_agent", VCAs "actor_id: <vca_id>", and
# the Coordinator with "actor_id: coordinator".
#
# NOTE: We copy these constants into the Foundry util package
# mercor-mcp-shared, and Studio. Keep them in sync with:
# - rl-studio/server/packages/vca_event_definitions/service.py
# - mercor-mcp-shared/packages/mcp_actor/mcp_actor/paths.py
TOOL_CALL_ACTOR_KEY = "actor_id"
TARGET_AGENT_ACTOR_ID_VALUE = "target_agent"
COORDINATOR_ACTOR_ID_VALUE = "coordinator"


# -------------------------------------------------------------------------------------
# Archipelago agents definitions (code gen'ed)
# -------------------------------------------------------------------------------------


# @apg_agent_config_model:start
class AgentConfig(BaseModel):
    """Agent configuration"""

    agent_config_id: str  # Which agent implementation (e.g., "loop_agent")
    agent_name: str  # Human-readable name (e.g., "Fast Loop Agent")
    agent_config_values: dict[str, Any]  # Agent-specific configuration values


# @apg_agent_config_model:end


# @apg_agent_run_input:start
class AgentRunInput(BaseModel):
    """Input to an agent implementation."""

    trajectory_id: str

    # The "actual" type of this is list[LitellmInputMessage] but given the way
    # pydantic works it makes sense to lazily handle this as just list[Any].
    # See also https://github.com/pydantic/pydantic/issues/9541.
    initial_messages: list[Any]

    mcp_gateway_url: str | None
    # Gateway calls can either populate `Authorization: Bearer <token>` with...
    # - mcp_gateway_auth_token = A real bearer token for secure AuthN
    # - mcp_gateway_actor_id = An Actor ID for user tenancy
    # - None = No authentication
    mcp_gateway_auth_token: str | None
    mcp_gateway_actor_id: str | None = None
    orchestrator_model: str
    orchestrator_extra_args: dict[str, Any] | None
    agent_config_values: dict[str, Any]

    # Parent trajectory output (for continuation trajectories used during multi-turn, None otherwise)
    parent_trajectory_output: dict[str, Any] | None = None

    # Arbitrary per-trajectory metadata from the orchestration request
    custom_args: dict[str, Any] | None = None

    # The task's custom_fields dict, passed through to all agents.
    # Most agents ignore this. Currently used by harness agents (e.g.
    # configurable_user_sim_agent) for task-level overrides of prompts/models.
    task_custom_fields: dict[str, Any] | None = None

    # Resolved inner agent config (for harness agents that delegate to an agent instance)
    inner_agent_config: dict[str, Any] | None = None

    # REST services to expose as tools, each {"name", "base_url", "openapi_path"}.
    # Empty = gateway-only (today's behavior). Built by the runner from the
    # agent-config's rest_service_names and the live sandbox URL.
    rest_services: list[dict[str, Any]] = []


# @apg_agent_run_input:end


# -------------------------------------------------------------------------------------
# Virtual Coworker Agents
# -------------------------------------------------------------------------------------


# @apg_vca_harness_config:start
class VCAHarnessConfig(BaseModel):
    vca_harness_config_id: str
    vca_id: str
    agent_id: str
    agent_version: int
    orchestrator_id: str
    orchestrator_version: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


# @apg_vca_harness_config:end


class VCAHarnessConfigEnriched(VCAHarnessConfig):
    agent_config: AgentConfig
    orchestrator_model: str
    orchestrator_extra_args: dict[str, Any] | None = None
    task_custom_fields: dict[str, Any] | None = None
    inner_agent_config: AgentConfig | None = None


class VirtualCoworkerAgent(BaseModel):
    actor_id: str
    persona: str
    instructions: str
    env: dict[str, str] = Field(default_factory=dict)
    vca_harness_config: VCAHarnessConfigEnriched


# -------------------------------------------------------------------------------------
# Agent Runs
# -------------------------------------------------------------------------------------


AgentRunStatus = Literal["running", "completed", "failed"]


class AgentRunRecord(BaseModel):
    run_id: str
    status: AgentRunStatus
    started_at: str
    completed_at: str | None = None
    error: str | None = None
