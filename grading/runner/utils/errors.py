from collections.abc import Iterable

CRASHED_SCORE_ERROR = "A verifier crashed; the score is not computable."


def format_exception_for_result(exc: BaseException) -> str:
    """Return a non-empty error string for persisted grading results.

    An exception group is reported by its first leaf, since its own message
    ("unhandled errors in a TaskGroup") names no cause.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves = _leaves(exc)
        if leaves:
            first = leaves[0]
            text = f"{type(first).__name__}: {format_exception_for_result(first)}"
            return f"{text} (+{len(leaves) - 1} more)" if len(leaves) > 1 else text

    message = str(exc).strip()
    if message:
        return message

    fallback = repr(exc).strip()
    if fallback:
        return fallback

    return exc.__class__.__name__


def crashed_score_error(messages: Iterable[str | None]) -> str:
    """The run-level error for a crash, carrying each crashed criterion's cause."""
    details = [m.strip() for m in messages if m and m.strip()]
    if not details:
        return CRASHED_SCORE_ERROR
    return CRASHED_SCORE_ERROR + "".join(f"\n- {d}" for d in details)


def _leaves(group: BaseExceptionGroup) -> list[BaseException]:
    out: list[BaseException] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            out.extend(_leaves(exc))
        else:
            out.append(exc)
    return out
