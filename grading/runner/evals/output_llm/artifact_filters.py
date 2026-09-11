"""
Constants and utilities for artifact filtering in verifiers.

These constants are used to:
1. Populate UI dropdowns for expected file types, change types, and artifact types
2. Filter artifacts before LLM evaluation in the grading pipeline
"""

from enum import StrEnum
from typing import Any

from loguru import logger

from runner.helpers.snapshot_diff.constants import PURE_IMAGE_EXTENSIONS

# =============================================================================
# File Type Categories
# =============================================================================
# These are high-level categories that map to specific file extensions


# @apg_file_type_extensions:start
class FileTypeCategory(StrEnum):
    """High-level file type categories for UI selection."""

    # Special: No files - only evaluate final answer text
    FINAL_ANSWER_ONLY = "Final Answer Only (No Files)"

    # Documents
    WORD_DOCUMENTS = "Word Documents (.docx, .doc, .odt)"
    TEXT_FILES = "Text Files (.txt)"
    PDF_DOCUMENTS = "PDF Documents (.pdf)"
    SPREADSHEETS = "Spreadsheets (.xlsx, .xls, .xlsm, .ods)"
    PRESENTATIONS = "Presentations (.pptx, .ppt, .odp)"

    # Code & Text
    PYTHON_FILES = "Python Files (.py)"
    JAVASCRIPT_FILES = "JavaScript/TypeScript (.js, .ts, .jsx, .tsx)"
    MARKDOWN = "Markdown (.md)"
    JSON_YAML = "JSON/YAML (.json, .yaml, .yml)"

    # Databases — never graded as text artifacts (a .db is a binary SQLite
    # file). Both targets delegate to a tool-augmented judge: DATABASE_FILES
    # judges the FINAL database state (db_state_llm_tools — no baseline, no
    # DB_DIFF helper), DATABASE_FILES_DIFF judges the baseline-vs-final diff
    # (db_diff_llm_tools, what DATABASE_FILES used to do). See llm_judge_eval
    # delegation.
    DATABASE_FILES = "Database Files (.db)"
    DATABASE_FILES_DIFF = "Database Files – Diff (.db)"

    # Images (limited to Gemini-supported formats)
    IMAGES = "Images (.png, .jpg, .jpeg, .webp)"

    ANY_FILES = "All output (modified files and final message in console)"


# Map categories to actual file extensions
# Special values:
#   - FINAL_ANSWER_ONLY: None means filter out ALL files
#   - ANY_FILES: Empty list means no filtering (allow all)
FILE_TYPE_CATEGORY_TO_EXTENSIONS: dict[FileTypeCategory, list[str] | None] = {
    FileTypeCategory.FINAL_ANSWER_ONLY: None,  # None means filter out ALL files
    FileTypeCategory.WORD_DOCUMENTS: [
        ".docx",
        ".doc",
        ".odt",
    ],
    FileTypeCategory.TEXT_FILES: [".txt"],
    FileTypeCategory.PDF_DOCUMENTS: [".pdf"],
    FileTypeCategory.SPREADSHEETS: [".xlsx", ".xls", ".xlsm", ".ods"],
    FileTypeCategory.PRESENTATIONS: [".pptx", ".ppt", ".odp"],
    FileTypeCategory.PYTHON_FILES: [".py"],
    FileTypeCategory.JAVASCRIPT_FILES: [".js", ".ts", ".jsx", ".tsx"],
    FileTypeCategory.MARKDOWN: [".md"],
    FileTypeCategory.JSON_YAML: [".json", ".yaml", ".yml"],
    FileTypeCategory.DATABASE_FILES: [".db"],
    FileTypeCategory.DATABASE_FILES_DIFF: [".db"],
    FileTypeCategory.IMAGES: list(
        PURE_IMAGE_EXTENSIONS
    ),  # Use constant for all image types
    FileTypeCategory.ANY_FILES: [],  # Empty list means no filtering
}
# @apg_file_type_extensions:end


# =============================================================================
# Helper Functions
# =============================================================================


def get_extensions_for_category(category: FileTypeCategory) -> list[str] | None:
    """
    Get the list of file extensions for a given file type category.

    Args:
        category: The file type category

    Returns:
        - None for FINAL_ANSWER_ONLY (filter out ALL files)
        - Empty list for ANY_FILES (no filtering, allow all)
        - List of extensions for specific file types
    """
    return FILE_TYPE_CATEGORY_TO_EXTENSIONS.get(category, [])


def get_file_type_options() -> list[str]:
    """
    Get all available file type options for UI dropdown.

    Returns:
        List of file type category display names
    """
    return [category.value for category in FileTypeCategory]


# =============================================================================
# Artifact Filtering Utilities
# =============================================================================


def is_valid_file_type(filter_value: str | None) -> bool:
    """
    Check if filter_value is a valid, recognized file type category.

    Strict equality against FileTypeCategory values. The Grading Target dropdown
    options (registry.py) are kept exactly in sync with this enum, so every
    user-selectable value matches here; a guard test enforces that sync. Returns
    False for None, empty, or unrecognized values.
    """
    if not filter_value:
        return False
    return filter_value in {category.value for category in FileTypeCategory}


# Former spellings of ANY_FILES, still held by legacy verifiers. Defined once so
# resolve_grading_target and should_skip_filter cannot disagree about what counts
# as an allow-all target: a disagreement there means a correctly-configured
# criterion gets reported as degraded.
LEGACY_ALLOW_ALL_TARGETS: frozenset[str] = frozenset({"Any File Type", "any"})


