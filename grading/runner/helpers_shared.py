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

# Evals that filter snapshot-diff artifacts purely by ``expected_file_type``.
# For these, content outside the selected extensions is discarded before grading,
# so it is safe to skip materializing it in the shared SNAPSHOT_DIFF helper. Other
# SNAPSHOT_DIFF consumers (file_diff_check, content_length_check, jupiter_*, vlm /
# multi-representation / browsing variants, account-specific verifiers, etc.) may
# need any file's content, so their presence disables the optimization entirely.
_SNAPSHOT_DIFF_SCOPE_SAFE_EVALS: frozenset[EvalIds] = frozenset(
    {
        EvalIds.OUTPUT_LLM,
        EvalIds.OUTPUT_LLM_LITE,
        EvalIds.OUTPUT_LLM_WEIGHTED,
        EvalIds.OUTPUT_LLM_DIFFICULTY_WEIGHTED,
    }
)

# Evals whose "Database Files (.db)" Grading Target delegates to
# db_diff_llm_tools_eval, which reads the DB_DIFF helper result. DB_DIFF is
# NOT a static dependency of these evals: the helper scans both snapshots and
# diffs SQLite/SQL-dump/JSON data, so running it on every output_llm grade
# (the most common verifier) would be wasteful. Instead it is collected only
# when a verifier actually selects the database target — see needs_db_diff().
_DB_DIFF_DELEGATING_EVALS: frozenset[EvalIds] = frozenset(
    {
        EvalIds.OUTPUT_LLM,
        EvalIds.OUTPUT_LLM_MULTI_REPRESENTATION,
    }
)


def _verifier_uses_db_target(
    verifier: Verifier,
    eval_defn_id: EvalIds | None,
) -> bool:
    """True if this verifier selects the "Database Files (.db)" Grading Target on
    an eval that delegates database grading to db_diff_llm_tools_eval."""
    if eval_defn_id not in _DB_DIFF_DELEGATING_EVALS:
        return False
    expected_file_type = (verifier.verifier_values or {}).get("expected_file_type")
    return expected_file_type == FileTypeCategory.DATABASE_FILES.value


def needs_db_diff(
    verifiers: list[Verifier],
    eval_configs: list[EvalConfig],
) -> bool:
    """True if any verifier selects the "Database Files (.db)" Grading Target on
    an eval that delegates database grading to db_diff_llm_tools_eval. Used to
    collect the DB_DIFF helper only for runs that actually need it."""
    eval_defn_by_config = {ec.eval_config_id: ec.eval_defn_id for ec in eval_configs}
    return any(
        _verifier_uses_db_target(v, eval_defn_by_config.get(v.eval_config_id))
        for v in verifiers
    )


def verifier_helper_ids(
    verifier: Verifier,
    eval_defn: EvalDefn,
) -> list[HelperIds]:
    """The helper results to forward to an eval impl for this verifier: its
    static ``helper_dependencies`` plus any conditionally-collected helpers the
    verifier needs. DB_DIFF is added for the "Database Files (.db)" Grading
    Target — it is intentionally NOT a static dependency (so it does not run on
    every grade), so the per-verifier forwarding in the runners must add it back
    here, mirroring collect_helpers()."""
    helper_ids = list(eval_defn.helper_dependencies)
    if HelperIds.DB_DIFF not in helper_ids and _verifier_uses_db_target(
        verifier, eval_defn.eval_id
    ):
        helper_ids.append(HelperIds.DB_DIFF)
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
        # The database target delegates to db_diff_llm_tools (which uses the
        # DB_DIFF helper, not this text diff), so such a verifier needs no
        # SNAPSHOT_DIFF file content and must not force materialize-all either.
        # This is checked BEFORE the scope-safe gate so it also applies to
        # non-scope-safe delegating evals (e.g. multi_representation); otherwise
        # those would short-circuit to None and materialize binary .db content.
        if (
            defn_id in _DB_DIFF_DELEGATING_EVALS
            and expected_file_type == FileTypeCategory.DATABASE_FILES.value
        ):
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

    # Conditionally pull in the DB_DIFF helper for output_llm-family verifiers
    # configured with the "Database Files (.db)" Grading Target (which delegates
    # to db_diff_llm_tools_eval). It is intentionally not a static dependency —
    # see _DB_DIFF_DELEGATING_EVALS — so non-database grades skip the expensive
    # database diff entirely.
    if HelperIds.DB_DIFF not in helpers and needs_db_diff(verifiers, eval_configs):
        helpers[HelperIds.DB_DIFF] = HELPER_REGISTRY[HelperIds.DB_DIFF]

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
    # Files (.db)" Grading Target) intentionally does NOT force diff_all_types.
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
            if helper_defn.helper_impl_with_context is not None:
                helper_results[helper_id] = await helper_defn.helper_impl_with_context(
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
