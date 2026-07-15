"""Browser verifier eval - validates browser state (URL, forms, visible text).

This verifier checks the final browser state extracted by the BROWSER_STATE helper,
validating URLs, form values, and visible text against expected values.
"""

import re
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.trace_models import BrowserState
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


def _check_url(
    actual_url: str | None,
    expected: str | None,
    match_type: str = "contains",
) -> tuple[bool, str]:
    """Check if URL matches expected value.

    Args:
        actual_url: The actual URL from browser state
        expected: Expected URL or pattern
        match_type: One of "exact", "contains", "regex", "startswith", "endswith"

    Returns:
        Tuple of (passed, reason)
    """
    if expected is None:
        return True, "No URL requirement"

    if actual_url is None:
        return False, "No URL captured in browser state"

    if match_type == "exact":
        passed = actual_url == expected
        if passed:
            return True, f"URL exactly matches: {actual_url}"
        return False, f"URL '{actual_url}' does not match expected '{expected}'"

    if match_type == "contains":
        passed = expected in actual_url
        if passed:
            return True, f"URL contains '{expected}'"
        return False, f"URL '{actual_url}' does not contain '{expected}'"

    if match_type == "startswith":
        passed = actual_url.startswith(expected)
        if passed:
            return True, f"URL starts with '{expected}'"
        return False, f"URL '{actual_url}' does not start with '{expected}'"

    if match_type == "endswith":
        passed = actual_url.endswith(expected)
        if passed:
            return True, f"URL ends with '{expected}'"
        return False, f"URL '{actual_url}' does not end with '{expected}'"

    if match_type == "regex":
        try:
            match = re.search(expected, actual_url)
            if match:
                return True, "URL matches regex pattern"
            return False, f"URL '{actual_url}' does not match regex"
        except re.error as e:
            return False, f"Invalid regex pattern: {e}"

    return False, f"Unknown match_type: {match_type}"


def _check_form_value(
    form_values: dict[str, str],
    selector: str,
    expected_value: str | None,
    match_type: str = "exact",
) -> tuple[bool, str]:
    """Check if form field has expected value.

    Args:
        form_values: Dict mapping selectors to values
        selector: The field selector to check
        expected_value: Expected value (None means field should exist)
        match_type: One of "exact", "contains", "regex"

    Returns:
        Tuple of (passed, reason)
    """
    actual_value = form_values.get(selector)

    if actual_value is None:
        # Try to find by partial selector match
        for key, value in form_values.items():
            if selector in key or key in selector:
                actual_value = value
                break

    if actual_value is None:
        return False, f"Form field '{selector}' not found"

    if expected_value is None:
        return True, f"Form field '{selector}' exists with value '{actual_value}'"

    if match_type == "exact":
        passed = actual_value == expected_value
        if passed:
            return True, f"Form field '{selector}' = '{actual_value}'"
        return (
            False,
            f"Form field '{selector}' is '{actual_value}', expected '{expected_value}'",
        )

    if match_type == "contains":
        passed = expected_value in actual_value
        if passed:
            return True, f"Form field '{selector}' contains '{expected_value}'"
        return False, f"Form field '{selector}' does not contain '{expected_value}'"

    if match_type == "regex":
        try:
            match = re.search(expected_value, actual_value)
            if match:
                return True, f"Form field '{selector}' matches regex"
            return False, f"Form field '{selector}' does not match regex"
        except re.error as e:
            return False, f"Invalid regex pattern: {e}"

    return False, f"Unknown match_type: {match_type}"


def _check_visible_text(
    visible_text: list[str],
    expected: str,
    match_type: str = "contains",
) -> tuple[bool, str]:
    """Check if visible text contains expected content.

    Args:
        visible_text: List of visible text segments
        expected: Expected text or pattern
        match_type: One of "contains", "regex", "not_contains"

    Returns:
        Tuple of (passed, reason)
    """
    combined_text = " ".join(visible_text)

    if match_type == "contains":
        passed = expected in combined_text
        if passed:
            return True, f"Page contains text '{expected}'"
        return False, f"Page does not contain text '{expected}'"

    if match_type == "not_contains":
        passed = expected not in combined_text
        if passed:
            return True, f"Page does not contain text '{expected}'"
        return False, f"Page unexpectedly contains text '{expected}'"

    if match_type == "regex":
        try:
            match = re.search(expected, combined_text, re.MULTILINE | re.DOTALL)
            if match:
                return True, "Page text matches regex pattern"
            return False, "Page text does not match regex"
        except re.error as e:
            return False, f"Invalid regex pattern: {e}"

    return False, f"Unknown match_type: {match_type}"


