"""File diff check verifier."""

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.helpers.snapshot_diff.types import ArtifactChange
from runner.models import VerifierResult
from runner.utils.file_validation import matches_pattern, validate_file_pattern


def _filter_artifacts(
    artifacts: list[ArtifactChange],
    target_file: str | None,
    file_pattern: str | None,
) -> list[ArtifactChange]:
    """Filter artifacts by target filename or pattern.

    Args:
        artifacts: List of artifacts to filter
        target_file: Specific filename to match (already stripped, case-insensitive substring match)
        file_pattern: Glob pattern to match (already stripped)

    Returns:
        Filtered list of artifacts

    Note:
        Assumes target_file and file_pattern have already been validated and stripped.
        If both are specified, target_file takes precedence.
    """
    # Prefer target_file over file_pattern
    if target_file:
        target_lower = target_file.lower()
        return [
            artifact for artifact in artifacts if target_lower in artifact.path.lower()
        ]

    # Use file_pattern
    if file_pattern:
        return [
            artifact
            for artifact in artifacts
            if matches_pattern(artifact.path, file_pattern)
        ]

    # No filter specified (shouldn't reach here due to validation)
    return artifacts


def _filter_by_change_type(
    artifacts: list[ArtifactChange],
    change_types: list[str] | None,
) -> list[ArtifactChange]:
    """Filter artifacts by change type.

    If change_types is None or empty, defaults to ["created", "modified"].
    """
    if not change_types:
        change_types = ["created", "modified"]

    # Normalize change types to lowercase for comparison
    change_types_lower = [ct.lower().strip() for ct in change_types]

    return [
        artifact
        for artifact in artifacts
        if artifact.change_type.value.lower() in change_types_lower
    ]


async def file_diff_check_eval(input: EvalImplInput) -> VerifierResult:
    """Check if specific files or file patterns were modified.

    This is a programmatic verifier that checks the snapshot diff to see if
    certain files were changed during task execution.
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # Extract configuration
    file_pattern_input = verifier_values.get("file_pattern")
    change_types = verifier_values.get("change_types")

    # Strip whitespace
    if file_pattern_input:
        file_pattern_input = file_pattern_input.strip()

    # Validate input is provided
    if not file_pattern_input:
        raise ValueError(
            "File diff check requires 'file_pattern' (filename or glob pattern) "
            "to be specified in verifier configuration"
        )

    # Validate input format (validates both glob patterns and filenames)
    try:
        validate_file_pattern(file_pattern_input)
    except ValueError as e:
        raise ValueError(f"Invalid file pattern '{file_pattern_input}': {e}") from e

    # Determine if input is a glob pattern or a specific filename
    # If it contains glob wildcards (*, ?, [seq]), treat as pattern; otherwise as filename
    has_wildcards = any(char in file_pattern_input for char in ["*", "?", "["])

    if has_wildcards:
        # Treat as glob pattern
        target_file = None
        file_pattern = file_pattern_input
        match_type = "pattern"
    else:
        # Treat as specific filename (partial match)
        target_file = file_pattern_input
        file_pattern = None
        match_type = "filename"

    logger.info(
        f"[FILE_DIFF_CHECK] task={task_id} | "
        + f"input={file_pattern_input} | match_type={match_type} | "
        + f"change_types={change_types}"
    )

    try:
        # Get snapshot diff from helpers
        if not input.helper_results:
            raise ValueError("Missing helper results")

        diff_result = input.helper_results.get(HelperIds.SNAPSHOT_DIFF)
        if not diff_result:
            raise ValueError("Missing snapshot diff helper result")

        # Extract all changed artifacts from diff
        all_artifacts = extract_artifact_changes_from_diff(diff_result)

        logger.info(
            f"[FILE_DIFF_CHECK] task={task_id} | "
            + f"extracted {len(all_artifacts)} artifacts from diff"
        )

        # Filter by change type first
        filtered_by_type = _filter_by_change_type(all_artifacts, change_types)

        logger.info(
            f"[FILE_DIFF_CHECK] task={task_id} | "
            + f"after change_type filter: {len(filtered_by_type)} artifacts"
        )

        # Filter by filename or pattern
        selected_artifacts = _filter_artifacts(
            filtered_by_type,
            target_file,
            file_pattern,
        )

        logger.info(
            f"[FILE_DIFF_CHECK] task={task_id} | "
            + f"after file filter: {len(selected_artifacts)} artifacts"
        )

        # Check if any matching files were found
        passed = len(selected_artifacts) > 0

        # Build rationale
        if passed:
            artifact_names = [a.path for a in selected_artifacts]
            change_type_summary = {}
            for artifact in selected_artifacts:
                ct = artifact.change_type.value
                change_type_summary[ct] = change_type_summary.get(ct, 0) + 1

            change_summary = ", ".join(
                f"{count} {ctype}"
                for ctype, count in sorted(change_type_summary.items())
            )

            rationale = (
                f"Found {len(selected_artifacts)} file(s) matching '{file_pattern_input}': "
                f"{', '.join(artifact_names)}. Changes: {change_summary}."
            )
        else:
            rationale = (
                f"No files matching '{file_pattern_input}' were found in the snapshot diff. "
                f"Total artifacts checked: {len(filtered_by_type)}."
            )

        evaluated_artifacts = ", ".join(a.path for a in selected_artifacts)

        logger.info(
            f"[FILE_DIFF_CHECK] task={task_id} | "
            + f"passed={passed} | evaluated_artifacts={evaluated_artifacts}"
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=1.0 if passed else 0.0,
            verifier_result_values={
                "passed": passed,
                "evaluated_artifacts": evaluated_artifacts,
                "rationale": rationale,
            },
        )

    except Exception as e:
        error_msg = f"File diff check failed: {str(e)}"
        logger.error(f"[FILE_DIFF_CHECK] task={task_id} | {error_msg}")
        raise ValueError(error_msg) from e
