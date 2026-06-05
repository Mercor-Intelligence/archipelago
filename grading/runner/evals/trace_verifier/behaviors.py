"""Behavior evaluation logic for trace verifier.

Behaviors are anti-patterns to detect in Playwright traces:
- excessive_actions: Too many actions in a short window
- retry_loop: Repeated same action (selector + method)
- error_cascade: Multiple consecutive action errors
"""

from typing import Any

from runner.evals.trace_verifier.models import BehaviorDef, EvalResult, Violation
from runner.helpers.trace_models import PlaywrightTraceData, TraceAction


def evaluate_behavior(
    trace_data: PlaywrightTraceData,
    behavior_def: BehaviorDef | dict[str, Any],
) -> EvalResult:
    """Evaluate a behavior definition against trace data.

    Args:
        trace_data: Parsed Playwright trace data
        behavior_def: Behavior definition with check_type, threshold, etc.

    Returns:
        Dict with score and result values for VerifierResult
    """
    check_type = behavior_def.get("check_type", "")
    threshold = behavior_def.get("threshold", 5)
    window_seconds = behavior_def.get("window_seconds", 10.0)
    penalty_points = behavior_def.get("penalty_points", 30.0)

    violations: list[Violation] = []
    actions = trace_data.actions

    if check_type == "excessive_actions":
        violations = _detect_excessive_actions(actions, threshold, window_seconds)
    elif check_type == "retry_loop":
        violations = _detect_retry_loop(actions, threshold, window_seconds)
    elif check_type == "error_cascade":
        violations = _detect_error_cascade(actions, threshold)
    else:
        # Unknown check_type - return error in result
        return {
            "score": 0.0,
            "verifier_result_values": {
                "check_name": behavior_def.get("name", "unknown"),
                "check_type": "behavior",
                "error": f"Unknown behavior check_type: {check_type}",
                "passed": False,
            },
        }

    # Calculate score (100 - penalties, min 0)
    total_penalty = len(violations) * penalty_points
    score = max(0.0, (100.0 - total_penalty) / 100.0)

    return {
        "score": score,
        "verifier_result_values": {
            "check_name": behavior_def.get("name", "unknown"),
            "check_type": "behavior",
            "behavior_check_type": check_type,
            "passed": len(violations) == 0,
            "violation_count": len(violations),
            "total_penalty": total_penalty,
            "violations": violations,
            "threshold": threshold,
            "window_seconds": window_seconds,
        },
    }


def _detect_excessive_actions(
    actions: list[TraceAction],
    threshold: int,
    window_seconds: float,
) -> list[Violation]:
    """Detect windows with excessive action frequency.

    Args:
        actions: List of trace actions (sorted by wall_time)
        threshold: Number of actions in window to trigger violation
        window_seconds: Time window size in seconds

    Returns:
        List of violation dicts with window_start, window_end, action_count
    """
    violations: list[Violation] = []

    if len(actions) < threshold:
        return violations

    # Sliding window detection (advance past each violation window like _detect_retry_loop)
    i = 0
    while i < len(actions):
        start_action = actions[i]
        window_start = start_action.wall_time
        window_end = window_start + window_seconds

        # Count actions in this window and find last index
        last_in_window_idx = i
        for k in range(i, len(actions)):
            if actions[k].wall_time >= window_end:
                break
            if actions[k].wall_time >= window_start:
                last_in_window_idx = k

        actions_in_window_count = last_in_window_idx - i + 1

        if actions_in_window_count >= threshold:
            violations.append(
                {
                    "window_start": window_start,
                    "window_end": window_end,
                    "action_count": actions_in_window_count,
                    "threshold": threshold,
                }
            )
            # Skip to end of this violation window to avoid duplicate reports
            i = last_in_window_idx + 1
        else:
            i += 1

    return violations


def _detect_retry_loop(
    actions: list[TraceAction],
    threshold: int,
    window_seconds: float,
) -> list[Violation]:
    """Detect repeated same action (method + selector) patterns.

    Args:
        actions: List of trace actions (sorted by wall_time)
        threshold: Number of repetitions to trigger violation
        window_seconds: Time window for repetitions

    Returns:
        List of violation dicts with action signature and count
    """
    violations: list[Violation] = []

    if len(actions) < threshold:
        return violations

    # Track consecutive same-action sequences
    i = 0
    while i < len(actions):
        current = actions[i]
        signature = _action_signature(current)
        window_start = current.wall_time
        window_end = window_start + window_seconds

        # Count repetitions of same action in window
        repetition_count = 1
        j = i + 1
        while j < len(actions):
            next_action = actions[j]
            if next_action.wall_time >= window_end:
                break
            if _action_signature(next_action) == signature:
                repetition_count += 1
            j += 1

        if repetition_count >= threshold:
            violations.append(
                {
                    "action_signature": signature,
                    "repetition_count": repetition_count,
                    "window_start": window_start,
                    "window_end": window_end,
                    "threshold": threshold,
                }
            )
            # Move past this violation window
            i = j
        else:
            i += 1

    return violations


def _detect_error_cascade(
    actions: list[TraceAction],
    threshold: int,
) -> list[Violation]:
    """Detect consecutive action errors.

    Args:
        actions: List of trace actions (sorted by wall_time)
        threshold: Number of consecutive errors to trigger violation

    Returns:
        List of violation dicts with error sequence info
    """
    violations: list[Violation] = []

    if len(actions) < threshold:
        return violations

    consecutive_errors = 0
    error_start_idx = -1
    error_messages: list[str] = []

    for i, action in enumerate(actions):
        if action.error:
            if consecutive_errors == 0:
                error_start_idx = i
            consecutive_errors += 1
            error_messages.append(action.error)

            if consecutive_errors >= threshold:
                violations.append(
                    {
                        "start_index": error_start_idx,
                        "end_index": i,
                        "consecutive_errors": consecutive_errors,
                        "errors": error_messages[:threshold],  # Limit to threshold
                        "threshold": threshold,
                    }
                )
                # Reset to find next cascade
                consecutive_errors = 0
                error_messages = []
        else:
            consecutive_errors = 0
            error_messages = []

    return violations


def _action_signature(action: TraceAction) -> str:
    """Create a signature string for action comparison.

    Args:
        action: TraceAction to create signature for

    Returns:
        String signature combining method and selector
    """
    return f"{action.method}:{action.selector or ''}"
