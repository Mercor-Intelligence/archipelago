"""Shared file validation utilities for evals.

This module provides common validation and pattern matching functions
used by multiple eval implementations (e.g., file_diff_check, pattern_match_check).
"""

from pathlib import PurePath

# Common file extensions that are typically used in tasks
ALLOWED_FILE_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".html",
    ".json",
    ".jsx",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".py",
    ".txt",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".zip",
}


def validate_file_pattern(pattern: str) -> None:
    """
    Validate file pattern (glob pattern or exact filename).

    Automatically detects whether the pattern contains wildcards and applies
    appropriate validation rules.

    This is a unified validation function that combines:
    - _validate_glob_pattern() from file_diff_check
    - _validate_filename() from file_diff_check
    - _validate_file_pattern() from pattern_match_check

    Args:
        pattern: File pattern to validate (e.g., '*.txt', 'report.docx', 'src/**/*.py')

    Raises:
        ValueError: If pattern is invalid

    Examples:
        >>> validate_file_pattern("report.txt")  # Valid filename
        >>> validate_file_pattern("*.txt")        # Valid glob pattern
        >>> validate_file_pattern("src/**/*.py")  # Valid recursive glob
        >>> validate_file_pattern("../secret.txt")  # Raises ValueError (path traversal)
        >>> validate_file_pattern("test.invalid")  # Raises ValueError (invalid extension)
    """
    if not pattern or not pattern.strip():
        raise ValueError("File pattern cannot be empty")

    # Check for path traversal (security check for both types)
    if ".." in pattern:
        raise ValueError(
            "File pattern contains '..' which is not supported. "
            "Path traversal is not allowed. Use '**' for recursive directory matching instead."
        )

    # Check for invalid characters (security check for both types)
    invalid_chars = '<>"|'
    for char in invalid_chars:
        if char in pattern:
            raise ValueError(
                f"File pattern contains invalid character '{char}'. "
                f"Valid patterns use alphanumeric characters, /, _, -, ., *, ?, and **"
            )

    # Detect if this is a glob pattern or exact filename
    has_wildcards = any(char in pattern for char in ["*", "?", "["])

    if has_wildcards:
        # Glob pattern validation
        if "***" in pattern:
            raise ValueError(
                "Glob pattern contains invalid wildcard sequence '***'. "
                "Use '*' for any characters or '**' for any directories."
            )

        # Validate ** usage
        if "**" in pattern:
            parts = pattern.split("/")
            for part in parts:
                if "**" in part and part != "**":
                    raise ValueError(
                        f"Invalid use of '**' in pattern component '{part}'. "
                        "'**' must be a standalone path component (e.g., 'src/**/file.py' not 'src/**file.py')"
                    )
    else:
        # Exact filename validation - must have recognized extension
        filename_lower = pattern.lower()
        has_valid_extension = any(
            filename_lower.endswith(ext) for ext in ALLOWED_FILE_EXTENSIONS
        )

        if not has_valid_extension:
            raise ValueError(
                f"Filename '{pattern}' must have a recognized file extension. "
                f"Common extensions: .txt, .py, .docx, .xlsx, .csv, .json, .md, .pdf, etc."
            )


def matches_pattern(path: str, pattern: str) -> bool:
    """Check if a path matches a glob pattern.

    Supports Unix-style glob patterns:
    - * matches any sequence of characters
    - ? matches any single character
    - [seq] matches any character in seq
    - [!seq] matches any character not in seq
    - ** matches any number of directories

    Uses PurePath.match() which properly supports ** patterns.

    Args:
        path: File path to check (e.g., "filesystem/src/app.py")
        pattern: Glob pattern to match against (e.g., "*.py", "src/**/*.py")

    Returns:
        True if path matches pattern, False otherwise

    Examples:
        >>> matches_pattern("filesystem/report.txt", "*.txt")
        True
        >>> matches_pattern("filesystem/src/app.py", "**/*.py")
        True
        >>> matches_pattern("filesystem/data.json", "*.txt")
        False
    """
    # Remove filesystem prefix if present
    clean_path = path
    if clean_path.startswith("filesystem/"):
        clean_path = clean_path[len("filesystem/") :]

    # Use PurePath.match() which handles ** correctly
    # PurePath.match() is case-sensitive by default on Unix-like systems
    return PurePath(clean_path).match(pattern)
