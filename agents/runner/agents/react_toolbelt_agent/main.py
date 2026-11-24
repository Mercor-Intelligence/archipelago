"""
ReAct Toolbelt Agent with ReSum Context Management.
"""

import asyncio
import contextlib
import time
from typing import Any, ClassVar, cast

from fastmcp import Client as FastMCPClient
from litellm import Choices
from litellm.exceptions import ContextWindowExceededError, Timeout
from litellm.experimental_mcp_client import load_mcp_tools
from litellm.experimental_mcp_client.tools import (
    transform_mcp_tool_to_openai_tool,
    transform_openai_tool_call_request_to_mcp_tool_call_request,
)
from litellm.files.main import ModelResponse
from loguru import logger
from mcp.types import Tool as MCPTool
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from runner.agents.descriptor_resolver import resolve_config
from runner.agents.models import (
    AgentConfigIds,
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
    LitellmAnyMessage,
    LitellmInputMessage,
    LitellmOutputMessage,
    get_msg_attr,
    get_msg_content,
    get_msg_role,
)
from runner.utils.budget_limited_attempt import (
    AttemptOutcome,
    budget_limited_attempt_marker,
)
from runner.utils.compaction.resum import EmptySummaryError, SummaryTimeoutError
from runner.utils.context_budget import (
    compaction_timeout_from_config,
    context_budget_from_config,
    reported_prompt_tokens,
)
from runner.utils.decorators import (
    ModelEndpointUnresponsiveError,
    llm_attempt_hook_ctx,
)
from runner.utils.error import (
    is_fatal_mcp_error,
    is_gateway_5xx,
    is_gateway_connection_error,
    is_system_error,
)
from runner.utils.file_staging import TurnFileStagingError, stage_message_files
from runner.utils.llm import generate_response
from runner.utils.logging.terminal_error import (
    Budget,
    FaultKind,
    budget_exhausted,
    loop_detected,
)
from runner.utils.mcp import (
    build_mcp_gateway_schema,
    content_blocks_to_messages,
    drain_shielded_task,
    empty_tool_result_content,
    is_replay_safe_tool,
    log_mcp_tool_result,
    mcp_call_never_sent,
    probe_gateway_health,
    reconnect_mcp_session,
    wait_before_gateway_5xx_replay,
)
from runner.utils.neosigma_tracing import call_openai_tool, trace_tool
from runner.utils.progress_monitor import (
    LOOP_OUTCOME,
    STALL_OUTCOME,
    ProgressConfigError,
    ProgressMonitor,
    ProgressPolicy,
    compaction_grace_s,
    config_flag,
    config_number,
    longest_call_s,
    progress_mode_on,
)
from runner.utils.sandbox_files import SandboxUploadError
from runner.utils.settings import get_settings
from runner.utils.toolsets import ToolsetRecorder
from runner.utils.usage import UsageTracker

from .resum import (
    KEEP_RECENT_MESSAGES,
    ReSumManager,
    _is_synthetic_summary_message,
)
from .tool_result import truncate_tool_messages
from .tools import (
    FINAL_ANSWER_TOOL,
    FINAL_ANSWER_TOOL_NAME,
    META_TOOL_NAMES,
    META_TOOLS,
    TODO_WRITE_TOOL,
    MetaToolHandler,
    parse_final_answer,
    resolve_tool_call_name,
)

# Bounds the extra LLM steps a flapping gateway can cost one run.
MCP_TOOL_RECONNECTS_PER_RUN = 3

# Mirrors rl-studio stop_continuation.COMPACT_ON_START_ARG across the deploy boundary.
COMPACT_ON_START_ARG = "compact_on_start"
# Bounds the wall clock that retrying stuck tools can add to one run.
MCP_TOOL_TIMEOUT_RETRIES_PER_RUN = 5

MCP_CALL_MAY_HAVE_RUN = (
    "Error: the connection to the environment dropped while this tool call was "
    "in flight, so it may or may not have taken effect. The connection has been "
    "re-established; check the current state before repeating the call."
)

# With a stall window, a call that exhausts its timeout retries is a stall.
_LLM_GIVE_UP_STALLS: tuple[type[Exception], ...] = (
    Timeout,  # not APIConnectionError: litellm's catch-all, often a real 4xx
    ModelEndpointUnresponsiveError,
)


class _LlmCallGaveUp(Exception):
    """A model call ran out of retries on timeouts; a stall in progress mode."""


# litellm normalizes Gemini MAX_TOKENS and Anthropic max_tokens to "length".
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})
NO_TOOL_CALL_MESSAGE = "No tools called. Use final_answer to submit your answer. Please continue completing the task."
TRUNCATED_REPLY_MESSAGE = (
    "Your last reply was cut off at the output limit before any tool call "
    "completed, so nothing was run. Do not resend the same large content. "
    "Write large files in smaller parts: create the file with the first part, "
    "then append the rest in separate tool calls."
)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """bool, or "true"/"false"/"0"/0 from Studio form paths; anything else is ``default``."""
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


def _whole_seconds(value: float) -> int | float:
    return int(value) if value.is_integer() else value


