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

A third rule covers an external artifact that DID carry a backfilled transcript
(RLS-10185): message-reading evals become gradeable, and only those needing
``trajectory.output`` keys (``OUTPUT_DEPENDENT_EVALS``) or live environment state
(``LIVE_ENV_DEPENDENT_HELPERS``) stay skipped. See
``partition_verifiers_for_artifact_import``.

Used by ``validate.py`` (golden-end-state validation, no trajectory) and
``main.py``'s scoring path.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from loguru import logger
from pydantic import BaseModel, ValidationError, field_validator

from runner.evals.models import EvalConfig, EvalIds, EvalType
from runner.evals.registry import EVAL_REGISTRY, EvalDefn
from runner.helpers.models import HelperIds
from runner.models import (
    AgentTrajectoryOutput,
    Verifier,
    VerifierResult,
    VerifierResultStatus,
)


class VerifierFilter(StrEnum):
    """Value of ``grading_run_args["verifier_filter"]`` — restrict which verifier
    TYPES a grading run executes. Absent / ``ALL`` runs every verifier (the default),
    so grading is unchanged unless a run explicitly opts in. Used to grade only the
    fast deterministic checks in our own containers while LLM verifiers are handled
    elsewhere (e.g. relayed to the client)."""

    ALL = "all"
    DETERMINISTIC_ONLY = "deterministic_only"  # only non-LLM (purely PROGRAMMATIC)
    LLM_ONLY = "llm_only"  # only LLM_JUDGE / AGENTIC


class GradingRunScopeArgs(BaseModel):
    """Typed view of the ``grading_run_args`` keys that narrow which verifiers a
    grading run executes (RLS-10923).

    Unknown keys are ignored. The jsonb column is shared with other consumers
    (actor attribution, the Sparta regrade wait-targets); this view reads only
    the key below.
    """

    # Restrict the run to these verifier ids. Absent / None / empty is a NO-OP
    # (every verifier runs), so grading is unchanged unless a run opts in. Set
    # by the server's repair loop to re-execute only the criteria that need it,
    # rather than regrading a whole rubric to fix a handful of rows.
    verifier_ids: list[str] | None = None

    @field_validator("verifier_ids")
    @classmethod
    def _drop_blank_ids(cls, value: list[str] | None) -> list[str] | None:
        # A blank id matches no verifier, so a list of them would be a NON-empty
        # scope that skips EVERY verifier -- the opposite of the no-op an empty
        # scope is defined to be.
        if value is None:
            return None
        return [v for v in (candidate.strip() for candidate in value) if v]


