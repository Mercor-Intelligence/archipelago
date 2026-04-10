"""Tool Call Check eval - deterministic check for specific tool calls in trajectory.

This verifier checks if a specific tool was called with expected inputs and
optionally whether it succeeded. Unlike POSTTRAINING_TOOL_CALL_CHECK, this
is fully deterministic (no LLM) and checks for a specific tool call.

Configuration via verifier_values:
    tool_name (str, required): The exact name of the tool to check for
    expected_args (dict, optional): Arguments that must be present with matching values
    check_success (bool, optional): If true, verify output has no error indicators (default: False)
    match_any (bool, optional): If true, pass if ANY matching call succeeds (default: True)
                                If false, ALL matching calls must succeed

Example verifier_values:
    {
        "tool_name": "send_email",
        "expected_args": {"to": "user@example.com"},
        "check_success": true
    }
"""

import json
import re
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.trajectory import extract_tool_calls_with_outputs

# Error patterns for tool outputs. Prioritize catching real failures over avoiding
# false positives. Patterns derived from production trajectories and infrastructure errors.
ERROR_PATTERNS = [
    # Line-start error prefixes
    r"^error:",
    r"^exception:",
    r"^failed:",
    # Error in middle of text (e.g. "... Error: MutationError(...)", "### Error")
    r"\berror\s*:\s*\w",  # "Error: Something"
    r"###\s+error\b",  # Markdown-style error header
    # Runtime/JS errors
    r"\b(?:ReferenceError|TypeError|SyntaxError|RangeError|ModuleNotFoundError|ImportError)\s*:",
    # Python traceback
    r"traceback\s*\(\s*most\s+recent\s+call\s+last\s*\)",
    r"\bstack\s+trace\b",  # Java/other stack traces
    # JSON-style: error="..." or "error": "msg" (excludes "error": null)
    r"\berror\s*=\s*['\"]",
    r'"error"\s*:\s*"[^"]+"',
    # HTTP status codes
    r"HTTP\s+[45]\d\d\b",
    r"\b500\s+internal\s+server\s+error\b",
    r"\b400\s+bad\s+request\b",
    r"\b401\s+unauthorized\b",
    r"\b403\s+forbidden\b",
    r"\b404\s+not\s+found\b",
    r"\b429\s+too\s+many\s+requests\b",
    r"\b502\s+bad\s+gateway\b",
    r"\b503\s+service\s+unavailable\b",
    # Common failure phrases
    r"\bfailed\s+to\s+\w+",
    r"\bconnection\s+refused\b",
    r"\bconnection\s+reset\b",
    r"\b(?:timed?\s+out|timeout)\b",
    r"\bpermission\s+denied\b",
    r"\baccess\s+denied\b",
    r"\bcommand\s+not\s+found\b",
    r"\bmodule\s+not\s+found\b",
    r"\bno\s+such\s+file\b",
    r"\bENOENT\b",
    r"\bECONNREFUSED\b",
    r"\bETIMEDOUT\b",
    r"\bsegmentation\s+fault\b",
]

ERROR_REGEX = re.compile("|".join(ERROR_PATTERNS), re.IGNORECASE | re.MULTILINE)


def _parse_arguments(arguments: str) -> dict[str, Any]:
    """Parse tool arguments string to dict."""
    if not arguments:
        return {}
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {}


