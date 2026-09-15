"""CLI verifier eval - validates command execution results from trajectory outputs.

This verifier checks CLI command execution results stored in trajectory output,
validating exit codes and output patterns against expected values.
"""

import re
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


def _config_error(
    verifier_id: str, verifier_version: int, message: str
) -> VerifierResult:
    """Return a result for configuration errors (status=ERROR, score=0.0)."""
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=0.0,
        verifier_result_values={"error": message},
        status=VerifierResultStatus.ERROR,
        message=message,
    )


def _get_cli_results(trajectory_output: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract CLI results from trajectory output.

    CLI results are expected in trajectory.output["cli_results"] with structure:
    {
        "commands": [
            {
                "command": "ls -la",
                "exit_code": 0,
                "stdout": "...",
                "stderr": "",
                "duration_ms": 150
            },
            ...
        ]
    }
    """
    if not trajectory_output:
        return None
    return trajectory_output.get("cli_results")


def _check_exit_code(
    actual_exit_code: int | None,
    expected_exit_code: int | str | None,
) -> tuple[bool, str]:
    """Check if exit code matches expected value.

    Args:
        actual_exit_code: The actual exit code from command execution
        expected_exit_code: Expected exit code (int, "success", "failure", or None for any)

    Returns:
        Tuple of (passed, reason)
    """
    if expected_exit_code is None:
        return True, "No exit code requirement"

    if actual_exit_code is None:
        return False, "No exit code captured"

    # Handle string values
    if isinstance(expected_exit_code, str):
        if expected_exit_code.lower() == "success":
            passed = actual_exit_code == 0
            return passed, f"Exit code {actual_exit_code} (expected: 0 for success)"
        elif expected_exit_code.lower() == "failure":
            passed = actual_exit_code != 0
            return (
                passed,
                f"Exit code {actual_exit_code} (expected: non-zero for failure)",
            )
        else:
            # Try to parse as int
            try:
                expected_int = int(expected_exit_code)
                passed = actual_exit_code == expected_int
                return (
                    passed,
                    f"Exit code {actual_exit_code} (expected: {expected_int})",
                )
            except ValueError:
                return False, f"Invalid expected_exit_code value: {expected_exit_code}"

    # Integer comparison
    passed = actual_exit_code == expected_exit_code
    return passed, f"Exit code {actual_exit_code} (expected: {expected_exit_code})"


def _check_output_pattern(
    output: str | None,
    pattern: str | None,
    pattern_type: str = "contains",
) -> tuple[bool, str]:
    """Check if output matches expected pattern.

    Args:
        output: The actual output (stdout or stderr)
        pattern: The expected pattern to match
        pattern_type: One of "contains", "regex", "exact", "not_contains"

    Returns:
        Tuple of (passed, reason)
    """
    if pattern is None:
        return True, "No output pattern requirement"

    if output is None:
        output = ""

    if pattern_type == "exact":
        passed = output.strip() == pattern.strip()
        if passed:
            return True, "Output exactly matches expected"
        return False, f"Output does not exactly match (got {len(output)} chars)"

    if pattern_type == "contains":
        passed = pattern in output
        if passed:
            return True, f"Output contains '{pattern}'"
        return False, f"Output does not contain '{pattern}'"

    if pattern_type == "not_contains":
        passed = pattern not in output
        if passed:
            return True, f"Output does not contain '{pattern}'"
        return False, f"Output unexpectedly contains '{pattern}'"

    if pattern_type == "regex":
        try:
            match = re.search(pattern, output, re.MULTILINE | re.DOTALL)
            if match:
                return True, "Output matches regex pattern"
            return False, "Output does not match regex pattern"
        except re.error as e:
            return False, f"Invalid regex pattern: {e}"

    return False, f"Unknown pattern_type: {pattern_type}"


async def cli_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Verify CLI command execution results.

    Reads check definitions from eval_config.eval_config_values["checks"],
    extracts CLI results from trajectory output, and validates exit codes
    and output patterns.

    Check definition structure (in eval_config_values):
    {
        "checks": {
            "check_name": {
                "description": "Human-readable description",
                "expected_exit_code": 0,  # or "success", "failure", null
                "stdout_pattern": "expected text",
                "stdout_pattern_type": "contains",  # or "regex", "exact", "not_contains"
                "stderr_pattern": null,
                "stderr_pattern_type": "contains",
                "command_index": 0  # Which command to check (-1 for last)
            }
        }
    }

    Verifier values:
    - check_to_run: Name of the check to execute
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    verifier_values = input.verifier.verifier_values or {}
    eval_config_values = input.eval_config.eval_config_values

    # Get check name to run
    check_name = verifier_values.get("check_to_run")
    if not check_name:
        return _config_error(
            verifier_id,
            verifier_version,
            "Missing 'check_to_run' in verifier_values",
        )

    # Get check definitions
    checks = eval_config_values.get("checks", {})
    if not checks:
        return _config_error(
            verifier_id,
            verifier_version,
            "No checks defined in eval_config_values['checks']",
        )

    check_defn = checks.get(check_name)
    if not check_defn:
        available = ", ".join(checks.keys()) if checks else "none"
        return _config_error(
            verifier_id,
            verifier_version,
            f"Check '{check_name}' not found. Available: {available}",
        )

    # Get CLI results from trajectory
    trajectory_output = input.trajectory.output if input.trajectory else None
    cli_results = _get_cli_results(trajectory_output)

    if not cli_results:
        logger.warning("[CLI_VERIFIER] No CLI results in trajectory output")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": False,
                "reason": "No CLI results found in trajectory output",
            },
            status=VerifierResultStatus.OK,
            message="No CLI results found",
        )

    commands = cli_results.get("commands", [])
    if not commands:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": False,
                "reason": "No commands in CLI results",
            },
            status=VerifierResultStatus.OK,
            message="No commands found",
        )

    # Get command to check
    command_index = check_defn.get("command_index", -1)  # Default to last command
    if command_index is None:
        command_index = -1  # Handle explicit null in config
    if command_index < 0:
        command_index = len(commands) + command_index
    if command_index < 0 or command_index >= len(commands):
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": False,
                "reason": f"Command index {command_index} out of range (0-{len(commands) - 1})",
            },
            status=VerifierResultStatus.OK,
            message="Invalid command index",
        )

    command = commands[command_index]
    actual_command = command.get("command", "")
    actual_exit_code = command.get("exit_code")
    actual_stdout = command.get("stdout", "")
    actual_stderr = command.get("stderr", "")

    logger.info(
        f"[CLI_VERIFIER] Checking command: {actual_command[:50]}... "
        f"exit_code={actual_exit_code}"
    )

    # Run all checks
    check_results: list[dict[str, Any]] = []
    all_passed = True

    # Check exit code (only if explicitly configured)
    expected_exit_code = check_defn.get("expected_exit_code")
    if expected_exit_code is not None:
        exit_passed, exit_reason = _check_exit_code(
            actual_exit_code, expected_exit_code
        )
        check_results.append(
            {
                "check": "exit_code",
                "passed": exit_passed,
                "reason": exit_reason,
            }
        )
        if not exit_passed:
            all_passed = False

    # Check stdout pattern
    stdout_pattern = check_defn.get("stdout_pattern")
    stdout_pattern_type = check_defn.get("stdout_pattern_type", "contains")
    if stdout_pattern is not None:
        stdout_passed, stdout_reason = _check_output_pattern(
            actual_stdout, stdout_pattern, stdout_pattern_type
        )
        check_results.append(
            {
                "check": "stdout",
                "pattern_type": stdout_pattern_type,
                "passed": stdout_passed,
                "reason": stdout_reason,
            }
        )
        if not stdout_passed:
            all_passed = False

    # Check stderr pattern
    stderr_pattern = check_defn.get("stderr_pattern")
    stderr_pattern_type = check_defn.get("stderr_pattern_type", "contains")
    if stderr_pattern is not None:
        stderr_passed, stderr_reason = _check_output_pattern(
            actual_stderr, stderr_pattern, stderr_pattern_type
        )
        check_results.append(
            {
                "check": "stderr",
                "pattern_type": stderr_pattern_type,
                "passed": stderr_passed,
                "reason": stderr_reason,
            }
        )
        if not stderr_passed:
            all_passed = False

    # Validate that at least one check was configured
    if not check_results:
        return _config_error(
            verifier_id,
            verifier_version,
            f"Check '{check_name}' has no requirements configured "
            "(need at least one of: expected_exit_code, stdout_pattern, stderr_pattern)",
        )

    # Build result
    result_values: dict[str, Any] = {
        "check_name": check_name,
        "description": check_defn.get("description", ""),
        "command": actual_command,
        "command_index": command_index,
        "exit_code": actual_exit_code,
        "passed": all_passed,
        "check_results": check_results,
    }

    # Include truncated output for debugging
    max_output_len = 500
    if actual_stdout:
        result_values["stdout_preview"] = (
            actual_stdout[:max_output_len] + "..."
            if len(actual_stdout) > max_output_len
            else actual_stdout
        )
    if actual_stderr:
        result_values["stderr_preview"] = (
            actual_stderr[:max_output_len] + "..."
            if len(actual_stderr) > max_output_len
            else actual_stderr
        )

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if all_passed else 0.0,
        verifier_result_values=result_values,
        status=VerifierResultStatus.OK,
        message="" if all_passed else "One or more checks failed",
    )
