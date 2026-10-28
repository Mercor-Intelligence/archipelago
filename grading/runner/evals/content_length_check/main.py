"""Content Length Check eval - programmatically verifies content length requirements."""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.models import VerifierResult, VerifierResultStatus

WORD_PATTERN = re.compile(r"\b[\w']+\b", re.UNICODE)
SENTENCE_PATTERN = re.compile(r"[.!?]+")
PARAGRAPH_PATTERN = re.compile(r"\n\s*\n")

METRIC_OPTIONS = {
    "Word Count",
    "Character Count",
    "Character Count (no spaces)",
    "Sentence Count",
    "Paragraph Count",
    "Line Count",
}

COMPARISON_OPTIONS = {
    "Between (inclusive)",
    "At least",
    "At most",
    "Exactly",
    "Greater than",
    "Less than",
}

AGGREGATION_OPTIONS = {
    "sum",
    "max",
    "min",
    "avg",
    "per_artifact_all_must_pass",
    "per_artifact_any_can_pass",
}

CONTENT_SOURCE_OPTIONS = {
    "Created/Modified Files",
    "Final Answer (agent's text response)",
}


def _count_metric(metric_type: str, text: str) -> int:
    if metric_type == "Word Count":
        return len(WORD_PATTERN.findall(text))
    if metric_type == "Character Count":
        return len(text)
    if metric_type == "Character Count (no spaces)":
        return len(re.sub(r"\s+", "", text))
    if metric_type == "Sentence Count":
        if not text.strip():
            return 0
        segments = SENTENCE_PATTERN.split(text)
        sentences = [s for s in segments if s.strip()]
        return max(len(sentences), 1) if text.strip() else 0
    if metric_type == "Paragraph Count":
        if not text.strip():
            return 0
        return len([p for p in PARAGRAPH_PATTERN.split(text) if p.strip()])
    if metric_type == "Line Count":
        if not text:
            return 0
        return len(text.splitlines())
    raise ValueError(f"Unsupported metric_type: {metric_type}")


def _artifact_label(artifact: Any) -> str:
    label = artifact.path
    if artifact.index is not None:
        label = f"{label} (index {artifact.index + 1})"
    return label


def _filter_artifacts(
    artifacts: list[Any],
    target_file: str | None,
) -> list[Any]:
    """Filter artifacts by target filename, or return all if not specified."""
    if not target_file:
        return artifacts
    target_file = target_file.strip()
    if not target_file:
        return artifacts
    # Match by filename (case-insensitive, supports partial path match)
    target_lower = target_file.lower()
    return [
        artifact
        for artifact in artifacts
        if target_lower in artifact.path.lower()
        or artifact.path.lower().endswith(target_lower)
    ]


def _validate_required_fields(
    comparison_type: str,
    min_value: float | None,
    max_value: float | None,
    expected_value: float | None,
) -> str | None:
    if comparison_type == "Between (inclusive)":
        if min_value is None or max_value is None:
            return "min_value and max_value are required for Between (inclusive)"
        if min_value > max_value:
            return "min_value must be less than or equal to max_value"
    elif comparison_type in {"At least", "Greater than"}:
        if min_value is None:
            return "min_value is required for the selected comparison"
    elif comparison_type in {"At most", "Less than"}:
        if max_value is None:
            return "max_value is required for the selected comparison"
    elif comparison_type == "Exactly":
        if expected_value is None:
            return "expected_value is required for Exactly"
    return None


def _evaluate_comparison(
    comparison_type: str,
    value: float,
    min_value: float | None,
    max_value: float | None,
    expected_value: float | None,
    tolerance: float,
) -> tuple[bool, str]:
    # Note: _validate_comparison_params ensures required values are not None before this is called
    if comparison_type == "Between (inclusive)":
        passed = min_value <= value <= max_value  # pyright: ignore
        return passed, f"{min_value} <= {value} <= {max_value}"
    if comparison_type == "At least":
        passed = value >= min_value  # pyright: ignore
        return passed, f"{value} >= {min_value}"
    if comparison_type == "At most":
        passed = value <= max_value  # pyright: ignore
        return passed, f"{value} <= {max_value}"
    if comparison_type == "Exactly":
        delta = abs(value - expected_value)  # pyright: ignore
        passed = delta <= tolerance
        return passed, f"{value} ~= {expected_value} (±{tolerance})"
    if comparison_type == "Greater than":
        passed = value > min_value  # pyright: ignore
        return passed, f"{value} > {min_value}"
    if comparison_type == "Less than":
        passed = value < max_value  # pyright: ignore
        return passed, f"{value} < {max_value}"
    raise ValueError(f"Unsupported comparison_type: {comparison_type}")