def resolve_grading_target(
    raw_value: str | None,
    *,
    task_id: str | None = None,
) -> tuple[str, str | None]:
    """Resolve a stored grading target to a usable one, reporting degradation.

    Returns ``(resolved_target, unresolved_raw)``. ``unresolved_raw`` is None on
    the happy path and carries the offending string when the stored value could
    not be resolved — callers persist it into ``verifier_result_values`` so the
    degradation is visible on the grade itself rather than only in a log line.

    An unrecognized value still resolves to ANY_FILES (grade everything, no
    filtering) rather than raising: hard-failing here would break every grading
    run that inherited a legacy value. But because the target widens, the
    missing-file-type auto-fail can no longer fire — a criterion scoped to
    ".xlsx" is silently graded against all output. That is the whole reason the
    caller records the flag.

    Shared by the three file-grading judges (output_llm, its multi-representation
    and browsing-check variants) so the fallback behaves identically in each.
    """
    if not raw_value:
        # Absent / None / "" is a legitimate "no restriction" configuration.
        return FileTypeCategory.ANY_FILES.value, None
    if is_valid_file_type(raw_value):
        return raw_value, None
    if raw_value in LEGACY_ALLOW_ALL_TARGETS:
        # Former spellings of ANY_FILES. These resolve to exactly what they
        # asked for, so they are not a degradation and must not be flagged as
        # one — the whole value of the flag is that it means "enforcement was
        # lost", and allow-all criteria never had any to lose.
        return FileTypeCategory.ANY_FILES.value, None
    logger.warning(
        f"[JUDGE][GRADER] task={task_id} | Unresolvable grading target "
        f"(expected_file_type): {raw_value!r}. Grading ALL output unfiltered and "
        "skipping the missing-file-type auto-fail. Valid options are: "
        f"{[c.value for c in FileTypeCategory]}"
    )
    return FileTypeCategory.ANY_FILES.value, raw_value


def should_skip_filter(filter_value: str | None) -> bool:
    """
    Check if filter should be skipped (None, empty, or special 'any' values).

    Special values:
    - "any"/"All output (modified files and final message in console)" → skip filtering (allow all)
    - "Final Answer Only (No Files)" → do NOT skip (we need to filter out all)
    """
    if not filter_value:
        return True

    # Only values that mean "allow all" should skip filtering
    return filter_value in {
        FileTypeCategory.ANY_FILES.value,
        *LEGACY_ALLOW_ALL_TARGETS,
    }


def should_filter_all_files(filter_value: str | None) -> bool:
    """
    Check if ALL files should be filtered out (Final Answer Only mode).

    When True, no artifacts should be passed to the LLM - only the final answer text.
    """
    if not filter_value:
        return False

    return filter_value == FileTypeCategory.FINAL_ANSWER_ONLY.value


def convert_file_types_to_extensions(file_type: str | None) -> list[str] | None:
    """
    Convert file type category to extensions.

    Args:
        file_type: File type category (string), or None

    Returns:
        - None for FINAL_ANSWER_ONLY (filter out ALL files)
        - Empty list for ANY_FILES, None input, or invalid values (no filtering, allow all)
        - List of extensions for specific file types
    """
    if not file_type:
        return []

    # Backwards compatibility: handle old "Any File Type" value
    if file_type == "Any File Type":
        return []

    # Strict lookup by exact category value
    try:
        category = FileTypeCategory(file_type)
    except ValueError:
        # Unknown value - log warning and default to no filtering
        # Note: Primary validation should happen upstream (in main.py), but this
        # provides a fallback in case this function is called from other places
        logger.warning(
            f"[ARTIFACT_FILTER] Invalid expected_file_type value: '{file_type}', "
            "defaulting to 'All output' (no filtering). "
            f"Valid options are: {[c.value for c in FileTypeCategory]}"
        )
        return []

    return get_extensions_for_category(category)


def get_file_extension(path: str) -> str | None:
    """Extract lowercase file extension from path, or None if no extension."""
    if "." not in path:
        return None
    return "." + path.rsplit(".", 1)[1].lower()


def filter_artifacts_to_expected_outputs(
    artifacts: list[Any],
    expected_output_files: list[str],
) -> list[Any]:
    """Keep only artifacts named by the verifier's expected output files.

    ``expected_output_files`` holds plain filenames (or relative paths) from
    the task's expected-outputs list, injected into verifier_values by the
    grading dispatch. Artifact paths are workspace-relative, so a name matches
    the full path or any path ending in ``/<name>`` — the native analog of GDM
    docker-world grading, where these files become the judge's only file
    dependencies.
    """
    names = {n.strip().lstrip("/") for n in expected_output_files if n and n.strip()}
    if not names:
        return artifacts
    return [
        artifact
        for artifact in artifacts
        if (path := artifact.path.lstrip("/")) in names
        or any(path.endswith(f"/{name}") for name in names)
    ]


def artifact_matches_filters(
    artifact: Any,
    allowed_extensions: list[str] | None,
) -> bool:
    """
    Check if artifact matches file type filter.

    Uses truthiness checks to handle both None and empty lists correctly.
    Empty lists are treated as "no filter" (allow all).
    """
    # File type filter
    if allowed_extensions:  # Checks for non-empty list
        file_ext = get_file_extension(artifact.path)
        if file_ext not in allowed_extensions:
            return False

    return True
