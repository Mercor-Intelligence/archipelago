"""
Loop Agent implementation.

This is a simple agent that runs in a loop, calling the LLM and executing tool calls
until the LLM returns a response without tool calls (indicating task completion).
"""

import asyncio
import time
from typing import Any

from fastmcp import Client as FastMCPClient
from litellm import Choices
from litellm.exceptions import Timeout
from litellm.experimental_mcp_client import call_openai_tool, load_mcp_tools
from litellm.files.main import ModelResponse
from loguru import logger
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmAnyMessage,
    LitellmInputMessage,
    LitellmOutputMessage,
)
from runner.utils.error import is_fatal_mcp_error, is_system_error
from runner.utils.llm import generate_response
from runner.utils.mcp import (
    build_mcp_gateway_schema,
    content_blocks_to_messages,
    drain_shielded_task,
)
from runner.utils.usage import UsageTracker

# Injected each step so the model wraps up before it runs out of steps.
# This loop finalizes on a response with no tool calls, so it steers toward
# "provide your final answer" rather than a termination tool.
TURN_WARNING_TEMPLATE = (
    "Warning: {remaining} step(s) remaining before this run ends. "
    "Provide your final answer before running out of steps."
)

# Joined with the turn warning (space-separated) when a budget is configured;
# stands alone when turn warnings are off.
TOKEN_BUDGET_WARNING_TEMPLATE = (
    "You have {tokens_remaining} of {token_budget} token(s) remaining "
    "in your total token budget."
)

# Injected instead of the turn warning once the token budget is spent.
TOKEN_BUDGET_EXHAUSTED_TEMPLATE = (
    "Warning: your token budget of {token_budget} token(s) is exhausted "
    "({tokens_spent} token(s) spent). This is your final turn. "
    "Provide your final answer now."
)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Capability-gate-safe bool coercion for agent config values.

    Recognizes explicit intent only: bool True/False, "true"/"false"
    (case/whitespace-insensitive), and "0"/int 0 as False. Anything else —
    missing, None, garbage — resolves to ``default``, so a typo can never
    silently flip a capability away from its documented default.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered in ("false", "0"):
            return False
        return default
    if isinstance(value, int) and value == 0:
        return False
    return default


def finalize_answer(final_answer: str | None = None) -> str | None:
    logger.bind(message_type="final_answer").info(final_answer)
    return final_answer


