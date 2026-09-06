"""
Loop Truncated Tools Agent implementation.

This is a variant of the loop agent that truncates tool outputs to prevent
context window overflow from large tool responses.
"""

import asyncio
import time
from typing import Any, cast

from fastmcp import Client as FastMCPClient
from litellm import Choices
from litellm.exceptions import ContextWindowExceededError, Timeout
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
from runner.agents.responses_agent.conversion import (
    convert_messages_for_responses_api,
    extract_reasoning_summary,
    to_responses_tool,
)
from runner.agents.responses_agent.main import (
    parse_responses_api_output,
    responses_output_to_message,
)
from runner.utils.context_budget import (
    DEFAULT_TRIM_FLOOR,
    PromptTokenProjector,
    count_tokens,
    resolve_max_input_tokens,
    trim_to_budget,
)
from runner.utils.error import is_fatal_mcp_error, is_system_error
from runner.utils.llm import (
    _is_context_window_error,  # pyright: ignore[reportPrivateUsage]
    call_responses_api,
    compute_call_cost_usd,
    generate_response,
)
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

# Appended as a user message immediately after a context trim fires, so a
# degraded answer is explainable and the model knows to re-fetch rather than
# totalling a view it can no longer see. Silent tail-drop is the failure mode
# where an agent answers from a partial history believing it complete.
CONTEXT_TRIM_NOTICE_TEMPLATE = (
    "Note: {dropped_turns} earlier exchange(s) ({dropped_messages} message(s), "
    "including their tool results) were removed from this conversation to keep "
    "it within the model's input limit. That content is no longer visible to "
    "you. Do not treat anything you can no longer see as complete — if you "
    "need that data, fetch it again, and prefer narrower queries from here on."
)

# Substituted for a tool result that would blow this step's tool-result
# budget. Deliberately a refusal with a preview rather than a quietly
# shortened payload: an agent handed a silently truncated result totals it
# and reports the total as if it were complete.
STEP_TOOL_BUDGET_REFUSAL_TEMPLATE = (
    "Error: this result was NOT delivered. It is ~{tokens} token(s) "
    "({chars} characters) and would exceed this step's tool-result budget "
    "({remaining} of {budget} token(s) left; the earlier calls in this "
    "parallel batch used the rest). None of it is in your context — the "
    "preview below is not partial data, do not total it or draw conclusions "
    "from it. Re-run this call with a narrower query, a smaller page size, "
    "or fewer records, and consider making fewer calls per turn."
    "\n\nFirst {preview_chars} characters of the discarded result:\n{preview}"
)

# How much of a refused result to show back, purely so the model can tell what
# it asked for. Small enough that a whole batch of refusals cannot itself
# become the overflow.
_REFUSAL_PREVIEW_CHARS = 400

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


def _coerce_fraction(
    value: Any, *, default: float, minimum: float, maximum: float
) -> float:
    """Clamped float coercion for a config value expressed as a fraction.

    Same defensive philosophy as `_coerce_bool`: missing/None/garbage all
    resolve to `default`, and an out-of-range number is clamped rather than
    honoured — a typo'd utilization must never trim the whole history away.
    """
    try:
        coerced = float(value)  # pyright: ignore[reportAny]
    except (TypeError, ValueError):
        return default
    if coerced != coerced:  # NaN
        return default
    return min(max(coerced, minimum), maximum)


def _truncate_output(content: str, max_lines: int = 200, max_chars: int = 32768) -> str:
    """Truncate content at whichever limit is hit first: max_lines or max_chars."""
    if not content:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    # Check if we need to truncate by lines
    if total_lines > max_lines:
        truncated_content = "\n".join(lines[:max_lines])
        # Also check char limit on the line-truncated content
        if len(truncated_content) > max_chars:
            truncated_content = truncated_content[:max_chars]
            kept_lines = truncated_content.count("\n") + 1
            hidden_lines = total_lines - kept_lines
        else:
            hidden_lines = total_lines - max_lines
        return truncated_content + f"\n... (truncated {hidden_lines} lines) ..."

    # Check if we need to truncate by chars
    if len(content) > max_chars:
        truncated_content = content[:max_chars]
        kept_lines = truncated_content.count("\n") + 1
        hidden_lines = total_lines - kept_lines

        if hidden_lines > 0:
            suffix = f"\n... (truncated {hidden_lines} lines) ..."
        else:
            hidden_chars = len(content) - len(truncated_content)
            suffix = f"\n... (truncated {hidden_chars} chars) ..."

        return truncated_content + suffix

    return content