def parse_grading_run_scope_args(raw: dict[str, Any] | None) -> GradingRunScopeArgs:
    """Parse the transport dict at the runner boundary. ``None`` gives defaults.

    Degrades to "no scope" instead of raising. ``grading_run_args`` is an
    unvalidated jsonb blob written by several producers, and a malformed value
    must never be able to stop a grading run: grading everything is the
    pre-RLS-10923 behavior and is always safe, whereas raising would fail a run
    over an argument it could simply ignore.
    """
    try:
        return GradingRunScopeArgs.model_validate(raw or {})
    except ValidationError as exc:
        logger.warning(
            "[GRADING][SCOPE] Ignoring malformed grading_run_args; "
            f"grading every verifier | error={exc}"
        )
        return GradingRunScopeArgs()


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
# Declared by string id and resolved to the ``EvalIds`` members present in THIS
# build. The published OSS runner filters ``EvalIds`` down to an allowlist
# (``oss_archipelago`` ``INCLUDED_EVAL_IDS``), so an id absent from a given build
# is dropped here rather than raising ``AttributeError`` when this module is
# imported. In the full (studio) enum every id resolves, so behavior is unchanged
# — but this keeps a filtered OSS build importable even if the allowlist lags an
# eval added to this set.
_TRANSCRIPT_ONLY_EVAL_IDS: frozenset[str] = frozenset(
    {
        "cli_verifier",  # reads trajectory.output["cli_results"]; no fs
        "sparta_mirror",  # reads trajectory.output["final_score"]; RAISES if missing
        "sparta_agentic_grading",  # reads trajectory.output keys; RAISES if missing
        "sparta_mcp_grading",  # reads trajectory.output keys; RAISES if missing
        "mcq_exact_match",  # extracts final assistant response; no fs
        "ace_criterion_verifier",  # reads trajectory.output["ace_grounding"]; no fs
        "response_tool_verifier",  # reads trajectory.output directly; no fs
        "mlebench_result",  # reads trajectory.output["grade"]; no fs
        "user_sim_judge",  # USER_SIM_JUDGE_RESULT helper reads trajectory.messages only
        "hle_judge",  # extracts final assistant response; no fs
        # Reads trajectory.messages only (renders the conversation for the
        # judge); no helper, no fs. Without this it is not covered by ANY rule —
        # the helper-dependency rule cannot reach it — so a no-transcript
        # trajectory would grade every criterion as criteria_met=false and
        # record a fabricated 0.0 for the task instead of skipping it.
        "healthbench_criterion",
        "mrcr_similarity",  # FINAL_ANSWER helper (transcript-only)
        "tool_call_check",  # reads trajectory.messages tool calls; no fs
        "tool_call_llm_check",  # reads trajectory.messages tool calls; no fs
        "posttraining_tool_call_check",  # reads trajectory.messages tool calls; no fs
        "interview_fraud_decision_match",  # extracts final assistant response; no fs
        "time_fraud_decision_match",  # extracts final assistant response; no fs
        # EXCLUDED: gdpval_judge (reads golden/initial snapshot filesystem),
        #           vca_behavior_llm_check (VCA_CONTEXT helper reads final snapshot fs)
    }
)
_EVAL_ID_VALUES: frozenset[str] = frozenset(e.value for e in EvalIds)
TRANSCRIPT_ONLY_EVALS: set[EvalIds] = {
    EvalIds(eval_id)
    for eval_id in _TRANSCRIPT_ONLY_EVAL_IDS
    if eval_id in _EVAL_ID_VALUES
}

# Evals reading ``trajectory.output`` KEYS, which a backfilled transcript never
# fills. Message-reading siblings (tool_call_*, hle_judge, mcq_exact_match, …) are
# deliberately absent — a transcript makes those gradeable, which is the point of
# RLS-10185. So is response_tool_verifier, which reads output but degrades to a
# no-artifacts prompt instead of raising.
_OUTPUT_DEPENDENT_EVAL_IDS: frozenset[str] = frozenset(
    {
        "cli_verifier",  # trajectory.output["cli_results"]
        "sparta_mirror",  # trajectory.output["final_score"]; RAISES if missing
        "sparta_agentic_grading",  # trajectory.output keys; RAISES if missing
        "sparta_mcp_grading",  # problem_run_id/final_score/taiga_job_id; RAISES if missing
        "ace_criterion_verifier",  # trajectory.output["ace_grounding"]
        "mlebench_result",  # trajectory.output["grade"]
    }
)
assert _OUTPUT_DEPENDENT_EVAL_IDS <= _TRANSCRIPT_ONLY_EVAL_IDS, (
    "every output-dependent eval must also be transcript-only, so the "
    "no-transcript skip stays a superset of the artifact-import skip"
)
OUTPUT_DEPENDENT_EVALS: set[EvalIds] = {
    EvalIds(eval_id)
    for eval_id in _OUTPUT_DEPENDENT_EVAL_IDS
    if eval_id in _EVAL_ID_VALUES
}

# Helpers whose signal only a live agent session can produce. The criterion for
# adding one is not "reads live state" — these read the final snapshot like
# everything else — but whether the artifact could be in an upload at all: a
# browser profile or Playwright trace only ever comes from an agent driving a
# browser. The ``*_state`` helpers (looker, quickbooks, tableau, artifact) stay
# out; an app database or CSV export is a plausible deliverable.
LIVE_ENV_DEPENDENT_HELPERS: set[HelperIds] = {
    HelperIds.BROWSER_STATE,
    HelperIds.PLAYWRIGHT_TRACE_PARSER,
}

