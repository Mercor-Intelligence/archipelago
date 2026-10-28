"""
ReSum: Context Summarization for Long-Horizon Agent Tasks.

Simplified implementation based on arXiv:2509.13313.
Uses fraction-based trigger and incremental summarization.
"""

import asyncio
from typing import Any, Literal
from uuid import uuid4

from litellm import Choices, token_counter
from loguru import logger

from runner.agents.models import (
    LitellmAnyMessage,
    LitellmOutputMessage,
    content_to_str,
    get_msg_attr,
    get_msg_content,
    get_msg_role,
)
from runner.utils.context_budget import (
    DEFAULT_COMPACTION_CUTOFF,
    PromptTokenProjector,
    observe_provider_overflow,
    resolve_context_window,
)
from runner.utils.llm import generate_response
from runner.utils.settings import get_settings

from .base import CompactionEvent, CompactionResult
from .transcript import CompactionTranscript

# Defaults
TRIGGER_FRACTION = DEFAULT_COMPACTION_CUTOFF
KEEP_RECENT_MESSAGES = 10  # Keep last N messages verbatim
SUMMARY_TIMEOUT_SECONDS = 300

SUMMARY_PROMPT = """Summarize this AI agent's work session into a compact reasoning state.

{conversation}

Create a structured summary:

## Task & Goal
What is the agent trying to accomplish?

## Progress
- Actions taken and key results
- Important discoveries

## Current State
- Where is the agent now?
- What are the next steps?

## Key Details
- File paths, function names, values
- Error messages (exact text)
- URLs, IDs, configuration

Be specific. Include concrete values needed to continue."""


def _is_synthetic_summary_message(msg: LitellmAnyMessage) -> bool:
    """Check if a message is a synthetic summary injected by _build_output."""
    if get_msg_role(msg) != "user":
        return False
    content = get_msg_content(msg)
    return isinstance(content, str) and content.startswith(
        "## Summary of Previous Work"
    )


def _find_safe_cut_index(messages: list[LitellmAnyMessage], target_keep: int) -> int:
    """
    Find a safe index to cut the message list without orphaning tool messages.

    Tool result messages reference tool_call_ids from their preceding assistant message.
    If we cut between an assistant message and its tool results, the tool results become
    orphaned and LLM APIs will reject the conversation.

    Returns the index where the "recent" portion should start.
    """
    if len(messages) <= target_keep:
        return 0

    # Start with the naive cut point
    cut_index = len(messages) - target_keep

    # Walk backwards from cut_index to find a safe boundary
    # Safe = not starting with orphaned tool messages
    while cut_index > 0 and get_msg_role(messages[cut_index]) == "tool":
        cut_index -= 1

    # Now cut_index points to either:
    # - A non-tool message (safe to cut here)
    # - Index 0 (keep everything)

    return cut_index


