"""
Webhook service for reporting trajectory results to RL Studio.

Payload schema:
- trajectory_id: The trajectory ID
- trajectory_json: JSON string of AgentTrajectoryOutput
- trajectory_snapshot_id: string
"""

import json
from typing import Any

import httpx
from loguru import logger

from runner.agents.models import AgentTrajectoryOutput
from runner.utils.settings import get_settings


def _recursive_serialize(value: Any) -> Any:
    """Serialize Pydantic ValidatorIterator objects into plain lists.

    Pydantic v2 has a bug (https://github.com/pydantic/pydantic/issues/9541) where
    model_dump_json() on str | Iterable unions creates ValidatorIterator objects
    instead of properly handling discriminated unions. This function walks the dict
    and converts any iterables into plain lists to avoid serialization failures.
    """
    if isinstance(value, dict):
        return {k: _recursive_serialize(v) for k, v in value.items()}
    if isinstance(value, (str, bytes)):
        return value
    if hasattr(value, "__iter__"):
        return [_recursive_serialize(item) for item in value]
    return value


async def report_trajectory_result(
    trajectory_id: str,
    output: AgentTrajectoryOutput,
    snapshot_id: str | None,
    post_populate_snapshot_id: str | None = None,
):
    """
    Report trajectory results to RL Studio via webhook.

    Args:
        trajectory_id: The trajectory ID
        output: The agent run output with status, messages, and metrics
        snapshot_id: The S3 snapshot ID (None if snapshot wasn't created)
        post_populate_snapshot_id: S3 snapshot captured after populate hooks run (None if no hooks)
    """
    settings = get_settings()

    url = settings.SAVE_WEBHOOK_URL
    api_key = settings.SAVE_WEBHOOK_API_KEY

    if not url or not api_key:
        logger.warning("No webhook URL/API key configured, skipping result reporting")
        return

    # Use model_dump() + json.dumps() instead of model_dump_json() to avoid
    # Pydantic v2 bug with str | Iterable unions (ValidatorIterator issue).
    # See https://github.com/pydantic/pydantic/issues/9541.
    trajectory_dict = _recursive_serialize(output.model_dump(mode="json"))

    payload = {
        "trajectory_id": trajectory_id,
        "trajectory_json": json.dumps(trajectory_dict),
        "trajectory_snapshot_id": snapshot_id if snapshot_id else None,
        "post_populate_snapshot_id": post_populate_snapshot_id,
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"X-API-Key": api_key},
        )
        response.raise_for_status()
        logger.info(
            f"Status saved successfully: {response.status_code} (trajectory_id={trajectory_id})"
        )
