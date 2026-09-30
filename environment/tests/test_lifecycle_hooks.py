"""Tests for run_lifecycle_hook.

Key regression: a hook that finishes while a backgrounded daemon keeps the
hook's stdout/stderr pipe open must NOT block the runner until pipe EOF. This
is the Retail Banking GTG continuation failure — Fineract's populate.sh printed
"=== Populate Complete ===" and exited 0, but its backgrounded `nohup java …`
server held the pipe open, so the hook never returned and the whole populate
phase hung until the agent's ~30-min read-timeout. Completion must be gated on
the process exiting, not pipe EOF.
"""

from __future__ import annotations

import asyncio

import pytest

from runner.data.populate import main as populate_main
from runner.data.populate.main import run_lifecycle_hook
from runner.data.populate.models import LifecycleHook


async def test_hook_returns_when_process_exits_despite_pipe_holder() -> None:
    """Regression for the populate hang.

    The hook exits 0 immediately, but a backgrounded child (standing in for a
    daemon like Fineract's server) inherits stdout and keeps it open for 60s.
    Before the fix the runner awaited pipe EOF and blocked the full 60s; now it
    returns once the process exits (plus the short drain grace). Wrapping in
    ``wait_for`` makes the "would hang" failure explicit — the old code raises
    TimeoutError here.
    """
    hook = LifecycleHook(
        name="fineract_like",
        command="echo '=== Populate Complete ==='; sleep 60 &",
    )
    # Generous vs the post-exit drain grace, far below the 60s the daemon holds
    # the pipe — so it passes only if completion is gated on process exit.
    await asyncio.wait_for(
        run_lifecycle_hook(hook),
        timeout=populate_main._STREAM_DRAIN_GRACE_SECONDS + 5,
    )


async def test_successful_hook() -> None:
    """A plain successful hook completes without raising."""
    await run_lifecycle_hook(LifecycleHook(name="ok", command="echo hello"))


async def test_failing_hook_raises_with_stderr() -> None:
    """A non-zero exit raises RuntimeError carrying the collected stderr."""
    hook = LifecycleHook(name="boom", command="echo 'kaboom' >&2; exit 1")
    with pytest.raises(RuntimeError) as exc_info:
        await run_lifecycle_hook(hook)
    msg = str(exc_info.value)
    assert "boom" in msg
    assert "exit code 1" in msg
    assert "kaboom" in msg


async def test_hook_timeout_kills_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook whose process never exits is killed and raises after the timeout."""
    monkeypatch.setattr(populate_main, "_HOOK_TIMEOUT_SECONDS", 1.0)
    hook = LifecycleHook(name="hang", command="sleep 60")
    with pytest.raises(RuntimeError, match="timed out"):
        await asyncio.wait_for(run_lifecycle_hook(hook), timeout=15)


async def test_per_hook_timeout_overrides_env_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hook.timeout_seconds wins over the runner's env-var default."""
    monkeypatch.setattr(populate_main, "_HOOK_TIMEOUT_SECONDS", 3000.0)
    hook = LifecycleHook(name="hang", command="sleep 60", timeout_seconds=1.0)
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        await asyncio.wait_for(run_lifecycle_hook(hook), timeout=15)


def test_hook_model_ignores_unknown_fields() -> None:
    """Payloads from newer agents must not break older-model parsing paths."""
    hook = LifecycleHook.model_validate(
        {"name": "n", "command": "true", "some_future_field": 1}
    )
    assert hook.timeout_seconds is None