# Written into ``trajectory_output`` by the external-artifact import remix, and the
# only signal here that no agent ran (trajectory_type isn't in the grading payload).
# Key on the marker alone, never on "output is empty": an ordinary run can finish
# with no structured output, and treating that as an import would neutral-skip its
# verifiers and drop them from scoring.
_EXTERNAL_ARTIFACT_OUTPUT_SOURCE = "external_artifact_import"


def is_external_artifact_import(trajectory: AgentTrajectoryOutput) -> bool:
    """Whether this trajectory is an uploaded artifact rather than an agent run."""
    return (trajectory.output or {}).get("source") == _EXTERNAL_ARTIFACT_OUTPUT_SOURCE


def should_skip_verifier_on_artifact_import(eval_defn: EvalDefn) -> bool:
    """True if the eval needs ``trajectory.output`` keys or live environment state
    — the two signals an import can never have. Filesystem and message-reading
    evals stay gradeable.
    """
    if eval_defn.eval_id in OUTPUT_DEPENDENT_EVALS:
        return True
    return bool(set(eval_defn.helper_dependencies) & LIVE_ENV_DEPENDENT_HELPERS)


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


def _is_llm_eval(eval_defn: EvalDefn) -> bool:
    """True if the eval is KNOWN to call an LLM to grade — an ``LLM_JUDGE`` or a whole-task
    ``AGENTIC`` grader. (An eval typed as both counts as LLM — it can issue a judge call.)
    An eval with empty ``eval_types`` is NOT positively an LLM eval — see ``_is_deterministic_eval``
    for why that is also NOT treated as deterministic."""
    return (
        EvalType.LLM_JUDGE in eval_defn.eval_types
        or EvalType.AGENTIC in eval_defn.eval_types
    )


def _is_deterministic_eval(eval_defn: EvalDefn) -> bool:
    """True only if the eval is KNOWN to be purely deterministic — it declares a type and none
    of them is LLM/AGENTIC. Empty ``eval_types`` (the field default) returns False: an
    unannotated eval CANNOT be confirmed model-free, so ``deterministic_only`` must not run it
    (it might call an LLM). Every current EVAL_REGISTRY entry sets ``eval_types``; this guards a
    future one that forgets to."""
    return bool(eval_defn.eval_types) and not _is_llm_eval(eval_defn)


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

    # Direct skips are decided by the eval's transcript dependence.
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
    def _direct_skip(eval_defn: EvalDefn | None) -> bool:
        if eval_defn is None:
            return include_transcript_only_evals
        return should_skip_verifier_without_transcript(
            eval_defn,
            include_transcript_only_evals=include_transcript_only_evals,
        )

    return _partition_by(
        verifiers,
        eval_configs,
        should_skip=_direct_skip,
        reason=reason,
        dependency_reason=dependency_reason,
        message=message,
        dependency_message=dependency_message,
    )


def partition_verifiers_for_artifact_import(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    *,
    reason: str,
    dependency_reason: str,
    message: str,
    dependency_message: str,
) -> tuple[list[Verifier], dict[str, VerifierResult]]:
    """Split verifiers for an external artifact that DID carry a transcript.

    Filesystem and message-reading evals run; the ones needing
    ``trajectory.output`` keys or live environment state are skipped neutrally
    rather than raising (or inventing a verdict about a browser session that never
    happened).

    An unresolved eval_defn is skipped, matching the no-transcript path:
    ``evaluate_verifier`` raises for it and main() gathers fail-fast, so leaving it
    runnable would abort the whole run instead of surfacing one ERROR row.
    """
    return _partition_by(
        verifiers,
        eval_configs,
        should_skip=lambda defn: (
            defn is None or should_skip_verifier_on_artifact_import(defn)
        ),
        reason=reason,
        dependency_reason=dependency_reason,
        message=message,
        dependency_message=dependency_message,
    )


