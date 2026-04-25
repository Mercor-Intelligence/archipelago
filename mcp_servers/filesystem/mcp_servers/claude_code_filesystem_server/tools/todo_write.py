import json
import os
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
TODO_PATH = os.path.join(FS_ROOT, ".claude", "todos.json")


class TodoItem(BaseModel):
    content: str = Field(description="Description of the task.")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="Current status of the task."
    )
    activeForm: str = Field(description="Active form identifier for this todo item.")


@make_async_background
def todo_write(
    todos: Annotated[
        list[TodoItem],
        Field(description="The updated todo list"),
    ],
) -> str:
    """Write the todo list for this session, replacing the previous list entirely."""
    os.makedirs(os.path.dirname(TODO_PATH), exist_ok=True)

    data = [item.model_dump() for item in todos]

    try:
        with open(TODO_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception as exc:
        raise RuntimeError(f"Failed to write todos: {exc}") from exc

    return f"Wrote {len(todos)} todo item(s)"
