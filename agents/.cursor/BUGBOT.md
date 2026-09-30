# Agent Concurrency Bug Bot

See `../linting/concurrency_checks.py` for background: since RLS-9074, every
agent lane in `modal_labs.py` runs under `@modal.concurrent(max_inputs=N)`.
Several trajectories share one container/process, executed as separate
asyncio tasks on a single thread — there is no per-call process or thread
isolation (https://modal.com/docs/guide/concurrent-inputs). Anything
process-global rather than call-local is shared, unguarded, across every
concurrent trajectory in that container.

A static linter (`mise run concurrency-safety`) already blocks the three
concrete shapes we've hit in practice: hardcoded shared `/tmp/...` fallback
paths, and direct `os.environ`/`os.chdir` mutation. It is deliberately narrow
and pattern-based — it cannot reason about *new* shapes of the same
underlying problem. That's what this file is for. Think from first
principles: **could two trajectories running this code at the same moment,
in the same process, corrupt each other's state?** Flag it even if it
doesn't match a known pattern.

## What to look for

Module-level (or class-level singleton) mutable state written from
per-call code — a dict/list/set used as a cache, buffer, or counter that
gets mutated inside a function reachable from `run()`, where "read then
write with an `await` in between" lets one concurrent call clobber
another's entry. Shared client objects that stash per-request state on
`self` (a `.last_response`, `.last_error`, an internal buffer) rather than
returning it directly from the call. Any state that assumes "this
trajectory is the only one running here" — signal handlers, `atexit`
hooks, or setup code that reads as call-local but actually touches
something process-wide, beyond the `os.chdir`/`os.environ` cases the
linter already catches. Cleanup after a timeout that deletes/mutates a
resource a spawned thread or subprocess was using, without confirming
that worker actually stopped — cancelling the wrapper future does not
stop the underlying thread/process. A config value applied identically to
every concurrent trajectory in a lane (e.g. an operator-set field) used as
a literal shared path/resource instead of being namespaced per call.

These are illustrative, not exhaustive — reason about the underlying
question each time rather than pattern-matching to this list.

## What NOT to flag

- Purely local variables, function arguments, and anything scoped to the
  call's own coroutine frame — these are already isolated per call by
  ordinary Python semantics.
- A module-level constant or lookup table that is never written to at
  runtime.
- Contextvars (`contextvars.ContextVar`) — these are correctly isolated
  per asyncio Task by design; that's what they're for.
- A shared async client with no per-request mutable attributes (the common,
  correct pattern for `httpx.AsyncClient`, `AsyncAnthropic`, etc.).
- Anything already covered by the deterministic linter — don't duplicate
  those findings, focus on the shapes it can't see.

## When you find a violation

State concretely: what two concurrent trajectories are doing, in what
order, and what gets corrupted or lost. "This might race" is not
actionable — "trajectory A's `_cache[key]` write can be silently overwritten
by trajectory B between the `await` on line X and the write on line Y,
because they'd race for the same `key`" is.
