import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import asyncer

_P = ParamSpec("_P")
_R = TypeVar("_R")


def make_async_background(fn: Callable[_P, _R]) -> Callable[_P, Awaitable[_R]]:
    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await asyncer.asyncify(fn)(*args, **kwargs)

    return wrapper
