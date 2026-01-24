"""Unit tests for add_content_text tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.add_content_text import add_content_text
from tools.create_document import create_document
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_content_added,
    assert_document_created,
    assert_error,
    assert_success,
    assert_text_in_document,
)


class TestAddContentText:
    """Test content addition functionality."""

    @pytest.mark.asyncio
    async def test_add_text_after_paragraph(self, mock_docs_root, sample_basic_content):
        """Test adding text after a paragraph."""
        create_result = await create_document(
            directory="/", file_name="add_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "add_test.docx")

        result = await add_content_text(
            "/add_test.docx", "body.p.1", "This is new added text", position="end"
        )
        assert_content_added(result)

        read_result = await read_document_content("/add_test.docx")
        assert_text_in_document(read_result, "This is new added text")

    @pytest.mark.asyncio
    async def test_add_text_before_paragraph(self, mock_docs_root):
        """Test adding text at start of paragraph."""
        content = [{"type": "paragraph", "text": "Original text"}]
        create_result = await create_document(
            directory="/", file_name="add_before.docx", content=content
        )
        assert_document_created(create_result, "add_before.docx")

        result = await add_content_text(
            "/add_before.docx", "body.p.0", "Prefix: ", position="start"
        )
        assert_content_added(result)

        read_result = await read_document_content("/add_before.docx")
        assert_text_in_document(read_result, "Prefix:")

    @pytest.mark.asyncio
    async def test_add_text_to_run(self, mock_docs_root):
        """Test adding text to a specific run."""
        content = [{"type": "paragraph", "text": "Run text"}]
        create_result = await create_document(
            directory="/", file_name="add_run.docx", content=content
        )
        assert_document_created(create_result, "add_run.docx")

        # Add to run at end
        result = await add_content_text(
            "/add_run.docx", "body.p.0.r.0", " added", position="end"
        )
        assert_content_added(result)

    @pytest.mark.asyncio
    async def test_add_text_to_run_start(self, mock_docs_root):
        """Test adding text at start of run."""
        content = [{"type": "paragraph", "text": "End text"}]
        create_result = await create_document(
            directory="/", file_name="add_run_start.docx", content=content
        )
        assert_document_created(create_result, "add_run_start.docx")

        result = await add_content_text(
            "/add_run_start.docx", "body.p.0.r.0", "Start ", position="before"
        )
        assert_content_added(result)

    @pytest.mark.asyncio
    async def test_add_text_to_cell(self, mock_docs_root):
        """Test adding text to table cell."""
        content = [
            {
                "type": "table",
                "rows": [["Cell content"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="add_cell.docx", content=content
        )
        assert_document_created(create_result, "add_cell.docx")

        result = await add_content_text(
            "/add_cell.docx", "body.tbl.0.r.0.c.0", " appended", position="end"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_add_text_to_cell_start(self, mock_docs_root):
        """Test adding text at start of cell."""
        content = [
            {
                "type": "table",
                "rows": [["Original"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="add_cell_start.docx", content=content
        )
        assert_document_created(create_result, "add_cell_start.docx")

        result = await add_content_text(
            "/add_cell_start.docx", "body.tbl.0.r.0.c.0", "Prefix: ", position="start"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_add_text_to_paragraph_without_runs(self, mock_docs_root):
        """Test adding text to paragraph - creates run if missing."""
        content = [{"type": "paragraph", "text": "Existing text"}]
        create_result = await create_document(
            directory="/", file_name="add_empty.docx", content=content
        )
        assert_document_created(create_result, "add_empty.docx")

        # Add to the paragraph (will use existing run)
        result = await add_content_text(
            "/add_empty.docx", "body.p.0", " New content", position="end"
        )
        assert_success(result)

    @pytest.mark.asyncio
    async def test_add_text_invalid_position(self, mock_docs_root):
        """Test adding text with invalid position."""
        content = [{"type": "paragraph", "text": "Test"}]
        create_result = await create_document(
            directory="/", file_name="add_invalid_pos.docx", content=content
        )
        assert_document_created(create_result, "add_invalid_pos.docx")

        result = await add_content_text(
            "/add_invalid_pos.docx", "body.p.0", "Text", position="invalid"
        )
        assert_error(result, "Position must be")

    @pytest.mark.asyncio
    async def test_add_text_nonexistent_document(self, mock_docs_root):
        """Test adding text to non-existent document."""
        result = await add_content_text("/nonexistent.docx", "body.p.0", "New text")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_add_text_invalid_path(self, mock_docs_root):
        """Test adding text with invalid path."""
        result = await add_content_text("no_slash.docx", "body.p.0", "New text")
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_add_text_wrong_extension(self, mock_docs_root):
        """Test adding text with wrong extension."""
        result = await add_content_text("/file.txt", "body.p.0", "New text")
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_add_text_empty_file_path(self, mock_docs_root):
        """Test adding text with empty file path."""
        result = await add_content_text("", "body.p.0", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_add_text_empty_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding text with empty identifier."""
        file_path, _ = sample_document_basic
        result = await add_content_text(file_path, "", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_add_text_whitespace_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding text with whitespace identifier."""
        file_path, _ = sample_document_basic
        result = await add_content_text(file_path, "   ", "New text")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_add_text_invalid_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding text with invalid identifier."""
        file_path, _ = sample_document_basic
        result = await add_content_text(file_path, "invalid.id", "New text")
        assert_error(result)

    @pytest.mark.asyncio
    async def test_add_text_not_a_file(self, mock_docs_root):
        """Test adding text when path is a directory."""
        import os

        dir_path = os.path.join(mock_docs_root, "test_dir.docx")
        os.makedirs(dir_path, exist_ok=True)

        result = await add_content_text("/test_dir.docx", "body.p.0", "New text")
        assert_error(result, "Not a file")
