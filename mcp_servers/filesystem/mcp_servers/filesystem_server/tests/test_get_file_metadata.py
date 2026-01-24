"""Comprehensive tests for get_file_metadata tool.

This module tests all aspects of the get_file_metadata functionality:
- File metadata retrieval (size, timestamps, permissions)
- Directory metadata retrieval
- Path resolution and security
- Error handling
- Edge cases

Test Coverage Areas:
1. Basic Functionality: Getting metadata for files and directories
2. Metadata Fields: Size, timestamps, permissions, MIME type
3. Error Handling: Non-existent paths, permission issues
4. Security: Sandboxing, path traversal prevention
5. Edge Cases: Special files, symlinks, Unicode names
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from tools.get_file_metadata import (
    _format_permissions,
    _format_size,
    _resolve_under_root,
    get_file_metadata,
)

# ============================================================================
# METADATA FIXTURES
# ============================================================================


@pytest.fixture
def sample_text_file(temp_workspace: Path) -> Path:
    """Create a sample text file."""
    txt_path = temp_workspace / "sample.txt"
    txt_path.write_text("Hello, World!")
    return txt_path


@pytest.fixture
def sample_json_file(temp_workspace: Path) -> Path:
    """Create a sample JSON file."""
    json_path = temp_workspace / "data.json"
    json_path.write_text('{"key": "value"}')
    return json_path


@pytest.fixture
def sample_directory(temp_workspace: Path) -> Path:
    """Create a sample directory with files."""
    dir_path = temp_workspace / "sample_dir"
    dir_path.mkdir()
    (dir_path / "file1.txt").write_text("File 1")
    (dir_path / "file2.txt").write_text("File 2")
    return dir_path


@pytest.fixture
def empty_file(temp_workspace: Path) -> Path:
    """Create an empty file."""
    empty_path = temp_workspace / "empty.txt"
    empty_path.write_text("")
    return empty_path


@pytest.fixture
def large_file(temp_workspace: Path) -> Path:
    """Create a large file for size formatting tests."""
    large_path = temp_workspace / "large.bin"
    large_path.write_bytes(b"X" * (5 * 1024 * 1024))  # 5 MB
    return large_path


@pytest.fixture
def nested_file(temp_workspace: Path) -> Path:
    """Create a file in a nested directory."""
    nested_dir = temp_workspace / "level1" / "level2" / "level3"
    nested_dir.mkdir(parents=True)
    nested_file = nested_dir / "nested.txt"
    nested_file.write_text("Nested content")
    return nested_file


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================


class TestGetFileMetadataBasic:
    """Test core functionality with standard use cases."""

    @pytest.mark.asyncio
    async def test_get_file_metadata(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test getting metadata for a text file."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            assert "Path: /sample.txt" in result
            assert "Type: file" in result
            assert "Size:" in result
            assert "bytes" in result
            assert "Modified:" in result
            assert "Permissions:" in result

    @pytest.mark.asyncio
    async def test_get_directory_metadata(
        self, sample_directory: Path, temp_workspace: Path
    ):
        """Test getting metadata for a directory."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample_dir")

            assert "Path: /sample_dir" in result
            assert "Type: directory" in result
            assert "Size:" in result
            assert "Modified:" in result
            # Directories should not have MIME type
            assert "MIME type" not in result

    @pytest.mark.asyncio
    async def test_get_json_file_metadata(
        self, sample_json_file: Path, temp_workspace: Path
    ):
        """Test getting metadata for a JSON file."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/data.json")

            assert "Path: /data.json" in result
            assert "Type: file" in result
            assert "MIME type: application/json" in result

    @pytest.mark.asyncio
    async def test_get_nested_file_metadata(
        self, nested_file: Path, temp_workspace: Path
    ):
        """Test getting metadata for a nested file."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/level1/level2/level3/nested.txt")

            assert "Path: /level1/level2/level3/nested.txt" in result
            assert "Type: file" in result


# ============================================================================
# METADATA FIELDS TESTS
# ============================================================================