def partition_verifiers_by_filter(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    verifier_filter: str | None,
) -> tuple[list[Verifier], dict[str, VerifierResult]]:
    """Split verifiers by TYPE per ``grading_run_args["verifier_filter"]``.

    - ``deterministic_only`` runs only verifiers KNOWN to be purely deterministic
      (``_is_deterministic_eval``); ``LLM_JUDGE`` / ``AGENTIC`` verifiers — and any that can't be
      positively classified (unresolvable eval, or empty ``eval_types``) — are skipped neutrally,
      so an unannotated eval is never run under this filter where it might call a model.
    - ``llm_only`` runs only ``LLM_JUDGE`` / ``AGENTIC`` verifiers; the rest are skipped.
    - ``all`` / None / anything unrecognized is a NO-OP (every verifier runs) — the
      backward-compatible default.

    Skipped verifiers get a NEUTRAL result (excluded from scoring by
    ``exclude_skipped_from_scoring``), and transitive dependents are skipped too (shared
    ``_partition_by``), so a kept verifier never runs against a filtered-out dependency.
    Each filter keeps only what it can POSITIVELY classify as matching; everything else —
    the other type, an unresolvable eval, or an unclassifiable one — is skipped.
    """
    if verifier_filter == VerifierFilter.DETERMINISTIC_ONLY:
        reason = f"verifier_filter={VerifierFilter.DETERMINISTIC_ONLY}"
        dependency_reason = f"{reason} (dependency)"
        return _partition_by(
            verifiers,
            eval_configs,
            should_skip=lambda defn: defn is None or not _is_deterministic_eval(defn),
            reason=reason,
            dependency_reason=dependency_reason,
            message=f"Skipped: non-deterministic verifier excluded by {reason}",
            dependency_message=f"Skipped: depends on a verifier excluded by {reason}",
        )
    if verifier_filter == VerifierFilter.LLM_ONLY:
        reason = f"verifier_filter={VerifierFilter.LLM_ONLY}"
        dependency_reason = f"{reason} (dependency)"
        return _partition_by(
            verifiers,
            eval_configs,
            should_skip=lambda defn: defn is None or not _is_llm_eval(defn),
            reason=reason,
            dependency_reason=dependency_reason,
            message=f"Skipped: non-LLM verifier excluded by {reason}",
            dependency_message=f"Skipped: depends on a verifier excluded by {reason}",
        )
    return list(verifiers), {}


