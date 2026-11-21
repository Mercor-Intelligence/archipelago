from uuid import uuid4

from runner.utils.settings import get_settings
from runner.utils.studio_http import studio_post_json

_REGISTER_PATH = "/internal/archipelago/webhooks/subagent-trajectory"
_REGISTER_TIMEOUT_SECONDS = 3.0


async def register_subagent_trajectory(
    parent_trajectory_id: str, spawn_ref: str | None, depth: int
) -> str:
    settings = get_settings()
    studio_api = settings.RL_STUDIO_API
    api_key = settings.RL_STUDIO_API_KEY
    if not studio_api or not api_key:
        raise RuntimeError("RL_STUDIO_API and RL_STUDIO_API_KEY must be configured")

    response = await studio_post_json(
        f"{studio_api}{_REGISTER_PATH}",
        {
            "parent_trajectory_id": parent_trajectory_id,
            "child_trajectory_id": f"traj_{uuid4().hex}",
            "spawn_tool_call_id": spawn_ref,
            "depth": depth,
        },
        {"X-API-Key": api_key},
        timeout=_REGISTER_TIMEOUT_SECONDS,
    )
    payload = response.json()
    child_trajectory_id = (
        payload.get("child_trajectory_id") if isinstance(payload, dict) else None
    )
    if not isinstance(child_trajectory_id, str) or not child_trajectory_id:
        raise ValueError(
            "Subagent registration response did not contain a child trajectory ID"
        )
    return child_trajectory_id
