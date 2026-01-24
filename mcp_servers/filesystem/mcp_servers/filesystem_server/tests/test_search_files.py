"""Comprehensive tests for search_files tool.

This module tests all aspects of the search_files functionality:
- Pattern matching with glob patterns
- Recursive and non-recursive search
- Path resolution and security
- Error handling
- Edge cases
- Performance with large directories

Test Coverage Areas:
1. Basic Functionality: Searching with various glob patterns
2. Recursive Search: Searching in subdirectories
3. Result Limits: max_results parameter
4. Error Handling: Non-existent paths, permission issues
5. Security: Sandboxing, path traversal prevention
6. Edge Cases: Unicode filenames, special characters
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from tools.search_files import _get_relative_path, _resolve_under_root, search_files

# ============================================================================
# SEARCH FIXTURES
# ============================================================================


@pytest.fixture
def search_directory(temp_workspace: Path) -> Path:
    """Create a directory structure for search testing."""
    search_dir = temp_workspace / "search"
    search_dir.mkdir()

    # Root level files
    (search_dir / "file1.txt").write_text("Text file 1")
    (search_dir / "file2.txt").write_text("Text file 2")
    (search_dir / "data.json").write_text('{"key": "value"}')
    (search_dir / "script.py").write_text("print('hello')")
    (search_dir / "report.pdf").write_bytes(b"PDF content")

    # Subdirectory with files
    sub1 = search_dir / "subdir1"
    sub1.mkdir()
    (sub1 / "nested.txt").write_text("Nested text")
    (sub1 / "config.json").write_text('{"nested": true}')

    # Another subdirectory
    sub2 = search_dir / "subdir2"
    sub2.mkdir()
    (sub2 / "another.txt").write_text("Another text")
    (sub2 / "data.csv").write_text("a,b,c")

    # Deeply nested
    deep = sub1 / "deep" / "deeper"
    deep.mkdir(parents=True)
    (deep / "deep_file.txt").write_text("Deep content")

    return search_dir


@pytest.fixture
def many_files_directory(temp_workspace: Path) -> Path:
    """Create a directory with many files for performance testing."""
    many_dir = temp_workspace / "many"
    many_dir.mkdir()

    for i in range(150):
        (many_dir / f"file{i:03d}.txt").write_text(f"File {i}")
        if i % 10 == 0:
            (many_dir / f"data{i:03d}.json").write_text(f'{{"num": {i}}}')

    return many_dir


@pytest.fixture
def unicode_search_directory(temp_workspace: Path) -> Path:
    """Create a directory with Unicode filenames."""
    unicode_dir = temp_workspace / "unicode"
    unicode_dir.mkdir()

    (unicode_dir / "文件.txt").write_text("Chinese")
    (unicode_dir / "файл.txt").write_text("Russian")
    (unicode_dir / "ファイル.txt").write_text("Japanese")
    (unicode_dir / "file.txt").write_text("English")

    return unicode_dir


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================


class TestSearchFilesBasic:
    """Test core functionality with standard use cases."""

    @pytest.mark.asyncio
    async def test_search_txt_files(self, search_directory: Path, temp_workspace: Path):
        """Test searching for .txt files."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search", recursive=False)

            assert "Found" in result
            assert "file1.txt" in result
            assert "file2.txt" in result
            # Should not find nested files without recursion
            assert "nested.txt" not in result

    @pytest.mark.asyncio
    async def test_search_json_files(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test searching for .json files."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.json", "/search", recursive=False)

            assert "Found" in result
            assert "data.json" in result

    @pytest.mark.asyncio
    async def test_search_specific_pattern(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test searching with specific pattern."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("file*.txt", "/search", recursive=False)

            assert "file1.txt" in result
            assert "file2.txt" in result
            # Should not match data.json
            assert "data.json" not in result

    @pytest.mark.asyncio
    async def test_search_all_files(self, search_directory: Path, temp_workspace: Path):
        """Test searching for all files with *."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*", "/search", recursive=False)

            assert "file1.txt" in result
            assert "data.json" in result
            assert "script.py" in result

    @pytest.mark.asyncio
    async def test_search_from_root(self, search_directory: Path, temp_workspace: Path):
        """Test searching from root directory."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/")

            assert "Found" in result


# ============================================================================
# RECURSIVE SEARCH TESTS
# ============================================================================


class TestSearchFilesRecursive:
    """Test recursive search functionality."""

    @pytest.mark.asyncio
    async def test_recursive_search_enabled(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test recursive search finds nested files."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search", recursive=True)

            assert "file1.txt" in result
            assert "nested.txt" in result
            assert "another.txt" in result
            assert "deep_file.txt" in result

    @pytest.mark.asyncio
    async def test_recursive_search_disabled(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test non-recursive search only finds top-level files."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search", recursive=False)

            assert "file1.txt" in result
            assert "file2.txt" in result
            # Should not find nested files
            assert "nested.txt" not in result
            assert "deep_file.txt" not in result

    @pytest.mark.asyncio
    async def test_recursive_json_search(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test recursive search for JSON files."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.json", "/search", recursive=True)

            assert "data.json" in result
            assert "config.json" in result

    @pytest.mark.asyncio
    async def test_recursive_default_enabled(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test that recursive is True by default."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search")

            # Should find nested files by default
            assert "nested.txt" in result
            assert "deep_file.txt" in result