def partition_verifiers_by_ids(
    verifiers: list[Verifier],
    verifier_ids: list[str] | None,
) -> tuple[list[Verifier], dict[str, VerifierResult]]:
    """Split verifiers by ID per ``grading_run_args["verifier_ids"]`` (RLS-10923).

    Verifiers named in ``verifier_ids`` run; every other verifier is skipped
    neutrally. ``None`` / empty is a NO-OP (every verifier runs) -- the
    backward-compatible default, mirroring the ``all`` branch of
    ``partition_verifiers_by_filter``. An id naming a verifier that no longer
    exists matches nothing and is otherwise ignored.

    Used by the server's repair loop: a grading run that crashed on some of its
    criteria is repaired by a NEW run scoped to just those, instead of paying
    for a whole rubric of judge calls to fix a handful of rows. The skipped
    placeholders are load-bearing for that loop rather than incidental -- the
    repair merges the parent run's stored verdicts onto exactly these rows,
    keyed on ``verifier_result_values->>'skipped'``.

    DELIBERATELY ONE PASS. This rule does NOT go through ``_partition_by``, and
    an in-scope verifier is NEVER skipped for depending on an out-of-scope one.
    ``_partition_by``'s second pass (transitively skip anything depending on a
    skipped verifier) is right for the type filter, where "skipped" means
    "ungradeable". Here "skipped" means "already holds a good verdict, do not
    pay to recompute it", so that pass would drop in-scope verifiers whose
    dependencies graded fine last time -- silently failing to repair a criterion
    the caller explicitly asked for. THE CALLER OWNS DEPENDENCY CLOSURE and is
    required to hand down an already-closed scope; scope means scope.

    That is safe today because ``EvalImplInput.dependencies`` is populated by
    the runner but read by no eval in the registry, so ``verifier_dependencies``
    affects execution ORDER and transitive skipping only.

    Dropping pass 2 does mean the caller takes on BOTH things it protected, and
    both need handling -- an earlier version of this docstring named only the
    first and called it "the one thing", which was wrong and shipped a crash:

    1. ``evaluate_verifier``'s ``verifier_results[dep_id]`` lookup. Covered here:
       out-of-scope verifiers still get placeholder rows, replaced by the parent's
       real verdicts in ``merge_carried_verifier_results``.
    2. ``group_by_dependency_level``'s dangling-reference check. NOT covered here,
       and not coverable here -- that function validates dependency ids against the
       narrowed verifier LIST and never sees ``verifier_results``, so a complete set
       of results does not satisfy it. ``runner.main`` passes
       ``satisfied_dependency_ids`` so an already-resolved dependency is dropped
       from the ordering while staying on ``verifier_dependencies``.

    Takes no ``eval_configs``, unlike its sibling rules: the scope is by id, so
    nothing here needs to resolve a verifier to its eval.
    """
    scope = set(verifier_ids or [])
    if not scope:
        return list(verifiers), {}

    reason = "verifier_ids (repair scope)"
    message = "Skipped: verifier outside the repair scope (RLS-10923)"

    runnable: list[Verifier] = []
    skipped: dict[str, VerifierResult] = {}
    for v in verifiers:
        if v.verifier_id in scope:
            runnable.append(v)
        else:
            skipped[v.verifier_id] = _skipped_result(v, reason, message)
    return runnable, skipped


def merge_carried_verifier_results(
    scope_skipped: dict[str, VerifierResult],
    carried_verifier_results: dict[str, VerifierResult] | None,
) -> dict[str, VerifierResult]:
    """Substitute a repair run's carried verdicts for its scope placeholders (RLS-10923).

    ``scope_skipped`` is what ``partition_verifiers_by_ids`` returned: a neutral
    placeholder per out-of-scope verifier. ``carried_verifier_results`` is the
    parent run's real verdict for those same verifiers, shipped on the grading
    config. Returns the rows to fold into ``verifier_results``, with a carried
    verdict replacing its placeholder wherever one exists.

    That substitution is what lets a repair publish a correct score by itself:
    scoring then runs over the COMPLETE rubric rather than the repaired subset,
    and an in-scope verifier whose dependency is out of scope reads the real
    dependency verdict instead of an empty placeholder.

    RAISES if any out-of-scope verifier is left without a scoreable verdict --
    either no carried row at all, or one still marked ``skipped``. Both leave the
    same hole: ``exclude_skipped_from_scoring`` drops the row AND its verifier
    config, the scoring method averages over whatever is left, and nothing else
    notices. ``any_verifier_crashed`` does not, because a placeholder is
    ``status=OK`` and carries no crash marker. The run would land COMPLETED with
    a final_score computed over part of the rubric, and
    ``build_latest_grading_runs_cte`` (DISTINCT ON trajectory_id ORDER BY
    created_at DESC, no status filter) would then hand that number to batch
    analytics as the trajectory's current score.

    Refusing mirrors ``any_verifier_crashed`` in ``runner.main``, which discards
    a score rather than publish one computed from the criteria that happened to
    survive. This is the same hazard reached through ``skipped`` instead of
    ``ERROR``: a shrinking denominator that still looks like a plausible number.
    Raising here, before helpers or judge calls, also means a malformed payload
    costs nothing to reject.

    Note this is NOT the "missing field degrades to old behavior" case. An
    unscoped run never reaches here -- ``scope_skipped`` is empty and
    ``runner.main`` skips the call -- so an ordinary grading run is untouched and
    an old server that sets neither field still grades everything. Only a run
    that opted into a scope can fail this check, and a scope whose excluded
    verifiers are not carried is not a state any caller should produce.

    Deliberately NARROW rather than a blanket ``dict.update``. Only ids already
    present in ``scope_skipped`` are substituted, which drops three hazards
    without a special case for any of them:

    - A carried row for a verifier no longer on the rubric would otherwise reach
      ``exclude_skipped_from_scoring``, which pairs results to verifiers by id,
      and hand the scoring method a result with no verifier config behind it.
    - A carried row for an IN-scope verifier would otherwise pre-seed a verdict
      the run is about to recompute.
    - A carried row for a verifier skipped by another rule (no transcript, or the
      type filter) would otherwise resurrect it into a run that deliberately
      excluded it.
    """
    merged = dict(scope_skipped)
    for verifier_id, carried in (carried_verifier_results or {}).items():
        if verifier_id in merged:
            merged[verifier_id] = carried

    uncarried = sorted(
        verifier_id
        for verifier_id, result in merged.items()
        if result.verifier_result_values.get("skipped")
    )
    if uncarried:
        shown = ", ".join(uncarried[:10])
        elided = f" (+{len(uncarried) - 10} more)" if len(uncarried) > 10 else ""
        raise ValueError(
            f"Repair scope is incomplete: {len(uncarried)} of {len(merged)} "
            f"out-of-scope verifiers have no scoreable carried verdict "
            f"[{shown}{elided}]. Scoring without them would publish a score over "
            f"part of the rubric as though it were complete. Every verifier the "
            f"scope excludes must carry a verdict that is not marked skipped "
            f"(RLS-10923)."
        )
    return merged


