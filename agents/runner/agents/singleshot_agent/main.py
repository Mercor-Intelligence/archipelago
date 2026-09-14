"""
SingleShotAgent implementation.

A Simplest agent that just runs a single shot of the LLM with prompt and returns the response.
No tool calls and no loops.
"""

import time
from typing import Any, cast

from litellm import Choices, get_model_info
from litellm.files.main import ModelResponse
from litellm.utils import trim_messages
from loguru import logger
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmAnyMessage,
    LitellmOutputMessage,
)
from runner.utils.error import is_system_error
from runner.utils.llm import generate_response
from runner.utils.usage import UsageTracker


def normalize_content(content: Any) -> str:
    """
    Normalize message content to a string.

    Handles multi-part content from models like Gemini when using code execution,
    which returns content as a list of parts (text, file, etc.) instead of a string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    text_parts.append(part["text"])
                elif "text" in part:
                    text_parts.append(part["text"])
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts) if text_parts else str(content)
    return str(content)


def extract_tool_artifacts(response_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Extract tool artifacts from LiteLLM response (text content, tool calls).
    """
    artifacts: dict[str, Any] = {
        "text_responses": [],
        "tool_calls": [],
    }

    for choice in response_dict.get("choices", []) or []:
        message = choice.get("message", {}) or {}

        content = message.get("content")
        if content:
            # Normalize content in case it's multi-part
            normalized = normalize_content(content)
            if normalized:
                artifacts["text_responses"].append(normalized)

        tool_calls = message.get("tool_calls") or []
        for tool_call in tool_calls:
            func = tool_call.get("function", {}) or {}
            artifacts["tool_calls"].append(
                {
                    "tool_id": tool_call.get("id"),
                    "tool_type": tool_call.get("type"),
                    "function_name": func.get("name"),
                    "arguments": func.get("arguments"),
                }
            )
    return {k: v for k, v in artifacts.items() if v}


def _first_choice(response: ModelResponse) -> Choices | None:
    choices = response.choices
    if choices and isinstance(choices[0], Choices):
        return choices[0]
    return None


def _first_finish_reason(response: ModelResponse) -> str | None:
    choice = _first_choice(response)
    return getattr(choice, "finish_reason", None) if choice else None


def _completion_tokens(response: ModelResponse) -> int:
    """The final completion's total output tokens (reasoning + answer), or 0.

    Read defensively: a stream-dropped response can carry a missing or bogus
    (e.g. 0) usage block, which then correctly fails the reached-the-cap check
    below and keeps the trajectory in the ERROR (rerun) bucket.
    """
    usage = getattr(response, "usage", None)
    raw = getattr(usage, "completion_tokens", None) if usage is not None else None
    try:
        value = int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


# Confirmed token exhaustion. With an explicit cap, require the completion to
# actually reach it — an explicit cap left unreached means the `length` was a
# mislabeled stream truncation. With no explicit cap, `length` plus real output
# is the only signal a provider default was hit.
CAP_REACHED_FRACTION = 0.95
CONTENT_FILTER_FINISH_REASON = "content_filter"


def _token_exhaustion_class(
    finish_reason: str | None,
    completion_tokens: int,
    max_tokens: int | None,
) -> str | None:
    """The exhaustion class for an empty answer, or None if not exhaustion."""
    if finish_reason != "length" or completion_tokens <= 0:
        return None
    if not max_tokens:
        return "default_cap_exhausted"
    if completion_tokens >= CAP_REACHED_FRACTION * max_tokens:
        return "unanswered_budget"
    return None


def classify_empty_response(
    response: ModelResponse, max_tokens: int | None
) -> tuple[AgentStatus, str]:
    """Map a response that produced no final answer to (status, empty_answer_class).

    Token exhaustion (configured or provider-default) and content-policy
    refusals are model failures: deterministic on rerun, so they stay scoreable
    and terminal. Anything else empty is a system ERROR, consistent with a
    dropped or malformed stream. No empty response is regenerated in-agent.
    """
    finish_reason = _first_finish_reason(response)
    exhaustion = _token_exhaustion_class(
        finish_reason, _completion_tokens(response), max_tokens
    )
    if exhaustion:
        return AgentStatus.FAILED, exhaustion
    if (finish_reason or "").lower() == CONTENT_FILTER_FINISH_REASON:
        return AgentStatus.FAILED, "content_filter"
    return AgentStatus.ERROR, "no_final_answer"


