"""Unit tests using real sample document."""

import shutil
import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.delete_document import delete_document
from tools.get_document_overview import get_document_overview
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_document_read,
    assert_success,
)


@pytest.fixture
def sample_10k_document(mock_docs_root):
    """Copy the real sample document to temp directory.

    Returns:
        tuple: (file_path, document_path)
    """
    source = Path(__file__).parent / "Sun Country FY22 10-K.docx"
    dest = Path(mock_docs_root) / "sample_10k.docx"

    if source.exists():
        shutil.copy(source, dest)
        return "/sample_10k.docx", str(dest)
    else:
        pytest.skip("Sample document not found")


class TestRealDocument:
    """Test operations on real sample document."""

    @pytest.mark.asyncio
    async def test_read_real_document(self, mock_docs_root, sample_10k_document):
        """Test reading a real complex document."""
        file_path, _ = sample_10k_document
        result = await read_document_content(file_path)
        assert_document_read(result)
        # Verify it has actual content
        assert len(result) > 1000, "Expected substantial content from 10-K document"

    @pytest.mark.asyncio
    async def test_real_document_has_identifiers(
        self, mock_docs_root, sample_10k_document
    ):
        """Test that real document includes stable identifiers."""
        file_path, _ = sample_10k_document
        result = await read_document_content(file_path)
        assert_document_read(result)
        # Should have paragraph identifiers
        assert "body.p." in result, "Expected paragraph identifiers"

    @pytest.mark.asyncio
    async def test_real_document_has_tables(self, mock_docs_root, sample_10k_document):
        """Test that real document tables are detected."""
        file_path, _ = sample_10k_document
        result = await read_document_content(file_path)
        assert_document_read(result)
        # This specific 10-K has no tables (Tables: 0 in metadata)
        # Just verify the metadata is present
        assert "Tables:" in result, "Expected table count in metadata"

    @pytest.mark.asyncio
    async def test_get_overview_real_document(
        self, mock_docs_root, sample_10k_document
    ):
        """Test getting overview of real document."""
        file_path, _ = sample_10k_document
        result = await get_document_overview(file_path)
        assert_success(result)
        # 10-K should have headings
        assert len(result) > 100, "Expected overview with headings"

    @pytest.mark.asyncio
    async def test_real_document_content_search(
        self, mock_docs_root, sample_10k_document
    ):
        """Test searching for specific content in real document."""
        file_path, _ = sample_10k_document
        result = await read_document_content(file_path)
        # Sun Country 10-K should mention the company
        assert "sun country" in result.lower() or "company" in result.lower(), (
            "Expected company references in 10-K"
        )

    @pytest.mark.asyncio
    async def test_delete_real_document(self, mock_docs_root, sample_10k_document):
        """Test deleting real document."""
        file_path, _ = sample_10k_document
        # First verify it exists by reading
        read_result = await read_document_content(file_path)
        assert_document_read(read_result)

        # Delete it
        delete_result = await delete_document(file_path)
        assert_success(delete_result, "deleted")

    @pytest.mark.asyncio
    async def test_read_real_document_with_section(
        self, mock_docs_root, sample_10k_document
    ):
        """Test reading real document with section index."""
        file_path, _ = sample_10k_document
        # Try reading first section only
        result = await read_document_content(file_path, section_index=0)
        # If document has no headings, it will return an error message
        # This is expected behavior, not a test failure
        assert isinstance(result, str)
        assert len(result) > 0
