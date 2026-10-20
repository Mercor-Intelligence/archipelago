"""Orchestrator-supplied capture of the environment between agent turns."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Returns the snapshot id. Set by the orchestrator, which holds the S3 minting
# identity; unset wherever no orchestrator does (local runs, agent_sandbox).
TurnSnapshotFn = Callable[[], Awaitable[str]]

turn_snapshot_ctx: ContextVar[TurnSnapshotFn | None] = ContextVar(
    "turn_snapshot", default=None
)


@contextmanager
def turn_snapshot_scope(capture: TurnSnapshotFn) -> Iterator[None]:
    """Expose `capture` to agents for one run, cleared even if the run raises."""
    token = turn_snapshot_ctx.set(capture)
    try:
        yield
    finally:
        turn_snapshot_ctx.reset(token)
