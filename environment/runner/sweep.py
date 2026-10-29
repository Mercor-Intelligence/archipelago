"""Clear the sandbox of the agent's processes before the grading image mounts.

Both risks the mount creates need a live process to exploit: reading the grade
subprocess's environment through ``/proc``, and rewriting the writable grading
tree before it runs. Killing the agent's processes removes the actor from both,
and the caller mounts only after this reports clean.

What this does NOT close is in ``SECURITY-inline-grading.md`` beside this file.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

#: The model's only route to running new commands. Killed by name because it is
#: the door: leaving it alive lets a new watcher start after a clean pass.
CODE_EXEC_PROCESS_NAMES = ("code-exec", "code_exec")

#: How long a round waits for the processes it signalled to leave the table.
SETTLE_SECONDS = 2.0
_SETTLE_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Proc:
    """One process, from a single read of ``/proc/<pid>/stat``.

    That one line carries every fact the sweep needs: the name in field 2, the
    state in field 3, the parent in field 4, and the start time in field 22.

    ``start_ticks`` is None when the file was present and could not be parsed,
    which is what something hiding would present.
    """

    pid: int
    start_ticks: float | None
    state: str
    comm: str
    ppid: int | None = None

    @property
    def alive(self) -> bool:
        """False only for a zombie, which is dead and awaiting reaping, so it
        holds no memory and runs no code. An unreadable state is not a claim
        that the process is dead."""
        return self.state != "Z"


@dataclass
class SweepResult:
    """What the sweep did, and whether the caller may mount."""

    killed: list[int] = field(default_factory=list)
    survivors: list[int] = field(default_factory=list)
    proc_trustworthy: bool = True
    detail: str = ""

    @property
    def clean(self) -> bool:
        """True only when /proc could be trusted and nothing survived."""
        return self.proc_trustworthy and not self.survivors


def read_uptime_ticks() -> float:
    """Ticks since boot, the clock ``/proc/<pid>/stat`` records against.

    Kernel-derived and monotonic, so a root process calling ``settimeofday``
    cannot move it, which a wall clock reference would allow.
    """
    with open("/proc/uptime", encoding="utf-8") as fh:
        seconds = float(fh.read().split()[0])
    return seconds * os.sysconf("SC_CLK_TCK")


def parse_stat(pid: int, stat: str) -> Proc:
    """One ``/proc/<pid>/stat`` line into a `Proc`.

    Fields are counted from the LAST ')'. Field 2 is the executable name in
    parentheses and a process may call itself ``foo) 1 2 3 (bar``, so counting
    from the left is how a hostile process hides.
    """
    try:
        close = stat.rindex(")")
        comm = stat[stat.index("(") + 1 : close]
        after = stat[close + 1 :].split()
        # Field 22 of the line, and the split above already dropped fields 1-2.
        return Proc(pid, float(after[19]), after[0], comm, int(after[1]))
    except (ValueError, IndexError):
        return Proc(pid, None, "", "")


def _read(pid: int) -> Proc | None:
    """The process, or None when it is gone.

    Gone between the listing and the read is an ordinary race. Present with an
    unreadable stat is not, so that comes back as a `Proc` with no start time.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return parse_stat(pid, fh.read())
    except OSError:
        return Proc(pid, None, "", "") if os.path.isdir(f"/proc/{pid}") else None


def _pids() -> list[int]:
    """Numeric entries only, so nothing but a pid is ever built into a path."""
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def _is_target(proc: Proc, reference_ticks: float) -> bool:
    """Whether the sweep must kill this process.

    A process it cannot classify is a target and never a skip. An unreadable or
    malformed stat is exactly what something hiding would present.
    """
    if proc.start_ticks is None:
        return True
    if not proc.alive:
        # Killed and not yet reaped. Targeting it again every round is what
        # stops the loop converging, since nothing here reaps another
        # process's child.
        return False
    return proc.start_ticks > reference_ticks or proc.comm in CODE_EXEC_PROCESS_NAMES


def _signal(pid: int, kill: Callable[[int, int], None]) -> None:
    """SIGTERM, then SIGKILL if it is still there."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            logger.warning(f"sweep: not permitted to signal pid {pid}")
            return


def _wait_for_exit(
    pids: list[int],
    *,
    read: Callable[[int], Proc | None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float,
) -> None:
    """Return once every pid is gone or a zombie, or `timeout` has passed.

    A process is still in the table for a while after SIGKILL, and the next
    listing would target it again.
    """
    deadline = clock() + timeout
    pending = set(pids)
    while True:
        pending = {
            pid for pid in pending if (proc := read(pid)) is not None and proc.alive
        }
        if not pending or clock() >= deadline:
            return
        sleep(_SETTLE_POLL_SECONDS)


def sweep_agent_processes(
    reference_ticks: float,
    *,
    pids: Callable[[], list[int]] = _pids,
    read: Callable[[int], Proc | None] = _read,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_rounds: int = 5,
    settle_seconds: float = SETTLE_SECONDS,
) -> SweepResult:
    """Kill everything started after `reference_ticks`, plus the code-exec door.

    Enumerates and signals from Python instead of shelling out to `ps` or
    `kill`: the agent had root and could have replaced either binary, and a
    sweep that asks a tampered tool what is running has answered nothing.

    Repeats until a whole pass finds nothing left to kill. One pass cannot be
    enough, because a process that forks while the pass runs leaves a child the
    pass never listed. A final listing after the last round decides: every
    target still in it is a survivor, whether it was signalled or not.

    A parent that replaces a child an earlier round killed is killed too, however
    old it is. A worker pool's manager (PHP-FPM, Collabora's forkit) refills
    every worker the sweep kills, so the listing would never come back empty.
    """
    mine = {os.getpid(), os.getppid()}
    killed: set[int] = set()
    bereaved: set[int] = set()
    targets: list[Proc] = []

    for round_ in range(max_rounds + 1):
        live = pids()
        # Root can mount a tmpfs over /proc, and an empty table would read as an
        # empty sandbox. That is the one wrong answer, because it grades beside
        # a watcher while reporting that none exists.
        if not mine <= set(live):
            return SweepResult(
                proc_trustworthy=False,
                detail="/proc does not list this process; the view is not the kernel's",
            )

        table = {
            pid: proc
            for pid in live
            if pid not in mine and pid != 1 and (proc := read(pid)) is not None
        }
        targets = [proc for proc in table.values() if _is_target(proc, reference_ticks)]
        if not targets:
            logger.info(f"sweep: clear after killing {len(killed)} process(es)")
            return SweepResult(killed=sorted(killed))
        if round_ == max_rounds:
            break

        doomed = {proc.pid: proc for proc in targets}
        for proc in targets:
            if proc.ppid is None or proc.pid in killed or proc.ppid not in bereaved:
                continue
            parent = table.get(proc.ppid)
            if parent is not None and parent.alive:
                doomed.setdefault(parent.pid, parent)
        for proc in doomed.values():
            killed.add(proc.pid)
            _signal(proc.pid, kill)
            if proc.ppid is not None:
                bereaved.add(proc.ppid)
        _wait_for_exit(
            list(doomed),
            read=read,
            sleep=sleep,
            clock=clock,
            timeout=settle_seconds,
        )

    survivor_pids = sorted(proc.pid for proc in targets)
    # repr: the name is the process's own choice and may hold a newline.
    names = ", ".join(sorted({repr(proc.comm) for proc in targets}))
    logger.warning(
        f"sweep: {len(survivor_pids)} process(es) still running after "
        f"{max_rounds} rounds: {names}"
    )
    return SweepResult(
        killed=sorted(killed - set(survivor_pids)),
        survivors=survivor_pids,
        detail=f"still running after {max_rounds} rounds: {names}",
    )
