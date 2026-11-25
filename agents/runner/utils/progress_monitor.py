"""Progress-aware run limits for the ReAct Toolbelt harness.

Off unless an agent sets ``progress_mode``. With it on:

* the base wall clock and step cap become a floor. A run still progressing
  when it reaches either is extended in chunks, up to hard ceilings;
* a run that repeats itself mechanically is stopped as a model outcome
  (``loop_detection``);
* a run where nothing completes for ``stall_window_s`` is stopped, and so
  is one whose model call gives up after its retries on timeouts (a
  provider that never answers is a stall, not the model's slowness);
* with ``snapshot_on_stop``, a step, wall-clock or loop stop marks its
  output so modal_labs snapshots the end state, and with ``grade_on_stop``
  Studio grades it (STOP_SNAPSHOT_MARKER and the keys beside it).

Progress in the last ``progress_window_s`` means at least
``progress_min_steps`` steps, at least ``progress_min_new_calls`` never-seen
(tool, canonical args) pairs or one mutating call, and no loop verdict in that
window. With ``loop_detection`` off a verdict is only a signal, so one that has
aged out of the window no longer blocks an extension. A limit extended before
also needs a tool-call step since: a failed or tool-free turn sends no call,
so a streak of them earns at most one chunk on the window's older work.

A call is compared by tool name plus a sha256 of its canonical JSON arguments.
Only digests are kept, and logs carry tool names and digest prefixes, never
argument values.

Loop rules, over the last ``loop_window_steps`` (N) steps with
K = ``loop_repeat_k``; meta tools are ignored:

* L1 exact repeat: one call in at least K of the N steps;
* L2 short cycle: the last p*max(K-1, 2) step signatures repeat with period 2 or 3;
* L3 re-read: one read-only call in at least max(K-1, 2) of the N steps, with no
  mutating call in any step from its first appearance through its last;
* L4 zero novelty: no new call in the last N steps that made a non-meta call.
  Steps with only meta calls (todo_write, toolbelt edits) or no tool call at
  all are skipped: they neither count toward N nor break the streak.

A near-duplicate (strings lower-cased, digits zeroed, paging keys dropped) in
all N steps is logged and counted, never a stop: paging through a ledger
legitimately matches it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol, cast

from loguru import logger

from runner.utils.compaction.resum import summary_worst_case_s
from runner.utils.context_budget import compaction_timeout_from_config
from runner.utils.logging.terminal_error import (
    harness,
    llm_connection,
    model_timeout,
    tool_timeout,
)
from runner.utils.settings import get_settings

StallStatus = Literal["error", "failed"]
STALL_STATUSES: tuple[StallStatus, ...] = ("error", "failed")

# Kept free below the runner's AGENT_TIMEOUT_SECONDS for the end-of-run
# snapshot of a stopped run, which happens after the agent returns but inside
# Modal's function timeout.
STOP_SNAPSHOT_RESERVE_S = 1800

# The most hard_wall_clock_s can ask for. A run is then clamped below the
# runner's AGENT_TIMEOUT_SECONDS (clamp_hard_wall_clock_s), which today is the
# lower of the two, about 22 h.
HARD_WALL_CLOCK_CAP_S = 48 * 60 * 60

# Which limit set a run's hard wall ceiling (ProgressPolicy.ceiling_limit):
# hard_wall_clock_s as configured (or the base wall clock when unset), the
# cap above, or the clamp below the runner's AGENT_TIMEOUT_SECONDS.
CEILING_CONFIGURED = "configured"
CEILING_CAP = f"{HARD_WALL_CLOCK_CAP_S // 3600}h cap"
CEILING_RUNNER_CLAMP = "runner clamp"

# Snapshot on stop (`snapshot_on_stop`): the output markers a stopped run
# carries for modal_labs, which snapshots a FAILED run only when the first is
# True, and for Studio, which grades a FAILED run only when `grade_on_stop` is
# True and the run has a snapshot. Only the agent sets them.
STOP_SNAPSHOT_MARKER = "stop_snapshot"
STOP_SNAPSHOT_TIMEOUT_MARKER = "stop_snapshot_timeout_s"
GRADE_ON_STOP_MARKER = "grade_on_stop"
# The ends that get a snapshot. A stall does not: whatever hung may be the
# sandbox itself, and with the default stall_status the run is ERROR and
# retried from scratch, so its end state would never be read.
STOP_SNAPSHOT_ENDS = frozenset({"steps", "wall_clock", "wall_clock_hard", "loop"})
# The least `stop_snapshot_timeout_s` accepted, as the descriptor's min_value.
STOP_SNAPSHOT_TIMEOUT_MIN_S = 60

# `output.attempt_outcome` for the stops that are not budgets.
LOOP_OUTCOME = "loop"
STALL_OUTCOME = "stall"

# The longest sleep generate_response's with_retry makes between two attempts:
# base_backoff 5 * 2**(5 - 2) before the 5th attempt, plus up to 5 s jitter
# (runner/utils/llm.py). Each attempt start resets the stall timer, so the
# stall window must outlast one attempt plus this sleep.
LLM_RETRY_BACKOFF_MAX_S = 45.0
# Slack on top of that minimum: an attempt's own timeout fires a little late
# (event-loop lag, connection teardown), and a window cut exactly to one
# attempt plus the backoff would read that lateness as a stall.
STALL_WINDOW_MARGIN_S = 60.0

# Slack on the wall-clock budget the runner declares for a progress-mode run:
# after the backstop fires the agent still closes the MCP session and builds
# its output, which must not read as overrunning the declared budget.
DECLARED_BUDGET_SLACK_S = 60

# Paging and size keys a near-duplicate ignores.
_NEAR_DUPLICATE_DROPPED_KEYS = frozenset(
    {
        "limit",
        "offset",
        "page",
        "page_size",
        "cursor",
        "max_results",
        "summary_limit",
        "start",
        "end",
        "start_page",
        "end_page",
        "page_token",
        "per_page",
        "max_chars",
        "max_lines",
        "timeout",
    }
)
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+(\.\d+)?")
_HASH_PREFIX = 12


class _Config(Protocol):
    def get(self, descriptor_id: str, default: Any = None) -> Any: ...


class ProgressConfigError(ValueError):
    """A progress-mode setting that cannot run; ``descriptor`` names it."""

    def __init__(self, descriptor: str, message: str) -> None:
        super().__init__(message)
        self.descriptor: str = descriptor


def _flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def config_flag(value: object) -> bool:
    """A boolean progress knob as the policy reads it: ``True`` or "true".

    For a caller that reports a knob without building the policy, so its
    reading always matches the policy's.
    """
    return _flag(value)


def progress_mode_on(value: object) -> bool:
    """Whether a ``progress_mode`` value turns progress mode on.

    True for ``True`` or a string reading "true" after strip and lower-case,
    the same reading the agent gives it, so the runner's declared budget and
    the agent's limits always agree.
    """
    return _flag(value)


def config_number(key: str, raw: object, *, low: float) -> float:
    """``raw`` as a finite number >= ``low``; numeric strings from the API are read.

    Raises ProgressConfigError naming ``key`` otherwise, which the agent reports
    as a ``harness_contract`` fault.
    """
    try:
        value = (
            math.nan if raw is None or isinstance(raw, bool) else float(cast(Any, raw))
        )
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value < low:
        raise ProgressConfigError(key, f"{key}={raw!r} must be a number >= {low:g}")
    return value


def _number(config: _Config, key: str, default: float, *, low: float) -> float:
    raw = config.get(key)
    if raw is None:
        return default
    return config_number(key, raw, low=low)


def longest_call_s(*timeouts: object) -> float:
    """The longest of the numeric per-call timeouts given; others are ignored."""
    numbers: list[float] = []
    for timeout in timeouts:
        try:
            numbers.append(config_number("timeout", timeout, low=0))
        except ProgressConfigError:
            continue
    return max(numbers, default=0.0)


def compaction_grace_s(
    config: _Config, *, model: str = "", extra_args: dict[str, Any] | None = None
) -> float:
    """The longest one compaction can take, as ReActAgent's ReSumManager makes it
    for ``model`` and the orchestrator's ``extra_args`` (summary_worst_case_s)."""
    raw = config.get("context_compaction_call_timeout_sec")
    timeout = compaction_timeout_from_config({"context_compaction_timeout": raw})
    return summary_worst_case_s(model, extra_args, timeout)


