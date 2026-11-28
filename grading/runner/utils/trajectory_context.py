"""The id of the trajectory this piece of work belongs to.

In its own import-free module so that both `settings` (which adds the trajectory's
trace headers to LLM calls) and `decorators` can read it without an import loop:
`decorators` imports `metrics`, which imports `settings`.
"""

from contextvars import ContextVar

trajectory_id_ctx: ContextVar[str | None] = ContextVar("trajectory_id", default=None)
