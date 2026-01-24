"""Comprehensive tests for read_text_file tool.

This module tests all aspects of the read_text_file functionality:
- Input validation
- Path resolution and security
- Text file format support
- Encoding handling
- Error handling
- Edge cases
- Security (sandboxing, path traversal prevention)

Test Coverage Areas:
1. Basic Functionality: Reading various text formats
2. Input Validation: Path validation, extension checks
3. Encoding: UTF-8, Latin-1, ASCII support
4. Error Handling: File not found, not a file, invalid formats
5. Security: Directory traversal prevention, sandboxing
6. Edge Cases: Large files, empty files, Unicode content
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from tools.read_text_file import (
    TEXT_EXTENSIONS,
    _get_extension,
    _resolve_under_root,
    read_text_file,
)

# ============================================================================
# TEXT FILE FIXTURES
# ============================================================================


@pytest.fixture
def sample_txt_file(temp_workspace: Path) -> Path:
    """Create a sample .txt file."""
    txt_path = temp_workspace / "test.txt"
    txt_path.write_text("Hello, World!")
    return txt_path


@pytest.fixture
def sample_json_file(temp_workspace: Path) -> Path:
    """Create a sample .json file."""
    json_path = temp_workspace / "data.json"
    json_path.write_text('{"key": "value", "number": 42}')
    return json_path


@pytest.fixture
def sample_python_file(temp_workspace: Path) -> Path:
    """Create a sample .py file."""
    py_path = temp_workspace / "script.py"
    py_path.write_text("def hello():\n    print('Hello, World!')\n")
    return py_path


@pytest.fixture
def sample_csv_file(temp_workspace: Path) -> Path:
    """Create a sample .csv file."""
    csv_path = temp_workspace / "data.csv"
    csv_path.write_text("name,age,city\nAlice,30,NYC\nBob,25,LA\n")
    return csv_path


@pytest.fixture
def sample_md_file(temp_workspace: Path) -> Path:
    """Create a sample .md file."""
    md_path = temp_workspace / "readme.md"
    md_path.write_text("# Header\n\nThis is **markdown** content.\n")
    return md_path


@pytest.fixture
def sample_yaml_file(temp_workspace: Path) -> Path:
    """Create a sample .yaml file."""
    yaml_path = temp_workspace / "config.yaml"
    yaml_path.write_text("name: test\nversion: 1.0\nitems:\n  - one\n  - two\n")
    return yaml_path


@pytest.fixture
def sample_xml_file(temp_workspace: Path) -> Path:
    """Create a sample .xml file."""
    xml_path = temp_workspace / "data.xml"
    xml_path.write_text('<?xml version="1.0"?>\n<root><item>value</item></root>\n')
    return xml_path


@pytest.fixture
def text_files_directory(temp_workspace: Path) -> Path:
    """Create a directory with various text files."""
    text_dir = temp_workspace / "texts"
    text_dir.mkdir()

    (text_dir / "file.txt").write_text("Text content")
    (text_dir / "script.py").write_text("print('hello')")
    (text_dir / "data.json").write_text('{"a": 1}')
    (text_dir / "style.css").write_text("body { margin: 0; }")
    (text_dir / "readme.md").write_text("# README")

    return text_dir


@pytest.fixture
def unicode_text_file(temp_workspace: Path) -> Path:
    """Create a file with Unicode content."""
    unicode_path = temp_workspace / "unicode.txt"
    unicode_path.write_text("你好世界 🌍 Привет мир", encoding="utf-8")
    return unicode_path


@pytest.fixture
def latin1_text_file(temp_workspace: Path) -> Path:
    """Create a file with Latin-1 encoding."""
    latin1_path = temp_workspace / "latin1.txt"
    latin1_path.write_text("Café résumé naïve", encoding="latin-1")
    return latin1_path


@pytest.fixture
def large_text_file(temp_workspace: Path) -> Path:
    """Create a large text file for testing size limits."""
    large_path = temp_workspace / "large.txt"
    large_path.write_text("X" * 500000)  # 500KB
    return large_path


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================


class TestReadTextFileBasic:
    """Test core functionality with standard use cases."""

    @pytest.mark.asyncio
    async def test_read_txt_file(self, sample_txt_file: Path, temp_workspace: Path):
        """Test reading a .txt file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/test.txt")

            assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_read_json_file(self, sample_json_file: Path, temp_workspace: Path):
        """Test reading a .json file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/data.json")

            assert '"key": "value"' in result
            assert '"number": 42' in result

    @pytest.mark.asyncio
    async def test_read_python_file(
        self, sample_python_file: Path, temp_workspace: Path
    ):
        """Test reading a .py file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/script.py")

            assert "def hello():" in result
            assert "print('Hello, World!')" in result

    @pytest.mark.asyncio
    async def test_read_csv_file(self, sample_csv_file: Path, temp_workspace: Path):
        """Test reading a .csv file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/data.csv")

            assert "name,age,city" in result
            assert "Alice,30,NYC" in result

    @pytest.mark.asyncio
    async def test_read_md_file(self, sample_md_file: Path, temp_workspace: Path):
        """Test reading a .md file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/readme.md")

            assert "# Header" in result
            assert "**markdown**" in result

    @pytest.mark.asyncio
    async def test_read_yaml_file(self, sample_yaml_file: Path, temp_workspace: Path):
        """Test reading a .yaml file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/config.yaml")

            assert "name: test" in result
            assert "version: 1.0" in result

    @pytest.mark.asyncio
    async def test_read_xml_file(self, sample_xml_file: Path, temp_workspace: Path):
        """Test reading a .xml file."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/data.xml")

            assert "<root>" in result
            assert "<item>value</item>" in result

    @pytest.mark.asyncio
    async def test_read_file_from_subdirectory(
        self, text_files_directory: Path, temp_workspace: Path
    ):
        """Test reading file from subdirectory."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/texts/file.txt")

            assert result == "Text content"


# ============================================================================
# ENCODING TESTS
# ============================================================================


class TestReadTextFileEncoding:
    """Test encoding handling."""

    @pytest.mark.asyncio
    async def test_utf8_encoding_default(
        self, unicode_text_file: Path, temp_workspace: Path
    ):
        """Test UTF-8 encoding (default)."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/unicode.txt")

            assert "你好世界" in result
            assert "🌍" in result
            assert "Привет мир" in result

    @pytest.mark.asyncio
    async def test_latin1_encoding(self, latin1_text_file: Path, temp_workspace: Path):
        """Test Latin-1 encoding."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/latin1.txt", encoding="latin-1")

            assert "Café" in result
            assert "résumé" in result
            assert "naïve" in result

    @pytest.mark.asyncio
    async def test_wrong_encoding_raises_error(
        self, latin1_text_file: Path, temp_workspace: Path
    ):
        """Test that wrong encoding raises appropriate error."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Failed to decode file"):
                await read_text_file("/latin1.txt", encoding="utf-8")

    @pytest.mark.asyncio
    async def test_ascii_encoding(self, temp_workspace: Path):
        """Test ASCII encoding."""
        ascii_file = temp_workspace / "ascii.txt"
        ascii_file.write_text("Hello ASCII World!", encoding="ascii")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/ascii.txt", encoding="ascii")

            assert result == "Hello ASCII World!"


