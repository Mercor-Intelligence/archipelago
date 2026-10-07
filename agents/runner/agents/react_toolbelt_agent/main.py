"""
ReAct Toolbelt Agent with ReSum Context Management.
"""

import asyncio
import time
from typing import Any

from fastmcp import Client as FastMCPClient
from litellm import Choices
from litellm.exceptions import ContextWindowExceededError, Timeout
from litellm.experimental_mcp_client import call_openai_tool, load_mcp_tools
from litellm.experimental_mcp_client.tools import (
    transform_openai_tool_call_request_to_mcp_tool_call_request,
)
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
    get_msg_attr,
    get_msg_content,
)
from runner.utils.context_budget import (
    compaction_timeout_from_config,
    context_budget_from_config,
    reported_prompt_tokens,
)
from runner.utils.error import (
    is_fatal_mcp_error,
    is_gateway_connection_error,
    is_system_error,
)
from runner.utils.file_staging import TurnFileStagingError, stage_message_files
from runner.utils.llm import generate_response
from runner.utils.logging.terminal_error import Budget, budget_exhausted
from runner.utils.mcp import (
    build_mcp_gateway_schema,
    content_blocks_to_messages,
    drain_shielded_task,
    empty_tool_result_content,
    mcp_call_never_sent,
    probe_gateway_health,
    reconnect_mcp_session,
)
from runner.utils.sandbox_files import SandboxUploadError
from runner.utils.settings import get_settings
from runner.utils.usage import UsageTracker

from .resum import ReSumManager
from .tool_result import truncate_tool_messages
from .tools import (
    FINAL_ANSWER_TOOL,
    META_TOOL_NAMES,
    META_TOOLS,
    TODO_WRITE_TOOL,
    MetaToolHandler,
    parse_final_answer,
)

# Bounds the extra LLM steps a flapping gateway can cost one run.
MCP_TOOL_RECONNECTS_PER_RUN = 3

