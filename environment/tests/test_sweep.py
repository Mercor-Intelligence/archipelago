"""The sweep that empties the sandbox before the grading image is mounted.

Both live Corridor findings on the inline path need a process the agent left
running: one reads the grade subprocess's environment through `/proc`, the other
rewrites the writable grading tree. These cover the filter, the door, and the
ways the sweep must fail closed instead of vouching for a box it cannot read.

The reader is injected, so these run anywhere. `/proc` exists only on Linux and
a test that runs only in CI is a test nobody has watched fail.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from runner.sweep import Proc, SweepResult, parse_stat, sweep_agent_processes

#: A real line, with the comm and its parentheses in field 2. Field 22, the
#: start time, is 4242.
_STAT = "7 (python3) S 1 7 7 0 -1 4194304 900 0 0 0 3 1 0 0 20 0 1 0 4242 1 2 3"

#: A process may name itself anything, parentheses and spaces included.
_HOSTILE_STAT = "9 (evil) 1 2 3 (x) S 1 9 9 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 99 1 2"

#: Every test measures against this, so a process at 500 is after it and one at
#: 50 is before it.
_REFERENCE = 100.0


def _proc(
    pid: int,
    *,
    ticks: float | None = 500.0,
    state: str = "S",
    comm: str = "",
) -> Proc:
    """A running process started after the reference, unless a test says else."""
    return Proc(pid, ticks, state, comm)


def _sweep(
    pids: Callable[[], list[int]] | None = None,
    read: Callable[[int], Proc | None] | None = None,
    kill: Callable[[int, int], None] | None = None,
) -> SweepResult:
    """Run the sweep against a fabricated process table."""
    return sweep_agent_processes(
        _REFERENCE,
        pids=pids or (lambda: [os.getpid(), os.getppid()]),
        read=read or (lambda pid: _proc(pid)),
        kill=kill or (lambda _pid, _sig: None),
    )


def test_one_stat_line_carries_the_name_the_state_and_the_start_time() -> None:
    """Three reads of the same file is what this replaced, so a parse that drops
    any of the three sends the sweep back to opening /proc per question."""
    got = parse_stat(7, _STAT)

    assert got.start_ticks == 4242.0
    assert got.state == "S"
    assert got.comm == "python3"
    assert got.alive


def test_the_fields_are_read_after_the_last_paren() -> None:
    """Counting from the left breaks on a process that puts spaces and
    parentheses in its own name, which is a thing a hostile process would do."""
    got = parse_stat(9, _HOSTILE_STAT)

    assert got.start_ticks == 99.0
    assert got.state == "S"
    assert got.comm == "evil) 1 2 3 (x"


def test_an_unparseable_stat_has_no_start_time() -> None:
    """The caller keys the fail-closed branch off this, so a malformed line must
    not read as a process that started long ago."""
    assert parse_stat(7, "").start_ticks is None
    assert parse_stat(7, "7 (python3) S").start_ticks is None


def test_a_process_started_after_the_reference_is_killed() -> None:
    """The reference is read before the agent loop, so anything newer is the
    agent's and is what both findings need."""
    gone: set[int] = set()
    killed: list[int] = []

    def _kill(pid: int, _sig: int) -> None:
        killed.append(pid)
        gone.add(pid)

    result = _sweep(
        pids=lambda: [p for p in [os.getpid(), os.getppid(), 77] if p not in gone],
        kill=_kill,
    )

    assert killed == [77, 77]  # SIGTERM then SIGKILL
    assert result.killed == [77]
    assert result.clean


def test_a_process_older_than_the_reference_is_left_alone() -> None:
    """The world's own services predate the agent loop. Killing them breaks the
    sandbox the grade is about to run in."""
    killed: list[int] = []

    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), 77],
        read=lambda pid: _proc(pid, ticks=50.0),
        kill=lambda pid, _sig: killed.append(pid),
    )

    assert killed == []
    assert result.clean


