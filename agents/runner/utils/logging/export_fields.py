"""What a log line may carry to Datadog, directly or through the Modal stdout drain.

Datadog keeps logs longer than the 7-day cap on Taiga content, so a line keeps only ids,
status, counts and timings.
"""

from __future__ import annotations

import traceback
from typing import Any

import loguru

EXPORT_EXTRA_KEYS = frozenset(
    {
        # ids
        "trajectory_id",
        "trajectory_log_id",
        "agent_config_id",
        "agent_id",
        "agent_version",
        "orchestrator_id",
        "orchestrator_version",
        "task_id",
        "task_version",
        "world_id",
        "world_version",
        "subagent_id",
        "child_run_id",
        "problem_run_id",
        "function_call_id",
        "call_id",
        "budget_call_id",
        "compaction_id",
        "execution_framework",
        # event type, status and error class
        "message_type",
        "event",
        "event_type",
        "phase",
        "status",
        "final_status",
        "success",
        "outcome",
        "stop_reason",
        "termination_reason",
        "reason_code",
        "failure_kind",
        "error_type",
        "final_error_class",
        "fault",
        "model",
        "orchestrator_model",
        "tool_name",
        "budget_unit",
        "high_volume",
        "ephemeral",
        # counts and timings
        "step",
        "turn",
        "attempt",
        "attempts",
        "retries_used",
        "remaining_turns",
        "remaining_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "llm_calls",
        "duration_seconds",
        "elapsed_seconds",
        "total_wall_seconds",
        "total_backoff_seconds",
        "failed_attempt_seconds",
        "final_attempt_seconds",
        "func_name",
        "call_cost_usd",
        "cumulative_cost_usd",
        "estimated_usd",
        "cost_unit",
        # routing, gateway probe and Taiga request fields
        "llm_route_target",
        "workload",
        "priority",
        "gateway_probe",
        "probe_reason",
        "probe_ok",
        "probe_status",
        "probe_latency_s",
        "probe_error_type",
        "taiga_endpoint",
        "taiga_method",
        "taiga_status",
        "taiga_outcome",
        "taiga_client",
        "taiga_duration_ms",
        "taiga_campaign",
        "taiga_actor_ip_present",
    }
)


# Harness code logs model and tool content freely, so its lines export no text by default.
CONTENT_MODULE_PREFIXES = ("runner.agents.",)
# Bind True on a vetted operational line in that code, or False on a content line elsewhere.
LOG_EXPORT_TEXT_KEY = "log_export_text"


def export_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """The allowlisted copy of a record's extras; the record is never modified."""
    return {k: v for k, v in extra.items() if k in EXPORT_EXTRA_KEYS}


def exports_text(record: loguru.Record) -> bool:
    """Whether a line's text may leave: its override, else never for an event, else by module."""
    extra = record["extra"]
    override = extra.get(LOG_EXPORT_TEXT_KEY)
    if override is not None:
        return bool(override)
    if extra.get("message_type") is not None:
        return False
    return not (record["name"] or "").startswith(CONTENT_MODULE_PREFIXES)


def export_message(record: loguru.Record) -> str:
    """The line's text when it may leave, else its ``message_type`` or calling function."""
    if exports_text(record):
        return record["message"]
    return str(record["extra"].get("message_type") or record["function"])


def export_exception(exception: Any) -> str:
    """The traceback's frames and exception class: an exception's message can echo content."""
    frames = (
        "".join(traceback.format_tb(exception.traceback)) if exception.traceback else ""
    )
    name = exception.type.__name__ if exception.type is not None else "Exception"
    return f"Traceback (most recent call last):\n{frames}{name}"


# Not an export key: a sink reads it in place of the exception an enqueued record loses.
EXPORT_EXCEPTION_KEY = "exported_exception"


def stamp_export_exception(record: loguru.Record) -> None:
    """Render the exported traceback at log time: an enqueued sink gets the record without frames."""
    exception = record["exception"]
    if exception is not None:
        record["extra"][EXPORT_EXCEPTION_KEY] = export_exception(exception)


def exported_exception(record: loguru.Record) -> str:
    """The traceback rendered at log time, else rendered now."""
    stamped = record["extra"].get(EXPORT_EXCEPTION_KEY)
    if isinstance(stamped, str):
        return stamped
    return export_exception(record["exception"])