async def browser_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Verify browser state against expected values.

    Reads check definitions from eval_config.eval_config_values["checks"],
    uses BROWSER_STATE helper results, and validates URLs, form values,
    and visible text.

    Check definition structure (in eval_config_values):
    {
        "checks": {
            "check_name": {
                "description": "Human-readable description",
                "check_type": "url",  # or "form_value", "visible_text"

                # For url checks:
                "expected_url": "https://example.com",
                "url_match_type": "contains",  # exact, contains, regex, startswith, endswith

                # For form_value checks:
                "selector": "#email",
                "expected_value": "test@example.com",
                "value_match_type": "exact",  # exact, contains, regex

                # For visible_text checks:
                "expected_text": "Welcome",
                "text_match_type": "contains"  # contains, not_contains, regex
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

    # Get browser state from helper results
    if not input.helper_results:
        return _config_error(
            verifier_id,
            verifier_version,
            "Missing helper results (BROWSER_STATE required)",
        )

    browser_state_data = input.helper_results.get(HelperIds.BROWSER_STATE)
    if not browser_state_data:
        logger.warning("[BROWSER_VERIFIER] No browser state in helper results")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": False,
                "reason": "No browser state found in helper results",
            },
            status=VerifierResultStatus.OK,
            message="No browser state found",
        )

    # Parse browser state
    browser_state = BrowserState(**browser_state_data)

    if browser_state.source == "empty":
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            verifier_result_values={
                "check_name": check_name,
                "passed": False,
                "reason": "Browser state is empty (no data captured)",
            },
            status=VerifierResultStatus.OK,
            message="Empty browser state",
        )

    logger.info(
        f"[BROWSER_VERIFIER] Check '{check_name}' with browser state source={browser_state.source}"
    )

    # Execute check based on type
    check_type = check_defn.get("check_type", "url")
    passed = False
    reason = ""
    check_details: dict[str, Any] = {
        "check_name": check_name,
        "check_type": check_type,
        "description": check_defn.get("description", ""),
        "browser_state_source": browser_state.source,
    }

    if check_type == "url":
        expected_url = check_defn.get("expected_url")
        url_match_type = check_defn.get("url_match_type", "contains")
        passed, reason = _check_url(browser_state.url, expected_url, url_match_type)
        check_details["actual_url"] = browser_state.url
        check_details["expected_url"] = expected_url
        check_details["url_match_type"] = url_match_type

    elif check_type == "form_value":
        selector = check_defn.get("selector")
        if not selector:
            return _config_error(
                verifier_id,
                verifier_version,
                f"Check '{check_name}' is missing 'selector' for form_value check",
            )
        expected_value = check_defn.get("expected_value")
        value_match_type = check_defn.get("value_match_type", "exact")
        passed, reason = _check_form_value(
            browser_state.form_values, selector, expected_value, value_match_type
        )
        check_details["selector"] = selector
        check_details["expected_value"] = expected_value
        check_details["value_match_type"] = value_match_type
        check_details["form_values"] = browser_state.form_values

    elif check_type == "visible_text":
        expected_text = check_defn.get("expected_text")
        if not expected_text:
            return _config_error(
                verifier_id,
                verifier_version,
                f"Check '{check_name}' is missing 'expected_text' for visible_text check",
            )
        text_match_type = check_defn.get("text_match_type", "contains")
        passed, reason = _check_visible_text(
            browser_state.visible_text, expected_text, text_match_type
        )
        check_details["expected_text"] = expected_text
        check_details["text_match_type"] = text_match_type
        # Include preview of visible text
        combined = " ".join(browser_state.visible_text)
        max_len = 500
        check_details["visible_text_preview"] = (
            combined[:max_len] + "..." if len(combined) > max_len else combined
        )

    else:
        return _config_error(
            verifier_id,
            verifier_version,
            f"Unknown check_type '{check_type}'. Valid types: url, form_value, visible_text",
        )

    check_details["passed"] = passed
    check_details["reason"] = reason

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values=check_details,
        status=VerifierResultStatus.OK,
        message="" if passed else reason,
    )
