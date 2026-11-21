"""Fan one harness provider call out into candidate turns Jev picks between.

A ``*_jev`` variant hooks the base harness's module-level provider function
rather than editing it. The hook is a no-op unless the calling task installed a
sampler, so agents sharing the process keep their ordinary single call.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any

from loguru import logger

from runner.utils.jev import JevClient
from runner.utils.usage import UsageTracker

Call = Callable[[], Awaitable[Any]]
Sampler = Callable[[str, Call], Awaitable[Any]]

_sampler: ContextVar[Sampler | None] = ContextVar("jev_sampler", default=None)
_shims: set[Callable[..., Any]] = set()


def current_sampler() -> Sampler | None:
    """Sampler installed for this task, if any."""
    return _sampler.get()


def _hook(module: ModuleType, name: str) -> None:
    target: Callable[..., Any] = getattr(module, name)
    if target in _shims:
        return

    @functools.wraps(target)
    async def shim(*args: Any, **kwargs: Any) -> Any:
        sampler = _sampler.get()
        if sampler is None:
            return await target(*args, **kwargs)
        return await sampler(name, lambda: target(*args, **kwargs))

    _shims.add(shim)
    setattr(module, name, shim)


@contextmanager
def sampling(
    sampler: Sampler, targets: Sequence[tuple[ModuleType, str]]
) -> Iterator[None]:
    """Route ``targets`` through ``sampler`` for this task only."""
    for module, name in targets:
        _hook(module, name)
    token = _sampler.set(sampler)
    try:
        yield
    finally:
        _sampler.reset(token)


def discarded_biller(
    tracker: UsageTracker, bill: Callable[[Any], None]
) -> Callable[[Any], None]:
    """Bill a discarded candidate without touching per-step breakdown state."""

    def billed(result: Any) -> None:
        pending = tracker._pending_compactions  # pyright: ignore[reportPrivateUsage]
        previous = tracker._prev_prompt_tokens  # pyright: ignore[reportPrivateUsage]
        tracker._pending_compactions = 0  # pyright: ignore[reportPrivateUsage]
        try:
            bill(result)
        finally:
            tracker._pending_compactions = pending  # pyright: ignore[reportPrivateUsage]
            tracker._prev_prompt_tokens = previous  # pyright: ignore[reportPrivateUsage]

    return billed


async def select_candidate(
    *,
    call: Call,
    samples: int,
    jev: JevClient,
    state: Callable[[], str],
    instructions: str,
    describe: Callable[[Any], str | None],
    bill: Callable[[Any], None],
) -> Any:
    """Sample ``call`` ``samples`` times and return the turn Jev picks.

    ``describe`` renders a candidate for Jev and returns None for a reply the
    loop cannot use. Discarded candidates are billed here; the caller bills the
    returned one, keeping it last in the usage log.
    """
    results = await asyncio.gather(
        *(call() for _ in range(samples)), return_exceptions=True
    )
    sampled = [r for r in results if not isinstance(r, BaseException)]
    if not sampled:
        # Every sample failed the way one unsampled call would have: re-raise so
        # the loop's own handlers (context overflow, timeout) still see it.
        raise next(r for r in results if isinstance(r, BaseException))

    selected = 0
    try:
        usable = [
            (i, option)
            for i, option in enumerate(_option(describe, result) for result in sampled)
            if option is not None
        ]
        if usable:
            selected = usable[0][0]
        if len(usable) > 1:
            choice = await jev.choose(
                state(), [option for _, option in usable], instructions
            )
            if choice is not None:
                selected = usable[choice.index][0]
                logger.bind(message_type="step").info(
                    f"Jev picked candidate {choice.index + 1}/{len(usable)} "
                    f"(p={choice.probability})"
                )
    except BaseException:
        # Nothing will go on to run the pick, so bill every sample here.
        for result in sampled:
            bill(result)
        raise
    for i, result in enumerate(sampled):
        if i != selected:
            bill(result)
    return sampled[selected]


def _option(describe: Callable[[Any], str | None], result: Any) -> str | None:
    try:
        return describe(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Unrenderable Jev candidate ({type(exc).__name__}); skipping")
        return None
