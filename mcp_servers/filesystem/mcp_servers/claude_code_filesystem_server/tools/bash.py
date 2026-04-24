import os
import subprocess
from typing import Annotated

from loguru import logger
from pydantic import Field
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
DEFAULT_TIMEOUT = int(os.getenv("BASH_COMMAND_TIMEOUT", "120"))
MAX_OUTPUT = 100_000


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n\n[output truncated — {len(text):,} chars total, showing first {MAX_OUTPUT:,}]"


@make_async_background
def bash(
    command: Annotated[
        str,
        Field(
            description=(
                "Shell command to execute inside the sandboxed environment. "
                "Runs in bash with the working directory set to the sandbox root (APP_FS_ROOT). "
                "stdout and stderr are both captured and returned. "
                "Example: 'ls -la', 'python -c \"print(1+1)\"', 'cat /myfile.txt'."
            )
        ),
    ],
    timeout: Annotated[
        int,
        Field(
            description=f"Maximum seconds to wait for the command to complete. Default: {DEFAULT_TIMEOUT}.",
            ge=1,
            le=600,
        ),
    ] = DEFAULT_TIMEOUT,
) -> str:
    """Run a shell command in the sandboxed environment. Returns combined stdout and stderr output."""
    logger.debug(f"bash: {command!r} (timeout={timeout}s)")

    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=FS_ROOT,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return f"System error: {exc}"

    output = result.stdout or ""
    if result.stderr:
        stderr = result.stderr.strip()
        if stderr:
            output = output.rstrip() + ("\n\n" if output.strip() else "") + f"Stderr:\n{stderr}"

    if result.returncode != 0:
        output = (output.rstrip() + "\n\n" if output.strip() else "") + f"Exit code: {result.returncode}"

    return _truncate(output) if output.strip() else "(no output)"
