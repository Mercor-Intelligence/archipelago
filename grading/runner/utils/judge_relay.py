"""Client-relayed LLM judging: collect the judge prompts a run would send."""

import contextvars
import json
from dataclasses import dataclass, field
from typing import Any

from litellm import Choices, Message

RELAY_PROMPT_CHAR_CAP = 50_000

_TRUNCATED = "\n\n... [middle truncated for relay]\n\n"

_TAIL_RESERVE_CHARS = 20_000

_STUB_VERDICT = json.dumps(
    {"rationale": "relay prompt-collection pass", "is_criteria_true": False}
)

_relay_ctx: contextvars.ContextVar["JudgeRelay | None"] = contextvars.ContextVar(
    "judge_relay", default=None
)
_verifier_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "judge_relay_verifier", default=""
)


class MissingRelayVerdict(RuntimeError):
    """A relayed verifier the caller returned no verdict for."""


@dataclass
class _RelayResponse:
    choices: list[Choices]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def _truncate_middle(text: str, allowed: int) -> str:
    """Fit ``text`` into ``allowed`` chars by dropping its MIDDLE.

    The judge prompt is an artifact-bearing prefix followed by a tail carrying the
    criterion and the response contract, so cutting from the end would hand the
    grader evidence and no question. Both ends are kept and the removed span is
    marked.
    """
    if len(text) <= allowed:
        return text
    if allowed <= len(_TRUNCATED):
        return text[:allowed]
    room = allowed - len(_TRUNCATED)
    tail = min(_TAIL_RESERVE_CHARS, room)
    head = room - tail
    return text[:head] + _TRUNCATED + (text[-tail:] if tail else "")


def cap_messages(messages: list[dict[str, Any]], char_cap: int) -> list[dict[str, Any]]:
    """Text-only copy of a judge prompt whose total content is within ``char_cap``.

    Non-text parts are dropped. The last message holds the artifacts and the
    criterion, so it absorbs the truncation while the judge's instructions are
    kept whole; if those alone exceed the cap they are truncated too, so the
    returned total never exceeds it.
    """
    capped: list[dict[str, Any]] = [
        {
            "role": str(m.get("role") or "user"),
            "content": _message_text(m.get("content")),
        }
        for m in messages
    ]
    if not capped:
        return capped
    budget = max(0, int(char_cap))
    fixed = sum(len(m["content"]) for m in capped[:-1])
    if fixed <= budget:
        last = capped[-1]
        last["content"] = _truncate_middle(last["content"], budget - fixed)
        return capped
    remaining = budget
    for m in capped:
        text = m["content"]
        if len(text) <= remaining:
            remaining -= len(text)
            continue
        m["content"] = _truncate_middle(text, remaining)
        remaining = 0
    return capped


@dataclass
class JudgeRelay:
    """Collect-pass judge: records one prompt per verifier, capped, and answers with a stub."""

    char_cap: int = RELAY_PROMPT_CHAR_CAP
    prompts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    async def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> _RelayResponse:
        verifier_id = _verifier_ctx.get()
        if verifier_id and verifier_id not in self.prompts:
            self.prompts[verifier_id] = cap_messages(messages, self.char_cap)
        return _RelayResponse(choices=[Choices(message=Message(content=_STUB_VERDICT))])


def set_relay(relay: "JudgeRelay | None") -> None:
    _ = _relay_ctx.set(relay)


def set_current_verifier(verifier_id: str) -> None:
    _ = _verifier_ctx.set(verifier_id)


def active_relay() -> "JudgeRelay | None":
    return _relay_ctx.get()


def resolve_judge_call_llm(default: Any) -> Any:
    """The relay installed for this run, else ``default``."""
    relay = _relay_ctx.get()
    return relay if relay is not None else default
