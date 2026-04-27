"""
Models for agent definitions and execution.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import Message
from openai.types.responses.easy_input_message_param import EasyInputMessageParam
from pydantic import BaseModel

from runner.models import TaskFieldSchema

# LiteLLM message types for agent execution:
# - InputMessage (AllMessageValues): TypedDict for Chat Completions API requests
# - ResponsesInputMessage (EasyInputMessageParam): TypedDict for Responses API requests
# - OutputMessage (Message): Pydantic model from API responses, used for new messages
# - AnyMessage: Union of all three, used for trajectory output (includes input + generated)
LitellmInputMessage = AllMessageValues
LitellmResponsesInputMessage = EasyInputMessageParam
LitellmOutputMessage = Message
LitellmAnyMessage = (
    LitellmInputMessage | LitellmResponsesInputMessage | LitellmOutputMessage
)

def get_msg_role(msg: LitellmAnyMessage) -> str:
    """Get role from either TypedDict or Pydantic Message."""
    if isinstance(msg, Message):
        return msg.role
    return msg["role"]

def get_msg_content(msg: LitellmAnyMessage) -> Any:
    """Get content from either TypedDict or Pydantic Message."""
    if isinstance(msg, Message):
        return msg.content
    return msg.get("content")

def get_msg_attr(msg: LitellmAnyMessage, key: str, default: Any = None) -> Any:
    """Get arbitrary attribute from either TypedDict or Pydantic Message."""
    if isinstance(msg, Message):
        return getattr(msg, key, default)
    return msg.get(key, default)

class AgentConfigIds(StrEnum):
    """Registry of available agent implementation IDs (e.g., 'loop_agent')."""

    LOOP_AGENT = "loop_agent"
    REACT_TOOLBELT_AGENT = "react_toolbelt_agent"
    ECHO_AGENT = "echo_agent"

class AgentStatus(StrEnum):
    """Status of an agent run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR = "error"

class AgentRunInput(BaseModel):
    """Input to an agent implementation."""

    trajectory_id: str

    # The "actual" type of this is list[LitellmInputMessage] but given the way
    # pydantic works it makes sense to lazily handle this as just list[Any].
    # See also https://github.com/pydantic/pydantic/issues/9541.
    initial_messages: list[Any]

    mcp_gateway_url: str | None
    mcp_gateway_auth_token: str | None  # None for local/unauthenticated
    orchestrator_model: str
    orchestrator_extra_args: dict[str, Any] | None
    agent_config_values: dict[str, Any]

    # Parent trajectory output (for continuation trajectories used during multi-turn, None otherwise)
    parent_trajectory_output: dict[str, Any] | None = None

    # Arbitrary per-trajectory metadata from the orchestration request
    custom_args: dict[str, Any] | None = None

class AgentTrajectoryOutput(BaseModel):
    """Output from an agent run"""

    messages: list[LitellmAnyMessage]
    output: dict[str, Any] | None = None
    status: AgentStatus
    time_elapsed: float
    usage: dict[str, int] | None = None

AgentImpl = Callable[[AgentRunInput], Awaitable[AgentTrajectoryOutput]]

class AgentDefn(BaseModel):
    """Definition of an agent implementation in the registry."""

    agent_config_id: AgentConfigIds
    agent_impl: AgentImpl | None = None  # Optional - server doesn't need implementation
    agent_config_fields: list[TaskFieldSchema]  # Configurable fields for this agent

    class Config:
        arbitrary_types_allowed = True
