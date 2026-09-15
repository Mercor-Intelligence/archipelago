"""
SingleShotMultimodalAgent — unified single-shot agent for Chat Completions and Responses API.

Dispatches by orchestrator model prefix:
- `responses/...` → Responses API via `litellm.aresponses`, with input blocks translated
  from Chat Completions shape (`image_url`) to Responses shape (`input_image`).
- anything else → Chat Completions via `litellm.acompletion` (streaming).

The agent is multimodal-capable on both branches. One `agent_config_id` covers any
single-turn benchmark (MMMU-Pro, HLE, MathVista, etc.) on any SOTA model, so worlds
can carry a single `default_agent_id` and only the orchestrator varies per batch.
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
from runner.agents.responses_agent.main import (
    parse_responses_api_output,
    responses_output_to_message,
)
from runner.agents.singleshot_agent.main import (
    CONTENT_FILTER_FINISH_REASON,
    classify_empty_response,
    extract_tool_artifacts,
    normalize_content,
)
from runner.utils.error import is_system_error
from runner.utils.llm import call_responses_api, generate_response
from runner.utils.usage import UsageTracker

RESPONSES_API_MODEL_PREFIX = "responses/"
META_MODEL_PREFIX = "meta/"
LOG_CONTENT_PREVIEW_CHARS = 4000


def _is_hosted_responses_tool(tool: Any) -> bool:
    """True for an OpenAI Responses-API hosted tool, e.g. `{"type": "web_search"}`.

    These bare `{"type": <kind>}` tools are only valid on the Responses API; on
    Chat Completions litellm rejects them with `tools[0] did not match any
    supported type`. A hosted tool is identified by having a non-"function"
    string `type` and NO `function` payload — and NO Anthropic-style `name`
    (Anthropic named hosted tools like `{"name": "web_search", "type":
    "web_search_20260318"}` are valid on Chat Completions and must stay there).
    """
    return (
        isinstance(tool, dict)
        and "function" not in tool
        and "name" not in tool
        and isinstance(tool.get("type"), str)
        and tool["type"] != "function"
    )


def _chat_to_responses_input(
    messages: list[LitellmAnyMessage],
) -> list[dict[str, Any]]:
    """Translate Chat Completions-format messages to Responses API input format.

    Block-level rewrites:
    - `{type: "text", text: X}`                → `{type: "input_text", text: X}`
    - `{type: "image_url", image_url: {url, detail?}}`
          → `{type: "input_image", image_url: url, detail?}`

    The optional `detail` field (`"low" | "high" | "auto"`) on `image_url` blocks
    controls image resolution / token usage and is preserved — dropping it would
    silently degrade results on multimodal benchmarks that rely on high-detail
    images.

    Messages with plain-string content, and blocks of unknown type, are passed through
    unchanged. Roles are preserved verbatim.
    """
    translated: list[dict[str, Any]] = []
    for msg in messages:
        raw = msg if isinstance(msg, dict) else msg.model_dump()
        msg_dict: dict[str, Any] = dict(raw)
        content = msg_dict.get("content")
        if not isinstance(content, list):
            translated.append(msg_dict)
            continue

        new_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_dict: dict[str, Any] = dict(block)
            btype = block_dict.get("type")
            if btype == "text":
                new_blocks.append(
                    {"type": "input_text", "text": block_dict.get("text", "")}
                )
            elif btype == "image_url":
                image_url = block_dict.get("image_url")
                if isinstance(image_url, dict):
                    url = image_url.get("url")
                    detail = image_url.get("detail")
                else:
                    url = image_url
                    detail = None
                input_image: dict[str, Any] = {"type": "input_image", "image_url": url}
                if detail is not None:
                    input_image["detail"] = detail
                new_blocks.append(input_image)
            else:
                new_blocks.append(block_dict)
        translated.append({**msg_dict, "content": new_blocks})
    return translated


def _truncate_for_log(value: Any, limit: int = LOG_CONTENT_PREVIEW_CHARS) -> Any:
    if isinstance(value, dict):
        return {k: _truncate_for_log(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_for_log(item, limit) for item in value]
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated {len(value) - limit} chars]"


def _message_to_log_payload(
    message: LitellmAnyMessage, index: int | None = None
) -> dict[str, Any]:
    raw = message if isinstance(message, dict) else message.model_dump()
    payload: dict[str, Any] = {
        "index": index,
        "role": raw.get("role"),
        "content": _truncate_for_log(raw.get("content")),
    }

    for key in (
        "tool_calls",
        "function_call",
        "reasoning_content",
        "thinking_blocks",
    ):
        value = raw.get(key)
        if value:
            payload[key] = _truncate_for_log(value)

    return {k: v for k, v in payload.items() if v is not None}


def _response_metadata_payload(response_dict: dict[str, Any]) -> dict[str, Any]:
    choices: list[dict[str, Any]] = []
    for choice in response_dict.get("choices", []) or []:
        message = choice.get("message", {}) or {}
        choice_payload: dict[str, Any] = {
            "index": choice.get("index"),
            "finish_reason": choice.get("finish_reason"),
            "message": {
                "role": message.get("role"),
                "content_preview": _truncate_for_log(message.get("content"), 1000),
                "has_reasoning_content": bool(message.get("reasoning_content")),
                "has_tool_calls": bool(message.get("tool_calls")),
            },
        }
        choices.append(choice_payload)

    return {
        "id": response_dict.get("id"),
        "created": response_dict.get("created"),
        "model": response_dict.get("model"),
        "object": response_dict.get("object"),
        "system_fingerprint": response_dict.get("system_fingerprint"),
        "usage": response_dict.get("usage"),
        "choices": choices,
    }


def _trajectory_output_payload(output: AgentTrajectoryOutput) -> dict[str, Any]:
    usage = output.usage or {}
    return {
        "status": output.status.value,
        "time_elapsed": output.time_elapsed,
        "message_count": len(output.messages),
        "has_output": output.output is not None,
        "usage": usage,
        "usage_metrics": usage,
    }


class SingleShotMultimodalAgent:
    """Single-shot agent routing to Chat Completions or Responses API by model prefix."""

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
        self._usage_tracker = UsageTracker(track_token_breakdown=True, model=self.model)

    def _uses_responses_api(self) -> bool:
        # Route to litellm's Responses API on an explicit `responses/` model
        # prefix, or whenever a hosted Responses tool is present — those tools
        # are invalid on Chat Completions, so a caller that supplied one must
        # have intended the Responses API.
        #
        # Meta models are the exception: they are served by the `meta_native`
        # custom provider (infra/litellm/meta_provider.py), which speaks Chat
        # Completions and translates hosted tools to /responses itself. So
        # `meta/...` always stays on the chat path even with a hosted tool.
        if self.model.startswith(RESPONSES_API_MODEL_PREFIX):
            return True
        if self.model.startswith(META_MODEL_PREFIX):
            return False
        return any(_is_hosted_responses_tool(t) for t in self.tools)

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

    async def _run_chat_completions(self) -> None:
        messages_dict = [
            msg if isinstance(msg, dict) else msg.model_dump() for msg in self.messages
        ]
        trim_limit = self._resolve_trim_limit()
        if trim_limit:
            trimmed = trim_messages(messages_dict, self.model, max_tokens=trim_limit)
        else:
            trimmed = trim_messages(messages_dict, self.model)
        trimmed_messages = cast(list[LitellmAnyMessage], list(trimmed))

        response: ModelResponse = await generate_response(
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
            "api": "chat_completions",
            "raw_response": response_dict,
            "tool_artifacts": tool_artifacts,
        }

        logger.bind(
            message_type="response_json",
            payload=_response_metadata_payload(response_dict),
        ).info("Model response metadata")

        if tool_artifacts:
            logger.bind(
                message_type="tool_artifacts",
                payload=_truncate_for_log(tool_artifacts),
            ).info("Tool artifacts")

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
            return

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
            response_message.content if response_message.content else "No content"
        )

        if response_message.content:
            self.status = AgentStatus.COMPLETED
        else:
            # No text answer. Classify the terminal cause before accepting
            # tool calls as the answer — mirroring the Responses-API branch —
            # so token exhaustion or a content-filter refusal that arrives
            # alongside (possibly truncated) tool calls stays a scoreable
            # FAILED instead of being graded as a normal answer. Exhaustion of
            # either a configured or provider-default token budget is a
            # genuine capability failure (FAILED); anything else empty is a
            # streaming artifact marked ERROR (excluded from scoring).
            status, empty_class = classify_empty_response(response, self.max_tokens)
            if status is AgentStatus.FAILED:
                self.status = status
                self.output["empty_answer_class"] = empty_class
                logger.bind(message_type="configure").warning(
                    f"Model produced a terminal non-answer ({empty_class}, finish_reason={getattr(choices[0], 'finish_reason', None)}); marking trajectory FAILED (scored 0)"
                )
            elif getattr(response_message, "tool_calls", None):
                # A tool call is a usable terminal output for this
                # single-shot agent.
                self.status = AgentStatus.COMPLETED
            else:
                self.status = status
                self.output["empty_answer_class"] = empty_class
                logger.bind(message_type="configure").error(
                    f"Model produced no final answer (finish_reason={getattr(choices[0], 'finish_reason', None)}); marking trajectory ERROR"
                )

    async def _run_responses_api(self) -> None:
        translated = _chat_to_responses_input(self.messages)

        # Strip the `responses/` routing prefix — LiteLLM does not recognize it
        # and will otherwise forward it to the upstream provider as part of the
        # model name, causing "model not found" errors.
        upstream_model = self.model.removeprefix(RESPONSES_API_MODEL_PREFIX)

        response = await call_responses_api(
            model=upstream_model,
            messages=cast(list[LitellmAnyMessage], translated),
            tools=self.tools,
            llm_response_timeout=self.llm_response_timeout,
            extra_args=self.extra_args,
            trajectory_id=self.trajectory_id,
        )

        parsed = parse_responses_api_output(response)
        response_dict = (
            response.model_dump() if hasattr(response, "model_dump") else dict(response)
        )
        self._usage_tracker.track_from_dict(response_dict)

        self.output = {
            "api": "responses",
            "raw_response": response_dict,
            "parsed": parsed.model_dump(),
            "text": parsed.text,
            "tool_calls": parsed.tool_calls,
            "web_search_calls": parsed.web_search_calls,
            "annotations": parsed.annotations,
        }

        logger.bind(
            message_type="response_json",
            payload={
                "api": "responses",
                "usage": response_dict.get("usage"),
                "output_item_types": [
                    item.get("type")
                    for item in response_dict.get("output", []) or []
                    if isinstance(item, dict)
                ],
                "tool_call_count": len(parsed.tool_calls),
                "web_search_count": len(parsed.web_search_calls),
                "annotation_count": len(parsed.annotations),
            },
        ).info("Model response metadata")

        if parsed.tool_calls:
            logger.bind(
                message_type="tool_calls",
                payload=_truncate_for_log(parsed.tool_calls),
            ).info(f"Tool calls: {len(parsed.tool_calls)}")

        if parsed.web_search_calls:
            logger.bind(
                message_type="web_search",
                payload=_truncate_for_log(parsed.web_search_calls),
            ).info(f"Web searches: {len(parsed.web_search_calls)}")

        if parsed.reasoning_content:
            logger.bind(message_type="reasoning").info(parsed.reasoning_content)

        if parsed.annotations:
            logger.bind(
                message_type="annotations",
                payload=_truncate_for_log(parsed.annotations),
            ).info(f"Annotations: {len(parsed.annotations)}")

        response_message = responses_output_to_message(parsed)
        self.messages.append(response_message)
        self._usage_tracker.track_final_answer(parsed.text)

        logger.bind(message_type="final_answer").info(
            parsed.text if parsed.text else "No text content"
        )

        if parsed.text:
            self.status = AgentStatus.COMPLETED
            return

        incomplete_details = response_dict.get("incomplete_details") or {}
        incomplete_reason = (
            incomplete_details.get("reason")
            if isinstance(incomplete_details, dict)
            else None
        )
        if incomplete_reason == "max_output_tokens":
            self.status = AgentStatus.FAILED
            self.output["empty_answer_class"] = "output_tokens_exhausted"
            logger.bind(message_type="configure").warning(
                "Responses API exhausted output tokens without a final answer; "
                "marking trajectory FAILED (scored 0)"
            )
        elif incomplete_reason == CONTENT_FILTER_FINISH_REASON:
            self.status = AgentStatus.FAILED
            self.output["empty_answer_class"] = "content_filter"
            logger.bind(message_type="configure").warning(
                "Responses API returned a content-filter non-answer; marking "
                "trajectory FAILED (scored 0)"
            )
        elif parsed.function_calls:
            # A function call is a usable terminal output for this single-shot
            # agent. Hosted actions such as web_search_call are intermediate
            # work and do not count as a final answer by themselves.
            self.status = AgentStatus.COMPLETED
        else:
            self.status = AgentStatus.ERROR
            self.output["empty_answer_class"] = "no_final_answer"
            logger.bind(message_type="configure").error(
                "Responses API produced no final answer "
                f"(status={response_dict.get('status')}, reason={incomplete_reason}); "
                "marking trajectory ERROR"
            )

    async def run(self) -> AgentTrajectoryOutput:
        self.start_time = time.time()
        self.status = AgentStatus.RUNNING

        api_label = "responses" if self._uses_responses_api() else "chat_completions"
        logger.bind(message_type="configure").info(
            f"Starting single-shot multimodal agent "
            f"(api={api_label}, model={self.model}, tools={len(self.tools)})"
        )
        for idx, message in enumerate(self.messages):
            payload = _message_to_log_payload(message, idx)
            role = payload.get("role") or "unknown"
            logger.bind(message_type="input", payload=payload).info(
                f"Input message {idx + 1}/{len(self.messages)} ({role})"
            )

        try:
            if self._uses_responses_api():
                await self._run_responses_api()
            else:
                await self._run_chat_completions()
            # Either API path can classify an empty response without raising,
            # so only log success when the run actually reached COMPLETED.
            if self.status == AgentStatus.COMPLETED:
                logger.info("Single-shot multimodal agent completed successfully")
        except Exception as e:
            logger.error(f"Error in single-shot multimodal agent: {repr(e)}")
            if is_system_error(e):
                self.status = AgentStatus.ERROR
            else:
                self.status = AgentStatus.FAILED

        trajectory_output = self._build_output()
        logger.bind(
            message_type="trajectory_output",
            payload=_trajectory_output_payload(trajectory_output),
        ).info("Trajectory output saved")
        return trajectory_output


async def run(input: AgentRunInput) -> AgentTrajectoryOutput:
    """Run the SingleShotMultimodalAgent."""
    agent = SingleShotMultimodalAgent(input)
    return await agent.run()
