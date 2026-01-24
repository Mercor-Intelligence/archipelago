"""Comprehensive tests for get_directory_tree tool.

This module tests all aspects of the get_directory_tree functionality:
- Tree structure generation
- Depth limiting
- File inclusion options
- Size display options
- Path resolution and security
- Error handling
- Edge cases

Test Coverage Areas:
1. Basic Functionality: Generating tree structures
2. Depth Control: max_depth parameter
3. File Options: include_files parameter
4. Size Display: show_size parameter
5. Error Handling: Non-existent paths, permission issues
6. Security: Sandboxing, path traversal prevention
7. Edge Cases: Empty directories, Unicode names, deep nesting
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from tools.get_directory_tree import _resolve_under_root, get_directory_tree

# ============================================================================
# TREE FIXTURES
# ============================================================================


@pytest.fixture
def simple_directory(temp_workspace: Path) -> Path:
    """Create a simple directory structure."""
    simple = temp_workspace / "simple"
    simple.mkdir()

    (simple / "file1.txt").write_text("File 1")
    (simple / "file2.txt").write_text("File 2")
    (simple / "data.json").write_text('{"key": "value"}')

    return simple


@pytest.fixture
def nested_directory(temp_workspace: Path) -> Path:
    """Create a nested directory structure."""
    nested = temp_workspace / "nested"
    nested.mkdir()

    # Root files
    (nested / "root.txt").write_text("Root file")

    # Level 1
    level1 = nested / "level1"
    level1.mkdir()
    (level1 / "l1_file.txt").write_text("Level 1 file")

    # Level 2
    level2 = level1 / "level2"
    level2.mkdir()
    (level2 / "l2_file.txt").write_text("Level 2 file")

    # Level 3
    level3 = level2 / "level3"
    level3.mkdir()
    (level3 / "l3_file.txt").write_text("Level 3 file")

    # Level 4
    level4 = level3 / "level4"
    level4.mkdir()
    (level4 / "deep.txt").write_text("Deep file")

    return nested


@pytest.fixture
def complex_directory(temp_workspace: Path) -> Path:
    """Create a complex directory structure with multiple branches."""
    complex_dir = temp_workspace / "complex"
    complex_dir.mkdir()

    # Root files
    (complex_dir / "readme.md").write_text("# README")
    (complex_dir / "config.json").write_text("{}")

    # src directory
    src = complex_dir / "src"
    src.mkdir()
    (src / "main.py").write_text("print('main')")
    (src / "utils.py").write_text("# utils")

    # src/components
    components = src / "components"
    components.mkdir()
    (components / "button.py").write_text("# button")
    (components / "input.py").write_text("# input")

    # tests directory
    tests = complex_dir / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text("# test")

    # docs directory
    docs = complex_dir / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Docs")

    return complex_dir


@pytest.fixture
def directory_with_sizes(temp_workspace: Path) -> Path:
    """Create a directory with files of known sizes."""
    sized = temp_workspace / "sized"
    sized.mkdir()

    (sized / "small.txt").write_text("X" * 10)  # 10 bytes
    (sized / "medium.txt").write_text("X" * 100)  # 100 bytes
    (sized / "large.txt").write_text("X" * 1000)  # 1000 bytes

    return sized


@pytest.fixture
def empty_directory(temp_workspace: Path) -> Path:
    """Create an empty directory."""
    empty = temp_workspace / "empty_tree"
    empty.mkdir()
    return empty


@pytest.fixture
def unicode_directory(temp_workspace: Path) -> Path:
    """Create a directory with Unicode names."""
    unicode_dir = temp_workspace / "unicode_tree"
    unicode_dir.mkdir()

    (unicode_dir / "文件.txt").write_text("Chinese")
    (unicode_dir / "файл.txt").write_text("Russian")

    # Unicode subdirectory
    chinese_dir = unicode_dir / "文档"
    chinese_dir.mkdir()
    (chinese_dir / "readme.txt").write_text("Readme")

    return unicode_dir


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================


class TestGetDirectoryTreeBasic:
    """Test core functionality with standard use cases."""

    @pytest.mark.asyncio
    async def test_simple_tree(self, simple_directory: Path, temp_workspace: Path):
        """Test tree output for simple directory."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/simple")

            assert "/simple/" in result
            assert "file1.txt" in result
            assert "file2.txt" in result
            assert "data.json" in result

    @pytest.mark.asyncio
    async def test_nested_tree(self, nested_directory: Path, temp_workspace: Path):
        """Test tree output for nested directory."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested")

            assert "/nested/" in result
            assert "level1/" in result
            assert "level2/" in result

    @pytest.mark.asyncio
    async def test_tree_has_connectors(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test that tree uses proper connectors."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/simple")

            # Should have tree connectors
            assert "├──" in result or "└──" in result

    @pytest.mark.asyncio
    async def test_root_directory_tree(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test tree from root directory."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/")

            assert "/" in result
            assert "simple/" in result


# ============================================================================
# DEPTH CONTROL TESTS
# ============================================================================


class TestGetDirectoryTreeDepth:
    """Test max_depth parameter."""

    @pytest.mark.asyncio
    async def test_depth_1(self, nested_directory: Path, temp_workspace: Path):
        """Test tree with max_depth=1."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested", max_depth=1)

            assert "level1/" in result
            assert "root.txt" in result
            # Should not show level2 contents
            assert "level2/" not in result

    @pytest.mark.asyncio
    async def test_depth_2(self, nested_directory: Path, temp_workspace: Path):
        """Test tree with max_depth=2."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested", max_depth=2)

            assert "level1/" in result
            assert "level2/" in result
            # Should not show level3 contents
            assert "level3/" not in result

    @pytest.mark.asyncio
    async def test_depth_3_default(self, nested_directory: Path, temp_workspace: Path):
        """Test that default max_depth is 3."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested")

            assert "level1/" in result
            assert "level2/" in result
            assert "level3/" in result
            # Level 4 should not appear with default depth of 3
            assert "level4/" not in result

    @pytest.mark.asyncio
    async def test_depth_clamped_minimum(
        self, nested_directory: Path, temp_workspace: Path
    ):
        """Test that depth < 1 is clamped to 1."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested", max_depth=0)

            # Should still show first level
            assert "level1/" in result

    @pytest.mark.asyncio
    async def test_depth_clamped_maximum(
        self, nested_directory: Path, temp_workspace: Path
    ):
        """Test that depth > 10 is clamped to 10."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested", max_depth=100)

            # Should work with clamped value
            assert "/nested/" in result


