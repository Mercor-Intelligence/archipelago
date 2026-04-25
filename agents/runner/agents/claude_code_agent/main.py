"""Claude Code agent implementation using Claude Agent SDK.

This agent intentionally routes tool execution away from the agent runner and to
Archipelago's environment MCP gateway.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpHttpServerConfig,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from loguru import logger

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmAnyMessage,
    LitellmInputMessage,
)
from runner.utils.error import is_system_error
from runner.utils.usage import UsageTracker

_BUILTIN_TOOLS_DISALLOWED_BASE = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Glob",
    "Grep",
    "LS",
    "Task",
]


def _normalize_model_name(model: str) -> str:
    """Normalize model IDs from Archipelago style to Claude SDK style."""
    if model.startswith("anthropic/"):
        return model.split("/", 1)[1]
    if model.startswith("anthropic:"):
        return model.split(":", 1)[1]
    return model


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(str(value))


def _normalize_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    chunks.append(text_value)
                    continue
                if item.get("type") == "text":
                    maybe_text = item.get("text")
                    if isinstance(maybe_text, str):
                        chunks.append(maybe_text)
                    continue
                chunks.append(_safe_json_dumps(item))
                continue
            chunks.append(str(item))
        return "\n".join(part for part in chunks if part).strip()
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        return _safe_json_dumps(content)
    return str(content)


class ClaudeCodeAgent:
    """Archipelago wrapper around Claude Agent SDK query loop."""

    def __init__(self, run_input: AgentRunInput):
        if run_input.mcp_gateway_url is None:
            raise ValueError("MCP gateway URL is required for claude_code_agent")

        self.trajectory_id = run_input.trajectory_id
        self.messages: list[LitellmAnyMessage] = list(run_input.initial_messages)
        self.start_time: float | None = None
        self.status = AgentStatus.PENDING

        config = run_input.agent_config_values
        self.timeout: int = int(config.get("timeout", 10800))
        self.max_steps: int = int(config.get("max_steps", 250))

        # Optional Day-1 toggle. By default, action space is MCP-only.
        self.enable_native_web_tools: bool = bool(
            config.get("enable_native_web_tools", True)
        )

        self.model = _normalize_model_name(run_input.orchestrator_model)
        self.mcp_gateway_url = run_input.mcp_gateway_url
        self.mcp_gateway_auth_token = run_input.mcp_gateway_auth_token

        self._usage_tracker = UsageTracker()
        self._tool_name_by_use_id: dict[str, str] = {}
        self._result_message: ResultMessage | None = None

    @staticmethod
    def _litellm_msg_role(msg: LitellmAnyMessage) -> str | None:
        if isinstance(msg, dict):
            role = msg.get("role")
            return role if isinstance(role, str) else None
        role = getattr(msg, "role", None)
        return role if isinstance(role, str) else None

    @staticmethod
    def _litellm_msg_content(msg: LitellmAnyMessage) -> Any:
        if isinstance(msg, dict):
            return msg.get("content")
        return getattr(msg, "content", None)

    def _build_prompt(self) -> str:
        transcript_rows: list[tuple[str, str]] = []
        for msg in self.messages:
            role = self._litellm_msg_role(msg)
            if role is None:
                continue
            text = _normalize_text_content(self._litellm_msg_content(msg))
            if not text:
                continue
            transcript_rows.append((role, text))

        if not transcript_rows:
            return "Complete the task."

        if len(transcript_rows) == 1 and transcript_rows[0][0] == "user":
            return transcript_rows[0][1]

        transcript = [
            "Continue the task using this prior conversation context:",
            "",
        ]
        for role, text in transcript_rows:
            transcript.append(f"{role.upper()}: {text}")
            transcript.append("")
        return "\n".join(transcript).strip()

    async def _prompt_stream(self) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": self._build_prompt(),
            },
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    def _build_mcp_servers(self) -> dict[str, McpHttpServerConfig]:
        server: McpHttpServerConfig = {
            "type": "http",
            "url": self.mcp_gateway_url,
        }
        if self.mcp_gateway_auth_token:
            server["headers"] = {
                "Authorization": f"Bearer {self.mcp_gateway_auth_token}"
            }
        return {"gateway": server}

    def _build_options(self) -> ClaudeAgentOptions:
        disallowed_tools = list(_BUILTIN_TOOLS_DISALLOWED_BASE)
        tools: list[str] = []
        if self.enable_native_web_tools:
            tools = ["WebSearch", "WebFetch"]
        else:
            disallowed_tools.extend(["WebSearch", "WebFetch"])

        options = ClaudeAgentOptions(
            model=self.model,
            max_turns=self.max_steps,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
            },
            permission_mode="bypassPermissions",
            tools=tools,
            disallowed_tools=disallowed_tools,
            mcp_servers=self._build_mcp_servers(),
        )
        return options

    def _append_tool_result_message(
        self,
        tool_use_id: str,
        content: str | list[dict[str, Any]] | dict[str, Any] | None,
        is_error: bool,
    ) -> None:
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        else:
            text = _safe_json_dumps(content)

        if is_error and not text.startswith("Error:"):
            text = f"Error: {text}" if text else "Error"

        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_use_id,
                "name": self._tool_name_by_use_id.get(tool_use_id, "tool"),
                "content": text,
            }
        )

    def _translate_user_message(self, message: UserMessage) -> None:
        if isinstance(message.content, str):
            self.messages.append({"role": "user", "content": message.content})
            return

        text_parts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text:
                    text_parts.append(block.text)
                continue

            if isinstance(block, ToolResultBlock):
                self._append_tool_result_message(
                    tool_use_id=block.tool_use_id,
                    content=block.content,
                    is_error=bool(block.is_error),
                )
                continue

            if isinstance(block, ToolUseBlock):
                self._tool_name_by_use_id[block.id] = block.name
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": _safe_json_dumps(block.input),
                                },
                            }
                        ],
                    }
                )
                continue

        if text_parts:
            self.messages.append({"role": "user", "content": "\n".join(text_parts)})

    def _translate_assistant_message(self, message: AssistantMessage) -> None:
        if message.usage:
            self._usage_tracker.track_from_dict({"usage": message.usage})

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        pending_tool_results: list[tuple[str, Any, bool]] = []

        for block in message.content:
            if isinstance(block, TextBlock):
                if block.text:
                    text_parts.append(block.text)
                continue

            if isinstance(block, ThinkingBlock):
                if block.thinking:
                    logger.bind(message_type="thinking").debug(block.thinking)
                continue

            if isinstance(block, ToolUseBlock):
                self._tool_name_by_use_id[block.id] = block.name
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": _safe_json_dumps(block.input),
                        },
                    }
                )
                continue

            if isinstance(block, ServerToolUseBlock):
                self._tool_name_by_use_id[block.id] = block.name
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": _safe_json_dumps(block.input),
                        },
                    }
                )
                continue

            if isinstance(block, ToolResultBlock):
                pending_tool_results.append(
                    (block.tool_use_id, block.content, bool(block.is_error))
                )
                continue

            if isinstance(block, ServerToolResultBlock):
                pending_tool_results.append((block.tool_use_id, block.content, False))
                continue

        assistant_message: LitellmInputMessage = {
            "role": "assistant",
            "content": "\n".join(text_parts).strip(),
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls  # type: ignore[index]

        if assistant_message["content"] or tool_calls:
            self.messages.append(assistant_message)
        for tool_use_id, content, is_error in pending_tool_results:
            self._append_tool_result_message(
                tool_use_id=tool_use_id,
                content=content,
                is_error=is_error,
            )

    def _handle_sdk_message(self, message: Any) -> None:
        if isinstance(message, UserMessage):
            self._translate_user_message(message)
            return

        if isinstance(message, AssistantMessage):
            self._translate_assistant_message(message)
            return

        if isinstance(message, ResultMessage):
            self._result_message = message
            if message.usage:
                self._usage_tracker.track_from_dict({"usage": message.usage})
            return

        if isinstance(message, SystemMessage):
            logger.bind(message_type="system").debug(
                f"Claude SDK system: {message.subtype}"
            )
            return

        if isinstance(message, StreamEvent):
            # Partial stream events are not translated into trajectory messages.
            return

        if isinstance(message, RateLimitEvent):
            logger.bind(message_type="rate_limit").warning(
                f"Rate limit status: {message.rate_limit_info.status}"
            )
            return

        logger.bind(message_type="debug").debug(
            f"Unhandled Claude SDK message: {type(message).__name__}"
        )

    @staticmethod
    def _extract_final_answer(messages: list[LitellmAnyMessage]) -> str:
        for msg in reversed(messages):
            role: str | None
            content: Any
            if isinstance(msg, dict):
                role = msg.get("role") if isinstance(msg.get("role"), str) else None
                content = msg.get("content")
            else:
                role = getattr(msg, "role", None)
                content = getattr(msg, "content", None)

            if role in {"assistant", "tool", "user"}:
                text = _normalize_text_content(content)
                if text:
                    return text
        return ""

    def _build_output(self) -> AgentTrajectoryOutput:
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            status=self.status,
            time_elapsed=time.time() - self.start_time if self.start_time else 0.0,
            usage=self._usage_tracker.to_dict(),
        )

    async def run(self) -> AgentTrajectoryOutput:
        try:
            async with asyncio.timeout(self.timeout):
                self.status = AgentStatus.RUNNING
                self.start_time = time.time()

                options = self._build_options()
                async for sdk_message in query(
                    prompt=self._prompt_stream(),
                    options=options,
                ):
                    self._handle_sdk_message(sdk_message)

                result_text = self._result_message.result if self._result_message else None
                final_answer = result_text or self._extract_final_answer(self.messages)
                logger.bind(message_type="final_answer").info(final_answer)

                if final_answer:
                    last = self.messages[-1] if self.messages else None
                    last_text = _normalize_text_content(
                        self._litellm_msg_content(last) if last is not None else ""
                    )
                    if last_text.strip() != final_answer.strip():
                        self.messages.append(
                            {"role": "assistant", "content": final_answer}
                        )

                if self._result_message and self._result_message.is_error:
                    self.status = AgentStatus.FAILED
                else:
                    self.status = AgentStatus.COMPLETED

                return self._build_output()

        except TimeoutError:
            logger.error(f"claude_code_agent timed out after {self.timeout}s")
            self.status = AgentStatus.ERROR
            logger.bind(message_type="final_answer").info("")
            return self._build_output()
        except asyncio.CancelledError:
            logger.error("claude_code_agent run cancelled")
            self.status = AgentStatus.CANCELLED
            logger.bind(message_type="final_answer").info("")
            return self._build_output()
        except Exception as e:
            logger.error(f"claude_code_agent failed: {repr(e)}")
            self.status = AgentStatus.ERROR if is_system_error(e) else AgentStatus.FAILED
            logger.bind(message_type="final_answer").info("")
            return self._build_output()


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """Entry point for the claude_code_agent."""
    return await ClaudeCodeAgent(run_input).run()
