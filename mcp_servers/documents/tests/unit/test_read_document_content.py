"""Unit tests for read_document_content tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_document_read,
    assert_error,
    assert_table_in_document,
    assert_text_in_document,
)


class TestReadDocumentContent:
    """Test document reading functionality."""

    @pytest.mark.asyncio
    async def test_read_basic_document(self, mock_docs_root, sample_document_basic):
        """Test reading a basic document."""
        file_path, _ = sample_document_basic
        result = await read_document_content(file_path)
        assert_document_read(result)
        assert_text_in_document(result, "Test Document")
        assert_text_in_document(result, "This is a test paragraph")

    @pytest.mark.asyncio
    async def test_read_document_with_table(
        self, mock_docs_root, sample_document_with_table
    ):
        """Test reading document with table."""
        file_path, _ = sample_document_with_table
        result = await read_document_content(file_path)
        assert_document_read(result)
        assert_table_in_document(result)
        assert_text_in_document(result, "Header 1")

    @pytest.mark.asyncio
    async def test_read_nonexistent_document(self, mock_docs_root):
        """Test reading non-existent document."""
        result = await read_document_content("/nonexistent.docx")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_read_document_invalid_path(self, mock_docs_root):
        """Test reading with invalid path."""
        result = await read_document_content("no_slash.docx")
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_read_document_wrong_extension(self, mock_docs_root):
        """Test reading file with wrong extension."""
        result = await read_document_content("/test.txt")
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_read_document_has_identifiers(
        self, mock_docs_root, sample_document_basic
    ):
        """Test that read document includes stable identifiers."""
        file_path, _ = sample_document_basic
        result = await read_document_content(file_path)
        assert_document_read(result)
        # Should have paragraph identifiers
        assert "body.p." in result, f"No paragraph identifiers found in: {result}"
