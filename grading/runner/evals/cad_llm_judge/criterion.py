"""Per-criterion CAD judge (eval id ``cad_llm_judge_criterion``).

Same evidence, probes and judge as ``cad_llm_judge``, shaped like an
``output_llm_weighted`` rubric row: one statement, true or false. The row's
weight is applied by the world's scoring method and never reaches the judge.
"""

from __future__ import annotations

from typing import Any

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult
from runner.utils.criterion_metadata import CRITERION_DEFINITIONS_KEY
from runner.utils.ungradeable import (
    REASON_KEY,
    SOFT_CAUSES_FIELD,
    UNGRADEABLE_CAUSE_KEY,
    UNGRADEABLE_KEY,
    is_no_deliverable,
    is_ungradeable,
    ungradeable_result,
)

from .main import cad_llm_judge_eval

CRITERION_ID = "criterion"
DECLINED_CAUSE = "judge_declined"
_RESERVED = frozenset(
    {UNGRADEABLE_KEY, UNGRADEABLE_CAUSE_KEY, REASON_KEY, "score", "score_100"}
)

# Row fields forwarded to the CAD judge; weights and labels stay behind.
ROW_FIELDS = ("criteria", "criteria_explanation", "artifacts_to_reference")

# cad_llm_judge's per-row knobs, set once per world on this eval's config.
WORLD_FIELDS = (
    "expected_cad_files",
    "expected_report_files",
    "cad_base_path",
    "render_views",
    "kicad_digest",
    "assembly_digest",
    "report_units",
    "expected_units",
    "orientation_tolerance_deg",
    "max_interference_pairs",
    "density_g_cm3",
    "report_mass",
    "extended_probes",
)


def _judge_values(input: EvalImplInput) -> dict[str, Any]:  # noqa: A002
    """The CAD judge's verifier values: one statement plus world knobs."""
    row = input.verifier.verifier_values or {}
    world = input.eval_config.eval_config_values or {}
    values: dict[str, Any] = {k: world[k] for k in WORLD_FIELDS if k in world}
    values.update({k: row[k] for k in ROW_FIELDS if k in row})
    criteria = str(row.get("criteria") or "").strip()
    if criteria:
        values[CRITERION_DEFINITIONS_KEY] = [
            {"criterion_id": CRITERION_ID, "criterion": criteria, "critical": False}
        ]
    return values


def _may_be_penalty(input: EvalImplInput) -> bool:  # noqa: A002
    """Whether any row value a scoring config could read as weight is negative."""
    v = input.verifier
    values = [
        *v.verifier_values.values(),
        *(v.verifier_custom_field_values or {}).values(),
    ]
    for raw in values:
        if isinstance(raw, bool):
            continue
        try:
            if float(raw) < 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _without_soft_declines(input: EvalImplInput) -> EvalImplInput:  # noqa: A002
    """The input with judge refusals voiding; agent-caused soft causes stay."""
    world = dict(input.eval_config.eval_config_values or {})
    soft = world.get(SOFT_CAUSES_FIELD)
    if isinstance(soft, list):
        world[SOFT_CAUSES_FIELD] = [c for c in soft if c != DECLINED_CAUSE]
    eval_config = input.eval_config.model_copy(update={"eval_config_values": world})
    return input.model_copy(update={"eval_config": eval_config})


async def cad_llm_judge_criterion_eval(input: EvalImplInput) -> VerifierResult:  # noqa: A002
    """Judge one CAD statement: 1.0 when true of the deliverable, else 0.0."""
    # Evals cannot see the scoring weight selector, so any negative candidate
    # counts: on a penalty row a soft 0.0 means "mistake absent".
    if _may_be_penalty(input):
        input = _without_soft_declines(input)  # noqa: A001
    verifier = input.verifier.model_copy(
        update={"verifier_values": _judge_values(input)}
    )
    result = await cad_llm_judge_eval(input.model_copy(update={"verifier": verifier}))
    values = dict(result.verifier_result_values)
    if "judge_grade" in values:
        values["criteria_ledger"] = values.pop("judge_grade")
    # A judge refusal must void the row, never score the agent 0.0.
    refused = values.get("uncategorized_refusals") or (
        values.get(UNGRADEABLE_CAUSE_KEY) == "config_error"
        and values.get("kernel_only_aspects")
    )
    if refused:
        return ungradeable_result(
            input,
            "the CAD judge could not decide this statement from the digests, "
            "measured geometry and renders; rephrase it as something the files "
            "show, or score judge_declined as zero on the eval config",
            cause=DECLINED_CAUSE,
            values={k: v for k, v in values.items() if k not in _RESERVED},
        )
    if is_ungradeable(result) or is_no_deliverable(result):
        return result
    met = result.score == 1.0
    values["judge_grade"] = "pass" if met else "fail"
    values["met"] = met
    return result.model_copy(update={"verifier_result_values": values})