# ============================================================================
# INPUT VALIDATION TESTS
# ============================================================================


class TestReadTextFileValidation:
    """Test input validation and parameter checking."""

    @pytest.mark.asyncio
    async def test_file_path_required(self, temp_workspace: Path):
        """Test that file_path is required."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await read_text_file("")

    @pytest.mark.asyncio
    async def test_file_path_must_be_string(self, temp_workspace: Path):
        """Test that file_path must be a string."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await read_text_file(None)  # type: ignore

    @pytest.mark.asyncio
    async def test_file_path_must_start_with_slash(
        self, sample_txt_file: Path, temp_workspace: Path
    ):
        """Test that file_path must start with '/'."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path must start with /"):
                await read_text_file("test.txt")

    @pytest.mark.asyncio
    async def test_unsupported_file_format_binary(self, temp_workspace: Path):
        """Test that binary formats raise error."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported file type"):
                await read_text_file("/image.png")

    @pytest.mark.asyncio
    async def test_unsupported_file_format_pdf(self, temp_workspace: Path):
        """Test that PDF format raises error."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported file type"):
                await read_text_file("/document.pdf")

    @pytest.mark.asyncio
    async def test_unsupported_file_format_exe(self, temp_workspace: Path):
        """Test that executable format raises error."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported file type"):
                await read_text_file("/program.exe")

    @pytest.mark.asyncio
    async def test_uppercase_extension(self, temp_workspace: Path):
        """Test that uppercase extensions are handled correctly."""
        upper_file = temp_workspace / "TEST.TXT"
        upper_file.write_text("Uppercase extension")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/TEST.TXT")

            assert result == "Uppercase extension"

    @pytest.mark.asyncio
    async def test_mixed_case_extension(self, temp_workspace: Path):
        """Test mixed case extension (e.g., .Json)."""
        mixed_file = temp_workspace / "data.Json"
        mixed_file.write_text('{"test": true}')

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/data.Json")

            assert '"test": true' in result


