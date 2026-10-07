"""Python-only tool, registered in place of ``code_exec`` when ``CODE_EXEC_PYTHON_ONLY=true``."""

import json
import os
from typing import Annotated, Any

from loguru import logger
from mcp_schema import FlatBaseModel as BaseModel
from pydantic import ConfigDict, Field
from tools.code_exec import (
    BLOCKED_PATHS,
    CODE_EXEC_COMMAND_TIMEOUT,
    FS_ROOT,
    SANDBOX_LIBRARY_PATH,
)
from utils.decorators import make_async_background
from utils.sandbox import run_sandboxed_argv

PYTHON_TOOL_NAME = "python_code_execution"
PYTHON_ONLY_ENV = "CODE_EXEC_PYTHON_ONLY"
MEMORY_LIMIT_ENV = "CODE_EXEC_MEMORY_LIMIT_MB"
PROFILE_DIR = "/usr/local/lib/code_execution/profile"
MANIFEST_PATH = f"{PROFILE_DIR}/MANIFEST.json"
MAX_STREAM_CHARS = 64_000


def python_only_enabled() -> bool:
    return os.getenv(PYTHON_ONLY_ENV, "").strip().lower() == "true"


def memory_limit_mb() -> int | None:
    raw = os.getenv(MEMORY_LIMIT_ENV, "").strip()
    return int(raw) if raw else None


def load_manifest() -> dict[str, Any]:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def verify_python_only_available() -> None:
    """Fail startup when python-only mode is on but the profile build is missing or broken."""
    manifest = load_manifest()
    for key in ("python", "launcher"):
        if not os.path.exists(manifest[key]):
            raise RuntimeError(
                f"{PYTHON_ONLY_ENV}=true but {key} {manifest[key]} is missing"
            )
    memory_limit_mb()


def python_tool_description() -> str:
    try:
        packages = ", ".join(
            f"{n}=={v}" for n, v in sorted(load_manifest()["packages"].items())
        )
    except (OSError, KeyError, ValueError):
        packages = "the preinstalled packages"
    memory = memory_limit_mb()
    limits = f"Time limit {CODE_EXEC_COMMAND_TIMEOUT} seconds per call"
    if memory:
        limits += f"; memory limit {memory} MB"
    return (
        "Run Python 3 code and return its stdout, stderr and exit code. Pass Python source "
        "in `code` (not a shell command). Available: the standard library and "
        f"{packages}. No shell, subprocesses, network access or package installs. Each call "
        "runs in a new process, so variables do not persist between calls; files written to "
        f"the working directory do. {limits}. Print the values you need."
    )


class PythonExecResponse(BaseModel):
    """Response model for python_code_execution."""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(
        ...,
        description="True when the code exited 0 without a timeout or system error.",
    )
    output: str = Field(
        ..., description="stdout, then stderr under 'Stderr output:' when present."
    )
    stdout: str = Field(..., description="Captured stdout (first 64000 characters).")
    stderr: str = Field(..., description="Captured stderr (first 64000 characters).")
    exit_code: int | None = Field(
        ..., description="Process exit code; null when the run did not finish."
    )
    timed_out: bool = Field(
        ..., description="True when the time limit stopped the code."
    )
    truncated: bool = Field(
        ..., description="True when stdout or stderr was cut to 64000 characters."
    )


def _cap(text: str) -> tuple[str, bool]:
    return text[:MAX_STREAM_CHARS], len(text) > MAX_STREAM_CHARS


def _response(
    stdout: str, stderr: str, exit_code: int | None, timed_out: bool, note: str = ""
) -> PythonExecResponse:
    out, out_cut = _cap(stdout)
    err, err_cut = _cap(stderr)
    combined = out
    if err.strip():
        combined = (
            f"{out.rstrip()}\n\nStderr output:\n{err.strip()}"
            if out.strip()
            else err.strip()
        )
    if note:
        combined = f"{combined}\n\n{note}" if combined else note
    return PythonExecResponse(
        success=exit_code == 0 and not timed_out and not note,
        output=combined,
        stdout=out,
        stderr=err,
        exit_code=exit_code,
        timed_out=timed_out,
        truncated=out_cut or err_cut,
    )


@make_async_background
def python_code_execution(
    code: Annotated[str, Field(description="Python source code to run.")],
) -> PythonExecResponse:
    """Run Python code in a fresh sandboxed process and return stdout, stderr and exit code."""
    try:
        timeout_value = int(CODE_EXEC_COMMAND_TIMEOUT)
        manifest = load_manifest()
        memory = memory_limit_mb()
    except (OSError, KeyError, ValueError) as e:
        logger.error(f"python_code_execution configuration error: {e}")
        return _response("", "", None, False, f"Configuration error: {e}")

    argv = [manifest["python"], "-I", manifest["launcher"]]
    if memory:
        argv += ["--memory-mb", str(memory)]
    result = run_sandboxed_argv(
        argv,
        timeout_value,
        working_dir=FS_ROOT,
        blocked_paths=BLOCKED_PATHS,
        library_path=SANDBOX_LIBRARY_PATH,
        input_text=code,
        # -I already ignores PYTHONPATH; drop the shared mirror so the profile venv is the only import source.
        extra_env={"PYTHONPATH": ""},
    )
    if result.timed_out:
        return _response(
            result.stdout,
            result.stderr,
            None,
            True,
            f"Execution timed out after {timeout_value} seconds",
        )
    if result.error:
        return _response(
            result.stdout, result.stderr, None, False, f"System error: {result.error}"
        )
    return _response(result.stdout, result.stderr, result.return_code, False)
