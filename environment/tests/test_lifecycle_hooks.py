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