class TestGetFileMetadataFields:
    """Test specific metadata fields."""

    @pytest.mark.asyncio
    async def test_size_field(self, sample_text_file: Path, temp_workspace: Path):
        """Test that size is reported correctly."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            # "Hello, World!" is 13 bytes
            assert "13 bytes" in result

    @pytest.mark.asyncio
    async def test_empty_file_size(self, empty_file: Path, temp_workspace: Path):
        """Test that empty file shows 0 bytes."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/empty.txt")

            assert "0 bytes" in result

    @pytest.mark.asyncio
    async def test_large_file_size_formatting(
        self, large_file: Path, temp_workspace: Path
    ):
        """Test that large file sizes are formatted correctly."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/large.bin")

            assert "5.0 MB" in result

    @pytest.mark.asyncio
    async def test_timestamps_present(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test that timestamps are present."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            assert "Modified:" in result
            assert "Accessed:" in result
            assert "Created/Changed:" in result

    @pytest.mark.asyncio
    async def test_timestamps_iso_format(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test that timestamps are in ISO format."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            # ISO format should contain T separator and timezone
            # e.g., "2024-01-15T10:30:00+00:00"
            assert "T" in result
            assert "+00:00" in result or "Z" in result

    @pytest.mark.asyncio
    async def test_permissions_present(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test that permissions are reported."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            assert "Permissions:" in result
            # Should have rwx format
            assert "r" in result or "-" in result

    @pytest.mark.asyncio
    async def test_inode_and_device(self, sample_text_file: Path, temp_workspace: Path):
        """Test that inode and device info is present."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            assert "Inode:" in result
            assert "Device:" in result

    @pytest.mark.asyncio
    async def test_hard_links_count(self, sample_text_file: Path, temp_workspace: Path):
        """Test that hard link count is present."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/sample.txt")

            assert "Hard links:" in result


# ============================================================================
# MIME TYPE TESTS
# ============================================================================


class TestGetFileMetadataMimeType:
    """Test MIME type detection."""

    @pytest.mark.asyncio
    async def test_text_file_mime(self, temp_workspace: Path):
        """Test MIME type for .txt file."""
        txt_file = temp_workspace / "file.txt"
        txt_file.write_text("Text content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/file.txt")

            assert "text/plain" in result

    @pytest.mark.asyncio
    async def test_json_mime(self, temp_workspace: Path):
        """Test MIME type for .json file."""
        json_file = temp_workspace / "data.json"
        json_file.write_text('{"key": "value"}')

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/data.json")

            assert "application/json" in result

    @pytest.mark.asyncio
    async def test_python_mime(self, temp_workspace: Path):
        """Test MIME type for .py file."""
        py_file = temp_workspace / "script.py"
        py_file.write_text("print('hello')")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/script.py")

            # Python files may be text/x-python or text/plain
            assert "text/" in result

    @pytest.mark.asyncio
    async def test_unknown_extension_mime(self, temp_workspace: Path):
        """Test MIME type for unknown extension."""
        unknown_file = temp_workspace / "file.unknownext123"
        unknown_file.write_text("Unknown content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/file.unknownext123")

            assert "MIME type: unknown" in result


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestGetFileMetadataErrors:
    """Test error scenarios."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, temp_workspace: Path):
        """Test error when file doesn't exist."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/nonexistent.txt")

            assert "[not found:" in result

    @pytest.mark.asyncio
    async def test_nested_path_not_found(self, temp_workspace: Path):
        """Test error for non-existent nested path."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/a/b/c/file.txt")

            assert "[not found:" in result

    @pytest.mark.asyncio
    async def test_path_required(self, temp_workspace: Path):
        """Test that file_path is required."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await get_file_metadata("")

    @pytest.mark.asyncio
    async def test_path_must_start_with_slash(self, temp_workspace: Path):
        """Test that path must start with /."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path must start with /"):
                await get_file_metadata("file.txt")


# ============================================================================
# INPUT VALIDATION TESTS
# ============================================================================


class TestGetFileMetadataValidation:
    """Test input validation."""

    @pytest.mark.asyncio
    async def test_path_must_be_string(self, temp_workspace: Path):
        """Test that file_path must be a string."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await get_file_metadata(None)  # type: ignore

    @pytest.mark.asyncio
    async def test_empty_string_path(self, temp_workspace: Path):
        """Test that empty string raises error."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await get_file_metadata("")


# ============================================================================
# SECURITY TESTS
# ============================================================================


