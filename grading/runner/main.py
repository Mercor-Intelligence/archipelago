import argparse
import asyncio
import io
import json
from collections.abc import Coroutine, Iterable, Sequence
from typing import IO, Any

from loguru import logger
from pydantic import TypeAdapter

from runner.concurrency import _get_eval_semaphore, _get_global_semaphore
from runner.evals.models import EvalConfig, EvalImplInput
from runner.helpers.db_diff.main import cleanup_source_files
from runner.helpers.models import HelperIds
from runner.helpers_shared import (
    build_parser_config_kwargs,
    collect_helpers,
    execute_helpers,
    verifier_helper_ids,
)
from runner.models import (
    AgentTrajectoryOutput,
    GradingRunStatus,
    GradingSettings,
    ScoringMethodResult,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)
from runner.scoring_methods.models import ScoringConfig, ScoringMethodIds
from runner.skip_rules import (
    exclude_agentic_from_scoring,
    exclude_skipped_from_scoring,
    is_external_artifact_import,
    partition_verifiers_by_filter,
    partition_verifiers_for_artifact_import,
    partition_verifiers_for_no_transcript,
)
from runner.utils.llm import prefix_cache_enabled
from runner.utils.metrics import distribution, increment, phase
from runner.utils.settings import get_settings

from .evals.registry import EVAL_REGISTRY
from .scoring_methods.registry import SCORING_METHOD_REGISTRY
from .utils.dependency_levels import group_by_dependency_level
from .utils.errors import format_exception_for_result
from .utils.grading_log import logger as grading_logger
from .utils.llm import grading_context

# from .save.main import save

GRADING_PREFIX = "studio.grading"


settings = get_settings()


async def evaluate_verifier(
    verifier: Verifier,
    verifier_results: dict[str, VerifierResult],
    eval_configs: list[EvalConfig],
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
    grading_settings: GradingSettings,
    helper_results: dict[HelperIds, Any],
    golden_snapshots: list[IO[bytes]] | None = None,
    grading_run_args: dict[str, Any] | None = None,
) -> VerifierResult:
    """
    Evaluate a single verifier and return its result.

    Args:
        verifier: The verifier to evaluate
        verifier_results: Dict of already-completed verifier results (for dependencies)
        eval_configs: List of eval configurations
        initial_snapshot_bytes: Initial snapshot
        final_snapshot_bytes: Final snapshot
        trajectory: Agent trajectory
        grading_settings: Grading settings
        helper_results: Results from helper evaluations

    Returns:
        VerifierResult for this verifier

    Raises:
        ValueError: If eval config or definition not found
        Exception: If evaluation fails
    """
    eval_config = next(
        (e for e in eval_configs if e.eval_config_id == verifier.eval_config_id),
        None,
    )
    if eval_config is None:
        if not verifier.eval_config_id:
            raise ValueError(
                f"No eval config id set on verifier {verifier.verifier_id}. The verifier row is malformed: delete it, or point it at an eval config defined on the world."
            )
        raise ValueError(
            f"No eval config found for verifier {verifier.verifier_id}: eval config {verifier.eval_config_id} is not defined on this world. The verifier is orphaned, which happens when an eval config is deleted while verifier rows still reference it. Delete the orphaned verifier, or restore the eval config. Regenerating the trajectory will not fix this."
        )

    eval_defn = EVAL_REGISTRY.get(eval_config.eval_defn_id)

    if eval_defn is None:
        raise ValueError(
            f"No eval definition found for eval config {eval_config.eval_config_id}"
        )

    if eval_defn.eval_impl is None:
        raise ValueError(
            f"Eval {eval_defn.eval_id} has no implementation (server-side schema only)"
        )

    # Capture eval_impl for type narrowing inside nested function
    eval_impl = eval_defn.eval_impl

    async def _run_eval() -> VerifierResult:
        return await eval_impl(
            EvalImplInput(
                initial_snapshot_bytes=initial_snapshot_bytes,
                final_snapshot_bytes=final_snapshot_bytes,
                golden_snapshots=golden_snapshots or [],
                trajectory=trajectory,
                grading_settings=grading_settings,
                verifier=verifier,
                eval_config=eval_config,
                dependencies=[
                    verifier_results[dep_id]
                    for dep_id in verifier.verifier_dependencies or []
                ],
                helper_results={
                    helper_id: helper_results[helper_id]
                    for helper_id in verifier_helper_ids(verifier, eval_defn)
                    if helper_id in helper_results
                },
                grading_run_args=grading_run_args,
            )
        )

    # eval_defn is the bounded verifier type; verifier_id is per-row and unbounded.
    verifier_tags = [f"eval_defn:{eval_defn.eval_id}"]

    try:
        # Acquire semaphores in correct order to avoid blocking unrelated
        # verifiers: eval-specific FIRST (if applicable) to queue this eval
        # type, then global to enforce total concurrency. This prevents
        # CODE_EXECUTION verifiers from holding global slots while waiting.
        # `phase("verifier", ...)` sits *inside* both semaphores so the metric
        # measures verifier execution only, not queue wait under contention.
        global_sem = _get_global_semaphore()

        if eval_defn.max_concurrency is not None:
            eval_sem = _get_eval_semaphore(eval_defn.eval_id, eval_defn.max_concurrency)
            async with eval_sem:
                async with global_sem:
                    async with phase(
                        "verifier", prefix=GRADING_PREFIX, tags=verifier_tags
                    ):
                        result = await _run_eval()
        else:
            async with global_sem:
                async with phase("verifier", prefix=GRADING_PREFIX, tags=verifier_tags):
                    result = await _run_eval()

        # `passed` derives from the numeric score; `status` reflects whether
        # the verifier ran cleanly. Both are useful — `passed` for criterion
        # outcome, `status` for verifier-impl health.
        increment(
            "studio.grading.verifier_results",
            tags=verifier_tags
            + [
                f"passed:{result.score > 0}",
                f"status:{result.status.value}",
            ],
        )
        return result
    except Exception as e:
        logger.error(
            f"[GRADING][ERROR] Error executing verifier {verifier.verifier_id} | error={repr(e)}"
        )
        raise e


