"""
Shared rules for skipping verifiers that cannot be graded without a trajectory.

A trajectory with no transcript (e.g. an ``external_artifact`` upload that only
carries output files) has an empty ``trajectory.output``/``trajectory.messages``.
Two classes of verifiers are ungradeable in that situation:

1. Verifiers whose eval depends on a *trajectory-dependent helper* — these
   helpers parse the transcript / live agent state and produce nothing useful
   on an empty trajectory.
2. Verifiers whose eval grades *only* the transcript/output and reads no
   filesystem signal (``TRANSCRIPT_ONLY_EVALS``) — there is nothing to grade,
   and several of these eval impls RAISE on missing output keys.

File/artifact verifiers (which read the final snapshot filesystem) are NOT
skipped and still run + get scored.

This module is the single source of truth for both:
- ``validate.py`` (golden-end-state validation, no trajectory), and
- ``main.py``'s full scoring path when ``skip_trajectory_verifiers=True``.
"""

from runner.evals.models import EvalConfig, EvalIds
from runner.evals.registry import EVAL_REGISTRY, EvalDefn
from runner.helpers.models import HelperIds
from runner.models import Verifier, VerifierResult, VerifierResultStatus

# Helpers that require actual trajectory data and cannot work with an empty
# trajectory. Verifiers whose eval depends on any of these are auto-skipped.
TRAJECTORY_DEPENDENT_HELPERS: set[HelperIds] = {
    HelperIds.FINAL_ANSWER,
    HelperIds.IF_JUDGE_RESULT,
    HelperIds.IF_SYSTEM_STEER_JUDGE_RESULT,
    HelperIds.PLAYWRIGHT_TRACE_PARSER,
    HelperIds.BROWSER_STATE,
}

# Evals that grade ONLY the transcript/output and read NO filesystem signal, so
# they are ungradeable on a no-transcript trajectory.
#
# Inclusion criterion (verified by reading each eval's main.py): the eval reads
# only ``trajectory.output`` / ``trajectory.messages`` (directly or via a
# transcript-only helper) and never opens the snapshot filesystem. Candidates
# that read golden/initial/final snapshot bytes are EXCLUDED (they may still
# carry an artifact signal worth grading). Kept conservative; the crash-risk
# evals that RAISE on missing output keys (sparta_*) are always included.
TRANSCRIPT_ONLY_EVALS: set[EvalIds] = {
    EvalIds.CLI_VERIFIER,  # reads trajectory.output["cli_results"]; no fs
    EvalIds.SPARTA_MIRROR,  # reads trajectory.output["final_score"]; RAISES if missing
    EvalIds.SPARTA_AGENTIC_GRADING,  # reads trajectory.output keys; RAISES if missing
    EvalIds.MCQ_EXACT_MATCH,  # extracts final assistant response; no fs
    EvalIds.ACE_CRITERION_VERIFIER,  # reads trajectory.output["ace_grounding"]; no fs
    EvalIds.RESPONSE_TOOL_VERIFIER,  # reads trajectory.output directly; no fs
    EvalIds.MLEBENCH_RESULT,  # reads trajectory.output["grade"]; no fs
    EvalIds.USER_SIM_JUDGE,  # USER_SIM_JUDGE_RESULT helper reads trajectory.messages only
    EvalIds.HLE_JUDGE,  # extracts final assistant response; no fs
    EvalIds.MRCR_SIMILARITY,  # FINAL_ANSWER helper (transcript-only)
    EvalIds.TOOL_CALL_CHECK,  # reads trajectory.messages tool calls; no fs
    EvalIds.TOOL_CALL_LLM_CHECK,  # reads trajectory.messages tool calls; no fs
    EvalIds.POSTTRAINING_TOOL_CALL_CHECK,  # reads trajectory.messages tool calls; no fs
    EvalIds.INTERVIEW_FRAUD_DECISION_MATCH,  # extracts final assistant response; no fs
    # EXCLUDED: gdpval_judge (reads golden/initial snapshot filesystem),
    #           vca_behavior_llm_check (VCA_CONTEXT helper reads final snapshot fs)
}


def should_skip_verifier_without_transcript(
    eval_defn: EvalDefn,
    *,
    include_transcript_only_evals: bool = True,
) -> bool:
    """True if a verifier's eval is ungradeable without a transcript.

    ``include_transcript_only_evals`` controls whether the ``TRANSCRIPT_ONLY_EVALS``
    set participates in the decision. main()'s no-transcript path passes True so
    transcript-only evals (cli/mcq/hle/tool_call) are skipped on an external
    artifact. validate() passes False to keep its original behavior of skipping
    ONLY helper-dependent verifiers, so misconfigured transcript-only verifiers
    are still surfaced during golden validation.
    """
    if include_transcript_only_evals and eval_defn.eval_id in TRANSCRIPT_ONLY_EVALS:
        return True
    return bool(set(eval_defn.helper_dependencies) & TRAJECTORY_DEPENDENT_HELPERS)


