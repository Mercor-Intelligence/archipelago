"""Client-relayed LLM judging: collect judge prompts, then replay caller verdicts."""

import contextvars
import hashlib
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
    """Replay reached a verifier the caller returned no verdict for."""


class RelayPromptMismatch(RuntimeError):
    """Replay rebuilt a different prompt than the one the verdict answered."""


def prompt_digest(messages: list[dict[str, Any]]) -> str:
    """Stable digest of a judge prompt, used to bind a verdict to its question.

    Taken over the prompt BEFORE capping. The capped form is a pure function of
    this plus the cap, so equal digests imply the caller saw the same question,
    and replay — which sends nothing and only applies the verdict — never needs to
    know the cap the collect pass used.
    """
    canonical = json.dumps(
        [
            [str(m.get("role") or "user"), _message_text(m.get("content"))]
            for m in messages
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    """Judge transport for one grading run.

    Collect mode records each built prompt, capped, and answers with a stub whose
    results the caller discards. Replay mode answers with the caller's completion
    for that verifier and raises ``MissingRelayVerdict`` when it has none, so an
    unanswered verifier fails the run instead of scoring as the stub's rejection.

    Replay also rebuilds the prompt rather than trusting that it matches: any step
    before the judge that is not deterministic (artifact relevance selection is one)
    could hand this pass a different question than the caller answered, and applying
    a verdict to the wrong question is the failure this whole path exists to avoid.
    A digest mismatch raises ``RelayPromptMismatch``.
    """

    verdicts: dict[str, str] = field(default_factory=dict)
    expected_digests: dict[str, str] = field(default_factory=dict)
    char_cap: int = RELAY_PROMPT_CHAR_CAP
    replay: bool = False
    prompts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    replayed: set[str] = field(default_factory=set)

    async def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> _RelayResponse:
        verifier_id = _verifier_ctx.get()
        digest = prompt_digest(messages)
        if self.replay:
            verdict = self.verdicts.get(verifier_id)
            if verdict is None:
                raise MissingRelayVerdict(
                    f"no relayed verdict for verifier {verifier_id or '<unknown>'}"
                )
            expected = self.expected_digests.get(verifier_id)
            if expected and expected != digest:
                raise RelayPromptMismatch(
                    f"relayed prompt changed between passes for verifier {verifier_id}"
                )
            self.replayed.add(verifier_id)
        else:
            if verifier_id and verifier_id not in self.prompts:
                self.prompts[verifier_id] = cap_messages(messages, self.char_cap)
                self.digests[verifier_id] = digest
            verdict = _STUB_VERDICT
        return _RelayResponse(choices=[Choices(message=Message(content=verdict))])


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