def clamp_hard_wall_clock_s(
    hard_wall_clock_s: float, grace_s: float, agent_timeout_s: float
) -> float:
    """The hard wall ceiling, lowered so backstop + stop-snapshot reserve fit.

    Past AGENT_TIMEOUT_SECONDS the runner ends the run ERROR with an empty
    transcript, so the backstop (ceiling + one step's grace) plus the
    stop-snapshot reserve must stay below it.
    """
    ceiling = agent_timeout_s - grace_s - STOP_SNAPSHOT_RESERVE_S
    if ceiling <= 0:
        raise ProgressConfigError(
            "hard_wall_clock_s",
            f"call timeouts ({grace_s:g}s) leave no room below "
            f"AGENT_TIMEOUT_SECONDS ({agent_timeout_s:g}s)",
        )
    return min(hard_wall_clock_s, ceiling)


def declared_backstop_s(
    config: _Config,
    *,
    agent_timeout_s: float | None = None,
    model: str = "",
    extra_args: dict[str, Any] | None = None,
) -> int | None:
    """How long a progress-mode run can take: the clamped hard ceiling plus one
    step's grace, as the agent's backstop computes it. None when the config
    cannot run (the agent then ends the run ``harness_contract``).

    Reads the defaults ReActAgent falls back to for unset timeouts; the
    compaction grace needs the run's orchestrator ``model`` and ``extra_args``.
    """
    if agent_timeout_s is None:
        agent_timeout_s = float(get_settings().AGENT_TIMEOUT_SECONDS)
    try:
        base = config_number(
            "wall_clock_timeout_sec",
            config.get("wall_clock_timeout_sec", 10800),
            low=1,
        )
        grace = (
            config_number(
                "llm_call_timeout_sec", config.get("llm_call_timeout_sec", 600), low=1
            )
            + config_number(
                "tool_call_timeout_sec", config.get("tool_call_timeout_sec", 180), low=1
            )
            + compaction_grace_s(config, model=model, extra_args=extra_args)
        )
        hard = min(
            max(_number(config, "hard_wall_clock_s", base, low=1), base),
            HARD_WALL_CLOCK_CAP_S,
        )
        return math.ceil(clamp_hard_wall_clock_s(hard, grace, agent_timeout_s) + grace)
    except ProgressConfigError:
        return None


