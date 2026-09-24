"""
ResponsesAgentV2 implementation.

A single-shot agent that uses the OpenAI Responses API (litellm.aresponses)
instead of the Chat Completions API. This properly handles:
- Text output
- Tool/function calls
- Web search calls
- Annotations and citations
- Reasoning content
- File normalization (Chat Completions -> Responses API format)
- Inline file uploads via the provider's Files API (with retry logic)
- Automatic docx/xlsx-to-text conversion for Gemini providers
"""

import asyncio
import random
import time
from typing import Any

from loguru import logger
from pydantic import BaseModel

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmAnyMessage,
    LitellmOutputMessage,
)
from runner.agents.responses_agent_v2.utils import (
    normalize_to_responses_format,
    process_inline_files,
)
from runner.utils.error import is_system_error
from runner.utils.llm import call_responses_api
from runner.utils.usage import UsageTracker


class ParsedResponsesOutput(BaseModel):
    """Parsed output from the Responses API."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = []
    web_search_calls: list[dict[str, Any]] = []
    function_calls: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    reasoning_content: str | None = None
    raw_output_items: list[dict[str, Any]] = []


def parse_responses_api_output(response: Any) -> ParsedResponsesOutput:
    """
    Parse the Responses API response into a structured format.

    The Responses API returns an 'output' array with items of different types:
    - message: Contains text content and annotations
    - function_call: A function/tool call made by the model
    - web_search_call: A web search action
    - reasoning: Model's reasoning/thinking (some models)

    Args:
        response: The raw response from litellm.aresponses()

    Returns:
        ParsedResponsesOutput with all extracted content
    """
    response_dict = (
        response.model_dump() if hasattr(response, "model_dump") else dict(response)
    )

    result = ParsedResponsesOutput()
    text_parts: list[str] = []

    output_items = response_dict.get("output", []) or []
    result.raw_output_items = output_items

    for output_item in output_items:
        item_type = output_item.get("type", "")

        if item_type == "message":
            for content_item in output_item.get("content", []):
                content_type = content_item.get("type", "")

                if content_type == "output_text":
                    text = content_item.get("text", "")
                    if text:
                        text_parts.append(text)

                    annotations = content_item.get("annotations", [])
                    result.annotations.extend(annotations)

                elif content_type == "refusal":
                    refusal = content_item.get("refusal", "")
                    if refusal:
                        text_parts.append(f"[REFUSAL]: {refusal}")

        elif item_type == "function_call":
            func_call = {
                "id": output_item.get("id"),
                "call_id": output_item.get("call_id"),
                "name": output_item.get("name"),
                "arguments": output_item.get("arguments"),
                "type": "function_call",
            }
            result.function_calls.append(func_call)
            result.tool_calls.append(func_call)

        elif item_type == "web_search_call":
            action = output_item.get("action") or {}
            web_search = {
                "id": output_item.get("id"),
                "query": action.get("query"),
                "status": output_item.get("status"),
                "type": "web_search_call",
            }
            result.web_search_calls.append(web_search)
            result.tool_calls.append(web_search)

        elif item_type == "reasoning":
            reasoning_parts: list[str] = []
            reasoning_content = output_item.get("content") or []
            for item in reasoning_content:
                if item.get("type") == "reasoning_text":
                    reasoning_text = item.get("text", "")
                    if reasoning_text:
                        reasoning_parts.append(reasoning_text)
            if reasoning_parts:
                existing = result.reasoning_content or ""
                new_content = "\n".join(reasoning_parts)
                result.reasoning_content = (
                    f"{existing}\n{new_content}".strip() if existing else new_content
                )

        elif item_type == "tool_call":
            func = output_item.get("function") or {}
            tool_call = {
                "id": output_item.get("id"),
                "call_id": output_item.get("id"),
                "name": func.get("name"),
                "arguments": func.get("arguments"),
                "type": "function_call",
            }
            result.function_calls.append(tool_call)
            result.tool_calls.append(tool_call)

        else:
            logger.debug(f"Unknown output item type: {item_type}")

    result.text = "\n".join(text_parts) if text_parts else ""
    return result


def responses_output_to_message(parsed: ParsedResponsesOutput) -> LitellmOutputMessage:
    """
    Convert parsed Responses API output to a LiteLLM Message format.

    This allows the response to be stored in the trajectory in a format
    compatible with other agents and the UI.
    """
    tool_calls = None
    if parsed.function_calls:
        tool_calls = [
            {
                "id": fc.get("call_id") or fc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": fc.get("name"),
                    "arguments": fc.get("arguments") or "{}",
                },
            }
            for i, fc in enumerate(parsed.function_calls)
        ]

    message_data: dict[str, Any] = {
        "role": "assistant",
        "content": parsed.text or None,
    }

    if tool_calls:
        message_data["tool_calls"] = tool_calls

    if parsed.reasoning_content:
        message_data["reasoning_content"] = parsed.reasoning_content

    return LitellmOutputMessage.model_validate(message_data)


_STARTUP_JITTER_MAX_SECONDS = 30.0


class ResponsesAgentV2:
    """
    Agent that uses the OpenAI Responses API for single-shot generation.

    V2 improvements over the baseline ResponsesAgent:
    - Normalizes Chat Completions content blocks to Responses API format
    - Uploads inline file data via the provider's Files API (with retry + backoff)
    - Converts unsupported Gemini MIME types (docx/xlsx) to text before upload
    - Startup jitter to prevent thundering-herd 429s in concurrent batch runs
    """

    def __init__(self, input: AgentRunInput):
        self.trajectory_id = input.trajectory_id
        self.model = input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(input.initial_messages)
        self.output: dict[str, Any] | None = None

        config = input.agent_config_values
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)

        self.extra_args: dict[str, Any] = dict(input.orchestrator_extra_args or {})
        self.tools: list[dict[str, Any]] = self.extra_args.pop("tools", []) or []

        self.start_time: float | None = None
        self.status: AgentStatus = AgentStatus.PENDING
        self._usage_tracker = UsageTracker()

    def _build_output(self) -> AgentTrajectoryOutput:
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            output=self.output,
            status=AgentStatus(self.status),
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=self._usage_tracker.to_dict(),
        )

    async def run(self) -> AgentTrajectoryOutput:
        """Run the responses agent v2."""
        self.start_time = time.time()
        self.status = AgentStatus.RUNNING

        logger.bind(message_type="configure").info(
            f"Starting responses agent v2 with model {self.model}, tools={len(self.tools)}"
        )

        try:
            normalized_messages = normalize_to_responses_format(self.messages)

            normalized_messages = await process_inline_files(
                normalized_messages, model=self.model
            )

            jitter = random.uniform(0, _STARTUP_JITTER_MAX_SECONDS)
            logger.info(
                f"Applying {jitter:.1f}s startup jitter to avoid thundering herd"
            )
            await asyncio.sleep(jitter)

            response = await call_responses_api(
                model=self.model,
                messages=normalized_messages,
                tools=self.tools,
                llm_response_timeout=self.llm_response_timeout,
                extra_args=self.extra_args,
                trajectory_id=self.trajectory_id,
            )

            parsed = parse_responses_api_output(response)

            response_dict = (
                response.model_dump()
                if hasattr(response, "model_dump")
                else dict(response)
            )

            self._usage_tracker.track_from_dict(response_dict)

            self.output = {
                "raw_response": response_dict,
                "parsed": parsed.model_dump(),
                "text": parsed.text,
                "tool_calls": parsed.tool_calls,
                "web_search_calls": parsed.web_search_calls,
                "annotations": parsed.annotations,
            }

            logger.bind(message_type="response_json").info(
                f"Response text: {parsed.text[:500]}..."
                if len(parsed.text) > 500
                else f"Response text: {parsed.text}"
            )

            if parsed.tool_calls:
                logger.bind(message_type="tool_calls").info(
                    f"Tool calls: {len(parsed.tool_calls)}"
                )
                for tc in parsed.tool_calls:
                    logger.bind(message_type="tool_call").info(tc)

            if parsed.web_search_calls:
                logger.bind(message_type="web_search").info(
                    f"Web searches: {[ws.get('query') for ws in parsed.web_search_calls]}"
                )

            if parsed.reasoning_content:
                logger.bind(message_type="reasoning").info(parsed.reasoning_content)

            if parsed.annotations:
                logger.bind(message_type="annotations").info(
                    f"Annotations: {len(parsed.annotations)}"
                )

            response_message = responses_output_to_message(parsed)
            self.messages.append(response_message)

            logger.bind(message_type="final_answer").info(
                parsed.text if parsed.text else "No text content"
            )

            self.status = AgentStatus.COMPLETED
            logger.info("Responses agent v2 completed successfully")

        except Exception as e:
            logger.error(f"Error in responses agent v2: {repr(e)}")
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
    Run the ResponsesAgentV2.
    """
    agent = ResponsesAgentV2(input)
    return await agent.run()