async def content_length_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify content length requirements for artifacts or final answer."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    metric_type = verifier_values.get("metric_type", "Word Count")
    comparison_type = verifier_values.get("comparison_type", "Between (inclusive)")
    aggregation_mode = verifier_values.get("aggregation_mode", "sum")

    if metric_type not in METRIC_OPTIONS:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid metric_type: {metric_type}",
        )

    if comparison_type not in COMPARISON_OPTIONS:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid comparison_type: {comparison_type}",
        )

    if aggregation_mode not in AGGREGATION_OPTIONS:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid aggregation_mode: {aggregation_mode}",
        )

    min_value_raw = verifier_values.get("min_value")
    max_value_raw = verifier_values.get("max_value")
    expected_value_raw = verifier_values.get("expected_value")
    tolerance_raw = verifier_values.get("tolerance", 0)

    min_value = float(min_value_raw) if min_value_raw is not None else None
    max_value = float(max_value_raw) if max_value_raw is not None else None
    expected_value = (
        float(expected_value_raw) if expected_value_raw is not None else None
    )
    tolerance = float(tolerance_raw) if tolerance_raw is not None else 0.0

    validation_error = _validate_required_fields(
        comparison_type, min_value, max_value, expected_value
    )
    if validation_error:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=validation_error,
        )

    if not input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Helper results not available",
        )

    content_source = verifier_values.get("content_source", "Created/Modified Files")
    target_file = verifier_values.get("target_file")

    if content_source not in CONTENT_SOURCE_OPTIONS:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Invalid content_source: {content_source}",
        )

    sources: list[tuple[str, str]] = []
    evaluated_artifacts: list[str] = []

    # Final answer only mode
    if content_source == "Final Answer (agent's text response)":
        final_answer = input.helper_results.get(HelperIds.FINAL_ANSWER, "") or ""
        sources.append(("final_answer", str(final_answer)))
        evaluated_artifacts.append("final_answer")
    else:
        # Created/Modified Files mode
        diff_result = input.helper_results.get(HelperIds.SNAPSHOT_DIFF)
        if diff_result is None:
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message="Snapshot diff not available",
            )

        artifacts = extract_artifact_changes_from_diff(diff_result)
        artifacts = [a for a in artifacts if a.change_type != "deleted"]
        selected_artifacts = _filter_artifacts(artifacts, target_file)

        for artifact in selected_artifacts:
            content = (
                artifact.new_content
                if artifact.new_content is not None
                else (artifact.content_diff or "")
            )
            label = _artifact_label(artifact)
            sources.append((label, content))
            evaluated_artifacts.append(label)

    if not sources:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "passed": False,
                "actual_value": 0,
                "metric_type": metric_type,
                "comparison_type": comparison_type,
                "aggregation_mode": aggregation_mode,
                "evaluated_artifacts": "",
            },
            message="No content available to measure",
        )

    per_source_values = [_count_metric(metric_type, text) for _label, text in sources]

    if aggregation_mode == "sum":
        aggregate_value = sum(per_source_values)
    elif aggregation_mode == "max":
        aggregate_value = max(per_source_values)
    elif aggregation_mode == "min":
        aggregate_value = min(per_source_values)
    elif aggregation_mode == "avg":
        aggregate_value = sum(per_source_values) / len(per_source_values)
    elif aggregation_mode == "per_artifact_all_must_pass":
        aggregate_value = min(per_source_values)
    elif aggregation_mode == "per_artifact_any_can_pass":
        aggregate_value = max(per_source_values)
    else:
        aggregate_value = sum(per_source_values)

    per_source_passes = [
        _evaluate_comparison(
            comparison_type,
            float(value),
            min_value,
            max_value,
            expected_value,
            tolerance,
        )[0]
        for value in per_source_values
    ]

    if aggregation_mode == "per_artifact_all_must_pass":
        passed = all(per_source_passes)
    elif aggregation_mode == "per_artifact_any_can_pass":
        passed = any(per_source_passes)
    else:
        passed, _ = _evaluate_comparison(
            comparison_type,
            float(aggregate_value),
            min_value,
            max_value,
            expected_value,
            tolerance,
        )

    if aggregation_mode in ("per_artifact_all_must_pass", "per_artifact_any_can_pass"):
        artifact_details = []
        for i, (label, _) in enumerate(sources):
            value = per_source_values[i]
            result = "PASS" if per_source_passes[i] else "FAIL"
            _, summary = _evaluate_comparison(
                comparison_type,
                float(value),
                min_value,
                max_value,
                expected_value,
                tolerance,
            )
            artifact_details.append(f"{label}: {value} ({summary}) -> {result}")

        pass_count = sum(per_source_passes)
        total_count = len(per_source_passes)
        mode_desc = (
            "all must pass"
            if aggregation_mode == "per_artifact_all_must_pass"
            else "any can pass"
        )
        rationale = (
            f"{metric_type} | {mode_desc}: {pass_count}/{total_count} passed\n"
            + "\n".join(artifact_details)
        )
    else:
        comparison_summary = _evaluate_comparison(
            comparison_type,
            float(aggregate_value),
            min_value,
            max_value,
            expected_value,
            tolerance,
        )[1]

        rationale = (
            f"{metric_type}: {aggregate_value} | "
            f"comparison={comparison_type} ({comparison_summary}) | "
            f"aggregation={aggregation_mode}"
        )

    logger.info(
        f"[CONTENT_LENGTH_CHECK] task={task_id} | metric={metric_type} | "
        f"value={aggregate_value} | comparison={comparison_type} | "
        f"passed={passed} | aggregation={aggregation_mode}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "passed": passed,
            "actual_value": aggregate_value,
            "metric_type": metric_type,
            "comparison_type": comparison_type,
            "aggregation_mode": aggregation_mode,
            "min_value": min_value,
            "max_value": max_value,
            "expected_value": expected_value,
            "tolerance": tolerance,
            "per_source_values": per_source_values,
            "per_source_passes": per_source_passes,
            "evaluated_artifacts": ", ".join(evaluated_artifacts),
            "rationale": rationale,
        },
    )
