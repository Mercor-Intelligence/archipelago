"""Unit tests for get_document_overview tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.get_document_overview import get_document_overview

from .helpers.assertions import assert_error, assert_heading_count, assert_success


class TestGetDocumentOverview:
    """Test document overview functionality."""

    @pytest.mark.asyncio
    async def test_get_overview_basic_document(
        self, mock_docs_root, sample_document_basic
    ):
        """Test getting overview of basic document."""
        file_path, _ = sample_document_basic
        result = await get_document_overview(file_path)
        assert_success(result)
        assert_heading_count(result, 1)
        assert "Test Document" in result

    @pytest.mark.asyncio
    async def test_get_overview_nonexistent_document(self, mock_docs_root):
        """Test getting overview of non-existent document."""
        result = await get_document_overview("/nonexistent.docx")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_get_overview_invalid_path(self, mock_docs_root):
        """Test getting overview with invalid path."""
        result = await get_document_overview("no_slash.docx")
        assert_error(result, "must start with")
