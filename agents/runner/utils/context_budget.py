"""Context-window budgeting for agent loops.

Two independent problems, both surfaced by BELLA-616 (six production runs
killed by ``prompt is too long: 1124770 tokens > 1000000 maximum``, with
``context_window_fallbacks=None`` so the 400 was unrecoverable):

1. **Nothing measures the prompt before it is sent.** ``trim_to_budget``
   drops the oldest assistant turns until the history is back under budget.
   A "turn" is an assistant message *plus its paired tool messages*, dropped
   as one unit — a ``tool_use`` whose ``tool_result`` went missing (or the
   reverse) is rejected outright by every provider, which would turn a
   recoverable overflow into an unrecoverable one. Ported from
   ``DeferredToolsAgent._apply_tail_drop``, which that agent keeps its own
   copy of for now; migrating it is a separate change with its own
   regression surface (a live Gemini agent) and no benefit to this fix.

2. **The in-process token estimate is not the number the provider bills.**
   litellm ships no Anthropic tokenizer for claude-3+, so ``token_counter``
   silently falls back to the OpenAI one — ``anthropic/claude-*`` and
   ``gpt-4o`` return the identical count. Measured on the payloads that
   killed those runs: 3.35-3.72 estimated chars/token against **2.54
   actually billed**, i.e. the estimate is 32-47% low, in the dangerous
   direction. It is also blind to the tool schemas, which cost ~106k tokens
   on the 333-tool gateway those runs used — 10.6% of the window consumed
   before the first token of work. A budget built on the raw estimate
   believes it has 850k of headroom while really having ~600k.

   ``PromptTokenProjector`` fixes both without a per-model constant: it
   anchors on the provider's own reported ``prompt_tokens`` from the previous
   call and scales only the *estimated growth* since then, so the tool-schema
   floor and any fixed per-request overhead ride along inside the anchor and
   only the marginal density has to be learned.

Counting is not free — measured ~190 ms on a 1M-token history — but it is
0.08% of an LLM call whose p100 in the failing batch was 241.8 s. Callers
still count once per step and reuse the number.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from litellm import get_model_info, token_counter
from loguru import logger

from runner.agents.models import LitellmAnyMessage, get_msg_role
from runner.utils.context_windows import lookup_context_window
from runner.utils.decorators import model_rates_ctx

# Used when litellm cannot price the model (internal aliases, gateway-only
# names). Deliberately Gemini-sized rather than a conservative 128k: an
# unknown model that really has a 1M window should not have 90% of it
# trimmed away on the strength of a lookup miss.
FALLBACK_MAX_INPUT_TOKENS = 1_048_576

# Fallback for harnesses that compact proactively on an estimate. Deliberately
# below FALLBACK_MAX_INPUT_TOKENS: over-estimating here means a provider 400.
FALLBACK_CONTEXT_WINDOW_TOKENS = 128_000

# Every harness's historical TRIGGER_FRACTION; not lowered because one fraction cannot fit two tokenizers.
DEFAULT_COMPACTION_CUTOFF = 0.70

# Operator-set cutoffs are clamped to this band. Above 0.9 the proactive trigger
# cannot fire before the provider rejects the prompt once the completion
# reservation is added; below 0.1 a harness summarizes on nearly every turn.
MIN_COMPACTION_CUTOFF = 0.1
MAX_COMPACTION_CUTOFF = 0.9


@dataclass(frozen=True)
class ContextWindow:
    """Input window and its origin:
    config | window_table | server_model_rates | litellm | fallback."""

    tokens: int
    source: str


def resolve_context_window(
    model: str,
    explicit: int | float | None = None,
    fallback: int = FALLBACK_CONTEXT_WINDOW_TOKENS,
    use_server_rates: bool = True,
) -> ContextWindow:
    """Agent config > checked-in table > server rate card > container litellm >
    ``fallback`` (warns). Pass ``use_server_rates=False`` for a harness-pinned
    model: the rate card describes the orchestrator's model."""
    tokens, source = resolve_max_input_tokens(
        model, explicit, fallback, use_server_rates=use_server_rates
    )
    return ContextWindow(tokens, source)


