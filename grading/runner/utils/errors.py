def format_exception_for_result(exc: BaseException) -> str:
    """Return a non-empty error string for persisted grading results."""
    message = str(exc).strip()
    if message:
        return message

    fallback = repr(exc).strip()
    if fallback:
        return fallback

    return exc.__class__.__name__