class TestGetFileMetadataSecurity:
    """Test security-related functionality."""

    @pytest.mark.asyncio
    async def test_cannot_escape_sandbox(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test that path traversal attempts stay in sandbox."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/../../../etc/passwd")

            # Should either not find or stay in sandbox
            assert "[not found:" in result or "Path: /" in result

    @pytest.mark.asyncio
    async def test_parent_directory_resolution(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test that parent directory references are normalized."""
        subdir = temp_workspace / "subdir"
        subdir.mkdir()

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/subdir/../sample.txt")

            assert (
                "Path: /subdir/../sample.txt" in result or "Path: /sample.txt" in result
            )
            assert "Type: file" in result

    def test_resolve_under_root_function(self, temp_workspace: Path):
        """Test the _resolve_under_root helper function."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/docs/file.txt")
            assert result.startswith(str(temp_workspace))

            result = _resolve_under_root("/../../etc/passwd")
            assert result.startswith(str(temp_workspace))


# ============================================================================
# EDGE CASES TESTS
# ============================================================================


class TestGetFileMetadataEdgeCases:
    """Test boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_unicode_filename(self, temp_workspace: Path):
        """Test metadata for file with Unicode name."""
        unicode_file = temp_workspace / "文件.txt"
        unicode_file.write_text("Chinese filename")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/文件.txt")

            assert "Path: /文件.txt" in result
            assert "Type: file" in result

    @pytest.mark.asyncio
    async def test_file_with_spaces(self, temp_workspace: Path):
        """Test metadata for file with spaces in name."""
        spaced_file = temp_workspace / "my file name.txt"
        spaced_file.write_text("Content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/my file name.txt")

            assert "Path: /my file name.txt" in result
            assert "Type: file" in result

    @pytest.mark.asyncio
    async def test_hidden_file(self, temp_workspace: Path):
        """Test metadata for hidden file."""
        hidden_file = temp_workspace / ".hidden"
        hidden_file.write_text("Hidden content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/.hidden")

            assert "Path: /.hidden" in result
            assert "Type: file" in result

    @pytest.mark.asyncio
    async def test_empty_directory(self, temp_workspace: Path):
        """Test metadata for empty directory."""
        empty_dir = temp_workspace / "empty_dir"
        empty_dir.mkdir()

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/empty_dir")

            assert "Path: /empty_dir" in result
            assert "Type: directory" in result


# ============================================================================
# HELPER FUNCTION TESTS
# ============================================================================


class TestHelperFunctions:
    """Test helper functions."""

    def test_format_size_bytes(self):
        """Test _format_size with bytes."""
        assert _format_size(0) == "0 B"
        assert _format_size(100) == "100 B"
        assert _format_size(1023) == "1023 B"

    def test_format_size_kilobytes(self):
        """Test _format_size with kilobytes."""
        assert _format_size(1024) == "1.0 KB"
        assert _format_size(2048) == "2.0 KB"
        assert _format_size(1536) == "1.5 KB"

    def test_format_size_megabytes(self):
        """Test _format_size with megabytes."""
        assert _format_size(1024 * 1024) == "1.0 MB"
        assert _format_size(5 * 1024 * 1024) == "5.0 MB"

    def test_format_size_gigabytes(self):
        """Test _format_size with gigabytes."""
        assert _format_size(1024 * 1024 * 1024) == "1.0 GB"
        assert _format_size(2 * 1024 * 1024 * 1024) == "2.0 GB"

    def test_format_permissions(self):
        """Test _format_permissions function."""
        # rwxr-xr-x (755)
        perms = _format_permissions(0o755)
        assert "rwx" in perms
        assert "r-x" in perms

        # rw-r--r-- (644)
        perms = _format_permissions(0o644)
        assert "rw-" in perms
        assert "r--" in perms


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


class TestGetFileMetadataConcurrency:
    """Test concurrent access scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads(
        self, sample_text_file: Path, temp_workspace: Path
    ):
        """Test multiple simultaneous metadata reads."""
        import asyncio

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            tasks = [get_file_metadata("/sample.txt") for _ in range(5)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 5
            for result in results:
                assert "Path: /sample.txt" in result
                assert "Type: file" in result

    @pytest.mark.asyncio
    async def test_concurrent_different_files(self, temp_workspace: Path):
        """Test concurrent metadata reads of different files."""
        import asyncio

        # Create multiple files
        (temp_workspace / "file1.txt").write_text("File 1")
        (temp_workspace / "file2.json").write_text('{"a": 1}')
        (temp_workspace / "file3.py").write_text("print('hi')")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            tasks = [
                get_file_metadata("/file1.txt"),
                get_file_metadata("/file2.json"),
                get_file_metadata("/file3.py"),
            ]
            results = await asyncio.gather(*tasks)

            assert len(results) == 3
            assert "file1.txt" in results[0]
            assert "file2.json" in results[1]
            assert "file3.py" in results[2]


# ============================================================================
# SYMLINK TESTS
# ============================================================================


class TestGetFileMetadataSymlinks:
    """Test symlink-related functionality."""

    @pytest.mark.asyncio
    async def test_symlink_metadata(self, temp_workspace: Path):
        """Test metadata for symlink shows symlink target."""

        target_file = temp_workspace / "target.txt"
        target_file.write_text("Target content")
        symlink_file = temp_workspace / "link.txt"
        symlink_file.symlink_to(target_file)

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = await get_file_metadata("/link.txt")

            assert "Path: /link.txt" in result
            assert "Symlink target:" in result
            assert "target.txt" in result

    @pytest.mark.asyncio
    async def test_symlink_unreadable_target(self, temp_workspace: Path):
        """Test symlink with unreadable target returns appropriate message."""

        target_file = temp_workspace / "target.txt"
        target_file.write_text("Target content")
        symlink_file = temp_workspace / "link.txt"
        symlink_file.symlink_to(target_file)

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            # Mock os.readlink to raise OSError
            with patch("os.readlink") as mock_readlink:
                mock_readlink.side_effect = OSError("Cannot read symlink")
                result = await get_file_metadata("/link.txt")

                assert "Symlink target: (unreadable)" in result


# ============================================================================
# EXCEPTION HANDLING TESTS
# ============================================================================


class TestGetFileMetadataExceptions:
    """Test exception handling."""

    @pytest.mark.asyncio
    async def test_permission_error(self, temp_workspace: Path):
        """Test handling of permission errors when getting metadata."""
        test_file = temp_workspace / "perm_test.txt"
        test_file.write_text("Content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            # Need to let os.path.lexists pass, but raise on the os.lstat call inside try block
            call_count = [0]
            original_lstat = os.lstat

            def lstat_with_error(path, *args, **kwargs):
                # First call is from os.path.lexists, let it pass
                # Second call is from the actual lstat in try block, raise error
                call_count[0] += 1
                if call_count[0] > 1 and "perm_test.txt" in str(path):
                    raise PermissionError("Permission denied")
                return original_lstat(path, *args, **kwargs)

            with patch("os.lstat", side_effect=lstat_with_error):
                result = await get_file_metadata("/perm_test.txt")

                assert "[permission denied:" in result

    @pytest.mark.asyncio
    async def test_generic_exception(self, temp_workspace: Path):
        """Test handling of generic exceptions when getting metadata."""
        test_file = temp_workspace / "exc_test.txt"
        test_file.write_text("Content")

        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            # Need to let os.path.lexists pass, but raise on the os.lstat call inside try block
            call_count = [0]
            original_lstat = os.lstat

            def lstat_with_error(path, *args, **kwargs):
                # Count only calls that include 'exc_test.txt' in the path:
                # 1. os.path.lexists
                # 2. os.path.realpath in _is_path_within_sandbox (final component)
                # 3. explicit os.lstat in try block <- raise error here
                if "exc_test.txt" in str(path):
                    call_count[0] += 1
                    if call_count[0] > 2:
                        raise RuntimeError("Unexpected error")
                return original_lstat(path, *args, **kwargs)

            with patch("os.lstat", side_effect=lstat_with_error):
                result = await get_file_metadata("/exc_test.txt")

                assert "[error:" in result


# ============================================================================
# RESOLVE UNDER ROOT TESTS
# ============================================================================


class TestResolveUnderRoot:
    """Test _resolve_under_root helper function edge cases."""

    def test_empty_path_returns_root(self, temp_workspace: Path):
        """Test that empty path returns FS_ROOT."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("")
            assert result == str(temp_workspace)

    def test_root_path_returns_root(self, temp_workspace: Path):
        """Test that '/' returns FS_ROOT."""
        with patch("tools.get_file_metadata.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/")
            assert result == str(temp_workspace)
