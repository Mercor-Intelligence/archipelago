"""
Models for agent definitions and execution.
"""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import Message
from pydantic import BaseModel

from runner.models import TaskFieldSchema

LitellmInputMessage = AllMessageValues
LitellmOutputMessage = Message
LitellmAnyMessage = LitellmInputMessage | LitellmOutputMessage


def get_msg_role(msg: LitellmAnyMessage) -> str:
    if isinstance(msg, Message):
        return msg.role
    return msg["role"]


def get_msg_content(msg: LitellmAnyMessage) -> Any:
    if isinstance(msg, Message):
        return msg.content
    return msg.get("content")


def get_msg_attr(msg: LitellmAnyMessage, key: str, default: Any = None) -> Any:
    if isinstance(msg, Message):
        return getattr(msg, key, default)
    return msg.get(key, default)


class AgentConfigIds(StrEnum):
    REACT_TOOLBELT_AGENT = "react_toolbelt_agent"


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR = "error"


class AgentRunInput(BaseModel):
    trajectory_id: str
    initial_messages: list[Any]
    mcp_gateway_url: str | None
    mcp_gateway_auth_token: str | None
    orchestrator_model: str
    orchestrator_extra_args: dict[str, Any] | None
    agent_config_values: dict[str, Any]
    parent_trajectory_output: dict[str, Any] | None = None


class AgentTrajectoryOutput(BaseModel):
    messages: list[LitellmAnyMessage]
    output: dict[str, Any] | None = None
    status: AgentStatus
    time_elapsed: float


AgentImpl = Callable[[AgentRunInput], Awaitable[AgentTrajectoryOutput]]


class AgentDefn(BaseModel):
    agent_config_id: AgentConfigIds
    agent_impl: AgentImpl | None = None
    agent_config_fields: list[TaskFieldSchema]

    class Config:
        arbitrary_types_allowed = True
