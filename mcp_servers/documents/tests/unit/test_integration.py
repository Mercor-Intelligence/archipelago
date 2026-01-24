"""Integration tests for docs operations."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.create_document import create_document
from tools.delete_document import delete_document
from tools.edit_content_text import edit_content_text
from tools.get_document_overview import get_document_overview
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_document_created,
    assert_document_deleted,
    assert_document_read,
    assert_edit_applied,
    assert_error,
    assert_heading_count,
    assert_text_in_document,
)


class TestIntegration:
    """Test integrated docs operations."""

    @pytest.mark.asyncio
    async def test_create_read_workflow(self, mock_docs_root, sample_basic_content):
        """Test create → read workflow."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="workflow.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "workflow.docx")

        # Read it
        read_result = await read_document_content("/workflow.docx")
        assert_document_read(read_result)
        assert_text_in_document(read_result, "Test Document")

    @pytest.mark.asyncio
    async def test_create_overview_workflow(
        self, mock_docs_root, sample_complex_content
    ):
        """Test create → overview workflow."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="overview.docx", content=sample_complex_content
        )
        assert_document_created(create_result, "overview.docx")

        # Get overview
        overview_result = await get_document_overview("/overview.docx")
        assert_heading_count(overview_result, 2)  # "Report" and "Data Analysis"

    @pytest.mark.asyncio
    async def test_create_edit_read_workflow(
        self, mock_docs_root, sample_basic_content
    ):
        """Test create → edit → read workflow."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="edit_flow.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "edit_flow.docx")

        # Edit paragraph
        edit_result = await edit_content_text(
            "/edit_flow.docx", "body.p.1", "Edited content"
        )
        assert_edit_applied(edit_result, "body.p.1", "Edited content")

        # Read and verify
        read_result = await read_document_content("/edit_flow.docx")
        assert_text_in_document(read_result, "Edited content")

    @pytest.mark.asyncio
    async def test_create_delete_workflow(self, mock_docs_root, sample_basic_content):
        """Test create → delete workflow."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="delete_flow.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "delete_flow.docx")

        # Delete it
        delete_result = await delete_document("/delete_flow.docx")
        assert_document_deleted(delete_result, "/delete_flow.docx")

        # Try to read (should fail)
        read_result = await read_document_content("/delete_flow.docx")
        assert_error(read_result, "not found")
