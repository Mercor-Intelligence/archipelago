"""
Shared helper execution logic for grading runners.

Provides common functions for preparing and executing helpers needed by verifiers.
"""

import io
from typing import Any

from loguru import logger

from runner.evals.models import EvalConfig
from runner.evals.registry import EVAL_REGISTRY
from runner.helpers.models import HelperIds
from runner.helpers.registry import HELPER_REGISTRY, HelperDefn
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier


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
    if json_id_field is not None or diff_all_types:
        db_diff_kwargs = {}
        if json_id_field is not None:
            db_diff_kwargs["json_id_field"] = json_id_field
        if diff_all_types:
            db_diff_kwargs["diff_all_types"] = diff_all_types
        helper_kwargs[HelperIds.DB_DIFF] = db_diff_kwargs

    return helper_kwargs


async def execute_helpers(
    helpers: dict[HelperIds, HelperDefn],
    helper_kwargs: dict[HelperIds, dict[str, Any]],
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
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
