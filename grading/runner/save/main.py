from runner.models import GradingRunStatus, ScoringMethodResult, VerifierResult

from .services.score import save_score_webhook
from .services.webhook import save_webhook


async def save(
    grading_run_id: str,
    grading_run_status: GradingRunStatus,
    verifier_results: list[VerifierResult],
    scoring_results: ScoringMethodResult,
):
    await save_webhook(
        grading_run_id,
        grading_run_status,
        verifier_results,
        scoring_results,
    )


async def save_score(
    scoring_run_id: str,
    scoring_results: ScoringMethodResult,
    error_message: str | None = None,
):
    await save_score_webhook(
        scoring_run_id,
        scoring_results,
        error_message,
    )