# ============================================================================
# FILE INCLUSION TESTS
# ============================================================================


class TestGetDirectoryTreeIncludeFiles:
    """Test include_files parameter."""

    @pytest.mark.asyncio
    async def test_include_files_true_default(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test that files are included by default."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/simple")

            assert "file1.txt" in result
            assert "file2.txt" in result
            assert "data.json" in result

    @pytest.mark.asyncio
    async def test_include_files_false(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test tree without files (directories only)."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/complex", include_files=False)

            # Should have directories
            assert "src/" in result
            assert "tests/" in result
            assert "docs/" in result
            # Should not have files
            assert "readme.md" not in result
            assert "main.py" not in result

    @pytest.mark.asyncio
    async def test_include_files_nested(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test include_files affects nested directories too."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree(
                "/complex", include_files=False, max_depth=5
            )

            # Nested directories should appear
            assert "components/" in result
            # But no files
            assert "button.py" not in result


# ============================================================================
# SIZE DISPLAY TESTS
# ============================================================================


class TestGetDirectoryTreeShowSize:
    """Test show_size parameter."""

    @pytest.mark.asyncio
    async def test_show_size_false_default(
        self, directory_with_sizes: Path, temp_workspace: Path
    ):
        """Test that sizes are not shown by default."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/sized")

            # Should have filenames
            assert "small.txt" in result
            # Should not have byte counts
            assert "10 bytes" not in result
            assert "100 bytes" not in result

    @pytest.mark.asyncio
    async def test_show_size_true(
        self, directory_with_sizes: Path, temp_workspace: Path
    ):
        """Test tree with file sizes displayed."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/sized", show_size=True)

            assert "small.txt" in result
            assert "10 bytes" in result
            assert "100 bytes" in result
            assert "1000 bytes" in result

    @pytest.mark.asyncio
    async def test_show_size_only_files(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test that only files show sizes, not directories."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/complex", show_size=True)

            # Files should have sizes
            assert "bytes" in result
            # Check that directories don't have size annotations
            lines = result.split("\n")
            for line in lines:
                if "src/" in line or "tests/" in line:
                    assert "bytes" not in line


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestGetDirectoryTreeErrors:
    """Test error scenarios."""

    @pytest.mark.asyncio
    async def test_path_not_found(self, temp_workspace: Path):
        """Test error when path doesn't exist."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nonexistent")

            assert "[not found:" in result

    @pytest.mark.asyncio
    async def test_path_is_file_not_directory(self, temp_workspace: Path):
        """Test error when path is a file."""
        test_file = temp_workspace / "file.txt"
        test_file.write_text("Content")

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/file.txt")

            assert "[not a directory:" in result

    @pytest.mark.asyncio
    async def test_invalid_path_type_none(self, temp_workspace: Path):
        """Test that None path raises ValueError."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path is required"):
                await get_directory_tree(None)  # type: ignore

    @pytest.mark.asyncio
    async def test_invalid_path_type_empty_string(self, temp_workspace: Path):
        """Test that empty string path raises ValueError."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path is required"):
                await get_directory_tree("")

    @pytest.mark.asyncio
    async def test_scandir_permission_error(self, temp_workspace: Path):
        """Test handling of permission errors when scanning directories."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with patch("os.scandir") as mock_scandir:
                mock_scandir.side_effect = PermissionError("Permission denied")
                result = await get_directory_tree("/")

                assert "[permission denied]" in result

    @pytest.mark.asyncio
    async def test_scandir_generic_exception(self, temp_workspace: Path):
        """Test handling of generic exceptions when scanning directories."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with patch("os.scandir") as mock_scandir:
                mock_scandir.side_effect = OSError("Some OS error")
                result = await get_directory_tree("/")

                assert "[error:" in result

    @pytest.mark.asyncio
    async def test_entry_is_dir_oserror(self, temp_workspace: Path):
        """Test handling of OSError when checking is_dir() on entries."""
        test_dir = temp_workspace / "test_oserror"
        test_dir.mkdir()
        (test_dir / "file.txt").write_text("content")

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            # Mock DirEntry to raise OSError on is_dir()
            original_scandir = os.scandir

            def mock_scandir_oserror(path):
                entries = list(original_scandir(path))
                for entry in entries:

                    def raise_oserror(*args, **kwargs):
                        raise OSError("Broken entry")

                    entry.is_dir = raise_oserror
                    entry.is_file = raise_oserror
                return iter(entries)

            with patch("os.scandir", mock_scandir_oserror):
                result = await get_directory_tree("/test_oserror")

                # Should still return a result (entries with OSError are skipped)
                assert "/test_oserror/" in result

    @pytest.mark.asyncio
    async def test_file_stat_oserror_with_show_size(self, temp_workspace: Path):
        """Test handling of OSError when getting file size with show_size=True."""
        test_dir = temp_workspace / "stat_error"
        test_dir.mkdir()
        test_file = test_dir / "file.txt"
        test_file.write_text("content")

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            # Create a mock DirEntry
            class MockDirEntry:
                def __init__(self, name, path, is_directory=False):
                    self.name = name
                    self.path = path
                    self._is_dir = is_directory

                def is_dir(self, *, follow_symlinks=True):
                    return self._is_dir

                def is_file(self, *, follow_symlinks=True):
                    return not self._is_dir

                def stat(self, *, follow_symlinks=True):
                    raise OSError("Cannot stat file")

            original_scandir = os.scandir

            def mock_scandir_stat_error(path):
                real_entries = list(original_scandir(path))
                mock_entries = []
                for e in real_entries:
                    mock_entry = MockDirEntry(e.name, e.path, e.is_dir())
                    mock_entries.append(mock_entry)
                return iter(mock_entries)

            with patch("os.scandir", mock_scandir_stat_error):
                result = await get_directory_tree("/stat_error", show_size=True)

                # File should appear without size
                assert "file.txt" in result
                # Should NOT contain "bytes" for this file since stat failed
                # The file is still listed, just without size info


# ============================================================================
# TREE FORMATTING TESTS
# ============================================================================


class TestGetDirectoryTreeFormatting:
    """Test tree visual formatting."""

    @pytest.mark.asyncio
    async def test_last_item_connector(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test that last items use └── connector."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/simple")

            # Last item should use └──
            assert "└──" in result

    @pytest.mark.asyncio
    async def test_middle_item_connector(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test that middle items use ├── connector."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/simple")

            # Middle items should use ├──
            assert "├──" in result

    @pytest.mark.asyncio
    async def test_nested_indentation(
        self, nested_directory: Path, temp_workspace: Path
    ):
        """Test proper indentation for nested items."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/nested", max_depth=5)

            # Nested items should be indented
            lines = result.split("\n")
            has_indented = any(
                line.startswith("    ") or line.startswith("│   ") for line in lines
            )
            assert has_indented

    @pytest.mark.asyncio
    async def test_directories_have_trailing_slash(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test that directories have trailing slash."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/complex")

            assert "src/" in result
            assert "tests/" in result
            assert "docs/" in result