# ============================================================================
# SIZE LIMIT TESTS
# ============================================================================


class TestReadTextFileSizeLimit:
    """Test file size limit handling."""

    @pytest.mark.asyncio
    async def test_large_file_within_limit(
        self, large_text_file: Path, temp_workspace: Path
    ):
        """Test reading large file within default limit."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/large.txt")

            assert len(result) == 500000
            assert result == "X" * 500000

    @pytest.mark.asyncio
    async def test_file_exceeds_limit(self, temp_workspace: Path):
        """Test that file exceeding limit raises error."""
        huge_file = temp_workspace / "huge.txt"
        huge_file.write_text("X" * 2000000)  # 2MB

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File too large"):
                await read_text_file("/huge.txt")

    @pytest.mark.asyncio
    async def test_custom_size_limit(self, temp_workspace: Path):
        """Test custom max_size parameter."""
        file_100b = temp_workspace / "small.txt"
        file_100b.write_text("X" * 100)

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            # Should fail with 50 byte limit
            with pytest.raises(ValueError, match="File too large"):
                await read_text_file("/small.txt", max_size=50)

    @pytest.mark.asyncio
    async def test_no_size_limit(self, temp_workspace: Path):
        """Test max_size=0 disables limit."""
        large_file = temp_workspace / "large.txt"
        large_file.write_text("X" * 2000000)

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/large.txt", max_size=0)

            assert len(result) == 2000000


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestReadTextFileErrors:
    """Test error scenarios and recovery."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, temp_workspace: Path):
        """Test error when file doesn't exist."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(FileNotFoundError, match="File not found"):
                await read_text_file("/nonexistent.txt")

    @pytest.mark.asyncio
    async def test_path_is_directory_not_file(self, temp_workspace: Path):
        """Test error when path points to a directory."""
        dir_with_txt = temp_workspace / "files.txt"
        dir_with_txt.mkdir()

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Not a file"):
                await read_text_file("/files.txt")

    @pytest.mark.asyncio
    async def test_empty_file(self, temp_workspace: Path):
        """Test reading an empty file."""
        empty_file = temp_workspace / "empty.txt"
        empty_file.write_text("")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/empty.txt")

            assert result == ""

    @pytest.mark.asyncio
    async def test_nested_path_not_found(self, temp_workspace: Path):
        """Test error for nested path that doesn't exist."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(FileNotFoundError):
                await read_text_file("/folder/subfolder/file.txt")


# ============================================================================
# SPECIAL FILES TESTS
# ============================================================================


