"""
Shared helper execution logic for grading runners.

Provides common functions for preparing and executing helpers needed by verifiers.
"""

from typing import IO, Any

from loguru import logger

from runner.evals.models import EvalConfig, EvalIds
from runner.evals.output_llm.artifact_filters import (
    FileTypeCategory,
    convert_file_types_to_extensions,
)
from runner.evals.registry import EVAL_REGISTRY, EvalDefn
from runner.helpers.models import HelperIds
from runner.helpers.registry import HELPER_REGISTRY, HelperDefn
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier
from runner.utils.metrics import phase

# Redeclared rather than imported from `runner.main`, which imports this module.
# `modal_labs.py` keeps its own copy for the same reason.
GRADING_PREFIX = "studio.grading"

# Evals that filter snapshot-diff artifacts purely by ``expected_file_type``.
# For these, content outside the selected extensions is discarded before grading,
# so it is safe to skip materializing it in the shared SNAPSHOT_DIFF helper. Other
# SNAPSHOT_DIFF consumers (file_diff_check, content_length_check, jupiter_*,
# browsing variants, account-specific verifiers, etc.) may need any file's
# content, so their presence disables the optimization entirely.
#
# The two visual evals belong here as well. They apply the same
# ``expected_file_type`` filter, and their extra steps — rendering a document as
# page images, and fetching reference artifacts — read bytes straight from the
# snapshot zips (transform_output_artifacts reads the final zip,
# fetch_artifacts_with_transformations the initial one), never from the diff's
# text content. OUTPUT_LLM_WEIGHTED_WITH_VLM is a wrapper that forwards to
# OUTPUT_LLM_MULTI_REPRESENTATION with visual grading on, so one argument covers
# both.
_SNAPSHOT_DIFF_SCOPE_SAFE_EVALS: frozenset[EvalIds] = frozenset(
    {
        EvalIds.OUTPUT_LLM,
        EvalIds.OUTPUT_LLM_LITE,
        EvalIds.OUTPUT_LLM_WEIGHTED,
        EvalIds.OUTPUT_LLM_DIFFICULTY_WEIGHTED,
        EvalIds.OUTPUT_LLM_MULTI_REPRESENTATION,
        EvalIds.OUTPUT_LLM_WEIGHTED_WITH_VLM,
    }
)

# Evals whose database Grading Targets delegate to a standalone database judge.
# The two targets now delegate to DIFFERENT judges and so need different helpers:
#
#   "Database Files (.db)"        -> db_state_llm_tools_eval, which judges the
#                                    FINAL database state and reads SNAPSHOT_DBS.
#   "Database Files – Diff (.db)" -> db_diff_llm_tools_eval, which judges the
#                                    baseline-vs-final diff and reads DB_DIFF.
#
# _DB_TARGET_HELPERS below is the single place that routing lives.
#
# NEITHER helper is a static dependency of these evals: DB_DIFF scans both
# snapshots and diffs SQLite/SQL-dump/JSON data, and SNAPSHOT_DBS extracts every
# database out of the final snapshot — so running either on every output_llm
# grade (the most common verifier) would be wasteful. Each is collected only
# when a verifier selects the matching target — see needs_db_diff() /
# needs_snapshot_dbs().
#
# OUTPUT_LLM_WEIGHTED_WITH_VLM inherits the delegation: it forwards to
# OUTPUT_LLM_MULTI_REPRESENTATION, which routes both database targets onward.
# Without the entry here the helper is never collected, and the delegate fails
# closed — score 0.0, "No database diff/state was available to evaluate." Its
# Grading Target dropdown omits the database options, so only an API-authored or
# imported verifier value reaches this path.
_DB_DELEGATING_EVALS: frozenset[EvalIds] = frozenset(
    {
        EvalIds.OUTPUT_LLM,
        EvalIds.OUTPUT_LLM_MULTI_REPRESENTATION,
        EvalIds.OUTPUT_LLM_WEIGHTED_WITH_VLM,
    }
)

# The helper each delegating database target needs.
_DB_TARGET_HELPERS: dict[str, HelperIds] = {
    FileTypeCategory.DATABASE_FILES.value: HelperIds.SNAPSHOT_DBS,
    FileTypeCategory.DATABASE_FILES_DIFF.value: HelperIds.DB_DIFF,
}


