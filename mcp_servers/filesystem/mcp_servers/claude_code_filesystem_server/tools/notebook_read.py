import json
import os
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root


def _cell_summary(index: int, cell: dict) -> str:
    cell_type = cell.get("cell_type", "unknown")
    cell_id = cell.get("id", "")
    source = cell.get("source", "")
    if isinstance(source, list):
        source = "".join(source)

    id_part = f" id={cell_id!r}" if cell_id else ""
    header = f"Cell {index} [{cell_type}{id_part}]"

    outputs_text = ""
    if cell_type == "code":
        outputs = cell.get("outputs", [])
        output_parts: list[str] = []
        for out in outputs:
            out_type = out.get("output_type", "")
            if out_type in {"stream"}:
                text = out.get("text", "")
                if isinstance(text, list):
                    text = "".join(text)
                if text.strip():
                    output_parts.append(text.rstrip())
            elif out_type in {"execute_result", "display_data"}:
                data = out.get("data", {})
                text = data.get("text/plain", "")
                if isinstance(text, list):
                    text = "".join(text)
                if text.strip():
                    output_parts.append(text.rstrip())
            elif out_type == "error":
                ename = out.get("ename", "")
                evalue = out.get("evalue", "")
                output_parts.append(f"{ename}: {evalue}")
        if output_parts:
            outputs_text = "\nOutput:\n" + "\n".join(output_parts)

    return f"{header}\n{source}{outputs_text}"


@make_async_background
def notebook_read(
    notebook_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the Jupyter notebook (.ipynb) within the sandbox filesystem. "
                "Must start with '/'. Example: '/notebooks/analysis.ipynb'."
            )
        ),
    ],
    cell_numbers: Annotated[
        list[int] | None,
        Field(
            description=(
                "Optional list of 0-indexed cell numbers to read. "
                "If omitted, all cells are returned."
            )
        ),
    ] = None,
) -> str:
    """Read a Jupyter notebook, returning cell source and outputs as formatted text."""
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
    total = len(cells)

    if cell_numbers is not None:
        for n in cell_numbers:
            if n < 0 or n >= total:
                raise ValueError(f"Cell number {n} out of range (notebook has {total} cells, 0-indexed)")
        selected = [(n, cells[n]) for n in cell_numbers]
    else:
        selected = list(enumerate(cells))

    parts = [f"Notebook: {notebook_path} ({total} cells total)"]
    for idx, cell in selected:
        parts.append("")
        parts.append(_cell_summary(idx, cell))

    return "\n".join(parts)
