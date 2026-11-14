"""A budget denial, recorded where a task boundary cannot hide it.

`budget_stop_ctx` is the lane's channel and stays so. It cannot serve the CLI:
every verifier runs in its own task and `asyncio.gather` copies the context into
each one, and `asyncio.run` copies it again, so a denial recorded inside the
grade is invisible to a read outside it.

Process-scoped, which is sound here because the grading CLI runs one grade per
process. `reset_budget_stop` is called at the start of that grade so a value can
never outlive the run that produced it.
"""

from __future__ import annotations

_stop: dict[str, object] | None = None


def record_budget_stop(stop: dict[str, object]) -> None:
    """Keep the FIRST denial. Later calls are consequences of the same halt, and
    the earliest one names the unit that ran out."""
    global _stop  # noqa: PLW0603 - one process, one grade
    if _stop is None:
        _stop = dict(stop)


def budget_stop() -> dict[str, object] | None:
    """The denial this grade recorded, or None."""
    return _stop


def reset_budget_stop() -> None:
    """Clear it before a grade, so nothing carries over."""
    global _stop  # noqa: PLW0603 - one process, one grade
    _stop = None
