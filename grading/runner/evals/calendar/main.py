"""Calendar field check verifier."""

from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.models import VerifierResult, VerifierResultStatus

from .utils import (
    ParsedCalendar,
    ParsedEvent,
    event_at_time,
    event_on_date,
    events_overlap,
    parse_date,
    parse_ics,
    parse_time,
    rrule_days,
    rrule_freq,
)

# Type alias for check function return values
CheckResult = tuple[bool, str, dict[str, Any]]


def _find_events(cal: ParsedCalendar, query: str) -> list[ParsedEvent]:
    """Find events with summary containing query (case-insensitive).

    Returns empty list if query is empty to avoid matching all events.
    """
    if not query:
        return []
    query_lower = query.lower()
    return [e for e in cal.events if query_lower in e.summary.lower()]


def _parse_int(value: str, field_name: str) -> tuple[int | None, str | None]:
    """Parse string to int, returning (value, error_message)."""
    if not value:
        return None, f"{field_name} is required"
    try:
        return int(value), None
    except ValueError:
        return None, f"Invalid {field_name}: '{value}' is not a number"


# =============================================================================
# Final State Checks
# =============================================================================


def check_event_exists(cal: ParsedCalendar, summary: str, **_: Any) -> CheckResult:
    found = _find_events(cal, summary)
    if found:
        return True, f"Found {len(found)} event(s) matching '{summary}'", {}
    return False, f"No event matching '{summary}' found", {}


def check_event_exists_on_date(
    cal: ParsedCalendar, summary: str, expected_date: str, **_: Any
) -> CheckResult:
    d = parse_date(expected_date)
    if not d:
        return False, f"Invalid date format: {expected_date}", {}
    for e in _find_events(cal, summary):
        if event_on_date(e, d):
            return True, f"Found '{e.summary}' on {d}", {}
    return False, f"No event matching '{summary}' found on {d}", {}


def check_event_time_matches(
    cal: ParsedCalendar,
    summary: str,
    expected_date: str,
    expected_time: str,
    tolerance_minutes: int = 5,
    **_: Any,
) -> CheckResult:
    d = parse_date(expected_date) if expected_date else None
    t = parse_time(expected_time) if expected_time else None
    for e in _find_events(cal, summary):
        if event_at_time(e, d, t, tolerance_minutes):
            return True, f"'{e.summary}' at {e.start}", {"actual": str(e.start)}
    return (
        False,
        f"No event matching '{summary}' at {expected_date} {expected_time}",
        {},
    )


def check_event_count(
    cal: ParsedCalendar, expected_value: str, **_: Any
) -> CheckResult:
    expected, err = _parse_int(expected_value, "expected_value")
    if err or expected is None:
        return False, err or "expected_value is required", {}
    actual = len(cal.events)
    return (
        actual == expected,
        f"Event count: {actual} (expected {expected})",
        {"actual": actual},
    )


def check_event_has_location(
    cal: ParsedCalendar, summary: str, expected_value: str, **_: Any
) -> CheckResult:
    for e in _find_events(cal, summary):
        if expected_value.lower() in e.location.lower():
            return True, f"'{e.summary}' at '{e.location}'", {}
    return False, f"No event matching '{summary}' with location '{expected_value}'", {}


def check_event_duration(
    cal: ParsedCalendar, summary: str, expected_value: str, **_: Any
) -> CheckResult:
    expected_mins, err = _parse_int(expected_value, "expected_value")
    if err or expected_mins is None:
        return False, err or "expected_value is required", {}
    for e in _find_events(cal, summary):
        if (
            e.duration_minutes is not None
            and abs(e.duration_minutes - expected_mins) <= 1
        ):
            return True, f"'{e.summary}' is {e.duration_minutes} min", {}
    return (
        False,
        f"No event matching '{summary}' with ~{expected_mins} min duration",
        {},
    )


