"""Unit tests for apply_formatting tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.apply_formatting import apply_formatting
from tools.create_document import create_document

from .helpers.assertions import (
    assert_document_created,
    assert_error,
    assert_formatting_applied,
)


class TestApplyFormatting:
    """Test formatting application functionality."""

    @pytest.mark.asyncio
    async def test_apply_bold_formatting(self, mock_docs_root, sample_basic_content):
        """Test applying bold formatting."""
        # Create document
        create_result = await create_document(
            directory="/", file_name="format_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "format_test.docx")

        # Apply bold to a run
        result = await apply_formatting("/format_test.docx", "body.p.0.r.0", bold=True)
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_italic_formatting(self, mock_docs_root, sample_basic_content):
        """Test applying italic formatting."""
        create_result = await create_document(
            directory="/", file_name="italic_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "italic_test.docx")

        result = await apply_formatting(
            "/italic_test.docx", "body.p.0.r.0", italic=True
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_underline_formatting(
        self, mock_docs_root, sample_basic_content
    ):
        """Test applying underline formatting."""
        create_result = await create_document(
            directory="/", file_name="underline_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "underline_test.docx")

        result = await apply_formatting(
            "/underline_test.docx", "body.p.1.r.0", underline=True
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_strikethrough_formatting(self, mock_docs_root):
        """Test applying strikethrough formatting."""
        content = [{"type": "paragraph", "text": "Strike this text"}]
        create_result = await create_document(
            directory="/", file_name="strike_test.docx", content=content
        )
        assert_document_created(create_result, "strike_test.docx")

        result = await apply_formatting(
            "/strike_test.docx", "body.p.0.r.0", strikethrough=True
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_font_size(self, mock_docs_root):
        """Test applying font size."""
        content = [{"type": "paragraph", "text": "Resize this text"}]
        create_result = await create_document(
            directory="/", file_name="fontsize_test.docx", content=content
        )
        assert_document_created(create_result, "fontsize_test.docx")

        result = await apply_formatting(
            "/fontsize_test.docx", "body.p.0.r.0", font_size=14.5
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_font_color(self, mock_docs_root):
        """Test applying font color."""
        content = [{"type": "paragraph", "text": "Color this text"}]
        create_result = await create_document(
            directory="/", file_name="fontcolor_test.docx", content=content
        )
        assert_document_created(create_result, "fontcolor_test.docx")

        result = await apply_formatting(
            "/fontcolor_test.docx", "body.p.0.r.0", font_color="FF0000"
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_font_color_with_hash(self, mock_docs_root):
        """Test applying font color with # prefix."""
        content = [{"type": "paragraph", "text": "Color with hash"}]
        create_result = await create_document(
            directory="/", file_name="color_hash.docx", content=content
        )
        assert_document_created(create_result, "color_hash.docx")

        result = await apply_formatting(
            "/color_hash.docx", "body.p.0.r.0", font_color="#00FF00"
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_font_color_invalid(self, mock_docs_root):
        """Test applying invalid font color."""
        content = [{"type": "paragraph", "text": "Invalid color"}]
        create_result = await create_document(
            directory="/", file_name="color_invalid.docx", content=content
        )
        assert_document_created(create_result, "color_invalid.docx")

        result = await apply_formatting(
            "/color_invalid.docx", "body.p.0.r.0", font_color="invalid"
        )
        assert_error(result, "6-hex")

    @pytest.mark.asyncio
    async def test_apply_multiple_formats(self, mock_docs_root, sample_basic_content):
        """Test applying multiple formats at once."""
        create_result = await create_document(
            directory="/", file_name="multi_format.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "multi_format.docx")

        result = await apply_formatting(
            "/multi_format.docx",
            "body.p.1.r.0",
            bold=True,
            italic=True,
            underline=True,
            strikethrough=False,
            font_size=12,
            font_color="0000FF",
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_formatting_to_paragraph(self, mock_docs_root):
        """Test applying formatting to entire paragraph (all runs)."""
        content = [{"type": "paragraph", "text": "Format entire paragraph"}]
        create_result = await create_document(
            directory="/", file_name="para_format.docx", content=content
        )
        assert_document_created(create_result, "para_format.docx")

        # Apply to paragraph (not run) - should affect all runs
        result = await apply_formatting("/para_format.docx", "body.p.0", bold=True)
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_formatting_to_cell(self, mock_docs_root):
        """Test applying formatting to table cell."""
        content = [
            {
                "type": "table",
                "rows": [["Cell 1", "Cell 2"], ["Data 1", "Data 2"]],
            }
        ]
        create_result = await create_document(
            directory="/", file_name="cell_format.docx", content=content
        )
        assert_document_created(create_result, "cell_format.docx")

        # Apply formatting to cell
        result = await apply_formatting(
            "/cell_format.docx", "body.tbl.0.r.0.c.0", bold=True, italic=True
        )
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_formatting_to_paragraph_without_explicit_run(
        self, mock_docs_root
    ):
        """Test applying formatting to paragraph - creates run if needed."""
        content = [{"type": "paragraph", "text": "Format this paragraph"}]
        create_result = await create_document(
            directory="/", file_name="format_para.docx", content=content
        )
        assert_document_created(create_result, "format_para.docx")

        # Apply formatting to paragraph (affects all runs)
        result = await apply_formatting("/format_para.docx", "body.p.0", bold=True)
        assert_formatting_applied(result)

    @pytest.mark.asyncio
    async def test_apply_formatting_nonexistent_document(self, mock_docs_root):
        """Test applying formatting to non-existent document."""
        result = await apply_formatting("/nonexistent.docx", "body.p.0.r.0", bold=True)
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_apply_formatting_invalid_path(self, mock_docs_root):
        """Test applying formatting with invalid path."""
        result = await apply_formatting("no_slash.docx", "body.p.0.r.0", bold=True)
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_apply_formatting_wrong_extension(self, mock_docs_root):
        """Test applying formatting with wrong extension."""
        result = await apply_formatting("/file.txt", "body.p.0.r.0", bold=True)
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_apply_formatting_empty_file_path(self, mock_docs_root):
        """Test applying formatting with empty file path."""
        result = await apply_formatting("", "body.p.0.r.0", bold=True)
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_apply_formatting_empty_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test applying formatting with empty identifier."""
        file_path, _ = sample_document_basic
        result = await apply_formatting(file_path, "", bold=True)
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_apply_formatting_whitespace_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test applying formatting with whitespace identifier."""
        file_path, _ = sample_document_basic
        result = await apply_formatting(file_path, "   ", bold=True)
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_apply_formatting_invalid_identifier(
        self, mock_docs_root, sample_document_basic
    ):
        """Test applying formatting with invalid identifier."""
        file_path, _ = sample_document_basic
        result = await apply_formatting(file_path, "invalid.id", bold=True)
        assert_error(result)

    @pytest.mark.asyncio
    async def test_apply_formatting_not_a_file(self, mock_docs_root):
        """Test applying formatting when path is a directory."""
        import os

        dir_path = os.path.join(mock_docs_root, "test_dir.docx")
        os.makedirs(dir_path, exist_ok=True)

        result = await apply_formatting("/test_dir.docx", "body.p.0.r.0", bold=True)
        assert_error(result, "Not a file")
