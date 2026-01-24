"""Unit tests for create_document tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.create_document import create_document

from .helpers.assertions import assert_document_created, assert_error


class TestCreateDocument:
    """Test document creation functionality."""

    @pytest.mark.asyncio
    async def test_create_basic_document(self, mock_docs_root, sample_basic_content):
        """Test creating a basic document."""
        result = await create_document(
            directory="/", file_name="test.docx", content=sample_basic_content
        )
        assert_document_created(result, "test.docx")

    @pytest.mark.asyncio
    async def test_create_document_in_subdirectory(
        self, mock_docs_root, sample_basic_content
    ):
        """Test creating document in subdirectory."""
        result = await create_document(
            directory="/reports", file_name="report.docx", content=sample_basic_content
        )
        assert_document_created(result, "report.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_metadata(
        self, mock_docs_root, sample_basic_content, sample_metadata
    ):
        """Test creating document with metadata."""
        result = await create_document(
            directory="/",
            file_name="meta.docx",
            content=sample_basic_content,
            metadata=sample_metadata,
        )
        assert_document_created(result, "meta.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_table(
        self, mock_docs_root, sample_complex_content
    ):
        """Test creating document with table."""
        result = await create_document(
            directory="/", file_name="table.docx", content=sample_complex_content
        )
        assert_document_created(result, "table.docx")

    @pytest.mark.asyncio
    async def test_create_document_invalid_directory(
        self, mock_docs_root, sample_basic_content
    ):
        """Test creating document with invalid directory."""
        result = await create_document(
            directory="no_slash", file_name="test.docx", content=sample_basic_content
        )
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_create_document_invalid_filename(
        self, mock_docs_root, sample_basic_content
    ):
        """Test creating document with invalid filename."""
        result = await create_document(
            directory="/", file_name="test.txt", content=sample_basic_content
        )
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_create_document_empty_content(self, mock_docs_root):
        """Test creating document with empty content."""
        result = await create_document(
            directory="/", file_name="empty.docx", content=[]
        )
        assert_error(result, "content")

    @pytest.mark.asyncio
    async def test_create_document_filename_with_slash(
        self, mock_docs_root, sample_basic_content
    ):
        """Test creating document with slash in filename."""
        result = await create_document(
            directory="/", file_name="sub/test.docx", content=sample_basic_content
        )
        assert_error(result, "cannot contain")

    @pytest.mark.asyncio
    async def test_create_document_overwrites_existing(
        self, mock_docs_root, sample_basic_content
    ):
        """Test that creating document overwrites existing file."""
        # Create first document
        result1 = await create_document(
            directory="/", file_name="overwrite.docx", content=sample_basic_content
        )
        assert_document_created(result1, "overwrite.docx")

        # Create again with same name
        new_content = [{"type": "paragraph", "text": "New content"}]
        result2 = await create_document(
            directory="/", file_name="overwrite.docx", content=new_content
        )
        assert_document_created(result2, "overwrite.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_page_break(self, mock_docs_root):
        """Test creating document with page break."""
        content = [
            {"type": "paragraph", "text": "Page 1 content"},
            {"type": "page_break"},
            {"type": "paragraph", "text": "Page 2 content"},
        ]
        result = await create_document(
            directory="/", file_name="page_break.docx", content=content
        )
        assert_document_created(result, "page_break.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_section_break(self, mock_docs_root):
        """Test creating document with section break (creates multi-section document)."""
        content = [
            {"type": "paragraph", "text": "Section 1 content"},
            {"type": "section_break", "start_type": "new_page"},
            {"type": "paragraph", "text": "Section 2 content"},
        ]
        result = await create_document(
            directory="/", file_name="section_break.docx", content=content
        )
        assert_document_created(result, "section_break.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_continuous_section_break(self, mock_docs_root):
        """Test creating document with continuous section break."""
        content = [
            {"type": "paragraph", "text": "Section 1"},
            {"type": "section_break", "start_type": "continuous"},
            {"type": "paragraph", "text": "Section 2"},
        ]
        result = await create_document(
            directory="/", file_name="continuous_section.docx", content=content
        )
        assert_document_created(result, "continuous_section.docx")

    @pytest.mark.asyncio
    async def test_create_document_with_invalid_section_break_type(
        self, mock_docs_root
    ):
        """Test creating document with invalid section break type."""
        content = [
            {"type": "paragraph", "text": "Content"},
            {"type": "section_break", "start_type": "invalid_type"},
        ]
        result = await create_document(
            directory="/", file_name="invalid_section.docx", content=content
        )
        assert_error(result, "Invalid section start_type")

    @pytest.mark.asyncio
    async def test_create_multi_section_document(self, mock_docs_root):
        """Test creating a document with multiple sections."""
        content = [
            {"type": "heading", "text": "Section 1", "level": 1},
            {"type": "paragraph", "text": "Content for section 1"},
            {"type": "section_break", "start_type": "new_page"},
            {"type": "heading", "text": "Section 2", "level": 1},
            {"type": "paragraph", "text": "Content for section 2"},
            {"type": "section_break", "start_type": "odd_page"},
            {"type": "heading", "text": "Section 3", "level": 1},
            {"type": "paragraph", "text": "Content for section 3"},
        ]
        result = await create_document(
            directory="/", file_name="multi_section.docx", content=content
        )
        assert_document_created(result, "multi_section.docx")
