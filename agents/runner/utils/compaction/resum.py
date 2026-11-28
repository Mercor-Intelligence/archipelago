"""
ReSum: Context Summarization for Long-Horizon Agent Tasks.

Simplified implementation based on arXiv:2509.13313.
Uses fraction-based trigger and incremental summarization.
"""

import asyncio
import math
import re
import time
from typing import Any, Literal, NamedTuple
from uuid import uuid4

from litellm import Choices, token_counter
from litellm.exceptions import Timeout
from litellm.files.main import ModelResponse
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
from runner.utils.decorators import llm_terminal_exceptions_ctx
from runner.utils.llm import generate_response

from .base import CompactionEvent, CompactionResult
from .transcript import CompactionTranscript

# Defaults
TRIGGER_FRACTION = DEFAULT_COMPACTION_CUTOFF
KEEP_RECENT_MESSAGES = 10  # Keep last N messages verbatim
PINNED_PREFIX_MESSAGES = 1  # Keep the task prompt (first non-system message) as-is
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


_SYNTHETIC_SUFFIX = "\n\n---\n\nContinue from this state."
_SUMMARY_HEADER = "## Summary of Previous Work\n\n"
_PART_JOINER = "\n\n---\n\n"


class EmptySummaryError(ValueError):
    """The summary call returned no choices or no text."""


class SummaryTimeoutError(Timeout):
    """The low-reasoning retry of a GLM summary timed out."""

    def __init__(self, message: str, model: str, llm_provider: str) -> None:
        super().__init__(message, model=model, llm_provider=llm_provider)
        # litellm prefixes "litellm.Timeout: "; keep ours so the message leads with its cause.
        self.message = message


