import dataclasses
import os
import re
from typing import Annotated

from loguru import logger
from models.code_exec import (
    CODE_DESCRIPTION,
    CodeExecRequest,
    CodeExecResponse,
)
from pydantic import Field
from utils.decorators import make_async_background
from utils.sandbox import (
    DEFAULT_LIBRARY_PATH,
    SandboxResult,
    configured_run_as_user,
    run_sandboxed_command,
    verify_sandbox_library_available,
)

MAX_OUTPUT_SIZE = 100_000  # 100KB general limit
MAX_HTML_OUTPUT_SIZE = 2_000  # 2KB for HTML content
TRUNCATION_TAIL_FRACTION = 4  # share of the budget reserved for the tail
BINARY_SCAN_SIZE = 4_096  # chars of a stream inspected for binary markers
# Share of replacement characters that marks a stream as a byte dump rather
# than text. Set well above what a mis-encoded document produces (a latin-1
# page of prose lands a few percent) and well below an actual byte stream,
# which is typically half replacement characters or more.
BINARY_REPLACEMENT_RATIO = 0.3
# Readable text kept after a suppressed byte dump. Covers the diagnostic
# `echo`/`ls` that commonly follows a binary head in the same call, without
# letting a document's own ASCII structure consume the output budget.
MAX_TRAILING_TEXT = 2_000
_HTML_PATTERN = re.compile(
    r"<(!DOCTYPE|html|head|body|div|script|style)\b", re.IGNORECASE
)
# Magic-number prefixes, written as they arrive here rather than as raw bytes:
# output is already decoded with errors="replace" by the time the tool sees it,
# so any header byte >= 0x80 has become U+FFFD and only all-ASCII magic is still
# matchable. These are the formats the reported dumps carried, and naming them
# lets the notice say what the stream was. Everything else is caught by the NUL
# and replacement-density checks below and gets the generic description.
_BINARY_MAGIC: tuple[tuple[str, str], ...] = (
    ("PK\x03\x04", "a ZIP archive (.zip, .xlsx, .docx, .pptx)"),
    ("PK\x05\x06", "an empty ZIP archive"),
    ("%PDF-", "a PDF document"),
)


def _looks_like_html(text: str) -> bool:
    """Detect if output looks like raw HTML."""
    return bool(_HTML_PATTERN.search(text[:1000]))


def _binary_format(text: str) -> str | None:
    """Describe ``text`` when it is a byte dump, or return None when it is text.

    Only the first ``BINARY_SCAN_SIZE`` characters are inspected, so the cost
    does not grow with output size.
    """
    if not text:
        return None
    for magic, description in _BINARY_MAGIC:
        if text.startswith(magic):
            return description
    head = text[:BINARY_SCAN_SIZE]
    if "\x00" in head:
        return "binary data"
    if head.count("\ufffd") / len(head) >= BINARY_REPLACEMENT_RATIO:
        return "binary data"
    return None


def _split_binary_prefix(text: str) -> tuple[str, str, int]:
    """Split a binary stream into the leading byte dump and the readable rest.

    One call usually runs several commands into the same stream, so a dump is
    routinely followed by another command's output — `cat x.pdf | head -c 100`
    then `ls`. The boundary is the last byte that could not be decoded: every
    NUL and U+FFFD belongs to the dump, and whatever trails the last one is
    text. Cutting there rather than at a line break is deliberate — `head -c N`
    and an archive's trailer both end mid-line, so a line-wise cut would take
    the following command's first line with them.

    The whole stream is scanned, not just ``BINARY_SCAN_SIZE``: the cut needs
    the LAST marker, and one `str.rfind` pass over a stream this size is free.

    What trails the cut is not always another command's output — it is just as
    often the document's own ASCII structure, and no byte in the stream says
    which. A PDF puts its font tables, xref and trailer after the last
    compressed stream, and one that compresses nothing has no marker past its
    header comment at all: measured across the seed corpus of a real world,
    these tails run from ~900 characters to the whole 9 MB file. Keeping them
    unbounded would hand the model the entire output budget for a plain
    `cat x.pdf`, which is what the notice exists to prevent. So only the last
    ``MAX_TRAILING_TEXT`` characters are kept — enough for the diagnostic
    `echo`/`ls` that motivated this, bounded for everything else — and the
    caller is told how much was dropped.

    Returns ``(text, "", 0)`` when there is nothing to keep: a stream with no
    markers at all is one only the magic check caught, and it has no boundary
    to cut at.
    """
    cut = max(text.rfind("\x00"), text.rfind("\ufffd")) + 1
    if not cut:
        return text, "", 0
    remainder = text[cut:]
    if not remainder or _binary_format(remainder) is not None:
        return text, "", 0
    if len(remainder) <= MAX_TRAILING_TEXT:
        return text[:cut], remainder, 0
    kept = remainder[-MAX_TRAILING_TEXT:]
    # Start on a line boundary so the kept text does not open mid-token — but
    # only when that leaves something. A single long line (a one-line `ls -m`,
    # a CSV row) puts its only newline at the end of the window, and trimming
    # past it would discard the output this function exists to preserve.
    line_start = kept.find("\n")
    if line_start != -1 and line_start + 1 < len(kept):
        kept = kept[line_start + 1 :]
    return text[:cut], kept, len(remainder) - len(kept)