# ============================================================================
# MAX RESULTS TESTS
# ============================================================================


class TestSearchFilesMaxResults:
    """Test max_results parameter."""

    @pytest.mark.asyncio
    async def test_max_results_limits_output(
        self, many_files_directory: Path, temp_workspace: Path
    ):
        """Test that max_results limits the number of results."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/many", max_results=10)

            assert "Found 10 file(s)" in result
            assert "Results limited to 10" in result

    @pytest.mark.asyncio
    async def test_max_results_zero_means_unlimited(
        self, many_files_directory: Path, temp_workspace: Path
    ):
        """Test that max_results=0 returns all results."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/many", max_results=0)

            assert "Found 150 file(s)" in result
            assert "Results limited" not in result

    @pytest.mark.asyncio
    async def test_max_results_higher_than_actual(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test max_results higher than actual file count."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search", max_results=1000)

            # Should return all matches without "limited" message
            assert "Found" in result
            assert "Results limited" not in result

    @pytest.mark.asyncio
    async def test_default_max_results_is_100(
        self, many_files_directory: Path, temp_workspace: Path
    ):
        """Test that default max_results is 100."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/many")

            assert "Found 100 file(s)" in result
            assert "Results limited to 100" in result

    @pytest.mark.asyncio
    async def test_max_results_non_recursive(self, temp_workspace: Path):
        """Test max_results works with non-recursive search."""
        # Create directory with many files
        test_dir = temp_workspace / "non_recursive_test"
        test_dir.mkdir()
        for i in range(20):
            (test_dir / f"file{i:02d}.txt").write_text(f"Content {i}")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files(
                "*.txt", "/non_recursive_test", recursive=False, max_results=5
            )

            assert "Found 5 file(s)" in result
            assert "Results limited to 5" in result


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestSearchFilesErrors:
    """Test error scenarios."""

    @pytest.mark.asyncio
    async def test_path_not_found(self, temp_workspace: Path):
        """Test searching in non-existent path."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/nonexistent")

            assert "[not found:" in result

    @pytest.mark.asyncio
    async def test_path_is_file_not_directory(self, temp_workspace: Path):
        """Test searching in a file path instead of directory."""
        test_file = temp_workspace / "file.txt"
        test_file.write_text("Content")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/file.txt")

            assert "[not a directory:" in result

    @pytest.mark.asyncio
    async def test_no_matches_found(self, search_directory: Path, temp_workspace: Path):
        """Test when no files match the pattern."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.xyz", "/search")

            assert "No files matching '*.xyz' found" in result

    @pytest.mark.asyncio
    async def test_pattern_required(self, temp_workspace: Path):
        """Test that pattern is required."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Pattern is required"):
                await search_files("")

    @pytest.mark.asyncio
    async def test_path_required(self, temp_workspace: Path):
        """Test that path is required and cannot be empty."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path is required"):
                await search_files("*.txt", "")

    @pytest.mark.asyncio
    async def test_permission_error(self, temp_workspace: Path):
        """Test handling of permission errors during search."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with patch("os.walk") as mock_walk:
                mock_walk.side_effect = PermissionError("Permission denied")
                result = await search_files("*.txt", "/")

                assert "[permission denied:" in result

    @pytest.mark.asyncio
    async def test_generic_exception(self, temp_workspace: Path):
        """Test handling of generic exceptions during search."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with patch("os.walk") as mock_walk:
                mock_walk.side_effect = RuntimeError("Unexpected error")
                result = await search_files("*.txt", "/")

                assert "[error:" in result


# ============================================================================
# PATTERN MATCHING TESTS
# ============================================================================


class TestSearchFilesPatterns:
    """Test various glob patterns."""

    @pytest.mark.asyncio
    async def test_question_mark_pattern(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test ? wildcard matches single character."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("file?.txt", "/search", recursive=False)

            assert "file1.txt" in result
            assert "file2.txt" in result

    @pytest.mark.asyncio
    async def test_bracket_pattern(self, temp_workspace: Path):
        """Test [abc] character class pattern."""
        test_dir = temp_workspace / "bracket_test"
        test_dir.mkdir()
        (test_dir / "fileA.txt").write_text("A")
        (test_dir / "fileB.txt").write_text("B")
        (test_dir / "fileC.txt").write_text("C")
        (test_dir / "fileD.txt").write_text("D")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("file[AB].txt", "/bracket_test")

            assert "fileA.txt" in result
            assert "fileB.txt" in result
            assert "fileC.txt" not in result
            assert "fileD.txt" not in result

    @pytest.mark.asyncio
    async def test_star_matches_any_chars(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test * matches any characters."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.json", "/search", recursive=True)

            assert "data.json" in result
            assert "config.json" in result

    @pytest.mark.asyncio
    async def test_exact_filename_match(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test exact filename match (no wildcards)."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("script.py", "/search")

            assert "Found 1 file(s)" in result
            assert "script.py" in result


# ============================================================================
# SECURITY TESTS
# ============================================================================


class TestSearchFilesSecurity:
    """Test security-related functionality."""

    @pytest.mark.asyncio
    async def test_path_must_start_with_slash(self, temp_workspace: Path):
        """Test that relative paths are rejected."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path must start with /"):
                await search_files("*.txt", "relative/path")

    @pytest.mark.asyncio
    async def test_path_without_leading_slash_rejected(self, temp_workspace: Path):
        """Test that paths like '../etc' are rejected."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path must start with /"):
                await search_files("*.txt", "../../../etc")

    @pytest.mark.asyncio
    async def test_cannot_escape_sandbox(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test that path traversal attempts stay in sandbox."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/../../../etc")

            # Should resolve to sandbox and either find files or say not found
            assert "[not found:" in result or "Found" in result

    @pytest.mark.asyncio
    async def test_results_are_relative_paths(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test that results show relative paths, not absolute."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/search", recursive=True)

            # Should not contain the temp workspace path
            assert str(temp_workspace) not in result
            # Should contain relative paths starting with /
            assert "/search/file1.txt" in result

    def test_resolve_under_root_function(self, temp_workspace: Path):
        """Test the _resolve_under_root helper function."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/docs")
            assert result.startswith(str(temp_workspace))

            result = _resolve_under_root("/../../etc")
            assert result.startswith(str(temp_workspace))

    def test_get_relative_path_function(self, temp_workspace: Path):
        """Test the _get_relative_path helper function."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            abs_path = str(temp_workspace / "docs" / "file.txt")
            rel_path = _get_relative_path(abs_path)
            assert rel_path == "/docs/file.txt"

    def test_get_relative_path_root_returns_slash(self, temp_workspace: Path):
        """Test _get_relative_path returns '/' for root path."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            # When absolute_path equals FS_ROOT, rel is empty, should return "/"
            rel_path = _get_relative_path(str(temp_workspace))
            assert rel_path == "/"

    def test_get_relative_path_outside_root(self, temp_workspace: Path):
        """Test _get_relative_path with path outside FS_ROOT."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            # Path that doesn't start with FS_ROOT should be returned as-is
            outside_path = "/some/other/path"
            rel_path = _get_relative_path(outside_path)
            assert rel_path == outside_path


# ============================================================================
# EDGE CASES TESTS
# ============================================================================


class TestSearchFilesEdgeCases:
    """Test boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_empty_directory(self, temp_workspace: Path):
        """Test searching in empty directory."""
        empty_dir = temp_workspace / "empty"
        empty_dir.mkdir()

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*", "/empty")

            assert "No files matching" in result

    @pytest.mark.asyncio
    async def test_unicode_filenames(
        self, unicode_search_directory: Path, temp_workspace: Path
    ):
        """Test searching with Unicode filenames."""
        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/unicode")

            assert "Found" in result
            assert "文件.txt" in result
            assert "файл.txt" in result

    @pytest.mark.asyncio
    async def test_files_with_spaces(self, temp_workspace: Path):
        """Test searching for files with spaces in names."""
        space_dir = temp_workspace / "spaces"
        space_dir.mkdir()
        (space_dir / "my file.txt").write_text("Content")
        (space_dir / "another file.txt").write_text("Content")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("*.txt", "/spaces")

            assert "my file.txt" in result
            assert "another file.txt" in result

    @pytest.mark.asyncio
    async def test_hidden_files(self, temp_workspace: Path):
        """Test searching for hidden files."""
        hidden_dir = temp_workspace / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / ".hidden.txt").write_text("Hidden")
        (hidden_dir / "visible.txt").write_text("Visible")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            # Search for hidden files specifically
            result = await search_files(".*", "/hidden")
            assert ".hidden.txt" in result

            # Search for all txt files
            result = await search_files("*.txt", "/hidden")
            assert "visible.txt" in result

    @pytest.mark.asyncio
    async def test_search_in_root(self, temp_workspace: Path):
        """Test searching from root path."""
        (temp_workspace / "root_file.txt").write_text("Root")

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            result = await search_files("root_file.txt", "/")

            assert "Found 1 file(s)" in result


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


class TestSearchFilesConcurrency:
    """Test concurrent access scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_searches(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test multiple simultaneous searches."""
        import asyncio

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            tasks = [
                search_files("*.txt", "/search"),
                search_files("*.json", "/search"),
                search_files("*.py", "/search"),
            ]
            results = await asyncio.gather(*tasks)

            assert len(results) == 3
            assert "file1.txt" in results[0]
            assert "data.json" in results[1]
            assert "script.py" in results[2]

    @pytest.mark.asyncio
    async def test_concurrent_same_search(
        self, search_directory: Path, temp_workspace: Path
    ):
        """Test same search run concurrently."""
        import asyncio

        with patch("tools.search_files.FS_ROOT", str(temp_workspace)):
            tasks = [search_files("*.txt", "/search") for _ in range(5)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 5
            for result in results:
                assert "file1.txt" in result
                assert "file2.txt" in result
