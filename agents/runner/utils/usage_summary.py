"""One log line per finished trajectory: dataset identity plus token usage.

Metrics cannot carry task ids (Datadog bills per tag combination), so the
identity rides on a log field set instead. Shared by the archipelago and Harbor
run paths so both frameworks produce the same record.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def usage_summary_fields(
    config: Any, output: Any, duration_seconds: float
) -> dict[str, object]:
    """The record for one trajectory; None where the run has no value."""
    usage = output.usage or {}
    return {
        "trajectory_id": config.trajectory_id,
        "task_id": config.task_id,
        "task_version": config.task_version,
        "world_id": config.world_id,
        "world_version": config.world_version,
        "agent_config_id": config.agent_config.agent_config_id,
        "agent_id": config.agent_id,
        "orchestrator_model": config.orchestrator_model,
        "status": output.status.value,
        **{key: usage.get(key) for key in _TOKEN_KEYS},
        "llm_calls": len(usage.get("call_log") or []),
        "duration_seconds": round(duration_seconds, 3),
    }


def log_usage_summary(config: Any, output: Any, duration_seconds: float) -> None:
    """Emit `trajectory_usage_summary`; a logging failure can never fail the run."""
    if config is None or output is None:
        return
    try:
        fields = usage_summary_fields(config, output, duration_seconds)
        logger.bind(**fields).info(
            f"trajectory_usage_summary trajectory_id={fields['trajectory_id']} "
            f"agent_config_id={fields['agent_config_id']} "
            f"task_id={fields['task_id']} task_version={fields['task_version']} "
            f"prompt_tokens={fields['prompt_tokens']} "
            f"cached_tokens={fields['cached_tokens']}"
        )
    except Exception as e:
        logger.debug(f"Failed to log trajectory usage summary: {e}")
