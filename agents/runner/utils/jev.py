"""Client for Jev, an evaluation model answering typed questions about a state.

Reached through the Vercel AI Gateway's ``/v1/evaluate`` endpoint; the
chat-completions endpoint rejects evaluation models. Every helper returns
``None`` on any failure so callers can keep their non-Jev behavior.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from runner.agents.models import LitellmAnyMessage, content_to_str, get_msg_attr

DEFAULT_JEV_MODEL = "typesafe-ai/jev"
DEFAULT_JEV_API_BASE = "https://ai-gateway.vercel.sh/v1"
# Jev is served by the Vercel AI Gateway, so it rides the gateway key the
# runner already gets in its env rather than a key of its own.
GATEWAY_KEY_ENV = ("VERCEL_API_KEY_2", "VERCEL_AI_GATEWAY_API_KEY", "VERCEL_API_KEY")
DEFAULT_JEV_TIMEOUT_SECONDS = 20.0
DEFAULT_STATE_CHARS = 12000
DEFAULT_MESSAGE_CHARS = 1200

# Option keys, kept content-free so the label carries no signal of its own.
OPTION_LABELS = (
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "option_e",
    "option_f",
    "option_g",
    "option_h",
)

_QUESTION_ID = "q"
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Matches the registry field bounds, which only constrain form input: config
# can also arrive from an import or the API.
MAX_CANDIDATE_SAMPLES = 4
MAX_STATE_CHARS = 60000


@dataclass(frozen=True)
class JevConfig:
    """Jev knobs the agent variants read from their Studio agent config."""

    candidate_samples: int = 2
    finish_gate: bool = True
    finish_gate_threshold: float = 0.85
    finish_gate_streak: int = 2
    model: str = DEFAULT_JEV_MODEL
    timeout_seconds: float = DEFAULT_JEV_TIMEOUT_SECONDS
    state_chars: int = DEFAULT_STATE_CHARS

    @classmethod
    def from_config_values(cls, values: dict[str, Any] | None) -> JevConfig:
        values = values or {}
        defaults = cls()

        def number(key: str, default: float) -> float:
            raw = values.get(key)
            if raw is None:
                return default
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                logger.warning(f"Ignoring non-numeric {key}={raw!r} in agent config")
                return default
            # int() of nan/inf raises, which would abort the run before its
            # first provider call.
            if not math.isfinite(parsed):
                logger.warning(f"Ignoring non-finite {key}={raw!r} in agent config")
                return default
            return parsed

        def clamp(value: float, low: float, high: float) -> float:
            return min(high, max(low, value))

        return cls(
            candidate_samples=int(
                clamp(
                    number("jev_candidate_samples", defaults.candidate_samples),
                    1,
                    MAX_CANDIDATE_SAMPLES,
                )
            ),
            finish_gate=_as_bool(values.get("jev_finish_gate"), defaults.finish_gate),
            finish_gate_threshold=clamp(
                number("jev_finish_gate_threshold", defaults.finish_gate_threshold),
                0.0,
                1.0,
            ),
            finish_gate_streak=int(
                clamp(
                    number("jev_finish_gate_streak", defaults.finish_gate_streak),
                    1,
                    10,
                )
            ),
            model=str(values.get("jev_model") or defaults.model),
            timeout_seconds=clamp(
                number("jev_timeout", defaults.timeout_seconds), 1.0, 120.0
            ),
            state_chars=int(
                clamp(
                    number("jev_state_chars", defaults.state_chars),
                    500,
                    MAX_STATE_CHARS,
                )
            ),
        )


@dataclass(frozen=True)
class JevChoice:
    """Index of the option Jev picked, with its probability distribution."""

    index: int
    probability: float | None
    probabilities: dict[str, float]


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    # Studio config values arrive as strings from some form paths, where
    # bool("false") would be True.
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n…[{len(text) - limit} chars elided]…\n{text[-tail:]}"


def describe_tool_call(call: Any) -> str:
    """Render one tool call as ``name(json args)``."""
    function = getattr(call, "function", None)
    if function is None and isinstance(call, dict):
        function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        raw_args = function.get("arguments")
    else:
        name = getattr(function, "name", None)
        raw_args = getattr(function, "arguments", None)
    if isinstance(raw_args, str):
        try:
            parsed: Any = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed = raw_args
    else:
        parsed = raw_args
    args = parsed if isinstance(parsed, str) else json.dumps(parsed, default=str)
    return f"{name or 'unknown_tool'}({_truncate(args, DEFAULT_MESSAGE_CHARS)})"


def describe_candidate_turn(message: Any) -> str:
    """Render a candidate assistant turn: its tool calls plus its rationale."""
    reasoning = _truncate(content_to_str(get_msg_attr(message, "content")).strip(), 600)
    calls = get_msg_attr(message, "tool_calls") or []
    lines = [describe_tool_call(call) for call in calls]
    body = "\n".join(lines) if lines else "(no tool call — ends the turn with text)"
    return f"{body}\n---\nstated rationale: {reasoning}" if reasoning else body


def first_user_text(messages: list[Any]) -> str | None:
    """Text of the first user message, i.e. the task statement."""
    for msg in messages:
        if get_msg_attr(msg, "role") == "user":
            text = content_to_str(get_msg_attr(msg, "content")).strip()
            if text:
                return text
    return None


def render_state(
    messages: list[LitellmAnyMessage],
    *,
    task_prompt: str | None = None,
    turn: int | None = None,
    max_chars: int = DEFAULT_STATE_CHARS,
    message_chars: int = DEFAULT_MESSAGE_CHARS,
) -> str:
    """Render the task plus the transcript tail, bounded to ``max_chars``.

    Each message is truncated individually so one huge tool result cannot crowd
    the recent history out of the budget.
    """
    header: list[str] = []
    if turn is not None:
        header.append(f"# Turn\n{turn}")
    if task_prompt:
        # Never more than half the budget, so history always has room.
        task_budget = min(3000, max(0, max_chars // 2 - len("# Task\n")))
        header.insert(0, f"# Task\n{_truncate(task_prompt.strip(), task_budget)}")

    rendered: list[str] = []
    for msg in messages:
        role = str(get_msg_attr(msg, "role") or "unknown")
        if role == "system":
            continue
        text = content_to_str(get_msg_attr(msg, "content")).strip()
        calls = get_msg_attr(msg, "tool_calls") or []
        parts = [text] if text else []
        parts.extend(describe_tool_call(call) for call in calls)
        if not parts:
            continue
        rendered.append(f"[{role}] {_truncate(' '.join(parts), message_chars)}")

    heading = "# Recent history\n"
    # Separators and headings count against max_chars too, so a caller's bound
    # holds for the whole rendered string.
    budget = max_chars - sum(len(part) + 2 for part in header) - len(heading)
    tail: list[str] = []
    for entry in reversed(rendered):
        cost = len(entry) + (1 if tail else 0)
        if cost > budget:
            # The newest entry is the evidence Jev needs most, so keep a slice
            # of it rather than an empty history.
            if not tail and budget > 1:
                tail.append(entry[: budget - 1] + "…")
            break
        tail.append(entry)
        budget -= cost
    header.append(heading + "\n".join(reversed(tail)))
    return "\n\n".join(header)


def _gateway_api_key() -> str | None:
    for name in GATEWAY_KEY_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


class JevClient:
    """Async client over the gateway's structured-evaluation endpoint."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_JEV_MODEL,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout_seconds: float = DEFAULT_JEV_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.api_key = api_key or _gateway_api_key()
        self.api_base = (api_base or DEFAULT_JEV_API_BASE).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.calls = 0
        self.failures = 0
        self._transport: httpx.AsyncBaseTransport | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def choose(
        self,
        state: str,
        options: list[str],
        instructions: str,
    ) -> JevChoice | None:
        """Pick one of ``options``, or None when Jev gave no usable answer."""
        if len(options) < 2:
            return None
        options = options[: len(OPTION_LABELS)]
        labels = OPTION_LABELS[: len(options)]
        answer = await self._evaluate(
            state,
            {
                "type": "choice",
                "instructions": instructions,
                "criteria": dict(zip(labels, options, strict=True)),
            },
        )
        if answer is None:
            return None
        picked = answer.get("choice")
        if picked not in labels:
            logger.warning(f"Jev returned unusable choice {picked!r}; falling back")
            return None
        raw_probabilities = answer.get("probabilities")
        probabilities = (
            {
                str(key): float(value)
                for key, value in raw_probabilities.items()
                if isinstance(value, int | float) and not isinstance(value, bool)
            }
            if isinstance(raw_probabilities, dict)
            else {}
        )
        return JevChoice(
            index=labels.index(picked),
            probability=probabilities.get(str(picked)),
            probabilities=probabilities,
        )

    async def probability(
        self,
        state: str,
        instructions: str,
        criteria: dict[str, str] | None = None,
    ) -> float | None:
        """Probability that a boolean claim about the state holds."""
        question: dict[str, Any] = {"type": "boolean", "instructions": instructions}
        if criteria:
            question["criteria"] = criteria
        answer = await self._evaluate(state, question)
        if answer is None:
            return None
        value = answer.get("probability")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        logger.warning("Jev boolean answer had no probability; falling back")
        return None

    async def _evaluate(
        self, state: str, question: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.enabled:
            logger.warning("Jev requested but no gateway API key is set; skipping")
            return None
        payload = {
            "model": self.model,
            "state": state,
            "questions": {_QUESTION_ID: question},
        }
        self.calls += 1
        try:
            body = await self._post(payload)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            logger.warning(
                f"Jev evaluation failed ({type(exc).__name__}); falling back"
            )
            return None
        answers = body.get("answers") if isinstance(body, dict) else None
        answer = answers.get(_QUESTION_ID) if isinstance(answers, dict) else None
        if not isinstance(answer, dict):
            self.failures += 1
            logger.warning("Jev response had no usable answer; falling back")
            return None
        return answer

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
        retry=retry_if_exception(
            lambda exc: isinstance(exc, httpx.TransportError)
            or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in _RETRYABLE_STATUS
            )
        ),
        reraise=True,
    )
    async def _post(self, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self._transport
        ) as client:
            response = await client.post(
                f"{self.api_base}/evaluate",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            return response.json()
