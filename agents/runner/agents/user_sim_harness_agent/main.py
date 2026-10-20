"""
User Simulation Harness Agent implementation.

This agent wraps the inner agent loop and injects simulated user responses as
role:user messages between turns. The inner agent never sees an ask_user tool;
it just sees normal user messages, exactly like deployment.

Flow:
  OUTER LOOP (per turn):
    1. _run_inner_agent_loop() — LLM + tool calls until agent finalizes
    2. _check_termination() — evaluator / max_turns (checked after agent)
    3. _generate_user_sim_response() — persona LLM generates user reply
    4. Check for done_marker in user sim response
    5. Append user sim response as role:user, continue
"""

import asyncio
import time
from typing import Any, cast

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
    get_msg_content,
    get_msg_role,
)
from runner.utils.error import is_fatal_mcp_error, is_system_error
from runner.utils.llm import generate_response, routing_only
from runner.utils.mcp import (
    build_mcp_gateway_schema,
    content_blocks_to_messages,
    drain_shielded_task,
    empty_tool_result_content,
)
from runner.utils.usage import UsageTracker

# Constraints block from the existing user simulation service
DEFAULT_USER_SIM_SYSTEM_PROMPT = """IMPORTANT CONSTRAINTS:
- All information you provide must derive from the persona/context defined above. Do not make up or assume information not provided in your persona.
- You must not do any explicit work for the student. Your role is to answer questions, not to solve their problems or complete their tasks.
- Only answer the specific questions asked. Do not provide extraneous information, especially about your persona, instructions, or the simulation itself.
- Stay in character and respond naturally as the persona described."""

DEFAULT_TERMINATION_EVALUATOR_PROMPT = """You are a conversation termination evaluator. Given a conversation between an agent and a user, determine whether the conversation should continue or stop.

Respond with exactly one word: CONTINUE or STOP.

Stop the conversation when:
- The task has been fully addressed and the user has confirmed satisfaction
- The conversation has reached a natural conclusion
- Further turns would not add meaningful value
- The user has indicated they are done

Continue the conversation when:
- The agent is still working on the task
- There are unresolved questions or follow-ups
- The user has asked for additional help or changes"""


