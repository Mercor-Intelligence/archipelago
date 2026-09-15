"""Score a relayed grade from its collect pass's rows and the caller's verdicts; opens no snapshot."""

import argparse
import asyncio
import json
from typing import Any

from loguru import logger
from pydantic import TypeAdapter, ValidationError

from runner.evals.models import EvalConfig
from runner.evals.output_llm.utils.services.grading_judge import parse_grading_response
from runner.helpers_shared import GRADING_PREFIX
from runner.models import (
    GradingRunStatus,
    ScoringMethodResult,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)
from runner.scoring_methods.models import ScoringConfig, ScoringMethodIds
from runner.scoring_methods.registry import SCORING_METHOD_REGISTRY
from runner.skip_rules import exclude_agentic_from_scoring, exclude_skipped_from_scoring
from runner.utils.errors import format_exception_for_result
from runner.utils.judge_relay import MissingRelayVerdict
from runner.utils.metrics import phase

# The key runner.main._CRASHED writes on a raised judge's row; a test pins the two equal.
CRASHED = "verifier_crashed"


def relayed_verifier_ids(collected: dict[str, Any]) -> set[str]:
    """The verifiers a collect pass sent a judge prompt for."""
    prompts = collected.get("relay_prompts")
    if not isinstance(prompts, list):
        return set()
    return {
        str(p["verifier_id"])
        for p in prompts
        if isinstance(p, dict) and p.get("verifier_id")
    }


def apply_relay_verdicts(
    results: list[VerifierResult],
    verdicts: dict[str, str],
    relayed: set[str],
) -> list[VerifierResult]:
    """Write the verdicts into the relayed rows only; an unreadable verdict becomes a crashed row."""
    unexpected = sorted(set(verdicts) - relayed)
    if unexpected:
        raise ValueError(
            f"verdicts for verifiers that were not relayed: {unexpected[:5]}"
        )
    missing = sorted(relayed - set(verdicts))
    if missing:
        raise MissingRelayVerdict(f"no relayed verdict for verifiers: {missing[:5]}")
    out: list[VerifierResult] = []
    for r in results:
        if r.verifier_id not in relayed:
            out.append(r)
            continue
        try:
            parsed = parse_grading_response(verdicts[r.verifier_id])
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            out.append(
                r.model_copy(
                    update={
                        "score": 0.0,
                        "status": VerifierResultStatus.ERROR,
                        "message": f"unreadable relayed verdict: {exc}",
                        "verifier_result_values": {
                            **r.verifier_result_values,
                            CRASHED: True,
                        },
                    }
                )
            )
            continue
        out.append(
            r.model_copy(
                update={
                    "score": 1.0 if parsed.is_criteria_true else 0.0,
                    "verifier_result_values": {
                        **r.verifier_result_values,
                        "judge_grade": "pass" if parsed.is_criteria_true else "fail",
                        "grade_rationale": parsed.rationale,
                    },
                }
            )
        )
    return out


async def _score(
    results: list[VerifierResult],
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    scoring_config: ScoringConfig,
) -> tuple[GradingRunStatus, ScoringMethodResult]:
    """The world's scoring method over the rows, the sequence runner.main runs inline."""
    defn = SCORING_METHOD_REGISTRY[ScoringMethodIds(scoring_config.scoring_defn_id)]
    if defn.scoring_method_impl is None:
        raise ValueError(
            f"Scoring method {scoring_config.scoring_defn_id} has no implementation"
        )
    scored_results, scored_verifiers = exclude_skipped_from_scoring(results, verifiers)
    scored_results, scored_verifiers = exclude_agentic_from_scoring(
        scored_results, scored_verifiers, eval_configs
    )
    async with phase(
        "scoring_method",
        prefix=GRADING_PREFIX,
        tags=["relay_score:1", f"scoring_defn:{scoring_config.scoring_defn_id}"],
    ):
        scoring_results = await defn.score(
            scored_results,
            scored_verifiers,
            scoring_config.scoring_config_values,
            reporting_results=results,
            reporting_verifiers=verifiers,
        )
    # A crashed row discards the number, as in runner.main.
    if any(r.verifier_result_values.get(CRASHED) for r in results):
        return GradingRunStatus.ERROR, ScoringMethodResult(
            scoring_method_result_values={
                "error": "A verifier crashed; the score is not computable."
            },
            final_score=0.0,
        )
    return GradingRunStatus.COMPLETED, scoring_results


