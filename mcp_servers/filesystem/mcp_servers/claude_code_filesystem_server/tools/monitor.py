import asyncio
import os
from typing import Annotated

from loguru import logger
from pydantic import Field

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
DEFAULT_TIMEOUT = int(os.getenv("BASH_COMMAND_TIMEOUT", "120"))
MAX_LINES = 10_000


async def monitor(
    command: Annotated[
        str,
        Field(
            description=(
                "Shell command to run in the background. stdout and stderr are streamed "
                "line by line and returned as they arrive. Intended for long-running "
                "processes (servers, build watchers, test runners) where you want to "
                "observe output as it is produced. "
                "Example: 'npm run dev', 'pytest --tb=short -q', 'tail -f /var/log/app.log'."
            )
        ),
    ],
    timeout: Annotated[
        int,
        Field(
            description=f"Maximum seconds to wait for the process to complete. Default: {DEFAULT_TIMEOUT}.",
            ge=1,
            le=600,
        ),
    ] = DEFAULT_TIMEOUT,
) -> str:
    """Run a command and stream its output line by line. Returns all output when the process exits or times out."""
    logger.debug(f"monitor: {command!r} (timeout={timeout}s)")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=FS_ROOT,
        )
    except Exception as exc:
        return f"System error: {exc}"

    lines: list[str] = []

    async def _read_lines() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            logger.debug(f"monitor output: {line}")
            lines.append(line)
            if len(lines) >= MAX_LINES:
                proc.kill()
                break

    try:
        await asyncio.wait_for(_read_lines(), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        lines.append(f"\n[process timed out after {timeout}s]")
    except Exception as exc:
        lines.append(f"\n[error reading output: {exc}]")

    if len(lines) >= MAX_LINES:
        lines.append(f"\n[output truncated — limit of {MAX_LINES} lines reached]")

    if proc.returncode is not None and proc.returncode != 0:
        lines.append(f"\n[exit code: {proc.returncode}]")

    return "\n".join(lines) if lines else "(no output)"