def _db_target_helper(
    verifier: Verifier,
    eval_defn_id: EvalIds | None,
) -> HelperIds | None:
    """The helper this verifier's database Grading Target needs, or None when it
    selects no database target (or sits on an eval that doesn't delegate)."""
    if eval_defn_id not in _DB_DELEGATING_EVALS:
        return None
    expected_file_type = (verifier.verifier_values or {}).get("expected_file_type")
    return _DB_TARGET_HELPERS.get(str(expected_file_type or ""))


def _verifier_uses_db_target(
    verifier: Verifier,
    eval_defn_id: EvalIds | None,
) -> bool:
    """True if this verifier selects EITHER database Grading Target on an eval
    that delegates database grading to a standalone database judge."""
    return _db_target_helper(verifier, eval_defn_id) is not None


def _needs_db_helper(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    helper_id: HelperIds,
) -> bool:
    eval_defn_by_config = {ec.eval_config_id: ec.eval_defn_id for ec in eval_configs}
    return any(
        _db_target_helper(v, eval_defn_by_config.get(v.eval_config_id)) == helper_id
        for v in verifiers
    )


def needs_db_diff(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> bool:
    """True if any verifier selects the "Database Files – Diff (.db)" Grading
    Target on an eval that delegates to db_diff_llm_tools_eval. Used to collect
    the DB_DIFF helper only for runs that actually need it."""
    return _needs_db_helper(verifiers, eval_configs, HelperIds.DB_DIFF)


def needs_snapshot_dbs(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> bool:
    """True if any verifier selects the "Database Files (.db)" Grading Target on
    an eval that delegates to db_state_llm_tools_eval. Used to collect the
    SNAPSHOT_DBS helper only for runs that actually need it. The standalone
    db_state_llm_tools eval declares SNAPSHOT_DBS statically and does not go
    through here."""
    return _needs_db_helper(verifiers, eval_configs, HelperIds.SNAPSHOT_DBS)


def verifier_helper_ids(
    verifier: Verifier,
    eval_defn: EvalDefn,
) -> list[HelperIds]:
    """The helper results to forward to an eval impl for this verifier: its
    static ``helper_dependencies`` plus any conditionally-collected helpers the
    verifier needs. SNAPSHOT_DBS / DB_DIFF are added for the two database Grading
    Targets — neither is a static dependency (so neither runs on every grade), so
    the per-verifier forwarding in the runners must add it back here, mirroring
    collect_helpers()."""
    helper_ids = list(eval_defn.helper_dependencies)
    db_helper = _db_target_helper(verifier, eval_defn.eval_id)
    if db_helper is not None and db_helper not in helper_ids:
        helper_ids.append(db_helper)
    return helper_ids


def compute_snapshot_diff_content_extensions(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> list[str] | None:
    """Compute the set of file extensions whose content the SNAPSHOT_DIFF helper
    must materialize, or ``None`` to materialize everything (original behavior).

    Returns a concrete (possibly empty) extension list ONLY when every verifier
    that depends on SNAPSHOT_DIFF is a scope-safe eval (see
    ``_SNAPSHOT_DIFF_SCOPE_SAFE_EVALS``) configured with a concrete
    ``expected_file_type``. In every other case it returns ``None`` so the diff is
    computed exactly as before. An empty list means no verifier needs file content
    (e.g. all are "Final Answer Only"), so all file content can be skipped.
    """
    eval_defn_by_config = {ec.eval_config_id: ec.eval_defn_id for ec in eval_configs}
    union: set[str] = set()
    saw_snapshot_diff = False

    for verifier in verifiers:
        defn_id = eval_defn_by_config.get(verifier.eval_config_id)
        if defn_id is None:
            return None  # can't reason about it → don't restrict
        eval_defn = EVAL_REGISTRY[defn_id]
        if HelperIds.SNAPSHOT_DIFF not in eval_defn.helper_dependencies:
            continue
        saw_snapshot_diff = True
        expected_file_type = (verifier.verifier_values or {}).get("expected_file_type")
        # Either database target delegates to a standalone database judge (which
        # uses the DB_DIFF or SNAPSHOT_DBS helper, not this text diff), so such a
        # verifier needs no SNAPSHOT_DIFF file content and must not force
        # materialize-all either. This is checked BEFORE the scope-safe gate for
        # two reasons: a scope-safe delegating eval must not add ``.db`` to the
        # union and pull binary SQLite content through the text diff, and a
        # delegating eval that is NOT scope-safe must not short-circuit to None
        # either.
        if _verifier_uses_db_target(verifier, defn_id):
            continue
        if defn_id not in _SNAPSHOT_DIFF_SCOPE_SAFE_EVALS:
            return None  # a consumer that may need any content → materialize all
        extensions = convert_file_types_to_extensions(expected_file_type)
        # None  → "Final Answer Only" (needs no files; contributes nothing)
        # []    → "All output"/unset/invalid (needs everything → disable optimization)
        # list  → specific extensions
        if extensions is None:
            continue
        if not extensions:
            return None
        union.update(ext.lower() for ext in extensions)

    if not saw_snapshot_diff:
        return None
    return sorted(union)


def collect_helpers(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> dict[HelperIds, HelperDefn]:
    """
    Collect all helpers needed by the given verifiers.

    Args:
        verifiers: List of verifiers to collect helpers for
        eval_configs: List of eval configurations

    Returns:
        Dict mapping HelperIds to HelperDefn
    """
    helpers: dict[HelperIds, HelperDefn] = {}
    used_eval_config_ids = {v.eval_config_id for v in verifiers}
    for eval_config in eval_configs:
        if eval_config.eval_config_id not in used_eval_config_ids:
            continue
        eval_defn = EVAL_REGISTRY[eval_config.eval_defn_id]
        for helper_id in eval_defn.helper_dependencies:
            helper_defn = HELPER_REGISTRY[helper_id]
            helpers[helper_id] = helper_defn

    # Conditionally pull in the database helper each output_llm-family verifier's
    # Grading Target delegates to: SNAPSHOT_DBS for "Database Files (.db)" (final
    # state) and DB_DIFF for "Database Files – Diff (.db)". Neither is a static
    # dependency — see _DB_DELEGATING_EVALS — so non-database grades skip both
    # the expensive database diff and the snapshot database extraction entirely.
    for helper_id, needed in (
        (HelperIds.DB_DIFF, needs_db_diff),
        (HelperIds.SNAPSHOT_DBS, needs_snapshot_dbs),
    ):
        if helper_id not in helpers and needed(verifiers, eval_configs):
            helpers[helper_id] = HELPER_REGISTRY[helper_id]

    return helpers


def build_parser_config_kwargs(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> dict[HelperIds, dict[str, Any]]:
    """
    Build helper kwargs, merging parser_config for ARTIFACT_STATE helper.

    Multiple eval configs may contribute table_mappings — we merge them.
    If they disagree on parser type or file_glob, raise immediately.

    Args:
        verifiers: List of verifiers being evaluated
        eval_configs: List of eval configurations

    Returns:
        Dict mapping HelperIds to kwargs dict

    Raises:
        ValueError: If eval configs have conflicting parser_config values
    """
    helper_kwargs: dict[HelperIds, dict[str, Any]] = {}
    merged_parser_config: dict[str, Any] | None = None
    used_eval_config_ids = {v.eval_config_id for v in verifiers}

    for eval_config in eval_configs:
        if eval_config.eval_config_id not in used_eval_config_ids:
            continue
        eval_defn = EVAL_REGISTRY[eval_config.eval_defn_id]
        if HelperIds.ARTIFACT_STATE in eval_defn.helper_dependencies:
            parser_config = eval_config.eval_config_values.get("parser_config")
            if not parser_config:
                continue
            if merged_parser_config is None:
                merged_parser_config = dict(parser_config)
                merged_parser_config["table_mappings"] = list(
                    parser_config.get("table_mappings", [])
                )
            else:
                if merged_parser_config.get("parser") != parser_config.get(
                    "parser"
                ) or merged_parser_config.get("file_glob") != parser_config.get(
                    "file_glob"
                ):
                    raise ValueError(
                        f"Conflicting parser_config for ARTIFACT_STATE helper: {eval_config.eval_config_id}"
                    )
                merged_parser_config["table_mappings"].extend(
                    parser_config.get("table_mappings", [])
                )

    if merged_parser_config is not None:
        helper_kwargs[HelperIds.ARTIFACT_STATE] = {
            "parser_config": merged_parser_config
        }

    # Collect json_id_field and diff_all_types for DB_DIFF helper (JSON file diffing)
    json_id_field: str | None = None
    diff_all_types: bool = False
    for eval_config in eval_configs:
        if eval_config.eval_config_id not in used_eval_config_ids:
            continue
        eval_defn = EVAL_REGISTRY[eval_config.eval_defn_id]
        if HelperIds.DB_DIFF in eval_defn.helper_dependencies:
            val = eval_config.eval_config_values.get("json_id_field")
            if val:
                json_id_field = val
            diff_all = eval_config.eval_config_values.get("diff_all_types")
            if diff_all is None:
                # Use default from registry schema when not explicitly set
                for field in eval_defn.eval_config_fields:
                    if field.field_id == "diff_all_types":
                        diff_all = field.default_value
                        break
            if diff_all is True or str(diff_all).lower() == "true":
                diff_all_types = True

    # NOTE: the conditional DB_DIFF path (output_llm-family with the "Database
    # Files – Diff (.db)" Grading Target) intentionally does NOT force
    # diff_all_types.
    # It defaults to False (priority-based fallback: SQLite .db first), which is
    # both the right semantics for a .db-specific target and the leaner choice
    # for the feature's purpose — avoiding large databases being pulled into
    # memory. diff_all_types=True would additionally load every JSON data file
    # (and SQL dumps) in the snapshot into memory, working against that goal.
    # This diverges from db_diff_llm_tools' default (True) by design; forcing it
    # here would also override a co-located db_diff_llm/db_diff_llm_tools eval's
    # explicit diff_all_types on the shared, run-global DB_DIFF helper.

    if json_id_field is not None or diff_all_types:
        db_diff_kwargs = {}
        if json_id_field is not None:
            db_diff_kwargs["json_id_field"] = json_id_field
        if diff_all_types:
            db_diff_kwargs["diff_all_types"] = diff_all_types
        helper_kwargs[HelperIds.DB_DIFF] = db_diff_kwargs

    # Restrict SNAPSHOT_DIFF content materialization to the file types the run's
    # verifiers actually grade, when it's provably safe to do so (avoids reading
    # large irrelevant artifacts into memory). None => no restriction.
    content_extensions = compute_snapshot_diff_content_extensions(
        verifiers, eval_configs
    )
    if content_extensions is not None:
        helper_kwargs[HelperIds.SNAPSHOT_DIFF] = {
            "content_extensions": content_extensions
        }

    return helper_kwargs


async def execute_helpers(
    helpers: dict[HelperIds, HelperDefn],
    helper_kwargs: dict[HelperIds, dict[str, Any]],
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
    grading_settings: GradingSettings,
) -> dict[HelperIds, Any]:
    """
    Execute all helpers and return their results.

    Args:
        helpers: Dict of helpers to execute
        helper_kwargs: Kwargs for helpers (e.g., parser_config)
        initial_snapshot_bytes: Initial snapshot
        final_snapshot_bytes: Final snapshot
        trajectory: Agent trajectory
        verifiers: List of verifiers
        eval_configs: List of eval configurations
        grading_settings: Grading settings

    Returns:
        Dict mapping HelperIds to helper results

    Raises:
        Exception: If any helper execution fails
    """
    eval_defn_id_by_config_id = {
        ec.eval_config_id: str(ec.eval_defn_id) for ec in eval_configs
    }

    helper_results: dict[HelperIds, Any] = {}
    for helper_id, helper_defn in helpers.items():
        try:
            # Per-helper timing. The enclosing `phase("helpers")` in `run_grading`
            # covers the whole loop, so it cannot say which helper spent the time:
            # `filesystem_setup` (unzipping both snapshots) and `db_diff` land in
            # one number. `helper` is a `HelperIds` enum value, so the tag stays
            # bounded. A snapshot id would not, which is why
            # `s3_transfer.note_snapshot_read` refuses to tag one.
            async with phase(
                "helper",
                prefix=GRADING_PREFIX,
                tags=[f"helper:{helper_id.value}"],
            ):
                if helper_defn.helper_impl_with_context is not None:
                    helper_results[
                        helper_id
                    ] = await helper_defn.helper_impl_with_context(
                        initial_snapshot_bytes,
                        final_snapshot_bytes,
                        trajectory,
                        verifiers,
                        eval_defn_id_by_config_id,
                        grading_settings,
                    )
                elif helper_defn.helper_impl is not None:
                    helper_results[helper_id] = await helper_defn.helper_impl(
                        initial_snapshot_bytes,
                        final_snapshot_bytes,
                        trajectory,
                        **helper_kwargs.get(helper_id, {}),
                    )
                else:
                    raise ValueError(f"Helper {helper_id} has no implementation")
        except Exception as e:
            logger.error(f"[HELPER] Error evaluating helper {helper_id}: {repr(e)}")
            raise

    return helper_results