MCP_CALL_MAY_HAVE_RUN = (
    "Error: the connection to the environment dropped while this tool call was "
    "in flight, so it may or may not have taken effect. The connection has been "
    "re-established; check the current state before repeating the call."
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


class ReActAgent:
    """ReAct Toolbelt Agent with ReSum context management."""

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
        config = run_input.agent_config_values
        self.timeout: int = config.get("timeout", 10800)
        self.max_steps: int = config.get("max_steps", 250)
        self.tool_call_timeout: int = config.get("tool_call_timeout", 60)
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)
        self.stage_files_to_env: bool = _coerce_bool(
            config.get("stage_files_to_env"), default=False
        )
        self._mcp_gateway_url: str = run_input.mcp_gateway_url
        self._mcp_gateway_auth_token: str | None = run_input.mcp_gateway_auth_token
        self.max_toolbelt_size: int = 80
        self.use_toolbelt: bool = _coerce_bool(config.get("use_toolbelt"), default=True)
        self._mcp_reconnect_enabled: bool = get_settings().MCP_TOOL_RECONNECT_ENABLED
        self._mcp_reconnects_left: int = MCP_TOOL_RECONNECTS_PER_RUN

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}

        # Components
        context_window_tokens, compaction_cutoff = context_budget_from_config(config)
        self.resum: ReSumManager = ReSumManager(
            self.model,
            self.extra_args,
            context_window_tokens=context_window_tokens,
            trigger_fraction=compaction_cutoff,
            summary_timeout=compaction_timeout_from_config(config),
        )

        # Toolbelt state
        self.all_tools: dict[str, ChatCompletionToolParam] = {}
        self.toolbelt: set[str] = set()
        self.meta_tool_handler: MetaToolHandler | None = None

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

    def _get_tools(self) -> list[ChatCompletionToolParam]:
        """Get tools for LLM: meta-tools + toolbelt + final_answer."""
        toolbelt_tools = [self.all_tools[name] for name in self.toolbelt]
        meta_tools = META_TOOLS if self.use_toolbelt else [TODO_WRITE_TOOL]
        return list(meta_tools) + toolbelt_tools + [FINAL_ANSWER_TOOL]

    async def _initialize_tools(self, client: Any) -> None:
        """Load tools from MCP gateway."""
        tools: list[ChatCompletionToolParam] = await load_mcp_tools(
            client.session, format="openai"
        )  # pyright: ignore[reportAssignmentType]

        for tool in tools:
            name = tool.get("function", {}).get("name")
            if name:
                self.all_tools[name] = tool

        if not self.use_toolbelt:
            self.toolbelt.update(self.all_tools)

        self.meta_tool_handler = MetaToolHandler(
            self.all_tools, self.toolbelt, self.max_toolbelt_size
        )

        mode = "toolbelt starts empty" if self.use_toolbelt else "toolbelt off"
        logger.bind(
            message_type="configure",
            payload=list(self.all_tools.keys()),
        ).info(f"Loaded {len(self.all_tools)} MCP tools ({mode})")

    async def step(self, client: Any) -> None:
        """Execute one step of the ReAct loop."""
        # Proactive ReSum check
        if await asyncio.to_thread(self.resum.should_summarize, self.messages):
            logger.bind(message_type="resum").info("Summarizing context")
            try:
                before = len(self.messages)
                self.messages = await self.resum.summarize(self.messages)
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
        try:
            response: ModelResponse = await generate_response(
                self.model,
                sent_messages,
                self._get_tools(),
                self.llm_response_timeout,
                self.extra_args,
                trajectory_id=self.trajectory_id,
            )
        except ContextWindowExceededError as e:
            logger.warning("Context exceeded, summarizing")
            # The provider just contradicted the projection for this exact
            # list; recalibrate so the proactive trigger fires earlier next time.
            await asyncio.to_thread(self.resum.observe_overflow, sent_messages, e)
            before = len(self.messages)
            self.messages = await self.resum.summarize(self.messages)
            if len(self.messages) < before:
                self._usage_tracker.track_compaction()
            return
        except Timeout:
            logger.error("LLM timeout")
            return
        except Exception as e:
            logger.error(f"LLM error: {e}")
            raise

        counts = await asyncio.to_thread(self._usage_tracker.breakdown_counts, response)
        self._usage_tracker.track(response, counts)
        await asyncio.to_thread(
            self.resum.observe_llm_usage,
            sent_messages,
            reported_prompt_tokens(response),
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
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

        step_msg_start = len(self.messages)
        self.messages.append(response_message)

        try:
            if tool_calls:
                await self._handle_tool_calls(client, tool_calls)
            else:
                self.messages.append(
                    LitellmOutputMessage(
                        role="user",
                        content="No tools called. Use final_answer to submit your answer. Please continue completing the task.",
                    )
                )
        finally:
            # Attribute this step's tool-result tokens even if tool handling
            # raised (partial results still count).
            self._record_step_tool_output(step_msg_start)

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

        for tool_call in tool_calls:
            name = tool_call.function.name

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
            await self._execute_mcp_tool(client, tool_call, deferred_image_messages)
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
        while True:
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
                if retried_after is not None:
                    self._log_mcp_tool_retry(name, retried_after, "failed", e)
                elif not isinstance(e, TimeoutError) and await self._reconnect_mcp(
                    client, tool_call, e
                ):
                    if mcp_call_never_sent(e):
                        retried_after = e
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
                await self._handle_mcp_tool_error(
                    tool_call, e, shielded_task, tool_result_logger
                )
                return
        if retried_after is not None:
            self._log_mcp_tool_retry(name, retried_after, "succeeded")

        # Empty content is a valid answer (a tool returning [] or None), not a failure.
        tool_content = result.content or empty_tool_result_content(
            result, name, tool_result_logger
        )

        tool_result_logger.bind(
            payload=[block.model_dump() for block in tool_content],
        ).info(f"Tool {name} called successfully")

        messages = content_blocks_to_messages(
            tool_content,
            tool_call.id,
            name,
            self.model,
            deferred_image_messages=deferred_image_messages,
        )
        truncate_tool_messages(messages, self.model)
        self.messages.extend(messages)

    async def _call_mcp_tool(self, client: Any, tool_call: Any) -> Any:
        if not self._mcp_reconnect_enabled:
            return await call_openai_tool(client.session, tool_call)
        # Session-monitored, so a dead transport raises now instead of hanging to the tool timeout.
        request = transform_openai_tool_call_request_to_mcp_tool_call_request(
            openai_tool=tool_call
        )
        return await client.call_tool_mcp(request.name, request.arguments or {})

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
    ) -> None:
        name = tool_call.function.name
        if isinstance(e, TimeoutError):
            tool_result_logger.error(f"Tool call {name} timed out")
            await asyncio.gather(
                drain_shielded_task(shielded_task),
                probe_gateway_health(self._gateway_schema, reason="tool_timeout"),
            )
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
        return AgentTrajectoryOutput(
            messages=self.resum.get_full_history(self.messages),
            status=self.status,
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=self._usage_tracker.to_dict(),
        )

    async def run(self) -> AgentTrajectoryOutput:
        """Run the agent loop with a single MCP connection."""
        try:
            async with asyncio.timeout(self.timeout):
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

                    for step in range(self.max_steps):
                        if self._finalized:
                            logger.info(f"Finalized after {step} steps")
                            break
                        logger.bind(message_type="step").info(
                            f"Starting step {step + 1}"
                        )
                        await self.step(client)

                    if not self._finalized:
                        logger.bind(
                            fault=budget_exhausted(Budget.STEPS, self.max_steps)
                        ).error(f"Not finalized after {self.max_steps} steps")
                        self.status = AgentStatus.FAILED
                    else:
                        self.status = AgentStatus.COMPLETED

                    return self._build_output()

        except TimeoutError:
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
