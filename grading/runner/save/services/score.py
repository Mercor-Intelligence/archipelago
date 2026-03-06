import httpx
from loguru import logger

from runner.models import ScoringMethodResult
from runner.utils.settings import get_settings

settings = get_settings()


async def save_score_webhook(
    scoring_run_id: str,
    scoring_results: ScoringMethodResult,
    error_message: str | None = None,
):
    """
    Send score result to webhook.

    Args:
        scoring_run_id: The scoring_run_id from scoring_runs table
        scoring_results: The scoring method result containing final_score and breakdown
        error_message: Optional error message if computation failed
    """
    URL = settings.SCORE_WEBHOOK_URL
    API_KEY = settings.SAVE_WEBHOOK_API_KEY

    if not URL or not API_KEY:
        logger.warning("No webhook environment variables set, skipping score webhook")
        return

    payload = {
        "scoring_run_id": scoring_run_id,
        "scoring_results": scoring_results.model_dump(mode="json"),
        "status": "error" if error_message else "completed",
        "error_message": error_message,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            URL,
            json=payload,
            headers={"X-API-Key": API_KEY},
        )
        response.raise_for_status()
        logger.info(
            f"Score webhook sent successfully: {response.status_code} (scoring_run_id={scoring_run_id})"
        )
