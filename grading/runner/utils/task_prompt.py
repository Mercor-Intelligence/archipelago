"""The prompt a task gave its agent, read back from the trajectory for a grader."""

from typing import Any

from runner.utils.trajectory import resolve_lazy_content

TASK_PROMPT_FIELD_ID = "prompt"
FROM_FIRST_USER_MESSAGE = "first_user_message"
FROM_PROMPT_FIELD = "prompt_field"


def task_prompt(trajectory: Any) -> tuple[str, str]:
    """The prompt's text and where it came from, or ``("", "")`` when there is none.

    The text is the first user message, which the task author wrote. Assistant
    and tool messages are never read. A trajectory with no user message falls
    back to the task's prompt field.
    """
    if trajectory is None:
        return "", ""
    for msg in trajectory.messages or []:
        if msg.get("role") != "user":
            continue
        resolve_lazy_content(msg)
        content = msg.get("content", "")
        if isinstance(content, str):
            return content, FROM_FIRST_USER_MESSAGE
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts), FROM_FIRST_USER_MESSAGE
    fields = getattr(trajectory, "task_custom_fields", None) or {}
    raw = fields.get(TASK_PROMPT_FIELD_ID)
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), FROM_PROMPT_FIELD
    return "", ""