async def test_hook_duration_is_emitted_not_just_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duration was already computed and then dropped on the populate path.

    `run_lifecycle_hook` has always returned it, and `PopulateResult` has always
    carried it, but nothing on the populate path consumed either — so ~405s of a
    ~492s populate had no attribution and an image-mount A/B came back "slower,
    cause unknown". Emitted from the hook itself, not the callers, so post-populate
    and pre-snapshot cannot diverge.
    """
    emitted: list[tuple[str, float, list[str]]] = []
    monkeypatch.setattr(
        populate_main,
        "distribution",
        lambda metric, value, tags=None: emitted.append((metric, value, tags or [])),
    )

    await run_lifecycle_hook(
        LifecycleHook(name="Campaign Database", command="echo hi"),
        phase="post_populate",
    )

    assert len(emitted) == 1, emitted
    metric, value, tags = emitted[0]
    assert metric == "studio.trajectory.lifecycle_hook_seconds"
    assert value >= 0.0
    # Tagged by BOTH, or the metric cannot answer "which phase is the 405s in".
    assert "hook:campaign_database" in tags
    assert "phase:post_populate" in tags
    assert "status:ok" in tags


def test_tag_values_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook names come from world config, so they are arbitrary operator text.

    Unbounded they would be a Datadog cardinality and content problem; this is the
    same reason `snapshot_size_bucket` buckets rather than tagging a raw count.
    """
    from runner.utils.metrics import safe_tag_value

    assert safe_tag_value("Campaign Database") == "campaign_database"
    assert safe_tag_value("weird/name:with spaces") == "weird_name_with_spaces"
    assert safe_tag_value("café-数据").isascii(), "a tag must never carry unicode"
    assert len(safe_tag_value("x" * 500)) <= 40
    assert safe_tag_value("") == "unknown"
    assert safe_tag_value("___") == "unknown"


async def test_concurrent_hooks_cost_the_phase_less_than_their_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason a phase timer exists at all.

    Hooks run under `asyncio.gather`, so two 0.3s hooks cost the populate ~0.3s,
    not 0.6s. Subtracting SUMMED hook durations from the populate total
    over-attributes and can drive the "extraction" residual negative — the exact
    arithmetic this PR is meant to make trustworthy.
    """
    emitted: list[tuple[str, float, list[str]]] = []
    monkeypatch.setattr(
        populate_main,
        "distribution",
        lambda metric, value, tags=None: emitted.append((metric, value, tags or [])),
    )

    await populate_main.run_lifecycle_hooks(
        [
            LifecycleHook(name="a", command="sleep 0.3"),
            LifecycleHook(name="b", command="sleep 0.3"),
        ],
        phase="post_populate",
    )

    per_hook = [v for m, v, _ in emitted if m.endswith("lifecycle_hook_seconds")]
    phase = [v for m, v, _ in emitted if m.endswith("lifecycle_hooks_phase_seconds")]

    assert len(per_hook) == 2, emitted
    assert len(phase) == 1, "the phase must be timed exactly once"
    assert phase[0] < sum(per_hook), (
        "phase wall clock must be less than the sum of concurrent hooks — "
        f"phase={phase[0]:.3f} sum={sum(per_hook):.3f}"
    )
    assert phase[0] >= max(per_hook), (
        "the phase cannot be shorter than its slowest hook"
    )


async def test_the_phase_is_timed_even_when_a_hook_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed hook still consumed populate wall clock; losing that would make
    the residual on every failing populate silently wrong."""
    emitted: list[str] = []
    monkeypatch.setattr(
        populate_main,
        "distribution",
        lambda metric, value, tags=None: emitted.append(metric),
    )

    with pytest.raises(RuntimeError):
        await populate_main.run_lifecycle_hooks(
            [LifecycleHook(name="boom", command="exit 1")], phase="post_populate"
        )

    assert "studio.trajectory.lifecycle_hooks_phase_seconds" in emitted


async def test_a_failing_hook_still_reports_the_clock_it_burned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook that fails is often the one that cost the most.

    `_HOOK_TIMEOUT_SECONDS` defaults to 3000s, so a hook that times out is BY
    DEFINITION the biggest number in the phase — and emitting only on the success
    path meant that one never appeared. Tagged by status so a failed run's clock
    is separable rather than silently mixed into the healthy distribution.
    """
    emitted: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        populate_main,
        "distribution",
        lambda metric, value, tags=None: emitted.append((metric, tags or [])),
    )

    with pytest.raises(RuntimeError):
        await run_lifecycle_hook(
            LifecycleHook(name="boom", command="exit 3"), phase="post_populate"
        )

    per_hook = [tags for m, tags in emitted if m.endswith("lifecycle_hook_seconds")]
    assert len(per_hook) == 1, emitted
    assert "status:failed" in per_hook[0]
    assert "hook:boom" in per_hook[0]