def check_event_is_recurring(
    cal: ParsedCalendar, summary: str, **_: Any
) -> CheckResult:
    for e in _find_events(cal, summary):
        if e.rrule:
            return True, f"'{e.summary}' recurs ({rrule_freq(e.rrule)})", {}
    return False, f"No recurring event matching '{summary}'", {}


def check_event_recurrence_pattern(
    cal: ParsedCalendar, summary: str, expected_value: str, **_: Any
) -> CheckResult:
    expected_freq = expected_value.upper()
    for e in _find_events(cal, summary):
        if rrule_freq(e.rrule) == expected_freq:
            return True, f"'{e.summary}' is {expected_freq}", {}
    return False, f"No event matching '{summary}' with {expected_freq} recurrence", {}


def check_event_recurrence_days(
    cal: ParsedCalendar, summary: str, expected_value: str, **_: Any
) -> CheckResult:
    expected_days = {d.strip().upper() for d in expected_value.split(",")}
    for e in _find_events(cal, summary):
        if rrule_days(e.rrule) == expected_days:
            return True, f"'{e.summary}' on {','.join(sorted(expected_days))}", {}
    return False, f"No event matching '{summary}' recurring on {expected_value}", {}


def check_no_events_on_date(
    cal: ParsedCalendar, expected_date: str, **_: Any
) -> CheckResult:
    d = parse_date(expected_date)
    if not d:
        return False, f"Invalid date format: {expected_date}", {}
    found = [e for e in cal.events if event_on_date(e, d)]
    if not found:
        return True, f"No events on {d}", {}
    return False, f"{len(found)} event(s) on {d}: {[e.summary for e in found]}", {}


def check_no_conflicts(cal: ParsedCalendar, **_: Any) -> CheckResult:
    events = cal.events
    for i, a in enumerate(events):
        for b in events[i + 1 :]:
            if events_overlap(a, b):
                return False, f"Conflict: '{a.summary}' and '{b.summary}'", {}
    return True, "No scheduling conflicts", {}


# =============================================================================
# Change-Based Checks (compare initial vs final calendar)
# =============================================================================


def check_event_was_created(
    init: ParsedCalendar | None, final: ParsedCalendar, summary: str, **_: Any
) -> CheckResult:
    init_uids = {e.uid for e in _find_events(init, summary)} if init else set()
    new_events = [e for e in _find_events(final, summary) if e.uid not in init_uids]
    if new_events:
        return True, f"Created {len(new_events)} event(s) matching '{summary}'", {}
    return False, f"No new event matching '{summary}' was created", {}


def check_event_was_deleted(
    init: ParsedCalendar | None, final: ParsedCalendar, summary: str, **_: Any
) -> CheckResult:
    if not init:
        return False, "No initial calendar to compare", {}
    final_uids = {e.uid for e in final.events}
    deleted = [e for e in _find_events(init, summary) if e.uid not in final_uids]
    if deleted:
        return True, f"Deleted {len(deleted)} event(s) matching '{summary}'", {}
    return False, f"Event matching '{summary}' was not deleted", {}


def check_no_events_deleted(
    init: ParsedCalendar | None, final: ParsedCalendar, **_: Any
) -> CheckResult:
    if not init:
        return True, "No initial calendar to compare", {}
    final_uids = {e.uid for e in final.events}
    deleted = [e for e in init.events if e.uid not in final_uids]
    if not deleted:
        return True, "No events were deleted", {}
    return False, f"Deleted: {[e.summary for e in deleted]}", {}


def check_no_events_modified(
    init: ParsedCalendar | None, final: ParsedCalendar, **_: Any
) -> CheckResult:
    if not init:
        return True, "No initial calendar to compare", {}
    init_by_uid = {e.uid: e for e in init.events}
    for final_event in final.events:
        if final_event.uid in init_by_uid:
            init_event = init_by_uid[final_event.uid]
            if (
                final_event.summary,
                final_event.start,
                final_event.end,
                final_event.location,
                final_event.rrule,
            ) != (
                init_event.summary,
                init_event.start,
                init_event.end,
                init_event.location,
                init_event.rrule,
            ):
                return False, f"Modified: '{final_event.summary}'", {}
    return True, "No existing events were modified", {}