def test_the_code_exec_door_is_killed_however_old_it_is() -> None:
    """It is the model's only route to running new commands. Leaving it up lets
    a new watcher start after a pass that found nothing."""
    gone: set[int] = set()

    def _read(pid: int) -> Proc | None:
        return _proc(pid, ticks=1.0, comm="code-exec" if pid == 77 else "")

    result = _sweep(
        pids=lambda: [p for p in [os.getpid(), os.getppid(), 77] if p not in gone],
        read=_read,
        kill=lambda pid, _sig: gone.add(pid),
    )

    assert result.killed == [77]
    assert result.clean


def test_a_zombie_is_neither_a_target_nor_a_survivor() -> None:
    """A zombie is dead and awaiting reaping, so it runs no code and holds no
    memory. Neither finding reaches it, and signalling it again is what stops
    the loop converging."""
    signalled: list[int] = []

    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), 4242],
        read=lambda pid: _proc(pid, state="Z"),
        kill=lambda pid, _sig: signalled.append(pid),
    )

    assert signalled == []
    assert result.survivors == []
    assert result.clean


def test_a_process_killed_but_not_yet_reaped_still_reports_clean() -> None:
    """A killed child stays in /proc until its parent reaps it, and its stat
    keeps a start time after the reference. Re-targeting it every round spends
    the round limit and calls a cleared sandbox dirty, which refuses the mount
    and sends every trajectory to the lane."""
    reaped_by_no_one: set[int] = set()

    def _read(pid: int) -> Proc | None:
        return _proc(pid, state="Z" if pid in reaped_by_no_one else "S")

    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), 4242],
        read=_read,
        kill=lambda pid, _sig: reaped_by_no_one.add(pid),
    )

    assert result.killed == [4242]
    assert result.survivors == []
    assert result.clean


def test_a_process_that_forks_during_the_sweep_is_caught() -> None:
    """One pass cannot be enough. A process that forks while the pass runs
    leaves a child the pass never listed, and rechecking only the pids it
    already chose would call that clean."""
    gone: set[int] = set()
    spawned = {"done": False}

    def _pids() -> list[int]:
        table = [os.getpid(), os.getppid(), 100]
        if spawned["done"]:
            table.append(200)
        return [p for p in table if p not in gone]

    def _kill(pid: int, _sig: int) -> None:
        if pid == 100 and not spawned["done"]:
            spawned["done"] = True  # it forks a child as it dies
        gone.add(pid)

    result = _sweep(pids=_pids, kill=_kill)

    assert result.killed == [100, 200]
    assert result.clean


def test_a_process_with_an_unreadable_stat_is_a_target_not_a_skip() -> None:
    """An unreadable or malformed stat is what something hiding would present.
    Skipping it lets exactly the process this exists to catch survive."""
    gone: set[int] = set()

    def _read(pid: int) -> Proc | None:
        if pid in gone:
            return None
        return _proc(pid, ticks=None if pid == 66 else 1.0, state="")

    result = _sweep(
        pids=lambda: [p for p in [os.getpid(), os.getppid(), 66] if p not in gone],
        read=_read,
        kill=lambda pid, _sig: gone.add(pid),
    )

    assert result.killed == [66]
    assert result.clean


def test_a_process_that_vanished_between_the_listing_and_the_read_is_skipped() -> None:
    """A pid that goes away mid-pass is an ordinary race and not a hiding
    process. Treating it as a target would signal a pid something else may have
    been given by then."""
    signalled: list[int] = []

    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), 66],
        read=lambda pid: None if pid == 66 else _proc(pid, ticks=1.0),
        kill=lambda pid, _sig: signalled.append(pid),
    )

    assert signalled == []
    assert result.clean


def test_a_proc_that_does_not_list_this_process_is_refused() -> None:
    """Root can mount a tmpfs over /proc. An empty table would otherwise read as
    an empty sandbox, which is the one wrong answer: grading beside a watcher
    while reporting none exists."""
    result = _sweep(pids=lambda: [12345])

    assert not result.proc_trustworthy
    assert not result.clean


def test_anything_still_alive_after_the_rounds_refuses_the_mount() -> None:
    """The mount is writable and has no read-only flag, so a survivor can
    rewrite the grader before it runs. The only safe answer is the lane."""
    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), 4242],
        kill=lambda _pid, _sig: None,
    )

    assert result.survivors == [4242]
    assert not result.clean