def _partition_by(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    *,
    should_skip: Callable[[EvalDefn | None], bool],
    reason: str,
    dependency_reason: str,
    message: str,
    dependency_message: str,
) -> tuple[list[Verifier], dict[str, VerifierResult]]:
    """Two-pass partition shared by every skip rule: direct skips per
    ``should_skip``, then transitive skips of anything depending on them."""
    verifier_results: dict[str, VerifierResult] = {}
    skipped_ids: set[str] = set()

    runnable: list[Verifier] = []
    for v in verifiers:
        if should_skip(_resolve_eval_defn(v, eval_configs)):
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
    (``modal_labs.run_scoring``), and is a no-op for normal runs
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


def exclude_agentic_from_scoring(
    verifier_results: list[VerifierResult],
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> tuple[list[VerifierResult], list[Verifier]]:
    """Drop ``EvalType.AGENTIC`` verifiers before scoring.

    An agentic verifier grades the whole task with one agent rather than one
    criterion per judge call, so it is reported in ``judge_grades`` but must not
    be folded into the rubric's weighted average. Excluding it by eval type keeps
    that true regardless of any weight left on the verifier row (an AV verifier
    with no weight override otherwise resolves to the fallback weight of 1.0 and
    silently contaminates the score). ``EvalType.AGENTIC`` is kept distinct from
    ``LLM_JUDGE`` for exactly this kind of separation.

    Unlike ``exclude_skipped_from_scoring`` this runs only in the initial grading
    run (``runner.main``): the filtered recompute
    (``modal_labs.run_scoring``) has no ``eval_configs`` to resolve the
    eval type, so it still relies on the verifier's ``numerical_weight`` (agentic
    verifiers should carry ``numerical_weight = 0``) to stay out of the score.

    The exclusion only applies when other scored verifiers remain. A task whose
    only verifier is the agentic one keeps it: an empty scored set collapses the
    weighted average to 0.0, which would report real work as a total failure.
    """
    agentic_ids = {
        v.verifier_id
        for v in verifiers
        if (eval_defn := _resolve_eval_defn(v, eval_configs)) is not None
        and EvalType.AGENTIC in eval_defn.eval_types
    }
    scored_results = [r for r in verifier_results if r.verifier_id not in agentic_ids]
    if not scored_results:
        return verifier_results, verifiers
    scored_ids = {r.verifier_id for r in scored_results}
    scored_verifiers = [v for v in verifiers if v.verifier_id in scored_ids]
    return scored_results, scored_verifiers
