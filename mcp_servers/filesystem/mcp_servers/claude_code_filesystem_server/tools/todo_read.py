import json
import os

from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
TODO_PATH = os.path.join(FS_ROOT, ".claude", "todos.json")


@make_async_background
def todo_read() -> str:
    """Read the current todo list for this session."""
    if not os.path.exists(TODO_PATH):
        return "[]"

    try:
        with open(TODO_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        raise RuntimeError(f"Failed to read todos: {exc}") from exc
