"""Code execution verifier implementation."""

import asyncio
import os
import re
import stat
import sys
import traceback
from pathlib import Path

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus

_SANDBOX_UID = 65534  # nobody
_SANDBOX_GID = 65534  # nogroup

_SANDBOX_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "PYTHONHASHSEED",
        "PYTHON_VERSION",
        "SSL_CERT_DIR",
    }
)


def _build_sandbox_env(working_dir: Path) -> dict[str, str]:
    """Build a minimal env for the sandboxed subprocess, stripping all secrets."""
    env: dict[str, str] = {}
    for key in _SANDBOX_ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONPATH"] = str(working_dir)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = "/tmp/sandbox"
    return env


_SANDBOX_HOME = Path("/tmp/sandbox")
_ROOT = Path("/")
_WORKING_DIR = Path("/filesystem")

_READABLE = stat.S_IROTH | stat.S_IRGRP
_TRAVERSABLE = _READABLE | stat.S_IXOTH | stat.S_IXGRP
_EXEC_ONLY = stat.S_IXOTH | stat.S_IXGRP


def _prepare_sandbox_fs(working_dir: Path, test_file: Path) -> None:
    """Make the working directory and test file accessible to the sandbox user."""
    _SANDBOX_HOME.mkdir(parents=True, exist_ok=True)
    os.chown(str(_SANDBOX_HOME), _SANDBOX_UID, _SANDBOX_GID)

    if not test_file.is_symlink():
        test_file.chmod(test_file.stat().st_mode | _READABLE)

    # working_dir is the PYTHONPATH root.  Python's FileFinder calls
    # os.scandir() on it to discover packages, which requires read+exec.
    # The os.walk loop below only processes its *contents* (subdirs and
    # files), never working_dir itself, so we set it explicitly here.
    if not working_dir.is_symlink():
        working_dir.chmod(working_dir.stat().st_mode | _TRAVERSABLE)

    # Make all files inside working_dir readable so the sandboxed pytest
    # can import user modules from PYTHONPATH.  Symlinks are skipped to
    # prevent following a link to a sensitive file outside the sandbox
    # and chmod-ing the target world-readable.
    for root, subdirs, filenames in os.walk(working_dir):
        root_path = Path(root)
        for dirname in subdirs:
            try:
                dp = root_path / dirname
                if dp.is_symlink():
                    continue
                dp.chmod(dp.stat().st_mode | _TRAVERSABLE)
            except OSError:
                continue
        for filename in filenames:
            try:
                fp = root_path / filename
                if fp.is_symlink():
                    continue
                fp.chmod(fp.stat().st_mode | _READABLE)
            except OSError:
                continue

    # Ensure parent directories up to / are traversable for both the working
    # dir and the test file's parent (which may differ -- GDM puts tests in
    # /tmp/programmatic_code_execution/ while working_dir is /app/files).
    # Only execute bits are needed for traversal -- read bits (directory
    # listing) are unnecessary and would leak filenames in e.g. /app.
    dirs_to_fix: set[Path] = set()
    for base in (working_dir, test_file.parent):
        d: Path = base
        while d != _ROOT:
            dirs_to_fix.add(d)
            d = d.parent

    for d in dirs_to_fix:
        try:
            d.chmod(d.stat().st_mode | _EXEC_ONLY)
        except OSError:
            continue


