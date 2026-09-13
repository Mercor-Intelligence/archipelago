"""
Context management for the Stirrup agent
"""

from collections.abc import Callable
from itertools import takewhile
from typing import Any

from litellm import Choices, get_model_info, token_counter
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
from runner.utils.llm import call_responses_api, generate_response

# Constants from Stirrup constants.py
CONTEXT_SUMMARIZATION_CUTOFF = 0.7

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


class StirrupContextManager:
    """
    Stirrup-style context manager

    Triggers summarization when context reaches CONTEXT_SUMMARIZATION_CUTOFF of max tokens

    Uses Stirrup's exact prompts for summarization and bridge messages
    """

    _CHAT_COMPLETIONS_ONLY_KEYS = {"ensure_alternating_roles"}

    def __init__(
        self,
        model: str,
        extra_args: dict[str, Any] | None = None,
        stream: bool = True,
        on_llm_call: Callable[[Any, str], None] | None = None,
    ):
        self.model: str = model
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
        self.max_tokens: int = 128000

        # Use stripped model name for lookups — LiteLLM doesn't recognise
        # the "responses/" prefix that we use to select the Responses API path.
        lookup_model = self._responses_model_name if self._is_responses_model else model

        try:
            model_info = get_model_info(lookup_model)
            self.max_tokens = (
                model_info.get("max_input_tokens")
                or model_info.get("max_tokens")
                or 128000
            )
        except Exception:
            pass

    @property
    def _lookup_model(self) -> str:
        """Model name suitable for LiteLLM info/token lookups (responses/ stripped)."""
        return self._responses_model_name if self._is_responses_model else self.model

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

    def should_summarize(self, messages: list[LitellmAnyMessage]) -> bool:
        """Check if we should summarize (context > 70% of max)"""

        current_tokens = self._get_token_count(messages)
        threshold = self.max_tokens * CONTEXT_SUMMARIZATION_CUTOFF
        return current_tokens >= threshold

    async def summarize(
        self, messages: list[LitellmAnyMessage]
    ) -> list[LitellmAnyMessage]:
        """Summarize messages using Stirrup's exact approach"""
        task_context: list[LitellmAnyMessage] = list(
            takewhile(lambda m: get_msg_role(m) != "assistant", messages)
        )

        logger.bind(message_type="context").info(
            f"Summarizing conversation (keeping {len(task_context)} task context messages)"
        )

        summary = await self._generate_summary(messages)

        bridge_content = BRIDGE_PROMPT.format(summary=summary)
        bridge_message = LitellmOutputMessage(role="user", content=bridge_content)

        acknowledgement = LitellmOutputMessage(role="user", content="Got it, thanks!")

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