def _is_synthetic_summary_message(msg: LitellmAnyMessage) -> bool:
    """Check if a message is a synthetic summary injected by _build_output."""
    if get_msg_role(msg) != "user":
        return False
    content = get_msg_content(msg)
    if not isinstance(content, str):
        return False
    if content.startswith("## Summary of Previous Work"):
        return True
    # Recent activity without a summary: a user turn could start with this
    # header, so also require the closing line _build_output appends.
    return content.startswith("## Recent Activity") and content.endswith(
        _SYNTHETIC_SUFFIX
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


# Lowercased last path segment of GLM 5.3 Flash model ids; GLM 5.3 non-Flash is excluded.
_GLM_5_3_FLASH_NAMES = frozenset({"glm-5.3-flash", "glm-5p3-flash"})
_REASONING_KEYS = ("reasoning_effort", "reasoning", "thinking", "chat_template_kwargs")
# Default thinking can spend 32,768 tokens before the summary starts; 65,536 leaves room.
GLM_SUMMARY_MAX_TOKENS = 65_536
# Low took 24–60 s in the real-failure replay; 300 s caps a worst-case GLM compaction at ~20 min.
GLM_SUMMARY_RETRY_TIMEOUT_S = 300
# Orchestrator extra args that generate_response lets replace llm_response_timeout.
_TIMEOUT_KEYS = ("timeout", "request_timeout")


def _glm_default_reasoning(model: str, extra_args: dict[str, Any]) -> bool:
    """Whether a summary call is GLM 5.3 Flash with no reasoning setting of its own."""
    if model.lower().rsplit("/", 1)[-1] not in _GLM_5_3_FLASH_NAMES:
        return False
    body = extra_args.get("extra_body")
    scopes = (extra_args, body if isinstance(body, dict) else {})
    return not any(key in scope for scope in scopes for key in _REASONING_KEYS)


def _timeout_s(value: object) -> float:
    """A timeout extra arg in seconds, or 0 when it is not a positive number."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def summary_worst_case_s(
    model: str, extra_args: dict[str, Any] | None, summary_timeout: int | None
) -> float:
    """The longest one summary can take, as ``_call_llm`` makes it: the first
    attempt, plus GLM 5.3 Flash's low-reasoning retry. With no summary timeout,
    an orchestrator ``timeout`` extra arg is the first's deadline, larger or smaller."""
    args = extra_args or {}
    if summary_timeout is not None:
        first = float(summary_timeout)
    elif (override := _timeout_s(args.get("timeout"))) > 0:
        # generate_response sends it as the call's own timeout, over the default.
        first = override
    else:
        first = max(
            float(SUMMARY_TIMEOUT_SECONDS), _timeout_s(args.get("request_timeout"))
        )
    if _glm_default_reasoning(model, args):
        return first + GLM_SUMMARY_RETRY_TIMEOUT_S
    return first


def _with_output_floor(extra_args: dict[str, Any], floor: int) -> dict[str, Any]:
    """A copy of ``extra_args`` whose output-token limit is at least ``floor``."""
    key = "max_tokens"
    if "max_completion_tokens" in extra_args:
        key = "max_completion_tokens"
    limit = extra_args.get(key)
    return {**extra_args, key: max(limit, floor) if isinstance(limit, int) else floor}


class _SummaryCall(NamedTuple):
    outcome: str
    latency_s: float
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    answer_tokens: int | None = None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# GLM can send a draft, </think>, then the whole summary again (SUMMARY_PROMPT's first heading).
_DRAFT_BEFORE_SUMMARY = re.compile(r"</think>\s*(?=## Task & Goal)")


def _summary_text(response: ModelResponse) -> str:
    """The reply's summary text, or "" when it has none."""
    choice = response.choices[0] if response.choices else None
    if not isinstance(choice, Choices):
        return ""
    text = content_to_str(choice.message.content)
    if not text.replace("</think>", "").strip():
        return ""
    # A truncated reply keeps its whole text: its draft may be the only complete copy.
    if choice.finish_reason != "length":
        text = _DRAFT_BEFORE_SUMMARY.split(text)[-1]
    return text.strip()


def _summary_call(
    response: ModelResponse, outcome: str, latency_s: float
) -> _SummaryCall:
    """One summary call's outcome and token counts; None where the provider omits them."""
    choice = response.choices[0] if response.choices else None
    finish = getattr(choice, "finish_reason", None)
    usage = getattr(response, "usage", None)
    completion = _int_or_none(getattr(usage, "completion_tokens", None))
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = _int_or_none(getattr(details, "reasoning_tokens", None))
    answer = None
    if completion is not None and reasoning is not None:
        answer = completion - reasoning
    return _SummaryCall(
        outcome=outcome,
        latency_s=round(latency_s, 1),
        finish_reason=finish if isinstance(finish, str) else None,
        reasoning_tokens=reasoning,
        answer_tokens=answer,
    )


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
        # Headers of the caller's carry parts, which a continuation's seed holds from its parent.
        self.carry_headers: tuple[str, ...] = ()
        self._foreign_parts: tuple[str, ...] = ()
        # Whether _build_output has run; before that, a synthetic message is a seed's.
        self._built: bool = False
        # The caller's carry text as the latest synthetic message holds it,
        # stripped when that message is summarized.
        self._carried_tail: str | None = None
        self.running_summary: str | None = None
        self.messages_summarized: int = PINNED_PREFIX_MESSAGES
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

    @property
    def longest_attempt_s(self) -> int:
        """The longest one summary attempt can take, GLM 5.3 Flash's low retry included."""
        timeout = self.summary_timeout or SUMMARY_TIMEOUT_SECONDS
        if _glm_default_reasoning(self.model, self.extra_args):
            return max(timeout, GLM_SUMMARY_RETRY_TIMEOUT_S)
        return timeout

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
    ) -> list[LitellmAnyMessage]:
        """Return the complete message history including pre-summarization messages.

        If no summarization has occurred, returns current_messages as-is.
        Otherwise, returns system messages + all pre-summarization messages, with each
        injected summary at its compaction point + any messages added after the last.
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
        return self.transcript.saved_messages(system_msgs, history)

    def compaction_records(
        self, current_messages: list[LitellmAnyMessage]
    ) -> list[dict[str, Any]]:
        if not self.transcript.event_count:
            return []
        system_count = sum(
            1 for message in current_messages if get_msg_role(message) == "system"
        )
        return self.transcript.records(system_count)

    def compaction_output(
        self, current_messages: list[LitellmAnyMessage]
    ) -> dict[str, Any]:
        """`{"compactions": records}` for the trajectory output, or `{}` if none ran."""
        records = self.compaction_records(current_messages)
        return {"compactions": records} if records else {}

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
        carry: str | None = None,
    ) -> CompactionResult:
        """Summarize messages, keeping recent ones verbatim.

        ``carry`` is text the caller wants at the end of the synthetic message
        (before its closing line), such as its own todo list.
        """
        # Passed on only when set, so calls without it are exactly as before.
        extra = {} if carry is None else {"carry": carry}
        # non_system includes synthetic summary messages — intentionally.
        # The incremental summarization relies on the combined summary+recent
        # message being in to_summarize so the LLM incorporates the "Recent
        # Activity" content into the next running_summary.
        system_messages: list[LitellmAnyMessage] = []
        non_system: list[LitellmAnyMessage] = []
        for m in messages:
            (system_messages if get_msg_role(m) == "system" else non_system).append(m)

        if len(non_system) <= KEEP_RECENT_MESSAGES + PINNED_PREFIX_MESSAGES:
            return CompactionResult(messages=messages, event=None)

        pinned = non_system[:PINNED_PREFIX_MESSAGES]
        # Find safe cut point that doesn't orphan tool messages
        cut_index = max(
            _find_safe_cut_index(non_system, KEEP_RECENT_MESSAGES),
            PINNED_PREFIX_MESSAGES,
        )
        recent = non_system[cut_index:]

        # Calculate the range of messages to summarize
        # Start from where we left off, end at the safe cut point
        summarize_end = cut_index
        summarize_start = self.messages_summarized
        if not self._built:
            # A continuation's seed: its parent's carry parts give way to this run's own.
            self._foreign_parts = self.carry_headers

        # Guard: nothing to summarize if we've already summarized up to the cut point
        if summarize_start >= summarize_end:
            # Nothing to summarize, but the rebuild still replaces the recent
            # messages with text, so save them first.
            self._pre_summarization_history.extend(self._history_to_add(non_system))
            return CompactionResult(
                messages=self._build_output(system_messages, pinned, recent, **extra),
                event=None,
            )

        to_summarize = non_system[summarize_start:summarize_end]

        if not to_summarize:
            # Nothing to summarize, but the rebuild still replaces the recent
            # messages with text, so save them first.
            self._pre_summarization_history.extend(self._history_to_add(non_system))
            return CompactionResult(
                messages=self._build_output(system_messages, pinned, recent, **extra),
                event=None,
            )

        history_to_add = self._history_to_add(non_system)
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
        output = self._build_output(
            system_messages, pinned, recent, running_summary=summary, **extra
        )
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

        # After _build_output, the message list is:
        # [system_messages..., task_prompt, combined_user_message]
        # The combined_user_message contains both the summary AND formatted recent activity.
        # Start the next cycle at that combined message (index 1 of non-system),
        # skipping the pinned task prompt, so "Recent Activity" is folded into
        # the next LLM-generated summary.
        self.messages_summarized = PINNED_PREFIX_MESSAGES

        return CompactionResult(messages=output, event=event)

    async def summarize(
        self,
        messages: list[LitellmAnyMessage],
        *,
        trigger: Literal["proactive", "overflow"] = "proactive",
        carry: str | None = None,
    ) -> list[LitellmAnyMessage]:
        extra = {} if carry is None else {"carry": carry}
        return self.transcript.apply(
            await self.compact(messages, trigger=trigger, **extra)
        )

    def _history_to_add(
        self, non_system: list[LitellmAnyMessage]
    ) -> list[LitellmAnyMessage]:
        """Real messages to save before compaction replaces them.

        Synthetic messages are skipped, and so is the pinned prompt once it is
        already in history.
        """
        start = PINNED_PREFIX_MESSAGES if self._pre_summarization_history else 0
        return [m for m in non_system[start:] if not _is_synthetic_summary_message(m)]

    def _build_output(
        self,
        system_messages: list[LitellmAnyMessage],
        pinned: list[LitellmAnyMessage],
        recent: list[LitellmAnyMessage],
        running_summary: str | None = None,
        carry: str | None = None,
    ) -> list[LitellmAnyMessage]:
        """
        Build output with summary + recent context as a single user message.

        This avoids issues with Anthropic's extended thinking requirement
        (assistant messages with tool_calls must have thinking_blocks).
        By converting everything to a user message, we sidestep the requirement.
        ``carry`` ends the message, before its closing line, so the message
        still starts and ends as before.
        """
        result: list[LitellmAnyMessage] = [*system_messages, *pinned]

        # Build content: summary + formatted recent messages
        content_parts: list[str] = []
        summary = (
            running_summary if running_summary is not None else self.running_summary
        )

        if summary:
            content_parts.append(f"{_SUMMARY_HEADER}{summary}")

        if recent:
            content_parts.append(
                f"## Recent Activity\n\n{self._format_messages(recent)}"
            )

        tail = carry if content_parts and carry else None
        if tail:
            content_parts.append(tail)
        if content_parts:
            result.append(
                LitellmOutputMessage(
                    role="user",
                    content=_PART_JOINER.join(content_parts) + _SYNTHETIC_SUFFIX,
                )
            )
        # Set only now: formatting `recent` above may strip the previous one.
        self._carried_tail = tail
        self._built = True

        return result

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM for summarization."""
        extra_args = self.extra_args
        timeout = SUMMARY_TIMEOUT_SECONDS
        if self.summary_timeout is not None:
            # generate_response lets an orchestrator timeout extra arg replace
            # llm_response_timeout, so drop both spellings for a configured one.
            extra_args = {k: v for k, v in extra_args.items() if k not in _TIMEOUT_KEYS}
            timeout = self.summary_timeout
        if _glm_default_reasoning(self.model, extra_args):
            return await self._call_glm(prompt, extra_args, timeout)
        response = await self._request_summary(prompt, extra_args, timeout)

        if not response.choices or not isinstance(response.choices[0], Choices):
            raise EmptySummaryError("Summarization returned empty response")

        # Some providers return content as a list (e.g. Anthropic); normalize to string.
        content = content_to_str(response.choices[0].message.content)
        if not content:
            raise EmptySummaryError("Summarization returned empty content")

        return content

    async def _request_summary(
        self, prompt: str, extra_args: dict[str, Any], timeout: int
    ) -> ModelResponse:
        return await generate_response(
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

    async def _call_glm(
        self, prompt: str, extra_args: dict[str, Any], timeout: int
    ) -> str:
        """Default thinking first; one low-reasoning retry if that is empty or times out."""
        retry_args = {k: v for k, v in extra_args.items() if k not in _TIMEOUT_KEYS}
        attempts = (
            # Summary calls only, never the main loop: GLM's default thinking
            # alone can fill a 32,768-token cap and return an empty summary.
            (_with_output_floor(extra_args, GLM_SUMMARY_MAX_TOKENS), timeout),
            # GLM 5.3 Flash 400s on the disable forms ("none", enabled: false).
            ({**retry_args, "reasoning_effort": "low"}, GLM_SUMMARY_RETRY_TIMEOUT_S),
        )
        calls: list[_SummaryCall] = []
        timed_out: Timeout | None = None
        start = time.perf_counter()
        # A timeout ends the call, so the retry starts now rather than after
        # with_retry re-sends the same request.
        terminal = llm_terminal_exceptions_ctx.set(
            (*llm_terminal_exceptions_ctx.get(), Timeout)
        )
        try:
            for args, deadline in attempts:
                call_start = time.perf_counter()
                try:
                    response = await self._request_summary(prompt, args, deadline)
                except Timeout as error:
                    timed_out = error
                    elapsed = time.perf_counter() - call_start
                    calls.append(_SummaryCall("timeout", round(elapsed, 1)))
                    continue
                content = _summary_text(response)
                outcome = "ok" if content else "empty"
                elapsed = time.perf_counter() - call_start
                calls.append(_summary_call(response, outcome, elapsed))
                if content:
                    return content
            if calls[-1].outcome == "timeout":
                raise SummaryTimeoutError(
                    "Summarization timed out; summary failed after low retry: timeout",
                    model=self.model,
                    llm_provider=getattr(timed_out, "llm_provider", None) or "",
                ) from timed_out
            raise EmptySummaryError(
                "Summarization returned empty content; "
                "summary failed after low retry: empty"
            )
        finally:
            llm_terminal_exceptions_ctx.reset(terminal)
            outcomes = [call.outcome for call in calls]
            path = "failed"
            if outcomes == ["ok"]:
                path = "default"
            elif outcomes[-1:] == ["ok"]:
                path = f"low_retry_after_{outcomes[0]}"
            logger.bind(
                message_type="resum",
                summary_path=path,
                model=self.model,
                outcomes=outcomes,
                finish_reasons=[call.finish_reason for call in calls],
                reasoning_tokens=[call.reasoning_tokens for call in calls],
                answer_tokens=[call.answer_tokens for call in calls],
                call_latency_s=[call.latency_s for call in calls],
                latency_s=round(time.perf_counter() - start, 1),
            ).info(f"GLM summary path: {path}")

    def _format_messages(self, messages: list[LitellmAnyMessage]) -> str:
        """Format messages for summarization prompt, including tool calls."""
        parts: list[str] = []

        for msg in messages:
            role = get_msg_role(msg).upper()
            raw_content = get_msg_content(msg)
            content = content_to_str(raw_content)

            if _is_synthetic_summary_message(msg):
                # Synthetic summary messages pass through untruncated so Recent Activity reaches the next summary.
                activity = self._synthetic_activity(content)
                if activity:
                    parts.append(f"**{role}**: {activity}")
                continue

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

    def _synthetic_activity(self, content: str) -> str:
        """A synthetic message's text without its closing line or the running
        summary, which the summary prompt already carries as Previous Summary,
        and without the carry text this manager added."""
        content = content.removesuffix(_SYNTHETIC_SUFFIX)
        if self._carried_tail:
            content = content.removesuffix(f"{_PART_JOINER}{self._carried_tail}")
        if self.running_summary:
            summary_part = f"{_SUMMARY_HEADER}{self.running_summary}"
            if content == summary_part:
                return ""
            content = content.removeprefix(f"{summary_part}\n\n---\n\n")
        if self._foreign_parts and not self._built:
            parts = content.split(_PART_JOINER)
            content = _PART_JOINER.join(
                part for part in parts if not part.startswith(self._foreign_parts)
            )
        return content.strip()

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
