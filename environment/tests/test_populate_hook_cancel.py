"""A populate that is cancelled or times out must not leave its hook's processes running."""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
from typing import Any

import pytest

from runner.data.populate import main as populate_main
from runner.data.populate.models import LifecycleHook


class _Spawned:
    """Wraps asyncio.create_subprocess_shell and keeps the process it spawned."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self._real = asyncio.create_subprocess_shell

    async def __call__(self, *args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        self.proc = await self._real(*args, **kwargs)
        return self.proc


def _gone(pid: int) -> bool:
    """Dead or a zombie: a killed child of an exited shell may wait on init to reap it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    if probe.returncode != 0:
        # init may have reaped the zombie between the two probes.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        raise AssertionError(probe.stderr)
    return probe.stdout.strip().startswith("Z")


def _hook_with_child(pid_file: pathlib.Path, **kwargs: Any) -> LifecycleHook:
    """A compound hook (`cd … && python seed.py`) runs its work in a child of the shell."""
    return LifecycleHook(
        name="slow", command=f"sleep 60 & echo $! > '{pid_file}'; wait", **kwargs
    )


async def _child_pid(pid_file: pathlib.Path) -> int:
    async with asyncio.timeout(10):
        while not pid_file.exists() or not pid_file.read_text().strip():
            await asyncio.sleep(0.01)
    return int(pid_file.read_text())


async def test_cancelling_a_hook_kills_its_process_and_its_children(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    spawned = _Spawned()
    monkeypatch.setattr(populate_main.asyncio, "create_subprocess_shell", spawned)
    pid_file = tmp_path / "child.pid"

    task = asyncio.create_task(
        populate_main.run_lifecycle_hook(_hook_with_child(pid_file))
    )
    child_pid = await _child_pid(pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawned.proc is not None and spawned.proc.returncode is not None
    assert _gone(child_pid)


async def test_a_timed_out_hook_takes_its_children_with_it(
    tmp_path: pathlib.Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    hook = _hook_with_child(pid_file, timeout_seconds=0.5)

    with pytest.raises(RuntimeError, match="timed out"):
        await populate_main.run_lifecycle_hook(hook)

    assert _gone(await _child_pid(pid_file))
