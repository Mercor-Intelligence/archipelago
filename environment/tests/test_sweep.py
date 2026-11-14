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
import signal
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


class _Clock:
    """Time that moves only when the sweep sleeps, so a settle wait costs nothing."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _sweep(
    pids: Callable[[], list[int]] | None = None,
    read: Callable[[int], Proc | None] | None = None,
    kill: Callable[[int, int], None] | None = None,
    clock: _Clock | None = None,
) -> SweepResult:
    """Run the sweep against a fabricated process table."""
    clock = clock or _Clock()
    return sweep_agent_processes(
        _REFERENCE,
        pids=pids or (lambda: [os.getpid(), os.getppid()]),
        read=read or (lambda pid: _proc(pid)),
        kill=kill or (lambda _pid, _sig: None),
        sleep=clock.sleep,
        clock=clock,
    )


def test_one_stat_line_carries_the_name_the_state_and_the_start_time() -> None:
    """Three reads of the same file is what this replaced, so a parse that drops
    any of the three sends the sweep back to opening /proc per question."""
    got = parse_stat(7, _STAT)

    assert got.start_ticks == 4242.0
    assert got.state == "S"
    assert got.comm == "python3"
    assert got.ppid == 1
    assert got.alive


def test_the_fields_are_read_after_the_last_paren() -> None:
    """Counting from the left breaks on a process that puts spaces and
    parentheses in its own name, which is a thing a hostile process would do."""
    got = parse_stat(9, _HOSTILE_STAT)

    assert got.start_ticks == 99.0
    assert got.state == "S"
    assert got.comm == "evil) 1 2 3 (x"
    assert got.ppid == 1


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


def test_a_process_that_takes_a_moment_to_die_reports_clean() -> None:
    """SIGKILL does not take a process out of the table at once. Listing again
    straight away targets it again, and every round spent that way called a
    cleared sandbox dirty and sent the trajectory to the lane."""
    clock = _Clock()
    dies_at: dict[int, float] = {}

    def _alive(pid: int) -> bool:
        return pid not in dies_at or clock.now < dies_at[pid]

    def _kill(pid: int, _sig: int) -> None:
        dies_at.setdefault(pid, clock.now + 0.3)

    result = _sweep(
        pids=lambda: [p for p in [os.getpid(), os.getppid(), 77] if _alive(p)],
        read=lambda pid: _proc(pid) if _alive(pid) else None,
        kill=_kill,
        clock=clock,
    )

    assert result.killed == [77]
    assert result.survivors == []
    assert result.clean


def test_a_child_forked_after_the_last_round_is_a_survivor() -> None:
    """The final listing decides. Checking only the pids already signalled would
    call the sandbox clean while the newest child runs beside the grader."""
    table = {os.getpid(), os.getppid(), 100}
    next_pid = [101]

    def _kill(pid: int, sig: int) -> None:
        if sig == signal.SIGKILL and pid in table:
            table.discard(pid)
            table.add(next_pid[0])  # it forks a replacement as it dies
            next_pid[0] += 1

    result = _sweep(
        pids=lambda: sorted(table),
        read=lambda pid: _proc(pid, comm="forker") if pid in table else None,
        kill=_kill,
    )

    assert not result.clean
    assert result.survivors == [105]
    assert result.killed == [100, 101, 102, 103, 104]
    assert "'forker'" in result.detail


class _Pool:
    """A process table where some parents refill every child the sweep kills.

    `refills` maps a parent pid to the name its replacement children take. A
    replacement starts after the reference, so it is a target like any worker.
    """

    def __init__(self, procs: dict[int, Proc], refills: dict[int, str]) -> None:
        self.procs = procs
        self.refills = refills
        self.signalled: list[int] = []
        self._next = 1000

    def pids(self) -> list[int]:
        return [os.getpid(), os.getppid(), *self.procs]

    def read(self, pid: int) -> Proc | None:
        return self.procs.get(pid)

    def kill(self, pid: int, sig: int) -> None:
        if sig != signal.SIGTERM:
            return
        self.signalled.append(pid)
        dead = self.procs.pop(pid, None)
        parent = dead.ppid if dead else None
        if (
            parent is not None
            and parent in self.refills
            and (parent in self.procs or parent in (os.getpid(), os.getppid()))
        ):
            self.procs[self._next] = Proc(
                self._next, 500.0, "S", self.refills[parent], parent
            )
            self._next += 1


def _elder(pid: int, comm: str, ppid: int) -> Proc:
    return Proc(pid, 50.0, "S", comm, ppid)


def _worker(pid: int, comm: str, ppid: int) -> Proc:
    return Proc(pid, 500.0, "S", comm, ppid)


def test_a_manager_that_refills_its_workers_is_killed_too() -> None:
    """PHP-FPM keeps spare workers. Killing them alone never empties the table,
    because the master replaces each one, and the sandbox was called dirty."""
    pool = _Pool(
        {
            40: _elder(40, "bash", 1),
            50: _elder(50, "php-fpm8.4", 40),
            60: _worker(60, "php-fpm8.4", 50),
            61: _worker(61, "php-fpm8.4", 50),
        },
        refills={50: "php-fpm8.4"},
    )

    result = _sweep(pids=pool.pids, read=pool.read, kill=pool.kill)

    assert result.clean
    assert 50 in result.killed
    assert 40 not in pool.signalled


def test_a_parent_that_does_not_replace_its_child_is_left_alone() -> None:
    """Age alone protects a process. Losing a child is not a reason to kill it,
    only replacing one is."""
    pool = _Pool(
        {40: _elder(40, "bash", 1), 60: _worker(60, "python3", 40)},
        refills={},
    )

    result = _sweep(pids=pool.pids, read=pool.read, kill=pool.kill)

    assert result.clean
    assert pool.signalled == [60]


def test_a_chain_of_managers_is_climbed_until_nothing_refills() -> None:
    """Collabora's coolwsd restarts its forkit and the forkit restarts its spare
    kit, so each level is killed once the level below it refills."""
    pool = _Pool(
        {
            20: _elder(20, "bash", 1),
            30: _elder(30, "coolwsd", 20),
            31: _elder(31, "coolforkit", 30),
            32: _worker(32, "kit_spare_00d", 31),
        },
        refills={30: "coolforkit", 31: "kit_spare_00e"},
    )

    result = _sweep(pids=pool.pids, read=pool.read, kill=pool.kill)

    assert result.clean
    assert {30, 31, 32} <= set(result.killed)
    assert 20 not in pool.signalled


def test_the_runner_and_init_are_never_killed_for_refilling() -> None:
    """The sweep runs inside the env runner, which serves the grade that
    follows. A child it or its launcher keeps replacing is a survivor, never a
    reason to kill either of them."""
    runner, launcher = os.getpid(), os.getppid()
    pool = _Pool(
        {
            1: _elder(1, "init", 0),
            launcher: _elder(launcher, "uv", 1),
            runner: _elder(runner, "python3", launcher),
            60: _worker(60, "grade", runner),
            61: _worker(61, "uvicorn", launcher),
            70: _worker(70, "orphan", 1),
        },
        refills={1: "orphan", runner: "grade", launcher: "uvicorn"},
    )

    result = _sweep(pids=pool.pids, read=pool.read, kill=pool.kill)

    assert not result.clean
    assert not {runner, launcher, 1} & set(pool.signalled)


def test_a_child_slow_to_die_does_not_condemn_its_parent() -> None:
    """Only a replacement counts as refilling. A child still dying after the
    settle wait is the same process, and its parent has replaced nothing."""
    clock = _Clock()
    dies_at: dict[int, float] = {}

    def _alive(pid: int) -> bool:
        return pid not in dies_at or clock.now < dies_at[pid]

    procs = {40: _elder(40, "bash", 1), 60: _worker(60, "python3", 40)}
    signalled: list[int] = []

    def _kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            signalled.append(pid)
        dies_at.setdefault(pid, clock.now + 3.0)

    result = _sweep(
        pids=lambda: [os.getpid(), os.getppid(), *(p for p in procs if _alive(p))],
        read=lambda pid: procs[pid] if pid in procs and _alive(pid) else None,
        kill=_kill,
        clock=clock,
    )

    assert result.clean
    assert 40 not in signalled