def finalize_answer(final_answer: str | None = None) -> str | None:
    logger.bind(message_type="final_answer").info(final_answer)
    return final_answer


class LoopTruncatedToolsAgent:
    """
    A loop-based agent that truncates tool outputs to prevent context overflow.

    Same as LoopAgent but applies _truncate_output() to all tool responses.
    """

    def __init__(self, run_input: AgentRunInput):
        self.trajectory_id: str = run_input.trajectory_id
        self.model: str = run_input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(run_input.initial_messages)

        if run_input.mcp_gateway_url is None:
            raise ValueError(
                "MCP gateway URL is required for loop truncated tools agent"
            )

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

        # Truncation config. max_output_lines is retired from the registry for
        # this agent (see the note there) but still read, so instances with a
        # stored value keep loading and behave bit-identically. It is
        # structurally unreachable on single-line JSON tool results; the char
        # cap is what actually bounds output here.
        self.max_output_lines: int = config.get("max_output_lines", 200)
        self.max_output_chars: int = config.get("max_output_chars", 32768)

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

        # Route through the Responses API (litellm.aresponses) when the
        # orchestrator model carries the "responses/" prefix — mirrors
        # stirrup_agent / single_shot_multimodal. This lets GPT-5.6 reasoning
        # params (reasoning.mode/effort/context/summary), supplied via
        # extra_args as {"extra_body": {"reasoning": {...}}}, reach the model
        # raw instead of being dropped by litellm's Chat Completions param
        # mapping. No prefix -> unchanged Chat Completions path (default).
        self._use_responses: bool = "responses/" in self.model
        self._api_model: str = self.model.replace("responses/", "")

        # ── Context-window management (BELLA-616) ───────────────────────
        # Fraction of the model's input window above which the oldest turns
        # are dropped before the request is dispatched. 0.0 (the default)
        # disables the whole mechanism — including the
        # ContextWindowExceededError recovery in step() — so agent instances
        # configured before this field existed behave exactly as before.
        self.context_trim_utilization: float = _coerce_fraction(
            config.get("context_trim_utilization"),
            default=0.0,
            minimum=0.0,
            maximum=0.95,
        )
        # Once trimming fires, trim down to this fraction rather than merely
        # back under the limit — see DEFAULT_TRIM_FLOOR on why (prompt cache).
        self.context_trim_floor: float = _coerce_fraction(
            config.get("context_trim_floor"),
            default=DEFAULT_TRIM_FLOOR,
            minimum=0.2,
            maximum=0.9,
        )
        # Calibrated tokens of tool results a single step's tool-call batch may
        # deliver. 0 (the default) disables. Covers the one case the trim
        # cannot: tail-drop refuses to drop the newest turn, so a step whose
        # own results overflow the window leaves the trim over budget and the
        # request is rejected anyway.
        try:
            self.max_step_tool_result_tokens: int = max(
                int(config.get("max_step_tool_result_tokens") or 0), 0
            )
        except (TypeError, ValueError):
            self.max_step_tool_result_tokens = 0
        # The system prompt + original task prompt arrive as initial_messages
        # and must survive every trim.
        self._anchor_count: int = len(run_input.initial_messages)
        self._max_input_tokens: int = resolve_max_input_tokens(self._api_model)
        # Converts litellm's (Anthropic-blind, tool-schema-blind) estimate into
        # a projection of what the provider will actually bill, calibrated from
        # the provider's own prompt_tokens as the run proceeds.
        self._projector: PromptTokenProjector = PromptTokenProjector(self._api_model)
        # Raw estimate of the history handed to the current in-flight call,
        # held so the response's reported prompt_tokens can be paired with it.
        self._pending_prompt_estimate: int | None = None

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

    def _truncate_tool_message(self, msg: LitellmAnyMessage) -> LitellmAnyMessage:
        """Apply truncation to a tool message's content (dict-style only)."""
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            return msg

        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = _truncate_output(
                content, self.max_output_lines, self.max_output_chars
            )
        elif isinstance(content, list):
            truncated_parts: list[dict[str, str]] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    truncated_text = _truncate_output(
                        str(part.get("text", "")),
                        self.max_output_lines,
                        self.max_output_chars,
                    )
                    truncated_parts.append({"type": "text", "text": truncated_text})
                else:
                    truncated_parts.append(part)  # pyright: ignore[reportArgumentType]
            msg["content"] = truncated_parts  # pyright: ignore[reportGeneralTypeIssues]
        return msg

    # ── Context-window management ───────────────────────────────────

    def _context_management_enabled(self) -> bool:
        """Whether pre-dispatch trimming (and its overflow recovery) is on."""
        return self.context_trim_utilization > 0.0

    def _trim_limit(self) -> int:
        """Projected billed prompt tokens above which trimming fires."""
        return int(self._max_input_tokens * self.context_trim_utilization)

    def _trim_target(self) -> int:
        """Projected billed prompt tokens a trim brings the history down to."""
        return min(
            int(self._max_input_tokens * self.context_trim_floor), self._trim_limit()
        )

    def _prepare_context(self) -> None:
        """Trim the history if needed, then record what is about to be sent.

        Runs before every dispatch, which is what makes the single-step
        cliffs survivable: a step that added 150k tokens is seen here, before
        the request that would have been rejected for it. No-op unless
        `context_trim_utilization` is set.
        """
        if not self._context_management_enabled():
            return
        estimate = count_tokens(self._api_model, self.messages)
        limit = self._trim_limit()
        projected = self._projector.project(estimate)
        if projected > limit:
            estimate, _ = self._trim_context(limit=limit, reason="pre_dispatch")
        self._pending_prompt_estimate = estimate

    def _trim_context(
        self, *, limit: int, reason: str, force: bool = False
    ) -> tuple[int, int]:
        """Drop oldest turns until the projected prompt is under the floor.

        Budgets are expressed in projected (billed) tokens but measurement
        happens in raw estimates, so the limits are inverted through the
        projector rather than the counts being projected — the projection is
        affine, and projecting per-turn counts would charge its offset once
        per turn.

        Returns ``(raw estimate of the resulting history, turns dropped)``.
        Zero turns dropped means the trim freed nothing — the caller has to
        decide whether that is survivable, and on the recovery path it is not.
        """
        target = self._trim_target()
        result = trim_to_budget(
            self.messages,
            anchor_count=self._anchor_count,
            limit=self._projector.invert(limit),
            floor=self._projector.invert(target),
            count=lambda msgs: count_tokens(self._api_model, msgs),
            force=force,
        )
        log = logger.bind(
            message_type="compaction",
            step=self.current_step,
            reason=reason,
            projected_prompt_tokens=self._projector.project(result.tokens_before),
            projected_limit=limit,
            calibration_ratio=round(self._projector.ratio, 3),
            calibrated=self._projector.calibrated,
        )
        if not result.trimmed:
            log.warning(
                "Context over budget but nothing droppable: the anchored "
                "messages plus the newest turn already exceed the limit "
                f"(projected {self._projector.project(result.tokens_before)} "
                f"> {limit} tokens)"
            )
            return result.tokens_after, 0
        self.messages = result.messages
        self._usage_tracker.track_compaction()
        self.messages.append(
            LitellmOutputMessage(
                role="user",
                content=CONTEXT_TRIM_NOTICE_TEMPLATE.format(
                    dropped_turns=result.dropped_turns,
                    dropped_messages=result.dropped_messages,
                ),
            )
        )
        log.bind(
            dropped_turns=result.dropped_turns,
            dropped_messages=result.dropped_messages,
        ).warning(
            f"Context trim ({reason}): dropped {result.dropped_turns} oldest "
            f"assistant turn(s) / {result.dropped_messages} message(s); "
            f"projected prompt {self._projector.project(result.tokens_before)} "
            f"-> {self._projector.project(result.tokens_after)} tokens "
            f"(limit {limit}, target {target})"
        )
        return count_tokens(self._api_model, self.messages), result.dropped_turns

    def _observe_prompt_tokens(self, calls_before: int) -> None:
        """Feed the provider's reported prompt_tokens back into the projector.

        This is the only source of truth about token density: litellm's
        estimator under-counts Anthropic by 32-47% and never sees the tool
        schemas at all.
        """
        estimate = self._pending_prompt_estimate
        self._pending_prompt_estimate = None
        if estimate is None:
            return
        call_log = self._usage_tracker.call_log
        if len(call_log) <= calls_before or not call_log:
            return
        reported = call_log[-1].get("prompt_tokens", 0)
        if reported <= 0:
            return
        self._projector.observe(estimate, reported)

    def _recover_from_context_overflow(self, exc: Exception) -> bool:
        """Trim and continue instead of ending the run on a 400.

        The retry decorator in `runner.utils.llm` deliberately refuses to
        retry this error — resending identical bytes can never clear it — so
        recovery has to *change* the bytes, which only the harness can do.
        Returning from step() without an assistant message leaves the history
        as it was minus the dropped turns, and run()'s loop retries on the
        next iteration: the 400 costs a step, not the run.

        Returns True only when the trim actually dropped something. False
        means the anchored messages plus the newest turn are themselves over
        the limit, so the retry would carry the byte-identical prompt the
        provider just refused — the caller must re-raise rather than spend
        the remaining `max_steps` collecting the same 400 over and over
        (BELLA-615's resend bug, in a new place).
        """
        estimate = self._pending_prompt_estimate
        self._pending_prompt_estimate = None
        logger.bind(
            message_type="compaction",
            step=self.current_step,
            reason="context_window_exceeded",
        ).warning(f"Context window exceeded; trimming history and retrying: {exc!r}")
        if estimate is not None:
            # The provider just contradicted our projection for this exact
            # history. Recalibrate before deciding how much to drop, or the
            # trim inherits the same under-estimate that let it through.
            self._projector.observe_overflow(estimate, self._max_input_tokens)
        _, dropped_turns = self._trim_context(
            limit=self._trim_target(),
            reason="context_window_exceeded",
            force=True,
        )
        if not dropped_turns:
            logger.bind(
                message_type="compaction",
                step=self.current_step,
                reason="context_window_unrecoverable",
            ).error(
                "Context window exceeded and the trim freed nothing: the "
                "anchored messages plus the newest turn are already over the "
                "limit. The next request would be byte-identical to the one "
                "just refused, so this fails now rather than repeating the "
                f"same 400 for every remaining step: {exc!r}"
            )
        return bool(dropped_turns)

    # ── Per-step tool-result budget ─────────────────────────────────

    def _step_tool_result_budget(self) -> int | None:
        """Calibrated tokens of tool results this step's batch may deliver.

        ``None`` means unlimited — the mechanism is off. A budget of ``0`` is
        a real budget with nothing left in it, and refuses the whole batch:
        the history is already at the trim limit and the newest turn is the
        one thing tail-drop will not drop, so delivering here produces a
        request that cannot be repaired by trimming at all.

        When context management is on, the configured cap is clamped to the
        headroom actually left, measured from the provider's own reported
        prompt size for the request we just sent — the exact number these
        results are about to be piled on top of.
        """
        if self.max_step_tool_result_tokens <= 0:
            return None
        budget = self.max_step_tool_result_tokens
        if self._context_management_enabled():
            call_log = self._usage_tracker.call_log
            reported = call_log[-1].get("prompt_tokens", 0) if call_log else 0
            if reported > 0:
                budget = min(budget, max(self._trim_limit() - reported, 0))
        return budget

    def _refuse_over_budget_result(
        self, msg: LitellmAnyMessage, *, budget: int, used: int, tokens: int
    ) -> LitellmAnyMessage:
        """Replace an over-budget tool result with a refusal + short preview.

        The message keeps its role/tool_call_id/name so the ``tool_use`` it
        answers still has a ``tool_result`` — dropping it outright would be
        rejected by the provider.
        """
        if not isinstance(msg, dict):
            return msg
        content: Any = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text", ""))
                for part in content  # pyright: ignore[reportUnknownVariableType]
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = ""
        refusal = STEP_TOOL_BUDGET_REFUSAL_TEMPLATE.format(
            tokens=tokens,
            chars=len(text),
            remaining=max(budget - used, 0),
            budget=budget,
            preview_chars=_REFUSAL_PREVIEW_CHARS,
            preview=text[:_REFUSAL_PREVIEW_CHARS],
        )
        msg["content"] = [{"type": "text", "text": refusal}]  # pyright: ignore[reportGeneralTypeIssues]
        return msg

    async def _get_response(self) -> LitellmOutputMessage | None:
        """Get one assistant reply via the Responses API or Chat Completions.

        Returns None when the reply was empty/invalid so the caller can nudge
        the model to continue and retry.
        """
        if self._use_responses:
            return await self._get_responses_reply()

        response: ModelResponse = await generate_response(
            self._api_model,
            self.messages,
            self.tools,
            self.llm_response_timeout,
            self.extra_args,
            trajectory_id=self.trajectory_id,
        )
        self._usage_tracker.track(response)
        if self.cost_accounting_active:
            self._track_step_cost(response)
        logger.debug(f"Response: {response}")

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            return None
        return LitellmOutputMessage.model_validate(choices[0].message)

    async def _get_responses_reply(self) -> LitellmOutputMessage | None:
        """Call the OpenAI Responses API and map the output back to a Chat
        Completions-style assistant message so the shared tool loop below is
        unchanged. Returns None on empty/unusable output (no tool calls and no
        text) so step() nudges the model to continue — parity with the Chat
        Completions branch.

        Reasoning params travel in ``self.extra_args`` as
        ``{"extra_body": {"reasoning": {...}}}``; ``call_responses_api``
        forwards ``extra_body`` raw to ``litellm.aresponses``, so GPT-5.6 fields
        (mode/effort/context/summary) reach the model without being stripped by
        litellm's param mapping.
        """
        responses_input = convert_messages_for_responses_api(self.messages)
        responses_tools = [to_responses_tool(t) for t in self.tools]
        response = await call_responses_api(
            self._api_model,
            cast(list[LitellmAnyMessage], responses_input),
            responses_tools,
            self.llm_response_timeout,
            self.extra_args,
            trajectory_id=self.trajectory_id,
        )
        self._usage_tracker.track_from_dict(
            response.model_dump() if hasattr(response, "model_dump") else dict(response)
        )
        if self.cost_accounting_active:
            self._track_step_cost(response)
        parsed = parse_responses_api_output(response)
        # Prefer the summary-aware extraction (handles the summary_text path that
        # parse_responses_api_output misses). Mirrors react_toolbelt_responses.
        summary = extract_reasoning_summary(response)
        if summary:
            parsed.reasoning_content = summary
        message = responses_output_to_message(parsed)
        if getattr(message, "content", None) is None:
            message.content = ""
        # Truly-empty reply (no tool calls, no text, no reasoning) -> let step()
        # nudge + retry. A reasoning-only turn is NOT empty: keep it so its
        # reasoning is preserved and step() handles it (parity with the Chat
        # Completions branch, which returns the parsed message).
        if (
            not getattr(message, "tool_calls", None)
            and not (message.content or "").strip()
            and not getattr(message, "reasoning_content", None)
        ):
            return None
        return message

    async def step(self):
        """Execute a single step of the agent loop."""
        self.current_step += 1

        # Compact before the LLM call so the request stays under the model's
        # input-token limit. Long tool-heavy trajectories accumulate results +
        # thinking until the window is exhausted, and a single step's parallel
        # tool batch can add 150k tokens on its own.
        self._prepare_context()

        calls_before = len(self._usage_tracker.call_log)
        try:
            response_message = await self._get_response()
        except ContextWindowExceededError as e:
            if not self._context_management_enabled():
                logger.bind(message_type="response").error(
                    f"Error generating response: {repr(e)}"
                )
                raise
            # Recovery that freed nothing is not recovery: returning here would
            # resend the identical prompt next iteration and collect the
            # identical 400 until max_steps, turning a fast failure into a slow
            # one. Re-raise instead — run() records it exactly as it would with
            # trimming disabled.
            if not self._recover_from_context_overflow(e):
                raise
            return
        except Timeout:
            logger.bind(message_type="response").error(
                "Response timed out, continuing with next step"
            )
            return
        except Exception as e:
            # Some providers (notably Gemini) return context overflow as a
            # plain BadRequestError instead of ContextWindowExceededError.
            if self._context_management_enabled() and _is_context_window_error(e):
                if self._recover_from_context_overflow(e):
                    return
                raise
            logger.bind(message_type="response").error(
                f"Error generating response: {repr(e)}"
            )
            raise e
        self._observe_prompt_tokens(calls_before)

        if response_message is None:
            logger.bind(message_type="step").warning(
                "LLM returned invalid/empty output, prompting to continue"
            )
            self.messages.append(
                LitellmOutputMessage(
                    role="user",
                    content="continue",
                )
            )
            return

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
            pre_tool_len = len(self.messages)
            fatal_exc: Exception | None = None
            deferred_image_messages: list[LitellmInputMessage] = []
            # Priced once per batch, before any result comes back: a 48-call
            # parallel batch can add 150k tokens between one pre-dispatch trim
            # and the next, which is more than the trim can repair on its own.
            step_result_budget = self._step_tool_result_budget()
            step_result_tokens = 0
            step_refusals = 0
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

                    # Apply truncation to tool result messages
                    truncated_messages = [
                        self._truncate_tool_message(msg) for msg in messages
                    ]

                    if step_result_budget is not None:
                        priced: list[LitellmAnyMessage] = []
                        for result_msg in truncated_messages:
                            cost = self._projector.scale(
                                count_tokens(self._api_model, [result_msg])
                            )
                            if step_result_tokens + cost > step_result_budget:
                                step_refusals += 1
                                tool_result_logger.bind(
                                    step=self.current_step,
                                    result_tokens=cost,
                                    step_result_tokens=step_result_tokens,
                                    step_result_budget=step_result_budget,
                                ).warning(
                                    f"Tool {name} result refused: ~{cost} token(s) "
                                    f"would exceed this step's tool-result budget "
                                    f"({step_result_tokens}/{step_result_budget} used)"
                                )
                                priced.append(
                                    self._refuse_over_budget_result(
                                        result_msg,
                                        budget=step_result_budget,
                                        used=step_result_tokens,
                                        tokens=cost,
                                    )
                                )
                            else:
                                step_result_tokens += cost
                                priced.append(result_msg)
                        truncated_messages = priced

                    tool_result_logger.bind(
                        payload=[result.model_dump() for result in call_result.content],
                    ).info(f"Tool {name} called successfully")

                    self.messages.extend(truncated_messages)
            if step_refusals:
                logger.bind(
                    message_type="step",
                    step=self.current_step,
                    step_refusals=step_refusals,
                    step_result_tokens=step_result_tokens,
                    step_result_budget=step_result_budget,
                ).warning(
                    f"{step_refusals} tool result(s) refused this step to stay "
                    f"within a {step_result_budget}-token tool-result budget"
                )
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

        Mirrors the loop agent: tool-result text comes from role=="tool" messages;
        tool-result images are elided before storage so their data-URIs must be
        counted live, wherever they land (embedded in the tool message or as
        deferred user messages). The text here is already truncated by
        ``_truncate_tool_message`` — i.e. what the model was actually billed for.
        No-op unless breakdown tracking is on. Messages may be dicts or pydantic
        models.
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

    def _track_step_cost(self, response: Any) -> None:
        """Compute and log this step's real $ cost (cost_accounting mode only).

        Always logs — cost_budget_usd=0 means "no cap", not "don't log".
        Called with either a Chat Completions `ModelResponse` (from
        `_get_response`) or a raw Responses-API response object (from
        `_get_responses_reply`) — `compute_call_cost_usd` reads either shape.

        A call with unreadable usage contributes $0 to _cost_spent (there's
        no token count to price), but is counted in _cost_unpriced_calls so a
        cost_budget_usd cap silently breached by unpriced calls is still
        explainable after the fact — real spend must never look like it was
        simply never incurred.

        Prices with `self._api_model` (the "responses/"-prefix-stripped
        string actually sent to the LLM/litellm), not `self.model` — the
        prefixed form can't be resolved by litellm's rate table and would
        silently fall back to default rates. Identical to `self.model` on
        the Chat Completions path, where the prefix is never present.
        """
        call_cost = compute_call_cost_usd(
            self._api_model, response, self.rate_overrides
        )
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
                if self.turn_warnings_enabled:
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
                        # warnings nor a token_budget/cost_budget_usd configured
                        # the loop runs exactly as before (no injected user
                        # messages).
                        if (
                            self.turn_warnings_enabled
                            or self.token_budget
                            or (self.cost_accounting_active and self.cost_budget_usd)
                        ):
                            budget_final_turn_taken = self._inject_step_warning(i)
                        logger.bind(message_type="step").info(f"Starting step {i + 1}")
                        await self.step()

                    if not self._finalized:
                        if budget_final_turn_taken and self.cost_accounting_active:
                            logger.error(
                                f"Agent loop not finalized after exhausting cost "
                                f"budget of ${self.cost_budget_usd:.4f}"
                            )
                        elif budget_final_turn_taken:
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
    Entry point for the loop truncated tools agent.

    Args:
        run_input: The input configuration for the agent run

    Returns:
        AgentTrajectoryOutput with status, messages, and metrics
    """
    agent = LoopTruncatedToolsAgent(run_input)
    return await agent.run()
