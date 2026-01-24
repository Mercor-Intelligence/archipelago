import os
import shutil
import signal
import subprocess

from loguru import logger
from models.code_exec import (
    ActionInfo,
    CodeExecRequest,
    CodeExecResponse,
    HelpResponse,
)
from utils.decorators import make_async_background

FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
CODE_EXEC_COMMAND_TIMEOUT = os.getenv("CODE_EXEC_COMMAND_TIMEOUT", "300")
APPS_DATA_PATH = "/.apps_data"  # Path to hide from code execution


def verify_proot_available() -> None:
    """Verify proot is available. Call at server startup, not import time.

    Raises:
        RuntimeError: If proot is not found in PATH.
    """
    if shutil.which("proot") is None:
        raise RuntimeError(
            "proot is required for sandboxed code execution but was not found. "
            "Install proot (apt-get install proot) before starting the server."
        )
    logger.info("proot is available - sandboxing enabled")


# Help definition
CODE_EXEC_HELP = HelpResponse(
    tool_name="code_exec",
    description="Execute shell commands in a sandboxed bash environment.",
    actions={
        "help": ActionInfo(
            description="Show available actions and usage",
            required_params=[],
            optional_params=[],
        ),
        "exec": ActionInfo(
            description="Execute a shell command",
            required_params=["code"],
            optional_params=[],
        ),
    },
)


def _run_sandboxed_command(
    command: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run a command with /.apps_data hidden via proot.

    proot uses ptrace to intercept syscalls and can bind-mount paths
    without requiring privileges. By binding /dev/null over /.apps_data,
    the directory appears empty to the executed command.

    The -w flag sets the initial working directory to FS_ROOT, so commands
    like `ls` will automatically list /filesystem contents.

    Uses start_new_session=True to create a new process group, allowing
    us to kill the entire tree (proot + shell + children) on timeout.
    """
    # Use proot to hide /.apps_data by binding /dev/null over it
    # -w sets the initial working directory so `ls` works like `ls /filesystem`
    proot_command = [
        "proot",
        "-w",
        FS_ROOT,  # Set working directory
        "-b",
        f"/dev/null:{APPS_DATA_PATH}",  # Hide /.apps_data (appears empty)
        "-b",
        "/dev/null:/app",  # Hide /app (appears empty)
        "sh",
        "-c",
        command,
    ]
    process = subprocess.Popen(
        proot_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # Create new process group
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire process group, not just the direct child
        # This ensures proot, the shell, and all child processes are terminated
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            # Process group may already be gone
            process.kill()
        # Drain remaining pipe data and wait for process to terminate
        # Using communicate() instead of wait() avoids deadlocks from full pipes
        process.communicate()
        raise

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)

    return subprocess.CompletedProcess(
        args=command, returncode=0, stdout=stdout, stderr=stderr
    )


@make_async_background
def code_exec(request: CodeExecRequest) -> CodeExecResponse:
    """Execute shell commands or get help."""
    # Handle help action
    if request.action == "help":
        return CodeExecResponse(
            action="help",
            success=True,
            help=CODE_EXEC_HELP,
        )

    # Handle exec action - only reject None, allow empty string (valid in bash)
    if request.code is None:
        return CodeExecResponse(
            action="exec",
            success=False,
            error="Required: code (command to execute)",
        )

    # Safety net: detect raw Python code and provide helpful error
    code_stripped = request.code.strip()

    def looks_like_python_import(code: str) -> bool:
        """Check if code looks like a Python import vs shell command.

        'import' is also an ImageMagick command for screenshots, e.g.:
        - import screenshot.png
        - import -window root desktop.png

        Python imports look like:
        - import module
        - import module.submodule
        - import module as alias
        """
        if not code.startswith("import "):
            return False
        rest = code[7:].strip()  # After "import "
        # Shell import typically has options (-flag) or file paths
        if rest.startswith("-") or "/" in rest.split()[0] if rest else False:
            return False
        # Shell import targets typically have file extensions
        first_documents = rest.split()[0] if rest else ""
        if "." in first_documents and first_documents.rsplit(".", 1)[-1].lower() in (
            "png",
            "jpg",
            "jpeg",
            "gif",
            "bmp",
            "tiff",
            "webp",
            "pdf",
            "ps",
            "eps",
        ):
            return False
        return True

    python_indicators = (
        looks_like_python_import(code_stripped),
        code_stripped.startswith("from "),
        code_stripped.startswith("def "),
        code_stripped.startswith("class "),
        code_stripped.startswith("async def "),
        code_stripped.startswith("@"),  # decorators
    )
    if any(python_indicators):
        return CodeExecResponse(
            action="exec",
            success=False,
            error=(
                "It looks like you passed raw Python code. This tool executes shell "
                "commands, not Python directly. To run Python:\n"
                "• One-liner: python -c 'your_code_here'\n"
                "• Multi-line: Write to file first, then run:\n"
                "  cat > script.py << 'EOF'\n"
                "  your_code\n"
                "  EOF && python script.py"
            ),
        )

    try:
        timeout_value = int(CODE_EXEC_COMMAND_TIMEOUT)
    except ValueError:
        error_msg = f"Invalid timeout value: {CODE_EXEC_COMMAND_TIMEOUT}"
        logger.error(error_msg)
        return CodeExecResponse(
            action="exec",
            output="",
            success=False,
            error=f"Configuration error: {error_msg}",
        )

    try:
        # Use proot-sandboxed execution
        result = _run_sandboxed_command(request.code, timeout_value)
        return CodeExecResponse(
            action="exec",
            output=result.stdout,
            success=True,
        )
    except subprocess.TimeoutExpired:
        error_msg = f"Command timed out after {timeout_value} seconds: {request.code}"
        logger.error(error_msg)
        return CodeExecResponse(
            action="exec",
            output="",
            success=False,
            error=f"Command execution timed out after {timeout_value} seconds",
        )
    except subprocess.CalledProcessError as e:
        error_msg = f"Error running command {request.code}: {e}"
        logger.error(error_msg)
        output = e.stdout if e.stdout else ""
        if e.stderr:
            output += f"\nError output:\n{e.stderr}"
        return CodeExecResponse(
            action="exec",
            output=f"{output}\n\nCommand failed with exit code {e.returncode}",
            success=False,
            error=error_msg,
        )
    except FileNotFoundError:
        error_msg = f"Working directory not found: {FS_ROOT}"
        logger.error(error_msg)
        return CodeExecResponse(
            action="exec",
            output="",
            success=False,
            error=f"Configuration error: {error_msg}",
        )
    except OSError as e:
        error_msg = f"OS error when executing command: {e}"
        logger.error(error_msg)
        return CodeExecResponse(
            action="exec",
            output="",
            success=False,
            error=f"System error: {error_msg}",
        )
