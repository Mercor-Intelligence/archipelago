"""
Stirrup Agent - GDPval-AA compatible agent

Replicates the Stirrup framework's behavior:
- configurable max_turns (default: 100, matching AA GDPval)
- turn warnings when remaining <= threshold (default: 80)
- context summarization at 70% threshold
- finish tool with (reason, paths) schema
- all MCP tools loaded directly from the gateway
- built-in tools: fetch_web_page, view_image
"""

import asyncio
import json
import re
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
    content_to_str,
    get_msg_attr,
)
from runner.agents.responses_agent_v2.main import (
    parse_responses_api_output,
    responses_output_to_message,
)
from runner.utils.error import is_fatal_mcp_error, is_system_error
from runner.utils.llm import (
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

from .builtin_tools import (
    BUILTIN_TOOLS,
    execute_fetch_web_page,
    parse_builtin_tool_args,
)
from .context import StirrupContextManager
from .prompts import build_system_prompt, build_turn_warning_message
from .tools import (
    ABANDON_TASK_TOOL,
    ABANDON_TASK_TOOL_NAME,
    FINISH_TOOL,
    FINISH_TOOL_NAME,
    parse_abandon_tool,
    parse_finish_tool,
)

# stirrup agent defaults
DEFAULT_MAX_TURNS = 100  # AA GDPval uses 100 turns
DEFAULT_TURNS_WARNING_THRESHOLD = 80  # AA GDPval warns starting at turn 80
# AA GDPval-AA v2 allows up to 250 turns; a run that actually uses them does not
# fit in the old 3h budget, so the wall-clock default moves with the ceiling.
# Well inside the runner's own asyncio.timeout(AGENT_TIMEOUT_SECONDS) of 22h30m
# (runner/main.py) and the Modal sandbox lifetime derived from it.
DEFAULT_TIMEOUT_SECONDS = 27000  # 7.5 hours
DEFAULT_CONTEXT_CUTOFF = 0.7
TOOL_ARGUMENT_PARSE_RECOVERY_PROMPT = (
    "The previous tool call could not be parsed by the model provider because "
    "its arguments were not valid JSON. Retry the step, and when calling shell "
    "tools make the command a valid JSON string: escape newlines as \\n and "
    "escape quotes/backslashes correctly. For long scripts, prefer writing the "
    "file in smaller chunks before running it."
)

# A turn that returns neither content nor tool calls is a provider-side miss,
# not a signal that the task is done: reasoning models (observed on Kimi K3 via
# the Fireworks MTP router) can spend a whole turn in `reasoning_content` and
# emit an empty completion. Telling that model to "use the finish tool" reads as
# an order to stop, and it obeys mid-task — one Project Balboa trajectory
# finished at step 19/100 with the deliverable unwritten and scored 0, its final
# answer opening "I was directed to finish before I could write the output file
# to disk." Nudge it to continue instead; every other agent in the runner
# already does ("...Please continue completing the task.").
# These three nudges are injected as user turns, so they also have to be stripped
# from delivered transcripts: rl-studio's `HARNESS_TURN_PATTERNS`
# (packages/islands/implementations/gdm/utils/trajectory_export.py) matches each
# on its OPENING sentence. Reword an opening and update that list in the same
# change, or the nudge ships to the customer -- which is what the last rewording
# of these prompts did, silently, until DEP-1019.
EMPTY_TURN_RECOVERY_PROMPT = (
    "Your last turn returned no content and no tool calls. That was an empty "
    "response, not a completed task. Continue working by calling a tool."
)
# A turn that produced prose but no tool call is different: the model tried to
# answer in the transcript, where `finish` is the only channel that counts.
NO_TOOL_CALLS_RECOVERY_PROMPT = (
    "No tools called. Text written outside a tool call is ignored. If the task "
    "is complete, call the finish tool with your final answer; otherwise "
    "continue working by calling a tool."
)
# Suggesting `finish` while most of the turn budget is unspent invites exactly
# the premature stop above, so early prose-only turns get a continue-only nudge.
NO_TOOL_CALLS_EARLY_RECOVERY_PROMPT = (
    "No tools called. Text written outside a tool call is ignored. Continue "
    "working by calling a tool."
)
# Fraction of max_turns that must be spent before a nudge may mention `finish`.
FINISH_NUDGE_MIN_TURN_FRACTION = 0.5
# Consecutive empty turns tolerated before the run is abandoned. A provider that
# will not emit tool calls is an infrastructure failure; failing loudly keeps it
# out of the scored population instead of banking a 0 against the model.
MAX_CONSECUTIVE_EMPTY_TURNS = 3

# MCP tools that return image content the model must be able to *see* to use.
# Hidden from known text-only models (below). Matched by name *suffix* because the
# gateway namespaces the tool (e.g. "stirrup_code_execution_read_image_file") — an
# exact-name match (the old disabled_tools="read_image_file" config) silently
# missed the namespaced name and hid nothing. `view_image` is the other
# image-returning tool the harness documents; it isn't a live gateway tool today
# (only read_image_file is observed), but it's covered here so the gate stays
# complete if the gateway ever exposes it.
IMAGE_INPUT_TOOL_SUFFIXES: tuple[str, ...] = ("read_image_file", "view_image")

# Models CONFIRMED text-only by observed "does not support image/multimodal" 400s.
# For these we proactively withhold the image tool and strip any image from their
# context — matching AA, whose View Image tool "is only exposed to models with
# vision support" — so they finish best-effort on the text instead of dying on a
# 400. Maintained by hand: when a new endpoint is seen erroring this way, add it.
# Safe to list because these are proven no-vision (cannot mis-blind a vision
# model); matched as lowercase substrings, precise enough to exclude vision
# siblings (kimi-k3, minimax-m3).
KNOWN_TEXT_ONLY_MODELS: frozenset[str] = frozenset(
    {
        "deepseek-v3.2",
        "deepseek-v4-pro",
        "glm-5.2",
        "qwen3p8-max",
        "kimi-k2-thinking",
        "minimax-m2.7",
        "nemotron-3-ultra",
    }
)


def _is_known_text_only(model: str) -> bool:
    """True if ``model`` is a confirmed text-only endpoint (proactive image gate).

    A token matches only when bounded by a non-alphanumeric char (or string edge)
    on each side, so it still matches inside a longer name via a separator
    (`nemotron-3-ultra` in `…/nemotron-3-ultra-nvfp4`) but NOT a run-on suffix — a
    `v`-suffixed vision sibling like `glm-5.2v` is not caught. Prevents silently
    blinding a vision model, which (with no reactive fallback) would be unrecoverable.
    """
    lowered = model.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered)
        for token in KNOWN_TEXT_ONLY_MODELS
    )


# Joined onto Stirrup's existing turn warning (space-separated) when a token
# budget is configured; stands alone on turns where the turn warning is silent.
TOKEN_BUDGET_WARNING_TEMPLATE = (
    "You have {tokens_remaining} of {token_budget} token(s) remaining "
    "in your total token budget."
)

