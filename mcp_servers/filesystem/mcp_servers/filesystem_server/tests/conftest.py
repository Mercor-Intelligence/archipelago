"""Test configuration for filesystem_server.

This module provides:
- Path setup for imports
- Common fixtures for all tests
- Mock helpers
- Test data factories
"""

import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

# Add the server root to Python path so we can import tools, utils, etc.
server_root = Path(__file__).parent.parent
sys.path.insert(0, str(server_root))

# Test data directory
FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ============================================================================
# PATH AND DIRECTORY FIXTURES
# ============================================================================


@pytest.fixture
def temp_workspace() -> Generator[Path]:
    """Provide a temporary workspace directory.

    Yields:
        Path: Temporary directory path that's cleaned up after test
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def empty_directory(temp_workspace: Path) -> Path:
    """Provide an empty directory.

    Returns:
        Path: Path to an empty directory
    """
    empty_dir = temp_workspace / "empty"
    empty_dir.mkdir()
    return empty_dir


@pytest.fixture
def directory_with_files(temp_workspace: Path) -> Path:
    """Provide a directory with various files.

    Creates:
        - file1.txt (10 bytes)
        - file2.pdf (20 bytes)
        - file3.py (15 bytes)

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "with_files"
    test_dir.mkdir()

    (test_dir / "file1.txt").write_text("Test file 1")  # 11 bytes
    (test_dir / "file2.pdf").write_bytes(b"PDF content test data")  # 21 bytes
    (test_dir / "file3.py").write_text("print('hello')")  # 15 bytes

    return test_dir


@pytest.fixture
def directory_with_folders(temp_workspace: Path) -> Path:
    """Provide a directory with subdirectories.

    Creates:
        - folder1/
        - folder2/
        - folder3/

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "with_folders"
    test_dir.mkdir()

    (test_dir / "folder1").mkdir()
    (test_dir / "folder2").mkdir()
    (test_dir / "folder3").mkdir()

    return test_dir


@pytest.fixture
def directory_with_mixed_content(temp_workspace: Path) -> Path:
    """Provide a directory with both files and folders.

    Creates:
        - file1.txt
        - file2.json
        - folder1/
        - folder2/

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "mixed"
    test_dir.mkdir()

    (test_dir / "file1.txt").write_text("File 1")
    (test_dir / "file2.json").write_text('{"key": "value"}')
    (test_dir / "folder1").mkdir()
    (test_dir / "folder2").mkdir()

    return test_dir


@pytest.fixture
def directory_with_unicode_names(temp_workspace: Path) -> Path:
    """Provide a directory with Unicode filenames.

    Creates files with names in different languages.

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "unicode"
    test_dir.mkdir()

    # Chinese
    (test_dir / "文件.txt").write_text("Chinese file")
    # Russian
    (test_dir / "файл.pdf").write_text("Russian file")
    # Japanese
    (test_dir / "ファイル.json").write_text("Japanese file")
    # Arabic
    (test_dir / "ملف.txt").write_text("Arabic file")
    # Emoji
    (test_dir / "📄document.txt").write_text("Emoji file")

    return test_dir


@pytest.fixture
def directory_with_special_chars(temp_workspace: Path) -> Path:
    """Provide a directory with special characters in filenames.

    Creates files with various special characters.

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "special_chars"
    test_dir.mkdir()

    (test_dir / "file with spaces.txt").write_text("Spaces")
    (test_dir / "file-with-dashes.txt").write_text("Dashes")
    (test_dir / "file_with_underscores.txt").write_text("Underscores")
    (test_dir / "file(with)parens.txt").write_text("Parens")
    (test_dir / "file[with]brackets.txt").write_text("Brackets")

    return test_dir


@pytest.fixture
def directory_with_various_sizes(temp_workspace: Path) -> Path:
    """Provide a directory with files of various sizes.

    Creates:
        - empty.txt (0 bytes)
        - small.txt (10 bytes)
        - medium.txt (1000 bytes)
        - large.bin (10000 bytes)

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "sizes"
    test_dir.mkdir()

    # Empty file
    (test_dir / "empty.txt").write_text("")

    # Small file
    (test_dir / "small.txt").write_text("Small file")

    # Medium file
    (test_dir / "medium.txt").write_text("x" * 1000)

    # Large file
    (test_dir / "large.bin").write_bytes(b"L" * 10000)

    return test_dir


@pytest.fixture
def directory_with_various_mime_types(temp_workspace: Path) -> Path:
    """Provide a directory with files of various MIME types.

    Creates files with different extensions.

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "mime_types"
    test_dir.mkdir()

    # Common text formats
    (test_dir / "document.txt").write_text("Text document")
    (test_dir / "script.py").write_text("print('hello')")
    (test_dir / "webpage.html").write_text("<html></html>")
    (test_dir / "style.css").write_text("body { margin: 0; }")
    (test_dir / "script.js").write_text("console.log('hi');")

    # Data formats
    (test_dir / "data.json").write_text('{"key": "value"}')
    (test_dir / "data.xml").write_text("<root></root>")
    (test_dir / "data.csv").write_text("a,b,c\n1,2,3")

    # Binary formats (just create with some content)
    (test_dir / "document.pdf").write_bytes(b"PDF content")
    (test_dir / "archive.zip").write_bytes(b"ZIP content")

    # No extension
    (test_dir / "README").write_text("No extension")

    # Unknown extension
    (test_dir / "custom.xyz").write_text("Unknown type")

    # Uppercase extension
    (test_dir / "UPPERCASE.TXT").write_text("Uppercase ext")

    # Multiple dots
    (test_dir / "archive.tar.gz").write_bytes(b"Gzip archive")

    return test_dir


@pytest.fixture
def nested_directory_structure(temp_workspace: Path) -> Path:
    """Provide a deeply nested directory structure.

    Creates:
        level1/level2/level3/level4/deep.txt

    Returns:
        Path: Path to the root directory
    """
    nested = temp_workspace / "nested"
    nested.mkdir()

    current = nested
    for i in range(1, 5):
        current = current / f"level{i}"
        current.mkdir()

    (current / "deep.txt").write_text("Deep file")

    return nested


@pytest.fixture
def large_directory(temp_workspace: Path) -> Path:
    """Provide a directory with many files (100+).

    Creates 150 files for performance testing.

    Returns:
        Path: Path to the directory
    """
    test_dir = temp_workspace / "large"
    test_dir.mkdir()

    for i in range(150):
        (test_dir / f"file{i:03d}.txt").write_text(f"File number {i}")

    return test_dir


# ============================================================================
# DATA FIXTURES
# ============================================================================


@pytest.fixture
def sample_data():
    """Provide common test data constants."""
    return {
        "unicode_text": "你好 Ñoño Français 日本語",
        "special_chars": "<>&\"' `~!@#$%^&*()",
        "long_text": "A" * 10000,
        "empty_string": "",
        "whitespace": "   ",
    }


@pytest.fixture
def unicode_test_cases():
    """Various Unicode test cases."""
    return [
        ("Chinese", "你好世界"),
        ("Japanese", "日本語のテキスト"),
        ("Arabic", "مرحبا بالعالم"),
        ("Russian", "Привет мир"),
        ("Emoji", "👍 🎉 ✨"),
        ("Mixed", "Hello 世界 🌍"),
    ]
