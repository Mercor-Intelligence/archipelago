"""
Context management for the Stirrup agent
"""

import itertools
from collections.abc import Callable
from itertools import takewhile
from typing import Any

from litellm import Choices, token_counter
from litellm.exceptions import ContextWindowExceededError
from loguru import logger

from runner.agents.models import (
    LitellmAnyMessage,
    LitellmOutputMessage,
    content_to_str,
    get_msg_attr,
    get_msg_content,
    get_msg_role,
)
from runner.agents.responses_agent_v2.main import parse_responses_api_output
from runner.utils.context_budget import (
    DEFAULT_COMPACTION_CUTOFF,
    PromptTokenProjector,
    observe_provider_overflow,
    overflow_numbers,
    resolve_context_window,
)
from runner.utils.llm import (
    _is_context_window_error,
    call_responses_api,
    generate_response,
)

# from the OS repo for Stirrup
SUMMARIZATION_PROMPT = """The context window is approaching its limit. Please create a concise summary of the conversation so far to preserve important information.

Your summary should include:

1. **Task Overview**: What is the main goal or objective?

2. **Progress Made**: What has been accomplished so far?
   - Key files created/modified (with paths)
   - Important functions/classes implemented
   - Tools used and their outcomes

3. **Current State**: Where are we now?
   - What is currently working?
   - What has been tested/verified?

4. **Next Steps**: What still needs to be done?
   - Outstanding TODOs (with specific file paths and line numbers if applicable)
   - Known issues or bugs to address
   - Features or functionality not yet implemented

5. **Important Context**: Any critical details that shouldn't be lost
   - Special configurations or setup requirements
   - Important variable names, API endpoints, or data structures
   - Edge cases or constraints to keep in mind
   - Dependencies or relationships between components

Keep the summary concise but comprehensive. Do not use any tools. Focus on actionable information that will allow smooth continuation of the work."""

# Stirrup's exact bridge prompt from prompts/message_summarizer_bridge.txt
BRIDGE_PROMPT = """**Context Continuation**

Due to context window limitations, the previous conversation has been summarized. Below is a summary of what happened before:

---

{summary}

---

You should continue working on this task from where it was left off. All the progress, current state, and next steps are described in the summary above. Proceed with completing any outstanding work."""

# Stirrup's prompts/message_summarizer_text_only.txt
SUMMARIZATION_TEXT_ONLY_PROMPT = "IMPORTANT: Respond with the summary as plain prose text only. Do NOT call any tools — a tool call cannot serve as a summary and will cause the summarization to fail."

_BRIDGE_PREFIX = BRIDGE_PROMPT.split("{summary}", 1)[0]
ACKNOWLEDGEMENT = "Got it, thanks!"

SUMMARIZE_FLATTENED = "flattened"
SUMMARIZE_IN_CONVERSATION = "in_conversation"
SUMMARIZATION_MODES = frozenset({SUMMARIZE_FLATTENED, SUMMARIZE_IN_CONVERSATION})


