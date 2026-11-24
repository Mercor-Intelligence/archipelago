"""Apply a request's ``limit`` to list responses whose provider call takes none."""


def apply_limit(result: dict, limit: int | None) -> dict:
    """Trim ``result["data"]`` to ``limit`` rows, keeping ``count`` in step."""
    if limit and isinstance(result.get("data"), list) and len(result["data"]) > limit:
        result = {**result, "data": result["data"][:limit]}
        if "count" in result:
            result["count"] = limit
    return result
