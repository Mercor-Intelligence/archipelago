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
    expected_args_contains (bool, optional): If true, each expected_args string value must
        appear as a substring in the actual value (str or any list element). No wildcards.
        Ignored when expected_args_regex is true. Default False (exact string match).
    expected_args_regex (bool, optional): If true, each expected_args string value is a
        Python regex matched with re.search() against the actual value (str or list[str]).
        Takes precedence over expected_args_contains. Nested objects recurse with the same
        mode. Non-string expected values still use exact equality.
    expect_absent (bool, optional): If true, invert the check — PASS when the tool was NOT
        called (and, when expected_args is set, when no call matches those args), FAIL if a
        matching call is present. For read-only / no-write guards. check_success is ignored
        in this mode (an attempted call counts as a violation). Default False.

Example verifier_values:
    {
        "tool_name": "send_email",
        "expected_args": {"to": "user@example.com"},
        "check_success": true
    }
"""

import json
import re
from typing import Any, Literal

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.trajectory import (
    extract_tool_calls_with_outputs,
    unwrap_gateway_cli_calls,
)

_StringMatchMode = Literal["exact", "contains", "regex"]

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


def _string_expected_matches_regex(pattern: str, actual: Any) -> bool:
    """Return True if ``pattern`` matches ``actual`` via re.search.

    Supports ``actual`` as str, or list (e.g. JSON array of recipient emails) by
    matching the joined list or any element.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        logger.warning("Invalid expected_args regex %r: %s", pattern, exc)
        return False
    if isinstance(actual, str):
        return regex.search(actual) is not None
    if isinstance(actual, list):
        joined = ",".join(str(x) for x in actual)
        if regex.search(joined):
            return True
        return any(regex.search(str(x)) for x in actual)
    return regex.search(str(actual)) is not None


def _string_expected_matches_contains(needle: str, actual: Any) -> bool:
    """True if ``needle`` is a substring of ``actual`` (str, list elements, or str(actual))."""
    if isinstance(actual, str):
        return needle in actual
    if isinstance(actual, list):
        return any(needle in str(x) for x in actual)
    return needle in str(actual)


def _args_match(
    actual_args: dict[str, Any],
    expected_args: dict[str, Any],
    *,
    string_match_mode: _StringMatchMode = "exact",
) -> bool:
    """Check if actual arguments contain all expected key-value pairs.

    Args:
        actual_args: The actual arguments from the tool call
        expected_args: The expected argument values to check for
        string_match_mode: How string expected values are compared to actuals.

    Returns:
        True if all expected args are present with matching values
    """
    for key, expected_value in expected_args.items():
        if key not in actual_args:
            return False
        actual_value = actual_args[key]
        if isinstance(expected_value, dict) and isinstance(actual_value, dict):
            if not _args_match(
                actual_value,
                expected_value,
                string_match_mode=string_match_mode,
            ):
                return False
        elif string_match_mode == "regex" and isinstance(expected_value, str):
            if not _string_expected_matches_regex(expected_value, actual_value):
                return False
        elif string_match_mode == "contains" and isinstance(expected_value, str):
            if not _string_expected_matches_contains(expected_value, actual_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _resolve_string_match_mode(verifier_values: dict[str, Any]) -> _StringMatchMode:
    """Regex wins if both expected_args_regex and expected_args_contains are set."""
    if bool(verifier_values.get("expected_args_regex", False)):
        return "regex"
    if bool(verifier_values.get("expected_args_contains", False)):
        return "contains"
    return "exact"


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
    *,
    string_match_mode: _StringMatchMode = "exact",
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
        args_matched = _args_match(
            actual_args,
            expected_args,
            string_match_mode=string_match_mode,
        )
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


def _build_absent_result(
    *,
    verifier_id: str,
    verifier_version: int,
    tool_name: str,
    tool_names: set[str],
    tool_calls: list[dict[str, Any]],
    expected_args: dict[str, Any] | None,
    string_match_mode: _StringMatchMode,
) -> VerifierResult:
    """Verdict for ``expect_absent``: pass iff no matching call is present.

    A call "matches" when its name is in ``tool_names`` and, when ``expected_args``
    is set, its arguments satisfy ``_args_match`` under ``string_match_mode``. Tool
    output success is intentionally ignored — an *attempted* call counts as a
    violation (mirrors criteria like "even if the final DB state did not change").
    """
    violating = [
        tc
        for tc in tool_calls
        if tc["tool_name"] in tool_names
        and (
            not expected_args
            or _args_match(
                _parse_arguments(tc["arguments"]),
                expected_args,
                string_match_mode=string_match_mode,
            )
        )
    ]
    passed = not violating
    target = (
        f"Tool '{tool_name}' with args {expected_args}"
        if expected_args
        else f"Tool '{tool_name}'"
    )
    message = (
        f"{target} was correctly not called"
        if passed
        else f"{target} should not have been called but was ({len(violating)} call(s))"
    )
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if passed else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "tool_name": tool_name,
            "expect_absent": True,
            "found": not passed,
            "violating_calls": len(violating),
            "violating_call_numbers": [tc["call_number"] for tc in violating],
            "expected_args": expected_args,
            "message": message,
        },
        message=message,
    )


