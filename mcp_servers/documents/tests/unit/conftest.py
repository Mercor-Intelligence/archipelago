"""Shared fixtures for docs server tests."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from docx import Document

# Add mcp_servers/docs_server to path for imports
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)


@pytest.fixture
def temp_docs_root(tmp_path):
    """Create a temporary docs data directory.

    Args:
        tmp_path: pytest's temporary directory fixture

    Yields:
        Path: Path to the temporary docs directory
    """
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    yield docs_dir


@pytest.fixture
def mock_docs_root(temp_docs_root, monkeypatch):
    """Mock the DOCS_ROOT to point to temp directory.

    Args:
        temp_docs_root: Temporary docs directory fixture
        monkeypatch: pytest's monkeypatch fixture

    Yields:
        str: Path to the temporary docs directory as string
    """
    # Mock environment variable so all tools pick it up
    monkeypatch.setenv("APP_DOCS_ROOT", str(temp_docs_root))
    monkeypatch.setenv("APP_FS_ROOT", str(temp_docs_root))

    # Also patch the DOCS_ROOT constant in each tool module that has it
    with patch("tools.create_document.DOCS_ROOT", str(temp_docs_root)):
        with patch("tools.read_document_content.DOCS_ROOT", str(temp_docs_root)):
            with patch("tools.get_document_overview.DOCS_ROOT", str(temp_docs_root)):
                with patch("tools.edit_content_text.DOCS_ROOT", str(temp_docs_root)):
                    with patch("tools.add_content_text.DOCS_ROOT", str(temp_docs_root)):
                        with patch(
                            "tools.delete_content_text.DOCS_ROOT",
                            str(temp_docs_root),
                        ):
                            with patch(
                                "tools.apply_formatting.DOCS_ROOT",
                                str(temp_docs_root),
                            ):
                                with patch(
                                    "tools.delete_document.DOCS_ROOT",
                                    str(temp_docs_root),
                                ):
                                    with patch(
                                        "tools.add_image.DOCS_ROOT",
                                        str(temp_docs_root),
                                    ):
                                        with patch(
                                            "tools.modify_image.DOCS_ROOT",
                                            str(temp_docs_root),
                                        ):
                                            with patch(
                                                "tools.comments.DOCS_ROOT",
                                                str(temp_docs_root),
                                            ):
                                                with patch(
                                                    "tools.header_footer.DOCS_ROOT",
                                                    str(temp_docs_root),
                                                ):
                                                    with patch(
                                                        "tools.page_orientation.DOCS_ROOT",
                                                        str(temp_docs_root),
                                                    ):
                                                        with patch(
                                                            "tools.page_margins.DOCS_ROOT",
                                                            str(temp_docs_root),
                                                        ):
                                                            yield str(temp_docs_root)


@pytest.fixture
def sample_basic_content():
    """Return basic document content."""
    return [
        {"type": "heading", "text": "Test Document", "level": 1},
        {"type": "paragraph", "text": "This is a test paragraph."},
        {"type": "bullet_list", "items": ["Item 1", "Item 2", "Item 3"]},
    ]


@pytest.fixture
def sample_complex_content():
    """Return complex document content with tables."""
    return [
        {"type": "heading", "text": "Report", "level": 1},
        {"type": "paragraph", "text": "Executive summary goes here.", "bold": True},
        {"type": "heading", "text": "Data Analysis", "level": 2},
        {
            "type": "table",
            "rows": [["Name", "Value"], ["A", "100"], ["B", "200"]],
            "header": True,
        },
        {"type": "numbered_list", "items": ["First point", "Second point"]},
    ]


@pytest.fixture
def sample_document_basic(mock_docs_root, sample_basic_content):
    """Create a basic test document.

    Returns:
        tuple: (file_path, document_path)
    """
    file_path = "/test_doc.docx"
    full_path = Path(mock_docs_root) / "test_doc.docx"

    doc = Document()
    doc.add_heading("Test Document", level=1)
    doc.add_paragraph("This is a test paragraph.")
    doc.save(str(full_path))

    return file_path, str(full_path)


@pytest.fixture
def sample_document_with_table(mock_docs_root):
    """Create a document with a table.

    Returns:
        tuple: (file_path, document_path)
    """
    file_path = "/table_doc.docx"
    full_path = Path(mock_docs_root) / "table_doc.docx"

    doc = Document()
    doc.add_heading("Document with Table", level=1)
    doc.add_paragraph("Table below:")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Header 1"
    table.cell(0, 1).text = "Header 2"
    table.cell(1, 0).text = "Data 1"
    table.cell(1, 1).text = "Data 2"

    doc.save(str(full_path))

    return file_path, str(full_path)


@pytest.fixture
def sample_metadata():
    """Return sample document metadata."""
    return {
        "title": "Test Document",
        "subject": "Testing",
        "author": "Test User",
        "comments": "This is a test document",
    }
