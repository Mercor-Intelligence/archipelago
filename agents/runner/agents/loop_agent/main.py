"""
Loop Agent implementation.

This is a simple agent that runs in a loop, calling the LLM and executing tool calls
until the LLM returns a response without tool calls (indicating task completion).
"""

import asyncio
import itertools
import time
from contextvars import Token
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
from runner.utils.file_staging import TurnFileStagingError, stage_message_files
from runner.utils.llm import (
    LLMIdleCreditAccumulator,
    compute_call_cost_usd,
    generate_response,
    llm_idle_credit_ctx,
)
from runner.utils.logging.terminal_error import (
    Budget,
    budget_exhausted,
    empty_response,
)
from runner.utils.mcp import (
    build_mcp_gateway_schema,
    content_blocks_to_messages,
    drain_shielded_task,
)
from runner.utils.sandbox_files import SandboxUploadError
from runner.utils.settings import get_settings
from runner.utils.usage import UsageTracker

# Local twin of MAX_CONSECUTIVE_EMPTY_REPLIES in loop_truncated_tools_agent.
MAX_CONSECUTIVE_REASONING_ONLY_TURNS = 4

# Does not refer the model back to its own reasoning: the Responses API
# request is rebuilt from `self.messages`, which carry none.
REASONING_ONLY_NUDGE = (
    "Your last turn gave no answer and made no tool call. Do not send "
    "another turn like that. Either call a tool to do the work, or state "
    "your final answer as text. Do not restate your reasoning as the "
    "answer: your working is not the deliverable. Give the result the task "
    "asked for, or take the action that produces it."
)

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

# Injected each step in "cost_accounting" mode, once cost_budget_usd is set.
COST_BUDGET_WARNING_TEMPLATE = (
    "You have ${cost_remaining:.4f} of ${cost_budget:.4f} remaining "
    "in your total cost budget."
)

# Injected instead of the cost-budget warning once the cost budget is spent.
COST_BUDGET_EXHAUSTED_TEMPLATE = (
    "Warning: your cost budget of ${cost_budget:.4f} is exhausted "
    "(${cost_spent:.4f} spent). This is your final turn. "
    "Provide your final answer now."
)

# Accounting modes: "default" (no budget/warning mechanism active), "token_accounting"
# (the existing token_budget/turn_warnings_enabled mechanism), "cost_accounting" (real
# $ cost tracking via rate overrides + cost_budget_usd). Mutually exclusive.
_ACCOUNTING_MODES = frozenset({"default", "token_accounting", "cost_accounting"})


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


def _coerce_choice(value: Any, *, choices: frozenset[str], default: str) -> str:
    """Allowlist coercion for a config value restricted to a fixed set of strings.

    Same defensive philosophy as `_coerce_bool`: missing/None/garbage/typo'd
    values all resolve to `default`, never passed through raw.
    """
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def finalize_answer(final_answer: str | None = None) -> str | None:
    logger.bind(message_type="final_answer").info(final_answer)
    return final_answer