@dataclass(frozen=True)
class ProgressPolicy:
    hard_wall_clock_s: float
    hard_max_steps: int
    window_s: float = 1800
    min_steps: int = 5
    min_new_calls: int = 3
    extension_s: float = 7200
    extension_steps: int = 100
    loop_detection: bool = False
    loop_repeat_k: int = 4
    loop_window_steps: int = 8
    stall_window_s: float = 0
    stall_status: StallStatus = "error"
    # One compaction, one LLM call and one tool call: how far past the hard
    # ceiling the backstop lets an in-flight step run.
    grace_s: float = 0
    # The base wall clock and step cap, read as numbers (the agent's own
    # attributes keep whatever type the config gave).
    base_wall_clock_s: float = 0
    base_max_steps: int = 0
    # Which limit set hard_wall_clock_s: CEILING_CONFIGURED, CEILING_CAP or
    # CEILING_RUNNER_CLAMP. A stop at the ceiling names it.
    ceiling_limit: str = CEILING_CONFIGURED
    # Snapshot on stop: off by default. See STOP_SNAPSHOT_ENDS.
    snapshot_on_stop: bool = False
    grade_on_stop: bool = False
    stop_snapshot_timeout_s: float = STOP_SNAPSHOT_RESERVE_S

    @property
    def backstop_s(self) -> float:
        """The asyncio.timeout around the whole run; always below AGENT_TIMEOUT_SECONDS."""
        return self.hard_wall_clock_s + self.grace_s

    def stop_markers(self, end: str) -> dict[str, Any]:
        """The snapshot-on-stop output markers for a run that ended at ``end``.

        Empty unless ``snapshot_on_stop`` is on and ``end`` is a step, wall
        clock or loop stop. ``grade_on_stop`` rides only with a snapshot: Studio
        never grades a stopped run without one.
        """
        if not self.snapshot_on_stop or end not in STOP_SNAPSHOT_ENDS:
            return {}
        timeout = float(self.stop_snapshot_timeout_s)
        markers: dict[str, Any] = {
            STOP_SNAPSHOT_MARKER: True,
            STOP_SNAPSHOT_TIMEOUT_MARKER: int(timeout)
            if timeout.is_integer()
            else timeout,
        }
        if self.grade_on_stop:
            markers[GRADE_ON_STOP_MARKER] = True
        return markers

    @classmethod
    def from_config(
        cls,
        config: _Config,
        *,
        base_wall_clock_s: float,
        base_max_steps: int,
        grace_s: float,
        longest_call_s: float,
        agent_timeout_s: float | None = None,
    ) -> ProgressPolicy:
        """Read and validate the progress-mode keys; raises ProgressConfigError.

        The base limits may arrive as numeric strings; anything else that is
        not a positive number is a ProgressConfigError naming its key.

        The hard wall ceiling is raised to the base wall clock or lowered to
        HARD_WALL_CLOCK_CAP_S, with a warning, then clamped so the backstop plus the
        stop-snapshot reserve stays below the runner's AGENT_TIMEOUT_SECONDS:
        past that the runner ends the run ERROR with an empty transcript.
        `ceiling_limit` records which of the three set it.
        """
        if agent_timeout_s is None:
            agent_timeout_s = float(get_settings().AGENT_TIMEOUT_SECONDS)
        base_wall_clock_s = config_number(
            "wall_clock_timeout_sec", base_wall_clock_s, low=1
        )
        base_max_steps = int(config_number("max_steps", base_max_steps, low=1))
        hard_wall = _number(config, "hard_wall_clock_s", base_wall_clock_s, low=1)
        if hard_wall < base_wall_clock_s:
            logger.bind(message_type="progress").warning(
                f"hard_wall_clock_s {hard_wall:g}s is below the base wall clock "
                f"({base_wall_clock_s:g}s); using {base_wall_clock_s:g}s "
                "(no wall-clock extension)"
            )
            hard_wall = base_wall_clock_s
        hard_steps = int(_number(config, "hard_max_steps", base_max_steps, low=1))
        if hard_steps < base_max_steps:
            logger.bind(message_type="progress").warning(
                f"hard_max_steps {hard_steps} is below max_steps ({base_max_steps}); "
                f"using {base_max_steps} (no step extension)"
            )
            hard_steps = base_max_steps
        ceiling_limit = CEILING_CONFIGURED
        if hard_wall > HARD_WALL_CLOCK_CAP_S:
            logger.bind(message_type="progress").warning(
                f"hard_wall_clock_s {hard_wall:g}s lowered to the "
                f"{HARD_WALL_CLOCK_CAP_S}s ({CEILING_CAP})"
            )
            hard_wall = HARD_WALL_CLOCK_CAP_S
            ceiling_limit = CEILING_CAP
        ceiling = clamp_hard_wall_clock_s(hard_wall, grace_s, agent_timeout_s)
        if hard_wall > ceiling:
            logger.bind(message_type="progress").warning(
                f"hard_wall_clock_s {hard_wall:g}s clamped to {ceiling:g}s: the "
                f"backstop (+{grace_s:g}s call grace) and the "
                f"{STOP_SNAPSHOT_RESERVE_S}s stop-snapshot reserve must stay below "
                f"AGENT_TIMEOUT_SECONDS ({agent_timeout_s:g}s)"
            )
            hard_wall = ceiling
            ceiling_limit = CEILING_RUNNER_CLAMP
        k = int(_number(config, "loop_repeat_k", 4, low=2))
        window_steps = int(_number(config, "loop_window_steps", 8, low=2))
        if window_steps < k:
            raise ProgressConfigError(
                "loop_window_steps",
                f"loop_window_steps={window_steps} is below loop_repeat_k={k}",
            )
        stall_window = _number(config, "stall_window_s", 0, low=0)
        # Every LLM attempt start and tool attempt start resets the stall
        # timer (ProgressMonitor.heartbeat), so the longest quiet gap is one
        # attempt plus the retry backoff before the next; the margin absorbs
        # an attempt timeout that fires late. A whole retried call can run far
        # longer (5 x the LLM timeout) without reading as a stall.
        min_stall = longest_call_s + LLM_RETRY_BACKOFF_MAX_S + STALL_WINDOW_MARGIN_S
        if 0 < stall_window <= min_stall:
            raise ProgressConfigError(
                "stall_window_s",
                f"stall_window_s={stall_window:g} must exceed the longest per-call "
                f"timeout ({longest_call_s:g}s) plus the "
                f"{LLM_RETRY_BACKOFF_MAX_S:g}s maximum retry backoff and a "
                f"{STALL_WINDOW_MARGIN_S:g}s margin ({min_stall:g}s), or one slow "
                "call reads as a stall",
            )
        stall_status = config.get("stall_status") or "error"
        if stall_status not in STALL_STATUSES:
            raise ProgressConfigError(
                "stall_status",
                f"stall_status={stall_status!r} is not one of {list(STALL_STATUSES)}",
            )
        snapshot_on_stop = _flag(config.get("snapshot_on_stop"))
        grade_on_stop = _flag(config.get("grade_on_stop"))
        stop_snapshot_timeout = float(STOP_SNAPSHOT_RESERVE_S)
        if snapshot_on_stop:
            stop_snapshot_timeout = _number(
                config,
                "stop_snapshot_timeout_s",
                STOP_SNAPSHOT_RESERVE_S,
                low=STOP_SNAPSHOT_TIMEOUT_MIN_S,
            )
            if stop_snapshot_timeout > STOP_SNAPSHOT_RESERVE_S:
                logger.bind(message_type="progress").warning(
                    f"stop_snapshot_timeout_s {stop_snapshot_timeout:g}s clamped to "
                    f"the {STOP_SNAPSHOT_RESERVE_S}s stop-snapshot reserve"
                )
                stop_snapshot_timeout = float(STOP_SNAPSHOT_RESERVE_S)
        elif grade_on_stop:
            logger.bind(message_type="progress").warning(
                "grade_on_stop ignored: snapshot_on_stop is off, and a stopped run "
                "is graded only on its snapshot"
            )
            grade_on_stop = False
        return cls(
            hard_wall_clock_s=hard_wall,
            hard_max_steps=hard_steps,
            window_s=_number(config, "progress_window_s", 1800, low=1),
            min_steps=int(_number(config, "progress_min_steps", 5, low=1)),
            min_new_calls=int(_number(config, "progress_min_new_calls", 3, low=0)),
            extension_s=_number(config, "extension_s", 7200, low=1),
            extension_steps=int(_number(config, "extension_steps", 100, low=1)),
            loop_detection=_flag(config.get("loop_detection")),
            loop_repeat_k=k,
            loop_window_steps=window_steps,
            stall_window_s=stall_window,
            stall_status=stall_status,
            grace_s=grace_s,
            base_wall_clock_s=base_wall_clock_s,
            base_max_steps=base_max_steps,
            ceiling_limit=ceiling_limit,
            snapshot_on_stop=snapshot_on_stop,
            grade_on_stop=grade_on_stop,
            stop_snapshot_timeout_s=stop_snapshot_timeout,
        )