def _args_match(actual_args: dict[str, Any], expected_args: dict[str, Any]) -> bool:
    """Check if actual arguments contain all expected key-value pairs.

    Args:
        actual_args: The actual arguments from the tool call
        expected_args: The expected argument values to check for

    Returns:
        True if all expected args are present with matching values
    """
    for key, expected_value in expected_args.items():
        if key not in actual_args:
            return False
        actual_value = actual_args[key]
        # Handle nested dict comparison
        if isinstance(expected_value, dict) and isinstance(actual_value, dict):
            if not _args_match(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _output_indicates_success(output: str | None) -> bool:
    """Check if tool output indicates success (no error patterns).

    Args:
        output: The tool output string (already normalized by extract_tool_calls_with_outputs)

    Returns:
        True if output appears successful (no error patterns found)
    """
    if output is None:
        return False
    if output == "(empty output)":
        # Empty output is ambiguous - treat as success
        return True
    return ERROR_REGEX.search(output) is None


def _check_tool_call(
    tool_call: dict[str, Any],
    expected_args: dict[str, Any] | None,
    check_success: bool,
) -> dict[str, Any]:
    """Check a single tool call against criteria.

    Returns:
        Dict with:
        - passed: bool
        - args_matched: bool or None if not checked
        - success_check_passed: bool or None if not checked
        - details: str describing what happened
    """
    result: dict[str, Any] = {
        "call_number": tool_call["call_number"],
        "tool_name": tool_call["tool_name"],
        "passed": True,
        "args_matched": None,
        "success_check_passed": None,
        "details": "",
    }

    # Check arguments if expected_args provided
    if expected_args:
        actual_args = _parse_arguments(tool_call["arguments"])
        args_matched = _args_match(actual_args, expected_args)
        result["args_matched"] = args_matched
        if not args_matched:
            result["passed"] = False
            result["details"] = (
                f"Args mismatch. Expected {expected_args}, got {actual_args}"
            )
            return result

    # Check success if requested
    if check_success:
        output = tool_call.get("output")
        success = _output_indicates_success(output)
        result["success_check_passed"] = success
        if not success:
            result["passed"] = False
            result["details"] = (
                f"Output indicates failure: {output[:200] if output else '(no output)'}"
            )
            return result

    result["details"] = "All checks passed"
    return result


async def tool_call_check_eval(input: EvalImplInput) -> VerifierResult:
    """
    Deterministic verifier that checks for a specific tool call.

    Checks if the specified tool was called, optionally with expected arguments,
    and optionally whether it succeeded (no error in output).

    Configuration via verifier_values:
        tool_name (str, required): The exact name of the tool to check for
        expected_args (dict, optional): Arguments that must match
        check_success (bool, optional): Check output for errors (default: False)
        match_any (bool, optional): Pass if any call matches (default: True)

    Returns:
        VerifierResult with:
        - score: 1.0 if checks pass, 0.0 otherwise
        - verifier_result_values containing match details
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    verifier_values = input.verifier.verifier_values or {}

    # Get configuration
    tool_name = verifier_values.get("tool_name")
    expected_args = verifier_values.get("expected_args")
    check_success = verifier_values.get(
        "check_success", False
    )  # Default False to avoid false positives
    match_any = verifier_values.get("match_any", True)

    # Validate required config
    if not tool_name:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "error": "tool_name is required in verifier_values"
            },
            message="Configuration error: tool_name is required",
        )

    logger.info(f"Checking for tool call: {tool_name}")

    # Extract tool calls from trajectory
    tool_calls = extract_tool_calls_with_outputs(input.trajectory.messages)

    if not tool_calls:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "tool_name": tool_name,
                "found": False,
                "matching_calls": [],
                "message": "No tool calls found in trajectory",
            },
            message=f"Tool '{tool_name}' not found - no tool calls in trajectory",
        )

    # Find all calls to the specified tool
    matching_calls = [tc for tc in tool_calls if tc["tool_name"] == tool_name]

    if not matching_calls:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.OK,
            verifier_result_values={
                "tool_name": tool_name,
                "found": False,
                "total_tool_calls": len(tool_calls),
                "tools_called": list({tc["tool_name"] for tc in tool_calls}),
                "matching_calls": [],
                "message": f"Tool '{tool_name}' was not called",
            },
            message=f"Tool '{tool_name}' not found in trajectory",
        )

    # Check each matching call
    check_results = [
        _check_tool_call(tc, expected_args, check_success) for tc in matching_calls
    ]

    passed_calls = [r for r in check_results if r["passed"]]
    failed_calls = [r for r in check_results if not r["passed"]]

    # Determine overall result based on match_any setting
    if match_any:
        # Pass if ANY matching call passed
        passed = len(passed_calls) > 0
    else:
        # Pass only if ALL matching calls passed
        passed = len(failed_calls) == 0

    score = 1.0 if passed else 0.0

    # Build result message
    if passed:
        message = f"Tool '{tool_name}' called successfully ({len(passed_calls)}/{len(matching_calls)} calls passed)"
    else:
        if match_any:
            message = f"Tool '{tool_name}' called but all {len(matching_calls)} calls failed checks"
        else:
            message = f"Tool '{tool_name}' called but {len(failed_calls)}/{len(matching_calls)} calls failed checks"

    logger.info(f"Tool call check result: {message}")

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "tool_name": tool_name,
            "found": True,
            "total_matching_calls": len(matching_calls),
            "passed_calls": len(passed_calls),
            "failed_calls": len(failed_calls),
            "match_any": match_any,
            "check_results": check_results,
            "expected_args": expected_args,
            "check_success": check_success,
        },
        message=message,
    )
