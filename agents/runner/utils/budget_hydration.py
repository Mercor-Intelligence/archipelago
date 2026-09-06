"""Runner side of meter repair: ask the server to rebuild a Redis meter it lost.

Runners can ``accrue`` but not ``hydrate`` — they have no DB access — and the pre-call gate
reads a missing cap as "unenforced". So an evicted or expired cap silently disables the
guardrail for every call in every trajectory until some server-side read happens to repair
it. This module supplies the callback ``budget_meter.hydrate_if_stale`` invokes, for the one
worker that wins the dedupe lock.

Deliberately NOT inside ``budget_meter``: that file is vendored byte-identical into three
deployables and must keep its runner-safe dependency set (redis/pydantic/loguru) — it must
never learn about HTTP or server config.
"""

from loguru import logger

from runner.utils.settings import get_settings
from runner.utils.studio_http import studio_post_json

settings = get_settings()

_HYDRATE_PATH = "/internal/archipelago/webhooks/budget/hydrate"
# Short on purpose: this sits on the LLM call path and the caller ignores the result — the
# current call proceeds either way, and the next one is gated. `hydrate_if_stale` also
# wraps the call in a hard `asyncio.wait_for`, so this only shapes the per-attempt wait.
_HYDRATE_TIMEOUT_SECONDS = 3.0


async def request_meter_hydration(budget_unit: str) -> None:
    """Ask the server to rebuild this unit's meter from the durable mirror.

    Sends ONLY the cost unit: the cap is read from Postgres server-side and must never be
    supplied by a runner, or a compromised runner could raise its own spend limit.
    """
    if not (settings.RL_STUDIO_API and settings.RL_STUDIO_API_KEY):
        logger.warning(
            f"budget_meter: cannot request rehydrate for {budget_unit} — "
            "RL_STUDIO_API/RL_STUDIO_API_KEY unset"
        )
        return
    await studio_post_json(
        f"{settings.RL_STUDIO_API}{_HYDRATE_PATH}",
        {"trajectory_batch_id": budget_unit},
        {"X-API-Key": settings.RL_STUDIO_API_KEY},
        timeout=_HYDRATE_TIMEOUT_SECONDS,
    )