def _resolve_eval_defn(
    verifier: Verifier,
    eval_configs: list[EvalConfig],
) -> EvalDefn | None:
    """Resolve verifier -> eval_config -> EVAL_REGISTRY entry (or None)."""
    eval_config = next(
        (e for e in eval_configs if e.eval_config_id == verifier.eval_config_id),
        None,
    )
    if eval_config is None:
        return None
    return EVAL_REGISTRY.get(eval_config.eval_defn_id)


def _skipped_result(verifier: Verifier, reason: str, message: str) -> VerifierResult:
    return VerifierResult(
        verifier_id=verifier.verifier_id,
        verifier_version=verifier.verifier_version,
        score=0.0,
        verifier_result_values={"skipped": True, "reason": reason},
        status=VerifierResultStatus.OK,
        message=message,
    )


def partition_verifiers_for_no_transcript(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    *,
    reason: str,
    dependency_reason: str,
    message: str,
    dependency_message: str,
    include_transcript_only_evals: bool = True,
) -> tuple[list[Verifier], dict[str, VerifierResult]]:
    """
    Split verifiers into runnable vs skipped for a no-transcript trajectory.

    A verifier is skipped if its eval is transcript-only (direct skip) or if any
    of its ``verifier_dependencies`` was skipped (transitive skip). Returns the
    runnable verifiers (original order) and a dict of NEUTRAL skipped
    VerifierResults keyed by verifier_id.

    ``include_transcript_only_evals`` is threaded through to
    ``should_skip_verifier_without_transcript``: True for main()'s external
    artifact path, False for validate() (helper-only, original behavior).
    """
    verifier_results: dict[str, VerifierResult] = {}
    skipped_ids: set[str] = set()

    # Pass 1: direct skips based on the eval's transcript dependence.
    #
    # An unresolved eval_defn (None — missing/typo'd eval_config_id, or an
    # eval_defn_id absent from EVAL_REGISTRY) is handled differently per path,
    # keyed off ``include_transcript_only_evals``:
    #   - External-artifact path (True): treat None as SKIPPED, defensively —
    #     on an empty trajectory an unresolvable verifier cannot be graded and
    #     would otherwise raise and abort scoring.
    #   - Validate path (False): treat None as RUNNABLE, restoring validate()'s
    #     original behavior (its old ``_verifier_requires_trajectory`` returned
    #     False for None). The verifier then runs in validate() and surfaces as
    #     an ERROR result, so a misconfigured rubric fails golden validation
    #     instead of being silently skipped (status OK).
    runnable: list[Verifier] = []
    for v in verifiers:
        eval_defn = _resolve_eval_defn(v, eval_configs)
        unresolved_skip = eval_defn is None and include_transcript_only_evals
        if unresolved_skip or (
            eval_defn is not None
            and should_skip_verifier_without_transcript(
                eval_defn,
                include_transcript_only_evals=include_transcript_only_evals,
            )
        ):
            skipped_ids.add(v.verifier_id)
            verifier_results[v.verifier_id] = _skipped_result(v, reason, message)
        else:
            runnable.append(v)

    # Pass 2: transitive skips — drop verifiers depending on an already-skipped
    # verifier, repeating until the set stabilizes.
    changed = True
    while changed:
        changed = False
        next_runnable: list[Verifier] = []
        for v in runnable:
            if v.verifier_id in skipped_ids:
                continue
            deps = v.verifier_dependencies or []
            if any(dep_id in skipped_ids for dep_id in deps):
                skipped_ids.add(v.verifier_id)
                verifier_results[v.verifier_id] = _skipped_result(
                    v, dependency_reason, dependency_message
                )
                changed = True
            else:
                next_runnable.append(v)
        runnable = next_runnable

    return runnable, verifier_results


def exclude_skipped_from_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
) -> tuple[list[VerifierResult], list[Verifier]]:
    """Drop no-transcript "skipped" results (and their verifier configs) before scoring.

    Skipped verifiers are still persisted (so they surface in judge_grades) with a
    NEUTRAL score of 0.0 and ``verifier_result_values["skipped"] = True``. The
    scoring methods only exclude ``status == ERROR``, not "skipped", so feeding
    skipped rows into a scoring method would deflate the final score. Excluding
    them here keeps the score identical between the initial grading run
    (``runner.main``) and a filtered recompute
    (``modal_labs.compute_filtered_score``), and is a no-op for normal runs
    (which have no skipped rows).

    INTENTIONAL SEMANTIC re: gate / critical-value verifiers — a verifier is only
    skipped because its eval is transcript-dependent (a filesystem/artifact eval is
    never skipped), so an excluded gate is one that measures transcript content and
    genuinely cannot be evaluated on a no-transcript trajectory; it correctly does
    not cap. File-based gates stay runnable and still cap.
    """
    scored_results = [
        r for r in verifier_results if not r.verifier_result_values.get("skipped")
    ]
    scored_ids = {r.verifier_id for r in scored_results}
    scored_verifiers = [v for v in verifiers if v.verifier_id in scored_ids]
    return scored_results, scored_verifiers