# Distinguishes a raise from a verifier that reports ERROR itself. Scoring
# methods like `swe_atlas_all_pass` exclude ERROR rows and score what is left,
# which would let a crashed criterion shrink the denominator.
_CRASHED = "verifier_crashed"


def any_verifier_crashed(results: Iterable[VerifierResult]) -> bool:
    """Whether any result came from a raised exception rather than a verdict."""
    return any(r.verifier_result_values.get(_CRASHED) for r in results)


def _should_prime_prefix_cache(fanout: int) -> bool:
    """Whether to run this level's first verifier alone to warm the cache.

    A level's verifiers grade the same deliverable, so their prompts share a
    long identical prefix. Fired together, the first ``LLM_CONCURRENCY_LIMIT``
    of them all look for a cache entry that nothing has written yet, so each
    pays the 1.25x write premium and none gets the 0.1x read. Measured on dev
    (grading run ``gr_8a8a5fe8``): writes and reads came back ~1:1 instead of
    1:N-1. Letting one finish first turns the rest into reads.

    The cost is wall clock: one extra wave, i.e. one verifier's latency. That
    is a fixed cost against a saving proportional to the fan-out, so it only
    pays on large levels -- hence a threshold rather than always-on. Off by
    default (0), because the right threshold depends on how much latency the
    caller will trade for spend.
    """
    minimum = settings.GRADING_PROMPT_CACHE_PRIME_MIN_FANOUT
    if minimum <= 1 or fanout < minimum:
        return False
    # Nothing to warm when the prefix is not marked cacheable in the first
    # place; serialising a verifier would buy latency and no discount.
    return prefix_cache_enabled()


def _fanout_bucket(fanout: int) -> str:
    """A bounded label for a level's size, for tagging without cardinality."""
    for edge in (10, 20, 50, 100, 200):
        if fanout <= edge:
            return f"<={edge}"
    return ">200"