# Injected alone (no turn clause) once the token budget is spent.
TOKEN_BUDGET_EXHAUSTED_TEMPLATE = (
    "Warning: your token budget of {token_budget} token(s) is exhausted "
    "({tokens_spent} token(s) spent). This is your final turn. Call the finish "
    "tool now with your complete final answer in the 'reason' parameter."
)

# Injected each turn in "cost_accounting" mode, once cost_budget_usd is set.
COST_BUDGET_WARNING_TEMPLATE = (
    "You have ${cost_remaining:.4f} of ${cost_budget:.4f} remaining "
    "in your total cost budget."
)

# Injected alone (no turn clause) once the cost budget is spent.
COST_BUDGET_EXHAUSTED_TEMPLATE = (
    "Warning: your cost budget of ${cost_budget:.4f} is exhausted "
    "(${cost_spent:.4f} spent). This is your final turn. Call the finish tool "
    "now with your complete final answer in the 'reason' parameter."
)

# Accounting modes: "default" (no budget mechanism active), "token_accounting"
# (token_budget), "cost_accounting" (real $ tracking via rate overrides +
# cost_budget_usd). Mutually exclusive. Orthogonal to Stirrup's own
# turns_remaining_warning_threshold turn warnings, which apply in every mode.
_ACCOUNTING_MODES = frozenset({"default", "token_accounting", "cost_accounting"})

# Per-token USD rate override keys read from agent config in cost_accounting mode.
_RATE_OVERRIDE_KEYS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cached_input_cost_per_token",
    "cache_creation_cost_per_token",
)


def _is_anthropic_tool_argument_parse_error(error: Exception) -> bool:
    message = str(error)
    return "Failed to parse tool call arguments" in message and "Anthropic" in message


def _incomplete_reason(payload: dict[str, Any]) -> str | None:
    """Responses-API stand-in for finish_reason: a cut-short generation is
    status="incomplete" plus incomplete_details.reason."""
    if payload.get("status") != "incomplete":
        return None
    details = payload.get("incomplete_details")
    return details.get("reason") if isinstance(details, dict) else None


def _repair_truncated_tool_arguments(message: Any) -> list[str]:
    """Reset unparsable tool-call ``arguments`` to ``{}``; return the affected ids.

    A model can emit a ``tool_call`` whose ``arguments`` string is cut off
    mid-value (``{"cmd": "cat <<'EOF'`` ...). The raw response is what gets stored
    in ``self.messages``, so that fragment is replayed on *every* subsequent
    request, and a provider that strictly parses the body then rejects the entire
    conversation rather than just the bad turn. Observed via OpenRouter->Tencent
    as ``forward bad request, [HTTP 400] ... Unterminated string starting at:
    line 1 column 9 (char 8)`` -- char 8 is where the first argument's value opens
    -- and from Anthropic as ``Failed to parse tool call arguments``.

    That makes the failure permanent, not transient: the offending bytes are in
    the history, so a retry re-sends them. Normalising to ``{}`` before the
    message is stored keeps the history valid JSON for the rest of the run. The
    caller reports the ids back as failed tool results, so the model retries the
    step deliberately instead of the tool executing with silently-empty
    arguments (which is what ``_execute_mcp_tool``'s ``args = {}`` fallback did --
    a shell tool invoked with no command).
    """
    broken: list[str] = []

    for tool_call in getattr(message, "tool_calls", None) or []:
        function = getattr(tool_call, "function", None)
        if function is None:
            continue
        raw = getattr(function, "arguments", None)
        if not raw:
            continue
        try:
            json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            broken.append(tool_call.id)
            function.arguments = "{}"

    if broken:
        logger.warning(
            f"Reset {len(broken)} truncated tool-call argument string(s) to '{{}}' "
            "so the message history stays parseable; reporting them as failed "
            "tool calls"
        )

    return broken