def _scrub_stream(text: str, stream: str) -> str:
    """Swap a binary stream for a short notice; return text streams untouched.

    Handing the model an escaped byte stream costs it the whole context window
    and tells it nothing, so the notice says what the stream looked like and
    what to do instead. Readable text trailing the dump is kept after the
    notice, and the count describes only what the notice replaced.
    """
    description = _binary_format(text)
    if description is None:
        return text
    suppressed, kept, dropped = _split_binary_prefix(text)
    notice = (
        f"[{stream} suppressed: {len(suppressed):,} characters of what looks "
        f"like {description}, not readable text. Write binary output to a file "
        f"instead (e.g. `curl -o out.bin <url>`, or `command > out.bin`) and "
        f"read that file with a tool for the format.]"
    )
    if not kept:
        return notice
    if dropped:
        notice += (
            f"\n[{dropped:,} further characters of it were readable but are not "
            f"shown; the last {len(kept):,} follow.]"
        )
    return f"{notice}\n{kept}"


def _replace_binary_streams(result: SandboxResult) -> SandboxResult:
    """Scrub binary output stream by stream, before the streams are combined.

    Per stream because the two usually differ: a command that writes a file to
    stdout puts its real diagnostics on stderr, and those are the part worth
    reading.
    """
    return dataclasses.replace(
        result,
        stdout=_scrub_stream(result.stdout, "stdout"),
        stderr=_scrub_stream(result.stderr, "stderr"),
    )


def _sanitize_output(text: str) -> str:
    """Truncate oversized output, with aggressive limits for HTML blobs.

    Keeps a head and a tail. A long run's opening says what it was doing and its
    closing lines say how it finished, so a head-only cap drops the half that
    usually carries the answer.
    """
    if not text:
        return text
    limit = MAX_HTML_OUTPUT_SIZE if _looks_like_html(text) else MAX_OUTPUT_SIZE
    if len(text) <= limit:
        return text
    tail = limit // TRUNCATION_TAIL_FRACTION
    head = limit - tail
    return (
        text[:head] + f"\n\n[output truncated — {len(text):,} chars total, "
        f"showing first {head:,} and last {tail:,}]\n\n" + text[-tail:]
    )


FS_ROOT = os.getenv("APP_FS_ROOT", "/filesystem")
CODE_EXEC_COMMAND_TIMEOUT = os.getenv("CODE_EXEC_COMMAND_TIMEOUT", "300")
SANDBOX_LIBRARY_PATH = os.getenv("SANDBOX_LIBRARY_PATH", DEFAULT_LIBRARY_PATH)
# Paths to hide from code execution.
# /proc is blocked to prevent reading /proc/self/environ (env var exfiltration)
# and /proc/self/root/* (access to blocked paths via procfs).
# /sys is blocked to prevent system configuration disclosure.
BLOCKED_PATHS = ["/app", "/.apps_data", "/proc", "/sys"]


def verify_sandbox_available() -> None:
    """Verify sandbox library is available. Call at server startup, not import time.

    Skipped in run-as-user mode (``CODE_EXEC_RUN_AS_USER`` set): there the
    boundary is the unprivileged user, not the LD_PRELOAD shim, and
    ``run_sandboxed_command`` never loads ``sandbox_fs.so``. Requiring the
    library at startup would otherwise stop the server from booting in a
    deployment that intentionally ships without it.

    Raises:
        RuntimeError: If the sandbox_fs.so library is not found (default mode).
    """
    if configured_run_as_user():
        logger.info(
            "Run-as-user mode enabled; skipping sandbox library check at startup."
        )
        return
    verify_sandbox_library_available(SANDBOX_LIBRARY_PATH)