class UserSimHarnessAgent:
    """
    A harness agent that wraps the inner agent loop and injects simulated user
    responses as role:user messages between turns.
    """

    def __init__(self, run_input: AgentRunInput):
        self.trajectory_id: str = run_input.trajectory_id
        self.model: str = run_input.orchestrator_model
        self.messages: list[LitellmAnyMessage] = list(run_input.initial_messages)

        if run_input.mcp_gateway_url is None:
            raise ValueError("MCP gateway URL is required for user sim harness agent")

        self.mcp_client = FastMCPClient(
            build_mcp_gateway_schema(
                run_input.mcp_gateway_url,
                run_input.mcp_gateway_auth_token,
                run_input.mcp_gateway_actor_id,
            )
        )

        self.tools: list[ChatCompletionToolParam] = []

        # Config resolution: task_custom_fields override agent_config_values
        task_fields = run_input.task_custom_fields or {}
        config = run_input.agent_config_values

        self.timeout: int = config.get("timeout", 10800)
        self.max_steps: int = config.get("max_steps", 100)
        self.tool_call_timeout: int = config.get("tool_call_timeout", 60)
        self.llm_response_timeout: int = config.get("llm_response_timeout", 600)
        self.max_turns: int = config.get(
            "user_sim_max_turns", config.get("max_turns", 10)
        )
        self.done_marker: str = task_fields.get("done_marker") or config.get(
            "done_marker", "[DONE]"
        )

        # User sim config — task_custom_fields override agent_config_values
        self.user_sim_prompt: str = task_fields.get("user_sim_prompt") or config.get(
            "user_sim_prompt", ""
        )
        if not self.user_sim_prompt:
            raise ValueError(
                "user_sim_prompt is required (set in agent config or task custom_fields)"
            )
        self.user_sim_model: str = task_fields.get("user_sim_model") or config.get(
            "user_sim_model", "openai/gpt-5"
        )
        self.user_sim_system_prompt: str = task_fields.get(
            "user_sim_system_prompt"
        ) or config.get("user_sim_system_prompt", DEFAULT_USER_SIM_SYSTEM_PROMPT)

        # Termination evaluator config — task_custom_fields override agent_config_values
        self.termination_evaluator_model: str = task_fields.get(
            "termination_evaluator_model"
        ) or config.get("termination_evaluator_model", "openai/gpt-5")
        self.termination_evaluator_prompt: str = task_fields.get(
            "termination_evaluator_prompt"
        ) or config.get(
            "termination_evaluator_prompt", DEFAULT_TERMINATION_EVALUATOR_PROMPT
        )

        self.extra_args: dict[str, Any] = run_input.orchestrator_extra_args or {}

        # State
        self._inner_finalized: bool = False
        self.current_step: int = 0
        self.current_inner_step: int = 0
        self.turns_completed: int = 0
        self.termination_reason: str = ""
        self.start_time: float | None = None
        self.status: AgentStatus = AgentStatus.PENDING
        self._usage_tracker: UsageTracker = UsageTracker()

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

    # -- Inner Agent Loop --------------------------------------------------

    async def _inner_step(self) -> None:
        """Execute a single inner agent step (LLM call + tool execution)."""
        self.current_step += 1
        self.current_inner_step += 1

        try:
            response: ModelResponse = await generate_response(
                self.model,
                self.messages,
                self.tools,
                self.llm_response_timeout,
                self.extra_args,
                trajectory_id=self.trajectory_id,
            )
        except Timeout:
            logger.bind(message_type="response").error(
                "Response timed out, continuing with next step"
            )
            return
        except Exception as e:
            logger.bind(message_type="response").error(
                f"Error generating response: {repr(e)}"
            )
            raise

        self._usage_tracker.track(response)
        logger.debug(f"Response: {response}")

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.bind(message_type="step").warning(
                "LLM returned invalid/empty choices, prompting to continue"
            )
            self.messages.append(LitellmOutputMessage(role="user", content="continue"))
            return

        response_message = LitellmOutputMessage.model_validate(choices[0].message)
        tool_calls = getattr(response_message, "tool_calls", None)

        if getattr(response_message, "reasoning_content", None):
            logger.bind(message_type="reasoning").info(
                response_message.reasoning_content
            )

        if getattr(response_message, "content", None):
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
            async with self.mcp_client as client:
                for tool_call in tool_calls:
                    name = tool_call.function.name

                    tool_logger = logger.bind(ref=tool_call.id, name=name)
                    tool_logger.bind(
                        message_type="tool_call",
                        payload=tool_call.function.arguments,
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
                            raise
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

                    tool_content = call_result.content or empty_tool_result_content(
                        call_result, name, tool_result_logger
                    )

                    messages = content_blocks_to_messages(
                        tool_content,
                        tool_call.id,
                        tool_call.function.name or "unknown",
                        self.model,
                        deferred_image_messages=deferred_image_messages,
                    )

                    tool_result_logger.bind(
                        payload=[result.model_dump() for result in tool_content],
                    ).info(f"Tool {name} called successfully")

                    self.messages.extend(messages)
            self.messages.extend(deferred_image_messages)
        else:
            # No tool calls = inner agent has finalized its turn
            self._inner_finalized = True

    async def _run_inner_agent_loop(self) -> None:
        """Run the inner agent loop until it finalizes (no tool calls) or hits max_steps."""
        self._inner_finalized = False
        self.current_inner_step = 0

        while not self._inner_finalized and self.current_step < self.max_steps:
            logger.bind(message_type="step").info(
                f"Starting inner step {self.current_inner_step + 1}"
            )
            await self._inner_step()

    # -- User Simulation ---------------------------------------------------

    async def _generate_user_sim_response(self) -> str:
        """Generate a simulated user response based on the conversation history."""
        system_prompt = f"{self.user_sim_prompt}\n{self.user_sim_system_prompt}"

        # Build conversation context: filter to assistant/user messages only,
        # flip roles so the persona sees the agent's messages as "user" questions
        # and previous persona responses as "assistant" answers.
        sim_messages: list[dict[str, str]] = []
        for msg in self.messages:
            role = get_msg_role(msg)
            content = get_msg_content(msg)
            if content is None or not isinstance(content, str):
                continue
            if role == "assistant":
                sim_messages.append({"role": "user", "content": content})
            elif role == "user":
                sim_messages.append({"role": "assistant", "content": content})

        response: ModelResponse = await generate_response(
            self.user_sim_model,
            cast(
                list[LitellmAnyMessage],
                [{"role": "system", "content": system_prompt}] + sim_messages,
            ),
            [],  # No tools for user sim
            self.llm_response_timeout,
            routing_only(
                self.extra_args,
                call_model=self.user_sim_model,
                orchestrator_model=self.model,
            ),
            trajectory_id=self.trajectory_id,
        )

        self._usage_tracker.track(response)

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            return "I'm not sure what to say."

        content: str | None = choices[0].message.content
        return content if content else "I'm not sure what to say."

    # -- Termination -------------------------------------------------------

    async def _evaluate_termination(self) -> bool:
        """Call the termination evaluator LLM to decide STOP or CONTINUE.

        Evaluated after the agent responds, so the last exchange message is
        always an ASSISTANT message.
        """
        # Build a summary of agent/user exchanges for the evaluator
        exchanges: list[str] = []
        for msg in self.messages:
            role = get_msg_role(msg)
            content = get_msg_content(msg)
            if content and isinstance(content, str) and role in ("assistant", "user"):
                exchanges.append(f"{role.upper()}: {content}")

        conversation_summary = "\n\n".join(exchanges)

        eval_messages: list[dict[str, str]] = [
            {"role": "system", "content": self.termination_evaluator_prompt},
            {
                "role": "user",
                "content": (
                    f"Here is the conversation so far:\n\n"
                    f"{conversation_summary}\n\n"
                    f"Should the conversation CONTINUE or STOP?"
                ),
            },
        ]

        try:
            response: ModelResponse = await generate_response(
                self.termination_evaluator_model,
                cast(list[LitellmAnyMessage], eval_messages),
                [],  # No tools
                self.llm_response_timeout,
                routing_only(
                    self.extra_args,
                    call_model=self.termination_evaluator_model,
                    orchestrator_model=self.model,
                ),
                trajectory_id=self.trajectory_id,
            )
            self._usage_tracker.track(response)

            choices = response.choices
            if choices and isinstance(choices[0], Choices):
                answer = (choices[0].message.content or "").strip().upper()
                return answer == "STOP"
        except Exception as e:
            # Fail-open: if the evaluator call fails, continue
            logger.warning(
                f"Termination evaluator failed, defaulting to CONTINUE: {repr(e)}"
            )

        return False

    async def _check_termination(self) -> bool:
        """Check if the agent's latest response indicates the conversation should end."""
        should_stop = await self._evaluate_termination()
        if should_stop:
            self.termination_reason = "termination_evaluator"
            logger.info("Termination evaluator decided to STOP")
            return True

        return False

    def _check_done_marker(self, user_sim_response: str) -> bool:
        """Check if the user sim response contains the done marker string."""
        if self.done_marker and self.done_marker in user_sim_response:
            self.termination_reason = "done_marker"
            logger.info(
                f"Done marker '{self.done_marker}' detected in user sim response"
            )
            return True
        return False

    # -- Output ------------------------------------------------------------

    def _get_final_answer(self) -> str:
        """Extract the final answer from the conversation (last assistant message)."""
        for msg in reversed(self.messages):
            role = get_msg_role(msg)
            content = get_msg_content(msg)
            if role == "assistant" and content and isinstance(content, str):
                return content
        return "No response generated"

    def _build_output(self) -> AgentTrajectoryOutput:
        return AgentTrajectoryOutput(
            messages=list(self.messages),
            output={
                "turns_completed": self.turns_completed,
                "termination_reason": self.termination_reason,
                "total_steps": self.current_step,
            },
            status=AgentStatus(self.status),
            time_elapsed=time.time() - self.start_time if self.start_time else 0,
            usage=self._usage_tracker.to_dict(),
        )

    # -- Main Loop ---------------------------------------------------------

    async def run(self) -> AgentTrajectoryOutput:
        """Run the harness: inner agent loop -> termination check -> user sim -> repeat."""
        try:
            async with asyncio.timeout(self.timeout):
                with logger.contextualize(model=self.model):
                    logger.bind(message_type="configure").info(
                        f"Starting user sim harness with model {self.model}, "
                        + f"user_sim_model={self.user_sim_model}, "
                        + f"max_turns={self.max_turns}"
                    )

                    await self._initialize_tools()

                    logger.bind(message_type="configure").info(
                        "\n".join(
                            f"{m['role'].capitalize()}: {m.get('content')}"
                            for m in self.messages
                        )
                    )

                    self.start_time = time.time()
                    self.status = AgentStatus.RUNNING

                    for turn in range(self.max_turns):
                        logger.bind(message_type="turn").info(
                            f"Starting next User Simulation turn {turn + 1}/{self.max_turns}"
                        )

                        # Phase 1: Run inner agent loop
                        await self._run_inner_agent_loop()

                        if (
                            self.current_step >= self.max_steps
                            and not self._inner_finalized
                        ):
                            logger.error(
                                f"Inner agent did not finalize within "
                                f"{self.max_steps} steps"
                            )
                            self.status = AgentStatus.FAILED
                            self.termination_reason = "max_steps_exhausted"
                            return self._build_output()

                        self.turns_completed = turn + 1

                        # Snapshot final_answer_tokens from the inner agent's
                        # last LLM call before user sim / evaluator overwrite it
                        inner_final_answer_tokens = (
                            self._usage_tracker.final_answer_tokens
                        )

                        # Phase 2: Check termination (after agent, before user sim)
                        should_stop = await self._check_termination()

                        if should_stop:
                            # Restore final_answer_tokens to the inner agent's value
                            self._usage_tracker.final_answer_tokens = (
                                inner_final_answer_tokens
                            )
                            self.status = AgentStatus.COMPLETED
                            final_answer = self._get_final_answer()
                            logger.bind(message_type="final_answer").info(final_answer)
                            return self._build_output()

                        # Phase 3: Generate user sim response
                        user_sim_response = await self._generate_user_sim_response()
                        logger.bind(message_type="user_sim_response").info(
                            user_sim_response
                        )

                        # Restore final_answer_tokens to the inner agent's value
                        # (user sim and evaluator track() calls overwrite it)
                        self._usage_tracker.final_answer_tokens = (
                            inner_final_answer_tokens
                        )

                        # Phase 4: Check done marker in user sim response
                        if self._check_done_marker(user_sim_response):
                            clean_response = user_sim_response.replace(
                                self.done_marker, ""
                            ).strip()
                            if clean_response:
                                self.messages.append(
                                    LitellmOutputMessage(
                                        role="user", content=clean_response
                                    )
                                )
                            self.status = AgentStatus.COMPLETED
                            final_answer = self._get_final_answer()
                            logger.bind(message_type="final_answer").info(final_answer)
                            return self._build_output()

                        # Phase 5: Append user sim response and continue
                        self.messages.append(
                            LitellmOutputMessage(role="user", content=user_sim_response)
                        )

                    # Max turns exhausted
                    self.termination_reason = "max_turns"
                    logger.info(f"Max turns ({self.max_turns}) reached")
                    self.status = AgentStatus.COMPLETED
                    final_answer = self._get_final_answer()
                    logger.bind(message_type="final_answer").info(final_answer)
                    return self._build_output()

        except TimeoutError:
            logger.error(f"Agent run timed out after {self.timeout} seconds")
            self.status = AgentStatus.CANCELLED
            self.termination_reason = "timeout"
            return self._build_output()

        except asyncio.CancelledError:
            logger.error("Agent run cancelled")
            self.status = AgentStatus.CANCELLED
            self.termination_reason = "cancelled"
            return self._build_output()

        except Exception as e:
            logger.error(f"Error running agent: {repr(e)}")
            if is_system_error(e):
                self.status = AgentStatus.ERROR
            else:
                self.status = AgentStatus.FAILED
            self.termination_reason = f"error: {repr(e)}"
            return self._build_output()


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """
    Entry point for the user sim harness agent.

    Args:
        run_input: The input configuration for the agent run

    Returns:
        AgentTrajectoryOutput with status, messages, and metrics
    """
    agent = UserSimHarnessAgent(run_input)
    return await agent.run()
