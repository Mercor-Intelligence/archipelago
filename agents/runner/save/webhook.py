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
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

    response = await _post_with_retry(url, payload, api_key)
    # 404 = stale trajectory_id (deleted or discarded+respawned). Treat as
    # terminal so the k8s worker ACKs the SQS message instead of redriving
    # the same dead ID until DLQ. Every other 4xx (401 bad key, 429 rate
    # limit, 422 bad payload) is transient or fixable — _post_with_retry
    # raises for those so the worker preserves the message.
    if response.status_code == 404:
        logger.warning(
            f"Webhook 404 for trajectory_id={trajectory_id}; "
            f"treating as terminal: {response.text}"
        )
        return
    logger.info(
        f"Status saved successfully: {response.status_code} (trajectory_id={trajectory_id})"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(httpx.TransportError),
    reraise=True,
)
async def _post_with_retry(
    url: str, payload: dict[str, Any], api_key: str
) -> httpx.Response:
    """POST the result webhook, retrying transport-level failures.

    This is the run's last chance to report its status — if the report is
    dropped on a transient network blip the trajectory stays PENDING until
    the dead-container detector catches it. HTTP status errors are not
    retried; the endpoint is an idempotent upsert keyed by trajectory_id.
    """
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"X-API-Key": api_key},
        )
        # 404 is returned to the caller (stale trajectory_id, handled as
        # terminal there); every other HTTP error raises.
        if response.status_code != 404:
            response.raise_for_status()
        return response
