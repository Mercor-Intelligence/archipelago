"""Read the reasoning effort that an orchestrator sets in its ``extra_args``.

Shared by the Harbor path (``lighthouse_code_agent``) and the CLI harness
wrappers, so that every harness reads the effort from the same locations.
"""

from collections.abc import Collection
from typing import Any

from loguru import logger

REASONING_EFFORT_PATHS: tuple[tuple[str, ...], ...] = (
    ("reasoning_effort",),
    ("effort",),
    ("reasoning", "effort"),
    ("output_config", "effort"),
    ("thinking", "effort"),
    ("thinking_config", "thinking_level"),
    ("thinkingConfig", "thinkingLevel"),
    ("extra_body", "chat_template_kwargs", "reasoning_effort"),
)

# Effort an orchestrator sets only inside extra_body, which agents without extra_model_args drop.
EXTRA_BODY_EFFORT_PATHS: tuple[tuple[str, ...], ...] = (
    ("extra_body", "reasoning", "effort"),
    ("extra_body", "extra_body", "reasoning", "effort"),
)


def dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_effort(
    extra_args: dict[str, Any] | None, paths: tuple[tuple[str, ...], ...]
) -> str | None:
    for path in paths:
        value = dig(extra_args or {}, path)
        if isinstance(value, str) and value:
            return value.lower()
    return None


def extra_body_effort(extra_args: dict[str, Any] | None) -> str | None:
    return _first_effort(extra_args, EXTRA_BODY_EFFORT_PATHS)


def orchestrator_effort(extra_args: dict[str, Any] | None) -> str | None:
    """The orchestrator's effort, lower-cased, in the order the Harbor path reads it:
    ``thinking_level``, then ``REASONING_EFFORT_PATHS``, then the nested extra_body paths."""
    return _first_effort(
        extra_args,
        (("thinking_level",),) + REASONING_EFFORT_PATHS + EXTRA_BODY_EFFORT_PATHS,
    )


def orchestrator_effort_field(
    field: str,
    extra_args: dict[str, Any] | None,
    accepted: Collection[str] | None,
    agent: str,
    *,
    declared: bool,
) -> dict[str, str]:
    """``{field: effort}`` to add to a CLI app's run request, else ``{}``.

    Sends the orchestrator's effort only when the value is in ``accepted`` (``None``
    accepts any value) and the deployed run tool declares ``field`` (``declared``,
    from ``tool_declares_request_field``). Otherwise it logs and sends nothing, so
    the app's own default applies.
    """
    effort = orchestrator_effort(extra_args)
    if not effort:
        return {}
    if accepted is not None and effort not in accepted:
        logger.warning(
            f"{agent}: orchestrator effort {effort!r} is not a value the CLI "
            f"accepts; not sent"
        )
        return {}
    if not declared:
        logger.warning(
            f"{agent}: the deployed run tool does not declare {field!r}; "
            f"orchestrator effort not sent"
        )
        return {}
    return {field: effort}
