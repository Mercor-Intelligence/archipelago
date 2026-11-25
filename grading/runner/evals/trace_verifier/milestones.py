"""Milestone evaluation logic for trace verifier.

Milestones are ordered workflow steps that should occur in a Playwright trace.
This module verifies that the expected actions occurred in the correct order.
"""

import re
from typing import Any

from runner.evals.trace_verifier.models import EvalResult, MilestoneDef, MilestoneStep
from runner.helpers.trace_models import PlaywrightTraceData, TraceAction

# Map Playwright method names to milestone action_type values
METHOD_TO_ACTION_TYPE: dict[str, str] = {
    "goto": "navigate",
    "click": "click",
    "fill": "fill",
    "type": "fill",
    "press": "fill",
    "check": "click",
    "uncheck": "click",
    "selectOption": "click",
    "scroll": "scroll",
    "scrollIntoViewIfNeeded": "scroll",
}


def evaluate_milestone(
    trace_data: PlaywrightTraceData,
    milestone_def: MilestoneDef | dict[str, Any],
) -> EvalResult:
    """Evaluate a milestone definition against trace data.

    Args:
        trace_data: Parsed Playwright trace data
        milestone_def: Milestone definition with name and milestones list

    Returns:
        Dict with score and result values for VerifierResult
    """
    milestones = milestone_def.get("milestones", [])
    if not milestones:
        return {
            "score": 1.0,
            "verifier_result_values": {
                "check_name": milestone_def.get("name", "unknown"),
                "check_type": "milestone",
                "passed": True,
                "found_count": 0,
                "total_count": 0,
                "milestones": [],
                "message": "No milestones defined",
            },
        }

    # Sort milestones by order (if specified)
    sorted_milestones = sorted(
        milestones,
        key=lambda m: m.get("order", 0) if isinstance(m, dict) else 0,
    )

    # Track which milestones were found and in what order
    found_milestones: list[dict[str, Any]] = []
    action_index = 0
    actions = trace_data.actions

    for milestone in sorted_milestones:
        milestone_dict = (
            milestone if isinstance(milestone, dict) else milestone.model_dump()
        )

        # Find matching action from current position
        found = False
        matched_action_index = -1
        matched_action: dict[str, Any] | None = None

        for i in range(action_index, len(actions)):
            if _matches_milestone(actions[i], milestone_dict):
                found = True
                matched_action_index = i
                matched_action = {
                    "method": actions[i].method,
                    "selector": actions[i].selector,
                    "wall_time": actions[i].wall_time,
                }
                action_index = i + 1
                break

        found_milestones.append(
            {
                "milestone": milestone_dict,
                "action_index": matched_action_index,
                "found": found,
                "matched_action": matched_action,
            }
        )

    # Calculate score
    found_count = sum(1 for m in found_milestones if m["found"])
    total_count = len(sorted_milestones)
    score = found_count / total_count if total_count > 0 else 1.0

    return {
        "score": score,
        "verifier_result_values": {
            "check_name": milestone_def.get("name", "unknown"),
            "check_type": "milestone",
            "passed": score == 1.0,
            "found_count": found_count,
            "total_count": total_count,
            "milestones": found_milestones,
        },
    }


def _matches_milestone(
    action: TraceAction, milestone: dict[str, Any] | MilestoneStep
) -> bool:
    """Check if a trace action matches milestone criteria.

    Args:
        action: TraceAction from Playwright trace
        milestone: Milestone definition dict with criteria

    Returns:
        True if action matches all specified criteria
    """
    # Match action_type (map Playwright method to milestone action_type)
    milestone_action_type = milestone.get("action_type")
    if milestone_action_type:
        action_type = METHOD_TO_ACTION_TYPE.get(action.method)
        if action_type != milestone_action_type:
            return False

    # Match selector (exact match)
    milestone_selector = milestone.get("selector")
    if milestone_selector:
        if action.selector != milestone_selector:
            return False

    # Match URL pattern (contains or regex)
    url_pattern = milestone.get("url_pattern")
    if url_pattern:
        # Get URL from action params (goto method stores URL in params)
        action_url = action.params.get("url", "")
        if not action_url:
            return False
        # Try regex match first, fall back to contains
        try:
            if not re.search(url_pattern, action_url):
                return False
        except re.error:
            # Invalid regex, use contains
            if url_pattern not in action_url:
                return False

    # Match text content (contains match in params)
    text_content = milestone.get("text_content")
    if text_content:
        # Check various param fields that might contain text
        param_text = str(action.params.get("text", ""))
        param_value = str(action.params.get("value", ""))
        if text_content not in param_text and text_content not in param_value:
            return False

    return True