class NoToolCallsTurnMixin:
    """Shared handling for a model turn that comes back with no tool calls.

    `LoopTruncatedToolsAgent` is a copy of `LoopAgent` and not a subclass of
    it, so both classes mix this in. That keeps one copy of the rule and one
    definition of the counter that bounds it.

    The host class declares and initializes the attributes below in its own
    `__init__`, which is what the ignores record.
    """

    model: str  # pyright: ignore[reportUninitializedInstanceVariable]
    messages: list[LitellmAnyMessage]  # pyright: ignore[reportUninitializedInstanceVariable]
    _usage_tracker: UsageTracker  # pyright: ignore[reportUninitializedInstanceVariable]
    _finalized: bool  # pyright: ignore[reportUninitializedInstanceVariable]
    _abort_reason: str | None  # pyright: ignore[reportUninitializedInstanceVariable]
    _consecutive_reasoning_only_turns: int  # pyright: ignore[reportUninitializedInstanceVariable]

    @staticmethod
    def _turn_carried_reasoning(response_message: LitellmOutputMessage) -> bool:
        """Report whether the turn carried reasoning or thinking text."""
        reasoning = getattr(response_message, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning.strip():
            return True
        blocks = getattr(response_message, "thinking_blocks", None)
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                thinking = block.get("thinking")
                if isinstance(thinking, str) and thinking.strip():
                    return True
                # Anthropic's safety-redacted reasoning: opaque `data`, no
                # `thinking`. Proves a thinking turn; payload is never read.
                if block.get("type") == "redacted_thinking" and block.get("data"):
                    return True
        return False

    def _handle_no_tool_calls(self, response_message: LitellmOutputMessage) -> None:
        """End the run on a tool-less turn, unless the turn was reasoning only.

        A turn with no tool calls and no content, but with reasoning or
        thinking text, is a model that worked something out and then neither
        spoke nor acted. Finalizing it records the placeholder "No content" as
        the run's answer, which grades as a bad answer instead of surfacing as
        a harness failure. So nudge the model and let the loop continue.

        The reasoning text itself is never promoted into the answer. It holds
        abandoned branches and self-corrections, so handing it to a grader is
        worse than failing the run.

        After MAX_CONSECUTIVE_REASONING_ONLY_TURNS the loop stops and `run()`
        records a terminal error. Any turn with content or tool calls resets
        the counter, and every such turn keeps its existing behaviour.
        """
        content = getattr(response_message, "content", None)
        if not content and self._turn_carried_reasoning(response_message):
            self._consecutive_reasoning_only_turns += 1
            seen = self._consecutive_reasoning_only_turns
            if seen >= MAX_CONSECUTIVE_REASONING_ONLY_TURNS:
                self._abort_reason = (
                    f"{self.model} produced reasoning with no answer and no "
                    f"tool call in {seen} consecutive steps"
                )
                logger.bind(message_type="step").warning(
                    f"Reasoning-only turn ({seen}/"
                    f"{MAX_CONSECUTIVE_REASONING_ONLY_TURNS}), giving up"
                )
                return
            logger.bind(message_type="step").warning(
                f"Reasoning-only turn ({seen}/"
                f"{MAX_CONSECUTIVE_REASONING_ONLY_TURNS}), asking the model "
                f"to act or answer"
            )
            self.messages.append(
                LitellmOutputMessage(role="user", content=REASONING_ONLY_NUDGE)
            )
            return

        self._consecutive_reasoning_only_turns = 0
        # No tool calls = task complete
        self._finalized = True
        self._usage_tracker.track_final_answer(content)
        finalize_answer(content if content else "No content")


class LoopAgent(NoToolCallsTurnMixin):
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
        # True while the step in flight is the one extra turn granted after a
        # token/cost budget ran out. The loop stops after that turn, so a step
        # handler that would otherwise postpone its answer to a later turn has
        # to know that no later turn exists.
        self._budget_final_turn: bool = False
        # Set when the loop gives up on a model that keeps reasoning without
        # answering or acting. run() turns it into a terminal error.
        self._abort_reason: str | None = None
        self._consecutive_reasoning_only_turns: int = 0
        self.tools: list[ChatCompletionToolParam] = []

        # Agent config values (with defaults)
        config = run_input.agent_config_values
        self.tool_call_timeout: int = config.get("tool_call_timeout", 60)
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)
        self.max_steps: int = config.get("max_steps", 100)
        self.timeout: int = config.get("timeout", 10800)  # 3 hours
        # Default False preserves today's non-streaming Loop behaviour; a
        # crediting world turns it on (TTFT is unmeasurable non-streaming).
        self.stream: bool = _coerce_bool(config.get("stream"), default=False)
        self.stage_files_to_env: bool = _coerce_bool(
            config.get("stage_files_to_env"), default=False
        )
        self._mcp_gateway_url: str = run_input.mcp_gateway_url
        self._mcp_gateway_auth_token: str | None = run_input.mcp_gateway_auth_token
        # Wall-clock budget fidelity, mirroring the Stirrup agent (both off by
        # default so an unchanged config is byte-identical). See the Stirrup
        # agent for the full rationale.
        self.complete_on_budget_exhausted: bool = _coerce_bool(
            config.get("complete_on_budget_exhausted"), default=False
        )
        try:
            self.llm_idle_credit_sec: float = max(
                float(config.get("llm_idle_credit_sec") or 0), 0.0
            )
        except (TypeError, ValueError):
            self.llm_idle_credit_sec = 0.0
        # Total provider-reported prompt+completion tokens the run may spend.
        # 0 disables budgeting. Defensively coerced: agent_config_values is a
        # passthrough dict, so missing/null/malformed values all resolve to
        # "disabled".
        try:
            raw_token_budget = max(int(config.get("token_budget") or 0), 0)
        except (TypeError, ValueError):
            raw_token_budget = 0
        # Inject a per-step "N step(s) remaining" turn warning. Off by default
        # so ordinary runs are unaffected; independent of token_budget.
        raw_turn_warnings_enabled = _coerce_bool(
            config.get("turn_warnings_enabled"), default=False
        )

        # accounting_mode picks one of three mutually-exclusive families: the
        # existing pure-token mechanism (token_budget) or the real $ cost
        # mechanism below. Unrecognized/missing values default to "default"
        # (no mechanism active). Back-compat: agents configured before this
        # field existed already have token_budget set directly with no
        # accounting_mode — promote those to "token_accounting" so their
        # warnings keep firing unchanged. Keyed on the RAW value being None
        # (the key truly absent), not the coerced value equaling "default" —
        # the UI never clears a hidden field's stored value on save, so a
        # user who explicitly switches back to "Default" with a stale
        # token_budget still in the config must have that choice stick
        # instead of being silently re-promoted.
        raw_accounting_mode = config.get("accounting_mode")
        accounting_mode = _coerce_choice(
            raw_accounting_mode, choices=_ACCOUNTING_MODES, default="default"
        )
        if raw_accounting_mode is None and raw_token_budget:
            accounting_mode = "token_accounting"
        self.accounting_mode: str = accounting_mode
        self.token_accounting_active: bool = accounting_mode == "token_accounting"
        self.cost_accounting_active: bool = accounting_mode == "cost_accounting"

        self.token_budget: int = raw_token_budget if self.token_accounting_active else 0
        # Orthogonal to accounting_mode: a step-count reminder, unrelated to
        # which budget currency (if any) is being tracked, so it applies in
        # every mode — unlike token_budget, which is specific to
        # token_accounting.
        self.turn_warnings_enabled: bool = raw_turn_warnings_enabled

        # Per-token $ rate overrides for cost_accounting mode. Only included
        # when explicitly set (never a silent 0.0 override) — otherwise cost
        # computation falls through to the litellm-table/model_rates_ctx chain.
        self.rate_overrides: dict[str, float] = {}
        for rate_key in (
            "input_cost_per_token",
            "output_cost_per_token",
            "cached_input_cost_per_token",
            "cache_creation_cost_per_token",
        ):
            rate_value = config.get(rate_key)
            if rate_value is None:
                continue
            try:
                self.rate_overrides[rate_key] = float(rate_value)
            except (TypeError, ValueError):
                logger.bind(message_type="configure").warning(
                    f"Ignoring malformed rate override {rate_key}={rate_value!r} "
                    "(not a number); falling back to table/ctx pricing for this rate"
                )
        # Total USD the run may spend in cost_accounting mode. 0 means "log
        # only, no cap" (mirrors token_budget's 0-disables-budgeting meaning).
        try:
            self.cost_budget_usd: float = max(
                float(config.get("cost_budget_usd") or 0), 0
            )
        except (TypeError, ValueError):
            self.cost_budget_usd = 0.0
        self._cost_spent: float = 0.0
        # Calls whose usage was unreadable (so contributed $0 to _cost_spent).
        # Surfaced in usage so a cost_budget_usd cap that's silently breached
        # by unpriced calls is at least explainable after the fact.
        self._cost_unpriced_calls: int = 0

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}

        self.current_step: int = 0
        # start_time is the budget clock (advanced by idle credit);
        # _raw_start_time stays raw so time_elapsed is honest.
        self.start_time: float | None = None
        self._raw_start_time: float | None = None
        self._budget_limited_attempt: bool = False
        self._attempt_outcome: str | None = None
        self._idle_accumulator: LLMIdleCreditAccumulator = LLMIdleCreditAccumulator()
        # run() resets the ContextVar with this token in its finally so the
        # accumulator reference does not outlive the run into a reused context.
        self._idle_credit_token: Token[LLMIdleCreditAccumulator | None] | None = None
        self._idle_credit_remaining: float = self.llm_idle_credit_sec
        self._idle_credit_granted: float = 0.0
        self._idle_credit_measured: float = 0.0
        self._idle_credit_calls: int = 0
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
            stream=self.stream,
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
        if self.cost_accounting_active:
            self._track_step_cost(response)
        logger.debug(f"Response: {response}")

        choices = response.choices

        if not choices or not isinstance(choices[0], Choices):
            self._consecutive_reasoning_only_turns = 0
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
            self._consecutive_reasoning_only_turns = 0
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
            self._handle_no_tool_calls(response_message)

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

    def _track_step_cost(self, response: ModelResponse) -> None:
        """Compute and log this step's real $ cost (cost_accounting mode only).

        Always logs — cost_budget_usd=0 means "no cap", not "don't log". A
        call with unreadable usage contributes $0 to _cost_spent (there's no
        token count to price), but is counted in _cost_unpriced_calls so a
        cost_budget_usd cap silently breached by unpriced calls is still
        explainable after the fact — real spend must never look like it was
        simply never incurred.
        """
        call_cost = compute_call_cost_usd(self.model, response, self.rate_overrides)
        if call_cost is None:
            self._cost_unpriced_calls += 1
            logger.bind(
                message_type="step_cost",
                step=self.current_step,
                unpriced_calls=self._cost_unpriced_calls,
            ).warning("Cost unavailable for this call (unreadable usage); not counted")
            return
        self._cost_spent += call_cost
        logger.bind(
            message_type="step_cost",
            step=self.current_step,
            call_cost_usd=call_cost,
            cumulative_cost_usd=self._cost_spent,
        ).info(f"Step cost: ${call_cost:.6f} (cumulative: ${self._cost_spent:.6f})")

    def _tokens_spent(self) -> int:
        """Exact provider-reported prompt+completion tokens spent so far."""
        return self._usage_tracker.prompt_tokens + self._usage_tracker.completion_tokens

    def _cost_budget_warning(self) -> tuple[bool, str, float]:
        """Compose the cost-budget warning (or exhausted variant) for
        cost_accounting mode. Mirrors the token-budget branch below, just
        priced in $ instead of tokens. Returns (exhausted, warning_msg,
        cost_remaining)."""
        cost_remaining = max(self.cost_budget_usd - self._cost_spent, 0.0)
        exhausted = (
            bool(self.cost_budget_usd) and self._cost_spent >= self.cost_budget_usd
        )
        if exhausted:
            warning_msg = COST_BUDGET_EXHAUSTED_TEMPLATE.format(
                cost_budget=self.cost_budget_usd,
                cost_spent=self._cost_spent,
            )
        else:
            warning_msg = COST_BUDGET_WARNING_TEMPLATE.format(
                cost_remaining=cost_remaining,
                cost_budget=self.cost_budget_usd,
            )
        return exhausted, warning_msg, cost_remaining

    @property
    def _turn_countdown_active(self) -> bool:
        """Whether to inject the "N step(s) remaining" turn countdown.

        Only with a finite step cap: uncapped (max_steps == 0) has no budget to
        count down, so `max_steps - step` would go negative. The run() loop gate
        already suppresses the countdown when it is the sole trigger, but a
        co-active token/cost budget can still call _inject_step_warning while
        uncapped — so the compose sites gate on this too. Mirrors the Stirrup
        agent's max_turns != 0 guard.
        """
        return self.turn_warnings_enabled and self.max_steps != 0

    def _inject_step_warning(self, step: int) -> bool:
        """Inject the per-step turn/token/cost budget warning as a user message.

        `turn_warnings_enabled` is orthogonal to `accounting_mode` — it may be
        combined with any mode, including cost_accounting. Only called when
        turn warnings, a token budget, or a cost budget (in cost_accounting
        mode) are enabled. Returns True once the active budget is exhausted,
        granting one final step to answer; turn_warnings_enabled alone never
        triggers exhaustion.
        """
        remaining = self.max_steps - step
        if self.cost_accounting_active:
            if not self.cost_budget_usd:
                # cost_accounting active but uncapped (log-only): the cost
                # clause has nothing meaningful to say ("$0.0000 of $0.0000
                # remaining" is nonsense), so only the turn-count reminder
                # applies here. Reaching this branch at all implies
                # turn_warnings_enabled is True — that's the only way the
                # run() loop's gate could have fired for this combination.
                warning_msg = TURN_WARNING_TEMPLATE.format(remaining=remaining)
                self.messages.append(
                    LitellmOutputMessage(role="user", content=warning_msg)
                )
                logger.bind(
                    message_type="turn_warning",
                    step=step + 1,
                    remaining_turns=remaining,
                ).info(warning_msg)
                return False
            exhausted, cost_msg, cost_remaining = self._cost_budget_warning()
            if exhausted:
                warning_msg = cost_msg
            else:
                parts: list[str] = []
                if self._turn_countdown_active:
                    parts.append(TURN_WARNING_TEMPLATE.format(remaining=remaining))
                parts.append(cost_msg)
                warning_msg = " ".join(parts)
            self.messages.append(LitellmOutputMessage(role="user", content=warning_msg))
            logger.bind(
                message_type="turn_warning",
                step=step + 1,
                remaining_turns=remaining,
                remaining_cost_usd=cost_remaining,
            ).info(warning_msg)
            return exhausted

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
            if self._turn_countdown_active:
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

    def _budget_config_error(self) -> str | None:
        """Validate the budget/credit knobs; return an error string or None."""
        if self.max_steps == 0 and not self.complete_on_budget_exhausted:
            return (
                "max_steps=0 (uncapped) requires complete_on_budget_exhausted=True: "
                "with no step cap the wall clock is the only harness-imposed stop, "
                "and without complete_on_budget_exhausted a wall-clock stop is an "
                "ungraded ERROR — the deliverable would be discarded."
            )
        if self.llm_idle_credit_sec > 0:
            if not self.stream:
                return (
                    "llm_idle_credit_sec > 0 requires stream=True: time-to-first-token "
                    "idle is unmeasurable on a non-streaming call."
                )
            if not self.complete_on_budget_exhausted:
                return (
                    "llm_idle_credit_sec > 0 requires complete_on_budget_exhausted=True: "
                    "a credit against a wall budget whose expiry is discarded (ERROR) "
                    "protects nothing."
                )
        if self.complete_on_budget_exhausted:
            # See StirrupAgent._budget_config_error: oversized call timeouts drive
            # the hard backstop (_hard_backstop_timeout) to/under the wall budget,
            # so the run completes immediately as wall_budget_hard without work.
            grace = float(self.llm_response_timeout + self.tool_call_timeout)
            agent_timeout = float(get_settings().AGENT_TIMEOUT_SECONDS)
            if float(self.timeout) + grace >= agent_timeout:
                return (
                    f"llm_response_timeout + tool_call_timeout ({grace:.0f}s) plus the "
                    f"wall budget ({self.timeout}s) must stay below "
                    f"AGENT_TIMEOUT_SECONDS ({agent_timeout:.0f}s): otherwise the hard "
                    "backstop is clamped to or below the wall budget and the run "
                    "completes immediately as wall_budget_hard without doing work."
                )
        return None

    def _wall_budget_remaining(self) -> float:
        """Seconds left on the wall budget; start_time is the credited clock."""
        if self.start_time is None:
            return float(self.timeout)
        return float(self.timeout) - (time.time() - self.start_time)

    def _hard_backstop_timeout(self) -> float:
        """asyncio.timeout wrapping the run. See StirrupAgent._hard_backstop_timeout."""
        if not self.complete_on_budget_exhausted:
            return float(self.timeout)
        grace = float(self.llm_response_timeout + self.tool_call_timeout)
        wanted = float(self.timeout) + float(self.llm_idle_credit_sec) + grace
        ceiling = float(get_settings().AGENT_TIMEOUT_SECONDS) - grace
        backstop = min(wanted, ceiling)
        if backstop < wanted:
            logger.warning(
                f"Hard backstop {wanted:.0f}s exceeds the runner ceiling; clamping "
                f"to {backstop:.0f}s (AGENT_TIMEOUT_SECONDS margin)"
            )
        return backstop

    def _apply_idle_credit(self) -> None:
        """Credit this step's TTFT idle back against the wall budget.

        See StirrupAgent._apply_idle_credit — same contract.
        """
        if self.llm_idle_credit_sec <= 0:
            return
        idle, calls = self._idle_accumulator.drain()
        if calls == 0:
            return
        self._idle_credit_measured += idle
        self._idle_credit_calls += calls
        credited = min(idle, self._idle_credit_remaining)
        if credited <= 0:
            return
        self._idle_credit_remaining -= credited
        self._idle_credit_granted += credited
        if self.start_time is not None:
            self.start_time += credited

    def _build_output(self) -> AgentTrajectoryOutput:
        usage = self._usage_tracker.to_dict()
        usage["accounting_mode"] = self.accounting_mode
        # token_budget/tokens_spent are recorded only when budgeting was on.
        if self.token_budget:
            usage["token_budget"] = self.token_budget
            usage["tokens_spent"] = self._tokens_spent()
        # cost_* fields are recorded only in cost_accounting mode; always
        # included together in that mode, regardless of whether a hard cap
        # (cost_budget_usd) was also set — cost is never silently unlogged.
        if self.cost_accounting_active:
            usage["cost_rate_overrides"] = self.rate_overrides
            usage["cost_usd_spent"] = self._cost_spent
            usage["cost_budget_usd"] = self.cost_budget_usd
            usage["cost_unpriced_calls"] = self._cost_unpriced_calls
        # idle_credit is recorded whenever the knob is set.
        if self.llm_idle_credit_sec > 0:
            usage["idle_credit"] = {
                "cap_sec": self.llm_idle_credit_sec,
                "granted_sec": self._idle_credit_granted,
                "measured_sec": self._idle_credit_measured,
                "calls": self._idle_credit_calls,
            }
        # A budget-limited attempt is graded (COMPLETED) but did not finish, so
        # carry the markers (same names as the Stirrup agent and Harbor path).
        output = (
            {
                "finish_reason": None,
                # None (not []): a budget-cut run made no submission decision, so
                # it declares no deliverable contract — GDPval's
                # declared_deliverable_paths then grades every on-disk file,
                # whereas [] would misread as abandon_task's "submitted nothing".
                "finish_paths": None,
                "abandoned": False,
                "budget_limited_attempt": True,
                "attempt_outcome": self._attempt_outcome,
            }
            if self._budget_limited_attempt
            else None
        )
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            output=output,
            status=AgentStatus(self.status),
            # Raw wall, never the credited budget clock.
            time_elapsed=time.time() - self._raw_start_time
            if self._raw_start_time
            else 0,
            usage=usage,
        )

    async def run(self) -> AgentTrajectoryOutput:
        """Run the agent loop until completion or timeout."""
        config_error = self._budget_config_error()
        if config_error is not None:
            self.start_time = self.start_time or time.time()
            self._raw_start_time = self._raw_start_time or self.start_time
            logger.error(f"Invalid agent config: {config_error}")
            self.status = AgentStatus.ERROR
            return self._build_output()

        backstop = self._hard_backstop_timeout()
        try:
            async with asyncio.timeout(backstop):
                with logger.contextualize(model=self.model):
                    logger.bind(message_type="configure").info(
                        f"Starting agent loop with model {self.model}"
                    )

                    await self._initialize_tools()
                    if self.stage_files_to_env:
                        try:
                            self.messages = await stage_message_files(
                                self.messages,
                                mcp_gateway_url=self._mcp_gateway_url,
                                auth_token=self._mcp_gateway_auth_token,
                            )
                        except TurnFileStagingError as exc:
                            logger.error(f"Attached file staging failed: {exc}")
                            # An unreachable or failing sandbox is infra, not a bad attachment.
                            self.status = (
                                AgentStatus.ERROR
                                if isinstance(exc.__cause__, SandboxUploadError)
                                else AgentStatus.FAILED
                            )
                            return self._build_output()

                    logger.bind(message_type="configure").info(
                        "\n".join(
                            f"{m['role'].capitalize()}: {m.get('content')}"
                            for m in self.messages
                        )
                    )

                    logger.info("Starting agent loop")
                    self.start_time = time.time()
                    self._raw_start_time = self.start_time
                    self.status = AgentStatus.RUNNING
                    if self.llm_idle_credit_sec > 0:
                        self._idle_credit_token = llm_idle_credit_ctx.set(
                            self._idle_accumulator
                        )

                    # max_steps == 0 means uncapped: the wall clock (checked
                    # below) is the only harness-imposed stop.
                    step_iter = (
                        itertools.count()
                        if self.max_steps == 0
                        else range(self.max_steps)
                    )
                    budget_final_turn_taken = False
                    for i in step_iter:
                        if self._finalized:
                            logger.info(f"Agent loop was finalized after {i + 1} steps")
                            break
                        if budget_final_turn_taken:
                            break
                        if self._abort_reason is not None:
                            break
                        # Graceful wall-budget stop: grade the deliverable on
                        # disk instead of discarding it as an ERROR.
                        if (
                            self.complete_on_budget_exhausted
                            and self._wall_budget_remaining() <= 0
                        ):
                            logger.bind(
                                fault=budget_exhausted(Budget.WALL_CLOCK, self.timeout)
                            ).info(
                                f"Wall budget of {self.timeout}s exhausted after {i} "
                                "steps; completing so the deliverable on disk is graded"
                            )
                            self._budget_limited_attempt = True
                            self._attempt_outcome = "wall_budget"
                            break
                        # Per-step warnings are opt-in: with neither turn
                        # warnings nor a token_budget/cost_budget_usd configured
                        # the loop runs exactly as before (no injected user
                        # messages). Turn-count warnings are skipped when uncapped
                        # (there is no step budget to count down).
                        if (
                            self._turn_countdown_active
                            or self.token_budget
                            or (self.cost_accounting_active and self.cost_budget_usd)
                        ):
                            budget_final_turn_taken = self._inject_step_warning(i)
                            # Publish it so step() can see that this turn is
                            # the last one the loop will run.
                            self._budget_final_turn = budget_final_turn_taken
                        logger.bind(message_type="step").info(f"Starting step {i + 1}")
                        await self.step()
                        self._apply_idle_credit()

                    if not self._finalized:
                        if self._budget_limited_attempt:
                            # Graceful wall-budget stop already flagged; grade it.
                            self.status = AgentStatus.COMPLETED
                        elif self._abort_reason is not None:
                            logger.bind(fault=empty_response()).error(
                                f"Agent loop abandoned: {self._abort_reason}"
                            )
                            self.status = AgentStatus.FAILED
                        # Accounting-mode exhaustion stays FAILED even in complete
                        # mode (that flag is about the wall clock and step cap).
                        elif budget_final_turn_taken and self.cost_accounting_active:
                            logger.bind(
                                fault=budget_exhausted(
                                    Budget.COST, self.cost_budget_usd
                                )
                            ).error(
                                f"Agent loop not finalized after exhausting cost "
                                f"budget of ${self.cost_budget_usd:.4f}"
                            )
                            self.status = AgentStatus.FAILED
                        elif budget_final_turn_taken:
                            logger.bind(
                                fault=budget_exhausted(Budget.TOKENS, self.token_budget)
                            ).error(
                                f"Agent loop not finalized after exhausting token "
                                f"budget of {self.token_budget}"
                            )
                            self.status = AgentStatus.FAILED
                        elif self.complete_on_budget_exhausted:
                            # Step cap reached without finish, in complete mode:
                            # grade the deliverable on disk.
                            logger.info(
                                f"Max steps ({self.max_steps}) reached; completing so "
                                "the deliverable on disk is graded "
                                "(complete_on_budget_exhausted=True)"
                            )
                            self._budget_limited_attempt = True
                            self._attempt_outcome = "max_steps"
                            self.status = AgentStatus.COMPLETED
                        else:
                            logger.bind(
                                fault=budget_exhausted(Budget.STEPS, self.max_steps)
                            ).error(
                                f"Agent loop was not finalized after {self.max_steps} steps"
                            )
                            self.status = AgentStatus.FAILED
                    else:
                        self.status = AgentStatus.COMPLETED

                    return self._build_output()

        except TimeoutError:
            # start_time is unset until the model loop starts; nothing is graded before that.
            if self.complete_on_budget_exhausted and self.start_time is not None:
                logger.bind(
                    fault=budget_exhausted(Budget.WALL_CLOCK, self.timeout)
                ).info(
                    f"Hard backstop ({backstop:.0f}s) reached mid-step; completing "
                    "so the deliverable on disk is graded"
                )
                self._budget_limited_attempt = True
                self._attempt_outcome = "wall_budget_hard"
                self.status = AgentStatus.COMPLETED
                return self._build_output()
            logger.bind(fault=budget_exhausted(Budget.WALL_CLOCK, self.timeout)).error(
                f"Agent run timed out after {self.timeout} seconds"
            )
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

        finally:
            # Reset the TTFT accumulator out of the ContextVar so its reference
            # does not outlive this run into a reused context. No-op when the
            # credit is off (token stays None).
            if self._idle_credit_token is not None:
                llm_idle_credit_ctx.reset(self._idle_credit_token)
                self._idle_credit_token = None


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