class LoopAgent:
    """
    A simple loop-based agent that calls the LLM and executes tool calls
    until the task is complete.
    """

    def __init__(self, run_input: AgentRunInput):
        self.trajectory_id: str = run_input.trajectory_id
        self.model: str = run_input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(run_input.initial_messages)

        if run_input.mcp_gateway_url is None:
            raise ValueError("MCP gateway URL is required for loop agent")

        # Build MCP client for gateway connection
        self.mcp_client = FastMCPClient(
            build_mcp_gateway_schema(
                run_input.mcp_gateway_url,
                run_input.mcp_gateway_auth_token,
                run_input.mcp_gateway_actor_id,
            )
        )

        self._finalized: bool = False
        self.tools: list[ChatCompletionToolParam] = []

        # Agent config values (with defaults)
        config = run_input.agent_config_values
        self.tool_call_timeout: int = config.get("tool_call_timeout", 60)
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)
        self.max_steps: int = config.get("max_steps", 100)
        self.timeout: int = config.get("timeout", 10800)  # 3 hours
        # Total provider-reported prompt+completion tokens the run may spend.
        # 0 disables budgeting. Defensively coerced: agent_config_values is a
        # passthrough dict, so missing/null/malformed values all resolve to
        # "disabled".
        try:
            self.token_budget: int = max(int(config.get("token_budget") or 0), 0)
        except (TypeError, ValueError):
            self.token_budget = 0
        # Inject a per-step "N step(s) remaining" turn warning. Off by default
        # so ordinary runs are unaffected; independent of token_budget.
        self.turn_warnings_enabled: bool = _coerce_bool(
            config.get("turn_warnings_enabled"), default=False
        )

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}

        self.current_step: int = 0
        self.start_time: float | None = None
        self.status: AgentStatus = AgentStatus.PENDING
        self._usage_tracker: UsageTracker = UsageTracker(
            track_token_breakdown=True, model=self.model
        )

    async def _initialize_tools(self) -> None:
        """Load available tools from the MCP gateway."""
        async with self.mcp_client as client:
            tools: list[ChatCompletionToolParam] = await load_mcp_tools(
                client.session, format="openai"
            )  # pyright: ignore[reportAssignmentType]

        logger.bind(
            message_type="configure",
            payload=[tool.get("function").get("name") for tool in tools],
        ).info(f"Loaded {len(tools)} MCP tools")
        self.tools = tools

    async def _generate_response(self) -> ModelResponse:
        """Call the LLM and return a LiteLLM `ModelResponse`.

        Hook so subclasses can swap the backend (e.g. call Anthropic directly)
        without copying the whole `step()` loop. The default routes through
        LiteLLM via `generate_response`.
        """
        return await generate_response(
            self.model,
            self.messages,
            self.tools,
            self.llm_response_timeout,
            self.extra_args,
            trajectory_id=self.trajectory_id,
        )

    async def step(self):
        """Execute a single step of the agent loop."""
        self.current_step += 1

        try:
            response: ModelResponse = await self._generate_response()
        except Timeout:
            logger.bind(message_type="response").error(
                "Response timed out, continuing with next step"
            )
            return
        except Exception as e:
            logger.bind(message_type="response").error(
                f"Error generating response: {repr(e)}"
            )
            raise e

        self._usage_tracker.track(response)
        logger.debug(f"Response: {response}")

        choices = response.choices

        if not choices or not isinstance(choices[0], Choices):
            logger.bind(message_type="step").warning(
                "LLM returned invalid/empty choices, prompting to continue"
            )
            self.messages.append(
                LitellmOutputMessage(
                    role="user",
                    content="continue",
                )
            )
            return

        response_message = LitellmOutputMessage.model_validate(choices[0].message)
        tool_calls = getattr(response_message, "tool_calls", None)

        if getattr(response_message, "reasoning_content", None):
            logger.bind(message_type="reasoning").info(
                response_message.reasoning_content
            )

        if getattr(response_message, "content", None) and tool_calls:
            logger.bind(message_type="response").info(response_message.content)

        if getattr(response_message, "thinking_blocks", None):
            if isinstance(response_message.thinking_blocks, list):
                for thinking_block in response_message.thinking_blocks:
                    if thinking_block.get("thinking"):
                        logger.bind(message_type="thinking").debug(
                            thinking_block.get("thinking")
                        )

        self.messages.append(response_message)

        if tool_calls:
            deferred_image_messages: list[LitellmInputMessage] = []
            pre_tool_len = len(self.messages)
            fatal_exc: Exception | None = None
            async with self.mcp_client as client:
                for tool_call in tool_calls:
                    name = tool_call.function.name

                    tool_logger = logger.bind(
                        ref=tool_call.id,
                        name=name,
                    )

                    tool_logger.bind(
                        message_type="tool_call", payload=tool_call.function.arguments
                    ).info(f"Calling tool {name}")

                    tool_result_logger = tool_logger.bind(message_type="tool_result")

                    shielded_task = asyncio.ensure_future(
                        call_openai_tool(client.session, tool_call)
                    )
                    try:
                        call_result = await asyncio.wait_for(
                            asyncio.shield(shielded_task),
                            timeout=self.tool_call_timeout,
                        )
                    except TimeoutError:
                        tool_result_logger.error(f"Tool call {name} timed out")
                        await drain_shielded_task(shielded_task)
                        self.messages.append(
                            LitellmOutputMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                name=tool_call.function.name,
                                content="Tool call timed out",
                            )
                        )
                        continue
                    except Exception as e:
                        if is_fatal_mcp_error(e):
                            tool_result_logger.error(
                                f"Fatal MCP error, ending run: {repr(e)}"
                            )
                            self.messages.append(
                                LitellmOutputMessage(
                                    role="tool",
                                    tool_call_id=tool_call.id,
                                    name=tool_call.function.name,
                                    content=f"Fatal error: {e}",
                                )
                            )
                            fatal_exc = e
                            break
                        tool_result_logger.error(
                            f"Error calling tool {name}: {repr(e)}"
                        )
                        self.messages.append(
                            LitellmOutputMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                name=tool_call.function.name,
                                content=f"Error calling tool: {repr(e)}",
                            )
                        )
                        continue

                    if not call_result.content:
                        tool_result_logger.error(
                            f"Call result for {name} is not valid: {call_result.content}"
                        )
                        self.messages.append(
                            LitellmOutputMessage(
                                role="tool",
                                tool_call_id=tool_call.id,
                                name=tool_call.function.name,
                                content=f"Call result is not valid, received {call_result.content}",
                            )
                        )
                        continue

                    messages = content_blocks_to_messages(
                        call_result.content,
                        tool_call.id,
                        tool_call.function.name or "unknown",
                        self.model,
                        deferred_image_messages=deferred_image_messages,
                    )

                    tool_result_logger.bind(
                        payload=[result.model_dump() for result in call_result.content],
                    ).info(f"Tool {name} called successfully")

                    self.messages.extend(messages)
            self.messages.extend(deferred_image_messages)
            self._track_tool_outputs(self.messages[pre_tool_len:])
            if fatal_exc is not None:
                raise fatal_exc
        else:
            # No tool calls = task complete
            self._finalized = True
            self._usage_tracker.track_final_answer(response_message.content)
            finalize_answer(
                response_message.content if response_message.content else "No content"
            )

    def _track_tool_outputs(self, new_messages: list[Any]) -> None:
        """Count this step's tool-result text + elided images for the breakdown.

        Mirrors the ReAct toolbelt agent: tool-result text comes from
        role=="tool" messages; tool-result images are elided before storage so
        their data-URIs must be counted live, wherever they land (embedded in
        the tool message or as deferred user messages). No-op unless breakdown
        tracking is on. Messages may be dicts or pydantic models.
        """
        tool_texts: list[str] = []
        image_uris: list[str] = []
        for m in new_messages:
            content = (
                m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            )
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        url = (b.get("image_url") or {}).get("url")
                        if url:
                            image_uris.append(url)
            if role != "tool":
                continue
            if isinstance(content, str):
                tool_texts.append(content)
            elif isinstance(content, list):
                tool_texts.append(
                    " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                )
        if tool_texts:
            self._usage_tracker.track_tool_output(" ".join(tool_texts))
        for uri in image_uris:
            self._usage_tracker.track_tool_output_image(uri)

    def _tokens_spent(self) -> int:
        """Exact provider-reported prompt+completion tokens spent so far."""
        return self._usage_tracker.prompt_tokens + self._usage_tracker.completion_tokens

    def _inject_step_warning(self, step: int) -> bool:
        """Inject the per-step turn/budget warning as a user message.

        Only called when turn warnings or a token budget are enabled. Returns
        True once the budget is exhausted, granting one final step to answer.
        """
        remaining = self.max_steps - step
        tokens_spent = self._tokens_spent()
        tokens_remaining = (
            max(self.token_budget - tokens_spent, 0) if self.token_budget else None
        )
        exhausted = bool(self.token_budget) and tokens_spent >= self.token_budget
        if exhausted:
            # Budget spent: this is the final step.
            warning_msg = TOKEN_BUDGET_EXHAUSTED_TEMPLATE.format(
                token_budget=self.token_budget,
                tokens_spent=tokens_spent,
            )
        else:
            # Compose the enabled pieces: steps remaining and/or budget left.
            parts: list[str] = []
            if self.turn_warnings_enabled:
                parts.append(TURN_WARNING_TEMPLATE.format(remaining=remaining))
            if tokens_remaining is not None:
                parts.append(
                    TOKEN_BUDGET_WARNING_TEMPLATE.format(
                        tokens_remaining=tokens_remaining,
                        token_budget=self.token_budget,
                    )
                )
            warning_msg = " ".join(parts)
        # Inject as a user message and mirror it to the structured log.
        self.messages.append(LitellmOutputMessage(role="user", content=warning_msg))
        log = logger.bind(
            message_type="turn_warning", step=step + 1, remaining_turns=remaining
        )
        if tokens_remaining is not None:
            log = log.bind(remaining_tokens=tokens_remaining)
        log.info(warning_msg)
        return exhausted

    def _build_output(self) -> AgentTrajectoryOutput:
        usage = self._usage_tracker.to_dict()
        # token_budget/tokens_spent are recorded only when budgeting was on.
        if self.token_budget:
            usage["token_budget"] = self.token_budget
            usage["tokens_spent"] = self._tokens_spent()
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            status=AgentStatus(self.status),
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=usage,
        )

    async def run(self) -> AgentTrajectoryOutput:
        """Run the agent loop until completion or timeout."""
        try:
            async with asyncio.timeout(self.timeout):
                with logger.contextualize(model=self.model):
                    logger.bind(message_type="configure").info(
                        f"Starting agent loop with model {self.model}"
                    )

                    await self._initialize_tools()

                    logger.bind(message_type="configure").info(
                        "\n".join(
                            f"{m['role'].capitalize()}: {m.get('content')}"
                            for m in self.messages
                        )
                    )

                    logger.info("Starting agent loop")
                    self.start_time = time.time()
                    self.status = AgentStatus.RUNNING

                    budget_final_turn_taken = False
                    for i in range(self.max_steps):
                        if self._finalized:
                            logger.info(f"Agent loop was finalized after {i + 1} steps")
                            break
                        if budget_final_turn_taken:
                            break
                        # Per-step warnings are opt-in: with neither turn
                        # warnings nor a token_budget configured the loop runs
                        # exactly as before (no injected user messages).
                        if self.turn_warnings_enabled or self.token_budget:
                            budget_final_turn_taken = self._inject_step_warning(i)
                        logger.bind(message_type="step").info(f"Starting step {i + 1}")
                        await self.step()

                    if not self._finalized:
                        if budget_final_turn_taken:
                            logger.error(
                                f"Agent loop not finalized after exhausting token "
                                f"budget of {self.token_budget}"
                            )
                        else:
                            logger.error(
                                f"Agent loop was not finalized after {self.max_steps} steps"
                            )
                        self.status = AgentStatus.FAILED
                    else:
                        self.status = AgentStatus.COMPLETED

                    return self._build_output()

        except TimeoutError:
            logger.error(f"Agent run timed out after {self.timeout} seconds")
            self.status = AgentStatus.ERROR
            return self._build_output()

        except asyncio.CancelledError:
            logger.error("Agent run cancelled")
            self.status = AgentStatus.CANCELLED
            return self._build_output()

        except Exception as e:
            logger.error(f"Error running agent: {repr(e)}")
            if is_system_error(e):
                self.status = AgentStatus.ERROR
            else:
                self.status = AgentStatus.FAILED
            return self._build_output()


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """
    Entry point for the loop agent.

    Args:
        run_input: The input configuration for the agent run

    Returns:
        AgentTrajectoryOutput with status, messages, and metrics
    """
    agent = LoopAgent(run_input)
    return await agent.run()
