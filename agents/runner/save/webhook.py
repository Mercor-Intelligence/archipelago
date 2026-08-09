"""
Webhook service for reporting trajectory results to RL Studio.

Payload schema:
- trajectory_id: The trajectory ID
- trajectory_json: JSON string of AgentTrajectoryOutput
- trajectory_snapshot_id: string
"""

import json
from typing import Any

from loguru import logger

from runner.agents.models import AgentTrajectoryOutput
from runner.utils.settings import get_settings
from runner.utils.studio_http import studio_post_json


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
    env_image_layer_s3_uri: str | None = None,
):
    """
    Report trajectory results to RL Studio via webhook.

    Args:
        trajectory_id: The trajectory ID
        output: The agent run output with status, messages, and metrics
        snapshot_id: The S3 snapshot ID (None if snapshot wasn't created)
        post_populate_snapshot_id: S3 snapshot captured after populate hooks run (None if no hooks)
        env_image_layer_s3_uri: S3 URI of the captured rootfs layer (None if
            capture is disabled or failed); triggers the env image build
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
        "env_image_layer_s3_uri": env_image_layer_s3_uri,
    }

    response = await studio_post_json(
        url,
        payload,
        {"X-API-Key": api_key},
        timeout=300.0,
        treat_404_as_success=True,
    )
    # 404 = stale trajectory_id (deleted or discarded+respawned). Treat as
    # terminal so the k8s worker ACKs the SQS message instead of redriving
    # the same dead ID until DLQ. Every other 4xx (401 bad key, 422 bad
    # payload) raises; 429/5xx are retried by studio_post_json before raising.
    if response.status_code == 404:
        logger.warning(
            f"Webhook 404 for trajectory_id={trajectory_id}; "
            f"treating as terminal: {response.text}"
        )
        return
    logger.info(
        f"Status saved successfully: {response.status_code} (trajectory_id={trajectory_id})"
    )