async def code_execution_eval(input: EvalImplInput) -> VerifierResult:
    """
    Executes Python code from final answer with configurable unit tests.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    config = input.verifier.verifier_values or {}

    # Extract configuration
    unit_test_code = config.get("unit_test_code", "")
    description = config.get("description") or "unit test"
    timeout_seconds = 300  # Fixed 5 minutes timeout

    # Validate configuration - raise on errors
    if not unit_test_code:
        raise ValueError("unit_test_code is required in verifier configuration")

    # Extract test name and description for better rationale
    test_info = _extract_test_info(unit_test_code)

    working_dir = _WORKING_DIR

    try:
        logger.info(f"[VERIFIER] Working directory: {working_dir}")

        # Write the unit test code to the working directory (filesystem if it exists)
        # Use verifier_id in filename to avoid conflicts between concurrent verifiers
        test_filename = f"test_code_{verifier_id}.py"
        test_file = working_dir / test_filename
        test_file.write_text(unit_test_code, encoding="utf-8")
        _prepare_sandbox_fs(working_dir, test_file)
        logger.info(f"[FILE EXTRACTION] Wrote {test_filename} to {test_file}")

        # Execute the tests - this is the only place we return pass/fail with scores
        execution_log = []
        test_passed = await _run_tests(
            working_dir, timeout_seconds, execution_log, test_filename
        )

        # Return result based on test outcome
        score = 1.0 if test_passed else 0.0

        # Extract actual failed test name from execution log if test failed
        actual_test_name = test_info["name"]
        if not test_passed and execution_log:
            # Parse pytest output to find which test actually failed
            for line in execution_log:
                # Look for FAILED test_code.py::TestClass::test_name pattern
                match = re.search(r"FAILED.*?::(\w+)", line)
                if match:
                    actual_test_name = match.group(1)
                    break

        # Build rationale with test information
        if test_passed:
            rationale = f"{actual_test_name} passed successfully"
            if description:
                rationale += f"\n\nDescription: {description}"
            if test_info["description"]:
                rationale += f"\n\nTest: {test_info['description']}"
        else:
            rationale = f"{actual_test_name} failed"
            if description:
                rationale += f"\n\nDescription: {description}"
            if test_info["description"]:
                rationale += f"\n\nTest: {test_info['description']}"
            # Include execution log to explain why the test failed
            if execution_log:
                rationale += "\n\n" + "\n".join(execution_log)

        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=score,
            status=VerifierResultStatus.OK,
            message="pass" if test_passed else "fail",
            verifier_result_values={
                "passed": test_passed,
                "rationale": rationale,
                "evaluated_artifacts": "",
            },
        )
    finally:
        pass


def _extract_test_info(unit_test_code: str) -> dict[str, str]:
    """
    Extract test name and description from unit test code.
    Returns dict with 'name' and 'description' keys.
    """
    # Extract test method name
    test_name_match = re.search(r"def\s+(test_\w+)\s*\(", unit_test_code)
    test_name = test_name_match.group(1) if test_name_match else "unit_test"

    # Extract docstring if present
    docstring_match = re.search(
        r'def\s+test_\w+\s*\([^)]*\):\s*"""(.*?)"""', unit_test_code, re.DOTALL
    )
    if not docstring_match:
        # Try single quotes
        docstring_match = re.search(
            r"def\s+test_\w+\s*\([^)]*\):\s*'''(.*?)'''", unit_test_code, re.DOTALL
        )

    description = ""
    if docstring_match:
        description = docstring_match.group(1).strip()
    else:
        # Try to extract first comment line as description
        comment_match = re.search(
            r"def\s+test_\w+\s*\([^)]*\):\s*\n\s*#\s*(.+)", unit_test_code
        )
        if comment_match:
            description = comment_match.group(1).strip()

    return {"name": test_name, "description": description}


async def _run_tests(
    working_dir: Path,
    timeout_seconds: int,
    log: list[str],
    test_filename: str,
) -> bool:
    """
    Run the unit tests in the working directory.
    Returns True if tests passed, False otherwise.
    Raises ValueError if there's a syntax error or collection failure in the test code.
    """
    log.append("Starting test execution...")

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        test_filename,
        "-v",
        "--tb=short",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(working_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_sandbox_env(working_dir),
            user=_SANDBOX_UID,
            group=_SANDBOX_GID,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        log.append(f"Test exit code: {process.returncode}")

        if stdout_text:
            log.append("=== Test Output ===")
            log.append(stdout_text)

        if stderr_text:
            log.append("=== Test Errors ===")
            log.append(stderr_text)

        combined_output = stdout_text + "\n" + stderr_text

        # Use pytest exit codes to determine status:
        # 0 = all tests passed
        # 1 = tests ran but some failed (assertion failures)
        # other = raise ValueError
        if process.returncode == 0:
            return True

        # Pytest system/collection errors (2-5) → raise ValueError
        if process.returncode in (2, 3, 4, 5):
            error_msg = (
                f"Test collection or execution error (exit code {process.returncode})"
            )
            if stderr_text:
                error_msg += f"\n\nError output:\n{stderr_text}"
            if stdout_text:
                error_msg += f"\n\nStdout:\n{stdout_text}"
            raise ValueError(error_msg)

        # Exit code 1 = tests ran but some failed
        # Need to distinguish: system errors vs pure test failures
        if process.returncode == 1:
            # Check for system/execution errors in the output
            system_error_patterns = [
                "can't open file",
                "No such file or directory",
                "FileNotFoundError",
                "ImportError",
                "ModuleNotFoundError",
                "ERROR collecting",
                "ERROR at setup",  # setUpClass/setUp failures
                "ERROR at teardown",  # tearDownClass/tearDown failures
                "INTERNALERROR",
                "RuntimeError",  # Explicit runtime errors
            ]

            for pattern in system_error_patterns:
                if pattern in combined_output:
                    error_msg = f"System error during test execution: {pattern} found"
                    if stderr_text:
                        error_msg += f"\n\nError output:\n{stderr_text}"
                    if stdout_text:
                        error_msg += f"\n\nStdout:\n{stdout_text}"
                    raise ValueError(error_msg)

            # No system error patterns found → pure test failure
            return False

        # Any other non-zero exit code → raise ValueError
        error_msg = f"Unexpected exit code {process.returncode}"
        if stderr_text:
            error_msg += f"\n\nError output:\n{stderr_text}"
        if stdout_text:
            error_msg += f"\n\nStdout:\n{stdout_text}"
        raise ValueError(error_msg)

    except asyncio.CancelledError:
        # Task was cancelled, kill the subprocess to prevent resource leak
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        log.append("Test execution cancelled")
        raise
    except TimeoutError:
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        log.append(f"Test execution timed out after {timeout_seconds} seconds")
        raise
    except ValueError:
        # Re-raise ValueError for syntax/collection errors
        raise
    except Exception as e:
        # On any other error, try to kill the subprocess
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
        log.append(f"Test execution error: {str(e)}")
        log.append(traceback.format_exc())
        raise