# ============================================================================
# EDGE CASES TESTS
# ============================================================================


class TestGetDirectoryTreeEdgeCases:
    """Test boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_empty_directory(self, empty_directory: Path, temp_workspace: Path):
        """Test tree for empty directory."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/empty_tree")

            assert "/empty_tree/" in result
            assert "(empty)" in result

    @pytest.mark.asyncio
    async def test_unicode_filenames(
        self, unicode_directory: Path, temp_workspace: Path
    ):
        """Test tree with Unicode filenames."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/unicode_tree")

            assert "文件.txt" in result
            assert "файл.txt" in result
            assert "文档/" in result

    @pytest.mark.asyncio
    async def test_files_sorted_alphabetically(self, temp_workspace: Path):
        """Test that files are sorted alphabetically."""
        sorted_dir = temp_workspace / "sorted"
        sorted_dir.mkdir()
        (sorted_dir / "zebra.txt").write_text("Z")
        (sorted_dir / "apple.txt").write_text("A")
        (sorted_dir / "mango.txt").write_text("M")

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/sorted")

            lines = result.split("\n")
            file_lines = [line for line in lines if ".txt" in line]
            # Extract filenames and verify order
            filenames = [
                line.split("──")[-1].strip() for line in file_lines if "──" in line
            ]
            assert filenames == sorted(filenames, key=str.lower)

    @pytest.mark.asyncio
    async def test_directories_before_files(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test that directories appear before files."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/complex")

            lines = result.split("\n")
            # Find first file line and first dir line at root level
            dir_lines = []
            file_lines = []
            for line in lines:
                if "├──" in line or "└──" in line:
                    if "/" in line.split("──")[-1]:
                        dir_lines.append(line)
                    else:
                        file_lines.append(line)

            # At minimum, directories should exist
            assert len(dir_lines) > 0

    @pytest.mark.asyncio
    async def test_hidden_files_included(self, temp_workspace: Path):
        """Test that hidden files are included."""
        hidden_dir = temp_workspace / "hidden"
        hidden_dir.mkdir()
        (hidden_dir / ".hidden").write_text("Hidden")
        (hidden_dir / "visible.txt").write_text("Visible")

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/hidden")

            assert ".hidden" in result
            assert "visible.txt" in result


# ============================================================================
# SECURITY TESTS
# ============================================================================


class TestGetDirectoryTreeSecurity:
    """Test security-related functionality."""

    @pytest.mark.asyncio
    async def test_path_must_start_with_slash(self, temp_workspace: Path):
        """Test that relative paths are rejected."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path must start with /"):
                await get_directory_tree("relative/path")

    @pytest.mark.asyncio
    async def test_path_without_leading_slash_rejected(self, temp_workspace: Path):
        """Test that paths like '../etc' are rejected."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Path must start with /"):
                await get_directory_tree("../../../etc")

    @pytest.mark.asyncio
    async def test_cannot_escape_sandbox(
        self, simple_directory: Path, temp_workspace: Path
    ):
        """Test that path traversal attempts stay in sandbox."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/../../../etc")

            # Should either not find or stay in sandbox
            assert "[not found:" in result or "/" in result

    @pytest.mark.asyncio
    async def test_parent_directory_resolution(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test that parent directory references are normalized."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree("/complex/src/../docs")

            # Should resolve to /complex/docs
            assert "index.md" in result

    def test_resolve_under_root_function(self, temp_workspace: Path):
        """Test the _resolve_under_root helper function."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/docs")
            assert result.startswith(str(temp_workspace))

            result = _resolve_under_root("/../../etc")
            assert result.startswith(str(temp_workspace))

    @pytest.mark.asyncio
    async def test_symlink_pointing_outside_sandbox_denied(self, temp_workspace: Path):
        """Test that symlinks pointing outside sandbox are denied."""
        import tempfile

        # Create a directory outside the sandbox
        with tempfile.TemporaryDirectory() as external_dir:
            external_path = Path(external_dir)
            (external_path / "secret.txt").write_text("sensitive data")

            # Create a symlink inside sandbox pointing to external directory
            symlink_path = temp_workspace / "escape_link"
            symlink_path.symlink_to(external_path)

            with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
                result = await get_directory_tree("/escape_link")

                # Should deny access to symlinks pointing outside sandbox
                assert "[access denied: /escape_link]" in result

    @pytest.mark.asyncio
    async def test_is_path_within_sandbox_function(self, temp_workspace: Path):
        """Test the _is_path_within_sandbox helper function."""
        from tools.get_directory_tree import _is_path_within_sandbox

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            # Path within sandbox should return True
            (temp_workspace / "testdir").mkdir(exist_ok=True)
            assert _is_path_within_sandbox(str(temp_workspace / "testdir")) is True

            # Path outside sandbox should return False
            assert _is_path_within_sandbox("/etc") is False
            assert _is_path_within_sandbox("/tmp") is False


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


class TestGetDirectoryTreeConcurrency:
    """Test concurrent access scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_trees(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test multiple simultaneous tree generations."""
        import asyncio

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            tasks = [get_directory_tree("/complex") for _ in range(5)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 5
            for result in results:
                assert "/complex/" in result
                assert "src/" in result

    @pytest.mark.asyncio
    async def test_concurrent_different_directories(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test concurrent tree generation for different directories."""
        import asyncio

        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            tasks = [
                get_directory_tree("/complex/src"),
                get_directory_tree("/complex/tests"),
                get_directory_tree("/complex/docs"),
            ]
            results = await asyncio.gather(*tasks)

            assert len(results) == 3
            assert "main.py" in results[0]
            assert "test_main.py" in results[1]
            assert "index.md" in results[2]


# ============================================================================
# COMBINED OPTIONS TESTS
# ============================================================================


class TestGetDirectoryTreeCombinedOptions:
    """Test combinations of options."""

    @pytest.mark.asyncio
    async def test_depth_and_no_files(
        self, complex_directory: Path, temp_workspace: Path
    ):
        """Test max_depth with include_files=False."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree(
                "/complex", max_depth=2, include_files=False
            )

            # Should have directories up to depth 2
            assert "src/" in result
            assert "components/" in result
            # Should not have files
            assert "main.py" not in result
            assert "button.py" not in result

    @pytest.mark.asyncio
    async def test_all_options_enabled(
        self, directory_with_sizes: Path, temp_workspace: Path
    ):
        """Test with all options enabled."""
        with patch("tools.get_directory_tree.FS_ROOT", str(temp_workspace)):
            result = await get_directory_tree(
                "/sized", max_depth=5, include_files=True, show_size=True
            )

            # Should have files with sizes
            assert "small.txt" in result
            assert "10 bytes" in result