async def score_from_rows(
    *,
    grading_run_id: str,
    collected: dict[str, Any],
    verdicts: dict[str, str],
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    scoring_config: ScoringConfig,
) -> tuple[str, GradingRunStatus, list[VerifierResult], ScoringMethodResult]:
    """Score the collect pass's rows with the verdicts; a failure is an ERROR status, never a raise."""
    results: list[VerifierResult] = []
    try:
        # An empty task set scores a perfect 1.0, so missing rows are refused, not scored.
        rows = collected.get("verifier_results")
        if not isinstance(rows, list) or not rows:
            raise ValueError(
                "the collect pass carries no verifier_results; nothing to score"
            )
        results = TypeAdapter(list[VerifierResult]).validate_python(rows)
        configured = sorted(v.verifier_id for v in verifiers)
        present = sorted(r.verifier_id for r in results)
        if present != configured:
            raise ValueError(
                "the collect pass's rows do not match the configured verifiers: "
                f"missing={sorted(set(configured) - set(present))[:5]} "
                f"extra={sorted(set(present) - set(configured))[:5]}"
            )
        results = apply_relay_verdicts(
            results, verdicts, relayed_verifier_ids(collected)
        )
        status, scoring_results = await _score(
            results, verifiers, eval_configs, scoring_config
        )
    except Exception as e:
        error_message = format_exception_for_result(e)
        logger.error(
            f"[GRADING][ERROR] Error scoring grading run {grading_run_id} from rows: "
            f"{error_message}"
        )
        return (
            grading_run_id,
            GradingRunStatus.ERROR,
            results,
            ScoringMethodResult(
                scoring_method_result_values={"error": error_message}, final_score=0.0
            ),
        )
    return grading_run_id, status, results, scoring_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score a relayed grade from its rows")
    parser.add_argument("--grading-run-id", type=str, required=True)
    parser.add_argument(
        "--score-from", type=str, required=True, help="Collect pass output JSON"
    )
    parser.add_argument(
        "--relay-verdicts",
        type=str,
        required=True,
        help="JSON verifier_id -> completion",
    )
    parser.add_argument("--verifiers", type=str, required=True)
    parser.add_argument("--eval-configs", type=str, required=True)
    parser.add_argument("--scoring-config", type=str, required=True)
    parser.add_argument("--output", type=str, required=True)
    # Accepted so both passes take the same arguments; nothing here reads them.
    for unused in (
        "--trajectory-id",
        "--grading-settings",
        "--account-id",
        "--actor-user-id",
        "--trajectory-batch-id",
    ):
        parser.add_argument(unused, type=str, default="")
    args = parser.parse_args()

    with open(args.score_from) as f:
        collected = json.load(f)
    if not isinstance(collected, dict):
        raise ValueError("--score-from must be a JSON object")
    with open(args.relay_verdicts) as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("--relay-verdicts must be a JSON object")
    with open(args.verifiers) as f:
        verifiers = TypeAdapter(list[Verifier]).validate_json(f.read())
    with open(args.eval_configs) as f:
        eval_configs = TypeAdapter(list[EvalConfig]).validate_json(f.read())
    with open(args.scoring_config) as f:
        scoring_config = ScoringConfig.model_validate_json(f.read())

    grading_run_id, status, results, scoring_results = asyncio.run(
        score_from_rows(
            grading_run_id=args.grading_run_id,
            collected=collected,
            verdicts={str(k): str(v) for k, v in raw.items()},
            verifiers=verifiers,
            eval_configs=eval_configs,
            scoring_config=scoring_config,
        )
    )
    output: dict[str, Any] = {
        "grading_run_id": grading_run_id,
        "grading_run_status": status,
        "verifier_results": [v.model_dump(mode="json") for v in results],
        "scoring_results": scoring_results.model_dump(mode="json"),
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