@make_async_background
def code_exec(
    request: CodeExecRequest | None = None,
    code: Annotated[str | None, Field(description=CODE_DESCRIPTION)] = None,
) -> CodeExecResponse:
    """Execute a shell command in a sandboxed environment. The 'code' parameter is a shell command string (e.g., 'ls -la', 'pip install pandas'). To run Python, use 'python -c \"your_code\"' or write a script file and execute it. Returns stdout, stderr, and exit code. Do NOT pass raw Python code directly. /proc and /sys are unavailable, so process tools such as ps, top and pgrep do not work."""
    # The command arrives either flat (``code``, as the description and README
    # document it) or inside the ``request`` wrapper this tool has advertised
    # since #10. Both are accepted; a flat ``code`` wins when both are sent.
    if code is None and request is not None:
        code = request.code

    # Reject None code - allow empty string (valid in bash)
    if code is None:
        return CodeExecResponse(
            success=False,
            output="Error: Required parameter 'code' (command to execute)",
        )

    # Safety net: detect raw Python code and provide helpful error
    code_stripped = code.strip()

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
        first_word = rest.split()[0] if rest else ""
        if "." in first_word and first_word.rsplit(".", 1)[-1].lower() in (
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
            success=False,
            output=(
                "Error: It looks like you passed raw Python code. This tool executes shell "
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
            success=False,
            output=f"Configuration error: {error_msg}",
        )

    try:
        # Use LD_PRELOAD-sandboxed execution
        result = run_sandboxed_command(
            command=code,
            timeout=timeout_value,
            working_dir=FS_ROOT,
            blocked_paths=BLOCKED_PATHS,
            library_path=SANDBOX_LIBRARY_PATH,
        )
        result = _replace_binary_streams(result)

        if result.timed_out:
            logger.error(f"Command timed out after {timeout_value} seconds")
            output = f"Command execution timed out after {timeout_value} seconds"
            # Include partial stdout/stderr if captured (helps user debug)
            if result.stdout or result.stderr:
                output += "\n\nPartial output before timeout:"
                if result.stdout:
                    output += f"\nStdout:\n{result.stdout}"
                if result.stderr:
                    output += f"\nStderr:\n{result.stderr}"
            return CodeExecResponse(
                success=False,
                output=_sanitize_output(output),
            )

        if result.error:
            logger.error(f"Error running command: {result.error}")
            output = f"System error: {result.error}"
            # Include partial stdout/stderr if captured (e.g. from exception during run)
            if result.stdout or result.stderr:
                output += "\n\nOutput before error:"
                if result.stdout:
                    output += f"\nStdout:\n{result.stdout}"
                if result.stderr:
                    output += f"\nStderr:\n{result.stderr}"
            return CodeExecResponse(
                success=False,
                output=_sanitize_output(output),
            )

        if result.return_code != 0:
            logger.error(f"Command failed with exit code {result.return_code}")
            output = result.stdout if result.stdout else ""
            if result.stderr:
                output += f"\nError output:\n{result.stderr}"
            return CodeExecResponse(
                success=False,
                output=_sanitize_output(
                    f"{output}\n\nCommand failed with exit code {result.return_code}"
                ),
            )

        output = result.stdout or ""
        if result.stderr:
            stderr_stripped = result.stderr.strip()
            if stderr_stripped:
                if output.strip():
                    output = f"{output.rstrip()}\n\nStderr output:\n{stderr_stripped}"
                else:
                    output = stderr_stripped

        return CodeExecResponse(
            success=True,
            output=_sanitize_output(output),
        )
    except FileNotFoundError:
        error_msg = f"Working directory not found: {FS_ROOT}"
        logger.error(error_msg)
        return CodeExecResponse(
            success=False,
            output=f"Configuration error: {error_msg}",
        )
    except OSError as e:
        error_msg = f"OS error when executing command: {e}"
        logger.error(error_msg)
        return CodeExecResponse(
            success=False,
            output=f"System error: {error_msg}",
        )
