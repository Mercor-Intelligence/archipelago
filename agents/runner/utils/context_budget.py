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

from collections.abc import Callable
from dataclasses import dataclass

from litellm import get_model_info, token_counter
from loguru import logger

from runner.agents.models import LitellmAnyMessage, get_msg_role

# Used when litellm cannot price the model (internal aliases, gateway-only
# names). Deliberately Gemini-sized rather than a conservative 128k: an
# unknown model that really has a 1M window should not have 90% of it
# trimmed away on the strength of a lookup miss.
FALLBACK_MAX_INPUT_TOKENS = 1_048_576

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

# Clamp on the learned ratio. A single anomalous step (a cache-only prompt, a
# provider that folds tool schemas in mid-run) must not be able to drive the
# budget to a nonsense value in either direction.
_MIN_RATIO = 0.5
_MAX_RATIO = 8.0


def resolve_max_input_tokens(model: str) -> int:
    """Model input-window size, with a fallback for unpriceable models.

    Only ``max_input_tokens`` is trusted outright. litellm's ``max_tokens``
    means different things in different entries: for the Anthropic models this
    agent actually runs it is the *completion* cap sitting next to a much
    larger input window (``claude-3-5-sonnet``: ``max_tokens`` 8192,
    ``max_input_tokens`` 200000), so reading it as the input window would set
    a ceiling ~25x too small and make ``_trim_limit``/``_trim_target`` shred
    history from the first step. It is accepted only when the entry also
    reports a strictly smaller ``max_output_tokens`` — the one shape in which
    ``max_tokens`` is provably the whole window rather than the output half of
    it. Everything else takes the fallback, and says so: an over-small window
    is otherwise completely silent, visible only as unexplained compaction.
    """
    try:
        info = get_model_info(model)
    except Exception:
        info = None
    if info is not None:
        max_input = info.get("max_input_tokens")
        if max_input:
            return int(max_input)
        max_tokens = info.get("max_tokens")
        max_output = info.get("max_output_tokens")
        if max_tokens and max_output and int(max_tokens) > int(max_output):
            return int(max_tokens)
    logger.bind(message_type="configure", model=model).warning(
        f"No usable max_input_tokens for {model!r} "
        f"(litellm's max_tokens is ambiguous and was not trusted); assuming a "
        f"{FALLBACK_MAX_INPUT_TOKENS}-token input window. Context trimming on "
        "this model is budgeting against a guess, not the real ceiling."
    )
    return FALLBACK_MAX_INPUT_TOKENS


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
            and estimate > prev_estimate
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
            # The ratio clamp (or a history that shrank below the anchor) left
            # the projection under a size the provider has already refused.
            # Re-anchor on the bound itself so it cannot happen twice.
            self._anchor_estimate = estimate
            self._anchor_reported = bound
        self._calibrated = True


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