class ReSumManager:
    """
    Simple ReSum context manager.

    Triggers summarization when context reaches trigger_fraction of max tokens.
    Uses incremental summarization to update a running summary.
    """

    def __init__(
        self,
        model: str,
        extra_args: dict[str, Any] | None = None,
        context_window_tokens: int | None = None,
        trigger_fraction: float = TRIGGER_FRACTION,
        use_server_rates: bool = True,
        summary_timeout: int | None = None,
    ):
        self.model: str = model
        self.extra_args: dict[str, Any] = extra_args or {}
        self.summary_timeout: int | None = summary_timeout
        self.running_summary: str | None = None
        self.messages_summarized: int = 0
        self.trigger_fraction: float = trigger_fraction
        window = resolve_context_window(
            model, context_window_tokens, use_server_rates=use_server_rates
        )
        self.max_tokens: int = window.tokens
        self.context_window_source: str = window.source
        # The loop must feed provider usage back via observe_llm_usage / observe_overflow, or the projector keeps its default 1.5x multiplier.
        self._projector: PromptTokenProjector = PromptTokenProjector(model)
        self.measurement: str = "projected_provider_tokens"

        # Full message history preserved across summarizations.
        # When compact() is called, the pre-summarization messages are
        # captured here so the trajectory output can include the complete
        # history (not just the post-summarization window).
        self._pre_summarization_history: list[LitellmAnyMessage] = []
        self._compaction_count = 0
        self.transcript = CompactionTranscript()

    @property
    def projector(self) -> PromptTokenProjector:
        """The calibrating projector, exposed so a run records its final state."""
        return self._projector

    def _get_token_count(self, messages: list[LitellmAnyMessage]) -> int:
        """Estimate token count."""
        try:
            return token_counter(model=self.model, messages=messages)
        except Exception:
            total_chars = sum(
                len(c) if isinstance(c := get_msg_content(m), str) else 0
                for m in messages
            )
            return total_chars // 4

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

    def get_full_history(
        self,
        current_messages: list[LitellmAnyMessage],
        *,
        with_compactions: bool = False,
    ) -> list[LitellmAnyMessage]:
        """Return the complete message history including pre-summarization messages.

        If no summarization has occurred, returns current_messages as-is.
        Otherwise, returns system messages + all pre-summarization messages +
        any messages added after the last summarization.
        """
        if not self._pre_summarization_history:
            return current_messages

        system_msgs: list[LitellmAnyMessage] = []
        non_system: list[LitellmAnyMessage] = []
        for m in current_messages:
            (system_msgs if get_msg_role(m) == "system" else non_system).append(m)

        # The synthetic summary message marks the boundary of the last
        # summarization. Everything after the last synthetic marker is new.
        # Using position rather than filtering avoids silently dropping real
        # messages if _build_output ever keeps recent messages as live objects.
        last_synthetic = -1
        for i, m in enumerate(non_system):
            if _is_synthetic_summary_message(m):
                last_synthetic = i

        new_messages = non_system[last_synthetic + 1 :]
        history = self._pre_summarization_history + new_messages
        if (
            with_compactions
            and get_settings().COMPACTION_TRANSCRIPT_ENABLED
            and self.transcript.event_count
        ):
            return self.transcript.saved_messages(system_msgs, history)
        return system_msgs + history

    def compaction_records(
        self, current_messages: list[LitellmAnyMessage]
    ) -> list[dict[str, Any]]:
        if (
            not get_settings().COMPACTION_TRANSCRIPT_ENABLED
            or not self.transcript.event_count
        ):
            return []
        system_count = sum(
            1 for message in current_messages if get_msg_role(message) == "system"
        )
        return self.transcript.records(system_count)

    def should_summarize(self, messages: list[LitellmAnyMessage]) -> bool:
        """Check if we should summarize (context > trigger_fraction of max)."""
        non_system = [m for m in messages if get_msg_role(m) != "system"]

        # Calculate how many new messages since last summarization
        new_messages = len(non_system) - self.messages_summarized

        # Need enough new messages beyond what we'll keep
        if new_messages <= KEEP_RECENT_MESSAGES:
            return False

        projected = self._projector.project(self._get_token_count(messages))
        threshold = self.max_tokens * self.trigger_fraction
        return projected > threshold

    async def compact(
        self,
        messages: list[LitellmAnyMessage],
        *,
        trigger: Literal["proactive", "overflow"] = "proactive",
    ) -> CompactionResult:
        """Summarize messages, keeping recent ones verbatim."""
        # non_system includes synthetic summary messages — intentionally.
        # The incremental summarization relies on the combined summary+recent
        # message being in to_summarize so the LLM incorporates the "Recent
        # Activity" content into the next running_summary.
        system_messages: list[LitellmAnyMessage] = []
        non_system: list[LitellmAnyMessage] = []
        for m in messages:
            (system_messages if get_msg_role(m) == "system" else non_system).append(m)

        if len(non_system) <= KEEP_RECENT_MESSAGES:
            return CompactionResult(messages=messages, event=None)

        # Find safe cut point that doesn't orphan tool messages
        cut_index = _find_safe_cut_index(non_system, KEEP_RECENT_MESSAGES)
        recent = non_system[cut_index:]

        # Calculate the range of messages to summarize
        # Start from where we left off, end at the safe cut point
        summarize_end = cut_index
        summarize_start = self.messages_summarized

        # Guard: nothing to summarize if we've already summarized up to the cut point
        if summarize_start >= summarize_end:
            return CompactionResult(
                messages=self._build_output(system_messages, recent), event=None
            )

        to_summarize = non_system[summarize_start:summarize_end]

        if not to_summarize:
            return CompactionResult(
                messages=self._build_output(system_messages, recent), event=None
            )

        # Capture history only when we're actually about to summarize.
        # Placed after early returns to avoid duplication if compact()
        # is called but returns without summarizing.
        # Filter out synthetic summary messages — only real messages belong
        # in the trajectory history.
        history_to_add = [m for m in non_system if not _is_synthetic_summary_message(m)]
        history_index = len(self._pre_summarization_history) + len(history_to_add)
        steps_total = sum(
            1
            for m in (*self._pre_summarization_history, *history_to_add)
            if get_msg_role(m) == "assistant"
        )
        step_range = (1, steps_total) if steps_total else None

        logger.bind(message_type="resum").info(
            f"Summarizing {len(to_summarize)} messages"
        )

        # Format messages for summarization
        formatted = self._format_messages(to_summarize)

        # If we have a running summary, include it
        if self.running_summary:
            conversation = (
                f"## Previous Summary:\n{self.running_summary}\n\n"
                f"## New Activity:\n{formatted}"
            )
        else:
            conversation = formatted

        # Generate summary
        summary = await self._call_llm(SUMMARY_PROMPT.format(conversation=conversation))

        # Build output first, then reset counter to reflect new list structure
        output = self._build_output(system_messages, recent, running_summary=summary)
        injected = [
            message
            for message in output
            if get_msg_role(message) != "system"
            and _is_synthetic_summary_message(message)
        ]
        tokens_before = self._projector.project(
            await asyncio.to_thread(self._get_token_count, messages)
        )
        output_estimate = await asyncio.to_thread(self._get_token_count, output)
        tokens_after = max(
            self._projector.project(output_estimate),
            self._projector.scale(output_estimate),
        )
        event = CompactionEvent(
            compaction_id=uuid4().hex,
            strategy="resum",
            trigger=trigger,
            sequence=self._compaction_count,
            history_index=history_index,
            replaced_count=len(to_summarize),
            kept_as_text_count=len(recent),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            summary=summary,
            injected=injected,
            step_range=step_range,
        )

        self._pre_summarization_history.extend(history_to_add)
        self.running_summary = summary
        self._compaction_count += 1

        # After _build_output, the message list is: [system_messages..., combined_user_message]
        # The combined_user_message contains both the summary AND formatted recent activity.
        # Reset to 0 so that on the next summarization cycle, this combined message
        # (at index 0 of non-system) gets included in to_summarize, ensuring the
        # "Recent Activity" content is incorporated into the next LLM-generated summary.
        self.messages_summarized = 0

        return CompactionResult(messages=output, event=event)

    async def summarize(
        self,
        messages: list[LitellmAnyMessage],
        *,
        trigger: Literal["proactive", "overflow"] = "proactive",
    ) -> list[LitellmAnyMessage]:
        return self.transcript.apply(await self.compact(messages, trigger=trigger))

    def _build_output(
        self,
        system_messages: list[LitellmAnyMessage],
        recent: list[LitellmAnyMessage],
        running_summary: str | None = None,
    ) -> list[LitellmAnyMessage]:
        """
        Build output with summary + recent context as a single user message.

        This avoids issues with Anthropic's extended thinking requirement
        (assistant messages with tool_calls must have thinking_blocks).
        By converting everything to a user message, we sidestep the requirement.
        """
        result: list[LitellmAnyMessage] = list(system_messages)

        # Build content: summary + formatted recent messages
        content_parts: list[str] = []
        summary = (
            running_summary if running_summary is not None else self.running_summary
        )

        if summary:
            content_parts.append(f"## Summary of Previous Work\n\n{summary}")

        if recent:
            content_parts.append(
                f"## Recent Activity\n\n{self._format_messages(recent)}"
            )

        if content_parts:
            result.append(
                LitellmOutputMessage(
                    role="user",
                    content="\n\n---\n\n".join(content_parts)
                    + "\n\n---\n\nContinue from this state.",
                )
            )

        return result

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM for summarization."""
        extra_args = self.extra_args
        timeout = SUMMARY_TIMEOUT_SECONDS
        if self.summary_timeout is not None:
            # generate_response lets an orchestrator timeout extra arg replace
            # llm_response_timeout, so drop both spellings for a configured one.
            extra_args = {
                k: v
                for k, v in extra_args.items()
                if k not in ("timeout", "request_timeout")
            }
            timeout = self.summary_timeout
        response = await generate_response(
            model=self.model,
            messages=[
                LitellmOutputMessage(
                    role="system", content="Summarize AI agent work sessions."
                ),
                LitellmOutputMessage(role="user", content=prompt),
            ],
            tools=[],
            llm_response_timeout=timeout,
            extra_args=extra_args,
        )

        if not response.choices or not isinstance(response.choices[0], Choices):
            raise ValueError("Summarization returned empty response")

        # Some providers return content as a list (e.g. Anthropic); normalize to string.
        content = content_to_str(response.choices[0].message.content)
        if not content:
            raise ValueError("Summarization returned empty content")

        return content

    def _format_messages(self, messages: list[LitellmAnyMessage]) -> str:
        """Format messages for summarization prompt, including tool calls."""
        parts: list[str] = []

        for msg in messages:
            role = get_msg_role(msg).upper()
            raw_content = get_msg_content(msg)
            content = content_to_str(raw_content)

            if len(content) > 2000:
                content = content[:2000] + "\n[truncated]"

            if get_msg_role(msg) == "tool":
                name = get_msg_attr(msg, "name", "unknown")
                if len(content) > 1000:
                    content = content[:1000] + "\n[truncated]"
                parts.append(f"**TOOL ({name})**: {content}")
            elif get_msg_role(msg) == "assistant":
                # Include tool_calls for assistant messages
                tool_calls = get_msg_attr(msg, "tool_calls")
                if tool_calls:
                    tool_calls_str = self._format_tool_calls(tool_calls)
                    if content:
                        parts.append(f"**ASSISTANT**: {content}\n{tool_calls_str}")
                    else:
                        parts.append(f"**ASSISTANT**: {tool_calls_str}")
                else:
                    parts.append(f"**ASSISTANT**: {content}")
            else:
                parts.append(f"**{role}**: {content}")

        return "\n\n".join(parts)

    def _format_tool_calls(self, tool_calls: list[object]) -> str:
        """Format tool calls for display in summaries."""
        lines: list[str] = ["Tool calls:"]
        for tc in tool_calls:
            name: str
            args: str
            if hasattr(tc, "function"):
                # Pydantic model (LitellmOutputMessage)
                func = getattr(tc, "function", None)
                if func is None:
                    continue
                name = getattr(func, "name", "unknown")
                args = getattr(func, "arguments", "{}")
            elif isinstance(tc, dict) and "function" in tc:
                # TypedDict
                func_dict = tc["function"]
                name = (
                    func_dict.get("name", "unknown")
                    if isinstance(func_dict, dict)
                    else "unknown"
                )
                args = (
                    func_dict.get("arguments", "{}")
                    if isinstance(func_dict, dict)
                    else "{}"
                )
            else:
                continue

            # Truncate long arguments
            if len(args) > 200:
                args = args[:200] + "..."
            lines.append(f"  - {name}({args})")

        return "\n".join(lines)