async def tool_call_check_eval(input: EvalImplInput) -> VerifierResult:
    """
    Deterministic verifier that checks for a specific tool call.

    Checks if the specified tool was called, optionally with expected arguments,
    and optionally whether it succeeded (no error in output).

    Configuration via verifier_values:
        tool_name (str | list[str], required): Tool name to check for, or a list of
            acceptable names — matches if the call's name is any of them
        expected_args (dict, optional): Arguments that must match
        check_success (bool, optional): Check output for errors (default: False)
        match_any (bool, optional): Pass if any call matches (default: True)
        expected_args_contains (bool, optional): Substring match for string expected_args
        expected_args_regex (bool, optional): Match string expected_args with re.search (overrides contains)
        expect_absent (bool, optional): Invert — pass when the tool was NOT called (default: False)

    Returns:
        VerifierResult with:
        - score: 1.0 if checks pass, 0.0 otherwise
        - verifier_result_values containing match details
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    verifier_values = input.verifier.verifier_values or {}

    # Get configuration
    raw_tool_name = verifier_values.get("tool_name")
    expected_args = verifier_values.get("expected_args")
    check_success = verifier_values.get(
        "check_success", False
    )  # Default False to avoid false positives
    match_any = verifier_values.get("match_any", True)
    expected_args_contains = bool(verifier_values.get("expected_args_contains", False))
    expected_args_regex = bool(verifier_values.get("expected_args_regex", False))
    string_match_mode = _resolve_string_match_mode(verifier_values)
    expect_absent = bool(verifier_values.get("expect_absent", False))

    # tool_name may be a single name or a list of acceptable names (any-of match).
    if isinstance(raw_tool_name, str):
        name_set = {raw_tool_name} if raw_tool_name else set()
    elif isinstance(raw_tool_name, (list, tuple)):
        name_set = {t for t in raw_tool_name if isinstance(t, str) and t}
    else:
        name_set = set()

    # Validate required config
    if not name_set:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "error": "tool_name is required (string or list of strings)"
            },
            message="Configuration error: tool_name is required",
        )

    tool_name = (
        raw_tool_name if isinstance(raw_tool_name, str) else ", ".join(sorted(name_set))
    )

    logger.info(f"Checking for tool call: {tool_name}")

    # Extract tool calls from trajectory
    tool_calls = extract_tool_calls_with_outputs(input.trajectory.messages)
    # Also surface tools invoked via the gateway CLI (mcp_cli through a shell runner)
    # so we match the real tool, not just the runner that wrapped it.
    tool_calls = tool_calls + unwrap_gateway_cli_calls(tool_calls)

    # Absence guard: pass when the (optionally arg-matched) tool was NOT called.
    if expect_absent:
        return _build_absent_result(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            tool_name=tool_name,
            tool_names=name_set,
            tool_calls=tool_calls,
            expected_args=expected_args,
            string_match_mode=string_match_mode,
        )

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

    # Find all calls to the specified tool(s)
    matching_calls = [tc for tc in tool_calls if tc["tool_name"] in name_set]

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
        _check_tool_call(
            tc,
            expected_args,
            check_success,
            string_match_mode=string_match_mode,
        )
        for tc in matching_calls
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
            "expected_args_contains": expected_args_contains,
            "expected_args_regex": expected_args_regex,
            "expected_args_string_match_mode": string_match_mode,
        },
        message=message,
    )