def _coerce_choice(value: Any, *, choices: frozenset[str], default: str) -> str:
    """Allowlist coercion for a config value restricted to a fixed set of strings.

    `agent_config_values` is a passthrough dict, so missing/None/garbage/typo'd
    values all resolve to `default` rather than being passed through raw — a
    typo can never silently activate a mechanism.
    """
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """Coercion for a config value that is meant to be a boolean.

    Sibling of `_coerce_choice`, and for the same reason: `agent_config_values`
    is a passthrough dict with no server-side validation, so the Studio checkbox
    sends a real bool but an API- or import-set config can carry the STRING
    "false". Bare `bool()` reads that as True — activating the mechanism its
    author switched off. Strings are read for their meaning; anything else falls
    back to `bool()`, and a missing/None value to `default`.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if value is None:
        return default
    return bool(value)


class StirrupAgent:
    """
    Stirrup-compatible agent for GDPval-AA benchmarking
    """

    def __init__(self, run_input: AgentRunInput):
        self.trajectory_id: str = run_input.trajectory_id
        self.model: str = run_input.orchestrator_model
        self.initial_messages: list[LitellmAnyMessage] = list(
            run_input.initial_messages
        )
        # Set for continuation trajectories (HITL), None otherwise
        self._is_continuation: bool = run_input.parent_trajectory_output is not None

        self.mcp_client: FastMCPClient[Any] | None = None
        if run_input.mcp_gateway_url is not None:
            self.mcp_client = FastMCPClient(
                build_mcp_gateway_schema(
                    run_input.mcp_gateway_url,
                    run_input.mcp_gateway_auth_token,
                    run_input.mcp_gateway_actor_id,
                )
            )

        # config from agent_config_values (custom_args can override max_turns for HITL)
        config = run_input.agent_config_values
        custom = run_input.custom_args or {}
        self.timeout: int = config.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        self.max_turns: int = custom.get(
            "max_turns", config.get("max_turns", DEFAULT_MAX_TURNS)
        )
        # custom_args can override for HITL golden-gen: 0 disables the
        # last-turn "call the finish tool now" command for branch segments.
        self.turns_warning_threshold: int = custom.get(
            "turns_remaining_warning_threshold",
            config.get(
                "turns_remaining_warning_threshold", DEFAULT_TURNS_WARNING_THRESHOLD
            ),
        )
        self.tool_call_timeout: int = config.get("tool_call_timeout", 300)
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)
        # Stream model responses so bytes flow continuously over the
        # archipelago -> ALB -> LiteLLM connection. A non-streaming generation
        # holds the connection idle (no bytes) for its entire duration; once
        # that idle gap reaches the ALB's 1800s (30-min) ``idle_timeout`` the
        # ALB tears the connection down. Depending on which deadline trips
        # first this surfaces as a ``litellm.Timeout`` (httpx read deadline,
        # also 1800s) or a 502/504 ``BadGatewayError`` (ALB teardown) — the two
        # race, which is exactly why a turn's retry failure reasons come back
        # "mixed together". ``@with_retry`` then re-issues the same call into
        # the same 30-min wall (a >30-min generation can NEVER complete
        # non-streaming, since ``llm_response_timeout`` is capped at the ALB's
        # idle_timeout) until the cumulative wall time blows the 3h
        # ``asyncio.timeout`` cap and fails the whole run. Streaming resets the
        # idle timer on every chunk, so neither deadline fires and
        # ``llm_response_timeout`` behaves as a per-chunk idle timeout rather
        # than a total-generation cap. Default on; configurable for an escape
        # hatch. ``generate_response`` rebuilds the chunks into an identical
        # ``ModelResponse`` (tool_calls + thinking signatures preserved by
        # ``stream_chunk_builder``), so the rest of the loop is unaffected.
        self.stream: bool = config.get("stream", True)
        self.custom_system_prompt: str | None = config.get("custom_system_prompt")
        self.input_files: list[str] = config.get("input_files", [])
        # Overrides prompts.BASE_SYSTEM_PROMPT. Blank/unset keeps the built-in
        # default, so a world that never sets it builds a byte-identical prompt.
        # Unlike custom_system_prompt (which is APPENDED), this REPLACES the base
        # clause — the input-files section and custom_system_prompt still compose
        # on top of it.
        self.base_system_prompt: str | None = config.get("base_system_prompt")
        # AA GDPval-AA v2's sixth tool. Default off: handing every model a
        # give-up tool would change the toolbelt for existing GDPval and
        # RedlineBench worlds, so parity is opt-in per world.
        self.enable_abandon_task: bool = _coerce_bool(
            config.get("enable_abandon_task"), default=False
        )

        # Optional MCP tools to hide from the model (e.g.
        # "stirrup_code_execution_read_image_file"). Accepts a comma-separated
        # string — what the Studio text field submits — or a list, which is what
        # API-set configs have always passed. Empty = expose every platform tool
        # (the default, so existing configs behave exactly as before).
        #
        # The string branch is load-bearing, not cosmetic: ``list("a,b")`` yields
        # ['a', ',', 'b'], so before the field was form-settable a comma-separated
        # value would have silently disabled nothing and blocked ",".
        disabled = config.get("disabled_tools") or []
        if isinstance(disabled, str):
            disabled = disabled.split(",")
        # ``str()`` rather than ``.strip()`` directly: main's ``list(...)`` never
        # inspected the elements, so a config carrying a non-string (already a
        # bug, but a silent one) kept running. Raising AttributeError here would
        # turn that into a dead trajectory at init.
        self.disabled_tools: list[str] = [
            str(name).strip() for name in disabled if name and str(name).strip()
        ]

        # accounting_mode picks one of three mutually-exclusive families:
        # "default" (nothing active — today's behavior), "token_accounting"
        # (token_budget) or "cost_accounting" (real $ spend). Unlike loop_agent
        # there is no back-compat promotion to do here: stirrup_agent never had
        # a token_budget mechanism predating this field, so no stored config can
        # be relying on one.
        accounting_mode = _coerce_choice(
            config.get("accounting_mode"), choices=_ACCOUNTING_MODES, default="default"
        )
        self.accounting_mode: str = accounting_mode
        self.token_accounting_active: bool = accounting_mode == "token_accounting"
        self.cost_accounting_active: bool = accounting_mode == "cost_accounting"

        # Total provider-reported prompt+completion tokens the run may spend.
        # 0 disables budgeting. Defensively coerced, and zeroed outside
        # token_accounting so a stale stored value can't leak into another mode.
        try:
            raw_token_budget = max(int(config.get("token_budget") or 0), 0)
        except (TypeError, ValueError):
            raw_token_budget = 0
        self.token_budget: int = raw_token_budget if self.token_accounting_active else 0

        # Per-token $ rate overrides for cost_accounting mode. Only included
        # when explicitly set — checked with `is not None`, never truthiness, so
        # a deliberately-zeroed rate (free/promo tier) survives instead of
        # falling through to the litellm-table/model_rates_ctx chain.
        self.rate_overrides: dict[str, float] = {}
        for rate_key in _RATE_OVERRIDE_KEYS:
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
        # Surfaced in usage so a cost_budget_usd cap that's silently breached by
        # unpriced calls is at least explainable after the fact.
        self._cost_unpriced_calls: int = 0

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}
        self.extra_args["ensure_alternating_roles"] = True
        # custom_args can override temperature for HITL per-turn control
        if "temperature" in custom:
            self.extra_args["temperature"] = custom["temperature"]

        # When True, reaching max_turns without calling finish is treated as
        # COMPLETED instead of FAILED.  Used by HITL branches where each branch
        # intentionally runs only 1 turn.
        self._complete_on_max_turns: bool = custom.get("complete_on_max_turns", False)

        # The chronological record of everything that has entered the context.
        # Verifiers read trajectory_messages alone, so without this the tool
        # calls proving the agent's work vanish from the graded transcript.
        self._recorded_messages: list[LitellmAnyMessage] = []

        # context manager
        self.context_manager = StirrupContextManager(
            self.model,
            self.extra_args,
            stream=self.stream,
            on_llm_call=self._track_summarization_call,
        )

        # tool storage — all MCP tools loaded directly from the gateway
        self.mcp_tools: dict[str, ChatCompletionToolParam] = {}

        # agent state
        self.messages: list[LitellmAnyMessage] = []
        self._finalized: bool = False
        self._finish_reason: str | None = None
        self._finish_paths: list[str] = []
        # True when the run ended via abandon_task rather than finish. The run is
        # still COMPLETED (it ended as the model intended, and it must stay in
        # the scored population — excluding give-ups would inflate a model's
        # average), so this marker is what lets analytics tell the two apart.
        self._abandoned: bool = False
        # Set once an exhausted token/cost budget has granted its one final
        # turn, so _run_loop stops after that turn instead of running to
        # max_turns.
        self._budget_final_turn_taken: bool = False
        self.status: AgentStatus = AgentStatus.PENDING
        self.start_time: float | None = None
        self._usage_tracker: UsageTracker = UsageTracker()
        # Run of consecutive empty completions; reset by any turn that yields
        # content or tool calls. See MAX_CONSECUTIVE_EMPTY_TURNS.
        self._consecutive_empty_turns: int = 0
        # Set when the run is abandoned because the provider kept returning
        # empty turns, so _run_loop can stop and mark the trajectory ERROR.
        self._empty_turn_abort: bool = False
        # Content-policy block within the *current* empty-turn streak. Withheld
        # output is a model outcome, so such a run is FAILED, not ERROR.
        self._policy_blocked: bool = False
        # True for confirmed text-only models (KNOWN_TEXT_ONLY_MODELS): the image
        # tool is withheld and any image is stripped from the model's context, so
        # it works best-effort on the text and never 400s on an image it can't see
        # (matches AA's vision-gated image tool).
        self._image_incapable: bool = _is_known_text_only(self.model)

    # Chat Completions-only params that must not leak into Responses API calls
    _CHAT_COMPLETIONS_ONLY_KEYS = {"ensure_alternating_roles"}

    @property
    def _is_responses_model(self) -> bool:
        """Check if the model uses the Responses API (e.g., openai/responses/gpt-5.4)."""
        return "responses/" in self.model

    @property
    def _responses_model_name(self) -> str:
        """Strip the responses/ prefix (e.g., openai/responses/gpt-5.4 → openai/gpt-5.4)."""
        return self.model.replace("responses/", "")

    @property
    def _responses_extra_args(self) -> dict[str, Any]:
        """Return extra_args with Chat Completions-only parameters removed."""
        return {
            k: v
            for k, v in self.extra_args.items()
            if k not in self._CHAT_COMPLETIONS_ONLY_KEYS
        }

    def _convert_messages_for_responses_api(self) -> list[dict[str, Any]]:
        """Convert Chat Completions messages to Responses API input format.

        Maps:
        - system → developer role
        - user → user role
        - assistant (text only) → assistant role
        - assistant (with tool_calls) → function_call items
        - tool → function_call_output items
        """
        converted: list[dict[str, Any]] = []

        for msg in self.messages:
            if isinstance(msg, LitellmOutputMessage):
                role = msg.role
                content = msg.content
                tool_calls = getattr(msg, "tool_calls", None)
                tool_call_id = getattr(msg, "tool_call_id", None)
            else:
                role = msg.get("role", "")
                content = msg.get("content")
                tool_calls = msg.get("tool_calls")
                tool_call_id = msg.get("tool_call_id")

            if role == "system":
                converted.append(
                    {"role": "developer", "content": content_to_str(content)}
                )

            elif role == "user":
                converted.append({"role": "user", "content": content_to_str(content)})

            elif role == "assistant":
                text_content = content_to_str(content)
                if text_content:
                    converted.append({"role": "assistant", "content": text_content})
                if tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            tc_id = tc.get("id", "")
                            converted.append(
                                {
                                    "type": "function_call",
                                    "call_id": tc_id,
                                    "name": func.get("name", ""),
                                    "arguments": func.get("arguments", "{}"),
                                }
                            )
                        else:
                            converted.append(
                                {
                                    "type": "function_call",
                                    "call_id": tc.id,
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                }
                            )

            elif role == "tool":
                call_id = tool_call_id or ""
                converted.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": content_to_str(content),
                    }
                )

        return converted

    def _convert_tools_for_responses_api(self) -> list[dict[str, Any]]:
        """Convert ChatCompletionToolParam (nested) to Responses API FunctionToolParam (flat).

        Chat Completions: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Responses API:    {"type": "function", "name": ..., "parameters": ...}
        """
        tools: list[dict[str, Any]] = []
        for tool in self._get_tools():
            func = tool.get("function", {})
            tools.append(
                {
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {}),
                }
            )
        return tools

    def _build_initial_messages(self) -> list[LitellmAnyMessage]:
        """Build initial messages with Stirrup's system prompt format.

        For continuation trajectories (HITL), initial_messages already contains
        the full conversation history including a system prompt with stirrup-specific
        instructions. In that case, we don't prepend another system prompt to avoid
        duplicate/conflicting system messages.

        For non-continuation trajectories, we always add the stirrup-specific system
        prompt (turn limits, finish tool instructions, input files) even if
        initial_messages already has a generic system message from the DB layer.
        """
        messages: list[LitellmAnyMessage] = []

        if not self._is_continuation:
            # For non-continuations, always add stirrup-specific system prompt
            # This ensures critical instructions (turn limits, finish tool usage)
            # are included even if initial_messages has a generic system message
            system_prompt = build_system_prompt(
                max_turns=self.max_turns,
                custom_system_prompt=self.custom_system_prompt,
                input_files=self.input_files if self.input_files else None,
                base_system_prompt=self.base_system_prompt,
                enable_abandon_task=self._abandon_task_available,
            )
            messages.append(LitellmOutputMessage(role="system", content=system_prompt))

        messages.extend(self.initial_messages)

        return messages

    @property
    def _abandon_task_available(self) -> bool:
        """Whether ``abandon_task`` is actually offered to the model this run.

        ONE decision, read by the three places that have to agree: the tool list,
        the system prompt, and dispatch. Enabling the tool and also naming it in
        ``disabled_tools`` is a coherent thing for a world to do — unlike
        ``finish`` it is not required for the agent to terminate — and driving
        the prompt off the flag alone would instruct the model to call a tool the
        tool list omits, burning turns on calls the disabled gate rejects.
        """
        return (
            self.enable_abandon_task
            and ABANDON_TASK_TOOL_NAME not in self.disabled_tools
        )

    def _get_tools(self) -> list[ChatCompletionToolParam]:
        """Get all tools presented to the model.

        Includes all MCP tools (loaded from gateway), built-in tools,
        and the finish tool.
        """
        tools: list[ChatCompletionToolParam] = []

        tools.extend(self.mcp_tools.values())
        tools.extend(BUILTIN_TOOLS.values())
        tools.append(FINISH_TOOL)
        # Opt-in second terminal tool. Deliberately NOT added to the
        # never-disable exemptions below: unlike finish, it is not required for
        # the agent to terminate, so a world that enables it can still hide it
        # again via disabled_tools without stranding the run.
        if self._abandon_task_available:
            tools.append(ABANDON_TASK_TOOL)

        # AA parity: a text-only model is never offered image-viewing tools, so it
        # can't call a tool whose result it can't consume (which would 400).
        # Matched by name suffix so it hits the gateway-namespaced tool name.
        # The finish tool is always kept.
        if self._image_incapable:
            tools = [
                t
                for t in tools
                if t.get("function", {}).get("name") == FINISH_TOOL_NAME
                or not (t.get("function", {}).get("name") or "").endswith(
                    IMAGE_INPUT_TOOL_SUFFIXES
                )
            ]

        # If disabled_tools is not empty, filter out the tools that are in the
        # disabled_tools list. The finish tool is always kept — disabling it
        # would leave the agent unable to terminate.
        if self.disabled_tools:
            tools = [
                t
                for t in tools
                if t.get("function", {}).get("name") == FINISH_TOOL_NAME
                or t.get("function", {}).get("name") not in self.disabled_tools
            ]

        return tools

    def _strip_images_from_messages(self) -> int:
        """Replace every image_url block in history with a text placeholder.

        Used for text-only models so an image already in the task prompt (or any
        tool result) never reaches the model and 400s it. Returns the count.
        """
        stripped = 0
        for message in self.messages:
            content = get_msg_attr(message, "content")
            if not isinstance(content, list):
                continue
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "image_url":
                    content[i] = {
                        "type": "text",
                        "text": "[image omitted: model cannot view images]",
                    }
                    stripped += 1
        return stripped

    async def _initialize_tools(self, client: Any) -> None:
        """Load all MCP tools from the gateway."""
        tools: list[ChatCompletionToolParam] = await load_mcp_tools(
            client.session, format="openai"
        )  # pyright: ignore[reportAssignmentType]

        for tool in tools:
            name = tool.get("function", {}).get("name")
            if name:
                self.mcp_tools[name] = tool

        terminal_tools = [FINISH_TOOL_NAME]
        if self._abandon_task_available:
            terminal_tools.append(ABANDON_TASK_TOOL_NAME)

        logger.bind(
            message_type="configure",
            payload={
                "mcp_tools": list(self.mcp_tools.keys()),
                "builtin_tools": list(BUILTIN_TOOLS.keys()),
                "terminal_tools": terminal_tools,
            },
        ).info(
            f"Stirrup agent configured with "
            f"{len(self.mcp_tools) + len(BUILTIN_TOOLS) + len(terminal_tools)} tools "
            f"({len(self.mcp_tools)} MCP, {len(BUILTIN_TOOLS)} built-in, "
            f"{len(terminal_tools)} terminal: {', '.join(terminal_tools)})"
        )

    def _inject_turn_warning(self, turn: int, turns_remaining: int) -> bool:
        """Inject this turn's warning message; return whether the budget is spent.

        Stirrup's own threshold-gated turn warning (unchanged) composed with the
        active accounting mode's budget clause, if any, into one user message.
        In "default" mode this is byte-for-byte what it always was: the turn
        clause alone, or nothing.
        """
        warning_parts: list[str] = []
        if turns_remaining <= self.turns_warning_threshold and turn != 0:
            warning_parts.append(build_turn_warning_message(turns_remaining))
        budget_exhausted, budget_clause = self._budget_warning()
        if budget_exhausted and budget_clause:
            # Exhausted: the exhausted template stands alone, no turn clause
            # joined in. The caller marks the turn spent once the model replies.
            warning_parts = [budget_clause]
        elif budget_clause:
            warning_parts.append(budget_clause)
        if warning_parts:
            warning_msg = " ".join(warning_parts)
            self.messages.append(LitellmOutputMessage(role="user", content=warning_msg))
            logger.bind(message_type="turn_warning").info(
                f"Step {turn + 1}: {warning_msg}"
            )
        return budget_exhausted

    async def step(self, client: Any, turn: int) -> None:
        """Execute one turn of the agent loop"""
        turns_remaining = self.max_turns - turn

        budget_exhausted = self._inject_turn_warning(turn, turns_remaining)

        # Per-call stop reason; unrelated to self._finish_reason (the finish
        # tool's answer). model_validate below keeps only choices[0].message.
        provider_finish_reason: str | None = None

        try:
            if self._is_responses_model:
                raw_response = await call_responses_api(
                    model=self._responses_model_name,
                    messages=cast(
                        list[LitellmAnyMessage],
                        self._convert_messages_for_responses_api(),
                    ),
                    tools=self._convert_tools_for_responses_api(),
                    llm_response_timeout=self.llm_response_timeout,
                    extra_args=self._responses_extra_args,
                    trajectory_id=self.trajectory_id,
                    stream=self.stream,
                )
                parsed = parse_responses_api_output(raw_response)
                response_message = responses_output_to_message(parsed)

                if parsed.reasoning_content:
                    logger.bind(message_type="reasoning").info(parsed.reasoning_content)

                response_dict: dict[str, Any] = (
                    raw_response.model_dump()  # type: ignore[reportAny]
                    if hasattr(raw_response, "model_dump")
                    else dict(raw_response)
                )
                provider_finish_reason = _incomplete_reason(response_dict)
                self._usage_tracker.track_from_dict(response_dict)
                if self.cost_accounting_active:
                    # Price with the prefix-stripped name actually sent to the
                    # LLM, not self.model — litellm can't resolve rates for a
                    # "responses/"-prefixed string and would silently fall back
                    # to default rates.
                    self._track_step_cost(raw_response, self._responses_model_name)
                # The call round-tripped, so the granted turn is spent. Set
                # here and not before the call: a context-overflow/timeout/
                # parse-error return answers nothing and must not burn it.
                if budget_exhausted:
                    self._budget_final_turn_taken = True
            else:
                response: ModelResponse = await generate_response(
                    self.model,
                    self.messages,
                    self._get_tools(),
                    self.llm_response_timeout,
                    self.extra_args,
                    trajectory_id=self.trajectory_id,
                    stream=self.stream,
                )

                self._usage_tracker.track(response)
                if self.cost_accounting_active:
                    self._track_step_cost(response, self.model)
                # Spent, same as the Responses branch above — including on an
                # empty-choices reply, which cost real tokens.
                if budget_exhausted:
                    self._budget_final_turn_taken = True
                choices = response.choices
                if not choices or not isinstance(choices[0], Choices):
                    self.messages.append(
                        LitellmOutputMessage(
                            role="user",
                            content="Continue. Use the finish tool when done.",
                        )
                    )
                    return

                response_message = LitellmOutputMessage.model_validate(
                    choices[0].message
                )
                provider_finish_reason = getattr(choices[0], "finish_reason", None)

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
        except ContextWindowExceededError:
            logger.warning("Context exceeded, summarizing")
            await self._summarize_context()
            return
        except Timeout:
            logger.error("LLM timeout")
            return
        except Exception as e:
            if _is_anthropic_tool_argument_parse_error(e):
                logger.warning(
                    "Recovering from Anthropic tool argument parse error; "
                    "prompting model to retry with valid JSON tool arguments"
                )
                self.messages.append(
                    LitellmOutputMessage(
                        role="user",
                        content=TOOL_ARGUMENT_PARSE_RECOVERY_PROMPT,
                    )
                )
                return
            logger.error(f"LLM error: {e}")
            raise

        tool_calls = getattr(response_message, "tool_calls", None)
        content = getattr(response_message, "content", None)
        substantive = bool(content or getattr(response_message, "refusal", None))
        self._policy_blocked |= provider_finish_reason == "content_filter"

        if content:
            logger.bind(message_type="response").info(content)

        if tool_calls:
            tool_names = [tc.function.name for tc in tool_calls]
            logger.bind(message_type="step").info(
                f"Step {turn + 1}: Calling {len(tool_calls)} tool(s): {', '.join(tool_names)}"
            )
        elif not substantive:
            logger.bind(message_type="step").warning(
                "No content and no tool calls "
                f"(finish_reason={provider_finish_reason!r}, "
                f"refusal={getattr(response_message, 'refusal', None)!r}, "
                f"has_thinking={bool(getattr(response_message, 'thinking_blocks', None))}, "
                f"has_reasoning={bool(getattr(response_message, 'reasoning_content', None))})"
            )

        # Repair before storing: once a truncated argument string is in the
        # history every later request replays it and the provider rejects the
        # whole body.
        unparsable_tool_calls = _repair_truncated_tool_arguments(response_message)
        if tool_calls or substantive:
            self.messages.append(response_message)
        elif getattr(response_message, "reasoning_content", None) or getattr(
            response_message, "thinking_blocks", None
        ):
            recorded = {id(m) for m in self._recorded_messages}
            self._recorded_messages.extend(
                m for m in self.messages if id(m) not in recorded
            )
            self._recorded_messages.append(response_message)

        if tool_calls:
            self._consecutive_empty_turns = 0
            self._policy_blocked = False
            await self._handle_tool_calls(client, tool_calls, unparsable_tool_calls)
        else:
            self.messages.append(
                LitellmOutputMessage(
                    role="user",
                    content=self._no_tool_calls_nudge(substantive, turn),
                )
            )
            if substantive:
                self._consecutive_empty_turns = 0
                self._policy_blocked = False
            else:
                self._consecutive_empty_turns += 1
                if self._consecutive_empty_turns >= MAX_CONSECUTIVE_EMPTY_TURNS:
                    logger.error(
                        f"{self._consecutive_empty_turns} consecutive empty turns from "
                        f"{self.model}; abandoning the run as an infrastructure error "
                        "rather than banking a score against an unfinished trajectory"
                    )
                    self._empty_turn_abort = True
                    return

        # _budget_final_turn_taken means the loop stops next iteration, so a
        # summary here would be paid for and then discarded.
        is_last_turn = (turn + 1) == self.max_turns
        if (
            not self._finalized
            and not is_last_turn
            and not self._budget_final_turn_taken
        ):
            if self.context_manager.should_summarize(self.messages):
                logger.bind(message_type="context").info(
                    "Summarizing context (70% threshold reached)"
                )
                try:
                    await self._summarize_context()
                except Exception as e:
                    logger.error(f"Summarization failed: {e}")

    async def _summarize_context(self) -> None:
        """Compact the working context, keeping the dropped turns for the record.

        ``summarize`` returns the leading task context plus a bridge/ack pair, so
        every tool call from the first assistant turn onward leaves ``self.messages``.
        Those turns are the only evidence a transcript-scored verifier has, and
        ``_build_output`` persists what grading reads.

        ``self.messages`` is chronological right up to this call, so recording it
        wholesale — minus what is already recorded — keeps the record in order
        across repeated summarizations. Identity, not equality: the retained task
        context is the same objects, and two turns can carry identical content.
        """
        before = self.messages
        after = await self.context_manager.summarize(before)
        recorded = {id(m) for m in self._recorded_messages}
        new = [m for m in before if id(m) not in recorded]
        self._recorded_messages.extend(new)
        self.messages = after
        logger.bind(message_type="context").info(
            f"Recorded {len(new)} summarized message(s) for the graded transcript "
            f"({len(self._recorded_messages)} total)"
        )

    def _no_tool_calls_nudge(self, had_content: bool, turn: int) -> str:
        """Pick the recovery prompt for a turn that called no tools.

        Three cases, previously collapsed into one "use the finish tool"
        message that read as a stop order:

        * empty completion (no content, no tool calls) -> continue; the model
          never chose to stop, the provider dropped the turn.
        * prose-only turn early in the budget -> continue; suggesting ``finish``
          with most turns unspent is what caused premature finishes.
        * prose-only turn past the halfway mark -> the model may genuinely be
          done, so name ``finish`` while still offering to continue.
        """
        if not had_content:
            return EMPTY_TURN_RECOVERY_PROMPT
        turns_used_fraction = (turn + 1) / max(1, self.max_turns)
        if turns_used_fraction < FINISH_NUDGE_MIN_TURN_FRACTION:
            return NO_TOOL_CALLS_EARLY_RECOVERY_PROMPT
        return NO_TOOL_CALLS_RECOVERY_PROMPT

    async def _handle_tool_calls(
        self,
        client: Any,
        tool_calls: list[Any],
        unparsable_tool_calls: list[str] | None = None,
    ) -> None:
        """Process tool calls"""
        deferred_image_messages: list[LitellmInputMessage] = []
        for tool_call in tool_calls:
            name = tool_call.function.name

            # Arguments arrived truncated and have already been reset to "{}" in
            # the stored message. Executing anyway would run the tool with empty
            # arguments (a shell tool with no command), so surface the failure and
            # let the model retry the step with valid JSON.
            if unparsable_tool_calls and tool_call.id in unparsable_tool_calls:
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=f"Error: {TOOL_ARGUMENT_PARSE_RECOVERY_PROMPT}",
                    )
                )
                continue

            # If the tool is disabled and not the finish tool, add an error message
            if (
                name != FINISH_TOOL_NAME and name in self.disabled_tools
            ):  # ← explicit block (finish can never be disabled)
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=f"Error: tool '{name}' is disabled for this run.",
                    )
                )
                continue

            # One response can carry BOTH terminal tools. Each branch below sets
            # only its own fields, so applying the second would leave the run
            # claiming one terminal state with the other's payload — abandoned
            # with finish's submitted paths, whichever order they arrive in.
            # Only the first terminal tool takes effect; a later DIFFERENT one
            # is refused, and still gets a tool result so every tool_call in the
            # response stays paired.
            #
            # This covers a REPEAT of the same tool too. The accepted result is
            # immutable once set: a second finish carrying a different reason or
            # different paths must not replace what the first one banked, or
            # "first terminal call wins" would hold across tools but not within
            # one. The repeat still gets an echo of the ACCEPTED reason, which is
            # what the export's duplicate-finish dedupe looks for — adjacent
            # call+response pairs with identical content — and echoing the
            # accepted answer makes that pair identical even when the two calls
            # were not.
            #
            # It covers NON-terminal tools as well, which is the point: a
            # `run_shell` sitting after `finish`/`abandon_task` in the same
            # response would otherwise still execute and write to the filesystem
            # AFTER the model ended the run. Snapshot grading reads that
            # filesystem, so a post-terminal call can change the graded
            # deliverable — and for an abandonment it can manufacture a
            # deliverable the model declined to submit. Nothing may run once the
            # run has ended; every skipped call still gets its paired tool
            # result, so provider history stays valid.
            if self._finalized:
                already = (
                    ABANDON_TASK_TOOL_NAME if self._abandoned else FINISH_TOOL_NAME
                )
                if name == already:
                    content = self._finish_reason or ""
                else:
                    logger.bind(message_type="finish").warning(
                        f"Ignoring '{name}' — the run already ended via "
                        f"'{already}' in this turn"
                    )
                    content = (
                        f"Error: this turn already ended the run with "
                        f"'{already}'; the '{name}' call was ignored."
                    )
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=content,
                    )
                )
                continue

            # Handle finish tool
            if name == FINISH_TOOL_NAME:
                reason, paths = parse_finish_tool(tool_call.function.arguments)
                logger.bind(message_type="finish").info(
                    f"Finish called: {reason} (paths: {paths})"
                )
                logger.bind(message_type="final_answer").info(reason)

                self._finalized = True
                self._finish_reason = reason
                self._finish_paths = paths

                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=FINISH_TOOL_NAME,
                        content=reason,
                    )
                )
                continue

            # Handle abandon_task, the second terminal tool. Mirrors finish
            # deliberately: it sets ``_finalized``, so _run_loop stops and the
            # status resolves to COMPLETED — the run ended exactly as the model
            # intended, and marking it FAILED would drop it from the scored
            # population and inflate the model's average by hiding its give-ups.
            # ``_finish_paths`` stays empty because abandoning is declining to
            # submit files, which keeps every downstream consumer that treats
            # finish_paths as a deliverable allow-list correct with no change.
            # The ``final_answer`` emission is what carries the model's stated
            # reason into the graded record, the trajectory logs and the UI.
            # Gated on the flag, not just on advertisement: a model can emit a
            # tool name it was never offered (hallucination, or a continuation
            # inheriting history from a world where it WAS offered). Without
            # this, such a call would terminate a default-off run with no
            # deliverable — the one thing "existing worlds are unchanged" has to
            # rule out. Disabled, it falls through to the unknown-tool error
            # below and the model keeps working.
            if name == ABANDON_TASK_TOOL_NAME and self._abandon_task_available:
                reason = parse_abandon_tool(tool_call.function.arguments)
                logger.bind(message_type="finish").info(f"Abandon called: {reason}")
                logger.bind(message_type="final_answer").info(reason)

                self._finalized = True
                self._abandoned = True
                self._finish_reason = reason

                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=ABANDON_TASK_TOOL_NAME,
                        content=reason,
                    )
                )
                continue

            if name in BUILTIN_TOOLS:
                await self._execute_builtin_tool(client, tool_call)
                continue

            if name in self.mcp_tools:
                await self._execute_mcp_tool(client, tool_call, deferred_image_messages)
                continue

            logger.warning(f"Unknown tool called: {name}")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=f"Error: Unknown tool '{name}'",
                )
            )
        self.messages.extend(deferred_image_messages)

    async def _execute_builtin_tool(self, client: Any, tool_call: Any) -> None:
        """Execute a built-in tool (fetch_web_page, view_image)."""
        name = tool_call.function.name
        args = parse_builtin_tool_args(tool_call.function.arguments)

        tool_logger = logger.bind(ref=tool_call.id, name=name)
        tool_logger.bind(
            message_type="tool_call",
            payload=args,
        ).info(f"Calling built-in tool {name}")

        tool_result_logger = tool_logger.bind(message_type="tool_result")

        try:
            if name == "fetch_web_page":
                url = args.get("url", "")
                result_content = await asyncio.wait_for(
                    execute_fetch_web_page(url),
                    timeout=self.tool_call_timeout,
                )
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=result_content,
                    )
                )
                tool_result_logger.bind(payload=result_content).info(
                    f"Tool {name} completed"
                )

            else:
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=f"Error: Unknown built-in tool '{name}'",
                    )
                )

        except TimeoutError:
            tool_result_logger.error(f"Tool {name} timed out")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content="Tool call timed out",
                )
            )
        except Exception as e:
            tool_result_logger.error(f"Error in built-in tool {name}: {repr(e)}")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=f"Error: {e}",
                )
            )

    async def _execute_mcp_tool(
        self,
        client: Any,
        tool_call: Any,
        deferred_image_messages: list[LitellmInputMessage],
    ) -> None:
        """Execute an MCP tool directly via the gateway."""
        name = tool_call.function.name

        tool_logger = logger.bind(ref=tool_call.id, name=name)
        try:
            args = (
                json.loads(tool_call.function.arguments)
                if tool_call.function.arguments
                else {}
            )
        except json.JSONDecodeError:
            args = {}

        tool_logger.bind(message_type="tool_call", payload=args).info(
            f"Calling MCP tool {name}"
        )
        tool_result_logger = tool_logger.bind(message_type="tool_result")

        mcp_tool_call = {
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            },
        }

        shielded_task = asyncio.ensure_future(
            call_openai_tool(client.session, mcp_tool_call)  # pyright: ignore[reportArgumentType]
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(shielded_task),
                timeout=self.tool_call_timeout,
            )
        except TimeoutError:
            tool_result_logger.error(f"Tool {name} timed out")
            await drain_shielded_task(shielded_task)
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content="Tool call timed out",
                )
            )
            return
        except Exception as e:
            if is_fatal_mcp_error(e):
                tool_result_logger.error(f"Fatal MCP error, ending run: {repr(e)}")
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=f"Fatal error: MCP session terminated - {e}",
                    )
                )
                raise
            tool_result_logger.error(f"Error calling {name}: {repr(e)}")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=f"Error: {e}",
                )
            )
            return

        if not result.content:
            tool_result_logger.error(f"Tool {name} returned no content")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content="No content returned",
                )
            )
            return

        # Convert MCP content blocks to messages (handles text, images, and mixed)
        result_messages = content_blocks_to_messages(
            result.content,
            tool_call.id,
            name,
            self.model,
            deferred_image_messages=deferred_image_messages,
            strip_images=self._image_incapable,
        )
        self.messages.extend(result_messages)  # pyright: ignore[reportArgumentType]

        tool_result_logger.bind(
            payload=[block.model_dump() for block in result.content],
        ).info(f"Tool {name} completed")

    def _track_step_cost(self, response: Any, model: str) -> None:
        """Compute and log this turn's real $ cost (cost_accounting mode only).

        Always logs — cost_budget_usd=0 means "no cap", not "don't log". A call
        with unreadable usage contributes $0 to _cost_spent (there's no token
        count to price), but is counted in _cost_unpriced_calls so a
        cost_budget_usd cap silently breached by unpriced calls is still
        explainable after the fact — real spend must never look like it was
        simply never incurred.

        `model` is passed explicitly rather than read off `self.model` because
        the Responses-API path sends a prefix-stripped model string; pricing
        must use the exact string that reached the LLM.
        """
        call_cost = compute_call_cost_usd(model, response, self.rate_overrides)
        if call_cost is None:
            self._cost_unpriced_calls += 1
            logger.bind(
                message_type="step_cost",
                unpriced_calls=self._cost_unpriced_calls,
            ).warning("Cost unavailable for this call (unreadable usage); not counted")
            return
        self._cost_spent += call_cost
        logger.bind(
            message_type="step_cost",
            call_cost_usd=call_cost,
            cumulative_cost_usd=self._cost_spent,
        ).info(f"Step cost: ${call_cost:.6f} (cumulative: ${self._cost_spent:.6f})")

    def _track_summarization_call(self, response: Any, model: str) -> None:
        """Fold a context-summarization LLM call into budget accounting.

        Summarization is a real LLM call, so a token/cost cap must not be
        evadable by it. Gated on an accounting mode being active: counting it
        unconditionally would change the reported token totals of every
        existing stirrup run (GDPval numbers included), which nothing outside
        accounting asked for.
        """
        if not (self.token_accounting_active or self.cost_accounting_active):
            return
        if self._is_responses_model:
            self._usage_tracker.track_from_dict(
                response.model_dump()
                if hasattr(response, "model_dump")
                else dict(response)
            )
        else:
            self._usage_tracker.track(response)
        if self.cost_accounting_active:
            self._track_step_cost(response, model)

    def _tokens_spent(self) -> int:
        """Exact provider-reported prompt+completion tokens spent so far."""
        return self._usage_tracker.prompt_tokens + self._usage_tracker.completion_tokens

    def _budget_warning(self) -> tuple[bool, str | None]:
        """(exhausted, clause) for the active accounting mode.

        Returns `(False, None)` whenever no budget is actually configured —
        including cost_accounting with `cost_budget_usd=0` (log-only, no cap),
        where a "$0.0000 of $0.0000 remaining" clause would be nonsense. Only
        an exhausted budget returns `exhausted=True`; Stirrup's own turn
        warnings never trigger it.
        """
        if self.token_accounting_active and self.token_budget:
            tokens_spent = self._tokens_spent()
            if tokens_spent >= self.token_budget:
                return True, TOKEN_BUDGET_EXHAUSTED_TEMPLATE.format(
                    token_budget=self.token_budget,
                    tokens_spent=tokens_spent,
                )
            return False, TOKEN_BUDGET_WARNING_TEMPLATE.format(
                tokens_remaining=max(self.token_budget - tokens_spent, 0),
                token_budget=self.token_budget,
            )
        if self.cost_accounting_active and self.cost_budget_usd:
            if self._cost_spent >= self.cost_budget_usd:
                return True, COST_BUDGET_EXHAUSTED_TEMPLATE.format(
                    cost_budget=self.cost_budget_usd,
                    cost_spent=self._cost_spent,
                )
            return False, COST_BUDGET_WARNING_TEMPLATE.format(
                cost_remaining=max(self.cost_budget_usd - self._cost_spent, 0.0),
                cost_budget=self.cost_budget_usd,
            )
        return False, None

    def _build_output(self) -> AgentTrajectoryOutput:
        """Build the trajectory output."""
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
        # By id, not equality: summarize re-uses the retained task-context
        # objects, so the record and the context overlap.
        recorded = {id(m) for m in self._recorded_messages}
        return AgentTrajectoryOutput(
            messages=[
                *self._recorded_messages,
                *(m for m in self.messages if id(m) not in recorded),
            ],
            output={
                "finish_reason": self._finish_reason,
                "finish_paths": self._finish_paths,
                # Distinguishes an abandon_task run from a finish run: both are
                # COMPLETED and both carry a finish_reason, so without this the
                # two are indistinguishable downstream. Always present (not
                # conditional) so a consumer can read it without a default.
                "abandoned": self._abandoned,
            }
            if self._finalized
            else None,
            status=self.status,
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=usage,
        )

    async def _run_loop(self, client: Any) -> AgentTrajectoryOutput:
        """Core agent loop, shared between MCP and non-MCP modes."""
        logger.info(
            f"Starting Stirrup agent with {self.model} "
            f"(max_turns={self.max_turns}, warning_threshold={self.turns_warning_threshold})"
        )
        if client is not None:
            await self._initialize_tools(client)

        self.messages = self._build_initial_messages()
        # Text-only model: strip any image already embedded in the task prompt so
        # the very first generation doesn't 400 on an image it can't see.
        if self._image_incapable:
            stripped = self._strip_images_from_messages()
            if stripped:
                logger.bind(
                    trajectory_id=self.trajectory_id,
                    event="image_stripped_for_text_only_model",
                    images_stripped=stripped,
                ).info(f"Stripped {stripped} prompt image(s) for text-only model")

        self.start_time = time.time()
        self.status = AgentStatus.RUNNING

        for turn in range(self.max_turns):
            if self._finalized:
                logger.info(f"Finished after {turn} turns")
                break
            if self._budget_final_turn_taken:
                # The exhausted budget already granted its one final turn.
                break
            logger.bind(message_type="step").info(f"Starting step {turn + 1}")
            await self.step(client, turn)
            if self._empty_turn_abort:
                break

        # Precedes the abort and max-turns branches: withheld output is a
        # model outcome however the loop ended.
        if self._policy_blocked and not self._finalized:
            logger.error(
                f"Empty turns from {self.model} were content-policy blocked; "
                "scoring rather than retrying as infra"
            )
            self.status = AgentStatus.FAILED
            return self._build_output()

        # ERROR (not FAILED) so the dispatcher treats it as retryable infra and
        # the trajectory stays out of the scored population — a provider that
        # will not emit tool calls says nothing about the model's task ability.
        if self._empty_turn_abort and not self._finalized:
            self.status = AgentStatus.ERROR
            return self._build_output()

        if not self._finalized:
            if self._complete_on_max_turns:
                logger.info(
                    f"Max turns ({self.max_turns}) reached; completing "
                    f"(complete_on_max_turns=True)"
                )
                self.status = AgentStatus.COMPLETED
            elif self._budget_final_turn_taken and self.cost_accounting_active:
                logger.error(
                    f"Not finished after exhausting cost budget of "
                    f"${self.cost_budget_usd:.4f}"
                )
                self.status = AgentStatus.FAILED
            elif self._budget_final_turn_taken:
                logger.error(
                    f"Not finished after exhausting token budget of {self.token_budget}"
                )
                self.status = AgentStatus.FAILED
            else:
                logger.error(f"Not finished after {self.max_turns} turns")
                self.status = AgentStatus.FAILED
        else:
            self.status = AgentStatus.COMPLETED

        return self._build_output()

    async def run(self) -> AgentTrajectoryOutput:
        """Run the agent loop."""
        try:
            async with asyncio.timeout(self.timeout):
                if self.mcp_client is not None:
                    async with self.mcp_client as client:
                        return await self._run_loop(client)
                else:
                    return await self._run_loop(None)

        except TimeoutError:
            logger.error(f"Timeout after {self.timeout}s")
            self.status = AgentStatus.ERROR
            return self._build_output()

        except asyncio.CancelledError:
            logger.error("Cancelled")
            self.status = AgentStatus.CANCELLED
            return self._build_output()

        except Exception as e:
            logger.error(f"Error: {e}")
            self.status = (
                AgentStatus.ERROR if is_system_error(e) else AgentStatus.FAILED
            )
            return self._build_output()


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """Entry point for the Stirrup agent."""
    return await StirrupAgent(run_input).run()
