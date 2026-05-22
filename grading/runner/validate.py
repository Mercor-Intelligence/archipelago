"""
Validation runner: runs verifiers against a golden end state snapshot.

Simplified version of runner/main.py that:
- Uses the golden end state as the "final" snapshot
- Creates an empty AgentTrajectoryOutput (no trajectory data)
- Skips verifiers whose eval requires trajectory-dependent helpers (e.g. FINAL_ANSWER)
- Returns VerifierResult list directly (no scoring)
"""

import asyncio
import io
from typing import Any

from loguru import logger

from runner.concurrency import _get_eval_semaphore, _get_global_semaphore
from runner.evals.models import EvalConfig, EvalImplInput
from runner.evals.registry import EVAL_REGISTRY
from runner.helpers.models import HelperIds
from runner.helpers_shared import (
    build_parser_config_kwargs,
    collect_helpers,
    execute_helpers,
)
from runner.models import (
    AgentStatus,
    AgentTrajectoryOutput,
    GradingSettings,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)
from runner.utils.dependency_levels import group_by_dependency_level

# Helpers that require actual trajectory data and cannot work with an empty trajectory.
# Verifiers depending on these helpers are auto-skipped with NEUTRAL status.
TRAJECTORY_DEPENDENT_HELPERS: set[HelperIds] = {
    HelperIds.FINAL_ANSWER,
    HelperIds.IF_JUDGE_RESULT,
    HelperIds.IF_SYSTEM_STEER_JUDGE_RESULT,
    HelperIds.PLAYWRIGHT_TRACE_PARSER,
    HelperIds.BROWSER_STATE,
}


def _make_empty_trajectory() -> AgentTrajectoryOutput:
    """Create a minimal empty trajectory for validation purposes."""
    return AgentTrajectoryOutput(
        messages=[],
        output=None,
        status=AgentStatus.COMPLETED,
        time_elapsed=0.0,
    )


def _verifier_requires_trajectory(
    verifier: Verifier,
    eval_configs: list[EvalConfig],
    skip_trajectory_verifiers: bool,
) -> bool:
    """Check if a verifier depends on trajectory-specific helpers."""
    if not skip_trajectory_verifiers:
        return False

    eval_config = next(
        (e for e in eval_configs if e.eval_config_id == verifier.eval_config_id),
        None,
    )
    if eval_config is None:
        return False

    eval_defn = EVAL_REGISTRY.get(eval_config.eval_defn_id)
    if eval_defn is None:
        return False

    return any(h in TRAJECTORY_DEPENDENT_HELPERS for h in eval_defn.helper_dependencies)