async def settle_level(
    level_verifiers: Sequence[Verifier],
    tasks: Sequence[Coroutine[Any, Any, VerifierResult]],
) -> list[VerifierResult]:
    """Run one dependency level, one result per verifier, never fewer.

    `return_exceptions=True` turns a raise into an errored row for that
    verifier alone, so its siblings keep the results they already produced.

    When `_should_prime_prefix_cache` says so, the first verifier runs to
    completion before the rest start. Ordering only -- every verifier still
    runs exactly once and a raise still lands on its own row.
    """
    tasks = list(tasks)
    settled: list[Any]
    if _should_prime_prefix_cache(len(tasks)):
        # Bucketed, not the raw count: a per-value tag on an unbounded integer
        # is exactly the cardinality trap `verifier_tags` avoids above.
        increment(
            "studio.grading.prefix_cache_primed",
            tags=[f"fanout_bucket:{_fanout_bucket(len(tasks))}"],
        )
        # gather() rather than await, so a raised primer becomes an errored row
        # for that verifier instead of skipping the level's remaining work.
        primer = await asyncio.gather(tasks[0], return_exceptions=True)
        remainder = (
            await asyncio.gather(*tasks[1:], return_exceptions=True)
            if len(tasks) > 1
            else []
        )
        settled = [*primer, *remainder]
    else:
        settled = list(await asyncio.gather(*tasks, return_exceptions=True))
    return [
        _errored_result(verifier, outcome)
        if isinstance(outcome, BaseException)
        else outcome
        for verifier, outcome in zip(level_verifiers, settled, strict=True)
    ]