class ReActAgent:
    """ReAct Toolbelt Agent with ReSum context management."""

    # Progress-aware limits (runner/utils/progress_monitor.py); set only when
    # the agent turns on progress_mode.
    monitor: ProgressMonitor | None = None
    # Progress mode needs step() to record each turn and mark what is in
    # flight. A subclass that overrides step() (the Anthropic workaround) does
    # neither, so support is not inherited: a subclass opts in by setting this
    # in its own body (see _supports_progress_mode).
    SUPPORTS_PROGRESS_MODE: ClassVar[bool] = True
    # Class-level False, so an agent built without __init__ never compacts on start.
    _compact_on_start: bool = False

    @classmethod
    def _supports_progress_mode(cls) -> bool:
        """True only if this exact class sets SUPPORTS_PROGRESS_MODE in its own body."""
        return bool(cls.__dict__.get("SUPPORTS_PROGRESS_MODE", False))

    def __init__(self, run_input: AgentRunInput):
        self.trajectory_id: str = run_input.trajectory_id
        self.model: str = run_input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(run_input.initial_messages)

        if run_input.mcp_gateway_url is None:
            raise ValueError("MCP gateway URL is required for react toolbelt agent")

        self._gateway_schema = build_mcp_gateway_schema(
            run_input.mcp_gateway_url,
            run_input.mcp_gateway_auth_token,
            run_input.mcp_gateway_actor_id,
        )
        self.mcp_client = FastMCPClient(self._gateway_schema)

        # Config
        config = resolve_config("react_toolbelt_agent", run_input.agent_config_values)
        for key, issue in config.issues.items():
            logger.bind(message_type="configure").warning(f"{key}: {issue}")
        self.timeout: int = cast(int, config.get("wall_clock_timeout_sec", 10800))
        self.max_steps: int = cast(int, config.get("max_steps", 250))
        self.tool_call_timeout: int = cast(
            int, config.get("tool_call_timeout_sec", 180)
        )
        self.llm_response_timeout: int = cast(
            int, config.get("llm_call_timeout_sec", 600)
        )
        self.stage_files_to_env: bool = _coerce_bool(
            config.get("attachments_staged_to_sandbox"), default=False
        )
        self._mcp_gateway_url: str = run_input.mcp_gateway_url
        self._mcp_gateway_auth_token: str | None = run_input.mcp_gateway_auth_token
        self.max_toolbelt_size: int = 80
        self.use_toolbelt: bool = _coerce_bool(config.get("use_toolbelt"), default=True)
        # Off by default: 29% of completed Julius Gemini runs had 3+ cut-offs in a row.
        self.max_consecutive_truncations: int = int(
            cast(int | float | str, config.get("max_consecutive_truncations") or 0)
        )
        self._consecutive_truncations: int = 0
        self._truncated_completion_tokens: int | None = None
        self._mcp_reconnect_enabled: bool = get_settings().MCP_TOOL_RECONNECT_ENABLED
        self._mcp_reconnects_left: int = MCP_TOOL_RECONNECTS_PER_RUN
        self._timeout_retry_enabled: bool = (
            get_settings().MCP_TOOL_TIMEOUT_RETRY_ENABLED
        )
        self._timeout_retries_left: int = MCP_TOOL_TIMEOUT_RETRIES_PER_RUN
        self._replay_safe_tools: set[str] = set()

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}

        # Components
        context_config = {
            "context_window_tokens": config.get("context_window_tokens"),
            "context_compaction_cutoff": config.get("context_compaction_threshold"),
            "context_compaction_timeout": config.get(
                "context_compaction_call_timeout_sec"
            ),
        }
        context_window_tokens, compaction_cutoff = context_budget_from_config(
            context_config
        )
        self.resum: ReSumManager = ReSumManager(
            self.model,
            self.extra_args,
            context_window_tokens=context_window_tokens,
            trigger_fraction=compaction_cutoff,
            summary_timeout=compaction_timeout_from_config(context_config),
        )

        # Toolbelt state
        self.all_tools: dict[str, ChatCompletionToolParam] = {}
        self.toolbelt: set[str] = set()
        self.meta_tool_handler: MetaToolHandler | None = None
        self.toolsets = ToolsetRecorder(self.messages)

        # Agent state
        self._finalized: bool = False
        self._final_answer: str | None = None
        self._final_status: str = "completed"
        self.status: AgentStatus = AgentStatus.PENDING
        self.start_time: float | None = None
        self._usage_tracker: UsageTracker = UsageTracker(
            track_token_breakdown=True, model=self.model
        )
        self._usage_tracker.set_context_policy(
            self.resum.max_tokens,
            self.resum.context_window_source,
            self.resum.trigger_fraction,
            measurement=self.resum.measurement,
            projector=self.resum.projector,
        )
        self._compact_on_start = (run_input.custom_args or {}).get(
            COMPACT_ON_START_ARG
        ) is True

        # Progress-aware limits: off unless the agent sets progress_mode. With
        # it off nothing here reads the config's values, so a null timeout
        # still means no harness clock, as before.
        self.policy: ProgressPolicy | None = None
        self._progress_config_error: ProgressConfigError | None = None
        self._stop_markers: dict[str, Any] = {}
        self._dispatched: list[tuple[str, Any]] = []
        self._progress_step: int = 0
        self._progress_step_budget: int = 0
        self._progress_wall_clock_s: float = 0.0
        self._ceiling_hit: bool = False
        self._step_ceiling_hit: bool = False
        self._progressing_at_stop: bool | None = None
        # Set once a progress-mode run's end (status and fault) is recorded.
        self._progress_ended: bool = False
        if progress_mode_on(config.get("progress_mode")):
            if type(self)._supports_progress_mode():
                self._init_progress(config)
            else:
                logger.bind(message_type="configure").warning(
                    f"progress_mode ignored: {type(self).__name__} does not "
                    "support progress-aware limits"
                )
        elif config_flag(config.get("snapshot_on_stop")):
            logger.bind(message_type="configure").warning(
                "snapshot_on_stop ignored: it needs progress_mode"
            )

    def _init_progress(self, config: Any) -> None:
        try:
            # The API can send numeric strings; anything else unrunnable is a
            # harness_contract error naming the key, never a TypeError.
            llm_s = config_number(
                "llm_call_timeout_sec", self.llm_response_timeout, low=1
            )
            tool_s = config_number(
                "tool_call_timeout_sec", self.tool_call_timeout, low=1
            )
            compaction_s = compaction_grace_s(
                config, model=self.model, extra_args=self.extra_args
            )
            self.policy = ProgressPolicy.from_config(
                config,
                base_wall_clock_s=self.timeout,
                base_max_steps=self.max_steps,
                grace_s=llm_s + tool_s + compaction_s,
                # An orchestrator timeout extra arg can replace either LLM timeout.
                longest_call_s=longest_call_s(
                    llm_s,
                    tool_s,
                    self.resum.longest_attempt_s,
                    self.extra_args.get("timeout"),
                    self.extra_args.get("request_timeout"),
                ),
            )
        except ProgressConfigError as error:
            self._progress_config_error = error
            return
        # Run on the numbers validated above, not numeric strings from the API.
        self.llm_response_timeout = cast(int, _whole_seconds(llm_s))
        self.tool_call_timeout = cast(int, _whole_seconds(tool_s))
        self._progress_step_budget = self.policy.base_max_steps
        self._progress_wall_clock_s = min(
            self.policy.base_wall_clock_s, self.policy.hard_wall_clock_s
        )
        self.monitor = ProgressMonitor(
            self.policy,
            read_only=self._replay_safe_tools,
            meta=META_TOOL_NAMES | {FINAL_ANSWER_TOOL_NAME},
        )

    def _progress_begin(self, activity: str) -> None:
        if self.monitor is not None:
            self.monitor.begin(activity)

    def _progress_end(self) -> None:
        if self.monitor is not None:
            self.monitor.end()

    def _served_builtins(self) -> list[ChatCompletionToolParam]:
        """The tools ``_get_tools`` serves that never enter ``all_tools``.

        Read from ``_get_tools`` rather than relisted, so a tool added there is
        inspectable without a second edit.
        """
        return [
            tool
            for tool in self._get_tools()
            if tool["function"]["name"] not in self.all_tools
        ]

    def _get_tools(self) -> list[ChatCompletionToolParam]:
        """Get tools for LLM: meta-tools + toolbelt + final_answer."""
        toolbelt_tools = [self.all_tools[name] for name in self.toolbelt]
        meta_tools = META_TOOLS if self.use_toolbelt else [TODO_WRITE_TOOL]
        return list(meta_tools) + toolbelt_tools + [FINAL_ANSWER_TOOL]

    async def _initialize_tools(self, client: Any) -> None:
        """Load tools from MCP gateway."""
        mcp_tools: list[MCPTool] = await load_mcp_tools(client.session, format="mcp")  # pyright: ignore[reportAssignmentType]

        for mcp_tool in mcp_tools:
            tool: ChatCompletionToolParam = transform_mcp_tool_to_openai_tool(mcp_tool)  # pyright: ignore[reportAssignmentType]
            name = tool.get("function", {}).get("name")
            if name:
                self.all_tools[name] = tool
                if is_replay_safe_tool(mcp_tool):
                    self._replay_safe_tools.add(name)

        if not self.use_toolbelt:
            self.toolbelt.update(self.all_tools)

        self.meta_tool_handler = MetaToolHandler(
            self.all_tools,
            self.toolbelt,
            self.max_toolbelt_size,
            served_builtins=self._served_builtins,
        )

        mode = "toolbelt starts empty" if self.use_toolbelt else "toolbelt off"
        logger.bind(
            message_type="configure",
            payload=list(self.all_tools.keys()),
        ).info(f"Loaded {len(self.all_tools)} MCP tools ({mode})")

    async def _generate_response(
        self, messages: list[LitellmAnyMessage], tools: list[ChatCompletionToolParam]
    ) -> ModelResponse:
        """One model call; its retries giving up on timeouts is a stall in
        progress mode with a stall window (see _LLM_GIVE_UP_STALLS)."""
        try:
            return await generate_response(
                self.model,
                messages,
                tools,
                self.llm_response_timeout,
                self.extra_args,
                trajectory_id=self.trajectory_id,
                system_instructions_harness=AgentConfigIds.REACT_TOOLBELT_AGENT,
            )
        except _LLM_GIVE_UP_STALLS as e:
            if self.monitor is not None and self.monitor.policy.stall_window_s:
                raise _LlmCallGaveUp(type(e).__name__) from e
            raise

    def _take_compact_on_start(self) -> bool:
        """True once, at a continuation's first step, if its seed needs compacting.

        That is when the seed holds more than ReSum's recent messages after its
        latest summary, or after the task prompt when it has none. The seed may
        then be as large as the parent's context just before it would compact,
        so it is compacted before the first call rather than sent whole.
        """
        if not self._compact_on_start:
            return False
        self._compact_on_start = False
        non_system = [m for m in self.messages if get_msg_role(m) != "system"]
        latest = max(
            (
                index
                for index, message in enumerate(non_system)
                if _is_synthetic_summary_message(message)
            ),
            default=0,
        )
        return len(non_system) - latest - 1 > KEEP_RECENT_MESSAGES

    async def step(self, client: Any) -> None:
        """Execute one step of the ReAct loop."""
        # Proactive ReSum check, and a continuation's compaction of its seed
        if self._take_compact_on_start() or await asyncio.to_thread(
            self.resum.should_summarize, self.messages
        ):
            logger.bind(message_type="resum").info("Summarizing context")
            try:
                before = len(self.messages)
                self._progress_begin("compaction")
                self.messages = await self.resum.summarize(self.messages)
                self._progress_end()
                # Only flag a compaction when context was actually reduced;
                # summarize() can no-op and return the messages unchanged.
                if len(self.messages) < before:
                    self._usage_tracker.track_compaction()
            except Exception as e:
                logger.error(f"Summarization failed: {e}")

        # Call LLM. Capture the list being sent before anything appends to
        # it: the projector's observation must pair the provider's
        # prompt_tokens with the estimate of exactly this list.
        sent_messages = self.messages
        tools = self._get_tools()
        self._progress_begin("llm")
        try:
            response: ModelResponse = await self._generate_response(
                sent_messages, tools
            )
        except _LlmCallGaveUp:
            # Progress mode with a stall window only: the run ends as a stall.
            raise
        except ContextWindowExceededError as e:
            self._consecutive_truncations = 0
            logger.warning("Context exceeded, summarizing")
            # The provider just contradicted the projection for this exact
            # list; recalibrate so the proactive trigger fires earlier next time.
            await asyncio.to_thread(self.resum.observe_overflow, sent_messages, e)
            before = len(self.messages)
            self._progress_begin("compaction")
            try:
                self.messages = await self.resum.summarize(
                    self.messages, trigger="overflow"
                )
            except EmptySummaryError as summary_error:
                # Still fails the run; the error now names the overflow as its cause.
                raise EmptySummaryError(
                    f"context overflow: compaction summary was empty ({summary_error})"
                ) from summary_error
            except SummaryTimeoutError as summary_error:
                raise SummaryTimeoutError(
                    f"context overflow: compaction summary timed out ({summary_error})",
                    model=summary_error.model,
                    llm_provider=summary_error.llm_provider,
                ) from summary_error
            self._progress_end()
            if len(self.messages) < before:
                self._usage_tracker.track_compaction()
            return
        except Timeout:
            self._consecutive_truncations = 0
            logger.error("LLM timeout")
            return
        except Exception as e:
            logger.error(f"LLM error: {e}")
            raise
        self._progress_end()

        counts = await asyncio.to_thread(self._usage_tracker.breakdown_counts, response)
        self._usage_tracker.track(response, counts)
        await asyncio.to_thread(
            self.resum.observe_llm_usage,
            sent_messages,
            reported_prompt_tokens(response),
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            self._consecutive_truncations = 0
            logger.bind(message_type="step").warning(
                "LLM returned an empty response with no choices, re-prompting with 'continue'"
            )
            self.messages.append(
                LitellmOutputMessage(
                    role="user", content="Continue. Use final_answer when done."
                )
            )
            return

        response_message = LitellmOutputMessage.model_validate(choices[0].message)
        tool_calls = getattr(response_message, "tool_calls", None)
        content = getattr(response_message, "content", None)

        # Log reasoning if present (o1/reasoning models)
        if getattr(response_message, "reasoning_content", None):
            logger.bind(message_type="reasoning").info(
                response_message.reasoning_content
            )

        # Log thinking blocks if present (Claude extended thinking)
        if getattr(response_message, "thinking_blocks", None):
            if isinstance(response_message.thinking_blocks, list):
                for thinking_block in response_message.thinking_blocks:
                    if thinking_block.get("thinking"):
                        logger.bind(message_type="thinking").debug(
                            thinking_block.get("thinking")
                        )

        # Log response content
        if content:
            logger.bind(message_type="response").info(content)

        # Log tool call summary
        if tool_calls:
            tool_names = [tc.function.name for tc in tool_calls]
            logger.bind(message_type="step").info(
                f"Calling {len(tool_calls)} tool(s): {', '.join(tool_names)}"
            )
        elif not content:
            logger.bind(message_type="step").warning("No content and no tool calls")
            try:
                finish_reason = choices[0].finish_reason if choices else None
                logger.bind(message_type="step").warning(
                    f"(finish_reason={finish_reason})"
                )
            except Exception as e:
                logger.error(f"Error getting finish reason: {e}")

        truncated = not tool_calls and self._is_truncated(choices[0])
        if truncated:
            self._record_truncation(response)
        else:
            self._consecutive_truncations = 0

        step_msg_start = len(self.messages)
        self._dispatched = []
        self.toolsets.record(tools)
        self.messages.append(response_message)

        try:
            if tool_calls:
                await self._handle_tool_calls(client, tool_calls)
            else:
                self.messages.append(
                    LitellmOutputMessage(
                        role="user",
                        content=TRUNCATED_REPLY_MESSAGE
                        if truncated
                        else NO_TOOL_CALL_MESSAGE,
                    )
                )
        finally:
            # Attribute this step's tool-result tokens even if tool handling
            # raised (partial results still count).
            self._record_step_tool_output(step_msg_start)
        if self.monitor is not None:
            # Only calls sent to the gateway count toward progress.
            self.monitor.record_step(self._dispatched)

    @staticmethod
    def _is_truncated(choice: Any) -> bool:
        reason = getattr(choice, "finish_reason", None)
        return str(reason or "").lower() in TRUNCATED_FINISH_REASONS

    def _record_truncation(self, response: ModelResponse) -> None:
        self._consecutive_truncations += 1
        usage = getattr(response, "usage", None)
        self._truncated_completion_tokens = getattr(usage, "completion_tokens", None)
        logger.bind(
            message_type="step",
            consecutive_truncations=self._consecutive_truncations,
            completion_tokens=self._truncated_completion_tokens,
        ).warning(
            f"Reply cut off at the output limit with no tool call "
            f"({self._consecutive_truncations} in a row)"
        )

    def _truncation_limit_reached(self) -> bool:
        return (
            self.max_consecutive_truncations > 0
            and self._consecutive_truncations >= self.max_consecutive_truncations
        )

    def _record_step_tool_output(self, step_msg_start: int) -> None:
        """Attribute this step's tool-result tokens to its call_log entry.

        Reads role/name/content with get_msg_attr/get_msg_content so it handles
        both Pydantic and TypedDict messages (MCP tool results are TypedDicts).
        No-op unless breakdown tracking is enabled.
        """
        tool_texts: list[str] = []
        image_uris: list[str] = []
        for m in self.messages[step_msg_start + 1 :]:
            c = get_msg_content(m)
            # Tool-result images land in this step's range either inside the tool
            # message (Anthropic embeds image_url blocks) or as deferred user
            # messages (other providers can't embed in tool results). Collect
            # either; they're elided before storage so they must be counted now.
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        url = (b.get("image_url") or {}).get("url")
                        if url:
                            image_uris.append(url)
            if get_msg_attr(m, "role") != "tool":
                continue
            # A *successful* final_answer is echoed as a tool message but is
            # already counted via final_answer_tokens, so skip it to avoid double
            # counting. A *rejected* final_answer (e.g. incomplete todos) is not,
            # so let its error body count as tool output.
            if get_msg_attr(m, "name") == "final_answer" and self._finalized:
                continue
            if isinstance(c, str):
                tool_texts.append(c)
            elif isinstance(c, list):
                tool_texts.append(
                    " ".join(b.get("text", "") for b in c if isinstance(b, dict))
                )
        if tool_texts:
            self._usage_tracker.track_tool_output(" ".join(tool_texts))
        for uri in image_uris:
            self._usage_tracker.track_tool_output_image(uri)

    async def _handle_tool_calls(self, client: Any, tool_calls: list[Any]) -> None:
        """Process tool calls."""
        mcp_tool_calls: list[Any] = []
        served = self.all_tools.keys() | META_TOOL_NAMES | {FINAL_ANSWER_TOOL_NAME}

        for tool_call in tool_calls:
            name = resolve_tool_call_name(tool_call, served)

            # Final answer - validate todos, then handle and return
            if name == "final_answer":
                # Check for incomplete todos
                assert self.meta_tool_handler
                incomplete = self.meta_tool_handler.get_incomplete_todos()
                if incomplete:
                    incomplete_list = ", ".join(
                        f"'{t.id}' ({t.status.value})" for t in incomplete
                    )
                    error_msg = (
                        f"ERROR: Cannot submit final_answer with incomplete todos. "
                        f"You have {len(incomplete)} incomplete task(s): {incomplete_list}. "
                        f"Use todo_write to mark each as 'completed' or 'cancelled' first."
                    )
                    logger.bind(message_type="tool").warning(
                        f"final_answer rejected: {len(incomplete)} incomplete todos"
                    )
                    self.messages.append(
                        LitellmOutputMessage(
                            role="tool",
                            tool_call_id=tool_call.id,
                            name="final_answer",
                            content=error_msg,
                        )
                    )
                    return

                answer, status = parse_final_answer(tool_call.function.arguments)
                self._usage_tracker.track_final_answer(answer)
                logger.bind(message_type="final_answer").info(answer)

                self._finalized = True
                self._final_answer = answer
                self._final_status = status

                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name="final_answer",
                        content=answer,
                    )
                )
                return

            # Meta-tool - handle locally
            if name in META_TOOL_NAMES and (self.use_toolbelt or name == "todo_write"):
                logger.bind(
                    message_type="tool_call",
                    ref=tool_call.id,
                    name=name,
                    payload=tool_call.function.arguments,
                ).info(f"Meta-tool: {name}")
                assert self.meta_tool_handler
                result = self.meta_tool_handler.handle(
                    name, tool_call.function.arguments
                )
                logger.bind(
                    message_type="tool_result",
                    ref=tool_call.id,
                    name=name,
                    payload=result,
                ).info(f"Meta-tool {name} completed")
                self.messages.append(
                    LitellmOutputMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        name=name,
                        content=result,
                    )
                )
                continue

            # MCP tool - collect for batch execution
            mcp_tool_calls.append(tool_call)

        # Execute MCP tools (using shared client connection)
        deferred_image_messages: list[LitellmInputMessage] = []
        for tool_call in mcp_tool_calls:
            self._progress_begin(f"tool:{tool_call.function.name}")
            await self._execute_mcp_tool(client, tool_call, deferred_image_messages)
            self._progress_end()
        self.messages.extend(deferred_image_messages)

    async def _execute_mcp_tool(
        self,
        client: Any,
        tool_call: Any,
        deferred_image_messages: list[LitellmInputMessage],
    ) -> None:
        """Execute an MCP tool call."""
        name = tool_call.function.name

        if name not in self.toolbelt:
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=(
                        f"Error: '{name}' not in toolbelt. Use toolbelt_add_tool first."
                        if self.use_toolbelt
                        else f"Error: unknown tool '{name}'."
                    ),
                )
            )
            return
        if self.monitor is not None:
            self._dispatched.append((name, tool_call.function.arguments))

        tool_logger = logger.bind(
            ref=tool_call.id,
            name=name,
        )
        tool_logger.bind(
            message_type="tool_call",
            payload=tool_call.function.arguments,
        ).info(f"Calling tool {name}")

        tool_result_logger = tool_logger.bind(message_type="tool_result")

        retried_after: Exception | None = None
        timeout_retried = False
        reconnected = False
        while True:
            if self.monitor is not None:
                # A tool attempt starting is liveness, as an LLM attempt is.
                self.monitor.heartbeat()
            shielded_task = asyncio.ensure_future(
                self._call_mcp_tool(client, tool_call)
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(shielded_task),
                    timeout=self.tool_call_timeout,
                )
                break
            except Exception as e:
                probed = False
                if (
                    isinstance(e, TimeoutError)
                    and not timeout_retried
                    and self._may_retry_timeout(name)
                ):
                    probed = True
                    _, healthy = await asyncio.gather(
                        drain_shielded_task(shielded_task),
                        probe_gateway_health(
                            self._gateway_schema, reason="tool_timeout"
                        ),
                    )
                    if healthy:
                        self._timeout_retries_left -= 1
                        timeout_retried = True
                        retried_after = retried_after or e
                        continue
                elif (
                    not isinstance(e, TimeoutError)
                    and not reconnected
                    and await self._reconnect_mcp(client, tool_call, e)
                ):
                    reconnected = True
                    replay_after_5xx = (
                        is_gateway_5xx(e) and name in self._replay_safe_tools
                    )
                    if mcp_call_never_sent(e) or replay_after_5xx:
                        if replay_after_5xx:
                            await wait_before_gateway_5xx_replay()
                        retried_after = retried_after or e
                        continue
                    self._log_mcp_tool_retry(name, e, "not_retried")
                    self.messages.append(
                        LitellmOutputMessage(
                            role="tool",
                            tool_call_id=tool_call.id,
                            name=name,
                            content=MCP_CALL_MAY_HAVE_RUN,
                        )
                    )
                    return
                if retried_after is not None:
                    self._log_mcp_tool_retry(name, retried_after, "failed", e)
                await self._handle_mcp_tool_error(
                    tool_call, e, shielded_task, tool_result_logger, probe=not probed
                )
                return
        if retried_after is not None:
            self._log_mcp_tool_retry(name, retried_after, "succeeded")

        # Empty content is a valid answer (a tool returning [] or None), not a failure.
        tool_content = result.content or empty_tool_result_content(
            result, name, tool_result_logger
        )

        log_mcp_tool_result(
            tool_result_logger,
            name,
            result,
            [block.model_dump() for block in tool_content],
        )

        messages = content_blocks_to_messages(
            tool_content,
            tool_call.id,
            name,
            self.model,
            deferred_image_messages=deferred_image_messages,
        )
        truncate_tool_messages(messages, self.model)
        self.messages.extend(messages)

    def _may_retry_timeout(self, name: str) -> bool:
        return (
            self._timeout_retry_enabled
            and self._timeout_retries_left > 0
            and name in self._replay_safe_tools
        )

    async def _call_mcp_tool(self, client: Any, tool_call: Any) -> Any:
        if not self._mcp_reconnect_enabled:
            return await call_openai_tool(client.session, tool_call)
        # Session-monitored, so a dead transport raises now instead of hanging to the tool timeout.
        request = transform_openai_tool_call_request_to_mcp_tool_call_request(
            openai_tool=tool_call
        )
        return await trace_tool(
            request.name,
            request.arguments or {},
            lambda: client.call_tool_mcp(request.name, request.arguments or {}),
        )

    async def _reconnect_mcp(
        self, client: Any, tool_call: Any, error: Exception
    ) -> bool:
        """Re-acquire the MCP session after a transport failure; False when not applicable."""
        name = tool_call.function.name
        if not (
            self._mcp_reconnect_enabled
            and self._mcp_reconnects_left
            and is_gateway_connection_error(error)
        ):
            return False
        self._mcp_reconnects_left -= 1
        try:
            await reconnect_mcp_session(client)
        except Exception as reconnect_error:
            self._log_mcp_tool_retry(name, error, "reconnect_failed", reconnect_error)
            if is_gateway_5xx(error):
                return False
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=f"Fatal error: {reconnect_error}",
                )
            )
            raise
        return True

    def _log_mcp_tool_retry(
        self,
        name: str,
        error: Exception,
        outcome: str,
        final_error: Exception | None = None,
    ) -> None:
        logger.bind(
            message_type="tool_result",
            mcp_tool_retry=True,
            mcp_tool_retry_outcome=outcome,
            tool_name=name,
            error_repr=repr(error),
            final_error_repr=repr(final_error) if final_error else None,
        ).warning(f"MCP tool {name} after {error!r}: {outcome}")

    async def _handle_mcp_tool_error(
        self,
        tool_call: Any,
        e: Exception,
        shielded_task: asyncio.Task[Any],
        tool_result_logger: Any,
        *,
        probe: bool = True,
    ) -> None:
        name = tool_call.function.name
        if isinstance(e, TimeoutError):
            tool_result_logger.error(f"Tool call {name} timed out")
            if probe:
                await asyncio.gather(
                    drain_shielded_task(shielded_task),
                    probe_gateway_health(self._gateway_schema, reason="tool_timeout"),
                )
            else:
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
        if is_fatal_mcp_error(e):
            tool_result_logger.error(f"Fatal MCP error, ending run: {repr(e)}")
            self.messages.append(
                LitellmOutputMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    name=name,
                    content=f"Fatal error: {e}",
                )
            )
            raise e
        tool_result_logger.error(f"Error calling tool {name}: {repr(e)}")
        self.messages.append(
            LitellmOutputMessage(
                role="tool",
                tool_call_id=tool_call.id,
                name=name,
                content=f"Error: {e}",
            )
        )

    def _build_output(self) -> AgentTrajectoryOutput:
        markers = self._stop_markers
        if self.monitor is not None:
            # Every end of a progress-mode run carries its record, including
            # exceptions, cancellation and a stray TimeoutError.
            markers = {**markers, "progress": self._progress_summary()}
        return AgentTrajectoryOutput(
            messages=self.resum.get_full_history(self.messages),
            output={
                **self.resum.compaction_output(self.messages),
                **self.toolsets.output(),
                **markers,
            }
            or None,
            status=self.status,
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=self._usage_tracker.to_dict(),
        )

    def _fail_truncated(self) -> None:
        logger.bind(
            fault=budget_exhausted(
                Budget.OUTPUT_TOKENS,
                self._truncated_completion_tokens,
            )
        ).error(
            f"Model output truncated: {self._consecutive_truncations} "
            "replies in a row hit the output token limit "
            f"({self._truncated_completion_tokens} tokens) "
            "before any tool call completed"
        )
        self.status = AgentStatus.FAILED

    def _progress_config_failure(
        self, error: ProgressConfigError
    ) -> AgentTrajectoryOutput:
        """A progress setting that cannot run: deterministic, so never retried."""
        self.start_time = time.time()
        logger.bind(
            fault={
                "kind": FaultKind.HARNESS_CONTRACT.value,
                "descriptor": error.descriptor,
            }
        ).error(f"Invalid agent config: {error}")
        self.status = AgentStatus.ERROR
        return self._build_output()

    async def _run_with_progress(self, client: Any) -> AgentTrajectoryOutput:
        """The step loop under progress_mode.

        The base wall clock and step cap are a floor: at either, a run that is
        still progressing is extended by one chunk, up to the hard ceilings.
        Loops and stalls stop the run early. See runner/utils/progress_monitor.py.
        """
        assert self.policy is not None and self.monitor is not None
        policy, monitor = self.policy, self.monitor
        started = monitor.clock()
        hard_deadline = started + policy.hard_wall_clock_s
        deadline = min(started + policy.base_wall_clock_s, hard_deadline)
        self._progress_wall_clock_s = deadline - started
        stall = monitor.stall_timer()
        # Each LLM attempt start resets the stall timer, so a timed-out call
        # that with_retry re-sends is not a stall (see LLM_RETRY_BACKOFF_MAX_S).
        attempt_hook = (
            llm_attempt_hook_ctx.set(monitor.heartbeat) if stall is not None else None
        )
        try:
            async with stall or contextlib.nullcontext():
                while not self._finalized:
                    step = self._progress_step
                    if self._truncation_limit_reached():
                        break
                    if policy.loop_detection and monitor.verdict is not None:
                        return self._end_progress("loop")
                    if step >= self._progress_step_budget:
                        budget = self._progress_step_budget
                        # Read at the hard cap too, for the output's verdict.
                        progressing = monitor.progressing("steps")
                        if budget >= policy.hard_max_steps or not progressing:
                            return self._end_progress("steps", progressing=progressing)
                        self._progress_step_budget = min(
                            budget + policy.extension_steps, policy.hard_max_steps
                        )
                        monitor.note_extension("steps", self._progress_step_budget)
                    now = monitor.clock()
                    if now >= deadline:
                        # Read at the ceiling too: whether the ceiling cut a
                        # still-progressing run goes in the output.
                        at_ceiling = now >= hard_deadline
                        progressing = monitor.progressing("wall_clock")
                        if at_ceiling or not progressing:
                            return self._end_progress(
                                "wall_clock",
                                at_ceiling=at_ceiling,
                                progressing=progressing,
                            )
                        deadline = min(deadline + policy.extension_s, hard_deadline)
                        self._progress_wall_clock_s = deadline - started
                        monitor.note_extension("wall_clock", deadline - started)
                    logger.bind(message_type="step").info(f"Starting step {step + 1}")
                    await self.step(client)
                    self._progress_step = step + 1
        except TimeoutError:
            if not monitor.stalled:
                raise
            return self._end_progress("stall")
        except _LlmCallGaveUp as gave_up:
            return self._end_progress("stall", gave_up=gave_up)
        finally:
            if attempt_hook is not None:
                llm_attempt_hook_ctx.reset(attempt_hook)
        if self._finalized:
            logger.info(f"Finalized after {self._progress_step} steps")
            self.status = AgentStatus.COMPLETED
        else:
            self._fail_truncated()
        return self._ended_output()

    def _progress_summary(self) -> dict[str, Any]:
        assert self.monitor is not None
        return self.monitor.summary(
            steps=self._progress_step,
            step_budget=self._progress_step_budget,
            wall_clock_s=self._progress_wall_clock_s,
            ceiling_hit=self._ceiling_hit,
            step_ceiling_hit=self._step_ceiling_hit,
            progressing_at_stop=self._progressing_at_stop,
        )

    def _ended_output(self) -> AgentTrajectoryOutput:
        """The output of a recorded progress-mode end.

        Marked ended only once the output is built, so a failure building it
        takes run()'s ordinary error path, not the teardown one.
        """
        output = self._build_output()
        self._progress_ended = True
        return output

    def _end_progress(
        self,
        end: str,
        *,
        gave_up: _LlmCallGaveUp | None = None,
        at_ceiling: bool = False,
        progressing: bool | None = None,
    ) -> AgentTrajectoryOutput:
        """Stop a progress-mode run: status, fault and output markers per end.

        steps, wall_clock and loop are model outcomes (FAILED, scored, never
        respawned); a stall is ERROR unless the agent sets stall_status=failed.
        `at_ceiling` is a wall_clock stop at the hard wall ceiling, not one
        below it for want of progress (wall_clock_hard always is), and
        `progressing` the progress verdict at a step or wall-clock stop, read
        here when not given.
        `gave_up` is a model call whose retries ran out on timeouts, a stall
        with "llm" in flight. Every stop passes here and sets
        `attempt_outcome`; with snapshot_on_stop on, a step, wall-clock or loop
        stop also sets the `stop_snapshot` marker (ProgressPolicy.stop_markers).
        """
        assert self.policy is not None and self.monitor is not None
        step = self._progress_step
        markers: dict[str, Any]
        if end == "steps":
            limit = self._progress_step_budget
            # The fault text stays as is (callers match it whole); a separate
            # line tells a stop at the hard step cap from a no-progress stop.
            self._step_ceiling_hit = limit >= self.policy.hard_max_steps
            self._progressing_at_stop = (
                self.monitor.progressing("steps")
                if progressing is None
                else progressing
            )
            if self._step_ceiling_hit:
                logger.bind(message_type="progress").info(
                    f"progress-aware step ceiling {limit} "
                    f"(progressing={self._progressing_at_stop})"
                )
            else:
                logger.bind(message_type="progress").info(
                    f"no progress in the last {self.policy.window_s:g}s at {limit} steps"
                )
            logger.bind(fault=budget_exhausted(Budget.STEPS, limit)).error(
                f"Not finalized after {limit} steps"
            )
            self.status = AgentStatus.FAILED
            markers = budget_limited_attempt_marker(AttemptOutcome.MAX_STEPS)
        elif end in ("wall_clock", "wall_clock_hard"):
            # Unlike the default path's ERROR: the run had its time, so a
            # wall-clock end is scored and not respawned from scratch.
            # A stop at the hard wall ceiling (at a step boundary, or the
            # backstop cutting a step short) and one below it for want of
            # progress log different reasons, so ceiling hits can be counted.
            hard = end == "wall_clock_hard"
            ceiling = hard or at_ceiling
            limit = round(
                self.policy.hard_wall_clock_s
                if ceiling
                else self._progress_wall_clock_s
            )
            if ceiling:
                hours = round(self.policy.hard_wall_clock_s / 3600, 1)
                why = f"progress-aware ceiling {hours:g}h ({self.policy.ceiling_limit})"
            else:
                why = f"no progress in the last {self.policy.window_s:g}s"
            self._ceiling_hit = ceiling
            self._progressing_at_stop = (
                self.monitor.progressing("wall_clock")
                if progressing is None
                else progressing
            )
            logger.bind(fault=budget_exhausted(Budget.WALL_CLOCK, limit)).error(
                f"Timeout after {limit}s: {why}"
            )
            self.status = AgentStatus.FAILED
            markers = budget_limited_attempt_marker(
                AttemptOutcome.WALL_BUDGET_HARD if hard else AttemptOutcome.WALL_BUDGET
            )
        elif end == "loop":
            verdict = self.monitor.verdict
            assert verdict is not None
            logger.bind(
                fault=loop_detected(
                    verdict.rule,
                    k=self.policy.loop_repeat_k,
                    window=self.policy.loop_window_steps,
                    step=step,
                    tool=verdict.tool,
                )
            ).error(f"Loop detected at step {step}: {verdict.detail}")
            self.status = AgentStatus.FAILED
            markers = {"attempt_outcome": LOOP_OUTCOME}
        else:
            if gave_up is not None:
                self.monitor.begin("llm")
            status, fault = self.monitor.stall_fault()
            in_flight = self.monitor.in_flight or "nothing"
            if gave_up is not None:
                logger.bind(fault=fault).error(
                    f"Stalled at step {step + 1}: the model call ran out of "
                    f"retries on {gave_up} (in flight: {in_flight})"
                )
            else:
                logger.bind(fault=fault).error(
                    f"Stalled at step {step + 1}: nothing completed for "
                    f"{self.policy.stall_window_s:g}s (in flight: {in_flight})"
                )
            self.status = AgentStatus(status)
            markers = {"attempt_outcome": STALL_OUTCOME}
        # snapshot_on_stop: modal_labs snapshots this FAILED run's end state,
        # and with grade_on_stop Studio grades it (steps, wall clock, loop).
        self._stop_markers = {**markers, **self.policy.stop_markers(end)}
        return self._ended_output()

    async def run(self) -> AgentTrajectoryOutput:
        """Run the agent loop with a single MCP connection."""
        if self._progress_config_error is not None:
            return self._progress_config_failure(self._progress_config_error)
        backstop: asyncio.Timeout | None = None
        try:
            # Built inside the try, as before progress mode: an unusable
            # timeout (a string) is the run's error, not run()'s.
            # Progress mode: the hard ceiling plus one call's grace, clamped
            # below the runner's AGENT_TIMEOUT_SECONDS (ProgressPolicy).
            backstop = asyncio.timeout(
                self.policy.backstop_s if self.policy else self.timeout
            )
            async with backstop:
                # Single MCP connection for entire agent lifecycle
                async with self.mcp_client as client:
                    logger.info(f"Starting ReAct Toolbelt agent with {self.model}")
                    await self._initialize_tools(client)
                    self.start_time = time.time()
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

                    self.status = AgentStatus.RUNNING
                    if self.monitor is not None:
                        return await self._run_with_progress(client)

                    for step in range(self.max_steps):
                        if self._finalized:
                            logger.info(f"Finalized after {step} steps")
                            break
                        if self._truncation_limit_reached():
                            break
                        logger.bind(message_type="step").info(
                            f"Starting step {step + 1}"
                        )
                        await self.step(client)

                    if not self._finalized and self._truncation_limit_reached():
                        self._fail_truncated()
                    elif not self._finalized:
                        logger.bind(
                            fault=budget_exhausted(Budget.STEPS, self.max_steps)
                        ).error(f"Not finalized after {self.max_steps} steps")
                        self.status = AgentStatus.FAILED
                    else:
                        self.status = AgentStatus.COMPLETED

                    return self._build_output()

        except TimeoutError:
            if self.monitor is not None and self._progress_ended:
                # The end is already recorded; the backstop fired while the MCP
                # session closed. Keep it rather than overwrite it with ERROR.
                logger.warning(
                    "Backstop reached while closing the MCP session; keeping "
                    f"the recorded {self.status} end"
                )
                return self._build_output()
            if (
                self.monitor is not None
                and self.status == AgentStatus.RUNNING
                and backstop is not None
                and backstop.expired()
            ):
                # The backstop cut a step short past the hard wall ceiling.
                return self._end_progress("wall_clock_hard")
            logger.bind(fault=budget_exhausted(Budget.WALL_CLOCK, self.timeout)).error(
                f"Timeout after {self.timeout}s"
            )
            self.status = AgentStatus.ERROR
            return self._build_output()

        except asyncio.CancelledError:
            logger.error("Cancelled")
            self.status = AgentStatus.CANCELLED
            return self._build_output()

        except Exception as e:
            if self.monitor is not None and self._progress_ended:
                # Only teardown runs after an end is recorded (_ended_output),
                # so this is the MCP session failing to close: keep that end.
                logger.warning(
                    f"Error closing the MCP session: {e!r}; keeping the recorded "
                    f"{self.status} end"
                )
                return self._build_output()
            logger.error(f"Error: {e!r}")
            self.status = (
                AgentStatus.ERROR if is_system_error(e) else AgentStatus.FAILED
            )
            if is_gateway_connection_error(e):
                await probe_gateway_health(
                    self._gateway_schema, reason=type(e).__name__
                )
            return self._build_output()


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """Entry point for the ReAct Toolbelt agent."""
    return await ReActAgent(run_input).run()