class SingleShotAgent:
    def __init__(self, input: AgentRunInput):
        self.trajectory_id = input.trajectory_id
        self.model = input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(input.initial_messages)
        self.output: dict[str, Any] | None = None

        config = input.agent_config_values
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)

        self.extra_args: dict[str, Any] = dict(input.orchestrator_extra_args or {})
        self.max_input_tokens: int | None = self.extra_args.pop(
            "max_input_tokens", None
        )
        self.max_tokens: int | None = self.extra_args.get("max_tokens")
        self.tools: list[dict[str, Any]] = self.extra_args.pop("tools", []) or []

        self.start_time: float | None = None
        self.status: AgentStatus = AgentStatus.PENDING
        self._usage_tracker: UsageTracker = UsageTracker(
            track_token_breakdown=True, model=self.model
        )

    def _build_output(self) -> AgentTrajectoryOutput:
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            output=self.output,
            status=AgentStatus(self.status),
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=self._usage_tracker.to_dict(),
        )

    def _resolve_trim_limit(self) -> int | None:
        buffer_tokens = 1000

        if self.max_input_tokens:
            return max(int(self.max_input_tokens) - buffer_tokens, 0)

        try:
            model_info = get_model_info(self.model)
        except Exception:
            return None

        max_input = model_info.get("max_input_tokens")
        if max_input is None:
            return None
        max_tokens = self.max_tokens or model_info.get("max_output_tokens") or 0
        return max(int(max_input) - int(max_tokens) - buffer_tokens, 0)

    async def run(self) -> AgentTrajectoryOutput:
        """Run the single-shot agent."""
        self.start_time = time.time()
        self.status = AgentStatus.RUNNING

        logger.bind(message_type="configure").info(
            f"Starting single-shot agent with model {self.model}, tools={len(self.tools)}"
        )

        logger.bind(message_type="configure").info(
            "\n".join(
                f"{m['role'].capitalize()}: {m.get('content')}" for m in self.messages
            )
        )

        try:
            messages_dict = [
                msg if isinstance(msg, dict) else msg.model_dump()
                for msg in self.messages
            ]
            trim_limit = self._resolve_trim_limit()
            if trim_limit:
                trimmed = trim_messages(
                    messages_dict, self.model, max_tokens=trim_limit
                )
            else:
                trimmed = trim_messages(messages_dict, self.model)
            # Use trimmed dicts directly — LitellmOutputMessage.model_validate()
            # rejects multimodal messages where content is a list of blocks
            # (e.g. image_url + text) because Message expects content: str.
            trimmed_messages = cast(list[LitellmAnyMessage], list(trimmed))

            response = await generate_response(
                self.model,
                trimmed_messages,
                tools=cast(list[ChatCompletionToolParam], self.tools),
                llm_response_timeout=self.llm_response_timeout,
                extra_args=self.extra_args,
                trajectory_id=self.trajectory_id,
                stream=True,
            )
            self._usage_tracker.track(response)
            response_dict = response.model_dump()
            tool_artifacts = extract_tool_artifacts(response_dict)

            self.output = {
                "raw_response": response_dict,
                "tool_artifacts": tool_artifacts,
            }

            logger.bind(message_type="response_json").info(
                response.model_dump_json(indent=2)
            )

            if tool_artifacts:
                logger.bind(message_type="tool_artifacts").info(tool_artifacts)

            choices = response.choices
            if not choices or not isinstance(choices[0], Choices):
                # No choice at all: no final answer was produced. Mark ERROR
                # (system error, excluded from scoring) rather than
                # scoring an unanswered run as a wrong answer.
                logger.error(
                    "Model returned an empty response with no choices; marking trajectory ERROR"
                )
                self.output["empty_answer_class"] = "no_choices"
                self.status = AgentStatus.ERROR
            else:
                # Normalize content before validation (handles multi-part responses)
                message_data = choices[0].message.model_dump()
                if "content" in message_data:
                    message_data["content"] = normalize_content(message_data["content"])

                response_message = LitellmOutputMessage.model_validate(message_data)
                self.messages.append(response_message)
                self._usage_tracker.track_final_answer(response_message.content)

                if getattr(response_message, "reasoning_content", None):
                    logger.bind(message_type="reasoning").info(
                        response_message.reasoning_content
                    )

                if getattr(response_message, "thinking_blocks", None):
                    if isinstance(response_message.thinking_blocks, list):
                        for thinking_block in response_message.thinking_blocks:
                            if thinking_block.get("thinking"):
                                logger.bind(message_type="thinking").debug(
                                    thinking_block.get("thinking")
                                )

                logger.bind(message_type="final_answer").info(
                    response_message.content
                    if response_message.content
                    else "No content"
                )

                if response_message.content:
                    self.status = AgentStatus.COMPLETED
                    logger.info("Single-shot agent completed successfully")
                else:
                    # No text answer. Classify the terminal cause before
                    # accepting tool calls as the answer — mirroring the
                    # Responses-API branch — so token exhaustion or a
                    # content-filter refusal that arrives alongside (possibly
                    # truncated) tool calls stays a scoreable FAILED instead of
                    # being graded as a normal answer. Exhaustion of either a
                    # configured or provider-default token budget is a genuine
                    # capability failure (FAILED); anything else empty is a
                    # streaming artifact marked ERROR (excluded from scoring).
                    status, empty_class = classify_empty_response(
                        response, self.max_tokens
                    )
                    if status is AgentStatus.FAILED:
                        self.status = status
                        self.output["empty_answer_class"] = empty_class
                        logger.bind(message_type="configure").warning(
                            f"Model produced a terminal non-answer ({empty_class}, finish_reason={_first_finish_reason(response)}); marking trajectory FAILED (scored 0)"
                        )
                    elif getattr(response_message, "tool_calls", None):
                        # A tool call is a usable terminal output for this
                        # single-shot agent.
                        self.status = AgentStatus.COMPLETED
                        logger.info("Single-shot agent completed successfully")
                    else:
                        self.status = status
                        self.output["empty_answer_class"] = empty_class
                        logger.bind(message_type="configure").error(
                            f"Model produced no final answer (finish_reason={getattr(choices[0], 'finish_reason', None)}); marking trajectory ERROR"
                        )

        except Exception as e:
            logger.error(f"Error in single-shot agent: {repr(e)}")
            if is_system_error(e):
                self.status = AgentStatus.ERROR
            else:
                self.status = AgentStatus.FAILED

        trajectory_output = self._build_output()
        logger.bind(message_type="trajectory_output").info(
            trajectory_output.model_dump_json(indent=2)
        )
        return trajectory_output


async def run(input: AgentRunInput) -> AgentTrajectoryOutput:
    """
    Run the SingleShotAgent.
    """
    agent = SingleShotAgent(input)
    return await agent.run()