class TestReadTextFileSpecialFiles:
    """Test special file types and names."""

    @pytest.mark.asyncio
    async def test_makefile(self, temp_workspace: Path):
        """Test reading Makefile."""
        makefile = temp_workspace / "Makefile"
        makefile.write_text("all:\n\techo hello\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/Makefile")

            assert "all:" in result
            assert "echo hello" in result

    @pytest.mark.asyncio
    async def test_dockerfile(self, temp_workspace: Path):
        """Test reading Dockerfile."""
        dockerfile = temp_workspace / "Dockerfile"
        dockerfile.write_text("FROM python:3.11\nWORKDIR /app\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/Dockerfile")

            assert "FROM python:3.11" in result
            assert "WORKDIR /app" in result

    @pytest.mark.asyncio
    async def test_gitignore(self, temp_workspace: Path):
        """Test reading .gitignore."""
        gitignore = temp_workspace / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/.gitignore")

            assert "*.pyc" in result
            assert "__pycache__/" in result

    @pytest.mark.asyncio
    async def test_env_file(self, temp_workspace: Path):
        """Test reading .env file."""
        env_file = temp_workspace / ".env"
        env_file.write_text("DB_HOST=localhost\nDB_PORT=5432\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/.env")

            assert "DB_HOST=localhost" in result
            assert "DB_PORT=5432" in result


# ============================================================================
# EDGE CASES TESTS
# ============================================================================


