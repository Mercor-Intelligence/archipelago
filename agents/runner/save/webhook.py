"""
Webhook service for reporting trajectory results to RL Studio.

Payload schema:
- trajectory_id: The trajectory ID
- trajectory_json: JSON string of AgentTrajectoryOutput
- trajectory_snapshot_id: string
"""

import httpx
from loguru import logger

from runner.agents.models import AgentTrajectoryOutput
from runner.utils.settings import get_settings


async def report_trajectory_result(
    trajectory_id: str,
    output: AgentTrajectoryOutput,
    snapshot_id: str | None,
):
    """
    Report trajectory results to RL Studio via webhook.

    Args:
        trajectory_id: The trajectory ID
        output: The agent run output with status, messages, and metrics
        snapshot_id: The S3 snapshot ID (None if snapshot wasn't created)
    """
    settings = get_settings()

    url = settings.SAVE_WEBHOOK_URL
    api_key = settings.SAVE_WEBHOOK_API_KEY

    if not url or not api_key:
        logger.warning("No webhook URL/API key configured, skipping result reporting")
        return

    payload = {
        "trajectory_id": trajectory_id,
        "trajectory_json": output.model_dump_json(),
        "trajectory_snapshot_id": snapshot_id if snapshot_id else None,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"X-API-Key": api_key},
        )
        response.raise_for_status()
        logger.info(
            f"Status saved successfully: {response.status_code} (trajectory_id={trajectory_id})"
        )