class CallKey(NamedTuple):
    """A call as the monitor sees it: name plus digests of its arguments."""

    name: str
    exact: str
    near: str


def _canonical(value: Any, *, near: bool) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _canonical(v, near=near)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
            if not (near and str(k).lower() in _NEAR_DUPLICATE_DROPPED_KEYS)
        }
    if isinstance(value, list):
        return [_canonical(v, near=near) for v in value]
    if isinstance(value, str):
        text = _WHITESPACE.sub(" ", value).strip()
        return _NUMBER.sub("0", text.lower()) if near else text
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return 0 if near else value
    return str(value)


def _parse_args(arguments: Any) -> Any:
    """JSON arguments as an object; a JSON string holding JSON is decoded twice."""
    parsed = arguments
    for _ in range(2):
        if not isinstance(parsed, str):
            break
        try:
            parsed = json.loads(parsed)
        except ValueError:
            break
    return parsed


def _digest(name: str, value: Any) -> str:
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(
        f"{name}\0{text}".encode("utf-8", "surrogatepass")
    ).hexdigest()


def call_key(name: str, arguments: Any) -> CallKey:
    """Name plus sha256 digests of the canonical and near-duplicate arguments."""
    try:
        parsed = _parse_args(arguments)
        exact, near = _canonical(parsed, near=False), _canonical(parsed, near=True)
    except RecursionError:
        # Pathologically nested arguments: compare them as raw text.
        exact = near = str(arguments)
    return CallKey(name, _digest(name, exact), _digest(name, near))