# =============================================================================
# Check Registry
# =============================================================================

FINAL_CHECKS: dict[str, Any] = {
    "Event Count": check_event_count,
    "Event Duration": check_event_duration,
    "Event Exists": check_event_exists,
    "Event Exists on Date": check_event_exists_on_date,
    "Event Has Location": check_event_has_location,
    "Event Is Recurring": check_event_is_recurring,
    "Event Recurrence Days": check_event_recurrence_days,
    "Event Recurrence Pattern": check_event_recurrence_pattern,
    "Event Time Matches": check_event_time_matches,
    "No Conflicts": check_no_conflicts,
    "No Events on Date": check_no_events_on_date,
}

CHANGE_CHECKS: dict[str, Any] = {
    "Event Was Created": check_event_was_created,
    "Event Was Deleted": check_event_was_deleted,
    "No Events Deleted": check_no_events_deleted,
    "No Events Modified": check_no_events_modified,
}

ALL_CHECKS: dict[str, Any] = {**FINAL_CHECKS, **CHANGE_CHECKS}


# =============================================================================
# Main Entry Point
# =============================================================================


async def calendar_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Programmatic calendar field verification."""
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    vals = input.verifier.verifier_values or {}
    check_type = str(vals.get("field_to_check", ""))

    def error(msg: str) -> VerifierResult:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=msg,
        )

    if check_type not in ALL_CHECKS:
        return error(f"Unknown check type: {check_type}")

    if not input.helper_results:
        return error("Helper results not available")

    # Extract .ics files from snapshot diff (exclude deleted files)
    diff = input.helper_results.get(HelperIds.SNAPSHOT_DIFF)
    artifacts = extract_artifact_changes_from_diff(diff) if diff else []
    # Filter to .ics files that have final content (not deleted)
    ics_files = [a for a in artifacts if a.path.endswith(".ics") and a.new_content]

    if not ics_files:
        # Check if file was deleted
        deleted_ics = [
            a for a in artifacts if a.path.endswith(".ics") and not a.new_content
        ]
        if deleted_ics:
            # File was deleted - return 0 score, not an error
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.OK,
                verifier_result_values={
                    "check": check_type,
                    "passed": False,
                    "rationale": f"Calendar file {deleted_ics[0].path} was deleted",
                },
                message=f"Calendar file {deleted_ics[0].path} was deleted",
            )
        return error("No .ics calendar file found in snapshot")

    # Parse calendars with error handling
    artifact = ics_files[0]
    assert artifact.new_content  # Filtered above
    try:
        final_cal = parse_ics(artifact.new_content)
    except Exception as e:
        return error(f"Failed to parse calendar {artifact.path}: {e}")

    init_cal = None
    if artifact.old_content:
        try:
            init_cal = parse_ics(artifact.old_content)
        except Exception:
            # Old content malformed is non-fatal; just skip comparison
            pass

    # Build check parameters (all strings for the check functions)
    tolerance_val = vals.get("tolerance_minutes")
    params: dict[str, Any] = {
        "summary": str(vals.get("event_summary", "")),
        "expected_date": str(vals.get("expected_date", "")),
        "expected_time": str(vals.get("expected_time", "")),
        "expected_value": str(vals.get("expected_value", "")),
        "tolerance_minutes": int(tolerance_val) if tolerance_val is not None else 5,
    }

    # Run the check
    check_fn = ALL_CHECKS[check_type]
    if check_type in CHANGE_CHECKS:
        passed, message, extra = check_fn(init_cal, final_cal, **params)
    else:
        passed, message, extra = check_fn(final_cal, **params)

    logger.info(f"[CALENDAR] {check_type} | {'PASS' if passed else 'FAIL'} | {message}")

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if passed else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "check": check_type,
            "passed": passed,
            "rationale": message,
            "calendar_file": artifact.path,
            "event_count": len(final_cal.events),
            **extra,
        },
        message=message,
    )
