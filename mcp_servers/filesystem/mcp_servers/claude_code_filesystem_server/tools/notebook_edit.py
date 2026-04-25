import json
import os
from typing import Annotated, Literal

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


def _find_cell_index(cells: list[dict], cell_id: str) -> int:
    for i, cell in enumerate(cells):
        if cell.get("id") == cell_id:
            return i
    raise ValueError(f"No cell with id '{cell_id}' found in notebook")


@make_async_background
def notebook_edit(
    notebook_path: Annotated[
        str,
        Field(description="The absolute path to the Jupyter notebook file to edit (must be absolute, not relative)"),
    ],
    new_source: Annotated[
        str,
        Field(description="The new source for the cell"),
    ],
    cell_id: Annotated[
        str | None,
        Field(
            description=(
                "The ID of the cell to edit. For 'insert', the new cell is inserted after this cell, "
                "or at the beginning if omitted. For 'replace' and 'delete', this is the cell to target."
            )
        ),
    ] = None,
    cell_type: Annotated[
        Literal["code", "markdown"],
        Field(description="Cell type for 'insert' operations. Ignored for 'replace' and 'delete'."),
    ] = "code",
    edit_mode: Annotated[
        Literal["replace", "insert", "delete"],
        Field(description="The type of edit to make (replace, insert, delete). Defaults to replace."),
    ] = "replace",
) -> str:
    """Edit a cell in a Jupyter notebook: replace its source, insert a new cell, or delete it."""
    if not notebook_path.startswith("/"):
        raise ValueError("notebook_path must start with '/'")

    try:
        resolved = resolve_under_root(notebook_path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(resolved):
        raise FileNotFoundError(f"File not found: {notebook_path}")
    if not os.path.isfile(resolved):
        raise ValueError(f"Not a file: {notebook_path}")

    try:
        with open(resolved, encoding="utf-8") as f:
            nb = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid notebook JSON: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to read notebook: {exc}") from exc

    cells: list[dict] = nb.get("cells", [])

    if edit_mode == "replace":
        if cell_id is None:
            raise ValueError("cell_id is required for edit_mode='replace'")
        idx = _find_cell_index(cells, cell_id)
        cells[idx]["source"] = new_source
        if cells[idx].get("cell_type") == "code":
            cells[idx]["outputs"] = []
            cells[idx]["execution_count"] = None
        action = f"Replaced source of cell '{cell_id}'"

    elif edit_mode == "insert":
        import uuid
        new_cell: dict = {
            "id": str(uuid.uuid4())[:8],
            "cell_type": cell_type,
            "source": new_source,
            "metadata": {},
        }
        if cell_type == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None
        if cell_id is None:
            cells.insert(0, new_cell)
            action = f"Inserted {cell_type} cell at beginning"
        else:
            idx = _find_cell_index(cells, cell_id)
            cells.insert(idx + 1, new_cell)
            action = f"Inserted {cell_type} cell after '{cell_id}'"

    elif edit_mode == "delete":
        if cell_id is None:
            raise ValueError("cell_id is required for edit_mode='delete'")
        idx = _find_cell_index(cells, cell_id)
        cells.pop(idx)
        action = f"Deleted cell '{cell_id}'"

    nb["cells"] = cells

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
            f.write("\n")
    except Exception as exc:
        raise RuntimeError(f"Failed to write notebook: {exc}") from exc

    return f"{action} in {notebook_path}"