def context_budget_from_config(
    config: Mapping[str, object], default_cutoff: float = DEFAULT_COMPACTION_CUTOFF
) -> tuple[int | None, float]:
    """``(context_window_tokens, compaction_cutoff)`` from agent config; 0/unset = default."""
    raw_window = config.get("context_window_tokens")
    window: int | None = None
    if raw_window is not None and raw_window != 0:
        value = _as_number(raw_window)
        if value is None or value < 0:
            logger.bind(message_type="context").warning(
                f"Ignoring context_window_tokens={raw_window!r}; expected a "
                "positive token count. Resolving the window per model instead."
            )
        elif value > 0:
            window = int(value)
    cutoff = resolve_compaction_cutoff(
        config.get("context_compaction_cutoff"), default_cutoff
    )
    return window, cutoff


def _as_number(raw: object) -> float | None:
    """``raw`` as a finite float, or None. Bools and non-numeric strings are not
    numbers; numeric strings are, because config values arrive through the API
    and CLI as well as the typed Studio form."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def resolve_compaction_cutoff(
    raw: object, default: float = DEFAULT_COMPACTION_CUTOFF
) -> float:
    """A fraction clamped to ``[MIN_COMPACTION_CUTOFF, MAX_COMPACTION_CUTOFF]``.

    Unset or 0 means ``default``. A non-numeric or negative value falls back to
    ``default`` with a warning; an out-of-band number is clamped with a warning,
    because an operator who asked for 1.0 wants "as late as possible", not the
    default.
    """
    if raw is None or raw == 0:
        return default
    value = _as_number(raw)
    if value == 0:
        return default
    if value is None or value < 0:
        logger.bind(message_type="context").warning(
            f"Ignoring context_compaction_cutoff={raw!r}; expected a fraction in "
            f"[{MIN_COMPACTION_CUTOFF}, {MAX_COMPACTION_CUTOFF}]. Using {default}."
        )
        return default
    clamped = min(max(value, MIN_COMPACTION_CUTOFF), MAX_COMPACTION_CUTOFF)
    if clamped != value:
        logger.bind(message_type="context").warning(
            f"Clamping context_compaction_cutoff={raw!r} to {clamped}; the "
            f"supported band is [{MIN_COMPACTION_CUTOFF}, {MAX_COMPACTION_CUTOFF}]."
        )
    return float(clamped)


# Trim down to this fraction of the window rather than merely back under the
# limit. Trimming to exactly the limit re-triggers on almost every following
# step, and every trim invalidates the prompt cache from the first dropped
# message on — 76% of the prompt tokens in the failing batch were cache
# reads at ~0.1x rate, which a re-write bills at ~1.25x.
DEFAULT_TRIM_FLOOR = 0.60

# Ratio of provider-billed tokens to litellm-estimated tokens, used until the
# first two provider responses have been observed. 1.5 sits inside the
# measured 1.32-1.47 under-count band, biased high because under-projecting
# is the failure mode this module exists to prevent.
DEFAULT_CALIBRATION_RATIO = 1.5

# Clamp on the learned ratio, so one anomalous step (a cache-only prompt, a
# provider that folds tool schemas in mid-run) cannot drive the budget to a
# nonsense value. No real tokenizer gap exceeds ~1.5x; 3.0 leaves room for a
# schema-heavy step.
_MIN_RATIO = 0.5
_MAX_RATIO = 3.0

# Minimum estimated growth between two observations before the slope between
# them is trusted. A nudge turn adding 200 estimated tokens against 1,800 more
# reported would otherwise teach a ratio of 9.0: at that scale the reported
# delta is per-request framing, cache boundaries and provider rounding, not
# density. The anchor still moves on such a step; only the slope is skipped.
_MIN_SLOPE_DELTA = 2_000

# ``_hidden_params`` marker set by ``generate_response`` on a streamed response
# rebuilt without a usage chunk: litellm's ``stream_chunk_builder`` fills the
# missing ``prompt_tokens`` with its own ``token_counter`` estimate, which is
# not billed truth and must not calibrate the projector.
USAGE_IS_ESTIMATE_KEY = "usage_is_estimate"


def input_window_from_model_info(info: Mapping[str, object] | None) -> int | None:
    """The input window a litellm model-info entry proves, or None.

    Only ``max_input_tokens`` is trusted outright. litellm's ``max_tokens``
    means different things in different entries: for the Anthropic models this
    agent actually runs it is the *completion* cap sitting next to a much
    larger input window (``claude-3-5-sonnet``: ``max_tokens`` 8192,
    ``max_input_tokens`` 200000), so reading it as the input window would set
    a ceiling ~25x too small. It is accepted only when the entry also reports
    a strictly smaller ``max_output_tokens`` — the one shape in which
    ``max_tokens`` is provably the whole window rather than the output half.
    """
    if info is None:
        return None
    max_input = info.get("max_input_tokens")
    if isinstance(max_input, (int, float)) and max_input > 0:
        return int(max_input)
    max_tokens = info.get("max_tokens")
    max_output = info.get("max_output_tokens")
    if (
        isinstance(max_tokens, (int, float))
        and isinstance(max_output, (int, float))
        and max_tokens > max_output > 0
    ):
        return int(max_tokens)
    return None


def resolve_max_input_tokens(
    model: str,
    explicit: int | float | None = None,
    fallback: int = FALLBACK_MAX_INPUT_TOKENS,
    use_server_rates: bool = True,
) -> tuple[int, str]:
    """``(window, source)``. config > table > server rate card > container litellm > ``fallback``.

    The rate card is run through ``input_window_from_model_info``, so both
    ``max_input_tokens`` and a proven ``max_tokens`` (strictly above
    ``max_output_tokens``) count. Default ``fallback`` is Gemini-sized for the
    trim path; compaction callers pass a smaller last resort. An assumed
    window is logged — silent fallbacks show up as unexplained compaction.
    Pass ``use_server_rates=False`` when ``model`` is a harness-pinned model
    rather than the orchestrator's.
    """
    provider = _provider_window(model, use_server_rates)

    pinned = _as_number(explicit) if explicit is not None else None
    if pinned is not None and pinned > 0:
        if provider is not None and pinned > provider[0]:
            # Kept, not clamped (operator decision); warn because compaction would fire only after a provider 400.
            logger.bind(message_type="context", model=model).warning(
                f"context_window_tokens={int(pinned)} exceeds the {provider[0]}-token "
                f"window {provider[1]} reports for {model!r}; prompts above the "
                "provider's own limit will be rejected before compaction fires."
            )
        return int(pinned), "config"

    if provider is not None:
        return provider

    logger.bind(message_type="context", model=model).warning(
        f"No usable max_input_tokens for {model!r}; falling back to {fallback} "
        "tokens. Set context_window_tokens on the agent to make this deterministic."
    )
    return fallback, "fallback"


def _provider_window(model: str, use_server_rates: bool) -> tuple[int, str] | None:
    """The window the checked-in table, the server rate card or the container's
    litellm proves, or None."""
    tabled = lookup_context_window(model)
    if tabled is not None:
        return tabled, "window_table"
    rates = (model_rates_ctx.get() or {}) if use_server_rates else {}
    threaded = input_window_from_model_info(rates)
    if threaded is not None:
        return threaded, "server_model_rates"
    try:
        info = get_model_info(model)
    except Exception:  # noqa: BLE001 — litellm raises a plain Exception on unknown models
        info = None
    resolved = input_window_from_model_info(info)
    if resolved is not None:
        return resolved, "litellm"
    return None


def count_tokens(model: str, messages: list[LitellmAnyMessage]) -> int:
    """litellm's token estimate for a message list, with a char/4 fallback.

    This is a *raw estimate*, not a prediction of what the provider will
    bill — see the module docstring. Feed it through ``PromptTokenProjector``
    before comparing it against a window size.
    """
    if not messages:
        return 0
    try:
        return token_counter(model=model, messages=messages)
    except Exception:
        return len(str(messages)) // 4


def reported_prompt_tokens(response: object) -> int:
    """The provider-reported ``prompt_tokens`` on a litellm response, or 0.

    Accepts a ``ModelResponse`` (``response.usage.prompt_tokens``) or a plain
    dict such as a Responses-API dump (``usage["prompt_tokens"]`` or
    ``usage["input_tokens"]``). Missing, non-numeric or non-positive values
    read as 0 so callers can skip the observation rather than poison the
    projector with a bogus anchor. So does a usage the runner synthesized
    itself (``USAGE_IS_ESTIMATE_KEY`` in ``_hidden_params``): billing may keep
    the estimate, calibration may not.
    """
    if usage_is_estimate(response):
        return 0
    usage: object = getattr(response, "usage", None)
    if usage is None and isinstance(response, Mapping):
        usage = response.get("usage")
    if usage is None:
        return 0
    raw: object = getattr(usage, "prompt_tokens", None)
    if raw is None and isinstance(usage, Mapping):
        raw = usage.get("prompt_tokens") or usage.get("input_tokens")
    try:
        value = int(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def usage_is_estimate(response: object) -> bool:
    """True when ``response`` carries the ``USAGE_IS_ESTIMATE_KEY`` marker, i.e.
    its usage was computed in-process rather than reported by the provider."""
    hidden: object = getattr(response, "_hidden_params", None)
    if hidden is None and isinstance(response, Mapping):
        hidden = response.get("_hidden_params")
    return isinstance(hidden, Mapping) and bool(hidden.get(USAGE_IS_ESTIMATE_KEY))


# Provider overflow messages: ``prompt``, ``limit`` and (where stated) the
# ``completion`` reservation; digits may be comma-grouped.
_NUM = r"\d[\d,]*"
# Overflow text is short; scanning past this can't contain more counts.
_MAX_OVERFLOW_SCAN = 4096
_OVERFLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic: "prompt is too long: 1124770 tokens > 1000000 maximum"
    re.compile(
        rf"prompt is too long:\s*(?P<prompt>{_NUM})\s*tokens\s*>\s*(?P<limit>{_NUM})\s*maximum",
        re.IGNORECASE,
    ),
    # Anthropic, completion-bound: "input length and `max_tokens` exceed
    # context limit: 190000 + 20000 > 200000"
    re.compile(
        rf"input length and .?max_tokens.? exceed context limit:\s*"
        rf"(?P<prompt>{_NUM})\s*\+\s*(?P<completion>{_NUM})\s*>\s*(?P<limit>{_NUM})",
        re.IGNORECASE,
    ),
    # Gemini: "input token count (1200000) exceeds the maximum number of
    # tokens allowed (1048576)"
    re.compile(
        rf"input token count \((?P<prompt>{_NUM})\) exceeds the maximum number "
        rf"of tokens allowed \((?P<limit>{_NUM})\)",
        re.IGNORECASE,
    ),
    # OpenAI: "This model's maximum context length is 128000 tokens. However,
    # you requested 131000 tokens (1000 in the messages, 130000 in the
    # completion)". The messages part is the prompt; without the breakdown the
    # requested total includes the completion reservation and is not returned
    # as a prompt size.
    re.compile(
        rf"maximum context length is (?P<limit>{_NUM}) tokens\..*?"
        rf"you requested {_NUM} tokens"
        rf"(?:\s*\((?P<prompt>{_NUM}) in (?:the|your) (?:messages|prompt)"
        rf"(?:,\s*(?P<completion>{_NUM}) in the completion)?)?",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _group_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def overflow_numbers(message: str) -> tuple[int | None, int | None, int | None]:
    """``(prompt, completion, limit)`` a provider's overflow text states; ``None`` where it does not.
    The limit is the window the lane enforces, which can be below the tabled one."""
    # The counts sit at the head of the provider's error; cap the scan so a DOTALL pattern can't go O(n^2) on a pathologically long message.
    message = message[:_MAX_OVERFLOW_SCAN]
    for pattern in _OVERFLOW_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        groups = match.groupdict()
        return (
            _group_int(groups.get("prompt")),
            _group_int(groups.get("completion")),
            _group_int(groups.get("limit")),
        )
    return None, None, None


def parse_context_overflow(message: str) -> tuple[int | None, int | None]:
    """``(prompt, limit)`` from :func:`overflow_numbers`."""
    prompt, _, limit = overflow_numbers(message)
    return prompt, limit


def overflow_is_completion_bound(message: str) -> bool:
    """True when the ``max_tokens`` reservation alone fills the window: no compaction can make room.
    A fitting prompt whose total overran is recoverable and is summarized."""
    _, completion, limit = overflow_numbers(message)
    return completion is not None and limit is not None and completion >= limit


def group_turns(messages: list[LitellmAnyMessage]) -> list[list[LitellmAnyMessage]]:
    """Group messages into turns: each turn is ``[assistant, tool*]``.

    A non-tool message starts a new turn, so an assistant message and every
    tool message answering its ``tool_calls`` stay together and are dropped
    together. This is the whole of the ``tool_use``/``tool_result`` pairing
    guarantee: the loop only ever appends tool messages directly after the
    assistant message that requested them, so "contiguous run of tool
    messages after an assistant" and "that assistant's tool results" are the
    same set.
    """
    turns: list[list[LitellmAnyMessage]] = []
    current: list[LitellmAnyMessage] = []
    for msg in messages:
        if get_msg_role(msg) == "tool":
            current.append(msg)
        else:
            if current:
                turns.append(current)
            current = [msg]
    if current:
        turns.append(current)
    return turns


class PromptTokenProjector:
    """Projects provider-billed prompt tokens from a raw litellm estimate.

    Self-calibrating, per the module docstring::

        ratio     = (reported_n - reported_{n-1}) / (estimate_n - estimate_{n-1})
        projected = reported_n + ratio * (estimate_now - estimate_n)

    Anchoring on the last *reported* value rather than scaling the whole
    estimate is what lets a fixed per-request overhead the estimator cannot
    see (tool schemas, system-level wrappers) be absorbed for free: it sits
    inside ``reported_n`` and never has to be modelled.
    """

    def __init__(
        self, model: str, *, default_ratio: float = DEFAULT_CALIBRATION_RATIO
    ) -> None:
        self.model: str = model
        self._ratio: float = default_ratio
        self._calibrated: bool = False
        self._anchor_estimate: int | None = None
        self._anchor_reported: int | None = None

    @property
    def ratio(self) -> float:
        """Billed tokens per estimated token, as currently believed."""
        return self._ratio

    @property
    def calibrated(self) -> bool:
        """True once the ratio has been learned from provider feedback."""
        return self._calibrated

    def estimate(self, messages: list[LitellmAnyMessage]) -> int:
        """Raw litellm estimate for a message list."""
        return count_tokens(self.model, messages)

    def project(self, estimate: int) -> int:
        """Projected billed prompt tokens for a history whose raw estimate is
        ``estimate``."""
        if self._anchor_estimate is None or self._anchor_reported is None:
            return max(int(estimate * self._ratio), 0)
        delta = estimate - self._anchor_estimate
        return max(int(self._anchor_reported + self._ratio * delta), 0)

    def scale(self, estimate: int) -> int:
        """Calibrated *marginal* cost of adding content whose raw estimate is
        ``estimate`` — the projection's slope only, with no anchor offset.

        Use this to price an individual message; use :meth:`project` for a
        whole history.
        """
        return max(int(estimate * self._ratio), 0)

    def invert(self, projected: int) -> int:
        """Raw estimate corresponding to a projected billed-token count.

        The inverse of :meth:`project`, so a budget expressed in real
        (billed) tokens can be handed to code that measures in raw estimates
        — which is what makes per-turn estimates subtractable, since the
        projection is affine and its offset would otherwise be counted once
        per turn.
        """
        if self._anchor_estimate is None or self._anchor_reported is None:
            return max(int(projected / self._ratio), 0)
        return max(
            int(
                self._anchor_estimate
                + (projected - self._anchor_reported) / self._ratio
            ),
            0,
        )

    def observe(self, estimate: int, reported: int) -> None:
        """Fold one provider-reported ``prompt_tokens`` into the calibration.

        ``estimate`` must be the raw estimate of the exact message list that
        produced ``reported``. The slope between consecutive observations is
        the marginal density; the newest observation becomes the anchor.
        """
        if reported <= 0 or estimate <= 0:
            return
        prev_estimate = self._anchor_estimate
        prev_reported = self._anchor_reported
        if (
            prev_estimate is not None
            and prev_reported is not None
            and estimate - prev_estimate >= _MIN_SLOPE_DELTA
            and reported > prev_reported
        ):
            slope = (reported - prev_reported) / (estimate - prev_estimate)
            self._ratio = min(max(slope, _MIN_RATIO), _MAX_RATIO)
            self._calibrated = True
        self._anchor_estimate = estimate
        self._anchor_reported = reported

    def observe_overflow(self, estimate: int, hard_limit: int) -> None:
        """Fold a provider *rejection* into the calibration.

        A ``ContextWindowExceededError`` is ground truth of a different shape:
        it says the true prompt size for ``estimate`` was above ``hard_limit``,
        i.e. that :meth:`project` under-shot. That is a lower bound and only
        ever pushes the projection up — a rejection can never justify
        believing a history is cheaper than we thought.
        """
        if estimate <= 0 or hard_limit <= 0:
            return
        bound = hard_limit + 1
        if self.project(estimate) >= bound:
            # Already projecting over the limit: the rejection agrees with us
            # and teaches nothing.
            return
        if self._anchor_estimate is not None and self._anchor_reported is not None:
            delta = estimate - self._anchor_estimate
            if delta > 0:
                implied = (bound - self._anchor_reported) / delta
                self._ratio = min(max(implied, self._ratio), _MAX_RATIO)
        else:
            self._ratio = min(max(bound / estimate, self._ratio), _MAX_RATIO)
        if self.project(estimate) < bound:
            # The projection is still under a size the provider has already
            # refused. Re-anchor on the bound so that cannot repeat, and take
            # the slope from the average density the rejection proves
            # (``bound / estimate``) rather than the clamp ceiling, which would
            # zero the projection across the range below the anchor.
            self._anchor_estimate = estimate
            self._anchor_reported = bound
            self._ratio = min(max(bound / estimate, _MIN_RATIO), _MAX_RATIO)
        self._calibrated = True


def observe_provider_overflow(
    projector: PromptTokenProjector,
    estimate: int,
    *,
    window: int,
    error: Exception | None = None,
    hard_limit: int | None = None,
) -> None:
    """Fold a provider rejection of a history estimating ``estimate`` into
    ``projector``, preferring the provider's own numbers.

    When ``error`` states the prompt size it counted, that exact figure is
    observed (truth beats a lower bound). Otherwise the rejection is a lower
    bound above ``hard_limit`` (default ``window``), capped by any limit the
    message names. A named limit below ``window`` is logged: the checked-in
    window may be wrong for this lane.
    """
    prompt_reported, limit_reported = (
        parse_context_overflow(str(error)) if error is not None else (None, None)
    )
    limit = hard_limit or window
    if limit_reported is not None:
        if limit_reported < window:
            logger.bind(message_type="context", model=projector.model).warning(
                f"Provider rejected the prompt against a {limit_reported}-token "
                f"limit, below the {window}-token window resolved for "
                f"{projector.model!r}; the checked-in window may be wrong for "
                "this lane."
            )
        limit = min(limit_reported, limit)
    if prompt_reported is not None:
        # The provider counted this exact list: anchor on it directly instead
        # of inferring a slope from "somewhere above the limit".
        projector.observe(estimate, prompt_reported)
        return
    projector.observe_overflow(estimate, limit)


@dataclass(frozen=True)
class TrimResult:
    """Outcome of one :func:`trim_to_budget` call. Token counts are raw
    estimates, in the same space as the ``count`` callable that produced
    them."""

    messages: list[LitellmAnyMessage]
    dropped_turns: int
    dropped_messages: int
    tokens_before: int
    tokens_after: int

    @property
    def trimmed(self) -> bool:
        return self.dropped_turns > 0


def trim_to_budget(
    messages: list[LitellmAnyMessage],
    *,
    anchor_count: int,
    limit: int,
    floor: int | None = None,
    count: Callable[[list[LitellmAnyMessage]], int],
    force: bool = False,
) -> TrimResult:
    """Drop the oldest assistant turns until the history fits the budget.

    Args:
        messages: Full history. Never mutated; a new list is returned.
        anchor_count: Leading messages that must survive — the system prompt
            and the original task prompt. Without them the conversation stops
            opening on its own instructions.
        limit: Trimming starts only once the history exceeds this.
        floor: Trim down to this instead of merely back under ``limit``.
            Defaults to ``limit`` (trim as little as possible), but callers
            should pass something lower; see ``DEFAULT_TRIM_FLOOR``.
        count: Token measurement, injected so the caller controls the unit.
        force: Drop at least one turn even if already under ``limit`` — used
            on the recovery path, where the provider has already contradicted
            our measurement and resending identical bytes cannot succeed.

    Two structural guarantees, both load-bearing:

    * **Pairing.** Turns are dropped whole (:func:`group_turns`), so no
      surviving ``tool`` message is ever separated from the assistant
      ``tool_calls`` that produced it.
    * **The newest turn is never dropped** — it is excluded from the scan
      itself (``turns[:-1]``), not merely from a length check. If the anchor
      plus the newest turn alone exceed the budget this returns over-budget
      rather than emptying the history — that case belongs to a per-step
      tool-result budget, not to tail-drop.

    A trailing *user* message (the trim notice, the "continue" nudge) is a
    turn of its own, so the newest assistant turn behind it stays droppable.
    That is deliberate: the anchor is the system prompt plus the original
    task, so the most-trimmed history this can produce is "the task, restated,
    with a notice saying the rest is gone" — a recoverable state, not an
    empty conversation.
    """
    tokens_before = count(messages)
    target = min(floor if floor is not None else limit, limit)
    if tokens_before <= limit and not force:
        return TrimResult(messages, 0, 0, tokens_before, tokens_before)

    anchor = messages[:anchor_count]
    turns = group_turns(messages[anchor_count:])
    # Measure each turn once and subtract as turns are dropped, instead of
    # recounting the whole history per drop — a full recount is ~190 ms on a
    # 1M-token history and a single trim can drop dozens of turns.
    turn_costs = [count(turn) for turn in turns]
    dropped_turns = 0
    dropped_messages = 0

    def drop_oldest_assistant_turn() -> int | None:
        """Drop the oldest assistant-headed turn, returning its measured cost.
        None when there is nothing droppable left."""
        nonlocal dropped_turns, dropped_messages
        if len(turns) <= 1:
            return None
        # Scan everything *but the newest turn*. A length check alone does not
        # protect it: this loop appends a user-role message after every trim
        # (and after every empty model reply), so histories interleave as
        # [assistant, user, assistant]. Dropping the leading assistant there
        # leaves [user, assistant] — two turns, so a `len(turns) > 1` guard
        # still passes — and the next scan finds the newest turn as the first
        # assistant-headed one and drops it, emptying the history the
        # guarantee exists to protect.
        idx = next(
            (
                i
                for i, turn in enumerate(turns[:-1])
                if get_msg_role(turn[0]) == "assistant"
            ),
            None,
        )
        if idx is None:
            # Nothing assistant-headed left to drop: user messages carry no
            # tool results and dropping them would not free meaningful space.
            return None
        dropped_messages += len(turns[idx])
        turns.pop(idx)
        dropped_turns += 1
        return turn_costs.pop(idx)

    running = tokens_before
    while running > target or (force and dropped_turns == 0):
        cost = drop_oldest_assistant_turn()
        if cost is None:
            break
        running -= cost

    if not dropped_turns:
        return TrimResult(messages, 0, 0, tokens_before, tokens_before)

    # Per-turn estimates are near-additive but not exact (per-message framing
    # overhead), so settle the last turn or two against a real count — the
    # budget has to be a fact, not an approximation.
    trimmed = anchor + [msg for turn in turns for msg in turn]
    tokens_after = count(trimmed)
    while tokens_after > target and drop_oldest_assistant_turn() is not None:
        trimmed = anchor + [msg for turn in turns for msg in turn]
        tokens_after = count(trimmed)

    return TrimResult(
        trimmed, dropped_turns, dropped_messages, tokens_before, tokens_after
    )