@dataclass(frozen=True)
class LoopVerdict:
    rule: str
    tool: str | None
    detail: str
    # The model turn (1-based) and monitor-clock time it fired at.
    step: int = 0
    at: float = 0.0


@dataclass(frozen=True)
class _Step:
    at: float
    calls: tuple[CallKey, ...]
    mutating: frozenset[str]
    new: int

    @property
    def signature(self) -> frozenset[str]:
        return frozenset(c.exact for c in self.calls)


class ProgressMonitor:
    """Per-run step record: progress for extensions, loop verdicts, stall timer."""

    def __init__(
        self,
        policy: ProgressPolicy,
        *,
        read_only: Collection[str],
        meta: Collection[str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy: ProgressPolicy = policy
        # Live view: the agent fills it while loading tools, after this is built.
        self._read_only: Collection[str] = read_only
        self._meta: Collection[str] = meta
        self.clock: Callable[[], float] = clock
        self._seen: set[str] = set()
        k, n = policy.loop_repeat_k, policy.loop_window_steps
        # A cycle needs two rounds and a re-read two reads, even at K=2.
        self._rounds: int = max(k - 1, 2)
        self._recent: deque[_Step] = deque(maxlen=max(n, 3 * self._rounds))
        # New-call counts of the last N steps that made a non-meta call (L4).
        self._novelty: deque[int] = deque(maxlen=n)
        self._timeline: deque[_Step] = deque()
        self.steps: int = 0
        # Steps that sent a non-meta tool call (see progressing).
        self._call_steps: int = 0
        # The first verdict (it ends the run under loop_detection) and the
        # latest, which is what gates an extension with detection off.
        self.verdict: LoopVerdict | None = None
        self.last_verdict: LoopVerdict | None = None
        self._loop_streak: bool = False
        self.extensions: dict[str, int] = {"steps": 0, "wall_clock": 0}
        # self._call_steps at each limit's last extension.
        self._steps_at_extension: dict[str, int] = {}
        self.near_duplicate_signals: int = 0
        self._near_streak: bool = False
        self.in_flight: str | None = None
        self._stall: asyncio.Timeout | None = None

    # -- recording ------------------------------------------------------

    def record_step(self, calls: Iterable[tuple[str, Any]]) -> None:
        """Record one model turn's tool calls as (name, raw JSON arguments)."""
        self.record_keys(
            call_key(name, args) for name, args in calls if name not in self._meta
        )

    def record_keys(self, keys: Iterable[CallKey]) -> None:
        calls = tuple(keys)
        new = 0
        for exact in dict.fromkeys(c.exact for c in calls):
            if exact not in self._seen:
                self._seen.add(exact)
                new += 1
        step = _Step(
            at=self.clock(),
            calls=calls,
            mutating=frozenset(c.exact for c in calls if c.name not in self._read_only),
            new=new,
        )
        self.steps += 1
        self._recent.append(step)
        if calls:
            self._novelty.append(new)
            self._call_steps += 1
        self._timeline.append(step)
        self._prune(step.at)
        self.heartbeat()
        self._near_duplicate_signal()
        verdict = self._loop_rules()
        if verdict is not None:
            verdict = dataclasses.replace(verdict, step=self.steps, at=step.at)
            self.last_verdict = verdict
            if self.verdict is None:
                self.verdict = verdict
            if not self.policy.loop_detection and not self._loop_streak:
                logger.bind(message_type="step", loop_rule=verdict.rule).warning(
                    f"Loop signal at model turn {self.steps} (detection off): "
                    f"{verdict.detail}"
                )
        self._loop_streak = verdict is not None

    def _prune(self, now: float) -> None:
        cutoff = now - self.policy.window_s
        while self._timeline and self._timeline[0].at < cutoff:
            self._timeline.popleft()

    # -- progress -------------------------------------------------------

    def progressing(self, limit: str) -> bool:
        """Whether the last window shows progress; logs the decision at ``limit``."""
        now = self.clock()
        self._prune(now)
        steps = len(self._timeline)
        new = sum(s.new for s in self._timeline)
        mutating = sum(len(s.mutating) for s in self._timeline)
        # Only a verdict inside the window blocks: with detection off an early
        # signal followed by a window of real work must not pin the run.
        recent = self.last_verdict
        loop = (
            recent
            if recent is not None and recent.at >= now - self.policy.window_s
            else None
        )
        # Tool-call steps since this limit's last extension (None: never
        # extended). A failed turn sends no call: an LLM timeout, no choices,
        # an empty, text-only or truncated reply, only rejected or meta calls,
        # or an overflow compaction. A streak of them can't keep extending on
        # the window's older work.
        extended_at = self._steps_at_extension.get(limit)
        since = None if extended_at is None else self._call_steps - extended_at
        ok = (
            steps >= self.policy.min_steps
            and (new >= self.policy.min_new_calls or mutating > 0)
            and loop is None
            and (since is None or since > 0)
        )
        logger.bind(
            message_type="step",
            progress_limit=limit,
            progress_steps=steps,
            progress_new_calls=new,
            progress_mutating_calls=mutating,
            progress_loop_rule=loop.rule if loop else None,
            progress_steps_since_extension=since,
        ).info(
            f"{'Progressing' if ok else 'No progress'} at the {limit} limit: "
            f"{steps} steps, {new} new calls, {mutating} mutating calls in the "
            f"last {self.policy.window_s:g}s"
            + (
                ""
                if since is None
                else f", {since} tool-call steps since its last extension"
            )
        )
        return ok

    def note_extension(self, limit: str, to: float) -> None:
        self.extensions[limit] += 1
        self._steps_at_extension[limit] = self._call_steps
        logger.bind(message_type="step", progress_limit=limit).info(
            f"Extended the {limit} limit to {to:g} (extension {self.extensions[limit]})"
        )

    # -- loops ----------------------------------------------------------

    def _window(self) -> list[_Step]:
        return list(self._recent)[-self.policy.loop_window_steps :]

    def _loop_rules(self) -> LoopVerdict | None:
        k, n = self.policy.loop_repeat_k, self.policy.loop_window_steps
        window = self._window()
        names = {c.exact: c.name for s in window for c in s.calls}

        repeats = Counter(e for s in window for e in s.signature)
        if repeats:
            exact, count = repeats.most_common(1)[0]
            if count >= k:
                return LoopVerdict(
                    "L1",
                    names[exact],
                    f"L1 exact repeat: {names[exact]} ({exact[:_HASH_PREFIX]}) "
                    f"in {count} of the last {n} steps",
                )

        cycles = self._rounds
        recent = list(self._recent)
        for period in (2, 3):
            length = period * cycles
            if len(recent) < length:
                continue
            sigs = [s.signature for s in recent[-length:]]
            if (
                all(sigs)
                and len(set(sigs[:period])) == period
                and all(sigs[i] == sigs[i % period] for i in range(length))
            ):
                first = recent[-length].calls[0].name
                return LoopVerdict(
                    "L2",
                    first,
                    f"L2 short cycle: period {period} repeated {cycles} times",
                )

        reads: dict[str, list[int]] = {}
        writes: list[int] = []
        for i, s in enumerate(window):
            for exact in s.signature - s.mutating:
                reads.setdefault(exact, []).append(i)
            if s.mutating:
                writes.append(i)
        for exact, seen_at in reads.items():
            # A write in any step from the first read through the last one,
            # including either end's own step, breaks the re-read.
            if len(seen_at) >= self._rounds and not any(
                seen_at[0] <= w <= seen_at[-1] for w in writes
            ):
                return LoopVerdict(
                    "L3",
                    names[exact],
                    f"L3 re-read: {names[exact]} ({exact[:_HASH_PREFIX]}) in "
                    f"{len(seen_at)} of the last {n} steps with no write between",
                )

        # Over the last N steps that made a non-meta call only: a run of
        # todo_write-only steps or text-only replies is neither novelty nor a
        # repeat, and must not end the run as an unretryable model failure.
        if len(self._novelty) >= n and not any(self._novelty):
            return LoopVerdict(
                "L4", None, f"L4 zero novelty: no new call in {n} tool-calling steps"
            )
        return None

    def _near_duplicate_signal(self) -> None:
        n = self.policy.loop_window_steps
        window = self._window()
        hit: tuple[str, int] | None = None
        if len(window) >= n:
            near = Counter(d for s in window for d in {c.near for c in s.calls})
            if near:
                digest, count = near.most_common(1)[0]
                if count >= n:
                    hit = (digest, count)
        if hit is not None and not self._near_streak:
            self.near_duplicate_signals += 1
            tool = next(c.name for s in window for c in s.calls if c.near == hit[0])
            logger.bind(message_type="step", near_duplicate_tool=tool).info(
                f"Near-duplicate signal (no stop): {tool} ({hit[0][:_HASH_PREFIX]}) "
                f"in all of the last {n} steps"
            )
        self._near_streak = hit is not None

    # -- stall ----------------------------------------------------------

    def stall_timer(self) -> asyncio.Timeout | None:
        """A fresh stall timeout to enter around the loop, or None when off."""
        if not self.policy.stall_window_s:
            return None
        self._stall = asyncio.timeout(self.policy.stall_window_s)
        return self._stall

    @property
    def stalled(self) -> bool:
        return self._stall is not None and self._stall.expired()

    def heartbeat(self) -> None:
        """Something completed or a call attempt started: push the stall
        deadline out by a full window."""
        if self._stall is None or self._stall.expired():
            return
        try:
            self._stall.reschedule(
                asyncio.get_running_loop().time() + self.policy.stall_window_s
            )
        except RuntimeError:
            # Not entered yet, or already left: nothing to push.
            pass

    def begin(self, activity: str) -> None:
        """Mark what is in flight: "llm", "compaction" or "tool:<name>"."""
        self.in_flight = activity

    def end(self) -> None:
        self.in_flight = None
        self.heartbeat()

    def stall_fault(self) -> tuple[StallStatus, dict[str, Any]]:
        """Status and fault for a stall stop.

        `error` names what hung, so Studio classes it as infra and retries it
        once. `failed` holds the model to the stall window as a declared model
        budget (`model_timeout`, a model failure that is never retried).
        """
        window = self.policy.stall_window_s
        if self.policy.stall_status == "failed":
            return "failed", model_timeout(window)
        activity = self.in_flight or ""
        if activity in ("llm", "compaction"):
            return "error", llm_connection()
        if activity.startswith("tool:"):
            return "error", tool_timeout(activity.removeprefix("tool:"), window)
        return "error", harness("react_toolbelt_agent", "running")

    # -- output ---------------------------------------------------------

    def summary(
        self,
        *,
        steps: int,
        step_budget: int,
        wall_clock_s: float,
        ceiling_hit: bool = False,
        step_ceiling_hit: bool = False,
        progressing_at_stop: bool | None = None,
    ) -> dict[str, Any]:
        """The `progress` output block: effective limits and what happened.

        `ceiling_hit` is a wall-clock stop at the hard wall ceiling;
        `ceiling_limit` then names the limit that set it. `step_ceiling_hit`
        is a step stop at hard_max_steps. `progressing_at_stop` is the progress
        verdict at a step or wall-clock stop (True: a ceiling cut a
        still-progressing run), None for any other end.
        """
        return {
            "steps": steps,
            "step_budget": step_budget,
            "wall_clock_s": round(wall_clock_s),
            "hard_max_steps": self.policy.hard_max_steps,
            "hard_wall_clock_s": round(self.policy.hard_wall_clock_s),
            "extensions": dict(self.extensions),
            "near_duplicate_signals": self.near_duplicate_signals,
            "loop_rule": self.verdict.rule if self.verdict else None,
            "ceiling_hit": ceiling_hit,
            "ceiling_limit": self.policy.ceiling_limit if ceiling_hit else None,
            "step_ceiling_hit": step_ceiling_hit,
            "progressing_at_stop": progressing_at_stop,
        }
