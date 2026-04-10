"""Pattern Match Check eval - verifies presence of word/phrase patterns in content."""

import os
import re
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.models import VerifierResult
from runner.utils.file_extraction import FileExtractionService
from runner.utils.file_validation import matches_pattern, validate_file_pattern


def _validate_regex_pattern(pattern: str) -> None:
    """Validate that a string is a valid regex pattern."""
    if not pattern or not pattern.strip():
        raise ValueError("Regex pattern cannot be empty")

    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {str(e)}") from e


def _find_matching_files(zip_file: zipfile.ZipFile, file_pattern: str) -> list[str]:
    """Find files in zip matching the pattern.

    Args:
        zip_file: ZipFile to search in
        file_pattern: Glob pattern to match (e.g. "*.txt", "src/**/*.py")

    Returns:
        List of unique file paths matching the pattern (deduplicated)
    """
    matching = []
    seen = set()

    for name in zip_file.namelist():
        if name.endswith("/"):  # Skip directories
            continue

        # Normalize path for matching
        normalized = name.replace("\\", "/").strip("/")

        if matches_pattern(normalized, file_pattern):
            # Deduplicate: only add if we haven't seen this path before
            if normalized not in seen:
                matching.append(name)
                seen.add(normalized)

    return matching


def _check_pattern(
    content: str,
    pattern: str,
    case_sensitive: bool,
) -> bool:
    """
    Check if pattern exists in content.

    Args:
        content: Text content to search
        pattern: Regex pattern to search for
        case_sensitive: Whether to use case-sensitive matching

    Returns:
        True if pattern is found, False otherwise
    """
    flags = 0 if case_sensitive else re.IGNORECASE

    try:
        regex = re.compile(pattern, flags)
        return regex.search(content) is not None
    except re.error as e:
        logger.error(f"Invalid regex pattern '{pattern}': {e}")
        return False


async def pattern_match_check_eval(input: EvalImplInput) -> VerifierResult:
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # Extract configuration
    pattern = verifier_values.get("pattern") or ""
    search_target = verifier_values.get("search_target", "final_answer")
    file_pattern = (verifier_values.get("file_pattern") or "*.txt").strip()
    case_sensitive = verifier_values.get("case_sensitive", False)

    # Validate pattern
    pattern = pattern.strip()
    if not pattern:
        raise ValueError("Pattern must be specified")

    try:
        _validate_regex_pattern(pattern)
    except ValueError as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}") from e

    # Validate file_pattern if search_target is "files"
    if search_target == "files":
        try:
            validate_file_pattern(file_pattern)
        except ValueError as e:
            raise ValueError(f"Invalid file pattern '{file_pattern}': {e}") from e

    logger.info(
        f"[PATTERN_MATCH] task={task_id} | target={search_target} | "
        f"pattern={pattern} | case_sensitive={case_sensitive}"
    )

    try:
        if search_target == "final_answer":
            # Search in final answer using helper
            if not input.helper_results:
                raise ValueError("Missing helper results")

            final_answer = input.helper_results.get(HelperIds.FINAL_ANSWER) or ""
            final_answer_str = str(final_answer) if final_answer else ""

            logger.info(
                f"[PATTERN_MATCH] task={task_id} | Searching in final answer "
                f"({len(final_answer_str)} characters)"
            )

            passed = _check_pattern(final_answer_str, pattern, case_sensitive)

            rationale = (
                "Pattern found in final answer"
                if passed
                else "Pattern not found in final answer"
            )

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0 if passed else 0.0,
                verifier_result_values={
                    "passed": passed,
                    "evaluated_artifacts": "final_answer",
                    "rationale": rationale,
                },
            )

        elif search_target == "files":
            # Build a map of pre-extracted content from snapshot diff (if available)
            # For multi-part documents (Excel, PowerPoint), merge all sheets/slides
            content_map = {}
            if input.helper_results:
                diff_result = input.helper_results.get(HelperIds.SNAPSHOT_DIFF)
                if diff_result:
                    all_artifacts = extract_artifact_changes_from_diff(diff_result)

                    # Group artifacts by path and merge content for multi-part docs
                    path_contents = defaultdict(list)
                    for artifact in all_artifacts:
                        if artifact.new_content:
                            path_contents[artifact.path].append(artifact.new_content)

                    # Merge all content for each path
                    content_map = {
                        path: "\n\n".join(contents)
                        for path, contents in path_contents.items()
                    }

                    logger.info(
                        f"[PATTERN_MATCH] task={task_id} | "
                        f"Found {len(content_map)} pre-extracted files from snapshot diff"
                    )

            # Read all matching files from final snapshot
            input.final_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.final_snapshot_bytes, "r") as zf:
                matching_files = _find_matching_files(zf, file_pattern)

                logger.info(
                    f"[PATTERN_MATCH] task={task_id} | "
                    f"Found {len(matching_files)} files matching pattern '{file_pattern}': "
                    f"{matching_files}"
                )

                file_results = []
                any_file_passed = False
                extraction_service = FileExtractionService()

                for file_path in matching_files:
                    # Try to use pre-extracted content first
                    normalized_path = file_path.replace("\\", "/").strip("/")
                    if normalized_path in content_map:
                        content = content_map[normalized_path]
                        logger.info(
                            f"[PATTERN_MATCH] task={task_id} | Using pre-extracted content for {file_path} "
                            f"({len(content)} characters)"
                        )
                    else:
                        # Extract content using FileExtractionService
                        ext = Path(file_path).suffix.lower()

                        # Write file to temp directory for extraction
                        tmp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(
                                suffix=ext, delete=False
                            ) as tmp_file:
                                tmp_file.write(zf.read(file_path))
                                tmp_path = Path(tmp_file.name)

                            extracted = await extraction_service.extract_from_file(
                                tmp_path,
                                include_images=False,
                                prefer_reducto=True,
                            )

                            if extracted and extracted.text:
                                content = extracted.text
                                logger.info(
                                    f"[PATTERN_MATCH] task={task_id} | Extracted {len(content)} chars from {file_path} "
                                    f"using {extracted.extraction_method}"
                                )
                            else:
                                content = ""
                        finally:
                            if tmp_path and tmp_path.exists():
                                os.unlink(tmp_path)

                    passed = _check_pattern(content, pattern, case_sensitive)

                    file_results.append({"file": file_path, "passed": passed})

                    if passed:
                        any_file_passed = True

            # Get list of files that matched
            passed_files = [
                fr["file"] for fr in file_results if fr.get("passed", False)
            ]
            evaluated_artifacts = ", ".join(passed_files) if passed_files else ""

            # Generate rationale message
            if len(file_results) == 0:
                rationale = (
                    f"No files matching pattern '{file_pattern}' found in output"
                )
            elif any_file_passed:
                rationale = f"Pattern found in files: {', '.join(passed_files[:3])}"
            else:
                rationale = f"Pattern not found in any files matching '{file_pattern}'"

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=1.0 if any_file_passed else 0.0,
                verifier_result_values={
                    "passed": any_file_passed,
                    "evaluated_artifacts": evaluated_artifacts,
                    "rationale": rationale,
                },
            )

        else:
            raise ValueError(f"Invalid search_target: {search_target}")

    except Exception as e:
        error_msg = f"Pattern match check failed: {str(e)}"
        logger.error(f"[PATTERN_MATCH] task={task_id} | {error_msg}")
        raise ValueError(error_msg) from e
