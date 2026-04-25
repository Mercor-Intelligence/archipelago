import subprocess
from typing import Annotated

from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root, to_sandbox_path

DEFAULT_HEAD_LIMIT = 250
MAX_OUTPUT = 200_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n\n[output truncated — {len(text):,} chars, showing first {MAX_OUTPUT:,}]"


@make_async_background
def grep(
    pattern: Annotated[
        str,
        Field(description="The regular expression pattern to search for in file contents"),
    ],
    path: Annotated[
        str | None,
        Field(
            description=(
                "File or directory to search in. Defaults to the sandbox root. "
                "Example: '/src', '/src/main.py'."
            )
        ),
    ] = None,
    glob: Annotated[
        str | None,
        Field(description="Glob pattern to filter files (e.g. '*.js', '*.{ts,tsx}') - maps to rg --glob"),
    ] = None,
    output_mode: Annotated[
        str,
        Field(
            description=(
                "'content' shows matching lines (supports -A/-B/-C context, -n line numbers, head_limit), "
                "'files_with_matches' shows file paths (supports head_limit), "
                "'count' shows match counts per file (supports head_limit). "
                "Defaults to 'files_with_matches'."
            )
        ),
    ] = "files_with_matches",
    B: Annotated[
        int | None,
        Field(alias="-B", description="Lines to show before each match (rg -B). Requires output_mode='content'."),
    ] = None,
    A: Annotated[
        int | None,
        Field(alias="-A", description="Lines to show after each match (rg -A). Requires output_mode='content'."),
    ] = None,
    C: Annotated[
        int | None,
        Field(alias="-C", description="Alias for context."),
    ] = None,
    context: Annotated[
        int | None,
        Field(description="Lines before and after each match (rg -C). Requires output_mode='content'."),
    ] = None,
    n: Annotated[
        bool,
        Field(alias="-n", description="Show line numbers (rg -n). Requires output_mode='content'. Defaults to true."),
    ] = True,
    i: Annotated[
        bool,
        Field(alias="-i", description="Case-insensitive search (rg -i)."),
    ] = False,
    type: Annotated[
        str | None,
        Field(description="File type filter (rg --type). Common: js, py, rust, go, java, ts."),
    ] = None,
    head_limit: Annotated[
        int,
        Field(
            description=(
                "Limit output to first N lines/entries. Defaults to 250. "
                "Pass 0 for unlimited (use sparingly)."
            ),
            ge=0,
        ),
    ] = DEFAULT_HEAD_LIMIT,
    offset: Annotated[
        int,
        Field(description="Skip first N lines/entries before applying head_limit. Defaults to 0.", ge=0),
    ] = 0,
    multiline: Annotated[
        bool,
        Field(description="Enable multiline mode where . matches newlines (rg -U --multiline-dotall). Default: false."),
    ] = False,
) -> str:
    """Search for a regex pattern in file contents using ripgrep."""
    if output_mode not in {"content", "files_with_matches", "count"}:
        raise ValueError(f"output_mode must be 'content', 'files_with_matches', or 'count', got {output_mode!r}")

    search_root = "/"
    if path is not None:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        try:
            search_root = to_sandbox_path(resolve_under_root(path))
        except PathTraversalError as exc:
            raise ValueError(str(exc)) from exc

    try:
        resolved_path = resolve_under_root(search_root)
    except PathTraversalError as exc:
        raise ValueError(str(exc)) from exc

    cmd: list[str] = ["rg", "--no-heading"]

    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    # content mode: default rg behaviour

    if i:
        cmd.append("-i")
    if multiline:
        cmd += ["-U", "--multiline-dotall"]
    if glob:
        cmd += ["--glob", glob]
    if type:
        cmd += ["--type", type]

    if output_mode == "content":
        if n:
            cmd.append("-n")
        ctx = C if C is not None else context
        if ctx is not None:
            cmd += ["-C", str(ctx)]
        else:
            if B is not None:
                cmd += ["-B", str(B)]
            if A is not None:
                cmd += ["-A", str(A)]

    cmd += ["--", pattern, resolved_path]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError("ripgrep (rg) is not installed in this environment")
    except subprocess.TimeoutExpired:
        raise RuntimeError("grep timed out after 30 seconds")

    # rg exits 0 = matches, 1 = no matches, 2 = error
    if result.returncode == 2:
        raise RuntimeError(f"rg error: {result.stderr.strip()}")

    raw = result.stdout
    if not raw.strip():
        return f"No matches for '{pattern}'"

    lines = raw.splitlines()

    # Replace absolute sandbox paths with sandbox-relative paths in output
    from utils.path_utils import get_fs_root
    fs_root = get_fs_root().rstrip("/")
    display_lines = [
        line.replace(fs_root, "", 1) if line.startswith(fs_root) else line
        for line in lines
    ]

    if offset:
        display_lines = display_lines[offset:]
    if head_limit > 0:
        display_lines = display_lines[:head_limit]

    return _truncate("\n".join(display_lines))