class StirrupContextManager:
    """
    Stirrup-style context manager

    Triggers summarization when context reaches ``summarization_cutoff`` of max tokens

    Uses Stirrup's exact prompts for summarization and bridge messages
    """

    _CHAT_COMPLETIONS_ONLY_KEYS = {"ensure_alternating_roles"}

    def __init__(
        self,
        model: str,
        extra_args: dict[str, Any] | None = None,
        stream: bool = True,
        on_llm_call: Callable[[Any, str], None] | None = None,
        context_window_tokens: int | None = None,
        summarization_cutoff: float = DEFAULT_COMPACTION_CUTOFF,
        summarization_mode: str = SUMMARIZE_FLATTENED,
        tools_provider: Callable[[], list[Any]] | None = None,
    ):
        self.model: str = model
        # Responses-API models stay flattened: no Responses message conversion here.
        self.summarization_mode: str = summarization_mode
        self.tools_provider: Callable[[], list[Any]] | None = tools_provider
        # By identity, not content: a task may quote the bridge text.
        self._bridges: list[LitellmAnyMessage] = []
        self.extra_args: dict[str, Any] = extra_args or {}
        # Called with (response, model) after each summarization call so the
        # owner can fold this spend into its own accounting. Summarization is a
        # real LLM call; without this a budget cap can be evaded by it.
        self.on_llm_call: Callable[[Any, str], None] | None = on_llm_call
        # Stream the summarization call for the same reason the main loop
        # streams (see StirrupAgent.__init__): summarization runs on a
        # near-full context, so a non-streaming call holds the connection idle
        # long enough to risk the load balancer's idle-timeout teardown.
        self.stream: bool = stream
        self.summarization_cutoff: float = summarization_cutoff
        window = resolve_context_window(self._lookup_model, context_window_tokens)
        self.max_tokens: int = window.tokens
        self.context_window_source: str = window.source
        # The loop must feed provider usage back via observe_llm_usage / observe_overflow, or the projector keeps its default 1.5x multiplier.
        self._projector: PromptTokenProjector = PromptTokenProjector(self._lookup_model)
        self.measurement: str = "projected_provider_tokens"
        logger.bind(message_type="context").info(
            f"Context window resolved to {self.max_tokens} tokens "
            f"(source={self.context_window_source}); summarization triggers at "
            f"{int(self.max_tokens * self.summarization_cutoff)} tokens "
            f"({self.summarization_cutoff:.0%})"
        )

    @property
    def _lookup_model(self) -> str:
        """Model name suitable for LiteLLM info/token lookups (responses/ stripped)."""
        return self._responses_model_name if self._is_responses_model else self.model

    @property
    def projector(self) -> PromptTokenProjector:
        """The calibrating projector, exposed so a run records its final state."""
        return self._projector

    def _get_token_count(self, messages: list[LitellmAnyMessage]) -> int:
        """Estimate token count"""
        try:
            return token_counter(model=self._lookup_model, messages=messages)
        except Exception:
            total_chars = sum(
                len(c) if isinstance(c := get_msg_content(m), str) else 0
                for m in messages
            )
            return total_chars // 4

    def estimate_tokens(self, messages: list[LitellmAnyMessage]) -> int:
        """Raw token estimate for ``messages`` (the unit ``should_summarize``
        measures in before projection)."""
        return self._get_token_count(messages)

    def observe_llm_usage(
        self, messages_sent: list[LitellmAnyMessage], prompt_tokens: int
    ) -> None:
        """Fold the provider-billed ``prompt_tokens`` for ``messages_sent`` into
        the projector. ``messages_sent`` must be the exact list that produced
        the response; non-positive counts (missing usage) are ignored."""
        if prompt_tokens <= 0:
            return
        self._projector.observe(self._get_token_count(messages_sent), prompt_tokens)

    def observe_overflow(
        self,
        messages_sent: list[LitellmAnyMessage],
        error: Exception | None = None,
        hard_limit: int | None = None,
    ) -> None:
        """Fold a provider rejection of ``messages_sent`` into the projector.

        ``error`` is parsed for the provider's own prompt size and limit
        (``parse_context_overflow``); the size is observed exactly when stated.
        Otherwise the rejection is a lower bound above ``hard_limit`` (default:
        the resolved window)."""
        observe_provider_overflow(
            self._projector,
            self._get_token_count(messages_sent),
            window=self.max_tokens,
            error=error,
            hard_limit=hard_limit,
        )

    def fits_rejected_request(
        self, messages: list[LitellmAnyMessage], error: Exception
    ) -> bool:
        """Whether ``messages`` project under the room ``error`` names: its limit
        (capped at the window) minus any completion reservation it states."""
        _, completion, limit = overflow_numbers(str(error))
        room = min(limit or self.max_tokens, self.max_tokens) - (completion or 0)
        return self._projector.project(self._get_token_count(messages)) < room

    def should_summarize(self, messages: list[LitellmAnyMessage]) -> bool:
        """Check if we should summarize (projected context >= cutoff fraction of max)"""

        projected = self._projector.project(self._get_token_count(messages))
        threshold = self.max_tokens * self.summarization_cutoff
        return projected >= threshold

    def adopt_inherited_bridges(self, messages: list[LitellmAnyMessage]) -> None:
        """Track inherited bridges: the bridge text followed by the harness's ack.

        The first user message is the task prompt, so it is never taken for one.
        """
        first_user = next((m for m in messages if get_msg_role(m) == "user"), None)
        for m, following in itertools.pairwise(messages):
            content = get_msg_content(m)
            if (
                m is not first_user
                and get_msg_role(m) == "user"
                and isinstance(content, str)
                and content.startswith(_BRIDGE_PREFIX)
                and get_msg_role(following) == "user"
                and get_msg_content(following) == ACKNOWLEDGEMENT
            ):
                self._bridges.append(m)

    async def summarize(
        self, messages: list[LitellmAnyMessage]
    ) -> list[LitellmAnyMessage]:
        """Summarize messages using Stirrup's exact approach"""
        in_conversation = (
            self.summarization_mode == SUMMARIZE_IN_CONVERSATION
            and not self._is_responses_model
        )
        # Upstream keeps only the original task context; an earlier summary ends it.
        task_context: list[LitellmAnyMessage] = list(
            takewhile(
                lambda m: get_msg_role(m) != "assistant"
                and not (in_conversation and any(m is b for b in self._bridges)),
                messages,
            )
        )

        logger.bind(message_type="context").info(
            f"Summarizing conversation (keeping {len(task_context)} task context messages)"
        )

        if in_conversation:
            summary = await self._generate_summary_in_conversation(messages)
        else:
            summary = await self._generate_summary(messages)

        bridge_content = BRIDGE_PROMPT.format(summary=summary)
        bridge_message = LitellmOutputMessage(role="user", content=bridge_content)
        self._bridges.append(bridge_message)

        acknowledgement = LitellmOutputMessage(role="user", content=ACKNOWLEDGEMENT)

        return [*task_context, bridge_message, acknowledgement]

    @property
    def _is_responses_model(self) -> bool:
        return "responses/" in self.model

    @property
    def _responses_model_name(self) -> str:
        return self.model.replace("responses/", "")

    @staticmethod
    def _format_messages(messages: list[LitellmAnyMessage]) -> str:
        """Format messages into a plain-text string for summarization.

        Renders tool_calls and tool results as readable text so the
        summarization payload contains no tool-related message blocks.
        This follows the same convention used by Toolbelt, ReSum, and
        BrowserUse context managers.
        """
        parts: list[str] = []

        for msg in messages:
            role = get_msg_role(msg)
            raw_content = get_msg_content(msg)
            content = content_to_str(raw_content) if raw_content else ""

            if len(content) > 2000:
                content = content[:2000] + "\n[truncated]"

            if role == "tool":
                name = get_msg_attr(msg, "name", "unknown")
                if len(content) > 1000:
                    content = content[:1000] + "\n[truncated]"
                parts.append(f"**TOOL ({name})**: {content}")
            elif role == "assistant":
                tool_calls = get_msg_attr(msg, "tool_calls")
                if tool_calls:
                    tc_lines: list[str] = ["Tool calls:"]
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            func = tc.get("function", {})
                            name = func.get("name", "unknown")
                            args = func.get("arguments", "")
                        else:
                            name = tc.function.name
                            args = tc.function.arguments or ""
                        if len(args) > 200:
                            args = args[:200] + "..."
                        tc_lines.append(f"  - {name}({args})")
                    tc_str = "\n".join(tc_lines)
                    if content:
                        parts.append(f"**ASSISTANT**: {content}\n{tc_str}")
                    else:
                        parts.append(f"**ASSISTANT**: {tc_str}")
                else:
                    parts.append(f"**ASSISTANT**: {content}")
            else:
                parts.append(f"**{role.upper()}**: {content}")

        return "\n\n".join(parts)

    async def _generate_summary(self, messages: list[LitellmAnyMessage]) -> str:
        """Generate summary by formatting the conversation as text and sending
        a simple ``[system, user]`` message pair to the LLM.

        This avoids passing tool-related message blocks to the summarization
        call (which would require a ``tools`` parameter that we don't provide).
        """
        formatted = self._format_messages(messages)
        prompt = f"{formatted}\n\n---\n\n{SUMMARIZATION_PROMPT}"

        system_content = "You are a helpful assistant that summarizes conversation histories concisely and accurately."
        user_message = LitellmOutputMessage(role="user", content=prompt)

        if self._is_responses_model:
            # Responses API uses "developer" instead of "system"
            summarization_messages: list[LitellmAnyMessage] = [
                {"role": "developer", "content": system_content},
                user_message,
            ]
            responses_extra_args = {
                k: v
                for k, v in self.extra_args.items()
                if k not in self._CHAT_COMPLETIONS_ONLY_KEYS
            }
            raw_response = await call_responses_api(
                model=self._responses_model_name,
                messages=summarization_messages,
                tools=[],
                llm_response_timeout=300,
                extra_args=responses_extra_args,
                stream=self.stream,
            )
            # Report before parsing: the spend is already incurred even if the
            # response turns out to be unusable below.
            if self.on_llm_call:
                self.on_llm_call(raw_response, self._responses_model_name)
            parsed = parse_responses_api_output(raw_response)
            content = parsed.text
        else:
            summarization_messages = [
                LitellmOutputMessage(role="system", content=system_content),
                user_message,
            ]
            response = await generate_response(
                model=self.model,
                messages=summarization_messages,
                tools=[],
                llm_response_timeout=300,
                extra_args=self.extra_args,
                stream=self.stream,
            )

            if self.on_llm_call:
                self.on_llm_call(response, self.model)

            if not response.choices or not isinstance(response.choices[0], Choices):
                raise ValueError("Summarization returned empty response")

            content = content_to_str(response.choices[0].message.content)

        if not content:
            raise ValueError("Summarization returned empty content")

        logger.bind(message_type="context").info("Context summarization complete")

        return content

    async def _generate_summary_in_conversation(
        self, messages: list[LitellmAnyMessage]
    ) -> str:
        """Upstream Stirrup's in-conversation summary; drops the latest turn and retries on overflow."""
        current = list(messages)
        while True:
            try:
                return await self._request_summary_in_conversation(current)
            except Exception as e:
                if not (
                    isinstance(e, ContextWindowExceededError)
                    or _is_context_window_error(e)
                ):
                    raise
                cut = next(
                    (
                        i
                        for i in range(len(current) - 1, -1, -1)
                        if get_msg_role(current[i]) == "assistant"
                    ),
                    None,
                )
                if cut is None:
                    raise
                logger.bind(message_type="context").warning(
                    "Summary request overflowed the context window; dropping the "
                    f"latest turn ({len(current) - cut} message(s)) and retrying"
                )
                current = current[:cut]

    async def _request_summary_in_conversation(
        self, messages: list[LitellmAnyMessage]
    ) -> str:
        """The summary text, escalating as upstream does when a reply has none."""
        tools = list(self.tools_provider()) if self.tools_provider else []
        text_only = f"{SUMMARIZATION_PROMPT}\n\n{SUMMARIZATION_TEXT_ONLY_PROMPT}"
        tool_docs = "\n".join(
            f"- {fn.get('name', '')}: {fn.get('description', '')}"
            for fn in (t.get("function", {}) for t in tools)
        )
        no_tools = (
            f"{text_only}\n\nTools are disabled for this response. For reference, "
            f"the tools available earlier in the conversation were:\n{tool_docs}"
        )
        for prompt, attempt_tools in (
            (SUMMARIZATION_PROMPT, tools),
            (text_only, tools),
            (no_tools, []),
        ):
            response = await generate_response(
                model=self.model,
                messages=[*messages, LitellmOutputMessage(role="user", content=prompt)],
                tools=attempt_tools,
                llm_response_timeout=300,
                extra_args=self.extra_args,
                stream=self.stream,
            )
            if self.on_llm_call:
                self.on_llm_call(response, self.model)
            content = (
                content_to_str(response.choices[0].message.content)
                if response.choices and isinstance(response.choices[0], Choices)
                else ""
            )
            if content and content.strip():
                logger.bind(message_type="context").info(
                    "Context summarization complete"
                )
                return content
            logger.bind(message_type="context").warning(
                "Summary reply had no text; retrying summarization"
            )
        raise ValueError("Summarization returned no text after every attempt")