class TestReadTextFileEdgeCases:
    """Test boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_file_with_unicode_filename(self, temp_workspace: Path):
        """Test reading file with Unicode filename."""
        unicode_name = temp_workspace / "文档.txt"
        unicode_name.write_text("Chinese filename content")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/文档.txt")

            assert result == "Chinese filename content"

    @pytest.mark.asyncio
    async def test_file_with_spaces_in_filename(self, temp_workspace: Path):
        """Test reading file with spaces in filename."""
        spaced_file = temp_workspace / "my file name.txt"
        spaced_file.write_text("Content with spaces in name")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/my file name.txt")

            assert result == "Content with spaces in name"

    @pytest.mark.asyncio
    async def test_file_with_special_chars_filename(self, temp_workspace: Path):
        """Test reading file with special characters in filename."""
        special_name = temp_workspace / "file(test)-[1].txt"
        special_name.write_text("Special chars content")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/file(test)-[1].txt")

            assert result == "Special chars content"

    @pytest.mark.asyncio
    async def test_deeply_nested_file(self, temp_workspace: Path):
        """Test reading file from deeply nested directory."""
        nested_dir = temp_workspace / "level1" / "level2" / "level3" / "level4"
        nested_dir.mkdir(parents=True)
        nested_file = nested_dir / "deep.txt"
        nested_file.write_text("Deep file content")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/level1/level2/level3/level4/deep.txt")

            assert result == "Deep file content"

    @pytest.mark.asyncio
    async def test_file_with_newlines(self, temp_workspace: Path):
        """Test reading file with various newline styles."""
        newlines_file = temp_workspace / "newlines.txt"
        newlines_file.write_text("line1\nline2\nline3\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/newlines.txt")

            assert "line1" in result
            assert "line2" in result
            assert "line3" in result

    @pytest.mark.asyncio
    async def test_file_with_tabs(self, temp_workspace: Path):
        """Test reading file with tab characters."""
        tabs_file = temp_workspace / "tabs.py"
        tabs_file.write_text("def func():\n\treturn True\n")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/tabs.py")

            assert "\treturn True" in result


# ============================================================================
# SECURITY TESTS
# ============================================================================


class TestReadTextFileSecurity:
    """Test security-related functionality."""

    @pytest.mark.asyncio
    async def test_cannot_escape_sandbox_with_traversal(self, temp_workspace: Path):
        """Test that directory traversal attempts stay in sandbox."""
        safe_file = temp_workspace / "safe.txt"
        safe_file.write_text("Safe content")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(FileNotFoundError):
                await read_text_file("/../../../etc/passwd.txt")

    @pytest.mark.asyncio
    async def test_parent_directory_resolution(self, temp_workspace: Path):
        """Test that parent directory references are normalized."""
        dir1 = temp_workspace / "dir1"
        dir2 = temp_workspace / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        file_path = dir2 / "file.txt"
        file_path.write_text("In dir2")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/dir1/../dir2/file.txt")

            assert result == "In dir2"

    @pytest.mark.asyncio
    async def test_absolute_path_stays_in_sandbox(self, temp_workspace: Path):
        """Test that absolute paths are resolved relative to sandbox."""
        test_file = temp_workspace / "test.txt"
        test_file.write_text("Sandbox content")

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = await read_text_file("/test.txt")

            assert result == "Sandbox content"

    def test_resolve_under_root_function(self, temp_workspace: Path):
        """Test the _resolve_under_root helper function."""
        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/docs/test.txt")
            assert result.startswith(str(temp_workspace))
            assert "docs" in result
            assert "test.txt" in result

            result = _resolve_under_root("/../../etc/file.txt")
            assert result.startswith(str(temp_workspace))


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


class TestReadTextFileConcurrency:
    """Test concurrent access scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads_same_file(
        self, sample_txt_file: Path, temp_workspace: Path
    ):
        """Test multiple simultaneous reads of the same file."""
        import asyncio

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            tasks = [read_text_file("/test.txt") for _ in range(5)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 5
            for result in results:
                assert result == "Hello, World!"

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads_different_files(
        self, text_files_directory: Path, temp_workspace: Path
    ):
        """Test concurrent reads of different files."""
        import asyncio

        with patch("tools.read_text_file.FS_ROOT", str(temp_workspace)):
            tasks = [
                read_text_file("/texts/file.txt"),
                read_text_file("/texts/script.py"),
                read_text_file("/texts/data.json"),
            ]
            results = await asyncio.gather(*tasks)

            assert len(results) == 3
            assert results[0] == "Text content"
            assert "print" in results[1]
            assert '"a": 1' in results[2]


# ============================================================================
# EXTENSION HELPER TESTS
# ============================================================================


class TestGetExtensionHelper:
    """Test the _get_extension helper function."""

    def test_normal_extension(self):
        """Test normal file extension extraction."""
        assert _get_extension("/path/to/file.txt") == "txt"
        assert _get_extension("/file.json") == "json"
        assert _get_extension("/path/script.py") == "py"

    def test_uppercase_extension(self):
        """Test uppercase extension normalization."""
        assert _get_extension("/FILE.TXT") == "txt"
        assert _get_extension("/Data.JSON") == "json"

    def test_no_extension(self):
        """Test file without extension."""
        assert _get_extension("/README") == ""
        assert _get_extension("/file") == ""

    def test_special_files(self):
        """Test special files like Makefile, Dockerfile."""
        assert _get_extension("/Makefile") == "makefile"
        assert _get_extension("/Dockerfile") == "dockerfile"
        assert _get_extension("/path/Vagrantfile") == "vagrantfile"

    def test_hidden_files(self):
        """Test hidden files like .gitignore."""
        assert _get_extension("/.gitignore") == "gitignore"
        assert _get_extension("/.env") == "env"
        assert _get_extension("/.dockerignore") == "dockerignore"

    def test_multiple_dots(self):
        """Test files with multiple dots."""
        assert _get_extension("/archive.tar.gz") == "gz"
        assert _get_extension("/file.test.txt") == "txt"


# ============================================================================
# SUPPORTED EXTENSIONS TEST
# ============================================================================


class TestSupportedExtensions:
    """Test that all expected extensions are supported."""

    def test_text_extensions_contains_common_formats(self):
        """Verify common text formats are in TEXT_EXTENSIONS."""
        common_formats = [
            "txt",
            "json",
            "csv",
            "py",
            "md",
            "xml",
            "yaml",
            "yml",
            "js",
            "ts",
            "html",
            "css",
            "java",
            "c",
            "cpp",
            "go",
            "rs",
            "rb",
            "php",
            "sh",
            "sql",
            "toml",
            "ini",
            "log",
        ]

        for ext in common_formats:
            assert ext in TEXT_EXTENSIONS, f"Extension '{ext}' should be supported"
