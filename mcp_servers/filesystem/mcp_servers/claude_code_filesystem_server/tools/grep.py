import fnmatch
import os
import re
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, get_fs_root, resolve_under_root, to_sandbox_path

MAX_MATCHES = 1000
MAX_LINE_LEN = 500


@make_async_background
def grep(
    pattern: Annotated[
        str,
        Field(
            description=(
                "Regular expression pattern to search for in file contents. "
                "Uses Python re syntax. Examples: 'def main', 'TODO:', r'\\berror\\b' (whole word). "
                "Case-sensitive by default."
            )
        ),
    ],
    path: Annotated[
        str,
        Field(
            description="Sandbox-relative directory or file to search in. Default: '/' (search all files). Example: '/src'."
        ),
    ] = "/",
    include: Annotated[
        str,
        Field(
            description="Glob pattern to filter which files are searched. Default: '*' (all files). Example: '*.py', '*.{js,ts}'."
        ),
    ] = "*",
    recursive: Annotated[
        bool,
        Field(description="Search subdirectories recursively. Default: true."),
    ] = True,
) -> str:
    """Search for a regex pattern in file contents. Returns file:line:content for each match."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {exc}") from exc

    try:
        root = resolve_under_root(path)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    if not os.path.exists(root):
        return f"[not found: {path}]"

    real_fs_root = os.path.realpath(get_fs_root())
    matches: list[str] = []

    def search_file(file_path: str) -> None:
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if regex.search(line):
                        display_path = to_sandbox_path(os.path.realpath(file_path))
                        line_content = line.rstrip("\n")
                        if len(line_content) > MAX_LINE_LEN:
                            line_content = line_content[:MAX_LINE_LEN] + "…"
                        matches.append(f"{display_path}:{lineno}:{line_content}")
                        if len(matches) >= MAX_MATCHES:
                            return
        except (UnicodeDecodeError, PermissionError, IsADirectoryError):
            pass

    if os.path.isfile(root):
        search_file(root)
    elif recursive:
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            real_dir = os.path.realpath(dirpath)
            if not real_dir.startswith(real_fs_root):
                continue
            for filename in files:
                if not fnmatch.fnmatch(filename, include):
                    continue
                search_file(os.path.join(dirpath, filename))
                if len(matches) >= MAX_MATCHES:
                    break
            if len(matches) >= MAX_MATCHES:
                break
    else:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_file() and fnmatch.fnmatch(entry.name, include):
                    search_file(entry.path)
                    if len(matches) >= MAX_MATCHES:
                        break

    if not matches:
        return f"No matches for '{pattern}' in {path}"

    result = "\n".join(matches)
    if len(matches) >= MAX_MATCHES:
        result += f"\n\n(Results limited to {MAX_MATCHES})"
    return result