def _errored_result(verifier: Verifier, exc: BaseException) -> VerifierResult:
    """A raised verifier as a result row, so its siblings keep theirs.

    `BaseException` because `gather` returns whatever was raised. A
    cancellation is re-raised: the run is being torn down, not judged.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    logger.error(
        f"[GRADING][ERROR] Verifier {verifier.verifier_id} raised | error={exc!r}"
    )
    return VerifierResult(
        verifier_id=verifier.verifier_id,
        verifier_version=verifier.verifier_version,
        # Placeholder. status=ERROR is the signal; never read this number.
        score=0.0,
        status=VerifierResultStatus.ERROR,
        message=str(exc),
        verifier_result_values={_CRASHED: True},
    )


async def main(
    grading_run_id: str,
    trajectory_id: str,
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
    grading_settings: GradingSettings,
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    scoring_config: ScoringConfig,
    golden_snapshots: list[IO[bytes]] | None = None,
    skip_trajectory_verifiers: bool = False,
    grading_run_args: dict[str, Any] | None = None,
):
    # Per-run ids stay out of metric tags; logs and traces carry them.
    base_tags: list[str] = []
    # Set grading_run_id in context for all downstream LLM calls
    with grading_context(grading_run_id):
        grading_logger.bind(
            message_type="grading_start",
            payload={"verifier_count": len(verifiers)},
        ).info("Grading run started")
        # Declared before the try so exception handlers can persist whatever
        # verdicts were collected before the failure.
        verifier_results: dict[str, VerifierResult] = {}
        helper_results: dict[HelperIds, Any] = {}
        try:
            logger.info(f"[GRADING][PREP][START] Preparing: verifiers={len(verifiers)}")

            # Checked before skip_trajectory_verifiers: the server still sends True
            # for every import, so the flag would swallow this branch (RLS-10185).
            if is_external_artifact_import(trajectory):
                if trajectory.messages:
                    runnable_verifiers, skipped_results = (
                        partition_verifiers_for_artifact_import(
                            verifiers,
                            eval_configs,
                            reason="external artifact (no agent run)",
                            dependency_reason="external artifact (no agent run)",
                            message="Skipped: verifier requires data no agent run produced",
                            dependency_message="Skipped: dependency requires data no agent run produced",
                        )
                    )
                    skip_kind = "needing agent-run-only data (external artifact)"
                else:
                    runnable_verifiers, skipped_results = (
                        partition_verifiers_for_no_transcript(
                            verifiers,
                            eval_configs,
                            reason="no transcript (external artifact)",
                            dependency_reason="no transcript (external artifact)",
                            message="Skipped: verifier requires trajectory data",
                            dependency_message="Skipped: dependency requires trajectory data",
                        )
                    )
                    skip_kind = "transcript-dependent verifiers (no transcript)"
                verifier_results.update(skipped_results)
                if skipped_results:
                    logger.info(
                        f"[GRADING][SKIP] Skipping {len(skipped_results)} {skip_kind}"
                    )
            elif skip_trajectory_verifiers:
                runnable_verifiers, skipped_results = (
                    partition_verifiers_for_no_transcript(
                        verifiers,
                        eval_configs,
                        reason="no transcript (external artifact)",
                        dependency_reason="no transcript (external artifact)",
                        message="Skipped: verifier requires trajectory data",
                        dependency_message="Skipped: dependency requires trajectory data",
                    )
                )
                verifier_results.update(skipped_results)
                if skipped_results:
                    logger.info(
                        f"[GRADING][SKIP] Skipping {len(skipped_results)} "
                        "transcript-dependent verifiers (no transcript)"
                    )
            else:
                runnable_verifiers = list(verifiers)

            # Optional verifier-TYPE filter (grading_run_args.verifier_filter): grade only
            # deterministic (non-LLM) or only LLM verifiers. Absent / "all" is a no-op, so
            # grading is unchanged unless a run opts in. Applied here (after the transcript
            # partition, before helpers/levels) so filtered verifiers also skip helper
            # collection and execution.
            runnable_verifiers, type_filter_skipped = partition_verifiers_by_filter(
                runnable_verifiers,
                eval_configs,
                (grading_run_args or {}).get("verifier_filter"),
            )
            if type_filter_skipped:
                verifier_results.update(type_filter_skipped)
                logger.info(
                    f"[GRADING][SKIP] Skipping {len(type_filter_skipped)} verifiers by "
                    f"verifier_filter={(grading_run_args or {}).get('verifier_filter')}"
                )

            # Collect helpers and build kwargs
            helpers = collect_helpers(runnable_verifiers, eval_configs)
            helper_kwargs = build_parser_config_kwargs(runnable_verifiers, eval_configs)

            helper_names = [helper_id.value for helper_id in helpers]
            logger.info(f"[GRADING][HELPERS][START] Executing: helpers={helper_names}")

            # Execute helpers
            async with phase(
                "helpers",
                prefix=GRADING_PREFIX,
                tags=base_tags + [f"helper_count:{len(helpers)}"],
            ):
                helper_results = await execute_helpers(
                    helpers,
                    helper_kwargs,
                    initial_snapshot_bytes,
                    final_snapshot_bytes,
                    trajectory,
                    runnable_verifiers,
                    eval_configs,
                    grading_settings,
                )
            helper_result_names = [helper_id.value for helper_id in helper_results]
            logger.info(
                f"[GRADING][HELPERS][END] Completed: helpers={helper_result_names}"
            )

            # Group verifiers into dependency levels for parallel execution
            levels = group_by_dependency_level(runnable_verifiers)
            distribution(
                "studio.grading.dependency_levels",
                float(len(levels)),
                tags=base_tags,
            )

            logger.info(
                f"[GRADING][START] Executing: verifiers={len(runnable_verifiers)} | dependency_levels={len(levels)}"
            )

            # Execute each level in sequence; verifiers within a level run in
            # parallel. A verifier that raises becomes an errored result for
            # that verifier alone: `return_exceptions=True` keeps its siblings'
            # results, which the scoring layer already knows how to refuse to
            # score. One verifier pointing at a deleted eval config used to
            # discard a whole run's work, 43 results at a time.
            for level_idx, level_verifiers in enumerate(levels):
                async with phase(
                    "dependency_level",
                    prefix=GRADING_PREFIX,
                    tags=base_tags + [f"level:{level_idx}"],
                ):
                    tasks = [
                        evaluate_verifier(
                            verifier=verifier,
                            verifier_results=verifier_results,
                            eval_configs=eval_configs,
                            initial_snapshot_bytes=initial_snapshot_bytes,
                            final_snapshot_bytes=final_snapshot_bytes,
                            trajectory=trajectory,
                            grading_settings=grading_settings,
                            helper_results=helper_results,
                            golden_snapshots=golden_snapshots,
                            grading_run_args=grading_run_args,
                        )
                        for verifier in level_verifiers
                    ]
                    results = await settle_level(level_verifiers, tasks)

                # Store results for next level's dependencies
                for verifier, result in zip(level_verifiers, results, strict=True):
                    verifier_results[verifier.verifier_id] = result

            verifier_results_list = list(verifier_results.values())

            scoring_method_defn = SCORING_METHOD_REGISTRY[
                ScoringMethodIds(scoring_config.scoring_defn_id)
            ]
            if scoring_method_defn.scoring_method_impl is None:
                raise ValueError(
                    f"Scoring method {scoring_config.scoring_defn_id} has no implementation"
                )

            # Exclude skipped (no-transcript) verifiers from scoring so their
            # NEUTRAL 0.0 results don't deflate the score (kept in
            # verifier_results_list so they still surface in judge_grades).
            # Single-sourced with the filtered-recompute path
            # (modal_labs.run_scoring) — see
            # exclude_skipped_from_scoring in runner/skip_rules.py for the full
            # rationale, including the gate/critical-value semantics.
            scored_results, scored_verifiers = exclude_skipped_from_scoring(
                verifier_results_list, verifiers
            )
            # Agentic verifiers grade the whole task and are reported, not
            # scored, so keep them out of the weighted average regardless of any
            # weight left on the verifier row.
            scored_results, scored_verifiers = exclude_agentic_from_scoring(
                scored_results, scored_verifiers, eval_configs
            )

            async with phase(
                "scoring_method",
                prefix=GRADING_PREFIX,
                tags=base_tags + [f"scoring_defn:{scoring_config.scoring_defn_id}"],
            ):
                scoring_results = await scoring_method_defn.scoring_method_impl(
                    scored_results,
                    scored_verifiers,  # task_id, is_primary_objective, etc.
                    scoring_config.scoring_config_values,
                )
            # Scoring runs first so every verifier's work is stored, then a
            # crash discards the number. A scoring method that excludes ERROR
            # rows would otherwise publish a score computed from whichever
            # criteria survived, and a consumer reading `final_score` without
            # checking status would take it at face value. Same shape as the
            # `except` paths below, which is what a crash produced before it
            # was isolated into a row.
            if any_verifier_crashed(verifier_results_list):
                logger.error(
                    f"[GRADING][ERROR] Verifier crash in grading run "
                    f"{grading_run_id}; grades stored, score discarded"
                )
                scoring_results = ScoringMethodResult(
                    scoring_method_result_values={
                        "error": "A verifier crashed; the score is not computable."
                    },
                    final_score=0.0,
                )
                grading_run_status = GradingRunStatus.ERROR
            else:
                grading_run_status = GradingRunStatus.COMPLETED

        except TimeoutError:
            logger.error(
                f"[GRADING][TIMEOUT] Timeout error grading run {grading_run_id}"
            )

            verifier_results_list = []
            scoring_results = ScoringMethodResult(
                scoring_method_result_values={"error": "Grading timeout exceeded"},
                final_score=0.0,
            )

            grading_run_status = GradingRunStatus.CANCELLED

        except asyncio.CancelledError:
            logger.error(
                f"[GRADING][CANCELLED] Grading run {grading_run_id} was cancelled"
            )

            verifier_results_list = []
            scoring_results = ScoringMethodResult(
                scoring_method_result_values={"error": "Grading was cancelled"},
                final_score=0.0,
            )

            grading_run_status = GradingRunStatus.CANCELLED

        except Exception as e:
            error_message = format_exception_for_result(e)
            logger.error(
                f"[GRADING][ERROR] Error scoring grading run {grading_run_id}: {error_message}"
            )

            # Persist whatever verdicts WERE collected before the failure.
            # Previously this was reset to [], so a scoring failure (e.g. 2 of
            # 62 judges erroring) silently dropped all 60 successful verdicts
            # and the UI showed every criterion as "Pending" forever, with no
            # visible reason. Keeping the partial results lets the UI render
            # per-criterion verdicts (including the errored ones, which carry
            # their failure message) alongside the run-level error.
            verifier_results_list = list(verifier_results.values())
            scoring_results = ScoringMethodResult(
                scoring_method_result_values={"error": error_message},
                final_score=0.0,
            )

            grading_run_status = GradingRunStatus.ERROR

        finally:
            # Retained diff source files (search_rows backends) are only needed
            # while verifiers run; delete them now instead of waiting for the
            # next run's age sweep, so disk on a warm grading container is
            # bounded by one run's snapshots.
            cleanup_source_files(helper_results.get(HelperIds.DB_DIFF))

        logger.info(
            f"[GRADING][END] Finished grading run {grading_run_id}: "
            f"status={grading_run_status.value}, "
            f"verifier_results={len(verifier_results_list)}, "
            f"final_score={scoring_results.final_score}"
        )

        grading_logger.bind(
            message_type="grading_end",
            payload={
                "status": grading_run_status.value,
                "verifier_result_count": len(verifier_results_list),
                "final_score": scoring_results.final_score,
            },
        ).info("Grading run finished")

        # await save(
        #     grading_run_id, grading_run_status, verifier_results_list, scoring_results
        # )

        return (
            grading_run_id,
            grading_run_status,
            verifier_results_list,
            scoring_results,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run grading runner")
    parser.add_argument("--grading-run-id", type=str, required=True)
    parser.add_argument("--trajectory-id", type=str, required=True)
    parser.add_argument("--initial-snapshot", type=str, required=True)
    parser.add_argument("--final-snapshot", type=str, required=True)
    parser.add_argument("--trajectory", type=str, required=True)
    parser.add_argument("--grading-settings", type=str, required=True)
    parser.add_argument("--verifiers", type=str, required=True)
    parser.add_argument("--eval-configs", type=str, required=True)
    parser.add_argument("--scoring-config", type=str, required=True)
    parser.add_argument(
        "--golden-snapshot",
        type=str,
        action="append",
        dest="golden_snapshots",
        help="Path to golden response snapshot zip (optional, can be repeated for multiple golden states)",
    )
    parser.add_argument("--output", type=str, help="Path to save the output JSON")

    args = parser.parse_args()

    with open(args.initial_snapshot, "rb") as f:
        initial_snapshot_bytes = io.BytesIO(f.read())

    with open(args.final_snapshot, "rb") as f:
        final_snapshot_bytes = io.BytesIO(f.read())

    # Load golden snapshots (supports multiple for tasks with multiple valid end states)
    golden_snapshots: list[IO[bytes]] = []
    if args.golden_snapshots:
        for path in args.golden_snapshots:
            with open(path, "rb") as f:
                golden_snapshots.append(io.BytesIO(f.read()))

    with open(args.trajectory) as f:
        # Use model_validate(json.loads(...)) instead of model_validate_json(...)
        # because of a Pydantic quirk with str | Iterable unions. model_validate_json
        # incorrectly iterates over strings as Iterable, causing ValidatorIterator
        # issues downstream. See https://github.com/pydantic/pydantic/issues/9541
        trajectory = AgentTrajectoryOutput.model_validate(json.loads(f.read()))

    with open(args.grading_settings) as f:
        grading_settings = GradingSettings.model_validate_json(f.read())

    with open(args.verifiers) as f:
        verifiers = TypeAdapter(list[Verifier]).validate_json(f.read())

    with open(args.eval_configs) as f:
        eval_configs = TypeAdapter(list[EvalConfig]).validate_json(f.read())

    with open(args.scoring_config) as f:
        scoring_config = ScoringConfig.model_validate_json(f.read())

    result = asyncio.run(
        main(
            grading_run_id=args.grading_run_id,
            trajectory_id=args.trajectory_id,
            initial_snapshot_bytes=initial_snapshot_bytes,
            final_snapshot_bytes=final_snapshot_bytes,
            trajectory=trajectory,
            grading_settings=grading_settings,
            verifiers=verifiers,
            eval_configs=eval_configs,
            scoring_config=scoring_config,
            golden_snapshots=golden_snapshots,
        )
    )

    if args.output:
        (
            grading_run_id,
            grading_run_status,
            verifier_results,
            scoring_results,
        ) = result
        output = {
            "grading_run_id": grading_run_id,
            "grading_run_status": grading_run_status,
            "verifier_results": [v.model_dump(mode="json") for v in verifier_results],
            "scoring_results": scoring_results.model_dump(mode="json"),
        }
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