async def validate(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    grading_settings: GradingSettings,
    golden_snapshots: list[io.BytesIO] | None = None,
    skip_trajectory_verifiers: bool = True,
) -> list[VerifierResult]:
    """
    Run verifiers against snapshots for validation (no trajectory needed).

    Args:
        initial_snapshot_bytes: World + task initial snapshot
        final_snapshot_bytes: Golden end state snapshot (used as "final")
        verifiers: Verifiers to evaluate
        eval_configs: Eval configurations
        grading_settings: LLM judge model settings
        golden_snapshots: Optional golden response file snapshots
        skip_trajectory_verifiers: If True, skip verifiers needing trajectory helpers

    Returns:
        List of VerifierResult for each verifier
    """
    trajectory = _make_empty_trajectory()
    verifier_results: dict[str, VerifierResult] = {}

    # Partition verifiers into runnable vs skipped
    runnable_verifiers: list[Verifier] = []
    skipped_verifier_ids: set[str] = set()

    for v in verifiers:
        if _verifier_requires_trajectory(v, eval_configs, skip_trajectory_verifiers):
            skipped_verifier_ids.add(v.verifier_id)
            verifier_results[v.verifier_id] = VerifierResult(
                verifier_id=v.verifier_id,
                verifier_version=v.verifier_version,
                score=0.0,
                verifier_result_values={
                    "skipped": True,
                    "reason": "Requires trajectory data (skipped during validation)",
                },
                status=VerifierResultStatus.OK,
                message="Skipped: verifier requires trajectory data",
            )
        else:
            runnable_verifiers.append(v)

    if skipped_verifier_ids:
        logger.info(
            f"[VALIDATE] Skipping {len(skipped_verifier_ids)} trajectory-dependent verifiers"
        )

    # Multi-pass: skip verifiers whose dependencies were skipped (handles transitive dependencies)
    final_runnable: list[Verifier] = []
    changed = True
    while changed:
        changed = False
        final_runnable = []
        for v in runnable_verifiers:
            if v.verifier_id in skipped_verifier_ids:
                continue
            deps = v.verifier_dependencies or []
            if any(dep_id in skipped_verifier_ids for dep_id in deps):
                skipped_verifier_ids.add(v.verifier_id)
                verifier_results[v.verifier_id] = VerifierResult(
                    verifier_id=v.verifier_id,
                    verifier_version=v.verifier_version,
                    score=0.0,
                    verifier_result_values={
                        "skipped": True,
                        "reason": "Dependency was skipped during validation",
                    },
                    status=VerifierResultStatus.OK,
                    message="Skipped: dependency requires trajectory data",
                )
                changed = True
            else:
                final_runnable.append(v)

    # Collect helpers and build kwargs
    helpers = collect_helpers(final_runnable, eval_configs)
    helper_kwargs = build_parser_config_kwargs(final_runnable, eval_configs)

    # Execute helpers
    helper_results = await execute_helpers(
        helpers,
        helper_kwargs,
        initial_snapshot_bytes,
        final_snapshot_bytes,
        trajectory,
        final_runnable,
        eval_configs,
        grading_settings,
    )

    # Group and execute verifiers by dependency level
    levels = group_by_dependency_level(final_runnable)

    logger.info(
        f"[VALIDATE][START] Executing: verifiers={len(final_runnable)} | "
        f"skipped={len(skipped_verifier_ids)} | dependency_levels={len(levels)}"
    )

    for _level_idx, level_verifiers in enumerate(levels):
        tasks = []
        for verifier in level_verifiers:
            eval_config = next(
                (
                    e
                    for e in eval_configs
                    if e.eval_config_id == verifier.eval_config_id
                ),
                None,
            )
            if eval_config is None:
                verifier_results[verifier.verifier_id] = VerifierResult(
                    verifier_id=verifier.verifier_id,
                    verifier_version=verifier.verifier_version,
                    score=0.0,
                    verifier_result_values={"error": "No eval config found"},
                    status=VerifierResultStatus.ERROR,
                    message=f"No eval config for eval_config_id={verifier.eval_config_id}",
                )
                continue

            eval_defn = EVAL_REGISTRY.get(eval_config.eval_defn_id)
            if eval_defn is None or eval_defn.eval_impl is None:
                verifier_results[verifier.verifier_id] = VerifierResult(
                    verifier_id=verifier.verifier_id,
                    verifier_version=verifier.verifier_version,
                    score=0.0,
                    verifier_result_values={"error": "No eval implementation found"},
                    status=VerifierResultStatus.ERROR,
                    message=f"No eval impl for {eval_config.eval_defn_id}",
                )
                continue

            async def _run_verifier(
                v: Verifier = verifier,
                ec: EvalConfig = eval_config,
                ed: Any = eval_defn,
            ) -> tuple[str, VerifierResult]:
                eval_impl = ed.eval_impl
                global_sem = _get_global_semaphore()

                # Acquire eval-specific semaphore first, then global semaphore
                # This prevents rate-limited verifiers from holding global slots
                if ed.max_concurrency is not None:
                    eval_sem = _get_eval_semaphore(
                        str(ec.eval_defn_id), ed.max_concurrency
                    )
                    async with eval_sem:
                        async with global_sem:
                            result = await eval_impl(
                                EvalImplInput(
                                    initial_snapshot_bytes=initial_snapshot_bytes,
                                    final_snapshot_bytes=final_snapshot_bytes,
                                    golden_snapshots=golden_snapshots or [],
                                    trajectory=trajectory,
                                    grading_settings=grading_settings,
                                    verifier=v,
                                    eval_config=ec,
                                    dependencies=[
                                        verifier_results[dep_id]
                                        for dep_id in v.verifier_dependencies or []
                                    ],
                                    helper_results={
                                        h_id: helper_results[h_id]
                                        for h_id in ed.helper_dependencies
                                    },
                                )
                            )
                else:
                    async with global_sem:
                        result = await eval_impl(
                            EvalImplInput(
                                initial_snapshot_bytes=initial_snapshot_bytes,
                                final_snapshot_bytes=final_snapshot_bytes,
                                golden_snapshots=golden_snapshots or [],
                                trajectory=trajectory,
                                grading_settings=grading_settings,
                                verifier=v,
                                eval_config=ec,
                                dependencies=[
                                    verifier_results[dep_id]
                                    for dep_id in v.verifier_dependencies or []
                                ],
                                helper_results={
                                    h_id: helper_results[h_id]
                                    for h_id in ed.helper_dependencies
                                },
                            )
                        )
                return v.verifier_id, result

            tasks.append(_run_verifier())

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.error(f"[VALIDATE][ERROR] Verifier error: {repr(result)}")
                    raise result
                vid, vresult = result
                verifier_results[vid] = vresult

    # Return results in original verifier order
    return [verifier_results[v.verifier_id] for v in verifiers]
