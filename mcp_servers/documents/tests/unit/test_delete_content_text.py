"""Unit tests for delete_content_text tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.create_document import create_document
from tools.delete_content_text import delete_content_text
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_content_deleted,
    assert_document_created,
    assert_error,
    assert_success,
)


class TestDeleteContentText:
    """Test content deletion functionality."""

    @pytest.mark.asyncio
    async def test_delete_paragraph_content(self, mock_docs_root, sample_basic_content):
        """Test deleting paragraph content (default scope)."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="delete_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "delete_test.docx")

        # Read to verify content exists
        read_before = await read_document_content("/delete_test.docx")
        assert "This is a test paragraph" in read_before

        # Delete paragraph content (body.p.1)
        result = await delete_content_text(
            "/delete_test.docx", "body.p.1", scope="content"
        )
        assert_content_deleted(result, "body.p.1")

        # Verify deletion - text should be gone
        read_after = await read_document_content("/delete_test.docx")
        assert "This is a test paragraph" not in read_after

    @pytest.mark.asyncio
    async def test_delete_paragraph_element(self, mock_docs_root):
        """Test deleting paragraph element (remove entire paragraph)."""
        # Create document with multiple paragraphs
        content = [
            {"type": "paragraph", "text": "First paragraph"},
            {"type": "paragraph", "text": "Second paragraph to delete"},
            {"type": "paragraph", "text": "Third paragraph"},
        ]
        create_result = await create_document(
            directory="/", file_name="delete_element.docx", content=content
        )
        assert_document_created(create_result, "delete_element.docx")

        # Delete second paragraph element
        result = await delete_content_text(
            "/delete_element.docx", "body.p.1", scope="element"
        )
        assert_success(result)

        # Verify deletion - paragraph should be removed
        read_after = await read_document_content("/delete_element.docx")
        assert "Second paragraph to delete" not in read_after
        assert "First paragraph" in read_after
        assert "Third paragraph" in read_after

    @pytest.mark.asyncio
    async def test_delete_run_content(self, mock_docs_root):
        """Test deleting run content."""
        content = [{"type": "paragraph", "text": "This has some text"}]
        create_result = await create_document(
            directory="/", file_name="delete_run.docx", content=content
        )
        assert_document_created(create_result, "delete_run.docx")

        # Delete run content
        result = await delete_content_text(
            "/delete_run.docx", "body.p.0.r.0", scope="content"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_delete_run_element(self, mock_docs_root):
        """Test deleting run element entirely."""
        content = [{"type": "paragraph", "text": "Text to remove"}]
        create_result = await create_document(
            directory="/", file_name="delete_run_el.docx", content=content
        )
        assert_document_created(create_result, "delete_run_el.docx")

        # Delete run element
        result = await delete_content_text(
            "/delete_run_el.docx", "body.p.0.r.0", scope="element"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_delete_cell_content(self, mock_docs_root):
        """Test deleting cell content in table."""
        content = [
            {
                "type": "table",
                "rows": [
                    ["Header1", "Header2"],
                    ["Data1", "Data2"],
                ],
                "header": True,
            }
        ]
        create_result = await create_document(
            directory="/", file_name="delete_cell.docx", content=content
        )
        assert_document_created(create_result, "delete_cell.docx")

        # Delete cell content
        result = await delete_content_text(
            "/delete_cell.docx", "body.tbl.0.r.1.c.0", scope="content"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_delete_cell_content_collapse_whitespace(self, mock_docs_root):
        """Test deleting cell content with collapse_whitespace."""
        content = [
            {
                "type": "table",
                "rows": [["Cell with content"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="delete_collapse.docx", content=content
        )
        assert_document_created(create_result, "delete_collapse.docx")

        # Delete with collapse_whitespace
        result = await delete_content_text(
            "/delete_collapse.docx",
            "body.tbl.0.r.0.c.0",
            scope="content",
            collapse_whitespace=True,
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_delete_cell_element_fails(self, mock_docs_root):
        """Test that deleting cell element fails (not supported)."""
        content = [
            {
                "type": "table",
                "rows": [["Cell content"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="delete_cell_el.docx", content=content
        )
        assert_document_created(create_result, "delete_cell_el.docx")

        # Try to delete cell element (should fail)
        result = await delete_content_text(
            "/delete_cell_el.docx", "body.tbl.0.r.0.c.0", scope="element"
        )
        assert_error(result, "cannot be deleted")

    @pytest.mark.asyncio
    async def test_delete_invalid_scope(self, mock_docs_root, sample_basic_content):
        """Test deleting with invalid scope value."""
        create_result = await create_document(
            directory="/", file_name="invalid_scope.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "invalid_scope.docx")

        result = await delete_content_text(
            "/invalid_scope.docx", "body.p.0", scope="invalid"
        )
        assert_error(result, "must be")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document(self, mock_docs_root):
        """Test deleting from non-existent document."""
        result = await delete_content_text("/nonexistent.docx", "body.p.0")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_delete_invalid_path(self, mock_docs_root):
        """Test deleting with invalid path."""
        result = await delete_content_text("no_slash.docx", "body.p.0")
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_delete_wrong_extension(self, mock_docs_root):
        """Test deleting with wrong file extension."""
        result = await delete_content_text("/file.txt", "body.p.0")
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_delete_empty_identifier(self, mock_docs_root, sample_document_basic):
        """Test deleting with empty identifier."""
        file_path, _ = sample_document_basic
        result = await delete_content_text(file_path, "")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_delete_whitespace_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test deleting with whitespace-only identifier."""
        file_path, _ = sample_document_basic
        result = await delete_content_text(file_path, "   ")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_delete_invalid_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test deleting with invalid identifier."""
        file_path, _ = sample_document_basic
        result = await delete_content_text(file_path, "invalid.id")
        assert_error(result)

    @pytest.mark.asyncio
    async def test_delete_empty_file_path(self, mock_docs_root):
        """Test deleting with empty file path."""
        result = await delete_content_text("", "body.p.0")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_delete_not_a_file(self, mock_docs_root):
        """Test deleting when path is a directory."""
        # Create a directory
        import os

        dir_path = os.path.join(mock_docs_root, "test_dir.docx")
        os.makedirs(dir_path, exist_ok=True)

        result = await delete_content_text("/test_dir.docx", "body.p.0")
        assert_error(result, "Not a file")
