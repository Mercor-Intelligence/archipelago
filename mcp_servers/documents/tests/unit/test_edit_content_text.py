"""Unit tests for edit_content_text tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.create_document import create_document
from tools.edit_content_text import edit_content_text
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_document_created,
    assert_edit_applied,
    assert_error,
    assert_success,
    assert_text_in_document,
)


class TestEditContentText:
    """Test content editing functionality."""

    @pytest.mark.asyncio
    async def test_edit_paragraph_text(self, mock_docs_root, sample_basic_content):
        """Test editing paragraph text."""
        create_result = await create_document(
            directory="/", file_name="edit_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "edit_test.docx")

        _ = await read_document_content("/edit_test.docx")
        identifier = "body.p.1"

        result = await edit_content_text(
            "/edit_test.docx", identifier, "Updated paragraph text"
        )
        assert_edit_applied(result, identifier, "Updated paragraph text")

        read_after = await read_document_content("/edit_test.docx")
        assert_text_in_document(read_after, "Updated paragraph text")

    @pytest.mark.asyncio
    async def test_edit_run_text(self, mock_docs_root):
        """Test editing specific run text."""
        content = [{"type": "paragraph", "text": "Original run text"}]
        create_result = await create_document(
            directory="/", file_name="edit_run.docx", content=content
        )
        assert_document_created(create_result, "edit_run.docx")

        result = await edit_content_text(
            "/edit_run.docx", "body.p.0.r.0", "New run text"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_edit_table_cell_text(self, mock_docs_root):
        """Test editing table cell text."""
        content = [
            {
                "type": "table",
                "rows": [["Cell1", "Cell2"], ["Data1", "Data2"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="edit_cell.docx", content=content
        )
        assert_document_created(create_result, "edit_cell.docx")

        result = await edit_content_text(
            "/edit_cell.docx", "body.tbl.0.r.0.c.0", "Updated Cell"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_edit_heading_text(self, mock_docs_root, sample_basic_content):
        """Test editing heading text."""
        create_result = await create_document(
            directory="/", file_name="edit_heading.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "edit_heading.docx")

        result = await edit_content_text(
            "/edit_heading.docx", "body.p.0", "New Heading Text"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_edit_nonexistent_document(self, mock_docs_root):
        """Test editing non-existent document."""
        result = await edit_content_text("/nonexistent.docx", "body.p.0", "New text")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_edit_invalid_path(self, mock_docs_root):
        """Test editing with invalid path (no leading slash)."""
        result = await edit_content_text("no_slash.docx", "body.p.0", "New text")
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_edit_wrong_extension(self, mock_docs_root):
        """Test editing with wrong file extension."""
        result = await edit_content_text("/file.txt", "body.p.0", "New text")
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_edit_empty_file_path(self, mock_docs_root):
        """Test editing with empty file path."""
        result = await edit_content_text("", "body.p.0", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_edit_invalid_identifier(self, mock_docs_root, sample_document_basic):
        """Test editing with invalid identifier."""
        file_path, _ = sample_document_basic
        result = await edit_content_text(file_path, "invalid.id", "New text")
        assert_error(result)

    @pytest.mark.asyncio
    async def test_edit_empty_identifier(self, mock_docs_root, sample_document_basic):
        """Test editing with empty identifier."""
        file_path, _ = sample_document_basic
        result = await edit_content_text(file_path, "", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_edit_whitespace_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test editing with whitespace-only identifier."""
        file_path, _ = sample_document_basic
        result = await edit_content_text(file_path, "   ", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_edit_not_a_file(self, mock_docs_root):
        """Test editing when path is a directory."""
        import os

        dir_path = os.path.join(mock_docs_root, "test_dir.docx")
        os.makedirs(dir_path, exist_ok=True)

        result = await edit_content_text("/test_dir.docx", "body.p.0", "New text")
        assert_error(result, "Not a file")

    @pytest.mark.asyncio
    async def test_edit_multiple_paragraphs(self, mock_docs_root):
        """Test editing multiple paragraphs in sequence."""
        content = [
            {"type": "paragraph", "text": "Paragraph 1"},
            {"type": "paragraph", "text": "Paragraph 2"},
            {"type": "paragraph", "text": "Paragraph 3"},
        ]
        create_result = await create_document(
            directory="/", file_name="multi_para.docx", content=content
        )
        assert_document_created(create_result, "multi_para.docx")

        # Edit each paragraph
        for i in range(3):
            result = await edit_content_text(
                "/multi_para.docx", f"body.p.{i}", f"Updated Paragraph {i + 1}"
            )
            assert_success(result)

    @pytest.mark.asyncio
    async def test_edit_empty_new_text(self, mock_docs_root, sample_basic_content):
        """Test editing with empty new text (clearing content)."""
        create_result = await create_document(
            directory="/", file_name="clear_text.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "clear_text.docx")

        result = await edit_content_text("/clear_text.docx", "body.p.1", "")
        assert_success(result)
